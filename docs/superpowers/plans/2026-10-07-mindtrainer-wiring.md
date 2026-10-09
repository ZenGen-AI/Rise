# MindTrainer 接线:调和循环派发缝、MindTrainer 派发器与启动入口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 ControlPlane 的调和循环能把一条 pending 的训练指令交给 MindTrainer,跑成一个待审核的候选 checkpoint。batch 模式从启动入口端到端可跑;Interact 模式在调用方注入实时客户端与策略模型后可跑。不注入时,调和循环的行为与现在逐字节相同。

**Architecture:** 在 `controlplane.daemon` 里开一条可注入的派发缝:一个 `Callable[[GitStore, Envelope], None]`,默认是现有的桩。`controlplane` 只认识这个形状,不认识 MindTrainer(依赖方向保持 `mindtrainer → controlplane`)。MindTrainer 一侧提供 `make_mindtrainer_dispatcher(...)`:读指令里的 `training_config`,按配置点名的数据集清单备好数据源,调用已有的 `resume_or_run`。失败分两类:确定性做不成的抛 `DirectiveFailedError`,调和循环把指令置 `failed`,不再重派;其余异常原样上抛,指令保持 `pending`,下一轮重新派发并从存点续训。`mindtrainer.wire` 把两边接成一个可运行的入口。

**Tech Stack:** Python 3.11+ / uv / pytest;**无新增第三方依赖**。

**Spec:**
- `docs/superpowers/specs/2026-10-03-mindtrainer-design.md` §11 第一条"接入 ControlPlane 调和循环"(本计划实现其中一部分,范围见下表)
- 前置:MindTrainer 上、下半段已合并到 `main`,全量基线 **763 通过**
- 背景:`docs/superpowers/plans/2026-10-03-mindtrainer-part2-training-and-orchestration.md`(`resume_or_run`、`SourcesFactory`、`MockExecutor`、`NeedsHumanError`)

**依赖已有产出的名字**

| 来源 | 名字 |
|---|---|
| `controlplane.daemon` | `apply_action`、`reconcile_once`、`run_daemon`、`read_tree_state`(任务 16 在前三个上加参数) |
| `controlplane.paths` | `run_dir`、`dataset_manifest_file`、`candidate_checkpoint_file`、`model_file` |
| `controlplane.states` | `assert_transition`(非审核类 `pending → executed \| failed`) |
| `simulator.stubs` | `execute_dispatch(store, run_id, target_dimension)`(现有的桩,默认派发器包的就是它) |
| `simulator.dataset` | `DatasetManifest`、`DatasetIntegrityError`、`read_manifest`、`produce_grouped_dataset`(后者只在测试里造数据) |
| `mindtrainer.training_config` | `TrainingConfig`、`load_config_from_directive`、`validate_training_config`、`InvalidTrainingConfigError` |
| `mindtrainer.orchestrator` | `resume_or_run`、`NotEnoughGroupsError` |
| `mindtrainer.trajectory_source` | `BatchTrajectorySource`、`InteractTrajectorySource` |
| `mindtrainer.reward_pipeline` | `build_attribution_backend`、`AttributionBackend`、`AttributionError` |
| `mindtrainer.executor` / `mindtrainer.checkpoint` | `MockExecutor`、`TrainingExecutor`、`SimulatedCrashError` / `MockConverter`、`CheckpointConverter` |
| `mindtrainer.resume` / `mindtrainer.grouping` | `NeedsHumanError` / `DegenerateGroupError` |
| `simulator.realtime` | `EnvironmentServiceClient`、`create_server`(只在 Interact 的测试里用真实 loopback 服务端) |

**设计 §11 第一条的各子项,本计划怎么处理**

| §11 的条目 | 处理 |
|---|---|
| 接入 ControlPlane 调和循环 | **做**(任务 16–18) |
| 替换 `mind_trainer_stub` | **不替换**:默认仍是桩,注入派发器才走 MindTrainer。`simulator/stubs.py` 与 `tests/test_stubs.py` 不动 |
| "候选出现 → 调真实诊断" | 不做:`request_diagnostic` 仍走 `diagnostic_evaluator_stub`(报告带 stub 标记,审核门会醒目标出) |
| `scenario_resolver` 读同一 ControlPlane | 不做:入口不启动实时服务端。派发器接受调用方注入的 `client_factory` 与 `policy`;Interact 的测试用真实 loopback 服务端演示了注入后的样子 |
| 注意事项:`trained_model_id` 未锚定 | 不处理:`TrainingConfig` 与 `config_hash` 不变 |
| 注意事项:channel 复用、失败 rollout 遗留 episode、`turn_timeout_s`、`on_session_closed` | 不改动:都是调用方构造实时客户端与服务端时的事 |
| 真实 ROLL、真实 LLM 归因、真实格式转换 | 不做:入口用 `MockExecutor` 与 `MockConverter` |
| `produce_grouped_dataset` 的驱动 | 不做:数据集清单必须已在树里,缺了就把指令判 `failed` 并写明哪一份缺 |
| RecipeStore、`FailureRecord`、`CostRecord`、Interact 并行 rollout | 不做 |

**本计划的取舍(要改的话改这里,任务按此排)**

1. **范围只到"缝 + 派发器 + batch 入口"。** 不含真实诊断、真实 ROLL、数据生产。
2. **派发器收到整个 directive 信封,不是 `run_id` 加字典。** 这样派发器直接用现成的 `load_config_from_directive(directive)`,不用另写一份解析。
3. **状态迁移归调和循环,不归派发器。** 派发器只产出候选;`executed` / `failed` 由 `apply_action` 在派发器返回或抛 `DirectiveFailedError` 之后写。派发器改不了指令状态。
4. **确定性失败 = 指令 `failed`(终态)。** 重来要写一条新指令(新 run id);数据集可以复用,因为数据所在的 run 从清单 id 里解出来(见下)。
5. **入口放在 `mindtrainer` 一侧**(`mindtrainer/wire.py`),`scripts/run_controlplane.py` 只是薄壳。`controlplane` 因此不会反向导入 `mindtrainer`。

**已核实的事实(实现者不要再猜)**

- 调和循环里 directive 的 `id` 就是 run 目录名:`apply_action` 用 `action.target_id` 拼出 `{run_dir(run_id)}/directive.yaml`。所以派发器取 `run_id = directive.id`。
- `GitStore.read_product` 读不存在的文件抛 `FileNotFoundError`,不是 `DatasetIntegrityError`;`read_manifest` 只会因清单内容与 episode 对不上才抛 `DatasetIntegrityError`。所以"清单缺失"必须显式判一次,否则会被当成瞬态失败,每轮重派。
- 数据集清单的 id 是 `dsm-{数据所在的 run}-{阶段}`,文件在 `dataset_manifest_file(数据所在的 run, 阶段)`。晋级后的下一轮是新 run,数据集仍挂在前一轮的 run 下,所以数据所在的 run 要从清单 id 解出来,不能假设它等于指令自己的 run。
- 非审核类状态机允许 `pending → executed`、`pending → failed`;终态不能再迁移。`failed` 的指令不会再被 `reconcile` 派发(有测试)。

## Global Constraints

每个任务的要求都隐含包含本节。

- **`controlplane` 不导入 `mindtrainer`**:派发缝在 `controlplane` 里只是一个 `Callable` 类型别名。**`mindtrainer` 不导入 `diagnostic.*`**(设计 §0 边界 1)。
- **默认行为逐字节不变**:不传派发器时,调和循环写出的产物与改动前完全相同;既有测试一行不改。
- **新参数全部 keyword-only**:`apply_action`、`reconcile_once`、`run_daemon` 的既有位置参数调用点不用动。
- **脱离真实环境可测**:不需要 GPU、ROLL、外部网络。全部测试用 `tmp_path` 隔离;Interact 的端到端用真实 loopback gRPC 服务端(`127.0.0.1`)。
- **读产物走 `GitStore.read_product`,写产物用 `Envelope` + `GitStore.write_product`。** 不直接碰文件系统读产物。
- **状态机沿用非审核类**:`pending → executed`、`pending → failed`。候选 checkpoint 走审核类,MindTrainer 只写 `pending_review`,由人批准。
- **确定性失败写 `failed` 并保留;其余异常不吞**,指令保持 `pending`,由 `resume_or_run` 的续训判定兜底(含 `max_resume_attempts` 上限)。
- **无新增第三方依赖;凭证绝不进 git 树。**
- `src/` 下每个源文件以 `from __future__ import annotations` 开头(模块 docstring 之后的第一条语句也算)。
- **提交信息**结尾空一行后加:`Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`;`git add` 逐个文件,**不要碰** `cli/package.json`、`cli/package-lock.json`。
- **测试基线**:`uv run --extra dev pytest -q` 为 **763** 通过、无警告;三个任务后依次为 **774 → 804 → 808**,不得出现警告。

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `src/controlplane/daemon.py` | 修改(137 → 178 行) | 派发缝:`Dispatcher`、`DirectiveFailedError`、`stub_dispatcher`,三个函数各加一个 keyword-only 的 `dispatcher` |
| `src/mindtrainer/dispatch.py` | 新建(195 行) | `make_mindtrainer_dispatcher`:directive → 配置 → 数据源 → `resume_or_run` |
| `src/mindtrainer/wire.py` | 新建 | `run` / `main`:把调和循环与 MindTrainer 派发器接成入口 |
| `scripts/run_controlplane.py` | 新建 | 薄壳,只负责把 `src` 放进 `sys.path` 再调 `mindtrainer.wire.main` |
| `tests/test_daemon_dispatcher.py` | 新建 | 派发缝(11 个用例) |
| `tests/test_mt_dispatch.py` | 新建 | MindTrainer 派发器(38 个用例) |
| `tests/test_mt_wire.py` | 新建 | 入口端到端(4 个用例) |
| `docs/superpowers/specs/2026-10-03-mindtrainer-design.md` | 修改 | §11 第一条改成"已落地一部分" |

**打包说明:** 无需改动。`pyproject.toml` 已把整个 `src/mindtrainer` 登记进 wheel,打包防回归测试只比对 `src/` 下的顶层包目录;`scripts/` 不进 wheel(目前只有 `gen_realtime_proto.sh`,本计划往里加一个开发用入口)。

---

### Task 16: 调和循环的派发缝

**Files:**
- Modify: `src/controlplane/daemon.py`(137 → 178 行;48 行新增、7 行删除)
- Test: `tests/test_daemon_dispatcher.py`

**Interfaces:**
- Consumes: `simulator.stubs.execute_dispatch(store, run_id, target_dimension)`(现有);`controlplane.states.assert_transition`;`GitStore.read_product` / `write_product`。
- Produces(任务 17、18 依赖这些名字):
  - `Dispatcher = Callable[[GitStore, Envelope], None]`:拿到 pending 的 directive 快照,把它做成产物(候选等)。**返回即视为成功**,状态迁移由调和循环做。
  - `DirectiveFailedError(reason: str, *, category: str = "T2")`:属性 `reason`、`category`。派发器用它说"这条指令确定性地做不成,重试无益"。
  - `stub_dispatcher(store, directive) -> None`:默认派发器,等价于改动前的 `execute_dispatch(store, directive.id, "tom")`。
  - `apply_action(store, action, *, dispatcher=None)`、`reconcile_once(store, *, dispatcher=None)`、`run_daemon(store, interval_s, max_ticks=None, *, dispatcher=None)`。

**设计要点:**
- **缝只有一条,位置在"下发指令"这一步**(`apply_action` 的 `dispatch_directive` 分支)。诊断、晋级、`await_review` 都不变。
- **派发器拿到的是快照。** `apply_action` 在派发器返回之后重新读一遍 directive 再迁移状态,所以派发器就算改了传进去的对象,也改不了状态(有测试)。
- **状态迁移归调和循环。** 派发器返回 → `executed`;抛 `DirectiveFailedError` → `failed`,原因写进 `payload["failure"] = {"category", "reason"}`,人写的其余内容原样保留;其余异常 → 原样上抛,指令保持 `pending`,下一轮重新派发。
- **`failed` 是终态。** 非审核类状态机 `pending → failed`;之后 `reconcile` 不再为这条指令产生派发动作,所以做不成的指令不会被每轮重派(有测试)。
- **一条指令失败不拖累同一轮的其他指令。** `DirectiveFailedError` 在 `apply_action` 内部处理完(有测试)。普通异常不一样:它沿调用栈上抛,`run_daemon` 不捕获,见文末"已知限制"。
- **默认行为逐字节不变。** `stub_dispatcher` 就是原来那一行 `execute_dispatch(store, run_id, "tom")`,连写死的 `"tom"` 都保留(target_dimension → purpose 的映射留给 recipe 计划)。测试把"不传"与"显式传桩"产出的两棵树逐项比较。
- **三个新参数都是 keyword-only。** 既有测试和调用点用位置参数传 `store`,一行不用改;有测试保证位置传入会抛 `TypeError`。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_daemon_dispatcher.py`:

```python
import pytest

from controlplane.daemon import DirectiveFailedError, reconcile_once, run_daemon, stub_dispatcher
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, run_dir


def _put(store, run_id, payload=None):
    env = Envelope(
        id=run_id, type="directive", version=1, created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1,
        payload=payload if payload is not None else {"target_dimension": "tom", "recipe_ref": ""},
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "human: write directive")


def _directive(store, run_id):
    return store.read_product(f"{run_dir(run_id)}/directive.yaml")


def _tree(store, run_id):
    return {rel: store.read_product(rel).to_dict() for rel in store.list_products(run_dir(run_id))}


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def test_without_a_dispatcher_the_stub_still_runs(store):
    _put(store, "run-1")
    reconcile_once(store)
    assert store.read_product(candidate_checkpoint_file("run-1")).id == "cand-run-1"
    assert _directive(store, "run-1").status == "executed"


def test_passing_the_stub_explicitly_changes_nothing(tmp_path):
    a, b = GitStore(str(tmp_path / "a")), GitStore(str(tmp_path / "b"))
    for s in (a, b):
        s.init()
        _put(s, "run-1")
    reconcile_once(a)
    reconcile_once(b, dispatcher=stub_dispatcher)
    assert _tree(a, "run-1") == _tree(b, "run-1")


def test_an_injected_dispatcher_replaces_the_stub_and_gets_the_directive(store):
    _put(store, "run-1", {"target_dimension": "tom", "recipe_ref": "", "note": "x"})
    seen = []
    reconcile_once(
        store, dispatcher=lambda s, d: seen.append((s, d.id, d.status, d.payload["note"]))
    )
    assert seen == [(store, "run-1", "pending", "x")]
    assert not store.exists(candidate_checkpoint_file("run-1"))  # 桩没有跑
    assert _directive(store, "run-1").status == "executed"


def test_a_dispatcher_cannot_change_the_state_by_mutating_what_it_was_given(store):
    _put(store, "run-1")

    def meddle(s, d):
        d.status = "failed"

    reconcile_once(store, dispatcher=meddle)
    assert _directive(store, "run-1").status == "executed"


def test_an_ordinary_exception_propagates_and_the_directive_stays_pending(store):
    _put(store, "run-1")
    calls = []

    def boom(s, d):
        calls.append(d.id)
        raise RuntimeError("transient")

    for _ in range(2):
        with pytest.raises(RuntimeError, match="transient"):
            reconcile_once(store, dispatcher=boom)
        assert _directive(store, "run-1").status == "pending"
    assert calls == ["run-1", "run-1"]  # 下一轮重新派发


def test_a_directive_failure_is_recorded_and_never_retried(store):
    _put(store, "run-1")
    calls = []

    def refuse(s, d):
        calls.append(d.id)
        raise DirectiveFailedError("config is nonsense")

    first = reconcile_once(store, dispatcher=refuse)
    assert [a.kind for a in first] == ["dispatch_directive"]
    env = _directive(store, "run-1")
    assert env.status == "failed"
    assert env.payload["failure"] == {"category": "T2", "reason": "config is nonsense"}
    assert env.payload["target_dimension"] == "tom"  # 人写的内容原样保留
    assert reconcile_once(store, dispatcher=refuse) == []
    assert calls == ["run-1"]


def test_a_failed_directive_does_not_stop_the_rest_of_the_tick(store):
    _put(store, "run-1")
    _put(store, "run-2")

    def pick(s, d):
        if d.id == "run-1":
            raise DirectiveFailedError("no", category="T3")
        stub_dispatcher(s, d)

    reconcile_once(store, dispatcher=pick)
    assert _directive(store, "run-1").status == "failed"
    assert _directive(store, "run-1").payload["failure"]["category"] == "T3"
    assert _directive(store, "run-2").status == "executed"
    assert store.exists(candidate_checkpoint_file("run-2"))


def test_the_dispatcher_is_keyword_only(store):
    _put(store, "run-1")
    with pytest.raises(TypeError):
        reconcile_once(store, stub_dispatcher)


def test_run_daemon_hands_the_dispatcher_to_the_ticks(store):
    _put(store, "run-1")
    seen = []
    run_daemon(store, 0, max_ticks=2, dispatcher=lambda s, d: seen.append(d.id))
    assert seen == ["run-1"]  # 第二轮时 directive 已 executed,不再派发


def test_run_daemon_without_a_dispatcher_uses_the_stub(store):
    _put(store, "run-1")
    run_daemon(store, 0, max_ticks=1)
    assert store.exists(candidate_checkpoint_file("run-1"))


def test_the_failure_error_carries_its_reason_and_category():
    exc = DirectiveFailedError("why", category="T3")
    assert (exc.reason, exc.category, str(exc)) == ("why", "T3", "why")
    assert DirectiveFailedError("x").category == "T2"
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run --extra dev pytest tests/test_daemon_dispatcher.py -q`
Expected: 收集阶段报错 `ImportError: cannot import name 'DirectiveFailedError' from 'controlplane.daemon'`。

- [ ] **Step 3: 写实现**

把下面的差异应用到 `src/controlplane/daemon.py`(存成 `daemon.patch` 后 `git apply daemon.patch`,或按块手改;共 +48 −7 行,改完 `git diff --stat` 应与此一致):

```diff
--- a/src/controlplane/daemon.py
+++ b/src/controlplane/daemon.py
@@ -1,6 +1,7 @@
 from __future__ import annotations
 
 import time
+from collections.abc import Callable
 
 from controlplane.envelope import Envelope, OwnerRef
 from controlplane.gitstore import GitStore
@@ -17,7 +18,27 @@
 
 _NOW = "2026-09-26T00:00:00Z"
 
+# 派发器:拿到 pending 的 directive,把它做成产物(候选等)。返回即视为派发成功,调和循环随后
+# 把 directive 置 executed。状态迁移归调和循环,派发器不碰 directive 的状态。
+Dispatcher = Callable[[GitStore, Envelope], None]
 
+
+class DirectiveFailedError(Exception):
+    """派发器用它说"这条指令确定性地做不成,重试无益"。调和循环把 directive 置 failed
+    (pending -> failed,非审核类状态机允许),原因写进 payload["failure"],之后不再派发。
+    其余异常原样上抛、directive 保持 pending:下一轮重新派发,续训与次数上限由派发器自己兜底。"""
+
+    def __init__(self, reason: str, *, category: str = "T2") -> None:
+        super().__init__(reason)
+        self.reason = reason
+        self.category = category
+
+
+def stub_dispatcher(store: GitStore, directive: Envelope) -> None:
+    """默认派发器:骨架里的桩,行为与引入派发缝之前逐字节相同。"""
+    execute_dispatch(store, directive.id, "tom")
+
+
 def _iter_run_ids(store: GitStore) -> list[str]:
     runs_dir = store.repo_root / CONTROL_PLANE_ROOT / "runs"
     if not runs_dir.is_dir():
@@ -54,10 +75,23 @@
     return TreeState(directives=directives, candidates=candidates, model_count=model_count)
 
 
-def apply_action(store: GitStore, action: Action) -> None:
+def apply_action(
+    store: GitStore, action: Action, *, dispatcher: Dispatcher | None = None
+) -> None:
     if action.kind == "dispatch_directive":
         run_id = action.target_id
-        execute_dispatch(store, run_id, "tom")
+        path = f"{run_dir(run_id)}/directive.yaml"
+        try:
+            (dispatcher or stub_dispatcher)(store, store.read_product(path))
+        except DirectiveFailedError as exc:
+            directive = store.read_product(path)
+            assert_transition("nonreview", directive.status, "failed")
+            directive.status = "failed"
+            directive.payload["failure"] = {"category": exc.category, "reason": exc.reason}
+            store.write_product(
+                path, directive, f"reconcile: directive {run_id} failed ({exc.category})"
+            )
+            return
         directive = store.read_product(f"{run_dir(run_id)}/directive.yaml")
         assert_transition("nonreview", directive.status, "executed")
         directive.status = "executed"
@@ -117,20 +151,27 @@
     # await_review / noop：无自动动作，等待人工
 
 
-def reconcile_once(store: GitStore) -> list[Action]:
-    """一次性调和：读状态 → reconcile → 执行非人工动作。返回本轮动作列表。"""
+def reconcile_once(store: GitStore, *, dispatcher: Dispatcher | None = None) -> list[Action]:
+    """一次性调和：读状态 → reconcile → 执行非人工动作。返回本轮动作列表。
+    dispatcher 缺省为桩;注入后只替换"下发 directive"这一步,其余动作不变。"""
     state = read_tree_state(store)
     actions = reconcile(state)
     for action in actions:
-        apply_action(store, action)
+        apply_action(store, action, dispatcher=dispatcher)
     return actions
 
 
-def run_daemon(store: GitStore, interval_s: float, max_ticks: int | None = None) -> None:
+def run_daemon(
+    store: GitStore,
+    interval_s: float,
+    max_ticks: int | None = None,
+    *,
+    dispatcher: Dispatcher | None = None,
+) -> None:
     """轮询守护进程（spec 补充 B.1：轮询而非 inotify）。max_ticks 供测试限次。"""
     ticks = 0
     while max_ticks is None or ticks < max_ticks:
-        reconcile_once(store)
+        reconcile_once(store, dispatcher=dispatcher)
         ticks += 1
         if max_ticks is not None and ticks >= max_ticks:
             break
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run --extra dev pytest tests/test_daemon_dispatcher.py -q`
Expected: 11 passed。

- [ ] **Step 5: 全量回归**

Run: `uv run --extra dev pytest -q`
Expected: 774 passed,无警告。再跑 `git status --short`:只应有 `src/controlplane/daemon.py` 和 `tests/test_daemon_dispatcher.py`(以及原本就有的 `cli/package*.json`,不要碰)。

- [ ] **Step 6: 提交**

```bash
git add src/controlplane/daemon.py tests/test_daemon_dispatcher.py
git commit -m "feat: controlplane dispatch seam (injectable dispatcher, stub stays the default, DirectiveFailedError fails a directive for good)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 17: MindTrainer 派发器

**Files:**
- Create: `src/mindtrainer/dispatch.py`
- Test: `tests/test_mt_dispatch.py`

**Interfaces:**
- Consumes: 任务 16 的 `Dispatcher`、`DirectiveFailedError`;`load_config_from_directive`、`validate_training_config`、`InvalidTrainingConfigError`;`resume_or_run`、`NotEnoughGroupsError`;`read_training_run`;`BatchTrajectorySource`、`InteractTrajectorySource`;`build_attribution_backend`;`MockExecutor`、`MockConverter`;`dataset_manifest_file`、`read_manifest`、`DatasetManifest`、`DatasetIntegrityError`。
- Produces(任务 18 依赖):
  - `make_mindtrainer_dispatcher(*, executor_factory=MockExecutor, converter=None, client_factory=None, policy=None) -> Dispatcher`。`executor_factory` 每次派发调用一次,所以每次派发拿到一个全新的执行器(续训靠 `training_run` 里的存点,不靠执行器对象的内存);`converter` 缺省 `MockConverter()`;`client_factory` 与 `policy` 只有 Interact 模式用。

**设计要点:**
- **派发器只做一件事:把一条 pending 的指令跑成候选。** 起点与续训交给 `resume_or_run`:指令若在上一轮崩在中途,它仍是 `pending`,下一轮重新派发时从 `training_run` 的存点接着跑,不从头来。派发器不碰指令状态,那是调和循环的事(任务 16)。
- **`run_id = directive.id`**,配置经 `load_config_from_directive(directive)` 读出,不另写解析。
- **失败分两类,界线在"重试有没有用":**

| 情形 | 结果 |
|---|---|
| 指令里没有 `training_config`、配置缺字段或类型不对、没过 `validate_training_config` | `DirectiveFailedError`,指令置 `failed` |
| `reward` 用 judge 归因(本入口没有 judge,`build_attribution_backend` 抛 `ValueError`) | `DirectiveFailedError` |
| `mode` 是 interact 但没有注入 `client_factory` 与 `policy` | `DirectiveFailedError`,原因里写明缺什么 |
| 点名的清单 id 不是 `dsm-<run>-<阶段>` 的形状、清单文件不在树里、文件里的 id 与点名的不一致 | `DirectiveFailedError` |
| `resume_or_run` 抛 `DatasetIntegrityError`、`DegenerateGroupError`、`AttributionError`、`NotEnoughGroupsError`、`InvalidTrainingConfigError`、`NeedsHumanError` | `DirectiveFailedError`,原因带异常类型名 |
| 其余异常,且 `training_run` 已被编排器写成 `failed`(例如服务端以 `invalid_session` 拒绝 rollout:编排器先写 `failed` 再重抛) | `DirectiveFailedError`。否则这一轮的异常会逃出调和循环,下一轮才因 `NeedsHumanError` 判失败 |
| 其余异常,`training_run` 仍是 `pending` 或不存在(`SimulatedCrashError`、`OSError`、瞬态 `RolloutFailedError` 等) | **原样上抛**,指令保持 `pending`,下一轮重新派发 |

- **清单缺失必须显式判。** `GitStore.read_product` 读不存在的文件抛 `FileNotFoundError`,`read_manifest` 只会因内容对不上抛 `DatasetIntegrityError`,所以缺清单若不显式判,会落进"其余异常",被当成瞬态失败每轮重派。
- **数据所在的 run 从清单 id 里解出来。** 清单 id 是 `dsm-{数据所在的 run}-{阶段}`。晋级后的下一轮是新 run,数据集仍挂在前一轮的 run 下,所以不能假设它等于指令自己的 run。解出来的 run 下必须有清单文件,且文件里的 `id` 必须等于点名的那个,否则判 `failed`。
- **只读用得到的阶段。** `sft_steps > 0` 才要 sft 的清单,`grpo_steps > 0` 才要 grpo_opd 的;`stage_manifest_refs` 里多写的阶段不读(有测试)。Interact 模式下 grpo_opd 来自实时会话,不读清单。
- **`NeedsHumanError` 也算确定性失败。** 续训次数用尽、`allow_resume` 为假、存点损坏,`resume_or_run` 都以它升级给人;指令因此置 `failed`,不再每轮重派(有测试:`max_resume_attempts=1` 的指令崩两次后被判失败)。
- **训练本身用 mock**:默认 `MockExecutor` 与 `MockConverter`,所以候选里的 `checkpoint_ref` 是 mock 的转换后引用(契约键名与形状不变,调和循环晋级时原样带走)。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_mt_dispatch.py`:

```python
"""MindTrainer 派发器:把调和循环的 pending 指令跑成候选。"""
import time
from types import SimpleNamespace

import grpc
import pytest

from controlplane.daemon import reconcile_once
from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, dataset_manifest_file, run_dir
from mindtrainer.dispatch import make_mindtrainer_dispatcher
from mindtrainer.executor import MockExecutor, SimulatedCrashError
from mindtrainer.grouping import DegenerateGroupError
from mindtrainer.orchestrator import NotEnoughGroupsError
from mindtrainer.resume import NeedsHumanError
from mindtrainer.reward_pipeline import AttributionError
from mindtrainer.training_config import InvalidTrainingConfigError, RewardConfig
from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
from simulator.dataset import DatasetIntegrityError, produce_grouped_dataset, read_manifest
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


def _cfg_dict(**kw):
    base = dict(
        mode="batch", trained_slot="a", base_checkpoint_ref=BASE.to_dict(),
        teacher_model_ref=TEACHER.to_dict(), reward=REWARD.to_dict(),
        sft_steps=1, grpo_steps=1, sft_batch_size=1, groups_per_step=1, group_size=2,
        grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=1, allow_resume=True,
        stage_manifest_refs={"sft": f"dsm-{RUN}-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"},
    )
    base.update(kw)
    return base


def _directive(store, run_id=RUN, payload=None):
    env = Envelope(
        id=run_id, type="directive", version=1, created_at="2026-10-07T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1,
        payload=payload if payload is not None else {"target_dimension": "tom", "recipe_ref": ""},
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "human: write directive")


def _scenario(sid):
    return Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")


def _rollout(scenario, k):
    first = ("agreed " if k == 0 else "no ") + f"{scenario.id}-a{k}"
    return {"a": ScriptedBackend({"a": [first, "a2"]}), "b": ScriptedBackend({"b": ["b1", "b2"]})}


def _seed_data(store, *, run_id=RUN, group_size=2):
    personas = {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}
    for name, size in (("sft", 1), ("grpo_opd", group_size)):
        produce_grouped_dataset(
            store, run_id, name,
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
    for sid in ("s1", "s2"):
        write_scenario(store, _scenario(sid))
    for p in personas.values():
        write_persona(store, p)


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    _seed_data(s)
    return s


def test_a_batch_directive_trains_and_leaves_a_candidate_for_review(store):
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    cand = store.read_product(candidate_checkpoint_file(RUN))
    assert cand.status == "pending_review"
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "executed"
    assert store.read_product(f"{run_dir(RUN)}/training_run.yaml").status == "executed"


def test_the_directive_is_marked_executed_only_after_the_candidate_exists(store):
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    seen = []

    def watching(s, d):
        seen.append(store.exists(candidate_checkpoint_file(RUN)))
        make_mindtrainer_dispatcher()(s, d)
        seen.append(store.exists(candidate_checkpoint_file(RUN)))

    reconcile_once(store, dispatcher=watching)
    assert seen == [False, True]  # 派发前没有候选,派发后有;状态迁移发生在派发器返回之后


def test_a_second_tick_does_not_train_again(store):
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    disp = make_mindtrainer_dispatcher()
    reconcile_once(store, dispatcher=disp)
    before = store.read_product(f"{run_dir(RUN)}/training_run.yaml")
    actions = reconcile_once(store, dispatcher=disp)
    assert [a.kind for a in actions] == ["request_diagnostic"]  # 候选待审,不再派发
    assert store.read_product(f"{run_dir(RUN)}/training_run.yaml") == before


def test_a_missing_manifest_fails_the_directive_instead_of_retrying(store, tmp_path):
    other = GitStore(str(tmp_path / "no-data"))
    other.init()
    _directive(other, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    actions = reconcile_once(other, dispatcher=make_mindtrainer_dispatcher())
    assert [a.kind for a in actions] == ["dispatch_directive"]  # 动作照常返回,但已被判失败
    env = other.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert "manifest" in env.payload["failure"]["reason"]


def test_a_directive_without_a_training_config_fails_rather_than_retrying(store):
    _directive(store)  # 只有 target_dimension/recipe_ref
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T2"
    assert "training_config" in env.payload["failure"]["reason"]


def test_an_invalid_config_fails_the_directive(store):
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "",
                               "training_config": _cfg_dict(sft_steps=-1)})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert "sft_steps" in env.payload["failure"]["reason"]


def test_interact_without_a_client_factory_fails_the_directive(store):
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "",
                               "training_config": _cfg_dict(mode="interact", scenario_ids=["s1"],
                                                            bindings=[RoleBinding("a", "pa", "self_play").to_dict()],
                                                            max_turns_per_episode=4)})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert "interact" in env.payload["failure"]["reason"]


def test_transient_failure_keeps_the_directive_pending_for_the_next_tick(store):
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    disp = make_mindtrainer_dispatcher(executor_factory=lambda: MockExecutor(crash_at_step=1))
    for _ in range(2):
        with pytest.raises(SimulatedCrashError):
            reconcile_once(store, dispatcher=disp)
        assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "pending"


def test_a_dispatch_crash_then_retry_resumes_from_the_checkpoint(store):
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    with pytest.raises(SimulatedCrashError):
        reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
            executor_factory=lambda: MockExecutor(crash_at_step=1)))
    pending = store.read_product(f"{run_dir(RUN)}/training_run.yaml")
    assert pending.status == "pending"

    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    run = store.read_product(f"{run_dir(RUN)}/training_run.yaml")
    assert (run.status, run.payload["resume_attempts"]) == ("executed", 1)
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"


def test_a_later_round_can_train_on_a_dataset_that_lives_under_another_run(store):
    """晋级后的下一轮是新的 run(run-2),数据集仍挂在 run-1 下:数据所在的 run 要从清单 id 解出来。"""
    _directive(store, run_id="run-2", payload={"target_dimension": "tom", "recipe_ref": "",
                                               "training_config": _cfg_dict()})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    assert store.read_product(f"{run_dir('run-2')}/directive.yaml").status == "executed"
    assert store.read_product(f"{run_dir('run-2')}/training_run.yaml").status == "executed"
    assert store.read_product(candidate_checkpoint_file("run-2")).status == "pending_review"


@pytest.mark.parametrize("ref", ["nonsense", "dsm--sft", f"dsm-{RUN}-grpo_opd"])
def test_a_manifest_id_that_is_not_for_this_stage_fails_the_directive(store, ref):
    cfg = _cfg_dict(stage_manifest_refs={"sft": ref, "grpo_opd": f"dsm-{RUN}-grpo_opd"})
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert "stage_manifest_refs['sft']" in env.payload["failure"]["reason"]


def test_a_manifest_naming_a_run_that_has_no_data_fails_the_directive(store):
    cfg = _cfg_dict(stage_manifest_refs={"sft": "dsm-run-9-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"})
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert "'dsm-run-9-sft'" in env.payload["failure"]["reason"]


def test_a_manifest_file_that_is_not_the_one_the_config_names_fails_the_directive(store):
    path = dataset_manifest_file(RUN, "sft")
    env = store.read_product(path)
    env.payload["id"] = "dsm-someone-else-sft"
    store.write_product(path, env, "tamper: manifest id")
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    failed = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert failed.status == "failed"
    assert "dsm-someone-else-sft" in failed.payload["failure"]["reason"]


def test_a_judge_attribution_directive_fails_because_this_entry_has_no_judge(store):
    reward = dict(REWARD.to_dict(), attribution="judge", judge_model_id="judge-1",
                  rubric_version="r1", outcome_rules={})
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "",
                               "training_config": _cfg_dict(reward=reward)})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert "judge" in env.payload["failure"]["reason"]
    assert not store.exists(f"{run_dir(RUN)}/training_run.yaml")  # 一步都没训,也没留半截记录


def test_a_directive_that_keeps_crashing_is_failed_once_the_resume_limit_is_spent(store):
    cfg = _cfg_dict(max_resume_attempts=1)
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    crashing = make_mindtrainer_dispatcher(executor_factory=lambda: MockExecutor(crash_at_step=1))
    for expected_attempts in (0, 1):  # 第一次新运行,第二次续训;都崩在第 1 步
        with pytest.raises(SimulatedCrashError):
            reconcile_once(store, dispatcher=crashing)
        assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "pending"
        assert store.read_product(f"{run_dir(RUN)}/training_run.yaml").payload["resume_attempts"] == expected_attempts

    reconcile_once(store, dispatcher=crashing)  # 次数已满:升级为人工,指令置 failed,不再重派
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert "resume attempts exhausted" in env.payload["failure"]["reason"]
    assert reconcile_once(store, dispatcher=crashing) == []


@pytest.mark.parametrize(
    "exc",
    [
        DatasetIntegrityError("x"), DegenerateGroupError(["g"], 2), AttributionError("x"),
        NotEnoughGroupsError("x"), InvalidTrainingConfigError("x"), NeedsHumanError("x"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_every_deterministic_orchestrator_failure_fails_the_directive(store, monkeypatch, exc):
    def raiser(*args, **kwargs):
        raise exc

    monkeypatch.setattr("mindtrainer.dispatch.resume_or_run", raiser)
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert type(exc).__name__ in env.payload["failure"]["reason"]


@pytest.mark.parametrize("exc", [RuntimeError("x"), OSError("disk")], ids=lambda e: type(e).__name__)
def test_any_other_orchestrator_exception_leaves_the_directive_pending(store, monkeypatch, exc):
    def raiser(*args, **kwargs):
        raise exc

    monkeypatch.setattr("mindtrainer.dispatch.resume_or_run", raiser)
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    with pytest.raises(type(exc)):
        reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "pending"


def test_a_tampered_episode_fails_the_directive(store):
    episode_id = read_manifest(store, RUN, "grpo_opd").episode_refs[0]["id"]
    episode = read_episode(store, RUN, episode_id)
    episode.scenario_id = "tampered"
    write_episode(store, RUN, episode)
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env.status == "failed"
    assert "DatasetIntegrityError" in env.payload["failure"]["reason"]
    assert store.read_product(f"{run_dir(RUN)}/training_run.yaml").status in ("pending", "failed")


@pytest.fixture
def live(store):
    """真实的 loopback gRPC 服务端(127.0.0.1),和编排器测试用的是同一套。"""
    channels, servers = [], []

    def start():
        closed = []

        def backends(scenario, assignment, personas, client_slot):
            return {
                slot: ScriptedBackend({slot: ["b1", "b2", "b3", "b4"]})
                for slot in assignment.slot_ids() - {client_slot}
            }

        server = create_server(
            backends, port=0, turn_timeout_s=2.0, on_session_closed=closed.append,
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


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_an_interact_directive_trains_against_a_live_server_and_leaves_a_candidate(store, live):
    env = live()
    cfg = _cfg_dict(
        mode="interact", grpo_steps=2, scenario_ids=["s1", "s2"], max_turns_per_episode=4,
        stage_manifest_refs={"sft": f"dsm-{RUN}-sft"},
        bindings=[RoleBinding("a", "pa", "self_play").to_dict(),
                  RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1").to_dict()],
    )
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    policy = ScriptedBackend({"a": ["agreed", "x", "no", "x"] * 8})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=env.client_factory, policy=policy))
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "executed"
    assert store.read_product(f"{run_dir(RUN)}/training_run.yaml").status == "executed"
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"
    assert _wait_until(lambda: len(env.closed) == 4)  # 2 个 GRPO step x 每步一组 2 条 rollout


def test_a_rollout_the_server_rejects_for_good_fails_the_directive_in_the_same_tick(store, live):
    """max_turns_per_episode 超过服务端的 max_turns_cap(200):服务端以 invalid_session 拒绝,重试无益。
    编排器把 training_run 写成 failed 再重抛;派发器不能让这个异常逃出调和循环,要当轮判指令 failed。"""
    env = live()
    cfg = _cfg_dict(
        mode="interact", grpo_steps=1, scenario_ids=["s1"], max_turns_per_episode=1000,
        stage_manifest_refs={"sft": f"dsm-{RUN}-sft"},
        bindings=[RoleBinding("a", "pa", "self_play").to_dict(),
                  RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1").to_dict()],
    )
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    disp = make_mindtrainer_dispatcher(
        client_factory=env.client_factory, policy=ScriptedBackend({"a": ["agreed", "x"] * 8}))
    reconcile_once(store, dispatcher=disp)  # 不抛
    env_d = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert env_d.status == "failed"
    assert "invalid_session" in env_d.payload["failure"]["reason"]
    assert store.read_product(f"{run_dir(RUN)}/training_run.yaml").status == "failed"
    assert reconcile_once(store, dispatcher=disp) == []


def test_a_stage_with_no_steps_is_not_read_even_if_the_config_names_a_manifest_for_it(store):
    """sft_steps=0:sft 的清单用不到。点名一份不在树里的,不该拖垮指令。"""
    cfg = _cfg_dict(sft_steps=0, stage_manifest_refs={"sft": "dsm-run-9-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"})
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "executed"
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"


def test_a_batch_directive_without_grpo_steps_does_not_read_the_grpo_manifest(store):
    cfg = _cfg_dict(grpo_steps=0, stage_manifest_refs={"sft": f"dsm-{RUN}-sft", "grpo_opd": "dsm-run-9-grpo_opd"})
    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": cfg})
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "executed"
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run --extra dev pytest tests/test_mt_dispatch.py -q`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindtrainer.dispatch'`。

- [ ] **Step 3: 写实现**

新建 `src/mindtrainer/dispatch.py`:

```python
from __future__ import annotations

from collections.abc import Callable

from controlplane.daemon import DirectiveFailedError, Dispatcher
from controlplane.envelope import Envelope
from controlplane.gitstore import GitStore
from controlplane.paths import dataset_manifest_file
from mindtrainer.checkpoint import CheckpointConverter, MockConverter
from mindtrainer.executor import MockExecutor, TrainingExecutor
from mindtrainer.grouping import DegenerateGroupError
from mindtrainer.orchestrator import NotEnoughGroupsError, resume_or_run
from mindtrainer.resume import NeedsHumanError
from mindtrainer.reward_pipeline import (
    AttributionError, AttributionBackend, build_attribution_backend,
)
from mindtrainer.store_adapter import read_training_run
from mindtrainer.training_config import (
    InvalidTrainingConfigError, TrainingConfig, load_config_from_directive, validate_training_config,
)
from mindtrainer.trajectory_source import (
    BatchTrajectorySource, InteractTrajectorySource, TrajectorySource,
)
from simulator.backends import ModelBackend
from simulator.dataset import DatasetIntegrityError, DatasetManifest, read_manifest

# 确定性地做不成的失败:重试同样的指令还会再失败,所以映射成 directive 的 failed。
# 其余异常原样上抛,directive 保持 pending,下一轮重新派发(瞬态恢复)。
_DETERMINISTIC = (
    DatasetIntegrityError, DegenerateGroupError, AttributionError,
    NotEnoughGroupsError, InvalidTrainingConfigError, NeedsHumanError,
)


def _required_stages(cfg: TrainingConfig) -> list[str]:
    """真正会用到的阶段:配了步数的才算。stage_manifest_refs 里多写的阶段不读。"""
    wanted = []
    if cfg.sft_steps > 0:
        wanted.append("sft")
    if cfg.grpo_steps > 0:
        wanted.append("grpo_opd")
    return wanted


def _data_run_id(run_id: str, ref: str, stage: str) -> str:
    """清单 id 的格式是 dsm-{数据所在的 run}-{阶段}(build_manifest 是唯一的构造点)。后一轮训练
    常常复用前一轮的数据集,所以数据所在的 run 从 id 里解出来,不假设它等于指令自己的 run。"""
    head, tail = "dsm-", f"-{stage}"
    if not (ref.startswith(head) and ref.endswith(tail) and len(ref) > len(head) + len(tail)):
        raise DirectiveFailedError(
            f"directive {run_id}: stage_manifest_refs[{stage!r}]={ref!r} is not a manifest id "
            f"of the form 'dsm-<run>-{stage}'"
        )
    return ref[len(head):-len(tail)]


def _batch_manifests(store: GitStore, run_id: str, cfg: TrainingConfig) -> dict[str, DatasetManifest]:
    """指令点名的清单必须在场,且就是点名的那一份。缺文件是配置问题(确定性),而 read_product
    缺文件抛的是 FileNotFoundError、read_manifest 又只会因内容对不上抛 DatasetIntegrityError,
    所以这里显式判一次,免得缺清单被当成瞬态失败反复重派。"""
    manifests: dict[str, DatasetManifest] = {}
    for name in _required_stages(cfg):
        ref = cfg.stage_manifest_refs.get(name)
        if not ref:
            continue
        data_run = _data_run_id(run_id, ref, name)
        if not store.exists(dataset_manifest_file(data_run, name)):
            raise DirectiveFailedError(
                f"directive {run_id}: dataset manifest {ref!r} for stage {name!r} is not in the tree"
            )
        manifest = read_manifest(store, data_run, name)
        if manifest.id != ref:
            raise DirectiveFailedError(
                f"directive {run_id}: the manifest at {dataset_manifest_file(data_run, name)} "
                f"is {manifest.id!r}, not the {ref!r} the config names"
            )
        manifests[name] = manifest
    return manifests


def _batch_sources(store: GitStore, run_id: str, cfg: TrainingConfig) -> Callable[[int], dict[str, TrajectorySource]]:
    """数据集清单在指令里点名(stage_manifest_refs),按 id 读回;清单内容与 episode 对不上时
    verify_manifest 抛 DatasetIntegrityError(确定性失败,映射见 _DETERMINISTIC)。"""
    manifests = _batch_manifests(store, run_id, cfg)

    def make_sources(_start: int) -> dict[str, TrajectorySource]:
        return {name: BatchTrajectorySource(store, m) for name, m in manifests.items()}

    return make_sources


def _interact_sources(
    store: GitStore,
    run_id: str,
    cfg: TrainingConfig,
    client_factory: Callable[[], object],
    policy: ModelBackend,
) -> Callable[[int], dict[str, TrajectorySource]]:
    """interact 的 grpo_opd 来自实时会话,只有 sft 用清单;清单缺失同样在这里先判。"""
    manifests = _batch_manifests(store, run_id, cfg)

    def make_sources(start: int) -> dict[str, TrajectorySource]:
        sources: dict[str, TrajectorySource] = {
            name: BatchTrajectorySource(store, m) for name, m in manifests.items()
        }
        if cfg.grpo_steps > 0:
            sources["grpo_opd"] = InteractTrajectorySource(
                client_factory, policy, store, cfg, start_step=start
            )
        return sources

    return make_sources


def make_mindtrainer_dispatcher(
    *,
    executor_factory: Callable[[], TrainingExecutor] = MockExecutor,
    converter: CheckpointConverter | None = None,
    client_factory: Callable[[], object] | None = None,
    policy: ModelBackend | None = None,
) -> Dispatcher:
    """构造 MindTrainer 派发器。它只做一件事:把一条 pending 的 directive 跑成候选。

    起点与续训交给 resume_or_run:指令若在上一轮崩在中途,directive 仍是 pending,
    下一轮重新派发时从 training_run 的存点接着跑,不从头再来。

    - batch:按 stage_manifest_refs 读数据集清单。
    - interact:另需 client_factory 与 policy,注入后 grpo_opd 走实时会话。
    - 确定性失败(数据不对、配置不合法、该人工介入)抛 DirectiveFailedError,调和循环把
      directive 置 failed,不再重试;其余异常原样上抛,保持 pending 等下一轮。
    """
    converter = converter or MockConverter()

    def dispatch(store: GitStore, directive: Envelope) -> None:
        run_id = directive.id
        try:
            cfg = load_config_from_directive(directive)
        except InvalidTrainingConfigError as exc:
            raise DirectiveFailedError(f"directive {run_id}: {exc}") from exc
        reason = validate_training_config(cfg)
        if reason is not None:
            raise DirectiveFailedError(f"directive {run_id}: {reason}")
        try:
            # 本入口不提供 judge:judge 归因的指令做不成,重试无益,所以判失败而不是让 ValueError 逃出去
            attribution: AttributionBackend = build_attribution_backend(cfg.reward)
        except ValueError as exc:
            raise DirectiveFailedError(f"directive {run_id}: {exc}") from exc
        if cfg.mode == "interact":
            if client_factory is None or policy is None:
                raise DirectiveFailedError(
                    f"directive {run_id}: interact mode needs a client_factory and a policy"
                )
            make_sources = _interact_sources(store, run_id, cfg, client_factory, policy)
        else:
            make_sources = _batch_sources(store, run_id, cfg)
        try:
            resume_or_run(
                cfg, store=store, run_id=run_id, make_sources=make_sources,
                attribution=attribution, executor=executor_factory(), converter=converter,
            )
        except _DETERMINISTIC as exc:
            raise DirectiveFailedError(f"directive {run_id}: {type(exc).__name__}: {exc}") from exc
        except Exception as exc:
            # 编排器对 invalid_session 这类确定性的 rollout 拒绝,会先把 training_run 写成 failed 再重抛。
            # 记录已是终态,下一轮重派只会换来 NeedsHumanError,而这一轮的异常会一路逃出守护循环,
            # 所以当轮就把指令判 failed。记录仍是 pending 的瞬态异常照旧上抛。
            record = read_training_run(store, run_id)
            if record is not None and record.status == "failed":
                raise DirectiveFailedError(f"directive {run_id}: {type(exc).__name__}: {exc}") from exc
            raise

    return dispatch
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run --extra dev pytest tests/test_mt_dispatch.py -q`
Expected: 30 passed(含两个用真实 loopback gRPC 服务端的 Interact 用例,约 20 秒属正常)。

- [ ] **Step 5: 全量回归**

Run: `uv run --extra dev pytest -q`
Expected: 804 passed,无警告。`git status --short` 只应有 `src/mindtrainer/dispatch.py` 和 `tests/test_mt_dispatch.py`(以及原本就有的 `cli/package*.json`,不要碰)。

- [ ] **Step 6: 提交**

```bash
git add src/mindtrainer/dispatch.py tests/test_mt_dispatch.py
git commit -m "feat: mindtrainer dispatcher (directive to candidate through resume_or_run, deterministic failures fail the directive for good)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 18: 接线入口与端到端

**Files:**
- Create: `src/mindtrainer/wire.py`
- Create: `scripts/run_controlplane.py`
- Modify: `docs/superpowers/specs/2026-10-03-mindtrainer-design.md`(§11 第一条)
- Test: `tests/test_mt_wire.py`

**Interfaces:**
- Consumes: `controlplane.daemon.run_daemon(store, interval_s, max_ticks=None, *, dispatcher=None)`(任务 16);`mindtrainer.dispatch.make_mindtrainer_dispatcher()`(任务 17)。
- Produces:
  - `run(repo_root: str, *, interval_s: float = 2.0, max_ticks: int | None = None) -> None`
  - `main(argv: list[str] | None = None) -> int`:命令行 `repo_root`、`--interval-s`、`--max-ticks`;返回 0。

**设计要点:**
- **入口住在 `mindtrainer` 一侧。** `mindtrainer` 依赖 `controlplane`,反过来不成立,所以 `controlplane.daemon` 只见到一个 `Callable`,不认识 MindTrainer;不会产生循环导入。
- **训练用 `MockExecutor` 与 `MockConverter`**(设计 §11:真实 ROLL 后置)。调和循环本身是真的:扫 git 树、决定动作、写产物、把指令置 `executed`。
- **batch 指令可跑;Interact 与 judge 归因的指令不可跑,会被判 `failed` 并写明原因**,而不是静默跑错。入口没有实时服务端、策略模型和 judge。
- **`scripts/run_controlplane.py` 只是薄壳**:把 `src` 放进 `sys.path`,调 `mindtrainer.wire.main`。逻辑都在 `wire.py`,所以测试直接测 `run` / `main`,不用起子进程。
- **`--max-ticks` 供测试和冒烟用**;不填则一直跑。
- **端到端测试走完整条链**:一条 batch 指令 → 候选 `pending_review` → 诊断(桩)→ 等待审核 → 人工批准 → 晋级为 `zengen-v1`。这条链上除了"派发"这一步换成了 MindTrainer,其余都是既有的调和循环。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_mt_wire.py`:

```python
"""接线入口:调和循环 + MindTrainer 派发器,端到端跑一条 batch 指令。"""
import pytest

from controlplane.daemon import reconcile_once
from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, model_file, run_dir
from mindtrainer.training_config import RewardConfig
from mindtrainer.wire import main, run
from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
from simulator.dataset import produce_grouped_dataset
from simulator.store_adapter import write_persona, write_scenario

RUN = "run-1"
BASE = BigRef("file:///ckpt/base", "a" * 64, 1)
TEACHER = BigRef("file:///ckpt/teacher", "b" * 64, 1)
REWARD = RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]})


def _scenario(sid):
    return Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")


def _rollout(scenario, k):
    first = ("agreed " if k == 0 else "no ") + f"{scenario.id}-a{k}"
    return {"a": ScriptedBackend({"a": [first, "a2"]}), "b": ScriptedBackend({"b": ["b1", "b2"]})}


@pytest.fixture
def repo(tmp_path):
    store = GitStore(str(tmp_path / "cp"))
    store.init()
    personas = {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}
    for name, size in (("sft", 1), ("grpo_opd", 2)):
        produce_grouped_dataset(
            store, RUN, name,
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
    for sid in ("s1", "s2"):
        write_scenario(store, _scenario(sid))
    for p in personas.values():
        write_persona(store, p)
    env = Envelope(
        id=RUN, type="directive", version=1, created_at="2026-10-07T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1,
        payload={
            "target_dimension": "tom", "recipe_ref": "",
            "training_config": {
                "mode": "batch", "trained_slot": "a",
                "base_checkpoint_ref": BASE.to_dict(), "teacher_model_ref": TEACHER.to_dict(),
                "reward": REWARD.to_dict(), "sft_steps": 1, "grpo_steps": 1, "sft_batch_size": 1,
                "groups_per_step": 1, "group_size": 2, "grpo_config": {}, "opd_coefficient": 0.1,
                "checkpoint_every_steps": 1, "allow_resume": True,
                "stage_manifest_refs": {"sft": f"dsm-{RUN}-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"},
            },
        },
    )
    store.write_product(f"{run_dir(RUN)}/directive.yaml", env, "human: write directive")
    return store


def test_one_tick_trains_the_directive_and_leaves_a_candidate(repo, tmp_path):
    run(str(tmp_path / "cp"), interval_s=0, max_ticks=1)
    assert repo.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"
    assert repo.read_product(f"{run_dir(RUN)}/directive.yaml").status == "executed"
    assert repo.read_product(f"{run_dir(RUN)}/training_run.yaml").status == "executed"


def test_main_parses_arguments_and_can_run_a_single_tick(repo, tmp_path):
    assert main([str(tmp_path / "cp"), "--interval-s", "0", "--max-ticks", "1"]) == 0
    assert repo.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"


def test_the_wired_loop_still_reaches_promotion(repo, tmp_path):
    """端到端:一条 batch 指令经调和循环 → 候选 → 诊断 → 人工批准 → 晋级。"""
    run(str(tmp_path / "cp"), interval_s=0, max_ticks=1)
    reconcile_once(repo)  # request_diagnostic
    reconcile_once(repo)  # await_review
    cand = repo.read_product(candidate_checkpoint_file(RUN))
    cand.status = "approved"
    repo.write_product(candidate_checkpoint_file(RUN), cand, "human: approve")
    reconcile_once(repo)  # promote_candidate
    model = repo.read_product(model_file(1))
    assert (model.id, model.payload["promoted_from"]) == ("zengen-v1", "cand-run-1")


def test_a_second_tick_over_a_finished_run_does_nothing(repo, tmp_path):
    run(str(tmp_path / "cp"), interval_s=0, max_ticks=1)
    before = repo.read_product(f"{run_dir(RUN)}/training_run.yaml")
    run(str(tmp_path / "cp"), interval_s=0, max_ticks=1)
    assert repo.read_product(f"{run_dir(RUN)}/training_run.yaml") == before
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run --extra dev pytest tests/test_mt_wire.py -q`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindtrainer.wire'`。

- [ ] **Step 3: 写实现**

新建 `src/mindtrainer/wire.py`:

```python
"""接线入口:把 ControlPlane 的调和循环接到 MindTrainer 派发器上。

分层方向:入口住在 mindtrainer 侧(mindtrainer 依赖 controlplane,反之不成立),
所以 controlplane.daemon 只见到一个 Callable,不认识 MindTrainer。

训练用 mock 执行器与 mock 转换器(设计 §11:真实 ROLL 后置)。调和循环本身是真的:
它扫 git 树、决定动作、写产物、把指令置 executed。

batch 指令可跑;interact 指令还需要 client_factory 与 policy(实时服务端与策略模型),
本入口不提供,所以 interact 指令会被判 failed 并写明原因,而不是静默跑错。
"""
from __future__ import annotations

import argparse
import sys

from controlplane.daemon import run_daemon
from controlplane.gitstore import GitStore
from mindtrainer.dispatch import make_mindtrainer_dispatcher


def run(repo_root: str, *, interval_s: float = 2.0, max_ticks: int | None = None) -> None:
    """在 repo_root 上跑调和循环,下发指令时用 MindTrainer 派发器。"""
    store = GitStore(repo_root)
    run_daemon(
        store, interval_s, max_ticks=max_ticks,
        dispatcher=make_mindtrainer_dispatcher(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="调和循环 + MindTrainer 派发器")
    parser.add_argument("repo_root", help="ControlPlane 所在的 git 仓库根目录")
    parser.add_argument("--interval-s", type=float, default=2.0, help="两次扫描之间的间隔秒数")
    parser.add_argument("--max-ticks", type=int, default=None, help="跑几轮后退出;不填则一直跑")
    args = parser.parse_args(argv)
    run(args.repo_root, interval_s=args.interval_s, max_ticks=args.max_ticks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

新建 `scripts/run_controlplane.py`,并加上可执行位:

```python
#!/usr/bin/env python3
"""调和循环入口的薄壳,逻辑在 mindtrainer.wire(见那里的说明)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mindtrainer.wire import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
```

```bash
chmod +x scripts/run_controlplane.py
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run --extra dev pytest tests/test_mt_wire.py -q`
Expected: 4 passed。

- [ ] **Step 5: 冒烟(手动,验证薄壳和参数解析)**

```bash
S=$(mktemp -d)
uv run python -c "from controlplane.gitstore import GitStore; GitStore('$S').init()"
uv run python scripts/run_controlplane.py "$S" --interval-s 0 --max-ticks 1; echo "退出码=$?"
uv run python scripts/run_controlplane.py; echo "退出码=$?"
```

Expected: 第一次 `退出码=0`(空仓库跑一轮,什么都不做);第二次打印 `error: the following arguments are required: repo_root`,`退出码=2`。

- [ ] **Step 6: 更新设计文档 §11**

```bash
python3 - <<'PYEOF'
import pathlib

p = pathlib.Path("docs/superpowers/specs/2026-10-03-mindtrainer-design.md")
s = p.read_text(encoding="utf-8")
old = '''- 接入 ControlPlane 调和循环、替换 `mind_trainer_stub`(接线后置,含"候选出现 → 调真实诊断"和"`scenario_resolver` 读同一 ControlPlane")。'''
new = '''- 接入 ControlPlane 调和循环、替换 `mind_trainer_stub`:**派发缝、MindTrainer 派发器与 batch 接线入口已落地**(见 `docs/superpowers/plans/2026-10-07-mindtrainer-wiring.md`;调和循环默认仍走桩,注入派发器后才走 MindTrainer)。仍推迟:"候选出现 → 调真实诊断"(daemon 的 `request_diagnostic` 仍是桩)、`scenario_resolver` 读同一 ControlPlane(入口不启动实时服务端,所以 Interact 指令在入口处被判 failed;注入 `client_factory` 与 `policy` 的调用方可以跑)、judge 归因(入口不提供 judge)、`produce_grouped_dataset` 的驱动(清单必须已在树里)。'''
assert s.count(old) == 1, s.count(old)
p.write_text(s.replace(old, new), encoding="utf-8")
PYEOF
git diff --stat
```

Expected: `git diff --stat` 只列出设计文档(1 行改动);其余子条目(`on_session_closed` 等"接线时需要注意"那几行)原样保留。

- [ ] **Step 7: 全量回归**

Run: `uv run --extra dev pytest -q`
Expected: 808 passed,无警告。

- [ ] **Step 8: 提交**

```bash
git add src/mindtrainer/wire.py scripts/run_controlplane.py tests/test_mt_wire.py docs/superpowers/specs/2026-10-03-mindtrainer-design.md
git commit -m "feat: wire the reconcile loop to the mindtrainer dispatcher (entry script, batch directive to promotion end to end)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## 完成标准

三个任务都提交后:

- `uv run --extra dev pytest -q` 为 **819 通过**、无警告(基线 763,加本计划三个任务的用例与两轮修复轮、一次整支评审修复波补的用例;各文件用例数见下方「实施后修订」)。
- `git diff f432cf4 --stat`(基点取计划自己的提交)只涉及本计划 File Structure 表里的文件,**不含** `cli/package*.json`,也不含 `simulator/stubs.py`、`tests/test_stubs.py`。
- `src/controlplane` 下没有任何 `mindtrainer` 的导入;`src/mindtrainer` 下没有任何 `diagnostic` 的导入。
- 对一个只含一条 batch 指令和已有数据集清单的仓库运行 `uv run python scripts/run_controlplane.py <repo> --interval-s 0 --max-ticks 1`,留下 `training_run`(`executed`)和待审核候选;跑完是指令 `executed`、`training_run` 为 `executed`、候选 `pending_review`;晋级须人工批准候选后再跑一轮才会出现 `zengen-v1`(批准是带外的人工动作)。端到端测试 `test_the_wired_loop_still_reaches_promotion` 覆盖了这条链。

## 已知限制与不在本计划内的事

1. **瞬态异常会让守护进程退出。** `run_daemon` 不捕获派发器上抛的普通异常(`SimulatedCrashError`、`OSError`、瞬态 `RolloutFailedError`):指令保持 `pending`,重启入口后从存点续训,次数由 `max_resume_attempts` 兜底。但入口本身不自动重启,需要外部的进程管理。要不要在 `run_daemon` 里每轮 `try/except`,会改变既有"出错即停"的行为,是独立决定,本计划不动。
2. **入口只能跑 batch + outcome_split。** Interact 需要实时服务端、策略模型;judge 归因需要 judge。这两类指令在入口处被判 `failed` 并写明缺什么,而不是静默跑错。要跑 Interact,调用方自己构造 `make_mindtrainer_dispatcher(client_factory=..., policy=...)` 再传给 `run_daemon`(任务 17 的 Interact 用例就是这么做的)。
3. **默认桩仍写死 `"tom"`。** `stub_dispatcher` 保留了原来的 `execute_dispatch(store, run_id, "tom")`,target_dimension → purpose 的映射仍留给 recipe 计划。
4. **`failed` 的指令是终态。** 重来要写新指令(新 run id)。数据集可以复用:清单 id 里带着数据所在的 run。
5. **候选里的 `checkpoint_ref` 是 mock 转换后的引用。** 调和循环晋级、下一轮取它当基座都照常工作(`tests/test_mt_integration.py` 已覆盖),但它不是真实权重。设计 §10 第 2 条(推理格式权重能否直接当训练起点)仍然未核实。
6. **`request_diagnostic` 仍走 `diagnostic_evaluator_stub`。** 审核门里的诊断报告带 stub 标记。接真实诊断是另一个计划。
7. **设计 §11 里"接线时需要注意"的几条**(channel 复用、失败 rollout 遗留 episode、`turn_timeout_s`、`trained_model_id` 未锚定、`on_session_closed`)都是调用方构造实时客户端与服务端时的事,本入口不涉及,原样保留在设计文档里。

---

## 实施后修订(Post-implementation amendments)

本计划的 3 个任务落地并通过逐任务评审后,执行中发现并修掉了一处计划自带的缺陷,整支评审又提出一处 Important 与若干小项,经一次合并修复波处理。**以下修订优先于上文任务里的代码块;上文保留原样,作为当时的计划记录**。最终全量测试为 819 passed。

- **任务 17 的 `_required_stages` 按模式过滤阶段(提交 07bb2de)**:原代码块是 `if cfg.grpo_steps > 0:`,改为 `if cfg.mode == "batch" and cfg.grpo_steps > 0:`。原因:Interact 模式下 `grpo_opd` 来自实时会话,`_interact_sources` 事后会整段覆盖它,从不读清单;原写法把用不上的清单引用也当需要校验的阶段,于是"interact 配置顺手点了一个指向无数据 run 的 `grpo_opd` 清单"会让指令被误判 `failed`。改后与 `validate_training_config`(training_config.py:414)对称,batch 路径不变。新增用例 `test_an_interact_directive_ignores_a_stray_grpo_opd_manifest_ref` 钉住它。
- **整支评审的修复波(提交 09aeeff)**:
  - **清单文件在场但读不出来,现在判指令 `failed`。** 原代码里 `read_manifest` 不在任何 `try` 里:清单缺 `episode_refs` 键、YAML 留着合并冲突标记、`schema_version` 为未知版本,都会让异常逃出 `dispatch`,指令永远 `pending`,而且守护进程每次都死在同一处,排在后面的指令全被堵住。现在把 `KeyError`、`TypeError`、`AttributeError`、`yaml.YAMLError`、`UnknownSchemaVersionError` 转成 `DirectiveFailedError`,原因里带清单 id、阶段与异常类型。`OSError` 之类的瞬态错误仍原样上抛。新增参数化用例和"毒指令不堵后面的指令"用例。
  - **早期配置校验是承重的,补了测试。** 先前的变异检查我把它记成等价行为(因为 `run_training` 内部会再校验一次),这只对类型都对的配置成立。把它拿掉,`sft_steps="3"`、`grpo_steps=None`、`stage_manifest_refs` 的值非字符串这三种配置会在 `_required_stages` 与 `_data_run_id` 里抛 `TypeError` 或 `AttributeError`,逃出守护循环。新增参数化用例钉住。
  - `NeedsHumanError` 记成类别 `T3`(设计 §6.2 的语义),其余确定性失败仍是 `T2`。
  - 篡改 episode 用例的断言从 `status in ("pending", "failed")` 收紧为 `== "failed"`;入口测试 `test_a_second_tick_over_a_finished_run_does_nothing` 改名为 `test_a_second_tick_leaves_the_training_record_unchanged` 并加强断言;`wire.py` 的模块说明补三句(诊断仍是桩、循环遇未处理异常即退出需外部重启、`failed` 是终态且数据集须先于指令入树)。
- **全量数与各文件数的更正**:计划里写的各任务后全量数(774 / 804 / 808)是推算,未计入两轮修复轮补的测试。实况:任务 16 后 774、任务 16 修复轮后 777、任务 17 后 807、任务 17 修复轮后 808、任务 18 后 812、整支评审修复波后 819。File Structure 表里的行数与用例数同样过期:`dispatch.py` 为 195 行,`tests/test_daemon_dispatcher.py` 为 14 个用例,`tests/test_mt_dispatch.py` 为 38 个,`tests/test_mt_wire.py` 为 4 个。
- **「完成标准」两处订正**:
  - `git diff 4bdc4d4 --stat` 的基点写错了。4bdc4d4 在计划提交之前,diff 会把计划文件本身(1312 行)也算进来,而它不在 File Structure 表里。基点应取计划自己的提交。
  - 跑 `--max-ticks 5` 之后**不会**出现 `zengen-v1`:批准是带外的人工动作。跑完的状态是指令 `executed`、`training_run` 为 `executed`、候选 `pending_review`。要看到晋级,须人工批准候选后再跑一轮,端到端测试 `test_the_wired_loop_still_reaches_promotion` 就是这么做的。
- **仍未处理(补进「已知限制」)**:
  - `training_run.yaml` 被写坏时 `read_training_run` 抛 `ParserError`,指令一直 `pending`;人为改坏这份记录的概率远低于清单,本分支不处理。
  - 失败指令配可晋级候选的窄窗口:候选已注册、`executed` 写入失败,下一轮续训预算又不让续,指令被判 `failed` 而候选仍是 `pending_review`,三个状态互相矛盾,审核人看不到提示。候选本身是完整训练的产物,所以可以接受;更好的做法是升级为 `failed` 时检查候选是否已存在并写进 `failure.reason`。
  - 入口不校验 `repo_root`(不存在或非 git 目录静默退出码 0)、`--interval-s` 为负数是回溯、全程零输出,派发了什么、哪条指令 `failed` 都不打印;cli 的 `status` 只列 `pending_review`,所以 `failed` 的指令对人不可见。
  - 指令必须晚于它点名的数据集入树:先入树的指令被判 `failed` 且不会复活。
  - 瞬态异常会让 `run_daemon` 退出(已有)。最小修复二选一:`wire.run` 外包一层重启;或在 `reconcile_once` 里按动作隔离,加一个 keyword-only 的 `on_error` 钩子(默认 `None`,原样上抛)。换成真实执行器、长训练之后,它会从已知限制升级为需要修的缺陷。
  - 派发在一个 tick 里是同步的:真实执行器长训练期间,审批与晋级动作会被饿住。
  - 默认桩里写死的 `"tom"` 仍然保留。
- **两处测试缺口(整支评审修复波的变异检查发现,已在 `572f5e7` 补齐)**:读清单那段 `try` 的异常元组刻意不含 `OSError`(瞬态错误要原样上抛、指令保持 `pending`),先前没有测试钉住 —— 把它放宽成接一切 `Exception`,相关 56 个用例全过;确定性失败的分类里,`NeedsHumanError` 记 `T3` 有断言,其余成员记 `T2` 没有 —— 把分类改成一律 `T3`,同样全过。两处都是代码正确、测试没钉住相邻行为。补法:新增 `test_a_transient_error_while_reading_a_manifest_leaves_the_directive_pending`(monkeypatch `mindtrainer.dispatch.read_manifest` 抛 `OSError`,断言异常原样逃出 `reconcile_once`、指令仍 `pending`);`test_every_deterministic_orchestrator_failure_fails_the_directive` 加断言 `failure["category"]`(`NeedsHumanError` 为 `T3`,其余为 `T2`)。两处变异复查均被新测试抓住;全量 820 passed。
