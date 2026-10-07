# MindTrainer(下):训练记录、执行器、checkpoint、续训、编排与集成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 MindTrainer 的下半段(设计文档 §5–§7,以及 §9 的两条集成测试):训练运行记录与存储适配、可注入崩溃的 mock 训练执行器(哈希链 checkpoint)、checkpoint 格式转换与候选注册、续训判定、薄编排器 `run_training` / `resume_or_run`,最后用两条集成测试证明 MindTrainer 产出的候选**不改调和循环**就能走完 审核 → 晋级 → 下一轮基座。

**Architecture:** 一模块一文件,与上半段同构。逻辑核心是纯函数(`decide_resume`)与可 mock 的协议(`TrainingExecutor`、`CheckpointConverter`);编排器只调下层、不含判定逻辑(沿用 `diagnostic.orchestrator.run_diagnostic` 的做法)。核心保证是**续训等价**:Batch 模式下,崩溃后从存点续训得到的最终 checkpoint 与一次跑完逐字节相同,这条由"在每一步注入崩溃"的参数化测试直接证明。

**Tech Stack:** Python 3.11+ / uv / pytest;**无新增第三方依赖**。

**Spec:**
- `docs/superpowers/specs/2026-10-03-mindtrainer-design.md`(本计划实现其 §5.3、§6、§7、§9)
- 上半段计划:`docs/superpowers/plans/2026-10-03-mindtrainer-part1-trajectory-and-rewards.md`(**必须先完成**;任务编号接着它的 1–9 往下排)
- 背景:`docs/superpowers/specs/2026-09-24-rismforge-subsystem-design.md` 补充 D.2、补充 E

**依赖上半段产出的名字(签名以上半段计划为准)**

| 来源 | 名字 |
|---|---|
| `mindtrainer.training_config` | `TrainingConfig`、`RewardConfig`、`validate_training_config`、`InvalidTrainingConfigError`、`config_hash`、`total_steps`、`phase_at`、`STAGES` |
| `mindtrainer.datamodel` | `TrajectoryBatch`、`RewardedTrajectory`(本计划任务 10 往同一文件加 `StepResult`、`TrainingRun`) |
| `mindtrainer.trajectory_source` | `TrajectorySource`、`BatchTrajectorySource`、`InteractTrajectorySource(..., start_step=0)`、`batch_for_step`、`RolloutFailedError` |
| `mindtrainer.reward_pipeline` | `AttributionBackend`、`build_attribution_backend`、`run_reward_pipeline`、`summarize_rewards`、`AttributionError`、`SelfEvalBiasError` |
| `mindtrainer.grouping` | `group_by_id`、`count_degenerate_groups`、`validate_groups`、`DegenerateGroupError` |
| `controlplane.paths` | `training_run_file`、`candidate_checkpoint_file`(上半段任务 1 已加) |

**上半段整支评审指出、本计划尚未处理的两件事(执行下半段前需要决定)**

- `SelfEvalBiasError` 由调用方在 `run_training` 之外构造归因后端时抛出(`build_attribution_backend`),这时还没有 `training_run` 可以写 `failed`。这是合理的,但任务 14 没有明说,执行者不要试图在编排器里捕获它。
- `RolloutFailedError` 目前**没有**被任务 14 引用:它不在 `_DETERMINISTIC` 里,所以 Interact 的 rollout 失败(无论是瞬态重试用尽,还是 `invalid_session` 这类确定性拒绝)都会让 `training_run` 保持 `pending`,交给 `decide_resume`,最坏会反复续训直到 `max_resume_attempts` 触顶才升级。是否把不可重试的原因归为确定性失败写 `failed`,以及 `failed_attempts` 如何进入 `curve_summary`,是尚未决定的取舍,不是遗漏的实现细节。

**整支评审的其余建议(可选,尚未纳入任何任务;要做就在对应任务里加步骤并同步更新测试数)**

- **配置类型校验的剩余缺口**(上半段整支评审的 F1,部分修复已合并,剩余缺口记在这里)。**已修好**的字段已用只读探针逐个复核,`validate_training_config` 对它们都返回含字段名的原因字符串:`allow_resume`;整数字段(`sft_steps`、`grpo_steps`、`checkpoint_every_steps`、`group_size`、`max_turns_per_episode` 等,拒绝 `bool` 和非 `int`);`opd_coefficient`;`reward.gamma`;`grpo_config`(须可 JSON 序列化);`base_checkpoint_ref` 与 `teacher_model_ref` 的 `sha256` 须是 `str`;`scenario_ids` 须是非空 `str` 组成的 `list`;`stage_manifest_refs` 本身须是 `dict`。**剩余未修**的字段不在整支评审报告列举的那 12 个现象里,是范围复审者按"任何字段"搜出来的。下面按 `run_training` 实际怎么处理分三类,都是直接构造 `TrainingConfig` 复现的:
  - **静默接受,run 照常 `executed`**:`reward.judge_model_id` 或 `reward.rubric_version` 是整数;`stage_manifest_refs` 里某个值不是 `str`(例如 `grpo_opd` 为 5)。这三项经 `load_config_from_directive` 的外部输入同样原样通过。
  - **抛裸异常,且此时没有任何 `training_run` 记录**:`reward.outcome_rules` 是 `list`、`bindings` 的元素不是 `RoleBinding`、`base_checkpoint_ref` 或 `teacher_model_ref` 不是 `BigRef`、`reward` 是 `None` 或 `dict`,抛的都是裸 `AttributeError`。`config_hash` 是 `run_training` 的第一条语句,先于 `validate_training_config`,所以走不到写记录那一步。
  - **抛具名异常,但消息指错了字段**:`reward.components` 或 `bindings` 不可迭代时,`config_hash` 抛 `InvalidTrainingConfigError`,消息却写成 "grpo_config must be JSON serializable",因为它把 `to_dict` 里的任何 `TypeError` 都归给了 `grpo_config`。同样没有记录。
  - **后果与范围**:编排器只对确定性失败写 `failed`(`_DETERMINISTIC` 里那几类,以及修复波之后的 `invalid_session` 拒绝),后两类在它写任何记录之前就已抛出,调用方直接看到异常,`failed` 记录里不会有痕迹。`load_config_from_directive` 会把 `from_dict` 阶段的 `TypeError` 与 `AttributeError` 包成 `InvalidTrainingConfigError`,所以后两类只有绕过它直接构造 `TrainingConfig` 才会遇到。范围复审者判断上半段计划给出的合法配置示例都不会触发这些缺口,这一点本次没有复核。它与设计 §10 第 11 条同源,接线时应由调用方在构造配置后、调编排器前先校验并自行记录。是否再开一轮修复波,留给用户决定。
- **边界 1 的自动化守卫。** 现在"`src/mindtrainer` 不导入 `diagnostic.*`"只靠人工预检。建议加一个约 15 行的测试(静态扫描 `src/mindtrainer/*.py` 的 import 语句,或导入全部 mindtrainer 模块后断言 `sys.modules` 里没有 `diagnostic*`),与 `test_mt_packaging` 同类。下半段还会再加 5 个源文件,越早加越划算。
- **`RolloutFailedError` 没有 `transient` 标志。** 编排器要区分"瞬态重试用尽"和"确定性拒绝"时,只能硬编码 reason 集合。给它加一个 `transient: bool` 属性成本很低(一行加一条断言),但要改上半段任务 6 的代码,需先决定上面"未决事项"里的取舍。
- **策略异常路径丢失已累积的瞬态 `failed_attempts`。** 简报要求策略异常原样上抛、不包装,所以异常上拿不到这些数据。评审给的不改异常类型的做法:`collect()` 开头把 `failed_attempts` 列表挂到 `self.last_failed_attempts`,这样策略异常和 `RolloutFailedError` 两条路径都能读到。
- **`RewardedTrajectory.from_dict` 不核对 `group_id`。** 手改的 `group_id` 会静默改变分组。可在 `from_dict` 里重算 `episode_group_id(episode)` 并在不一致时抛 `ValueError`;是否值得取决于有没有人会手改落盘的 `RewardedTrajectory`,目前没有这个路径。

**打包说明:** 设计把打包防回归放在第二段,但上半段任务 1 已把 `src/mindtrainer` 登记进 wheel 包列表并加了 `tomllib` 防回归测试(越早加,后面每个任务都受保护)。所以本计划没有单独的打包任务。

## Global Constraints

每个任务的要求都隐含包含本节。

- **MindTrainer 不导入 `diagnostic.*`**(设计 §0 边界 1)。OPD 是否失效由诊断侧判定,MindTrainer 只如实记录。
- **复用 `simulator.*`、`controlplane.*` 的数据结构是允许的**:受限的是子系统间的运行时调用。
- **脱离真实环境可测**:不需要 GPU、ROLL、外部网络。全部测试用 `tmp_path` 隔离;Interact 的端到端用 Plan 4 的真实 loopback gRPC 服务端(`127.0.0.1`)。
- **读产物走 `GitStore.read_product`**,禁止直接反序列化;写产物用 `Envelope` + `GitStore.write_product`。**时间戳用固定常量**:产物内容必须是输入的纯函数,崩溃后重放才会逐字节相同(`write_product` 对此是空操作)。
- **状态机沿用非审核类**:`pending → executed`、`pending → failed`;`failed` 保留不删,也不自动重跑。候选 checkpoint 走审核类,由人批准,MindTrainer 只写 `pending_review`。
- **确定性失败写 `failed`(category `T2`)并原样重抛;其余异常不吞**,`training_run` 保持 `pending`,由 `decide_resume` 在下次进入时判定。
- **mock 执行器的损失值是 step 的合成函数,不是学习证据**;来自输入的量(组统计、`mean_reward`、`opd_weight`)是真的。
- **候选 payload 的 `checkpoint_ref` 键名与形状是契约**:daemon 晋级时原样带进 `zengen_model`,不得改动,调和循环与现有端到端测试都不用为它改。
- **凭证绝不进 git 树**;**无新增第三方依赖**。
- 每个源文件以 `from __future__ import annotations` 开头;提交信息结尾空一行后加:`Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`。
- **测试基线**:上半段全部完成后,`uv run --extra dev pytest -q` 为 581 通过、无警告;每个任务后应递增,不得出现警告。

---

## File Structure

**新增 `src/mindtrainer/`**
- `store_adapter.py` — `write_training_run`、`read_training_run`(任务 10)
- `executor.py` — `TrainingExecutor` 协议、`MockExecutor`、`ExecutorNotLoadedError`、`CheckpointCorruptError`、`SimulatedCrashError`(任务 11)
- `checkpoint.py` — `CheckpointConverter` 协议、`MockConverter`、`register_candidate`、`CandidateAlreadyReviewedError`(任务 12)
- `resume.py` — `decide_resume`、`ResumeDecision`、`NeedsHumanError`(任务 13)
- `orchestrator.py` — `run_training`、`resume_or_run`、`NotEnoughGroupsError`(任务 14)

**修改既有**
- `src/mindtrainer/datamodel.py` — 加 `StepResult`、`TrainingRun`(任务 10)

**新增测试**
- `tests/test_mt_training_run.py`(任务 10)、`tests/test_mt_executor.py`(任务 11)、`tests/test_mt_checkpoint.py`(任务 12)、`tests/test_mt_resume.py`(任务 13)、`tests/test_mt_orchestrator.py`(任务 14)、`tests/test_mt_integration.py`(任务 15)

---

### Task 10: 训练运行记录与存储适配

**Files:**
- Modify: `src/mindtrainer/datamodel.py`(替换顶部 import 区,并在文件末尾追加)
- Create: `src/mindtrainer/store_adapter.py`
- Test: `tests/test_mt_training_run.py`

**Interfaces:**
- Consumes: `controlplane.envelope`(`BigRef`、`Envelope`、`OwnerRef`、`CURRENT_SCHEMA_VERSION`);`controlplane.gitstore.GitStore`;`controlplane.states`(`NONREVIEW`、`assert_transition`、`InvalidTransitionError`);上半段任务 1 的 `controlplane.paths.training_run_file`;`controlplane.observe.trace`(只在测试里用)。
- Produces(任务 11–15 依赖这些名字):
  - `StepResult(step: int, phase: str, metrics: dict[str, float])`:frozen dataclass。
  - `TrainingRun(run_id, config_hash, checkpoint_step=0, last_checkpoint_ref=None, resume_attempts=0, curve_summary={}, metric_stream_refs=[], status="pending", failure=None)`,带 `to_dict()` 与 `from_dict(d, *, status="pending")`。**`status` 不进 `to_dict()`**,它属于信封。
  - `write_training_run(store, run) -> str`(返回相对路径)、`read_training_run(store, run_id) -> TrainingRun | None`。

**设计要点:**
- **状态属于信封,不属于业务字段**:`TrainingRun.status` 只是为了让 `decide_resume` 在一个对象上判定,落盘时写进信封的 `status`,读回时由 `read_training_run` 填回。
- **状态机沿用非审核类**:`pending → executed | failed`。同状态重写(`pending → pending`,更新存点进度)允许;终态不能被改回 `pending`,也不能互相改写,违者抛 `InvalidTransitionError`。
- **失败是数据**:`failed` 记录必须带 `failure["category"]`(确定性失败为 `T2`),否则抛 `ValueError`,事后才追查得到为什么失败。
- **`metric_stream_refs` 的键名固定**:`rismforge trace` 读的就是这个键,有测试用真实的 `trace()` 验证。逐步指标序列不进 git(补充 D.2),只留摘要和引用。
- **放在 run 目录顶层**:`GitStore.list_products` 只扫 `*.yaml` 且不递归,放进子目录 `trace` 就看不到。
- **时间戳用固定常量**:产物内容必须是输入的纯函数,崩溃后重放才会逐字节相同,`write_product` 对此是空操作(有测试数提交数)。
- 信封:`id = trainrun-{run_id}`,`type = training_run`,`links = [run_id]`。

- [ ] **Step 1: 写失败测试**

`tests/test_mt_training_run.py`:

```python
import dataclasses
import json
import subprocess

import pytest
import yaml

from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from controlplane.observe import trace
from controlplane.paths import run_dir, training_run_file
from controlplane.states import InvalidTransitionError
from mindtrainer.datamodel import StepResult, TrainingRun
from mindtrainer.store_adapter import read_training_run, write_training_run


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _ref(ch):
    return BigRef(f"mock://ckpt/{ch * 64}@2", ch * 64, 0, {"type": "mock", "tag": "t"})


def _run(**kw):
    base = dict(run_id="run-1", config_hash="c" * 64)
    base.update(kw)
    return TrainingRun(**base)


def _commits(root):
    out = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return int(out.stdout)


# ---------------- StepResult ----------------

def test_step_result_is_frozen():
    result = StepResult(0, "sft", {"sft_loss": 1.0})
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.step = 1


# ---------------- TrainingRun ----------------

def test_defaults_describe_a_fresh_run():
    run = _run()
    assert (run.checkpoint_step, run.last_checkpoint_ref, run.resume_attempts) == (0, None, 0)
    assert (run.curve_summary, run.metric_stream_refs, run.status, run.failure) == ({}, [], "pending", None)


def test_round_trip_keeps_refs_and_progress():
    run = _run(
        checkpoint_step=4, last_checkpoint_ref=_ref("a"), resume_attempts=1,
        curve_summary={"final_mean_reward": 0.5}, metric_stream_refs=[_ref("b")],
    )
    assert TrainingRun.from_dict(run.to_dict()) == run


def test_status_belongs_to_the_envelope_not_the_payload():
    assert "status" not in _run(status="executed").to_dict()
    assert TrainingRun.from_dict(_run().to_dict(), status="executed").status == "executed"


def test_the_metric_stream_key_is_the_one_trace_reads():
    assert "metric_stream_refs" in _run(metric_stream_refs=[_ref("a")]).to_dict()


def test_to_dict_is_plain_data_and_does_not_alias():
    run = _run(curve_summary={"curve": {"mean": 0.5}}, failure={"category": "T2"})
    d = run.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert yaml.safe_load(yaml.safe_dump(d)) == d
    d["curve_summary"]["curve"]["mean"] = 99
    d["failure"]["category"] = "X"
    assert run.curve_summary["curve"]["mean"] == 0.5 and run.failure["category"] == "T2"


# ---------------- 存储适配 ----------------

def test_write_then_read_round_trips_with_the_status(store):
    run = _run(checkpoint_step=2, last_checkpoint_ref=_ref("a"), status="pending")
    write_training_run(store, run)
    assert read_training_run(store, "run-1") == run


def test_reading_a_run_that_was_never_written_is_none(store):
    assert read_training_run(store, "run-404") is None


def test_the_record_sits_at_the_top_of_the_run_dir_where_trace_can_see_it(store):
    path = write_training_run(store, _run())
    assert path == training_run_file("run-1")
    assert path in store.list_products(run_dir("run-1"))
    env = store.read_product(path)
    assert (env.type, env.id, env.links) == ("training_run", "trainrun-run-1", ["run-1"])


def test_trace_reports_metric_streams_through_the_expected_key(store, tmp_path):
    present = tmp_path / "metrics.jsonl"
    present.write_text("{}\n")
    refs = [
        BigRef(present.as_uri(), "d" * 64, 3),
        BigRef((tmp_path / "gone.jsonl").as_uri(), "e" * 64, 3),
    ]
    write_training_run(store, _run(metric_stream_refs=refs))
    out = trace(store, "run-1")
    assert "training_run" in out
    assert f"metric_ref [ok] {present.as_uri()}" in out
    assert "metric_ref [unreachable]" in out


def test_rewriting_identical_content_adds_no_commit(store, tmp_path):
    write_training_run(store, _run(checkpoint_step=2))
    before = _commits(tmp_path)
    write_training_run(store, _run(checkpoint_step=2))
    assert _commits(tmp_path) == before


def test_progress_updates_while_pending_are_allowed(store):
    write_training_run(store, _run(checkpoint_step=0))
    write_training_run(store, _run(checkpoint_step=3, last_checkpoint_ref=_ref("a")))
    assert read_training_run(store, "run-1").checkpoint_step == 3


def test_pending_can_become_executed_or_failed(store):
    write_training_run(store, _run())
    write_training_run(store, _run(status="executed"))
    assert read_training_run(store, "run-1").status == "executed"
    write_training_run(store, _run(run_id="run-2"))
    write_training_run(store, _run(run_id="run-2", status="failed", failure={"category": "T2", "point": "grouping", "detail": "x"}))
    assert read_training_run(store, "run-2").failure["category"] == "T2"


@pytest.mark.parametrize("final,then", [("executed", "pending"), ("executed", "failed"), ("failed", "executed"), ("failed", "pending")])
def test_terminal_states_cannot_be_reopened_or_swapped(store, final, then):
    failure = {"category": "T2"}
    write_training_run(store, _run())
    write_training_run(store, _run(status=final, failure=failure if final == "failed" else None))
    with pytest.raises(InvalidTransitionError):
        write_training_run(store, _run(status=then, failure=failure if then == "failed" else None))


def test_a_failed_run_must_say_why(store):
    write_training_run(store, _run())
    with pytest.raises(ValueError, match="failure"):
        write_training_run(store, _run(status="failed"))
    with pytest.raises(ValueError, match="failure"):
        write_training_run(store, _run(status="failed", failure={"detail": "no category"}))
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run --extra dev pytest tests/test_mt_training_run.py -q`
Expected: 收集阶段报错 `ImportError: cannot import name 'StepResult' from 'mindtrainer.datamodel'`。

- [ ] **Step 3: 写实现**

先把 `src/mindtrainer/datamodel.py` 顶部的 import 区(从 `from __future__ import annotations` 到 `from simulator.datamodel import Episode` 为止)整体替换为:

```python
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from controlplane.envelope import BigRef
from simulator.datamodel import Episode
```

再在 `src/mindtrainer/datamodel.py` 末尾追加:

```python
@dataclass(frozen=True)
class StepResult:
    """执行器单步的结果。指标名是契约,后续做"训练奖励 vs 诊断分"背离检测靠它们稳定。"""

    step: int  # 全局 step,从 0 起
    phase: str  # "sft" | "grpo_opd"
    metrics: dict[str, float]


@dataclass
class TrainingRun:
    """训练运行记录(产物 type="training_run")。

    status 属于信封而不属于业务字段,这里带上只是为了让 decide_resume 能在一个对象上
    判定:pending(运行中)| executed(成功)| failed(确定性失败,保留)。
    failure 仅在 failed 时有值:{"category": "T2", "point": ..., "detail": ...}。
    逐步指标序列不进 git(补充 D.2),只留 curve_summary 摘要和指标流引用。
    metric_stream_refs 的键名固定,rismforge trace 读的就是它。
    """

    run_id: str
    config_hash: str
    checkpoint_step: int = 0  # 最近一次中间 checkpoint 对应的全局 step;从未存点则为 0
    last_checkpoint_ref: BigRef | None = None
    resume_attempts: int = 0
    curve_summary: dict = field(default_factory=dict)
    metric_stream_refs: list[BigRef] = field(default_factory=list)
    status: str = "pending"
    failure: dict | None = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "checkpoint_step": self.checkpoint_step,
            "last_checkpoint_ref": (
                self.last_checkpoint_ref.to_dict() if self.last_checkpoint_ref else None
            ),
            "resume_attempts": self.resume_attempts,
            "curve_summary": copy.deepcopy(self.curve_summary),
            "metric_stream_refs": [r.to_dict() for r in self.metric_stream_refs],
            "failure": copy.deepcopy(self.failure),
        }

    @classmethod
    def from_dict(cls, d: dict, *, status: str = "pending") -> "TrainingRun":
        ref = d.get("last_checkpoint_ref")
        return cls(
            run_id=d["run_id"],
            config_hash=d["config_hash"],
            checkpoint_step=d.get("checkpoint_step", 0),
            last_checkpoint_ref=BigRef.from_dict(ref) if ref else None,
            resume_attempts=d.get("resume_attempts", 0),
            curve_summary=copy.deepcopy(d.get("curve_summary", {})),
            metric_stream_refs=[BigRef.from_dict(r) for r in d.get("metric_stream_refs", [])],
            status=status,
            failure=copy.deepcopy(d.get("failure")),
        )
```

新建 `src/mindtrainer/store_adapter.py`:

```python
from __future__ import annotations

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import training_run_file
from controlplane.states import NONREVIEW, assert_transition
from mindtrainer.datamodel import TrainingRun

# 沿用仓库既有的固定时间戳写法:产物内容必须是输入的纯函数,
# 崩溃后重放才会逐字节相同(GitStore.write_product 对此是空操作)。
_NOW = "2026-10-03T00:00:00Z"


def write_training_run(store: GitStore, run: TrainingRun) -> str:
    """写 training_run 产物。状态机沿用非审核类:pending -> executed | failed。
    同状态重写(pending -> pending,更新 checkpoint 进度)允许;其余非法迁移抛
    InvalidTransitionError,所以 executed/failed 的记录不会被悄悄改回 pending。"""
    if run.status == "failed" and not (run.failure and run.failure.get("category")):
        # 失败是数据:failed 记录必须说明为什么失败,否则事后无法追查
        raise ValueError(f"training_run {run.run_id}: a failed run needs failure['category']")
    path = training_run_file(run.run_id)
    if store.exists(path):
        previous = store.read_product(path).status
        if previous != run.status:
            assert_transition(NONREVIEW, previous, run.status)
    env = Envelope(
        id=f"trainrun-{run.run_id}",
        type="training_run",
        version=1,
        created_at=_NOW,
        created_by=OwnerRef(),
        status=run.status,
        schema_version=CURRENT_SCHEMA_VERSION,
        links=[run.run_id],
        payload=run.to_dict(),
    )
    store.write_product(path, env, f"training_run: {run.run_id} [{run.status}]")
    return path


def read_training_run(store: GitStore, run_id: str) -> TrainingRun | None:
    """不存在返回 None。读取走 read_product(经迁移器链),状态取自信封。"""
    path = training_run_file(run_id)
    if not store.exists(path):
        return None
    env = store.read_product(path)
    return TrainingRun.from_dict(env.payload, status=env.status)
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run --extra dev pytest tests/test_mt_training_run.py tests/test_mt_datamodel.py -q`
Expected: 28 passed(本任务 18 + 任务 4 的 10)。

- [ ] **Step 5: 全量回归**

Run: `uv run --extra dev pytest -q`
Expected: 599 passed,无警告。

- [ ] **Step 6: 提交**

```bash
git add src/mindtrainer/datamodel.py src/mindtrainer/store_adapter.py tests/test_mt_training_run.py
git commit -m "feat: mindtrainer training_run record and git store adapter (nonreview state machine, loud failure)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: mock 训练执行器

**Files:**
- Create: `src/mindtrainer/executor.py`
- Test: `tests/test_mt_executor.py`

**Interfaces:**
- Consumes: `controlplane.envelope.BigRef`;任务 4/10 的 `RewardedTrajectory`、`StepResult`;任务 9 的 `group_by_id`、`count_degenerate_groups`;`simulator.dataset.episode_digest`。
- Produces(任务 12–15 依赖这些名字):
  - `TrainingExecutor`(Protocol):`load(ref)`、`sft_step(episodes, *, trained_slot) -> StepResult`、`grpo_opd_step(rewarded, *, opd_coefficient, teacher_model_ref) -> StepResult`、`save_checkpoint(tag) -> BigRef`、`checkpoint_intact(ref) -> bool`。
  - `MockExecutor(*, crash_at_step=None)`,另有只读属性 `state_hash`、`steps_done`,以及可改的 `crash_at_step`。
  - `ExecutorNotLoadedError(RuntimeError)`、`CheckpointCorruptError(Exception)`、`SimulatedCrashError(RuntimeError)`。

**设计要点:**
- **协议比设计 §5 多一个 `checkpoint_intact`**:`decide_resume` 需要"这个 checkpoint 能否重建"这个事实,只有执行器知道怎么重建(设计 §6.2 说它"来自执行器能否重建该哈希",但 §5 的协议里漏了)。设计文档在计划自检时同步修订。
- **GRPO 与 OPD 只有一个方法**:spec §3.2 明说两者作用在同一个训练 step 里,不是先后阶段。SFT 可跳过(`sft_steps=0` 就不调 `sft_step`)。
- **指标名是契约**,后续做"训练奖励 vs 诊断分"背离检测靠它们稳定:SFT 只有 `sft_loss`;GRPO+OPD 有 `grpo_loss`、`opd_loss`、`opd_weight`、`total_loss`、`mean_reward`、`n_groups`、`n_degenerate_groups`。`opd_weight` 记的是**实际生效的系数**;系数为 0 时 `opd_loss` 仍报告。MindTrainer 无法自证 OPD 是否起作用,那是诊断侧的事。
- **来自输入的量是真的**(组统计、`mean_reward`、`opd_weight`);**损失值是 step 的合成函数,不是学习证据**。
- **checkpoint 是哈希链**:每步 `state = sha256(上一步 state + 阶段 + step + 本步数据)`,`BigRef` 的 sha256 就是当前 state,uri 为 `mock://ckpt/{state}@{已训步数}`。所以 `load(ref)` 无需任何文件就能还原状态与步数,续训可以精确测试。**`tag` 只放在 `fetch_hint` 里,不进哈希**,否则同一状态因 tag 不同而哈希不同,续训等价就没法比。
- **损坏的引用在改动任何状态之前就被拒绝**(`CheckpointCorruptError`,有测试断言执行器原状态不变)。
- **`load` 非 mock 方案的引用当作外部起点权重**(`state = ref.sha256`,步数 0);**推理格式(`fetch_hint["format"] == "hf"`)的引用同样如此**:晋级后下一轮训练的基座正是转换后的那份(设计 §6.1)。这一条是写任务 15 的集成测试时才暴露的——mock 起初把它当成损坏的 mock 存点而拒绝加载。真实 ROLL 能否直接加载推理格式作为训练起点**没有核实**(设计 §10 第 9 条),mock 不区分格式,所以暴露不出该问题。
- **`crash_at_step` 在改动任何状态之前抛 `SimulatedCrashError`**,供续训测试注入崩溃。被拒绝的调用(空数据、非法系数、空 tag)不推进状态。

- [ ] **Step 1: 写失败测试**

`tests/test_mt_executor.py`:

```python
import dataclasses

import pytest

from controlplane.envelope import BigRef
from mindtrainer.datamodel import RewardedTrajectory
from mindtrainer.executor import (
    CheckpointCorruptError, ExecutorNotLoadedError, MockExecutor, SimulatedCrashError,
)
from simulator.datamodel import Episode, RoleAssignment, RoleBinding, TurnRecord

BASE = BigRef("file:///ckpt/base", "a" * 64, 1)
TEACHER = BigRef("file:///ckpt/teacher", "b" * 64, 1)
GRPO_KEYS = {
    "grpo_loss", "opd_loss", "opd_weight", "total_loss", "mean_reward",
    "n_groups", "n_degenerate_groups",
}


def _episode(ep_id="e0", utterance="u0"):
    assignment = RoleAssignment(
        "s1",
        [RoleBinding("a", "pa", "self_play"),
         RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")],
        max_turns=2,
    )
    return Episode(ep_id, "s1", assignment, [TurnRecord(0, "a", utterance), TurnRecord(1, "b", "v")], "max_turns")


def _rt(group, reward, ep_id):
    return RewardedTrajectory(_episode(ep_id), group, [reward, None], reward)


def _loaded(**kw):
    ex = MockExecutor(**kw)
    ex.load(BASE)
    return ex


def _sft(ex, i):
    """第 i 步的数据是 i 的纯函数——续训等价靠这一点。"""
    return ex.sft_step([_episode(f"e{i}", f"u{i}")], trained_slot="a")


def _grpo(ex, rewards=(("g1", 1.0), ("g1", 0.0)), coef=0.1):
    batch = [_rt(g, r, f"e{i}") for i, (g, r) in enumerate(rewards)]
    return ex.grpo_opd_step(batch, opd_coefficient=coef, teacher_model_ref=TEACHER)


# ---------------- 未加载 ----------------

@pytest.mark.parametrize(
    "call",
    [
        lambda ex: ex.sft_step([_episode()], trained_slot="a"),
        lambda ex: ex.grpo_opd_step([_rt("g", 0.5, "e")], opd_coefficient=0.1, teacher_model_ref=TEACHER),
        lambda ex: ex.save_checkpoint("t"),
    ],
    ids=["sft", "grpo_opd", "save"],
)
def test_nothing_works_before_load(call):
    with pytest.raises(ExecutorNotLoadedError):
        call(MockExecutor())


# ---------------- 指标契约 ----------------

def test_sft_step_reports_the_phase_the_global_step_and_only_sft_loss():
    ex = _loaded()
    first, second = _sft(ex, 0), _sft(ex, 1)
    assert (first.phase, first.step, second.step) == ("sft", 0, 1)
    assert set(first.metrics) == {"sft_loss"}


def test_grpo_step_reports_exactly_the_contract_metrics():
    result = _grpo(_loaded())
    assert result.phase == "grpo_opd"
    assert set(result.metrics) == GRPO_KEYS
    assert all(isinstance(v, float) for v in result.metrics.values())


def test_input_derived_metrics_are_real():
    rewards = (("g1", 1.0), ("g1", 0.0), ("g2", 0.5), ("g2", 0.5))
    m = _grpo(_loaded(), rewards, coef=0.3).metrics
    assert m["mean_reward"] == 0.5
    assert m["n_groups"] == 2.0
    assert m["n_degenerate_groups"] == 1.0  # g2 奖励全相同
    assert m["opd_weight"] == 0.3


def test_total_loss_combines_grpo_and_the_weighted_opd_loss():
    m = _grpo(_loaded(), coef=0.4).metrics
    assert m["total_loss"] == pytest.approx(m["grpo_loss"] + 0.4 * m["opd_loss"])


def test_zero_coefficient_reports_zero_weight_but_still_reports_the_opd_loss():
    m = _grpo(_loaded(), coef=0.0).metrics
    assert m["opd_weight"] == 0.0
    assert m["opd_loss"] > 0
    assert m["total_loss"] == m["grpo_loss"]


# ---------------- 哈希链 ----------------

def test_same_data_same_state_across_independent_executors():
    a, b = _loaded(), _loaded()
    for i in range(3):
        _sft(a, i), _sft(b, i)
    assert a.state_hash == b.state_hash


@pytest.mark.parametrize(
    "tamper",
    [
        lambda ex: ex.sft_step([_episode("e0", "DIFFERENT")], trained_slot="a"),
        lambda ex: ex.sft_step([_episode("e0", "u0")], trained_slot="b"),
    ],
    ids=["episode_content", "trained_slot"],
)
def test_different_data_gives_a_different_state(tamper):
    baseline, other = _loaded(), _loaded()
    _sft(baseline, 0)
    tamper(other)
    assert baseline.state_hash != other.state_hash


def test_step_order_matters():
    ab, ba = _loaded(), _loaded()
    _sft(ab, 0), _sft(ab, 1)
    ba.sft_step([_episode("e1", "u1")], trained_slot="a")
    ba.sft_step([_episode("e0", "u0")], trained_slot="a")
    assert ab.state_hash != ba.state_hash


def test_rewards_and_opd_settings_feed_the_chain():
    states = set()
    for kwargs in (
        dict(), dict(rewards=(("g1", 1.0), ("g1", 0.25))), dict(coef=0.2),
    ):
        ex = _loaded()
        _grpo(ex, **kwargs)
        states.add(ex.state_hash)
    assert len(states) == 3


# ---------------- checkpoint ----------------

def test_saved_checkpoint_carries_the_state_hash_and_the_step_count():
    ex = _loaded()
    _sft(ex, 0), _sft(ex, 1)
    ref = ex.save_checkpoint("mid")
    assert ref.sha256 == ex.state_hash and len(ref.sha256) == 64
    assert ref.uri.endswith("@2")
    assert ref.fetch_hint == {"type": "mock", "tag": "mid"}


def test_the_tag_does_not_change_the_hash():
    ex = _loaded()
    _sft(ex, 0)
    assert ex.save_checkpoint("one").sha256 == ex.save_checkpoint("two").sha256


def test_loading_a_checkpoint_restores_state_and_step_counter_exactly():
    original = _loaded()
    _sft(original, 0), _sft(original, 1)
    ref = original.save_checkpoint("mid")

    restored = MockExecutor()
    restored.load(ref)
    assert (restored.state_hash, restored.steps_done) == (original.state_hash, 2)

    assert _sft(original, 2) == _sft(restored, 2)  # 下一步的 StepResult 也一致,含 step=2
    assert original.state_hash == restored.state_hash


def test_loading_a_foreign_base_checkpoint_starts_from_step_zero():
    ex = MockExecutor()
    ex.load(BASE)
    assert (ex.state_hash, ex.steps_done) == (BASE.sha256, 0)


def test_an_inference_format_ref_is_accepted_as_a_starting_point_like_any_foreign_base():
    # 晋级后下一轮的基座是转换后的推理格式引用。mock 不区分格式,把它当外部起点;
    # 真实 ROLL 能否这样加载未核实(设计 §10 第 9 条)。
    hf = BigRef(
        "mock://ckpt/" + "c" * 64 + "@3.hf", "d" * 64, 0,
        {"type": "mock", "format": "hf", "source_sha256": "c" * 64},
    )
    ex = MockExecutor()
    ex.load(hf)
    assert (ex.state_hash, ex.steps_done) == (hf.sha256, 0)
    assert ex.checkpoint_intact(hf) is False  # 无法当作中间存点来确认
    assert _sft(ex, 0).step == 0  # 训练可以从它继续


def test_resume_equivalence_at_the_executor_level():
    straight = _loaded()
    for i in range(5):
        _sft(straight, i)

    first = _loaded()
    for i in range(3):
        _sft(first, i)
    ref = first.save_checkpoint("mid")
    second = MockExecutor()
    second.load(ref)
    for i in range(3, 5):
        _sft(second, i)
    assert second.state_hash == straight.state_hash


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: dataclasses.replace(r, sha256="f" * 64),
        lambda r: dataclasses.replace(r, uri=r.uri.rsplit("@", 1)[0]),
        lambda r: dataclasses.replace(r, uri=r.uri.rsplit("@", 1)[0] + "@x"),
        lambda r: dataclasses.replace(r, uri="mock://ckpt/garbage"),
    ],
    ids=["sha_mismatch", "no_step_suffix", "bad_step", "garbage"],
)
def test_a_corrupt_mock_checkpoint_is_rejected_and_leaves_the_executor_untouched(mutate):
    ex = _loaded()
    _sft(ex, 0)
    good = ex.save_checkpoint("t")
    before = (ex.state_hash, ex.steps_done)
    bad = mutate(good)
    assert ex.checkpoint_intact(bad) is False
    with pytest.raises(CheckpointCorruptError):
        ex.load(bad)
    assert (ex.state_hash, ex.steps_done) == before


def test_checkpoint_intact_is_true_only_for_restorable_mock_refs():
    ex = _loaded()
    _sft(ex, 0)
    assert ex.checkpoint_intact(ex.save_checkpoint("t")) is True
    assert ex.checkpoint_intact(BASE) is False  # 非 mock 方案:无法确认


# ---------------- 崩溃注入与参数校验 ----------------

def test_a_crash_is_raised_before_any_state_changes_and_can_be_cleared():
    ex = _loaded(crash_at_step=1)
    _sft(ex, 0)
    before = (ex.state_hash, ex.steps_done)
    with pytest.raises(SimulatedCrashError, match="step 1"):
        _sft(ex, 1)
    assert (ex.state_hash, ex.steps_done) == before
    ex.crash_at_step = None
    assert _sft(ex, 1).step == 1


def test_bad_arguments_are_rejected():
    ex = _loaded()
    with pytest.raises(ValueError, match="at least one episode"):
        ex.sft_step([], trained_slot="a")
    with pytest.raises(ValueError, match="at least one trajectory"):
        ex.grpo_opd_step([], opd_coefficient=0.1, teacher_model_ref=TEACHER)
    for bad in (-0.1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="opd_coefficient"):
            _grpo(ex, coef=bad)
    with pytest.raises(ValueError, match="tag"):
        ex.save_checkpoint("")
    assert ex.steps_done == 0  # 被拒绝的调用不推进状态
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run --extra dev pytest tests/test_mt_executor.py -q`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindtrainer.executor'`。

- [ ] **Step 3: 写实现**

`src/mindtrainer/executor.py`:

```python
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Protocol

from controlplane.envelope import BigRef
from mindtrainer.datamodel import RewardedTrajectory, StepResult
from mindtrainer.grouping import count_degenerate_groups, group_by_id
from simulator.dataset import episode_digest
from simulator.datamodel import Episode

_URI_PREFIX = "mock://ckpt/"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ExecutorNotLoadedError(RuntimeError):
    """没有 load 就训练或存点。"""


class CheckpointCorruptError(Exception):
    """checkpoint 引用无法还原(格式不对、哈希对不上)。"""


class SimulatedCrashError(RuntimeError):
    """mock 执行器按 crash_at_step 注入的崩溃,用来测试续训。"""


class TrainingExecutor(Protocol):
    """有状态的训练执行器,对应 ROLL 里持有权重的训练进程。

    GRPO 与 OPD 只有一个方法:spec §3.2 明说两者作用在同一个训练 step 里,不是先后阶段。
    StepResult.step 是执行器自己的全局 step 计数,从 0 起;load 一个中间 checkpoint
    会把计数恢复到存点时的值。

    checkpoint_intact 是对设计文档协议的补充:decide_resume 需要"能否重建该 checkpoint"
    这个事实,而只有执行器知道怎么重建。
    """

    def load(self, checkpoint_ref: BigRef) -> None: ...

    def sft_step(self, episodes: list[Episode], *, trained_slot: str) -> StepResult: ...

    def grpo_opd_step(
        self,
        rewarded: list[RewardedTrajectory],
        *,
        opd_coefficient: float,
        teacher_model_ref: BigRef,
    ) -> StepResult: ...

    def save_checkpoint(self, tag: str) -> BigRef: ...

    def checkpoint_intact(self, checkpoint_ref: BigRef) -> bool: ...


def _sha256(obj) -> str:
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_inference_format(ref: BigRef) -> bool:
    """MockConverter 产出的推理格式(HF)引用。晋级链路里候选带的就是它,下一轮训练的
    基座又从它解析而来(设计 §6.1)。"""
    return bool(ref.fetch_hint) and ref.fetch_hint.get("format") == "hf"


def _parse_mock_ref(ref: BigRef) -> tuple[str, int]:
    """mock://ckpt/{state}@{steps}。state 必须等于 ref.sha256,否则视为损坏。"""
    body = ref.uri[len(_URI_PREFIX):]
    state, sep, steps = body.partition("@")
    if not sep or not steps.isdigit() or not _SHA256.match(state) or state != ref.sha256:
        raise CheckpointCorruptError(f"cannot restore from {ref.uri!r} (sha256 {ref.sha256!r})")
    return state, int(steps)


class MockExecutor:
    """确定性,不训练任何东西。

    - 来自输入的量是真的:组统计、mean_reward、opd_weight。
    - 损失值是 step 的合成函数,**不是学习证据**,只为让指标名与形状可测。
    - checkpoint 是哈希链:每步 state = sha256(上一步 state + 本步数据),BigRef 的
      sha256 就是当前 state,uri 里带步数。所以 load(ref) 无需任何文件就能还原状态,
      续训可以精确测试。tag 不进哈希,只放在 fetch_hint 里——否则两条不同路径存出的
      同一状态会因 tag 不同而哈希不同,续训等价就没法比。
    - load 一个非 mock 方案的引用(例如配置里的 base_checkpoint_ref)当作起点权重:
      state = ref.sha256,步数 0。
    - **推理格式(HF)的引用同样当作外部起点**:晋级后下一轮训练的基座正是转换后的那份
      (设计 §6.1)。真实的 ROLL 能否直接加载推理格式作为训练起点**没有核实**(设计 §10
      第 9 条);mock 在这里不区分格式,所以暴露不出该问题——这一行只是把假设写明。
    - crash_at_step:到该 step 时在改动任何状态之前抛 SimulatedCrashError。
    """

    def __init__(self, *, crash_at_step: int | None = None) -> None:
        self._state: str | None = None
        self._steps = 0
        self.crash_at_step = crash_at_step

    @property
    def state_hash(self) -> str | None:
        return self._state

    @property
    def steps_done(self) -> int:
        return self._steps

    def load(self, checkpoint_ref: BigRef) -> None:
        if checkpoint_ref.uri.startswith(_URI_PREFIX) and not _is_inference_format(checkpoint_ref):
            state, steps = _parse_mock_ref(checkpoint_ref)  # 失败时不改动当前状态
        else:
            state, steps = checkpoint_ref.sha256, 0
        self._state, self._steps = state, steps

    def checkpoint_intact(self, checkpoint_ref: BigRef) -> bool:
        """mock 方案的引用能否还原。非 mock 方案无法确认,返回 False。"""
        if not checkpoint_ref.uri.startswith(_URI_PREFIX) or _is_inference_format(checkpoint_ref):
            return False
        try:
            _parse_mock_ref(checkpoint_ref)
        except CheckpointCorruptError:
            return False
        return True

    def _advance(self, phase: str, data: dict) -> int:
        if self._state is None:
            raise ExecutorNotLoadedError("call load() before training")
        step = self._steps
        if self.crash_at_step is not None and step == self.crash_at_step:
            raise SimulatedCrashError(f"simulated crash at step {step}")
        self._state = _sha256({"prev": self._state, "phase": phase, "step": step, "data": data})
        self._steps += 1
        return step

    def sft_step(self, episodes: list[Episode], *, trained_slot: str) -> StepResult:
        if self._state is None:
            raise ExecutorNotLoadedError("call load() before training")
        if not episodes:
            raise ValueError("sft_step needs at least one episode")
        step = self._advance(
            "sft", {"episodes": [episode_digest(e) for e in episodes], "slot": trained_slot}
        )
        return StepResult(step, "sft", {"sft_loss": 1.0 / (1 + step)})

    def grpo_opd_step(
        self,
        rewarded: list[RewardedTrajectory],
        *,
        opd_coefficient: float,
        teacher_model_ref: BigRef,
    ) -> StepResult:
        if self._state is None:
            raise ExecutorNotLoadedError("call load() before training")
        if not rewarded:
            raise ValueError("grpo_opd_step needs at least one trajectory")
        if not (math.isfinite(opd_coefficient) and opd_coefficient >= 0):
            raise ValueError(f"opd_coefficient must be a finite number >= 0, got {opd_coefficient}")
        groups = {
            gid: [r.combined_reward for r in members]
            for gid, members in group_by_id(rewarded).items()
        }
        step = self._advance(
            "grpo_opd",
            {
                "rewards": [[r.episode.id, r.group_id, r.combined_reward] for r in rewarded],
                "opd": opd_coefficient,
                "teacher": teacher_model_ref.sha256,
            },
        )
        grpo_loss = 1.0 / (1 + step)
        opd_loss = 0.5 / (1 + step)  # 系数为 0 时仍然报告:opd_weight 才表示是否生效
        return StepResult(
            step,
            "grpo_opd",
            {
                "grpo_loss": grpo_loss,
                "opd_loss": opd_loss,
                "opd_weight": float(opd_coefficient),
                "total_loss": grpo_loss + opd_coefficient * opd_loss,
                "mean_reward": math.fsum(r.combined_reward for r in rewarded) / len(rewarded),
                "n_groups": float(len(groups)),
                "n_degenerate_groups": float(count_degenerate_groups(groups)),
            },
        )

    def save_checkpoint(self, tag: str) -> BigRef:
        if self._state is None:
            raise ExecutorNotLoadedError("call load() before saving")
        if not tag:
            raise ValueError("tag must be non-empty")
        return BigRef(
            uri=f"{_URI_PREFIX}{self._state}@{self._steps}",
            sha256=self._state,
            size_bytes=0,
            fetch_hint={"type": "mock", "tag": tag},
        )
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run --extra dev pytest tests/test_mt_executor.py -q`
Expected: 26 passed。

- [ ] **Step 5: 全量回归**

Run: `uv run --extra dev pytest -q`
Expected: 625 passed,无警告。

- [ ] **Step 6: 提交**

```bash
git add src/mindtrainer/executor.py tests/test_mt_executor.py
git commit -m "feat: mindtrainer mock executor (hash-chain checkpoints, metric contract, crash injection)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: checkpoint 格式转换与候选注册

**Files:**
- Create: `src/mindtrainer/checkpoint.py`
- Test: `tests/test_mt_checkpoint.py`

**Interfaces:**
- Consumes: `controlplane.envelope`(`BigRef`、`Envelope`、`OwnerRef`、`CURRENT_SCHEMA_VERSION`);`GitStore`;上半段任务 1 的 `controlplane.paths.candidate_checkpoint_file`。测试里用既有的 `controlplane.daemon.read_tree_state` / `reconcile_once` 与 `controlplane.reconcile.CandidateView` 验证形状。
- Produces(任务 14、15 依赖这些名字):
  - `CheckpointConverter`(Protocol):`convert(ref: BigRef) -> BigRef`。
  - `MockConverter`:确定性 mock。
  - `CandidateAlreadyReviewedError(Exception)`。
  - `register_candidate(store, *, run_id, checkpoint_ref, base_checkpoint_ref, config_hash, curve_summary, manifest_ids) -> str`:写 `candidate_checkpoint.yaml`,返回相对路径。

**设计要点:**
- **转换器**:`MockConverter` 只给 uri 加 `.hf` 后缀,sha256 取 `sha256(原哈希 + "hf")`,体现转换后是另一份制品;`fetch_hint` 带 `format: hf` 与 `source_sha256`。重复转换同一个引用会让后缀叠加,所以拒绝。真实的 ROLL → HF/vLLM 转换后置。
- **候选的形状沿用既有 `mind_trainer_stub`**:`type = candidate_checkpoint`、`status = pending_review`、`id = cand-{run_id}`;payload 在原有 `checkpoint_ref`、`train_curve_summary` 之外另加 `training_run_ref`、`base_checkpoint_ref`、`config_hash`;`links = [run_id, 各阶段数据集清单 id]`。
- **`checkpoint_ref` 的键名与形状是契约,不能改**:daemon 晋级时把候选 payload 里的 `checkpoint_ref` 原样带进 `zengen_model`。放进去的必须是**转换后**的引用——诊断评测和 MindAgent 要加载的就是它。所以调和循环和现有端到端测试都不用为 MindTrainer 改一行(有测试用真实的 `reconcile_once` 验证)。
- **重放注册是安全的**:逐字节相同的内容是空操作(`write_product` 保证,有测试数提交数);候选仍在 `pending_review` 时再次注册会更新它(训练曲线可能变了)。
- **候选一旦被审核就拒绝重放**(`CandidateAlreadyReviewedError`):否则崩溃后重放注册会悄悄把 `approved`/`rejected`/`effective` 写回 `pending_review`,把人工审核的结论抹掉。
- **未决(设计 §10 第 9 条)**:候选带的是转换后的推理格式引用,而下一轮训练的基座又是从它解析出来的。ROLL 能否直接加载该格式作为训练起点没有核实;若不能,候选要同时带一个 ROLL 格式的 `training_checkpoint_ref`,daemon 晋级与 `latest_promoted_checkpoint` 要多带、多解析一个键。mock 不区分格式,测试暴露不出这个问题。

- [ ] **Step 1: 写失败测试**

`tests/test_mt_checkpoint.py`:

```python
import hashlib
import subprocess

import pytest

from controlplane.daemon import read_tree_state, reconcile_once
from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, diagnostic_report_file
from controlplane.reconcile import CandidateView
from mindtrainer.checkpoint import CandidateAlreadyReviewedError, MockConverter, register_candidate


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _src():
    return BigRef("mock://ckpt/" + "1" * 64 + "@6", "1" * 64, 7, {"type": "mock", "tag": "final"})


def _register(store, run_id="run-1", **kw):
    args = dict(
        checkpoint_ref=MockConverter().convert(_src()),
        base_checkpoint_ref=BigRef("file:///ckpt/base", "a" * 64, 1),
        config_hash="c" * 64,
        curve_summary={"final_mean_reward": 0.5},
        manifest_ids=["dsm-run-1-sft", "dsm-run-1-grpo_opd"],
    )
    args.update(kw)
    return register_candidate(store, run_id=run_id, **args)


def _commits(root):
    out = subprocess.run(["git", "-C", str(root), "rev-list", "--count", "HEAD"],
                         capture_output=True, text=True, check=True)
    return int(out.stdout)


# ---------------- 转换器 ----------------

def test_conversion_yields_a_different_artifact_with_a_derived_hash():
    src, out = _src(), MockConverter().convert(_src())
    assert out.uri == src.uri + ".hf"
    assert out.sha256 == hashlib.sha256((src.sha256 + "hf").encode()).hexdigest()
    assert out.sha256 != src.sha256 and out.size_bytes == src.size_bytes
    assert out.fetch_hint == {"type": "mock", "format": "hf", "source_sha256": src.sha256}


def test_conversion_is_deterministic_and_input_dependent():
    other = BigRef("mock://ckpt/" + "2" * 64 + "@6", "2" * 64, 7)
    converter = MockConverter()
    assert converter.convert(_src()) == converter.convert(_src())
    assert converter.convert(_src()).sha256 != converter.convert(other).sha256


def test_converting_twice_is_refused_rather_than_stacking_suffixes():
    once = MockConverter().convert(_src())
    with pytest.raises(ValueError, match="already converted"):
        MockConverter().convert(once)


# ---------------- 注册 ----------------

def test_registered_candidate_has_the_shape_the_daemon_and_cli_expect(store):
    path = _register(store)
    assert path == candidate_checkpoint_file("run-1")
    env = store.read_product(path)
    assert (env.type, env.id, env.status) == ("candidate_checkpoint", "cand-run-1", "pending_review")
    assert env.links == ["run-1", "dsm-run-1-sft", "dsm-run-1-grpo_opd"]
    assert set(env.payload) == {
        "checkpoint_ref", "train_curve_summary", "training_run_ref",
        "base_checkpoint_ref", "config_hash",
    }
    assert env.payload["training_run_ref"] == "trainrun-run-1"
    assert env.payload["train_curve_summary"] == {"final_mean_reward": 0.5}


def test_the_candidate_carries_the_converted_ref_not_the_training_format(store):
    converted = MockConverter().convert(_src())
    _register(store, checkpoint_ref=converted)
    stored = store.read_product(candidate_checkpoint_file("run-1")).payload["checkpoint_ref"]
    assert BigRef.from_dict(stored) == converted
    assert stored["uri"].endswith(".hf") and stored["sha256"] != _src().sha256


def test_the_daemon_reads_it_as_a_candidate_awaiting_review(store):
    _register(store)
    assert read_tree_state(store).candidates == [
        CandidateView(id="cand-run-1", status="pending_review", run_id="run-1", has_diagnostic=False)
    ]


def test_reconcile_asks_for_a_diagnostic_without_any_change_to_the_loop(store):
    _register(store)
    actions = reconcile_once(store)
    assert [(a.kind, a.target_id) for a in actions] == [("request_diagnostic", "cand-run-1")]
    assert store.exists(diagnostic_report_file("run-1"))


def test_replaying_the_same_registration_adds_no_commit(store, tmp_path):
    _register(store)
    before = _commits(tmp_path)
    _register(store)
    assert _commits(tmp_path) == before


def test_re_registering_while_still_pending_review_updates_the_candidate(store):
    _register(store, curve_summary={"final_mean_reward": 0.5})
    _register(store, curve_summary={"final_mean_reward": 0.9})
    env = store.read_product(candidate_checkpoint_file("run-1"))
    assert env.payload["train_curve_summary"] == {"final_mean_reward": 0.9}


@pytest.mark.parametrize("reviewed", ["approved", "rejected", "effective"])
def test_a_replay_after_review_refuses_and_keeps_the_human_decision(store, reviewed):
    _register(store)
    path = candidate_checkpoint_file("run-1")
    cand = store.read_product(path)
    cand.status = reviewed
    store.write_product(path, cand, "human: decide")
    with pytest.raises(CandidateAlreadyReviewedError, match=reviewed):
        _register(store)
    assert store.read_product(path).status == reviewed


def test_payload_does_not_alias_the_callers_summary(store):
    summary = {"curve": {"mean": 0.5}}
    _register(store, curve_summary=summary)
    summary["curve"]["mean"] = 99
    stored = store.read_product(candidate_checkpoint_file("run-1")).payload["train_curve_summary"]
    assert stored == {"curve": {"mean": 0.5}}


def test_empty_run_id_is_rejected(store):
    with pytest.raises(ValueError, match="run_id"):
        _register(store, run_id="")
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run --extra dev pytest tests/test_mt_checkpoint.py -q`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindtrainer.checkpoint'`。

- [ ] **Step 3: 写实现**

`src/mindtrainer/checkpoint.py`:

```python
from __future__ import annotations

import hashlib
from typing import Protocol

from controlplane.envelope import CURRENT_SCHEMA_VERSION, BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file

# 固定时间戳:产物内容必须是输入的纯函数,崩溃后重放注册才会逐字节相同。
_NOW = "2026-10-03T00:00:00Z"
_HF_SUFFIX = ".hf"


class CandidateAlreadyReviewedError(Exception):
    """候选已离开 pending_review(被批准、拒绝或已生效)。重放注册若悄悄写回
    pending_review,会把人工审核的结论抹掉,所以拒绝。"""


class CheckpointConverter(Protocol):
    """训练格式 -> 推理格式。诊断评测和 MindAgent 要加载的是转换后的那份。"""

    def convert(self, ref: BigRef) -> BigRef: ...


class MockConverter:
    """确定性 mock:只给 uri 加后缀,sha256 = sha256(原哈希 + "hf"),体现转换后是另一份
    制品。真实的 ROLL -> HF/vLLM 转换后置。重复转换同一个引用会让后缀叠加,所以拒绝。"""

    def convert(self, ref: BigRef) -> BigRef:
        if ref.uri.endswith(_HF_SUFFIX):
            raise ValueError(f"{ref.uri!r} is already converted")
        digest = hashlib.sha256((ref.sha256 + "hf").encode("utf-8")).hexdigest()
        return BigRef(
            uri=ref.uri + _HF_SUFFIX,
            sha256=digest,
            size_bytes=ref.size_bytes,
            fetch_hint={"type": "mock", "format": "hf", "source_sha256": ref.sha256},
        )


def register_candidate(
    store: GitStore,
    *,
    run_id: str,
    checkpoint_ref: BigRef,
    base_checkpoint_ref: BigRef,
    config_hash: str,
    curve_summary: dict,
    manifest_ids: list[str],
) -> str:
    """写 candidate_checkpoint.yaml,形状沿用现有 mind_trainer_stub。

    checkpoint_ref 必须是**转换后**的引用。daemon 晋级时会把 payload["checkpoint_ref"]
    原样带进 zengen_model,所以该键名与形状是契约,不能改。
    逐字节相同的重放是空操作(GitStore.write_product 保证);候选已被审核过则拒绝。"""
    if not run_id:
        raise ValueError("run_id must be non-empty")
    path = candidate_checkpoint_file(run_id)
    if store.exists(path):
        status = store.read_product(path).status
        if status != "pending_review":
            raise CandidateAlreadyReviewedError(
                f"candidate for {run_id} is already {status}; refusing to reset it to pending_review"
            )
    env = Envelope(
        id=f"cand-{run_id}",
        type="candidate_checkpoint",
        version=1,
        created_at=_NOW,
        created_by=OwnerRef(),
        status="pending_review",
        schema_version=CURRENT_SCHEMA_VERSION,
        links=[run_id, *manifest_ids],
        payload={
            "checkpoint_ref": checkpoint_ref.to_dict(),
            "train_curve_summary": dict(curve_summary),
            "training_run_ref": f"trainrun-{run_id}",
            "base_checkpoint_ref": base_checkpoint_ref.to_dict(),
            "config_hash": config_hash,
        },
    )
    store.write_product(path, env, f"candidate_checkpoint: {env.id}")
    return path
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run --extra dev pytest tests/test_mt_checkpoint.py -q`
Expected: 14 passed。

- [ ] **Step 5: 全量回归**

Run: `uv run --extra dev pytest -q`
Expected: 639 passed,无警告。

- [ ] **Step 6: 提交**

```bash
git add src/mindtrainer/checkpoint.py tests/test_mt_checkpoint.py
git commit -m "feat: mindtrainer checkpoint converter and candidate registration (idempotent, never resets a reviewed candidate)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 13: 续训判定

**Files:**
- Create: `src/mindtrainer/resume.py`
- Test: `tests/test_mt_resume.py`

**Interfaces:**
- Consumes: `controlplane.envelope.BigRef`;任务 3 的 `TrainingConfig`;任务 10 的 `TrainingRun`。
- Produces(任务 14 依赖这些名字):
  - `ACTIONS = ("noop", "resume", "restart", "escalate")`。
  - `ResumeDecision(action, reason, checkpoint_ref=None, from_step=0)`:frozen dataclass。
  - `decide_resume(run, config, *, checkpoint_intact: bool | None) -> ResumeDecision`:纯函数,`checkpoint_intact` 是关键字必填参数。
  - `NeedsHumanError(reason)`:`escalate` 的落点(T3,需人工),属性 `reason`。

**判定顺序(先命中者生效):**

| # | 条件 | 动作 | 说明 |
|---|---|---|---|
| 1 | `status` 为 `executed` / `consumed` | `noop` | 已经做完 |
| 2 | `status == "failed"` | `escalate` | 失败已落盘,不自动重跑(补充 E.3 机制三) |
| 3 | `resume_attempts >= max_resume_attempts` | `escalate` | 否则每次续训都在同一步崩溃会无限循环 |
| 4 | `allow_resume` 为假 | `escalate` | 配置禁止 |
| 5 | 有 `last_checkpoint_ref` | `intact is True` → `resume`;`False` 或 `None` → `escalate` | 无法确认也按中间态损坏处理,不猜 |
| 6 | 从未存过点 | `restart` | 从 `base_checkpoint_ref` 的 step 0 重来 |

**设计要点:**
- **第 6 条是设计里的解读**:spec 写的是"允许就续,不允许或损坏就 T3",没覆盖"还没存过点"。这时什么都没丢,从头跑等价于新运行,判 T3 反而让人去处理一件没坏的事。
- **补充 E.2 与 E.4 有一处张力**:E.2 把"训练崩溃"列为 T3、不自动重跑,E.4 又说允许就从中间 checkpoint 续。这里按"E.4 是训练崩溃的专门规则,T3 是续不了时的结局"来读。
- **次数上限的边界**:`resume_attempts` 刚好等于上限就升级,少一次仍可续(有测试);第 3 条排在第 4 条之前,所以两者同时成立时报告的是"次数用尽"。
- **`checkpoint_intact` 由调用方算**(来自任务 11 执行器的 `checkpoint_intact`):计算层算好传进来,控制层只判"该不该续、从哪续",对应 E.4 的分工。没有 checkpoint 时它被忽略。
- **每次 `resume` 或 `restart` 之后由调用方把 `resume_attempts` 加一**(任务 14 做),第 3 条才会生效。
- 未知的 `status` 抛 `ValueError`,不猜。

- [ ] **Step 1: 写失败测试**

`tests/test_mt_resume.py`:

```python
import dataclasses

import pytest

from controlplane.envelope import BigRef
from mindtrainer.datamodel import TrainingRun
from mindtrainer.resume import ACTIONS, NeedsHumanError, ResumeDecision, decide_resume
from mindtrainer.training_config import RewardConfig, TrainingConfig

CKPT = BigRef("mock://ckpt/" + "9" * 64 + "@4", "9" * 64, 0)


def _cfg(**kw):
    cfg = TrainingConfig(
        mode="batch", trained_slot="a",
        base_checkpoint_ref=BigRef("file:///ckpt/a", "a" * 64, 1),
        teacher_model_ref=BigRef("file:///ckpt/b", "b" * 64, 1),
        reward=RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]}),
        sft_steps=2, grpo_steps=4, sft_batch_size=2, groups_per_step=2, group_size=2,
        grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=2, allow_resume=True,
        stage_manifest_refs={"sft": "m1", "grpo_opd": "m2"},
    )
    return dataclasses.replace(cfg, **kw)


def _run(**kw):
    base = dict(run_id="run-1", config_hash="c" * 64)
    base.update(kw)
    return TrainingRun(**base)


def _checkpointed(**kw):
    return _run(checkpoint_step=4, last_checkpoint_ref=CKPT, **kw)


def _decide(run, cfg=None, intact=True):
    return decide_resume(run, cfg or _cfg(), checkpoint_intact=intact)


# ---------------- 1. 已结束 ----------------

@pytest.mark.parametrize("status", ["executed", "consumed"])
def test_a_finished_run_is_left_alone_even_if_everything_else_looks_bad(status):
    run = _checkpointed(status=status, resume_attempts=99)
    decision = _decide(run, _cfg(allow_resume=False), intact=False)
    assert decision.action == "noop" and "already" in decision.reason


# ---------------- 2. 失败 ----------------

def test_a_failed_run_escalates_and_is_never_rerun_automatically():
    run = _checkpointed(status="failed", failure={"category": "T2"})
    decision = _decide(run)
    assert decision.action == "escalate" and "failed deterministically" in decision.reason
    assert decision.checkpoint_ref is None


# ---------------- 3. 尝试次数 ----------------

def test_attempts_exactly_at_the_limit_escalate_and_one_below_still_resumes():
    cfg = _cfg(max_resume_attempts=3)
    at_limit = _decide(_checkpointed(resume_attempts=3), cfg)
    below = _decide(_checkpointed(resume_attempts=2), cfg)
    assert (at_limit.action, below.action) == ("escalate", "resume")
    assert "exhausted" in at_limit.reason and "3 >= 3" in at_limit.reason


def test_zero_allowed_attempts_escalates_even_a_run_with_no_checkpoint():
    decision = _decide(_run(), _cfg(max_resume_attempts=0))
    assert decision.action == "escalate" and "exhausted" in decision.reason


def test_exhausted_attempts_are_reported_before_the_resume_ban():
    decision = _decide(_checkpointed(resume_attempts=3), _cfg(max_resume_attempts=3, allow_resume=False))
    assert "exhausted" in decision.reason


# ---------------- 4. 不允许续训 ----------------

@pytest.mark.parametrize("run", [_checkpointed(), _run()], ids=["with_checkpoint", "no_checkpoint"])
def test_resume_ban_escalates_even_when_a_restart_would_otherwise_be_possible(run):
    decision = _decide(run, _cfg(allow_resume=False))
    assert decision.action == "escalate" and "allow_resume" in decision.reason


# ---------------- 5. 有 checkpoint ----------------

def test_an_intact_checkpoint_resumes_from_its_step_with_its_ref():
    decision = _decide(_checkpointed(), intact=True)
    assert decision == ResumeDecision(
        "resume", "resume from the checkpoint at step 4", checkpoint_ref=CKPT, from_step=4
    )
    assert decision.checkpoint_ref is not None and decision.checkpoint_ref == CKPT


def test_a_checkpoint_that_fails_the_check_escalates_as_corruption():
    decision = _decide(_checkpointed(), intact=False)
    assert decision.action == "escalate" and "integrity" in decision.reason


def test_an_unconfirmable_checkpoint_escalates_instead_of_guessing():
    decision = _decide(_checkpointed(), intact=None)
    assert decision.action == "escalate" and "cannot confirm" in decision.reason


# ---------------- 6. 没有 checkpoint ----------------

@pytest.mark.parametrize("intact", [True, False, None])
def test_a_run_that_never_saved_restarts_from_the_base_whatever_the_flag_says(intact):
    decision = _decide(_run(), intact=intact)
    assert decision.action == "restart"
    assert (decision.checkpoint_ref, decision.from_step) == (None, 0)


# ---------------- 其余 ----------------

def test_an_unknown_status_is_an_error_not_a_guess():
    with pytest.raises(ValueError, match="unknown training_run status 'weird'"):
        _decide(_run(status="weird"))


def test_every_action_returned_is_a_declared_action():
    runs = [_run(status="executed"), _run(status="failed", failure={"category": "T2"}),
            _run(), _checkpointed(), _checkpointed(resume_attempts=9)]
    for run in runs:
        for intact in (True, False, None):
            assert _decide(run, intact=intact).action in ACTIONS


def test_the_decision_is_pure_and_frozen():
    run = _checkpointed(resume_attempts=1)
    snapshot = run.to_dict()
    first, second = _decide(run), _decide(run)
    assert first == second and run.to_dict() == snapshot
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.action = "noop"


def test_the_intactness_flag_cannot_be_left_out():
    with pytest.raises(TypeError):
        decide_resume(_run(), _cfg())


def test_needs_human_error_carries_the_reason():
    err = NeedsHumanError("attempts exhausted")
    assert err.reason == "attempts exhausted" and str(err) == "attempts exhausted"
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run --extra dev pytest tests/test_mt_resume.py -q`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindtrainer.resume'`。

- [ ] **Step 3: 写实现**

`src/mindtrainer/resume.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from controlplane.envelope import BigRef
from mindtrainer.datamodel import TrainingRun
from mindtrainer.training_config import TrainingConfig

ACTIONS = ("noop", "resume", "restart", "escalate")


class NeedsHumanError(Exception):
    """escalate 的落点(T3,需人工)。reason 说明为什么不能自动续训。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ResumeDecision:
    action: str  # ACTIONS 之一
    reason: str
    checkpoint_ref: BigRef | None = None  # 仅 resume 有值
    from_step: int = 0  # resume 时为 checkpoint_step;restart 为 0


def decide_resume(
    run: TrainingRun, config: TrainingConfig, *, checkpoint_intact: bool | None
) -> ResumeDecision:
    """续训判定,纯函数。按下面的顺序,先命中者生效:

    1. executed/consumed -> noop
    2. failed -> escalate(失败已落盘,不自动重跑;补充 E.3 机制三)
    3. resume_attempts >= max_resume_attempts -> escalate(否则每次续训都在同一步崩溃会无限循环)
    4. allow_resume 为假 -> escalate
    5. 有 last_checkpoint_ref:checkpoint_intact 为 True -> resume;False 或 None(无法确认)
       -> escalate,按中间态损坏处理
    6. 没有任何 checkpoint(崩在第一次存点之前) -> restart,从 base_checkpoint_ref 的 step 0 重来

    第 6 条是设计里的解读:spec 没覆盖"还没存过点",这时什么都没丢,从头跑等价于新运行,
    判 T3 反而让人去处理一件没坏的事。
    checkpoint_intact 由调用方算(来自执行器能否重建该 checkpoint):计算层算好传进来,
    控制层只判"该不该续、从哪续"。没有 checkpoint 时它被忽略。
    调用方每次 resume 或 restart 之后要把 resume_attempts 加一,第 3 条才会生效。"""
    if run.status in ("executed", "consumed"):
        return ResumeDecision("noop", f"run {run.run_id} is already {run.status}")
    if run.status == "failed":
        return ResumeDecision(
            "escalate",
            f"run {run.run_id} failed deterministically and is kept; it is not rerun automatically",
        )
    if run.status != "pending":
        raise ValueError(f"unknown training_run status {run.status!r}")
    if run.resume_attempts >= config.max_resume_attempts:
        return ResumeDecision(
            "escalate",
            f"resume attempts exhausted ({run.resume_attempts} >= {config.max_resume_attempts})",
        )
    if not config.allow_resume:
        return ResumeDecision("escalate", "resume is not allowed by the config (allow_resume=false)")
    if run.last_checkpoint_ref is not None:
        if checkpoint_intact is True:
            return ResumeDecision(
                "resume",
                f"resume from the checkpoint at step {run.checkpoint_step}",
                checkpoint_ref=run.last_checkpoint_ref,
                from_step=run.checkpoint_step,
            )
        if checkpoint_intact is False:
            return ResumeDecision(
                "escalate",
                f"the checkpoint at step {run.checkpoint_step} failed its integrity check",
            )
        return ResumeDecision(
            "escalate",
            f"cannot confirm the checkpoint at step {run.checkpoint_step} is intact",
        )
    return ResumeDecision("restart", "no checkpoint was ever saved; restart from the base checkpoint")
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run --extra dev pytest tests/test_mt_resume.py -q`
Expected: 19 passed。

- [ ] **Step 5: 全量回归**

Run: `uv run --extra dev pytest -q`
Expected: 658 passed,无警告。

- [ ] **Step 6: 提交**

```bash
git add src/mindtrainer/resume.py tests/test_mt_resume.py
git commit -m "feat: mindtrainer resume decision (pure function; escalates instead of guessing)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 14: 训练编排器

**Files:**
- Create: `src/mindtrainer/orchestrator.py`
- Test: `tests/test_mt_orchestrator.py`

**Interfaces:**
- Consumes: 上半段的全部公开名字(`TrainingConfig`、`validate_training_config`、`config_hash`、`total_steps`、`phase_at`、`STAGES`、`BatchTrajectorySource`、`InteractTrajectorySource`、`batch_for_step`、`run_reward_pipeline`、`summarize_rewards`、`build_attribution_backend`、`group_by_id`、`validate_groups`、`DegenerateGroupError`、`AttributionError`);本计划的 `StepResult`、`TrainingRun`、`read_training_run`、`write_training_run`、`TrainingExecutor` / `MockExecutor`、`CheckpointConverter` / `MockConverter`、`register_candidate`、`decide_resume`、`NeedsHumanError`;`simulator.dataset.DatasetIntegrityError`。
- Produces:
  - `run_training(cfg, *, store, run_id, make_sources, attribution, executor, converter, resume_from=None, metric_stream_refs=None, on_step=None) -> TrainingRun`
  - `resume_or_run(cfg, *, store, run_id, make_sources, attribution, executor, converter, metric_stream_refs=None, on_step=None) -> TrainingRun`:入口。
  - `NotEnoughGroupsError(ValueError)`。
  - `SourcesFactory = Callable[[int], dict[str, TrajectorySource]]`:参数是**已完成的 GRPO step 数**,返回 `{"sft": 源, "grpo_opd": 源}`。

**与设计 §7.1 的签名差异(设计文档在计划自检时同步修订):**
1. 设计写的是 `source`,这里是 **`make_sources` 工厂**。原因:Interact 的轨迹源在续训时要从正确的 scenario 轮转位置起步(`start_step`),而这取决于续训点;SFT 与 GRPO 的源也不是同一个。
2. 多了 `metric_stream_refs`(调用方写好指标流文件,只把引用交给这里记录)和 `on_step`(逐步指标不进 git,这是调用方观察每一步的唯一出口)。

**设计要点:**
- **薄编排**:不含判定逻辑,只调下层纯函数和协议(沿用 `diagnostic.orchestrator.run_diagnostic` 的做法)。
- **起点**:新运行从 `base_checkpoint_ref` 的 step 0 起;`resume_from` 带中间 checkpoint 则从 `checkpoint_step` 起,不带(崩在第一次存点之前)则从头重来。**两种续训都先把 `resume_attempts` 加一并落盘,再动执行器**——即便 `load` 本身反复失败,次数也会触顶升级。
- **配置变了不能续**:`config_hash` 与记录里的不一致就抛 `NeedsHumanError`,不改动任何记录。
- **续训等价(Batch)**:每个 step 用的数据是 step 的纯函数——SFT 取 `batch_for_step(episodes, step, sft_batch_size)`,GRPO 取 `batch_for_step(组 id, grpo 序号, groups_per_step)` 个完整组——所以"一次跑完"与"崩溃后从存点续"最终 checkpoint 逐字节相同。**测试在 6 个全局 step 的每一步各注入一次崩溃**。**Interact 不声称等价**:它的 rollout 是实时的,spec §5.1 说的"高度相似而非字节一致"才是上限。
- **Batch 的 GRPO 数据在训练开始前就整份校验**(奖励管线、`validate_groups`、组数够不够 `groups_per_step`),而不是等 SFT 跑完:数据有问题时一步都不浪费。组数不够会让 `batch_for_step` 回绕、同一个组一步里出现两次,所以是 `NotEnoughGroupsError`。
- **存点节奏**:每 `checkpoint_every_steps` 步存点,并把引用、`checkpoint_step` 与**当时的曲线摘要**一起回写。曲线摘要只在存点时落盘,所以续训后它与存点时一致,不会重复计入崩溃前多跑的步。
- **收尾顺序**:最终 checkpoint **先回写记录**,再转换、注册候选,最后置 `executed`。转换或注册崩了,续训直接回到这里收尾,一步都不重训(有测试)。
- **失败处理**:确定性失败(`DatasetIntegrityError`、`DegenerateGroupError`、`AttributionError`、`NotEnoughGroupsError`、`InvalidTrainingConfigError`)写 `failed`(category `T2`,`point` 记在哪一步)并原样重抛;**其余异常不吞**,记录保持 `pending`,由 `decide_resume` 在下次进入时判定。执行器报告的 step 与编排器不一致时抛 `RuntimeError`,不当成确定性失败。
- **`resume_or_run`**:读已有记录,交给 `decide_resume`;`noop` 直接返回已有记录(执行器根本不会被 `load`);`resume`/`restart` 进入 `run_training`;`escalate` 抛 `NeedsHumanError`。
- **Interact 的 `start_step`** = `max(0, 起点全局 step − sft_steps)`;失败尝试数(源报告的 `failed_attempts`)累计进曲线摘要的 `failed_rollout_attempts`。
- 测试里的 Interact 用例走真实 loopback gRPC 服务端;整个文件约需 25 秒,主要是 6 个崩溃点的参数化用例。

- [ ] **Step 1: 写失败测试**

`tests/test_mt_orchestrator.py`:

```python
import dataclasses
import time
from types import SimpleNamespace

import grpc
import pytest

from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from controlplane.observe import trace
from controlplane.paths import candidate_checkpoint_file
from mindtrainer.checkpoint import MockConverter
from mindtrainer.datamodel import StepResult, TrainingRun, TrajectoryBatch
from mindtrainer.executor import MockExecutor, SimulatedCrashError
from mindtrainer.grouping import DegenerateGroupError
from mindtrainer.orchestrator import NotEnoughGroupsError, resume_or_run, run_training
from mindtrainer.resume import NeedsHumanError
from mindtrainer.reward_pipeline import AttributionError, build_attribution_backend
from mindtrainer.store_adapter import read_training_run, write_training_run
from mindtrainer.training_config import InvalidTrainingConfigError, RewardConfig, TrainingConfig
from mindtrainer.trajectory_source import BatchTrajectorySource, InteractTrajectorySource
from simulator.backends import ScriptedBackend
from simulator.dataset import DatasetIntegrityError, produce_grouped_dataset, read_manifest
from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
from simulator.realtime.client import EnvironmentServiceClient
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server
from simulator.store_adapter import (
    read_episode, read_scenario, write_episode, write_persona, write_scenario,
)

RUN = "run-1"
BASE = BigRef("file:///ckpt/base", "a" * 64, 1)
TEACHER = BigRef("file:///ckpt/teacher", "b" * 64, 1)
REWARD = RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]})


# ---------------- 世界:数据集、配置、源 ----------------

def _trained():
    return RoleBinding("a", "pa", "self_play")


def _opponent():
    return RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")


def _scenario(sid):
    return Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")


def _personas():
    return {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}


def _rollout(scenario, k):
    """k=0 的训练角色位发言含 "agreed"(奖励 1.0),k=1 不含(奖励 0.0):组内有差异。"""
    first = ("agreed " if k == 0 else "no ") + f"{scenario.id}-a{k}"
    return {
        "a": ScriptedBackend({"a": [first, f"{scenario.id}-a{k}-2"]}),
        "b": ScriptedBackend({"b": [f"{scenario.id}-b{k}-1", f"{scenario.id}-b{k}-2"]}),
    }


def _store(tmp_path, name="repo"):
    s = GitStore(str(tmp_path / name))
    s.init()
    return s


def _populate(store, *, grpo_group_size=2):
    for name, size in (("sft", 1), ("grpo_opd", grpo_group_size)):
        produce_grouped_dataset(
            store, RUN, name,
            scenarios=[
                (_scenario(s), RoleAssignment(s, [_trained(), _opponent()], max_turns=4))
                for s in ("s1", "s2")
            ],
            personas=_personas(), backends_for_rollout=_rollout, group_size=size,
        )
    for sid in ("s1", "s2", "s3"):
        write_scenario(store, _scenario(sid))
    for persona in _personas().values():
        write_persona(store, persona)


@pytest.fixture
def store(tmp_path):
    s = _store(tmp_path)
    _populate(s)
    return s


def _batch_cfg(**kw):
    base = dict(
        mode="batch", trained_slot="a", base_checkpoint_ref=BASE, teacher_model_ref=TEACHER,
        reward=REWARD, sft_steps=2, grpo_steps=4, sft_batch_size=1, groups_per_step=1,
        group_size=2, grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=2,
        allow_resume=True,
        stage_manifest_refs={"sft": "dsm-run-1-sft", "grpo_opd": "dsm-run-1-grpo_opd"},
    )
    base.update(kw)
    return TrainingConfig(**base)


def _batch_sources(store, calls=None):
    def make(start):
        if calls is not None:
            calls.append(start)
        return {
            name: BatchTrajectorySource(store, read_manifest(store, RUN, name))
            for name in ("sft", "grpo_opd")
        }
    return make


def _go(store, cfg, executor, *, make_sources=None, converter=None, attribution=None,
        steps=None, **kw):
    return resume_or_run(
        cfg, store=store, run_id=RUN,
        make_sources=make_sources or _batch_sources(store),
        attribution=attribution or build_attribution_backend(cfg.reward),
        executor=executor, converter=converter or MockConverter(),
        on_step=steps.append if steps is not None else None, **kw,
    )


def _candidate(store):
    return store.read_product(candidate_checkpoint_file(RUN))


# ====================== Batch 端到端 ======================

def test_a_batch_run_trains_every_step_and_registers_a_candidate(store):
    ex, steps = MockExecutor(), []
    run = _go(store, _batch_cfg(), ex, steps=steps)
    assert run.status == "executed"
    assert [(s.step, s.phase) for s in steps] == [
        (0, "sft"), (1, "sft"), (2, "grpo_opd"), (3, "grpo_opd"), (4, "grpo_opd"), (5, "grpo_opd"),
    ]
    assert ex.steps_done == 6
    assert (run.checkpoint_step, run.resume_attempts, run.failure) == (6, 0, None)
    assert read_training_run(store, RUN) == run
    cand = _candidate(store)
    assert cand.status == "pending_review"
    assert cand.links == [RUN, "dsm-run-1-sft", "dsm-run-1-grpo_opd"]
    assert BigRef.from_dict(cand.payload["checkpoint_ref"]) == MockConverter().convert(run.last_checkpoint_ref)


def test_the_curve_summary_records_what_the_orchestrator_computed(store):
    summary = _go(store, _batch_cfg(), MockExecutor()).curve_summary
    assert summary["sft_steps_done"] == 2 and summary["grpo_steps_done"] == 4
    assert summary["first_mean_reward"] == 0.5 and summary["final_mean_reward"] == 0.5
    assert summary["final_opd_weight"] == 0.1
    assert (summary["degenerate_groups_total"], summary["failed_rollout_attempts"]) == (0, 0)


def test_metric_stream_refs_are_kept_and_visible_to_trace(store, tmp_path):
    stream = tmp_path / "metrics.jsonl"
    stream.write_text("{}\n")
    ref = BigRef(stream.as_uri(), "d" * 64, 3)
    run = _go(store, _batch_cfg(), MockExecutor(), metric_stream_refs=[ref])
    assert run.metric_stream_refs == [ref]
    assert f"metric_ref [ok] {stream.as_uri()}" in trace(store, RUN)


# ====================== 续训等价(核心) ======================

CRASH_START = {0: 0, 1: 0, 2: 2, 3: 2, 4: 4, 5: 4}  # 崩在哪一步 -> 续训从哪一步起


@pytest.mark.parametrize("crash_at", sorted(CRASH_START))
def test_crash_then_resume_reaches_exactly_the_result_of_an_uninterrupted_run(tmp_path, crash_at):
    straight_store = _store(tmp_path, "straight")
    _populate(straight_store)
    straight_ex = MockExecutor()
    straight = _go(straight_store, _batch_cfg(), straight_ex)

    store = _store(tmp_path, "crashy")
    _populate(store)
    calls, steps = [], []
    with pytest.raises(SimulatedCrashError):
        _go(store, _batch_cfg(), MockExecutor(crash_at_step=crash_at),
            make_sources=_batch_sources(store, calls))
    pending = read_training_run(store, RUN)
    assert (pending.status, pending.failure) == ("pending", None)
    assert pending.checkpoint_step == CRASH_START[crash_at]

    fresh = MockExecutor()  # 新进程:没有任何内存状态
    resumed = _go(store, _batch_cfg(), fresh, make_sources=_batch_sources(store, calls), steps=steps)

    assert [s.step for s in steps] == list(range(CRASH_START[crash_at], 6))
    assert calls == [0, max(0, CRASH_START[crash_at] - 2)]
    assert fresh.state_hash == straight_ex.state_hash
    assert resumed.resume_attempts == 1
    assert resumed.curve_summary == straight.curve_summary
    assert _candidate(store).payload["checkpoint_ref"] == _candidate(straight_store).payload["checkpoint_ref"]


class _FlakyConverter(MockConverter):
    def __init__(self):
        self.calls = 0

    def convert(self, ref):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("converter down")
        return super().convert(ref)


def test_a_failure_after_the_final_checkpoint_resumes_straight_into_registration(store):
    flaky = _FlakyConverter()
    with pytest.raises(RuntimeError, match="converter down"):
        _go(store, _batch_cfg(), MockExecutor(), converter=flaky)
    pending = read_training_run(store, RUN)
    assert (pending.status, pending.checkpoint_step) == ("pending", 6)
    assert not store.exists(candidate_checkpoint_file(RUN))

    steps = []
    run = _go(store, _batch_cfg(), MockExecutor(), converter=flaky, steps=steps)
    assert steps == []  # 没有重训任何一步
    assert run.status == "executed" and store.exists(candidate_checkpoint_file(RUN))


# ====================== 升级(T3)与幂等 ======================

def test_resume_attempts_are_counted_and_eventually_escalate(store):
    cfg = _batch_cfg(max_resume_attempts=1)
    with pytest.raises(SimulatedCrashError):
        _go(store, cfg, MockExecutor(crash_at_step=3))
    with pytest.raises(SimulatedCrashError):  # 第 1 次续训,又崩在同一步
        _go(store, cfg, MockExecutor(crash_at_step=3))
    assert read_training_run(store, RUN).resume_attempts == 1  # 次数在动执行器之前就落盘了
    with pytest.raises(NeedsHumanError, match="exhausted"):
        _go(store, cfg, MockExecutor())


def test_a_config_that_forbids_resuming_escalates(store):
    cfg = _batch_cfg(allow_resume=False)
    with pytest.raises(SimulatedCrashError):
        _go(store, cfg, MockExecutor(crash_at_step=3))
    with pytest.raises(NeedsHumanError, match="allow_resume"):
        _go(store, cfg, MockExecutor())


def test_a_changed_config_cannot_resume_the_run_and_nothing_is_modified(store):
    with pytest.raises(SimulatedCrashError):
        _go(store, _batch_cfg(), MockExecutor(crash_at_step=3))
    before = read_training_run(store, RUN)
    with pytest.raises(NeedsHumanError, match="config changed"):
        _go(store, _batch_cfg(opd_coefficient=0.2), MockExecutor())
    assert read_training_run(store, RUN) == before


def test_a_finished_run_is_returned_untouched_and_the_executor_never_loaded(store):
    first = _go(store, _batch_cfg(), MockExecutor())
    ex, steps = MockExecutor(), []
    again = _go(store, _batch_cfg(), ex, steps=steps)
    assert again == first and steps == [] and ex.state_hash is None


# ====================== 确定性失败 ======================

def test_a_tampered_sft_episode_fails_the_run_for_good(store):
    target = read_manifest(store, RUN, "sft").episode_refs[0]["id"]
    episode = read_episode(store, RUN, target)
    episode.turns[0].utterance = "rewritten after the manifest was built"
    write_episode(store, RUN, episode)

    ex = MockExecutor()
    with pytest.raises(DatasetIntegrityError, match="sha256 mismatch"):
        _go(store, _batch_cfg(), ex)
    failed = read_training_run(store, RUN)
    assert failed.status == "failed"
    assert (failed.failure["category"], failed.failure["point"]) == ("T2", "step 0 (sft)")
    assert "DatasetIntegrityError" in failed.failure["detail"]
    assert ex.steps_done == 0 and not store.exists(candidate_checkpoint_file(RUN))
    with pytest.raises(NeedsHumanError, match="failed"):  # 失败被保留,不自动重跑
        _go(store, _batch_cfg(), MockExecutor())


def test_undersized_groups_fail_before_a_single_training_step(tmp_path):
    store = _store(tmp_path)
    _populate(store, grpo_group_size=1)
    ex = MockExecutor()
    with pytest.raises(DegenerateGroupError) as exc:
        _go(store, _batch_cfg(), ex)
    assert len(exc.value.group_ids) == 2
    assert ex.steps_done == 0  # 连 SFT 也没有跑:数据有问题时一步都不浪费
    failed = read_training_run(store, RUN)
    assert (failed.status, failed.failure["point"]) == ("failed", "grpo precheck")


def test_asking_for_more_groups_per_step_than_exist_fails(store):
    with pytest.raises(NotEnoughGroupsError, match="3 exceeds the 2 group"):
        _go(store, _batch_cfg(groups_per_step=3), MockExecutor())
    assert read_training_run(store, RUN).status == "failed"


def test_an_invalid_config_is_recorded_as_failed_and_touches_nothing_else(store):
    calls, ex = [], MockExecutor()
    with pytest.raises(InvalidTrainingConfigError, match="below min_group_size"):
        _go(store, _batch_cfg(group_size=1), ex, make_sources=_batch_sources(store, calls))
    failed = read_training_run(store, RUN)
    assert (failed.status, failed.failure["point"]) == ("failed", "config")
    assert calls == [] and ex.state_hash is None


class _ConstJudge:
    model_id = "external-judge"

    def score_turn(self, episode, turn_index, component):
        return 1.5  # 越界


def test_an_unusable_attribution_fails_the_run(store):
    reward = RewardConfig(["goal"], "judge", "sum", judge_model_id="external-judge", rubric_version="r1")
    attribution = build_attribution_backend(reward, judge=_ConstJudge(), trained_model_id="zengen-v0")
    with pytest.raises(AttributionError, match="not a finite number in"):
        _go(store, _batch_cfg(reward=reward), MockExecutor(), attribution=attribution)
    assert read_training_run(store, RUN).failure["point"] == "grpo precheck"


# ====================== 不吞异常、不替调用方掩盖 bug ======================

class _SkewedExecutor(MockExecutor):
    def sft_step(self, episodes, *, trained_slot):
        real = super().sft_step(episodes, trained_slot=trained_slot)
        return StepResult(real.step + 1, real.phase, real.metrics)


def test_an_executor_that_loses_count_of_steps_is_not_silently_accepted(store):
    with pytest.raises(RuntimeError, match="out of sync"):
        _go(store, _batch_cfg(), _SkewedExecutor())
    assert read_training_run(store, RUN).status == "pending"  # 不是确定性失败,不写 failed


def test_a_missing_stage_source_is_a_caller_bug_and_writes_nothing(store):
    only_sft = lambda start: {  # noqa: E731
        "sft": BatchTrajectorySource(store, read_manifest(store, RUN, "sft"))
    }
    with pytest.raises(ValueError, match="no 'grpo_opd' source"):
        _go(store, _batch_cfg(), MockExecutor(), make_sources=only_sft)
    assert read_training_run(store, RUN) is None


def test_run_training_validates_the_record_it_is_asked_to_resume(store):
    cfg = _batch_cfg()
    common = dict(
        store=store, run_id=RUN, make_sources=_batch_sources(store),
        attribution=build_attribution_backend(cfg.reward), executor=MockExecutor(),
        converter=MockConverter(),
    )
    with pytest.raises(ValueError, match="belongs to 'other'"):
        run_training(cfg, resume_from=TrainingRun("other", "c" * 64), **common)
    with pytest.raises(ValueError, match="only a pending run"):
        run_training(cfg, resume_from=TrainingRun(RUN, "c" * 64, status="executed"), **common)


# ====================== Interact 端到端(真实 loopback 服务端) ======================

def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def live(store):
    channels, servers = [], []

    def start():
        closed = []

        def factory(scenario, assignment, personas, client_slot):
            return {
                s: ScriptedBackend({s: ["b1", "b2", "b3", "b4"]})
                for s in assignment.slot_ids() - {client_slot}
            }

        server = create_server(
            factory, port=0, turn_timeout_s=2.0, on_session_closed=closed.append,
            scenario_resolver=lambda sid: read_scenario(store, sid),
        )
        server.start()
        servers.append(server)

        def client_factory():
            channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
            channels.append(channel)
            return EnvironmentServiceClient(pb2_grpc.EnvironmentServiceStub(channel))

        return SimpleNamespace(closed=closed, client_factory=client_factory)

    yield start
    for channel in channels:
        channel.close()
    for server in servers:
        server.stop(0)


def _interact_cfg(**kw):
    base = dict(
        mode="interact", trained_slot="a", base_checkpoint_ref=BASE, teacher_model_ref=TEACHER,
        reward=REWARD, sft_steps=1, grpo_steps=3, sft_batch_size=1, groups_per_step=1,
        group_size=2, grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=2,
        allow_resume=True, stage_manifest_refs={"sft": "dsm-run-1-sft"},
        scenario_ids=["s1", "s2", "s3"], bindings=[_trained(), _opponent()],
        max_turns_per_episode=4,
    )
    base.update(kw)
    return TrainingConfig(**base)


def _interact_sources(store, env, cfg, calls):
    def make(start):
        calls.append(start)
        # 每个 rollout 训练角色位说两句:偶数 rollout 含 "agreed"(奖励 1.0),奇数不含(0.0)
        policy = ScriptedBackend({"a": ["agreed", "x", "no", "x"] * 8})
        return {
            "sft": BatchTrajectorySource(store, read_manifest(store, RUN, "sft")),
            "grpo_opd": InteractTrajectorySource(
                env.client_factory, policy, store, cfg, start_step=start
            ),
        }
    return make


def test_an_interact_run_collects_fresh_rollouts_for_every_grpo_step(store, live):
    env, cfg, calls, steps = live(), _interact_cfg(), [], []
    run = _go(store, cfg, MockExecutor(), steps=steps,
              make_sources=_interact_sources(store, env, cfg, calls))
    assert run.status == "executed" and calls == [0]
    assert [s.phase for s in steps] == ["sft", "grpo_opd", "grpo_opd", "grpo_opd"]
    for s in steps[1:]:
        assert (s.metrics["n_groups"], s.metrics["n_degenerate_groups"], s.metrics["mean_reward"]) == (1.0, 0.0, 0.5)
    assert _wait_until(lambda: len(env.closed) == 6)
    assert [e.scenario_id for e in env.closed] == ["s1", "s1", "s2", "s2", "s3", "s3"]
    assert run.curve_summary["grpo_steps_done"] == 3
    assert store.exists(candidate_checkpoint_file(RUN))


def test_an_interact_resume_continues_the_scenario_rotation(store, live):
    env, cfg, calls = live(), _interact_cfg(), []
    with pytest.raises(SimulatedCrashError):  # 全局第 3 步 = 第 3 个 GRPO step,收集之后、训练之时崩
        _go(store, cfg, MockExecutor(crash_at_step=3),
            make_sources=_interact_sources(store, env, cfg, calls))
    assert _wait_until(lambda: len(env.closed) == 6)
    assert read_training_run(store, RUN).checkpoint_step == 2

    run = _go(store, cfg, MockExecutor(), make_sources=_interact_sources(store, env, cfg, calls))
    assert calls == [0, 1]  # 续训从第 2 个 GRPO step(序号 1)接着轮转
    assert _wait_until(lambda: len(env.closed) == 10)
    assert [e.scenario_id for e in env.closed[6:]] == ["s2", "s2", "s3", "s3"]
    assert (run.status, run.resume_attempts, run.curve_summary["grpo_steps_done"]) == ("executed", 1, 3)


# ====================== 接线:每步数据来自 batch_for_step;Interact 的失败尝试与组校验 ======================

class _Recording(MockExecutor):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.sft_ids, self.grpo_gids = [], []

    def sft_step(self, episodes, *, trained_slot):
        self.sft_ids.append([e.id for e in episodes])
        return super().sft_step(episodes, trained_slot=trained_slot)

    def grpo_opd_step(self, rewarded, *, opd_coefficient, teacher_model_ref):
        self.grpo_gids.append(sorted({r.group_id for r in rewarded}))
        return super().grpo_opd_step(
            rewarded, opd_coefficient=opd_coefficient, teacher_model_ref=teacher_model_ref
        )


def test_each_sft_step_takes_the_next_episode_of_the_manifest(store):
    ex = _Recording()
    _go(store, _batch_cfg(), ex)
    ids = [r["id"] for r in read_manifest(store, RUN, "sft").episode_refs]
    assert ex.sft_ids == [[ids[0]], [ids[1]]]  # 每步换一条,不是反复训同一条


def test_each_grpo_step_takes_the_next_group_and_wraps_around(store):
    ex = _Recording()
    _go(store, _batch_cfg(), ex)  # 数据集里 2 个组,每步 1 个组,共 4 个 GRPO step
    g = ex.grpo_gids
    assert [len(x) for x in g] == [1, 1, 1, 1]
    assert g[0] != g[1] and g[0] == g[2] and g[1] == g[3]


class _StubSource:
    """替身 Interact 源:每次 collect 返回固定的 episode 与元数据,用来精确控制源的输出。"""

    def __init__(self, episodes, failed_attempts=()):
        self._episodes = list(episodes)
        self._failed = list(failed_attempts)

    def collect(self):
        return TrajectoryBatch(
            list(self._episodes),
            {
                "episode_ids": [e.id for e in self._episodes],
                "failed_attempts": [dict(a) for a in self._failed],
            },
        )


def _stub_sources(store, grpo_source):
    def make(start):
        return {
            "sft": BatchTrajectorySource(store, read_manifest(store, RUN, "sft")),
            "grpo_opd": grpo_source,
        }
    return make


def _grpo_episodes(store):
    return BatchTrajectorySource(store, read_manifest(store, RUN, "grpo_opd")).collect().episodes


def test_failed_rollout_attempts_reported_by_the_source_reach_the_curve_summary(store):
    failed = [{"reason": "timeout"}, {"reason": "timeout"}]
    source = _StubSource(_grpo_episodes(store), failed)
    run = _go(store, _interact_cfg(), MockExecutor(), make_sources=_stub_sources(store, source))
    assert run.curve_summary["failed_rollout_attempts"] == 6  # 3 个 GRPO step,每步 2 次


def test_an_undersized_group_from_the_source_fails_the_run_at_that_step(store):
    source = _StubSource(_grpo_episodes(store)[:3])  # s1 两条,s2 只有一条
    ex = MockExecutor()
    with pytest.raises(DegenerateGroupError) as exc:
        _go(store, _interact_cfg(), ex, make_sources=_stub_sources(store, source))
    assert len(exc.value.group_ids) == 1
    failed = read_training_run(store, RUN)
    assert (failed.status, failed.failure["point"]) == ("failed", "step 1 (grpo_opd)")
    assert ex.steps_done == 1  # SFT 那一步已跑,出问题的 GRPO 步没有训练


def test_a_corrupt_checkpoint_in_the_record_escalates_instead_of_being_loaded(store):
    with pytest.raises(SimulatedCrashError):
        _go(store, _batch_cfg(), MockExecutor(crash_at_step=3))
    pending = read_training_run(store, RUN)
    assert pending.last_checkpoint_ref is not None
    broken = dataclasses.replace(pending.last_checkpoint_ref, sha256="f" * 64)
    tampered = dataclasses.replace(pending, last_checkpoint_ref=broken)
    write_training_run(store, tampered)

    with pytest.raises(NeedsHumanError, match="integrity"):
        _go(store, _batch_cfg(), MockExecutor())
    assert read_training_run(store, RUN) == tampered  # 升级时不改动任何记录
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run --extra dev pytest tests/test_mt_orchestrator.py -q`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindtrainer.orchestrator'`。

- [ ] **Step 3: 写实现**

`src/mindtrainer/orchestrator.py`:

```python
from __future__ import annotations

import copy
import dataclasses
from typing import Callable

from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from mindtrainer.checkpoint import CheckpointConverter, register_candidate
from mindtrainer.datamodel import StepResult, TrainingRun
from mindtrainer.executor import TrainingExecutor
from mindtrainer.grouping import DegenerateGroupError, group_by_id, validate_groups
from mindtrainer.resume import NeedsHumanError, decide_resume
from mindtrainer.reward_pipeline import (
    AttributionBackend, AttributionError, run_reward_pipeline, summarize_rewards,
)
from mindtrainer.store_adapter import read_training_run, write_training_run
from mindtrainer.training_config import (
    STAGES, InvalidTrainingConfigError, TrainingConfig, config_hash, phase_at,
    total_steps, validate_training_config,
)
from mindtrainer.trajectory_source import TrajectorySource, batch_for_step
from simulator.dataset import DatasetIntegrityError


class NotEnoughGroupsError(ValueError):
    """Batch 数据集的组数少于 groups_per_step。继续的话 batch_for_step 回绕,同一个组会在
    一步里出现两次,成员被重复计入——静默地扭曲组内基线,所以是错误。"""


# 这几类是确定性失败(补充 E.2 的 T2):同样的输入还会再失败,写 failed 并保留,不进续训。
# 其余异常一律不吞,run 保持 pending,交给 decide_resume 判定。
_DETERMINISTIC = (
    DatasetIntegrityError, DegenerateGroupError, AttributionError,
    NotEnoughGroupsError, InvalidTrainingConfigError,
)

# start_grpo_step -> {"sft": 源, "grpo_opd": 源}。用工厂而不是现成的源,因为续训时
# Interact 源要从正确的 scenario 轮转位置起步(start_step),而这取决于续训点。
SourcesFactory = Callable[[int], dict[str, TrajectorySource]]


def _failed(base: TrainingRun, point: str, exc: Exception) -> TrainingRun:
    return dataclasses.replace(
        base,
        status="failed",
        failure={"category": "T2", "point": point, "detail": f"{type(exc).__name__}: {exc}"},
    )


def _prepare_batch_grpo(cfg, source, attribution):
    """Batch 的 GRPO 数据是固定的,所以整份数据集只算一次奖励、只校验一次。
    在训练开始前就做(而不是等 SFT 跑完):数据有问题时一步都不浪费。"""
    batch = source.collect()
    rewarded = run_reward_pipeline(batch, cfg.reward, attribution, trained_slot=cfg.trained_slot)
    groups = group_by_id(rewarded)
    validate_groups(groups, cfg.min_group_size)
    if cfg.groups_per_step > len(groups):
        raise NotEnoughGroupsError(
            f"groups_per_step {cfg.groups_per_step} exceeds the {len(groups)} group(s) in the dataset"
        )
    return groups


def _update_summary(summary: dict, result: StepResult, reward_mean: float | None, failed: int) -> None:
    summary["failed_rollout_attempts"] = summary.get("failed_rollout_attempts", 0) + failed
    if result.phase == "sft":
        summary["sft_steps_done"] = summary.get("sft_steps_done", 0) + 1
        summary["final_sft_loss"] = result.metrics["sft_loss"]
        return
    summary["grpo_steps_done"] = summary.get("grpo_steps_done", 0) + 1
    summary.setdefault("first_mean_reward", reward_mean)
    summary["final_mean_reward"] = reward_mean
    summary["final_grpo_loss"] = result.metrics["grpo_loss"]
    summary["final_opd_weight"] = result.metrics["opd_weight"]
    summary["degenerate_groups_total"] = (
        summary.get("degenerate_groups_total", 0) + int(result.metrics["n_degenerate_groups"])
    )


def run_training(
    cfg: TrainingConfig,
    *,
    store: GitStore,
    run_id: str,
    make_sources: SourcesFactory,
    attribution: AttributionBackend,
    executor: TrainingExecutor,
    converter: CheckpointConverter,
    resume_from: TrainingRun | None = None,
    metric_stream_refs: list[BigRef] | None = None,
    on_step: Callable[[StepResult], None] | None = None,
) -> TrainingRun:
    """薄编排:不含判定逻辑,只调下层纯函数和协议(沿用 run_diagnostic 的做法)。

    - 新运行从 base_checkpoint_ref 的 step 0 起;resume_from 带中间 checkpoint 则从
      checkpoint_step 起,不带(崩在第一次存点之前)则从头重来。两种情况都先把
      resume_attempts 加一并落盘,再动执行器——这样即便 load 本身反复失败也会触顶升级。
    - 数据是 step 的纯函数(Batch):SFT 取 batch_for_step(episodes, step, sft_batch_size);
      GRPO 取 batch_for_step(组 id, grpo 序号, groups_per_step) 个完整组。所以"一次跑完"
      与"崩溃后从存点续"逐字节等价。Interact 的 rollout 是实时的,不声称等价。
    - 每 checkpoint_every_steps 步存点,并把引用、checkpoint_step 与当时的曲线摘要一起回写。
      曲线摘要只在存点时落盘,所以续训后它与存点时一致,不会重复计入崩溃前多跑的步。
    - 收尾:存最终 checkpoint(先回写 pending,再转换),注册候选,置 executed。
    - 确定性失败写 failed(category T2)并原样重抛;其余异常不吞,run 保持 pending。
    - metric_stream_refs 与 on_step:逐步指标不进 git(补充 D.2),on_step 是调用方观察
      每一步的唯一出口;指标流文件本身由调用方写,只把引用交给这里记录。
    """
    chash = config_hash(cfg)
    if resume_from is not None:
        if resume_from.run_id != run_id:
            raise ValueError(f"resume_from belongs to {resume_from.run_id!r}, not {run_id!r}")
        if resume_from.status != "pending":
            raise ValueError(f"only a pending run can be resumed, got {resume_from.status!r}")
        if resume_from.config_hash != chash:
            raise NeedsHumanError(
                f"config changed since run {run_id} started "
                f"({resume_from.config_hash[:12]} -> {chash[:12]}); resuming would not be reproducible"
            )
    base = resume_from or TrainingRun(run_id, chash)

    reason = validate_training_config(cfg)
    if reason is not None:
        write_training_run(store, _failed(base, "config", InvalidTrainingConfigError(reason)))
        raise InvalidTrainingConfigError(reason)

    # 起点:纯计算,先不写任何东西
    if resume_from is None:
        run, start, from_base = base, 0, True
    elif base.last_checkpoint_ref is not None:
        run = dataclasses.replace(base, resume_attempts=base.resume_attempts + 1)
        start, from_base = base.checkpoint_step, False
    else:
        run = dataclasses.replace(
            base, resume_attempts=base.resume_attempts + 1, checkpoint_step=0, curve_summary={}
        )
        start, from_base = 0, True
    if metric_stream_refs is not None:
        run = dataclasses.replace(run, metric_stream_refs=list(metric_stream_refs))

    sources = make_sources(max(0, start - cfg.sft_steps))
    for stage, steps in (("sft", cfg.sft_steps), ("grpo_opd", cfg.grpo_steps)):
        if steps > 0 and stage not in sources:
            raise ValueError(f"make_sources() returned no {stage!r} source but {steps} {stage} step(s) are configured")

    write_training_run(store, run)  # 先落盘本次尝试,再动执行器
    executor.load(cfg.base_checkpoint_ref if from_base else run.last_checkpoint_ref)

    summary = copy.deepcopy(run.curve_summary)
    total = total_steps(cfg)
    sft_episodes = None
    batch_groups = None
    point = "start"
    try:
        if cfg.mode == "batch" and cfg.grpo_steps > 0:
            point = "grpo precheck"
            batch_groups = _prepare_batch_grpo(cfg, sources["grpo_opd"], attribution)
        for step in range(start, total):
            phase = phase_at(step, cfg)
            point = f"step {step} ({phase})"
            reward_mean, failed = None, 0
            if phase == "sft":
                if sft_episodes is None:
                    sft_episodes = sources["sft"].collect().episodes
                chosen = batch_for_step(sft_episodes, step, cfg.sft_batch_size)
                result = executor.sft_step(chosen, trained_slot=cfg.trained_slot)
            else:
                grpo_index = step - cfg.sft_steps
                if cfg.mode == "batch":
                    gids = batch_for_step(list(batch_groups), grpo_index, cfg.groups_per_step)
                    rewarded = [r for gid in gids for r in batch_groups[gid]]
                else:
                    batch = sources["grpo_opd"].collect()
                    rewarded = run_reward_pipeline(
                        batch, cfg.reward, attribution, trained_slot=cfg.trained_slot
                    )
                    validate_groups(group_by_id(rewarded), cfg.min_group_size)
                    failed = len(batch.metadata.get("failed_attempts", []))
                reward_mean = summarize_rewards(rewarded)["mean"]
                result = executor.grpo_opd_step(
                    rewarded, opd_coefficient=cfg.opd_coefficient,
                    teacher_model_ref=cfg.teacher_model_ref,
                )
            if result.step != step:
                raise RuntimeError(
                    f"executor reports step {result.step} but the orchestrator is at step {step}: "
                    f"executor and run record are out of sync"
                )
            _update_summary(summary, result, reward_mean, failed)
            if on_step is not None:
                on_step(result)
            done = step + 1
            if done < total and done % cfg.checkpoint_every_steps == 0:
                ref = executor.save_checkpoint(f"step-{done}")
                run = dataclasses.replace(
                    run, checkpoint_step=done, last_checkpoint_ref=ref,
                    curve_summary=copy.deepcopy(summary),
                )
                write_training_run(store, run)
    except _DETERMINISTIC as exc:
        write_training_run(store, _failed(run, point, exc))
        raise

    final_ref = executor.save_checkpoint("final")
    run = dataclasses.replace(
        run, checkpoint_step=total, last_checkpoint_ref=final_ref,
        curve_summary=copy.deepcopy(summary),
    )
    write_training_run(store, run)  # 先记下最终存点:转换或注册崩了,续训直接回到这里收尾
    register_candidate(
        store,
        run_id=run_id,
        checkpoint_ref=converter.convert(final_ref),
        base_checkpoint_ref=cfg.base_checkpoint_ref,
        config_hash=chash,
        curve_summary=summary,
        manifest_ids=[cfg.stage_manifest_refs[s] for s in STAGES if cfg.stage_manifest_refs.get(s)],
    )
    run = dataclasses.replace(run, status="executed")
    write_training_run(store, run)
    return run


def resume_or_run(
    cfg: TrainingConfig,
    *,
    store: GitStore,
    run_id: str,
    make_sources: SourcesFactory,
    attribution: AttributionBackend,
    executor: TrainingExecutor,
    converter: CheckpointConverter,
    metric_stream_refs: list[BigRef] | None = None,
    on_step: Callable[[StepResult], None] | None = None,
) -> TrainingRun:
    """入口:读已有的 training_run,交给 decide_resume 判定。
    noop 直接返回已有记录;resume / restart 带记录进入 run_training;
    escalate 抛 NeedsHumanError(带原因)。没有记录就是新运行。"""
    existing = read_training_run(store, run_id)
    kwargs = dict(
        store=store, run_id=run_id, make_sources=make_sources, attribution=attribution,
        executor=executor, converter=converter,
        metric_stream_refs=metric_stream_refs, on_step=on_step,
    )
    if existing is None:
        return run_training(cfg, **kwargs)
    intact = (
        executor.checkpoint_intact(existing.last_checkpoint_ref)
        if existing.last_checkpoint_ref is not None else None
    )
    decision = decide_resume(existing, cfg, checkpoint_intact=intact)
    if decision.action == "noop":
        return existing
    if decision.action == "escalate":
        raise NeedsHumanError(decision.reason)
    return run_training(cfg, resume_from=existing, **kwargs)
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run --extra dev pytest tests/test_mt_orchestrator.py -q`
Expected: 29 passed。

- [ ] **Step 5: 全量回归**

Run: `uv run --extra dev pytest -q`
Expected: 687 passed,无警告。

- [ ] **Step 6: 提交**

```bash
git add src/mindtrainer/orchestrator.py tests/test_mt_orchestrator.py
git commit -m "feat: mindtrainer orchestrator (crash-resume equivalence for batch, deterministic failures kept, thin coordinator)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 15: 集成测试:候选走完 审核 → 晋级 → 下一轮基座

**Files:**
- Test: `tests/test_mt_integration.py`

**Interfaces:**
- Consumes: 任务 14 的 `resume_or_run`;任务 11/12 的 `MockExecutor`、`MockConverter`;既有的 `controlplane.daemon.reconcile_once`、`controlplane.models.latest_promoted_checkpoint`、`controlplane.paths`(`candidate_checkpoint_file`、`diagnostic_report_file`、`model_file`);`simulator.dataset.produce_grouped_dataset` / `read_manifest`。
- Produces: 无(只加测试)。

**这是个验证任务,没有新实现**,所以没有"确认失败"步骤。它的价值是锁住设计 §9 的两条集成测试:

1. **MindTrainer 注册的候选,不改一行调和循环,就能依次走 `request_diagnostic` → `await_review` →(人工批准)→ `promote_candidate`**,晋级后 `zengen_model` 带上候选的 `checkpoint_ref`,候选置 `effective`。
2. **晋级后用 `latest_promoted_checkpoint` 解析出基座,作为下一轮 `base_checkpoint_ref` 起一次新训练**:执行器 `load` 的正是晋级的那份权重,第二个候选的 `base_checkpoint_ref` 指向它。

写这个任务时发现了任务 11 里 mock 执行器的一个缺口:晋级后的基座是转换后的推理格式引用,mock 起初把它当成损坏的 mock 存点而拒绝加载。已在任务 11 里修复并带有自己的单元测试。**这只说明 mock 的假设**:真实 ROLL 能否直接加载推理格式作为训练起点没有核实(设计 §10 第 9 条),若不能,要加 `training_checkpoint_ref` 并改 daemon 晋级与 `latest_promoted_checkpoint`。

- [ ] **Step 1: 写测试**

`tests/test_mt_integration.py`:

```python
"""MindTrainer 产出的候选,不改一行调和循环就能走完 审核 -> 晋级 -> 下一轮基座。"""
import pytest

from controlplane.daemon import reconcile_once
from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from controlplane.models import latest_promoted_checkpoint
from controlplane.paths import candidate_checkpoint_file, diagnostic_report_file, model_file
from mindtrainer.checkpoint import MockConverter
from mindtrainer.executor import MockExecutor
from mindtrainer.orchestrator import resume_or_run
from mindtrainer.reward_pipeline import build_attribution_backend
from mindtrainer.training_config import RewardConfig, TrainingConfig
from mindtrainer.trajectory_source import BatchTrajectorySource
from simulator.backends import ScriptedBackend
from simulator.dataset import produce_grouped_dataset, read_manifest
from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario

DATA_RUN = "run-1"  # 数据集挂在这个 run 下;两轮训练共用同一份清单
BASE = BigRef("file:///ckpt/base", "a" * 64, 1)
TEACHER = BigRef("file:///ckpt/teacher", "b" * 64, 1)


def _scenario(sid):
    return Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")


def _rollout(scenario, k):
    first = ("agreed " if k == 0 else "no ") + f"{scenario.id}-a{k}"
    return {
        "a": ScriptedBackend({"a": [first, "a2"]}),
        "b": ScriptedBackend({"b": ["b1", "b2"]}),
    }


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    personas = {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}
    for name, size in (("sft", 1), ("grpo_opd", 2)):
        produce_grouped_dataset(
            s, DATA_RUN, name,
            scenarios=[
                (_scenario(sid), RoleAssignment(
                    sid,
                    [RoleBinding("a", "pa", "self_play"),
                     RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")],
                    max_turns=4))
                for sid in ("s1", "s2")
            ],
            personas=personas, backends_for_rollout=_rollout, group_size=size,
        )
    return s


def _cfg(base_ref):
    return TrainingConfig(
        mode="batch", trained_slot="a", base_checkpoint_ref=base_ref, teacher_model_ref=TEACHER,
        reward=RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]}),
        sft_steps=1, grpo_steps=2, sft_batch_size=1, groups_per_step=1, group_size=2,
        grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=1, allow_resume=True,
        stage_manifest_refs={"sft": "dsm-run-1-sft", "grpo_opd": "dsm-run-1-grpo_opd"},
    )


def _train(store, run_id, cfg, executor):
    def make_sources(start):
        return {
            name: BatchTrajectorySource(store, read_manifest(store, DATA_RUN, name))
            for name in ("sft", "grpo_opd")
        }

    return resume_or_run(
        cfg, store=store, run_id=run_id, make_sources=make_sources,
        attribution=build_attribution_backend(cfg.reward),
        executor=executor, converter=MockConverter(),
    )


def _approve(store, run_id):
    path = candidate_checkpoint_file(run_id)
    cand = store.read_product(path)
    cand.status = "approved"
    store.write_product(path, cand, "human: approve")


class _Recording(MockExecutor):
    def __init__(self):
        super().__init__()
        self.loads = []

    def load(self, checkpoint_ref):
        self.loads.append(checkpoint_ref)
        super().load(checkpoint_ref)


def test_a_mindtrainer_candidate_walks_the_unmodified_reconcile_loop_to_promotion(store):
    _train(store, "run-1", _cfg(BASE), MockExecutor())
    candidate = store.read_product(candidate_checkpoint_file("run-1"))

    first = reconcile_once(store)
    assert [(a.kind, a.target_id) for a in first] == [("request_diagnostic", "cand-run-1")]
    assert store.exists(diagnostic_report_file("run-1"))

    second = reconcile_once(store)
    assert [a.kind for a in second] == ["await_review"]
    assert store.read_product(candidate_checkpoint_file("run-1")).status == "pending_review"

    _approve(store, "run-1")
    third = reconcile_once(store)
    assert [a.kind for a in third] == ["promote_candidate"]

    model = store.read_product(model_file(1))
    assert model.payload["promoted_from"] == "cand-run-1"
    assert model.payload["checkpoint_ref"] == candidate.payload["checkpoint_ref"]
    assert store.read_product(candidate_checkpoint_file("run-1")).status == "effective"


def test_the_promoted_checkpoint_becomes_the_base_of_the_next_training_round(store):
    _train(store, "run-1", _cfg(BASE), MockExecutor())
    reconcile_once(store)
    reconcile_once(store)
    _approve(store, "run-1")
    reconcile_once(store)

    promoted = latest_promoted_checkpoint(store)
    assert promoted.model_id == "zengen-v1"

    executor = _Recording()
    run = _train(store, "run-2", _cfg(promoted.checkpoint_ref), executor)

    assert run.status == "executed"
    assert executor.loads[0] == promoted.checkpoint_ref  # 起点正是晋级的那份权重
    second = store.read_product(candidate_checkpoint_file("run-2"))
    assert second.payload["base_checkpoint_ref"] == promoted.checkpoint_ref.to_dict()
    assert second.status == "pending_review"
```

- [ ] **Step 2: 运行,确认通过**

Run: `uv run --extra dev pytest tests/test_mt_integration.py -q`
Expected: 2 passed。

- [ ] **Step 3: 全量回归**

Run: `uv run --extra dev pytest -q`
Expected: 689 passed,无警告。

- [ ] **Step 4: 提交**

```bash
git add tests/test_mt_integration.py
git commit -m "test: mindtrainer candidates walk the unmodified reconcile loop to promotion and seed the next round

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## 下半段完成标准与交接

**完成标准**

- `uv run --extra dev pytest -q` 为 689 passed,无警告(上半段 581 + 本计划 108)。
- `src/mindtrainer/` 新增 `store_adapter.py`、`executor.py`、`checkpoint.py`、`resume.py`、`orchestrator.py`;`datamodel.py` 多了 `StepResult` 与 `TrainingRun`。
- 本计划的代码在编写时已在隔离副本上逐任务做过红绿验证,并对 49 处关键断言做过变异验证(逐处植入回归,确认对应测试变红),全部被测试捕获。其中 5 处最初存活(逐步取数据、失败尝试计入摘要、Interact 组校验、checkpoint 完整性核对),补了 6 条守护测试后才被捕获——这正是变异验证的用处。执行时若某个新测试在"实现尚不存在"时意外通过,先怀疑它是不是空转。
- 变异验证请**关闭字节码缓存**(`PYTHONDONTWRITEBYTECODE=1`)且**不要两组并行**,否则陈旧字节码或 CPU 争抢会让结果不可信。

**执行建议**

设计 §12 建议两段之间过一次评审:上半段(任务 1–9)做完、全量绿后,先评审一次再开始本计划。任务 14 的测试文件约需 25 秒,主要是 6 个崩溃点的参数化用例,属正常。

**明确不做(设计 §11,推迟到接线计划)**

- 接入调和循环、替换 `mind_trainer_stub`(含"候选出现 → 调真实诊断"和"服务端 `scenario_resolver` 读同一 ControlPlane")。
- 真实 ROLL 训练执行器、真实 LLM 归因、真实格式转换。
- RecipeStore 与 `recipe_ref`;完整 `FailureRecord` 产物;`CostRecord` 成本记账。
- 编排层调用 `produce_grouped_dataset`(生产方函数已有,没有谁在驱动它)。
- Interact 的并行 rollout;真实策略模型作为 `policy`。

**接入真实环境前必须核实的几条(设计 §10 全文九条,以下三条风险最高)**

1. **离线分组 GRPO 在 ROLL 上的语义**:本计划只保证分组契约和退化组的显式报错,不保证算法正确性。
2. **推理格式权重能否直接作为下一轮训练的起点**(任务 11、12、15 都碰到了它):mock 不区分格式,测试暴露不出问题;若不能,要加 `training_checkpoint_ref`,并改 daemon 晋级与 `latest_promoted_checkpoint`。
3. **审核门证据只保证在场,不保证被读过、也不保证可信**:真实诊断接入前,这道门是"形式在场"。

---

## 实施后修订(Post-implementation amendments)

本计划的 6 个任务落地并通过逐任务评审后,整支评审提出了若干问题,随后经一次修复波修订了下列内容。**以下修订优先于上文任务里的代码块;上文保留原样,作为当时的计划记录**(上文的代码块曾逐字回放验证过,逐块改写会破坏这条证据链)。最终全量测试为 700 passed。

- **`register_candidate` 的摘要拷贝(任务 12,提交 390440f)**:`"train_curve_summary": dict(curve_summary)` 改为 `copy.deepcopy(curve_summary)`(并 `import copy`);测试 `test_payload_does_not_alias_the_callers_summary` 改名为 `test_the_stored_summary_keeps_the_values_from_registration_time`,断言不变。原因:函数在调用栈内同步落盘,调用返回后再改入参根本碰不到已写入的内容,旧名字承诺了一件测不到的事;深拷贝则防住嵌套结构在落盘前被别处改写。上文任务 12 的两处代码块与此不一致。
- **`invalid_session` 的拒绝写 failed(任务 14,提交 ceb64d6)**:编排器现在对 `RolloutFailedError` 且 `reason == "invalid_session"` 写 failed(category T2)并重抛;`timeout` 用尽与 `role_backend_failure`(服务端分不出 T1/T2)仍保持 pending,由 `max_resume_attempts` 兜底。**上文「尚未处理的两件事」把它列为未决取舍;执行时控制者曾裁决不加入 `_DETERMINISTIC`(理由是写 failed 与"失败是数据"冲突),终审后改判。** 改判的依据是实际相反——`pending` 且 `failure` 为空才是把原因丢了,而且每次续训都要再开一次真实会话去撞同一个拒绝。服务端、客户端、轨迹源三处文档都写明 `invalid_session` 重试无益。
- **终态记录只接受逐字节相同的重放(任务 10,提交 89d10fa)**:`write_training_run` 对 `pending → pending` 仍允许更新内容;`executed`/`failed` 的记录只接受与磁盘内容相同的重放,内容不同抛 `ValueError`。原因:原先同状态重写一律放行,直接调 `run_training` 连续两次写不同失败原因时,前一条会被悄悄覆盖。
- **收尾窗口内候选被批准(任务 14,提交 07674d0)**:崩在 `register_candidate` 之后、置 `executed` 之前,期间人批准了候选,原先续训会连续抛 `CandidateAlreadyReviewedError` 直到升级。现在编排器捕获它,核对候选的 `training_run_ref`、`config_hash`、`checkpoint_ref` 确属本 run 本配置同一份权重,才视为注册已完成并置 `executed`;否则原样重抛。`register_candidate` 本身不变。对应的「候选不属于本 run」测试原先只篡改 `config_hash` 一个字段,变异检查发现删掉另外两个比较测试照样绿,所以参数化为三个用例各篡改一个字段(提交 c724748)。
- **仍未处理(已记入设计 §10 第 11–13 条)**:`config_hash` 在不可序列化的 `grpo_config` 上于没有任何记录时抛出,失败原因不落盘;`max_resume_attempts` 与 `allow_resume` 在 `config_hash` 内,升级后调大它们会被拒为"配置变了";续训等价只在 mock 下有证据。另有两件只是性能:收尾崩溃后的续训会再存一次同哈希的最终点,Batch 续训会重算整份奖励。`_DETERMINISTIC` 里的 `InvalidTrainingConfigError` 实际是防御性冗余(它只会在 `try` 之前被抛出)。
