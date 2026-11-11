import pytest

from mindtrainer.datamodel import TrajectoryBatch
from mindtrainer.reward_pipeline import (
    AttributionError, JudgeAttribution, MockTurnJudge, OutcomeSplitAttribution,
    SelfEvalBiasError, build_attribution_backend, combine, run_reward_pipeline,
    summarize_rewards, validate_attribution,
)
from mindtrainer.training_config import RewardConfig
from simulator.dataset import episode_group_id
from simulator.datamodel import Episode, RoleAssignment, RoleBinding, TurnRecord


def _episode(utterances, ep_id="ep-1", opponent_ref="zengen-v1", thoughts=None):
    """a(被训练,self_play)与 b(冻结对手)交替发言;a 说偶数轮,b 说奇数轮。"""
    assignment = RoleAssignment(
        "s1",
        [RoleBinding("a", "pa", "self_play"),
         RoleBinding("b", "pb", "frozen_reference", model_ref=opponent_ref)],
        max_turns=len(utterances),
    )
    thoughts = thoughts or {}
    turns = [
        TurnRecord(i, "a" if i % 2 == 0 else "b", u, inner_thought=thoughts.get(i))
        for i, u in enumerate(utterances)
    ]
    return Episode(ep_id, "s1", assignment, turns, "max_turns")


def _rule_cfg(**kw):
    base = dict(
        components=["goal"], attribution="outcome_split", combination="sum",
        outcome_rules={"goal": ["agreed"]}, gamma=1.0,
    )
    base.update(kw)
    return RewardConfig(**base)


def _judge_cfg(**kw):
    base = dict(
        components=["goal"], attribution="judge", combination="sum",
        judge_model_id="external-judge", rubric_version="r1",
    )
    base.update(kw)
    return RewardConfig(**base)


class _ConstJudge:
    def __init__(self, value, model_id="external-judge"):
        self.model_id = model_id
        self._value = value

    def score_turn(self, episode, turn_index, component):
        return self._value


# ---------------- 规则归因 ----------------

def test_a_hit_scores_one_on_every_trained_turn_when_gamma_is_one():
    ep = _episode(["we agreed", "ok", "fine", "ok"])
    scores = OutcomeSplitAttribution({"goal": ["agreed"]}).attribute(ep, "a", ["goal"])
    assert scores == {"goal": [1.0, None, 1.0, None]}


def test_gamma_below_one_makes_later_trained_turns_score_higher():
    ep = _episode(["agreed", "x", "y", "x", "z", "x"])
    scores = OutcomeSplitAttribution({"goal": ["agreed"]}, gamma=0.5).attribute(ep, "a", ["goal"])
    assert scores["goal"] == [0.25, None, 0.5, None, 1.0, None]


def test_no_hit_scores_zero_on_trained_turns_and_none_elsewhere():
    ep = _episode(["hello", "ok", "bye", "ok"])
    scores = OutcomeSplitAttribution({"goal": ["agreed"]}).attribute(ep, "a", ["goal"])
    assert scores == {"goal": [0.0, None, 0.0, None]}


def test_a_keyword_only_the_opponent_says_does_not_count():
    ep = _episode(["hello", "we agreed", "bye", "agreed again"])
    scores = OutcomeSplitAttribution({"goal": ["agreed"]}).attribute(ep, "a", ["goal"])
    assert scores == {"goal": [0.0, None, 0.0, None]}


def test_a_keyword_only_in_the_inner_thought_does_not_count():
    ep = _episode(["hello", "ok", "bye", "ok"], thoughts={0: "agreed in my head"})
    scores = OutcomeSplitAttribution({"goal": ["agreed"]}).attribute(ep, "a", ["goal"])
    assert scores == {"goal": [0.0, None, 0.0, None]}


def test_any_one_of_several_keywords_is_enough():
    ep = _episode(["hello", "ok", "it is a deal", "ok"])
    scores = OutcomeSplitAttribution({"goal": ["agreed", "deal"]}).attribute(ep, "a", ["goal"])
    assert scores == {"goal": [1.0, None, 1.0, None]}


def test_matching_is_a_case_sensitive_substring_match():
    ep = _episode(["Agreed", "ok", "disagreed", "ok"])
    scores = OutcomeSplitAttribution({"goal": ["agreed"]}).attribute(ep, "a", ["goal"])
    assert scores == {"goal": [1.0, None, 1.0, None]}  # "disagreed" 含 "agreed";"Agreed" 不含
    ep_only_capital = _episode(["Agreed", "ok", "ok", "ok"])
    assert OutcomeSplitAttribution({"goal": ["agreed"]}).attribute(
        ep_only_capital, "a", ["goal"]
    ) == {"goal": [0.0, None, 0.0, None]}


def test_components_are_decided_independently():
    ep = _episode(["we agreed", "ok", "bye", "ok"])
    rules = {"goal": ["agreed"], "norm": ["please"]}
    scores = OutcomeSplitAttribution(rules).attribute(ep, "a", ["goal", "norm"])
    assert scores == {
        "goal": [1.0, None, 1.0, None],
        "norm": [0.0, None, 0.0, None],
    }


def test_the_trained_slot_can_be_the_one_that_speaks_second():
    ep = _episode(["x", "agreed", "y", "z"])
    scores = OutcomeSplitAttribution({"goal": ["agreed"]}).attribute(ep, "b", ["goal"])
    assert scores == {"goal": [None, 1.0, None, 1.0]}


def test_a_component_without_a_rule_is_an_error():
    with pytest.raises(AttributionError, match="no outcome rule for component 'norm'"):
        OutcomeSplitAttribution({"goal": ["agreed"]}).attribute(_episode(["x", "y"]), "a", ["norm"])


@pytest.mark.parametrize("gamma", [0.0, -0.1, 1.1, float("nan")])
def test_outcome_split_rejects_a_gamma_outside_zero_one(gamma):
    with pytest.raises(ValueError, match="gamma must be > 0 and <= 1"):
        OutcomeSplitAttribution({"goal": ["agreed"]}, gamma=gamma)


# ---------------- judge 归因 ----------------

def test_judge_is_called_only_for_trained_turns():
    class Recorder:
        model_id = "external-judge"

        def __init__(self):
            self.calls = []

        def score_turn(self, episode, turn_index, component):
            self.calls.append((turn_index, component))
            return 0.5

    recorder = Recorder()
    scores = JudgeAttribution(recorder, "zengen-v1").attribute(
        _episode(["a0", "b1", "a2", "b3"]), "a", ["goal", "norm"]
    )
    assert recorder.calls == [(0, "goal"), (2, "goal"), (0, "norm"), (2, "norm")]
    assert scores == {"goal": [0.5, None, 0.5, None], "norm": [0.5, None, 0.5, None]}


def test_mock_turn_judge_reads_its_table():
    judge = MockTurnJudge("external-judge", {("ep-1", 0, "goal"): 0.9, ("ep-1", 2, "goal"): 0.1})
    scores = JudgeAttribution(judge, "zengen-v1").attribute(_episode(["a", "b", "c", "d"]), "a", ["goal"])
    assert scores == {"goal": [0.9, None, 0.1, None]}


def test_mock_turn_judge_missing_key_is_loud():
    judge = MockTurnJudge("external-judge", {("ep-1", 0, "goal"): 0.9})
    with pytest.raises(KeyError):
        JudgeAttribution(judge, "zengen-v1").attribute(_episode(["a", "b", "c", "d"]), "a", ["goal"])


@pytest.mark.parametrize(
    "judge_id,trained_id",
    [
        ("zengen-v1", "zengen-v1"),          # 相同
        ("zengen", "zengen-v1-sft"),         # judge 是被训模型的前缀(祖先)
        ("zengen-v1-sft", "zengen-v1"),      # 被训模型是 judge 的前缀(后代)
    ],
    ids=["same", "judge_is_prefix", "trained_is_prefix"],
)
def test_self_eval_guard_rejects_near_relatives_in_both_directions(judge_id, trained_id):
    with pytest.raises(SelfEvalBiasError, match="self-eval bias"):
        JudgeAttribution(MockTurnJudge(judge_id, {}), trained_id)


def test_an_independent_judge_is_accepted():
    JudgeAttribution(MockTurnJudge("external-judge", {}), "zengen-v1")


@pytest.mark.parametrize("judge_id,trained_id", [("", "zengen-v1"), ("external-judge", "")])
def test_empty_model_ids_are_rejected_not_treated_as_a_prefix_of_everything(judge_id, trained_id):
    with pytest.raises(ValueError, match="non-empty"):
        JudgeAttribution(MockTurnJudge(judge_id, {}), trained_id)


# ---------------- 组装 ----------------

def test_build_outcome_split_backend_uses_the_configured_rules_and_gamma():
    backend = build_attribution_backend(_rule_cfg(gamma=0.5))
    assert isinstance(backend, OutcomeSplitAttribution)
    scores = backend.attribute(_episode(["agreed", "x", "y", "x"]), "a", ["goal"])
    assert scores["goal"] == [0.5, None, 1.0, None]


def test_build_judge_backend_requires_a_judge_that_matches_the_config():
    cfg = _judge_cfg()
    backend = build_attribution_backend(cfg, judge=_ConstJudge(0.5), trained_model_id="zengen-v1")
    assert isinstance(backend, JudgeAttribution)
    with pytest.raises(ValueError, match="requires a judge"):
        build_attribution_backend(cfg, trained_model_id="zengen-v1")
    with pytest.raises(ValueError, match="does not match"):
        build_attribution_backend(
            cfg, judge=_ConstJudge(0.5, model_id="another-judge"), trained_model_id="zengen-v1"
        )
    with pytest.raises(ValueError, match="trained_model_id"):
        build_attribution_backend(cfg, judge=_ConstJudge(0.5))


def test_build_judge_backend_still_applies_the_self_eval_guard():
    cfg = _judge_cfg(judge_model_id="zengen-v1-judge")
    with pytest.raises(SelfEvalBiasError):
        build_attribution_backend(
            cfg, judge=_ConstJudge(0.5, model_id="zengen-v1-judge"), trained_model_id="zengen-v1"
        )


def test_build_outcome_split_backend_refuses_a_stray_judge():
    with pytest.raises(ValueError, match="outcome_split"):
        build_attribution_backend(_rule_cfg(), judge=_ConstJudge(0.5))


# ---------------- 校验 ----------------

def _valid():
    return {"goal": [0.5, None, 1.0, None], "norm": [0.0, None, 0.25, None]}


def _set(name, index, value):
    def mutate(scores):
        scores[name][index] = value
    return mutate


INVALID = [
    ("missing_component", lambda s: s.pop("norm"), "components"),
    ("extra_component", lambda s: s.update(extra=[0.5, None, 0.5, None]), "components"),
    ("short_list", lambda s: s["goal"].pop(), "scores for"),
    ("long_list", lambda s: s["goal"].append(None), "scores for"),
    ("none_on_trained_turn", _set("goal", 0, None), "has no score"),
    ("value_on_opponent_turn", _set("goal", 1, 0.5), "must be None"),
    ("negative", _set("goal", 0, -0.1), "not a finite number in [0, 1]"),
    ("above_one", _set("goal", 0, 1.01), "not a finite number in [0, 1]"),
    ("nan", _set("goal", 0, float("nan")), "not a finite number in [0, 1]"),
    ("inf", _set("goal", 0, float("inf")), "not a finite number in [0, 1]"),
    ("string", _set("goal", 0, "0.5"), "not a finite number in [0, 1]"),
    ("bool", _set("goal", 0, True), "not a finite number in [0, 1]"),
]


def test_valid_attribution_passes_and_includes_the_boundaries_zero_and_one():
    validate_attribution(_episode(["a", "b", "c", "d"]), "a", ["goal", "norm"], _valid())


@pytest.mark.parametrize("mutate,needle", [(m, n) for _, m, n in INVALID], ids=[i for i, _, _ in INVALID])
def test_invalid_attribution_is_rejected_with_the_episode_id_and_reason(mutate, needle):
    scores = _valid()
    mutate(scores)
    with pytest.raises(AttributionError) as exc:
        validate_attribution(_episode(["a", "b", "c", "d"]), "a", ["goal", "norm"], scores)
    assert needle in str(exc.value)
    assert "ep-1" in str(exc.value)


def test_an_unbound_trained_slot_is_rejected():
    with pytest.raises(AttributionError, match="'ghost' is not bound"):
        validate_attribution(_episode(["a", "b", "c", "d"]), "ghost", ["goal", "norm"], _valid())


def test_an_episode_where_the_trained_slot_never_spoke_is_rejected_not_skipped():
    ep = _episode(["only a spoke"])  # 只有 a 说过话
    with pytest.raises(AttributionError, match="no turns from trained slot 'b'"):
        validate_attribution(ep, "b", ["goal"], {"goal": [None]})


# ---------------- 组合 ----------------

def test_sum_and_product_combine_per_turn_and_keep_none():
    scores = {"goal": [0.5, None, 1.0, None], "norm": [0.5, None, 0.25, None]}
    assert combine(scores, ["goal", "norm"], "sum") == [1.0, None, 1.25, None]
    assert combine(scores, ["goal", "norm"], "product") == [0.25, None, 0.25, None]


def test_combine_does_not_depend_on_dict_order():
    a = {"goal": [0.3, None], "norm": [0.6, None]}
    b = {"norm": [0.6, None], "goal": [0.3, None]}
    assert combine(a, ["goal", "norm"], "sum") == combine(b, ["goal", "norm"], "sum")


def test_a_single_component_combines_to_itself():
    scores = {"goal": [0.4, None, 0.9, None]}
    assert combine(scores, ["goal"], "sum") == [0.4, None, 0.9, None]
    assert combine(scores, ["goal"], "product") == [0.4, None, 0.9, None]


def test_combine_rejects_bad_inputs():
    with pytest.raises(ValueError, match="combination must be one of"):
        combine({"goal": [0.5]}, ["goal"], "avg")
    with pytest.raises(ValueError, match="different lengths"):
        combine({"goal": [0.5, None], "norm": [0.5]}, ["goal", "norm"], "sum")
    with pytest.raises(ValueError, match="disagree"):
        combine({"goal": [0.5, None], "norm": [None, None]}, ["goal", "norm"], "sum")
    with pytest.raises(ValueError, match="non-empty"):
        combine({}, [], "sum")


# ---------------- 管线入口 ----------------

def _run(episodes, cfg=None, backend=None, trained_slot="a"):
    cfg = cfg or _rule_cfg()
    backend = backend or OutcomeSplitAttribution(cfg.outcome_rules, cfg.gamma)
    return run_reward_pipeline(TrajectoryBatch(list(episodes)), cfg, backend, trained_slot=trained_slot)


def test_pipeline_attaches_group_id_aligned_rewards_and_the_mean():
    ep = _episode(["agreed", "x", "y", "x", "z", "x"])
    [out] = _run([ep], _rule_cfg(gamma=0.5))
    assert out.episode is ep
    assert out.group_id == episode_group_id(ep)
    assert out.per_turn_rewards == [0.25, None, 0.5, None, 1.0, None]
    assert out.combined_reward == pytest.approx((0.25 + 0.5 + 1.0) / 3)


def test_combined_reward_has_no_length_bias():
    short = _episode(["agreed", "x", "y", "x"], ep_id="short")
    long = _episode(["agreed", "x"] + ["y", "x"] * 4, ep_id="long")  # 5 个训练轮
    out = _run([short, long])
    assert [r.combined_reward for r in out] == [1.0, 1.0]  # 取总和的话 long 会是 5.0


def test_product_combination_gates_on_every_component():
    cfg = _rule_cfg(
        components=["goal", "norm"], combination="product",
        outcome_rules={"goal": ["agreed"], "norm": ["please"]},
    )
    both = _episode(["agreed, please", "x", "y", "x"], ep_id="both")
    only_goal = _episode(["agreed", "x", "y", "x"], ep_id="only_goal")
    out = _run([both, only_goal], cfg)
    assert [r.combined_reward for r in out] == [1.0, 0.0]


def test_pipeline_preserves_episode_order_and_handles_an_empty_batch():
    eps = [_episode(["agreed", "x"], ep_id=f"e{i}") for i in range(3)]
    assert [r.episode.id for r in _run(eps)] == ["e0", "e1", "e2"]
    assert _run([]) == []


def test_pipeline_with_a_judge_backend():
    judge = MockTurnJudge("external-judge", {("ep-1", 0, "goal"): 0.9, ("ep-1", 2, "goal"): 0.3})
    backend = JudgeAttribution(judge, "zengen-v1")
    [out] = _run([_episode(["a", "b", "c", "d"])], _judge_cfg(), backend)
    assert out.per_turn_rewards == [0.9, None, 0.3, None]
    assert out.combined_reward == pytest.approx(0.6)


def test_a_judge_missing_a_table_entry_surfaces_as_keyerror():
    backend = JudgeAttribution(MockTurnJudge("external-judge", {}), "zengen-v1")
    with pytest.raises(KeyError):
        _run([_episode(["a", "b", "c", "d"])], _judge_cfg(), backend)


@pytest.mark.parametrize("bad", [1.5, -0.2, float("nan"), float("inf")])
def test_a_judge_returning_an_unusable_score_is_caught_by_validation(bad):
    backend = JudgeAttribution(_ConstJudge(bad), "zengen-v1")
    with pytest.raises(AttributionError, match="ep-1"):
        _run([_episode(["a", "b", "c", "d"])], _judge_cfg(), backend)


def test_pipeline_rejects_an_episode_where_the_trained_slot_is_unbound():
    with pytest.raises(AttributionError, match="'ghost' is not bound"):
        _run([_episode(["agreed", "x"])], trained_slot="ghost")


# ---------------- 摘要 ----------------

def test_summarize_rewards_reports_mean_and_count():
    out = _run([_episode(["agreed", "x"], ep_id="hit"), _episode(["nope", "x"], ep_id="miss")])
    summary = summarize_rewards(out)
    assert summary == {"mean": 0.5, "n": 2}


def test_summarize_rewards_refuses_to_average_nothing():
    with pytest.raises(ValueError, match="zero trajectories"):
        summarize_rewards([])
