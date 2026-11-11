"""完整闭环与 MindAgent 端到端(任务 108):全部经 factory 的命令行与计划 3、5、7、8 的真实代码。

人工动作(批准、确认、驳回)用写状态模拟,与 tests/test_mt_wire.py 同一手法(TypeScript 命令行的等价操作)。
诊断一律是 mock 后端;mock SoMEval 恒满分,所以第一轮要有一个低分的基线,提名才有正增益:
run-000 是人工登记的基线(全错的 mock 报告 + 已晋级的 zengen-v1)。
"""
import sys

import pytest

from controlplane.config import write_config
from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.evidence import diagnostic_grade
from controlplane.paths import (
    agent_round_file, backends_config_file, candidate_checkpoint_file, dataset_manifest_file,
    diagnostic_report_file, episodes_dir, failure_record_file, library_state_file, model_file, nomination_file,
    planner_decision_file, proposal_file, recipe_candidate_file, recipe_track_note_file, run_dir,
    skill_file,
)
from diagnostic.evaluator import make_diagnostic_evaluator
from diagnostic.scorer import MockJudgeBackend
from diagnostic.someval_adapter import MockSoMEvalRunner
from factory.cli import main
from factory.defaults import DEFAULT_EVALUATOR_CONFIG, DEFAULT_ITEM_POOL
from factory_fixtures import closed_loop_repo, produce_datasets, write_directive
from mindagent.library import load_library
from recipestore.gain import compute_gain
from recipestore.library import find_recipes, get_recipe

DIM = "mental_representation"  # 默认分类表唯一的一级维度
LOOP = ["--allow-mock-evidence", "--planner-backend", "heuristic"]
GOAL = {"id": "skill-goal", "version": 1, "level": "macro", "priority": 0, "trigger": {"turn_at_least": 0},
        "policy": {"instructions": ["推进目标"]}, "description": "baseline: ignores emotion"}


def _env(id_, type_, status, payload, links=()):
    return Envelope(id=id_, type=type_, version=1, created_at="2026-10-09T00:00:00Z", created_by=OwnerRef(),
                    status=status, schema_version=1, links=list(links), payload=payload)


def _seed_baseline(store):
    """run-000:全错的 mock 报告 + 已晋级的 zengen-v1(带 checkpoint_ref,规划 new 时要用)。"""
    cfg = DEFAULT_EVALUATOR_CONFIG
    wrong = MockSoMEvalRunner(fixed={i.id: ["X"] * cfg["sampling_k"] for i in DEFAULT_ITEM_POOL.items})
    make_diagnostic_evaluator(
        taxonomy_version=cfg["taxonomy_version"], item_pool_version=cfg["item_pool_version"],
        someval_runner=wrong, judge_backend=MockJudgeBackend(model_id="j", fixed={}),
        benchmark_version=cfg["benchmark_version"], tier=cfg["tier"], sampling_k=cfg["sampling_k"],
    )(store, "run-000", "cand-run-000")
    ckpt = BigRef("file:///ckpt/v1", "1" * 64, 1)
    store.write_product(model_file(1), _env("zengen-v1", "zengen_model", "effective",
                        {"promoted_from": "cand-run-000", "checkpoint_ref": ckpt.to_dict()},
                        ["cand-run-000", "run-000"]), "human: register baseline zengen-v1")


@pytest.fixture
def store(tmp_path):
    s = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    _seed_baseline(s)
    produce_datasets(s, write_directive(s, "run-001", dimension=DIM))
    return s


def tick(store, enable, *extra, env=None):
    """一轮 factory reconcile --once,返回打印出的动作行。"""
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(["reconcile", str(store.repo_root), "--once", "--enable", enable, *extra],
                    env={} if env is None else env)
    assert code == 0
    out = buf.getvalue().splitlines()
    return [] if out == ["no actions"] else out


def set_status(store, rel, status, **payload):
    env = store.read_product(rel)
    env.status = status
    env.payload.update(payload)
    store.write_product(rel, env, f"human: {env.id} -> {status}")


def directive(store, run_id):
    return store.read_product(f"{run_dir(run_id)}/directive.yaml")


def train_and_promote(store, run_id, enable, *extra):
    """派发 → mock 诊断 → 待审 → 人工批准 → 晋级。返回每个 tick 的动作行。"""
    seen = [tick(store, enable, *extra) for _ in range(3)]
    set_status(store, candidate_checkpoint_file(run_id), "approved")
    seen.append(tick(store, enable, *extra))
    assert store.read_product(candidate_checkpoint_file(run_id)).status == "effective"
    return seen


def test_the_whole_loop_turns_twice(store, capsys):
    enable = "nominate,track,admit"
    assert train_and_promote(store, "run-001", enable, *LOOP) == [
        ["dispatch_directive run-001"], ["request_diagnostic cand-run-001"],
        ["await_review cand-run-001"], ["promote_candidate cand-run-001"],
    ]
    assert diagnostic_grade(store.read_product(diagnostic_report_file("run-001")).payload) == "mock"
    assert compute_gain(store, "run-001", DIM).gain > 0.01

    # 提名 → 配方候选(repro 在场) → 门③ → 收录
    assert tick(store, enable, *LOOP) == ["nominate_recipe run-001"]
    assert store.read_product(nomination_file("run-001")).payload["result"] == "nominated"
    cand = store.read_product(recipe_candidate_file("run-001"))
    assert cand.status == "pending_review"
    assert set(cand.payload["repro"]) == {"errors", "warnings"}
    assert cand.payload["code_version"]["repo_commit"] is not None or \
        any("repo_commit" in e for e in cand.payload["repro"]["errors"])
    set_status(store, recipe_candidate_file("run-001"), "approved")
    assert tick(store, enable, *LOOP) == ["admit_recipe recipe-run-001"]
    assert [v.id for v in find_recipes(store, dimension=DIM)] == ["recipe-run-001"]
    assert get_recipe(store, "recipe-run-001").status == "effective"

    # 打开规划:启发式后端 clone 刚收录的配方;指令先停在 awaiting_confirmation
    enable = "plan,nominate,track,admit"
    assert tick(store, enable, *LOOP) == ["plan_next_round run-001"]
    decision = store.read_product(planner_decision_file("run-001"))
    assert decision.status == "executed", decision.payload["failure"]
    assert (decision.payload["template_version"], decision.payload["next_run_id"]) == ("v1", "run-002")
    assert decision.payload["decision"]["action"] == "clone"
    assert decision.payload["decision"]["source_recipe_id"] == "recipe-run-001"
    d2 = directive(store, "run-002")
    assert (d2.status, d2.payload["recipe_ref"]) == ("awaiting_confirmation", "recipe-run-001")
    assert "data_spec" not in d2.payload  # clone 复用配方锚定的清单
    assert tick(store, enable, *LOOP) == []  # 等人确认,闭环不动

    # 模拟 rise confirm run-002 → 第二轮
    set_status(store, f"{run_dir('run-002')}/directive.yaml", "pending")
    train_and_promote(store, "run-002", enable, *LOOP)
    assert store.exists(model_file(3))
    # 第二轮用的是 run-001 的清单,没有为 run-002 合成
    assert not store.exists(dataset_manifest_file("run-002", "sft"))

    # 战绩回写到 recipe-run-001;提名因增益为 0 写 skipped 标记。规划要等提名标记(CP-11),这个 tick 还不规划
    assert tick(store, enable, *LOOP) == ["record_track run-002", "nominate_recipe run-002"]
    assert store.exists(recipe_track_note_file("run-002"))
    assert store.read_product(nomination_file("run-002")).payload["result"] == "skipped"
    assert not store.exists(planner_decision_file("run-002"))
    # 下一个 tick:提名标记在场(skipped 也放行,且没有配方候选),规划落在 run-002(最大的 run)
    assert tick(store, enable, *LOOP) == ["plan_next_round run-002"]
    assert directive(store, "run-003").status == "awaiting_confirmation"

    capsys.readouterr()
    assert main(["cost", str(store.repo_root), "run-001"]) == 0
    assert "None" not in capsys.readouterr().out


def test_a_new_round_without_confirmation_synthesizes_its_data(store):
    enable = "plan,track,admit"
    train_and_promote(store, "run-001", enable, *LOOP, "--no-confirm-directives")
    # 没开提名,规划不等配方入库门(CP-11);没有配方可 clone,启发式后端走 new batch
    assert tick(store, enable, *LOOP, "--no-confirm-directives") == ["plan_next_round run-001"]
    d2 = directive(store, "run-002")
    assert d2.status == "pending" and d2.payload["data_spec"]["purpose"] == "training"
    assert not store.exists(dataset_manifest_file("run-002", "sft"))
    assert tick(store, enable, *LOOP, "--no-confirm-directives") == ["dispatch_directive run-002"]
    assert store.exists(dataset_manifest_file("run-002", "sft"))
    assert store.exists(dataset_manifest_file("run-002", "grpo_opd"))
    assert store.read_product(candidate_checkpoint_file("run-002")).status == "pending_review"


def test_only_the_largest_run_is_planned(store):
    """R5:run-001 已晋级、run-002 被驳回,两者都满足规划条件;只有 run-002 触发。"""
    train_and_promote(store, "run-001", "track", *LOOP)
    produce_datasets(store, write_directive(store, "run-002", dimension=DIM))
    for _ in range(3):
        tick(store, "track", *LOOP)
    set_status(store, candidate_checkpoint_file("run-002"), "rejected", reject_reason="not convincing")
    assert tick(store, "plan", *LOOP) == ["plan_next_round run-002"]
    assert not store.exists(planner_decision_file("run-001"))
    assert store.read_product(planner_decision_file("run-002")).status == "executed"


def test_planning_waits_for_the_recipe_gate(store):
    """CP-11:启用提名时,规划要等来源 run 的提名标记,且配方候选离开 pending_review 与 approved。"""
    enable = "plan,nominate,admit"
    train_and_promote(store, "run-001", enable, *LOOP)
    # 晋级后的 tick:只提名;这个 tick 的快照里还没有提名标记,规划不出现
    assert tick(store, enable, *LOOP) == ["nominate_recipe run-001"]
    assert store.read_product(nomination_file("run-001")).payload["result"] == "nominated"
    assert store.read_product(recipe_candidate_file("run-001")).status == "pending_review"
    # 配方候选停在 pending_review:多个 tick 都不规划
    for _ in range(2):
        assert tick(store, enable, *LOOP) == []
    assert not store.exists(planner_decision_file("run-001"))
    # 人工批准后的 tick:只收录;快照里候选是 approved,规划仍被挡着
    set_status(store, recipe_candidate_file("run-001"), "approved")
    assert tick(store, enable, *LOOP) == ["admit_recipe recipe-run-001"]
    assert not store.exists(planner_decision_file("run-001"))
    # 候选已是 effective:下一个 tick 才规划,并且 clone 刚收录的配方
    assert tick(store, enable, *LOOP) == ["plan_next_round run-001"]
    d = store.read_product(planner_decision_file("run-001"))
    assert d.status == "executed", d.payload.get("failure")
    assert (d.payload["decision"]["action"], d.payload["decision"]["source_recipe_id"]) == ("clone", "recipe-run-001")


def test_a_bridge_failure_fails_the_decision_and_stops_the_loop(store, capsys):
    """R18:规划桥接用尽重试 → 决策 failed、T3 失败记录(带桥接日志),此后不再触发规划。"""
    write_config(store, backends_config_file(), "backends", {"planner": {
        "backend": "broken_cli", "invocation_form": "cli", "model_name": "m", "prompt_template_version": "v1",
        "token_env": None, "retry_policy": {"max_attempts": 1, "backoff_base_s": 0},
        "command": [sys.executable, "-c", "import sys; sys.stderr.write('tool down'); sys.exit(3)"],
    }}, "human: a planner backend that always fails")
    train_and_promote(store, "run-001", "plan", "--allow-mock-evidence")
    assert tick(store, "plan", "--allow-mock-evidence") == ["plan_next_round run-001"]
    d = store.read_product(planner_decision_file("run-001"))
    assert d.status == "failed" and d.payload["failure"]["category"] == "T3"
    assert d.payload["failure"]["reason"].startswith("bridge T1")
    rec = store.read_product(failure_record_file("run-001", "plan-run-001")).payload
    assert (rec["category"], rec["point"]) == ("T3", "trainscheduler.plan")
    assert len(rec["log_refs"]) == 1
    assert not store.exists(f"{run_dir('run-002')}/directive.yaml")
    assert tick(store, "plan", "--allow-mock-evidence") == []  # 循环停住,等人
    capsys.readouterr()
    assert main(["logs", str(store.repo_root), "run-001", "--task", "plan-run-001"]) == 0
    out = capsys.readouterr().out
    assert "log_ref [ok]" in out and "tool down" in out


def test_without_mock_evidence_the_loop_does_not_turn(store):
    enable = "plan,nominate,track,admit"
    train_and_promote(store, "run-001", enable, "--planner-backend", "heuristic")
    assert tick(store, enable, "--planner-backend", "heuristic") == []
    for rel in (nomination_file("run-001"), planner_decision_file("run-001"), recipe_candidate_file("run-001")):
        assert not store.exists(rel)


def test_the_mindagent_line_runs_inside_the_reconcile_loop(store):
    """计划 7 任务 88 的环路,但每一步都由 factory 装配的 handlers 经计划 8 的 reconcile_once 触发:
    空库轮次零提案 → 放基线技能 → 下一个模型的轮次产出提案 → 门② → 收录 → 库版本是字符串。"""
    enable = "agent,admit"
    train_and_promote(store, "run-001", enable)
    assert tick(store, enable) == ["start_agent_round zengen-v2"]
    r = store.read_product(agent_round_file("zengen-v2"))
    assert r.status == "executed" and r.payload["proposals"] == [] and r.payload["run_id"] == "run-001"
    assert r.payload["library_version"] == {"skill": "0", "memory": "0"}
    assert tick(store, enable) == []  # 同一模型不再开第二轮

    store.write_product(skill_file("skill-goal"), _env("skill-goal", "skill", "effective", GOAL), "human: skill")
    store.write_product(library_state_file(), _env("library-state", "library_state", "executed",
                        {"skill_version": "1", "memory_version": "0", "admitted": []}), "human: library")
    produce_datasets(store, write_directive(store, "run-002", dimension=DIM))
    train_and_promote(store, "run-002", enable)
    assert tick(store, enable) == ["start_agent_round zengen-v3"]
    r3 = store.read_product(agent_round_file("zengen-v3"))
    assert r3.status == "executed" and r3.payload["library_version"] == {"skill": "1", "memory": "0"}
    (pid,) = r3.payload["proposals"]
    proposal = store.read_product(proposal_file(pid))
    assert proposal.status == "pending_review" and proposal.payload["verification"]["method"] == "replay_behavior"

    set_status(store, proposal_file(pid), "approved")
    assert tick(store, enable) == [f"admit_proposal {pid}"]
    assert store.read_product(proposal_file(pid)).status == "effective"
    lib = load_library(store)
    assert lib.version == {"skill": "2", "memory": "0"}
    assert all(isinstance(v, str) for v in lib.version.values())
    assert tick(store, enable) == []


# ---- R19:start_agent_round 对 pending 轮次的重入(计划 7 留给本计划验证) ----

class _Flaky:
    """抛出之前构造的后端共用一个发言计数,第 fail_after 次发言抛 OSError(瞬态,原样上抛)并解除;
    之后构造的后端正常。部署驱动器每条 episode 新造一个实例(各带一个后端),所以计数要跨后端累计。"""

    def __init__(self, fail_after):
        self.fail_after = fail_after
        self.armed = True
        self.calls = 0

    def factory(self, model_id, ckpt):
        from factory.agent import CannedGuidedBackend
        inner = CannedGuidedBackend()
        if not self.armed:
            return inner
        outer = self

        class Backend:
            def respond_guided(self, *a):
                if outer.armed:
                    outer.calls += 1
                    if outer.calls >= outer.fail_after:
                        outer.armed = False
                        raise OSError("model host went away")
                return inner.respond_guided(*a)

        return Backend()


def _agent_ready(store):
    """晋级 zengen-v2,并放一条不处理情绪的基线技能,让这一轮一定产出提案。"""
    store.write_product(skill_file("skill-goal"), _env("skill-goal", "skill", "effective", GOAL), "human: skill")
    store.write_product(library_state_file(), _env("library-state", "library_state", "executed",
                        {"skill_version": "1", "memory_version": "0", "admitted": []}), "human: library")
    train_and_promote(store, "run-001", "agent")


def _episode_files(store, run_id):
    root = store.repo_root / episodes_dir(run_id)
    return sorted(p.name for p in root.glob("deploy-*.yaml")) if root.exists() else []


def _proposals_on_disk(store, model_id="zengen-v2"):
    """该模型轮次写出的提案 id(skill_memory_proposals/<id>/proposal.yaml)。
    基线 zengen-v1 的轮次在 train_and_promote 期间也会被触发,它的提案不计在内。"""
    root = (store.repo_root / proposal_file("x")).parent.parent
    prefix = f"proposal-agent-round-{model_id}-"
    return sorted(p.parent.name for p in root.glob(f"{prefix}*/proposal.yaml")) if root.exists() else []


def _agent_failures(store, run_id):
    root = store.repo_root / run_dir(run_id)
    return sorted(p.name for p in root.glob("failure-agent-*.yaml"))


def test_a_transient_failure_leaves_the_round_pending_and_the_next_tick_resumes(store, monkeypatch):
    from factory import handlers as fh
    _agent_ready(store)
    flaky = _Flaky(fail_after=5)  # 第一个情境 2 句、第二个情境 2 句,第三个情境的第一句抛
    monkeypatch.setattr(fh, "canned_backend_factory", flaky.factory)

    # 第 1 次:瞬态异常原样上抛,轮次 pending,没有失败记录,前两条 episode 已落盘
    with pytest.raises(OSError, match="model host went away"):
        tick(store, "agent")
    r = store.read_product(agent_round_file("zengen-v2"))
    assert r.status == "pending" and r.payload["proposals"] == []
    assert _agent_failures(store, "run-001") == []
    done = _episode_files(store, "run-001")
    assert len(done) == 2
    before = {n: (store.repo_root / episodes_dir("run-001") / n).read_bytes() for n in done}

    # 第 2 次:同一棵树、后端恢复;R19 再次触发,复用已落盘的 episode 续跑,轮次 executed
    assert tick(store, "agent") == ["start_agent_round zengen-v2"]
    r = store.read_product(agent_round_file("zengen-v2"))
    assert r.status == "executed"
    assert len(r.payload["episode_ids"]) == 3 and len(_episode_files(store, "run-001")) == 3
    assert {n: (store.repo_root / episodes_dir("run-001") / n).read_bytes() for n in done} == before
    (pid,) = r.payload["proposals"]
    assert pid == "proposal-agent-round-zengen-v2-1"
    assert _proposals_on_disk(store) == [pid]
    assert _agent_failures(store, "run-001") == []

    # 第 3 次:executed 挡住
    assert tick(store, "agent") == []


def test_a_crash_between_episodes_and_proposals_converges_on_the_next_tick(store, monkeypatch):
    """模拟进程在写完部署 episode 之后、写提案之前被杀:package 抛一次 KeyboardInterrupt 以外的异常。"""
    import mindagent.rounds as mr
    _agent_ready(store)
    real_package = mr.package
    calls = []

    def dying_package(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("killed before packaging")
        return real_package(*a, **kw)

    monkeypatch.setattr(mr, "package", dying_package)
    with pytest.raises(RuntimeError, match="killed before packaging"):
        tick(store, "agent")
    assert store.read_product(agent_round_file("zengen-v2")).status == "pending"
    episodes = _episode_files(store, "run-001")
    assert len(episodes) == 3 and _proposals_on_disk(store) == []
    before = {n: (store.repo_root / episodes_dir("run-001") / n).read_bytes() for n in episodes}

    assert tick(store, "agent") == ["start_agent_round zengen-v2"]
    r = store.read_product(agent_round_file("zengen-v2"))
    assert r.status == "executed" and len(calls) == 2
    assert {n: (store.repo_root / episodes_dir("run-001") / n).read_bytes() for n in episodes} == before
    assert _proposals_on_disk(store) == r.payload["proposals"] != []
    assert _agent_failures(store, "run-001") == []
    assert tick(store, "agent") == []


def test_an_unreadable_round_product_is_failed_by_the_handler_and_the_loop_moves_on(store):
    """计划 7 修订 + 计划 8 Ruling 20:轮次产物读不出时,视图按"没有轮次"再次触发 start_agent_round,
    由轮次处理器自己把它整份重写为 failed(T3),而不是每个 tick 抛异常让守护进程崩溃循环。"""
    train_and_promote(store, "run-001", "agent")
    rel = agent_round_file("zengen-v2")
    tick(store, "agent")  # 正常跑完一轮,产物 executed
    path = store.repo_root / rel
    path.write_text("<<<<<<< HEAD\nstatus: executed\n=======\nstatus: pending\n>>>>>>> other\n", encoding="utf-8")
    store._git("add", rel)
    store._git("commit", "-q", "-m", "human: botched merge of the round product")
    before = _agent_failures(store, "run-001")

    assert tick(store, "agent") == ["start_agent_round zengen-v2"]  # 没有异常逃出 tick()
    r = store.read_product(rel)
    assert r.status == "failed"
    assert (r.payload["failure"]["category"], r.payload["failure"]["reason"]) == ("T3", "unreadable round product")
    assert r.payload["model_id"] == "zengen-v2" and r.payload["run_id"] == "run-001"
    assert "<<<<<<<" not in path.read_text(encoding="utf-8")  # 整份重写,不是补丁
    assert _agent_failures(store, "run-001") == sorted(set(before) | {"failure-agent-zengen-v2.yaml"})
    assert len(_agent_failures(store, "run-001")) - len(before) == 1

    assert tick(store, "agent") == []  # failed 挡住


# ---- 任务 108 的测试缺口:repo_commit 两种状态分开钉、第二轮逐 tick 动作行、增益阈值来自配置、fail_after=5 依赖的罐头形状 ----

def _promote_and_nominate(store, *extra):
    enable = "nominate,track,admit"
    train_and_promote(store, "run-001", enable, *LOOP)
    return tick(store, enable, *LOOP, *extra)


def test_a_valid_repo_commit_reaches_the_recipe_candidate_and_is_not_a_repro_error(store):
    """补任务 108 的缺口:大循环用例里 repo_commit 的断言是析取式(有值,或 repro 里报了它),两种状态都能过。
    这里分开钉:传一个合法的短哈希,候选的 code_version 带着它,repro 错误里不出现 repo_commit。"""
    assert _promote_and_nominate(store, "--repo-commit", "c0ffee1") == ["nominate_recipe run-001"]
    cand = store.read_product(recipe_candidate_file("run-001"))
    assert cand.payload["code_version"] == {"repo_commit": "c0ffee1"}
    assert not any("repo_commit" in err for err in cand.payload["repro"]["errors"])


def test_an_invalid_repo_commit_is_kept_on_the_candidate_and_reported_as_a_repro_error(store):
    """补任务 108 的缺口(析取式断言的另一半):不是 7 到 40 位小写十六进制的值照样写进候选,但 repro 报错点名 repo_commit。"""
    assert _promote_and_nominate(store, "--repo-commit", "c0ffee") == ["nominate_recipe run-001"]
    cand = store.read_product(recipe_candidate_file("run-001"))
    assert cand.payload["code_version"] == {"repo_commit": "c0ffee"}
    assert any("code_version.repo_commit" in err for err in cand.payload["repro"]["errors"])


def test_the_second_round_runs_the_same_four_actions_one_per_tick(store):
    """补任务 108 的缺口:第二轮的逐 tick 动作行没有钉住(大循环用例丢掉了 train_and_promote 的返回值)。
    规划后第二条指令不用确认,训练、诊断、待审、晋级各占一个 tick,动作行与第一轮同形,目标换成 run-002。"""
    enable = "plan,track,admit"
    extra = (*LOOP, "--no-confirm-directives")
    train_and_promote(store, "run-001", enable, *extra)
    assert tick(store, enable, *extra) == ["plan_next_round run-001"]
    assert train_and_promote(store, "run-002", enable, *extra) == [
        ["dispatch_directive run-002"], ["request_diagnostic cand-run-002"],
        ["await_review cand-run-002"], ["promote_candidate cand-run-002"],
    ]


def test_the_nomination_threshold_is_the_scheduler_configs_min_gain_not_a_constant(store):
    """补任务 108 的缺口:`gain > 0.01` 把默认阈值写死在断言里。这里让阈值来自配置:把 min_gain 抬到比增益还高,
    同一个 run 的提名就变成 skipped、不产生配方候选,理由里点名这个阈值;增益本身不变。"""
    from controlplane.paths import scheduler_config_file
    from trainscheduler.config import load_scheduler_config

    enable = "nominate,track,admit"
    train_and_promote(store, "run-001", enable, *LOOP)
    config = load_scheduler_config(store)
    gain = compute_gain(store, "run-001", DIM).gain
    assert gain > config.min_gain                                      # 默认阈值下会提名(大循环用例钉的那条)
    write_config(store, scheduler_config_file(), "scheduler",
                 dict(config.to_dict(), min_gain=gain + 1.0), "human: raise min_gain above the gain")
    assert tick(store, enable, *LOOP) == ["nominate_recipe run-001"]
    nomination = store.read_product(nomination_file("run-001")).payload
    assert nomination["result"] == "skipped" and nomination["recipe_candidate_id"] is None
    assert f"below min_gain {gain + 1.0}" in nomination["reason"]
    assert not store.exists(recipe_candidate_file("run-001"))
    assert compute_gain(store, "run-001", DIM).gain == gain


def test_the_canned_agent_round_has_the_shape_the_flaky_failure_point_assumes(store, monkeypatch):
    """补任务 108 的缺口:`_Flaky(fail_after=5)` 依赖罐头情境的形状(三个情境、四个回合、智能体后说,
    所以每条部署 episode 里智能体说 2 句:2 + 2 + 第三条的第一句 = 第 5 次)。形状变了,那条用例会静默地换成
    另一个失败点。这里直接数:部署的头三个后端各被调用 2 次,而且失败点的算式与配置对得上。"""
    from factory import handlers as fh
    from factory.agent import CannedGuidedBackend
    from factory.defaults import DEFAULT_AGENT_CONFIG

    scenarios, max_turns = len(DEFAULT_AGENT_CONFIG["scenario_ids"]), DEFAULT_AGENT_CONFIG["max_turns"]
    per_episode = max_turns // 2
    assert (scenarios, max_turns) == (3, 4) and per_episode * (scenarios - 1) + 1 == 5

    _agent_ready(store)
    built = []

    def counting_factory(model_id, ckpt):
        inner, calls = CannedGuidedBackend(), []
        built.append(calls)

        class Backend:
            def respond_guided(self, *a):
                calls.append(1)
                return inner.respond_guided(*a)

        return Backend()

    monkeypatch.setattr(fh, "canned_backend_factory", counting_factory)
    assert tick(store, "agent") == ["start_agent_round zengen-v2"]
    assert [len(calls) for calls in built[:scenarios]] == [per_episode] * scenarios
