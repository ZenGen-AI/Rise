import json

import pytest

from simple_actio.skills import Skill
from controlplane.gitstore import GitStore
from controlplane.paths import run_trace_file
from inference_bridge import BridgeError, ScriptedBridge
from mindagent.analysis import BehaviorFinding
from mindagent.errors import MindAgentDataError, ProposalParseError
from mindagent.proposals import (
    BRIDGE_GENERATOR, CANDIDATE_PRIORITY, RULE_GENERATOR, ProposalDraft, attempt_task_id, parse_strategy, propose,
)


def _finding(signal="emotion_high", fid=None):
    return BehaviorFinding(id=fid or f"finding-{signal}-abc", signal=signal, pattern=f"p-{signal}",
                           support=3, ratio=0.75, episode_refs=("e0", "e1", "e2"))


GOOD = json.dumps({"instructions": ["先安抚对方", "再谈问题"], "description": "情绪优先"}, ensure_ascii=False)


def test_no_findings_no_drafts():
    assert propose([]) == []


def test_the_rule_template_makes_one_skill_that_targets_the_signal_and_reads_back():
    (d,) = propose([_finding()])
    assert isinstance(d, ProposalDraft) and d.finding_id == "finding-emotion_high-abc"
    assert d.generator == RULE_GENERATOR
    (inc,) = d.increments
    assert inc["kind"] == "skill"
    skill = Skill.from_dict(inc["definition"])
    assert skill.trigger == {"signal": "emotion_high"}
    assert skill.priority == CANDIDATE_PRIORITY and skill.level == "micro" and skill.version == 1
    assert skill.policy_instructions == ("先回应对方的情绪,确认对方的感受,再谈具体问题",)
    assert d.skill_definitions() == [inc["definition"]]


def test_each_finding_gets_its_own_draft_in_order_and_unknown_signals_get_a_generic_text():
    drafts = propose([_finding("no_new_information"), _finding("custom_signal")])
    assert [d.finding_id for d in drafts] == ["finding-no_new_information-abc", "finding-custom_signal-abc"]
    assert Skill.from_dict(drafts[1].increments[0]["definition"]).policy_instructions == (
        "信号 custom_signal 为真时,先处理它指向的状况",)


def test_two_findings_of_one_signal_are_refused_before_any_bridge_call():
    bridge = ScriptedBridge([GOOD, GOOD])
    with pytest.raises(MindAgentDataError, match="distinct signals"):
        propose([_finding("emotion_high", "f1"), _finding("emotion_high", "f2")], bridge=bridge, run_id="run-003")
    assert bridge.calls == []
    with pytest.raises(MindAgentDataError, match="distinct signals"):
        propose([_finding("emotion_high", "f1"), _finding("emotion_high", "f2")])


def test_with_a_bridge_the_model_writes_the_policy_text(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    bridge = ScriptedBridge([GOOD], store=store)
    (d,) = propose([_finding()], bridge=bridge, run_id="run-003", task_id="agent-zengen-v1-propose")
    assert d.generator == BRIDGE_GENERATOR
    definition = d.increments[0]["definition"]
    assert definition["policy"] == {"instructions": ["先安抚对方", "再谈问题"]}
    assert definition["description"] == "情绪优先"
    assert definition["trigger"] == {"signal": "emotion_high"}  # 触发条件不由外部模型决定
    ((prompt, run_id, task_id),) = bridge.calls
    assert (run_id, task_id) == ("run-003", "agent-zengen-v1-propose-1-attempt1")
    assert "emotion_high" in prompt and "只输出一个 JSON 对象" in prompt
    records = store.read_product(run_trace_file("run-003")).payload["cost_records"]
    assert [(r["task_id"], r["cost_type"], r["calls"]) for r in records] == [
        ("agent-zengen-v1-propose-1-attempt1", "external_inference", 1)]


def test_every_attempt_gets_its_own_task_id_and_its_own_cost_record(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    bridge = ScriptedBridge(["x", "y", GOOD, GOOD], store=store)
    propose([_finding("emotion_high", "f1"), _finding("no_new_information", "f2")],
            bridge=bridge, run_id="run-003", task_id="t")
    assert [c[2] for c in bridge.calls] == ["t-1-attempt1", "t-1-attempt2", "t-1-attempt3", "t-2-attempt1"]
    records = store.read_product(run_trace_file("run-003")).payload["cost_records"]
    assert [r["task_id"] for r in records] == ["t-1-attempt1", "t-1-attempt2", "t-1-attempt3", "t-2-attempt1"]
    assert attempt_task_id("t-1", 2) == "t-1-attempt2"


def test_a_bridge_without_a_run_id_is_rejected_before_any_call():
    bridge = ScriptedBridge([GOOD])
    with pytest.raises(MindAgentDataError, match="non-empty run_id"):
        propose([_finding()], bridge=bridge)
    assert bridge.calls == []
    assert len(propose([_finding()])) == 1  # 不用桥接时 run_id 可以缺省


def test_a_parse_failure_is_retried_with_the_error_in_the_prompt():
    bridge = ScriptedBridge(["not json", GOOD])
    (d,) = propose([_finding()], bridge=bridge, run_id="run-003")
    assert len(bridge.calls) == 2
    assert "上一次的输出不合格" in bridge.calls[1][0] and "not a single JSON object" in bridge.calls[1][0]
    assert d.increments[0]["definition"]["policy"]["instructions"] == ["先安抚对方", "再谈问题"]


def test_parse_retries_are_bounded_and_end_in_a_deterministic_error():
    bridge = ScriptedBridge(["x", "[]", '{"instructions": []}'])
    with pytest.raises(ProposalParseError) as exc:
        propose([_finding()], bridge=bridge, run_id="run-003", max_parse_retries=2)
    assert exc.value.attempts == 3 and "non-empty list" in exc.value.reason
    assert bridge.remaining == 0


def test_zero_parse_retries_means_one_call():
    bridge = ScriptedBridge(["x", GOOD])
    with pytest.raises(ProposalParseError):
        propose([_finding()], bridge=bridge, run_id="run-003", max_parse_retries=0)
    assert len(bridge.calls) == 1


@pytest.mark.parametrize("category", ["T1", "T2"])
def test_a_bridge_error_propagates_unchanged(category):
    err = BridgeError(category, "boom", attempts=3)
    with pytest.raises(BridgeError) as exc:
        propose([_finding()], bridge=ScriptedBridge([err]), run_id="run-003")
    assert exc.value is err


@pytest.mark.parametrize("text,msg", [
    ("", "not a single JSON object"),
    ('{"instructions": ["a"]} trailing', "not a single JSON object"),
    ('["a"]', "must be a JSON object"),
    ('{"instructions": ["a"], "trigger": {}}', "unknown keys"),
    ('{"instructions": "a"}', "non-empty list"),
    ('{"instructions": ["  "]}', "non-empty list"),
    ('{"instructions": ["a"], "description": 3}', "description must be a string"),
])
def test_parse_strategy_is_strict(text, msg):
    with pytest.raises(ValueError, match=msg):
        parse_strategy(text)


def test_parse_strategy_accepts_surrounding_whitespace_and_a_missing_description():
    assert parse_strategy('  {"instructions": [" a "]}\n') == (["a"], "")


def test_max_parse_retries_is_validated():
    with pytest.raises(ValueError):
        propose([], max_parse_retries=-1)
