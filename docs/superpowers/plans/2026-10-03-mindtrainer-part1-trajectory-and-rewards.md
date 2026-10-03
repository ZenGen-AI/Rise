# MindTrainer(上):包骨架、客户端增量、训练配置、轨迹源、奖励管线、分组 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 MindTrainer 的上半段(设计文档 §2–§5.1):`src/mindtrainer` 包骨架与打包防回归、Plan 4 客户端的两处增量(`turns`、`abort()`)、训练配置与校验、两种轨迹源(BatchTrain 读带 sha256 清单的数据集;InteractTrain 作为客户端驱动 Plan 4 的实时环境服务)、奖励管线(规则归因 + judge 归因 + 校验 + 组合)、GRPO 分组与优势。产出的接口供下半段(执行器、checkpoint、续训、编排)直接消费。

**Architecture:** 一模块一文件,与 `src/diagnostic` 同构。下游(奖励管线、分组)只依赖 `TrajectorySource` 协议产出的 `TrajectoryBatch`,不关心轨迹来源——这是设计的核心原则,由"同一套奖励管线吃两种轨迹源"的测试直接验证。逻辑核心全部是纯函数或可 mock 的协议,不需要 GPU、ROLL、外部网络;Interact 的测试用 Plan 4 的真实 loopback gRPC 服务端(`127.0.0.1`,不算外部网络)。

**Tech Stack:** Python 3.11+ / uv / pytest;标准库 `tomllib` 用于打包防回归测试;**无新增第三方依赖**。

**Spec:**
- `docs/superpowers/specs/2026-10-03-mindtrainer-design.md`(本计划实现其 §2–§5.1,以及 §7.3 的打包防回归部分)
- 背景:`docs/superpowers/specs/2026-09-24-rismforge-subsystem-design.md` §3、§5.1、补充 D.2、补充 E

**依赖的既有实现(均已在 `main`,基线提交 `275781b`)**
- `controlplane.envelope`:`Envelope`、`OwnerRef`、`BigRef(uri, sha256, size_bytes, fetch_hint=None)`,均带 `to_dict`/`from_dict`。
- `controlplane.gitstore.GitStore(repo_root)`:`.init()`、`.write_product(rel, env, message)`(逐字节相同则空操作)、`.read_product(rel)`(经迁移器链)、`.list_products(rel_dir)`(只扫 `*.yaml`,**不递归**)、`.exists(rel)`。
- `controlplane.paths`:`run_dir(run_id)`、`episode_file`、`dataset_manifest_file(run_id, name)` 等。
- `controlplane.daemon.read_tree_state(store)`:读候选信封的 `id`/`status`/`run_id`。
- `simulator.datamodel`:`Persona`、`Scenario`、`RoleSlot`、`RoleBinding(slot_id, persona_id, model_backend, model_ref=None)`、`RoleAssignment(scenario_id, bindings, max_turns)`、`TurnRecord`、`Episode`。
- `simulator.backends.ModelBackend` 协议:`respond(persona, scenario, history, speaker_slot) -> (utterance, inner_thought)`;`ScriptedBackend`。
- `simulator.store_adapter`:`write_episode`、`read_episode`、`read_scenario`、`read_persona`、`write_scenario`、`write_persona`。
- `simulator.dataset`:`DatasetManifest`、`verify_manifest(store, manifest) -> list[Episode]`、`episode_group_id(episode)`、`DatasetIntegrityError`。
- `simulator.realtime.client.EnvironmentServiceClient` 与 `simulator.realtime.server.create_server`(Plan 4)。

---

## Global Constraints

每个任务的要求都隐含包含本节。

- **MindTrainer 不导入 `diagnostic.*`**(设计 §0 边界 1)。OPD 是否失效由诊断侧判定,MindTrainer 只如实记录,不读诊断结果。
- **复用 `simulator.datamodel`、`simulator.store_adapter`、`simulator.dataset` 是允许的**(设计 §0 边界 2):受限的是子系统间的运行时调用,不是共享数据结构。`Episode`/`TurnRecord`/`RoleBinding` 直接复用,不另造轨迹模型。
- **脱离真实环境可测**:不需要 GPU、ROLL、外部网络。全部测试用 `tmp_path` 隔离。
- **读产物走 `GitStore.read_product`**(内部经 `migration.load_product`),禁止直接反序列化;写产物用 `Envelope` + `GitStore.write_product`。
- **纯函数优先**:校验、分组、归因规则、组合全部是纯函数;有副作用的只有轨迹源(读 git / 调 gRPC)。
- **确定性失败显式报错,不静默跳过**:数据集哈希不符、组太小、归因不合法、配置非法,一律抛带原因的异常。
- **失败是数据**(补充 E.1):Interact 的失败尝试记入 `metadata["failed_attempts"]`,不丢。
- **凭证绝不进 git 树**(补充 G.3):judge 配置只存 `judge_model_id` 与 `rubric_version`,不含任何凭证字段。
- **无新增第三方依赖**。
- **命名**:一模块一文件,测试文件 `tests/test_mt_*.py`(客户端增量的测试沿用 `tests/test_realtime_*.py`);轨道/阶段/模式用小写下划线标识符:模式 `"batch"|"interact"`,阶段 `"sft"|"grpo_opd"`,归因 `"outcome_split"|"judge"`,组合 `"sum"|"product"`。
- 每个源文件以 `from __future__ import annotations` 开头。
- 提交信息结尾空一行后加:`Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`。
- **测试基线**:`main` 上 `uv run --extra dev pytest -q` 为 323 通过、无警告;每个任务后应递增,不得出现警告。

---

## File Structure

**新增 `src/mindtrainer/`**
- `__init__.py` — 包标识(任务 1)
- `training_config.py` — `RewardConfig`、`TrainingConfig`、`validate_training_config`、`config_hash`、`phase_at`、`load_config_from_directive`(任务 3)
- `datamodel.py` — `TrajectoryBatch`、`RewardedTrajectory`(任务 4;下半段会往里加 `StepResult`、`TrainingRun`)
- `trajectory_source.py` — `TrajectorySource` 协议、`batch_for_step`、`BatchTrajectorySource`(任务 5);`RolloutFailedError`、`InteractTrajectorySource`(任务 6)
- `reward_pipeline.py` — 归因协议、`OutcomeSplitAttribution`、`MockTurnJudge`、`JudgeAttribution`(含自评偏差守卫)、`build_attribution_backend`(任务 7);`validate_attribution`、`combine`、`run_reward_pipeline`、`summarize_rewards`(任务 8)
- `grouping.py` — `group_by_id`、`group_advantages`、`trajectory_advantages`、`count_degenerate_groups`、`validate_groups`、`DegenerateGroupError`(任务 9)

**修改既有**
- `pyproject.toml` — wheel 包列表加 `src/mindtrainer`(任务 1)
- `src/controlplane/paths.py` — 加 `training_run_file`、`candidate_checkpoint_file`(任务 1)
- `src/simulator/realtime/client.py` — 加 `turns` 属性与 `abort()`(任务 2)

**新增测试**
- `tests/test_mt_packaging.py`、`tests/test_mt_paths.py`(任务 1)
- `tests/test_realtime_client_abort.py`(任务 2)
- `tests/test_mt_config.py`(任务 3)
- `tests/test_mt_datamodel.py`(任务 4)
- `tests/test_mt_trajectory_batch.py`(任务 5)、`tests/test_mt_trajectory_interact.py`(任务 6)
- `tests/test_mt_reward_pipeline.py`(任务 7、8)
- `tests/test_mt_grouping.py`(任务 9)

---

### Task 1: 包骨架、打包防回归、路径助手

**Files:**
- Create: `src/mindtrainer/__init__.py`
- Modify: `pyproject.toml`(wheel 包列表)
- Modify: `src/controlplane/paths.py`(末尾追加两个函数)
- Test: `tests/test_mt_packaging.py`、`tests/test_mt_paths.py`

**Interfaces:**
- Consumes: 无。
- Produces:
  - 包 `mindtrainer` 可导入且被打进 wheel。
  - `controlplane.paths.training_run_file(run_id: str) -> str` = `f"{run_dir(run_id)}/training_run.yaml"`。
  - `controlplane.paths.candidate_checkpoint_file(run_id: str) -> str` = `f"{run_dir(run_id)}/candidate_checkpoint.yaml"`。

**背景:** Plan 3 新增顶层包 `src/diagnostic` 时漏登记 wheel 包列表,那次修复只改了一行 `pyproject.toml`、没留任何测试。这里先加防回归测试,再建包,让测试先红后绿。两个路径助手都放 run 目录**顶层**——`GitStore.list_products` 不递归,`rismforge trace` 靠它扫描,放进子目录就看不见了。

- [ ] **Step 1: 写打包防回归测试**

`tests/test_mt_packaging.py`:

```python
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _listed() -> set[str]:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"])


def _on_disk() -> set[str]:
    return {
        f"src/{p.name}"
        for p in (ROOT / "src").iterdir()
        if p.is_dir() and (p / "__init__.py").exists()
    }


def test_every_top_level_src_package_is_in_the_wheel_list():
    missing = sorted(_on_disk() - _listed())
    assert not missing, f"missing from [tool.hatch...wheel].packages: {missing}"


def test_wheel_list_has_no_stale_entries():
    stale = sorted(_listed() - _on_disk())
    assert not stale, f"listed but not a package on disk: {stale}"
```

- [ ] **Step 2: 运行,确认当前为绿(此时还没有 `src/mindtrainer`)**

Run: `uv run --extra dev pytest tests/test_mt_packaging.py -v`
Expected: 2 passed。

- [ ] **Step 3: 建包,让测试变红**

`src/mindtrainer/__init__.py`:

```python
"""MindTrainer:权重级训练引擎(BatchTrain + InteractTrain)。

模块边界见 docs/superpowers/specs/2026-10-03-mindtrainer-design.md。
本包不导入 diagnostic.*。
"""
```

Run: `uv run --extra dev pytest tests/test_mt_packaging.py -v`
Expected: `test_every_top_level_src_package_is_in_the_wheel_list` FAIL,信息含 `['src/mindtrainer']`。

- [ ] **Step 4: 登记包,变绿**

把 `pyproject.toml` 里这一行

```toml
packages = ["src/controlplane", "src/simulator", "src/diagnostic"]
```

改成

```toml
packages = ["src/controlplane", "src/simulator", "src/diagnostic", "src/mindtrainer"]
```

Run: `uv run --extra dev pytest tests/test_mt_packaging.py -v`
Expected: 2 passed。

- [ ] **Step 5: 写路径助手测试**

`tests/test_mt_paths.py`:

```python
from controlplane.daemon import read_tree_state
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import (
    candidate_checkpoint_file, run_dir, training_run_file,
)


def _env(id_, type_, status):
    return Envelope(
        id=id_, type=type_, version=1, created_at="2026-10-03T00:00:00Z",
        created_by=OwnerRef(), status=status, schema_version=1,
        links=["run-1"], payload={},
    )


def test_training_run_file_is_at_the_top_level_of_the_run_dir():
    path = training_run_file("run-1")
    assert path == "control-plane/runs/run-1/training_run.yaml"
    # 顶层 = 比 run_dir 多恰好一段;list_products 不递归,放深了 trace 就看不到
    assert path.count("/") == run_dir("run-1").count("/") + 1


def test_candidate_checkpoint_file_matches_the_path_the_daemon_reads(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    store.write_product(
        candidate_checkpoint_file("run-1"),
        _env("cand-run-1", "candidate_checkpoint", "pending_review"),
        "seed candidate",
    )
    views = read_tree_state(store).candidates
    assert [(c.id, c.run_id, c.status) for c in views] == [
        ("cand-run-1", "run-1", "pending_review")
    ]


def test_training_run_is_listed_by_list_products(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    store.write_product(
        training_run_file("run-1"),
        _env("training-run-1", "training_run", "pending"),
        "seed training run",
    )
    assert training_run_file("run-1") in store.list_products(run_dir("run-1"))
```

- [ ] **Step 6: 运行,确认失败**

Run: `uv run --extra dev pytest tests/test_mt_paths.py -v`
Expected: FAIL,`ImportError: cannot import name 'candidate_checkpoint_file'`(收集阶段错误)。

- [ ] **Step 7: 实现路径助手**

在 `src/controlplane/paths.py` 末尾追加:

```python


def training_run_file(run_id: str) -> str:
    """训练运行记录。放 run 目录顶层——GitStore.list_products 不递归,
    rismforge trace 靠它扫描,放进子目录就看不到了。"""
    return f"{run_dir(run_id)}/training_run.yaml"


def candidate_checkpoint_file(run_id: str) -> str:
    """候选 checkpoint 产物。与 controlplane.daemon 读取的路径一致。"""
    return f"{run_dir(run_id)}/candidate_checkpoint.yaml"
```

- [ ] **Step 8: 运行,确认通过 + 全量回归**

Run: `uv run --extra dev pytest tests/test_mt_paths.py tests/test_mt_packaging.py -v`
Expected: 5 passed。

Run: `uv run --extra dev pytest -q`
Expected: 328 passed,无警告。

- [ ] **Step 9: 提交**

```bash
git add src/mindtrainer/__init__.py pyproject.toml src/controlplane/paths.py tests/test_mt_packaging.py tests/test_mt_paths.py
git commit -m "feat: mindtrainer package skeleton, wheel-list guard test, run-level path helpers

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: 实时客户端的两处增量:`turns` 与 `abort()`

**Files:**
- Modify: `src/simulator/realtime/client.py`(在 `opening_turns` 属性之后加 `turns`;在 `end_session` 方法之后加 `abort`)
- Test: `tests/test_realtime_client_abort.py`

**Interfaces:**
- Consumes: Plan 4 的 `EnvironmentServiceClient`(`_turns`、`_abandon`、`closed`、`create_session`、`submit_turn`、`end_session`、`drain_until_end`)与 `create_server`。
- Produces:
  - `EnvironmentServiceClient.turns -> list[TurnRecord]`:目前已收到的完整历史(含客户端自己轮次的回显)的**深拷贝**。策略(`ModelBackend`)据此决定下一句。
  - `EnvironmentServiceClient.abort() -> None`:放弃会话,**不发 `EndSession`**,置 `closed`,释放请求流。可重复调用;会话已结束时是空操作,不改写已有的终止原因。

**为什么需要 `abort()`:** 策略自己抛异常时,不能走 `end_session()`——服务端会把这条半截 episode 当作正常结束,经 `on_session_closed` 写进 ControlPlane,污染训练数据。直接放弃(请求流结束而没有 `EndSession`)时,服务端按断连判 `FAILED`,不落盘。这条语义我已对着真实服务端验证过:放弃后落盘钩子没有被触发,正常 `end_session` 则触发。

**为什么 `turns` 要深拷贝:** `TurnRecord` 是可变数据类。浅拷贝列表时策略若改了某个 `TurnRecord`,会直接改坏客户端自己重建的 Episode。

- [ ] **Step 1: 写失败测试**

`tests/test_realtime_client_abort.py`:

```python
import threading
import time

import grpc
import pytest

from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleAssignment, RoleBinding
from simulator.realtime.client import EnvironmentServiceClient
from simulator.realtime.proto import realtime_env_pb2 as pb2
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server


def _assignment(max_turns=100):
    return RoleAssignment(
        "s1",
        [RoleBinding("a", "pa", "self_play"),
         RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")],
        max_turns=max_turns,
    )


def _personas():
    return {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}


def _factory(scenario, assignment, personas, client_slot):
    others = assignment.slot_ids() - {client_slot}
    return {s: ScriptedBackend({s: ["opp1", "opp2", "opp3", "opp4"]}) for s in others}


@pytest.fixture
def env():
    closed: list[str] = []
    server = create_server(
        _factory, port=0, turn_timeout_s=2.0,
        on_session_closed=lambda ep: closed.append(ep.id),
    )
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
    yield pb2_grpc.EnvironmentServiceStub(channel), closed
    channel.close()
    server.stop(0)


def _open(stub, max_turns=100):
    client = EnvironmentServiceClient(stub)
    client.create_session(
        scenario_id="s1", role_assignment=_assignment(max_turns),
        personas=_personas(), purpose="training", client_driven_slot="a",
    )
    return client


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------- turns ----------------

def test_turns_holds_own_echo_and_opponent_replies_in_order(env):
    stub, _ = env
    client = _open(stub)
    assert client.turns == []
    client.submit_turn("hello")
    assert [(t.speaker_slot, t.utterance) for t in client.turns] == [
        ("a", "hello"), ("b", "opp1"),
    ]
    client.abort()


def test_turns_is_a_deep_copy_so_a_policy_cannot_corrupt_the_clients_state(env):
    stub, _ = env
    client = _open(stub)
    client.submit_turn("hello")
    snapshot = client.turns
    snapshot[0].utterance = "TAMPERED"
    snapshot.clear()
    assert [t.utterance for t in client.turns] == ["hello", "opp1"]
    client.abort()


# ---------------- abort ----------------

def test_abort_closes_the_client_and_blocks_further_submits(env):
    stub, _ = env
    client = _open(stub)
    client.abort()
    assert client.closed
    with pytest.raises(RuntimeError):
        client.submit_turn("too late")


def test_abort_is_idempotent(env):
    stub, _ = env
    client = _open(stub)
    client.abort()
    client.abort()
    assert client.closed


def test_abort_after_a_normal_end_does_not_rewrite_the_reason(env):
    stub, _ = env
    client = _open(stub, max_turns=2)
    client.submit_turn("hi")  # a, b => 到达 max_turns,服务端发 EndSession
    assert client.closed
    client.abort()
    assert client.drain_until_end().terminated_reason == "max_turns"


def test_aborted_session_is_never_persisted_but_an_ended_one_is(env):
    stub, closed = env
    aborted = _open(stub)
    aborted.submit_turn("half-finished")
    aborted.abort()

    ended = _open(stub)
    ended.submit_turn("complete")
    ended_episode = ended.end_session()

    # 正向对照:落盘钩子确实会触发,所以下面"被放弃的那条没进来"不是空转
    assert _wait_until(lambda: ended_episode.id in closed)
    time.sleep(0.3)  # 给"错误地落盘"留出显形的时间
    assert closed == [ended_episode.id]


# ---------------- abort 释放请求流(不泄漏后台线程) ----------------

class _FakeStub:
    def __init__(self, script):
        self._script = list(script)
        self.request_gen = None

    def Interact(self, request_gen):
        self.request_gen = request_gen
        return iter(self._script)


def test_abort_releases_the_request_stream():
    ready = pb2.ServerMessage(session_ready=pb2.SessionReady(session_id="sess-fake"))
    stub = _FakeStub([ready])
    client = EnvironmentServiceClient(stub)
    client.create_session(
        scenario_id="s1", role_assignment=_assignment(), personas=_personas(),
        purpose="training", client_driven_slot="a",
    )
    client.abort()

    drained: list = []
    worker = threading.Thread(target=lambda: drained.extend(stub.request_gen), daemon=True)
    worker.start()
    worker.join(1.0)
    assert not worker.is_alive(), "请求生成器仍阻塞:abort() 没有释放请求流"
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run --extra dev pytest tests/test_realtime_client_abort.py -v`
Expected: 各用例 FAIL,`AttributeError: 'EnvironmentServiceClient' object has no attribute 'turns'`(或 `abort`)。

- [ ] **Step 3: 实现**

在 `src/simulator/realtime/client.py` 顶部 import 区加一行 `from copy import deepcopy`(放在 `import queue` 之后):

```python
import queue
from copy import deepcopy
```

在 `opening_turns` 属性之后加:

```python
    @property
    def turns(self) -> list[TurnRecord]:
        """目前已收到的完整历史(含客户端自己轮次的回显)的深拷贝。

        策略据此决定下一句。必须是深拷贝:TurnRecord 是可变数据类,策略若改了
        某条记录,会直接改坏客户端自己重建的 Episode。"""
        return deepcopy(self._turns)
```

在 `end_session` 方法之后加:

```python
    def abort(self) -> None:
        """放弃会话:**不发 EndSession**,置 closed 并释放请求流。

        服务端看到请求流结束而没有 EndSession,按客户端断连判 FAILED,
        不会经 on_session_closed 落盘。策略抛异常时必须走它——若改发
        end_session(),服务端会把这条半截 episode 当作正常结束并写进
        ControlPlane,污染训练数据。可重复调用;会话已结束时是空操作,
        不改写已有的终止原因。放弃之后不要使用 drain_until_end() 的结果。"""
        self._abandon("client_aborted")
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run --extra dev pytest tests/test_realtime_client_abort.py -v`
Expected: 7 passed。

- [ ] **Step 5: 全量回归(Plan 4 既有客户端测试不得受影响)**

Run: `uv run --extra dev pytest -q`
Expected: 335 passed,无警告。

- [ ] **Step 6: 提交**

```bash
git add src/simulator/realtime/client.py tests/test_realtime_client_abort.py
git commit -m "feat: realtime client turns (deep copy) and abort() that never persists a half episode

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: 训练配置与校验

**Files:**
- Create: `src/mindtrainer/training_config.py`
- Test: `tests/test_mt_config.py`

**Interfaces:**
- Consumes: `controlplane.envelope.BigRef`、`Envelope`;`simulator.datamodel.RoleBinding`。
- Produces(任务 5–10 与下半段依赖这些名字):
  - `RewardConfig`(frozen dataclass):`components, attribution, combination, outcome_rules, gamma, judge_model_id, rubric_version`;`to_dict()` / `from_dict(d)`。
  - `TrainingConfig`(frozen dataclass):字段见下方实现;`to_dict()` / `from_dict(d)`。可选键缺省值:`max_retries_per_episode=2, min_group_size=2, max_resume_attempts=3`。
  - `InvalidTrainingConfigError(ValueError)`。
  - `validate_training_config(cfg) -> str | None`:返回第一条违规的可读原因,合法返回 `None`。纯函数。
  - `ensure_valid_config(cfg) -> TrainingConfig`:合法则原样返回,否则抛 `InvalidTrainingConfigError`。
  - `config_hash(cfg) -> str`:规范化 JSON 的 sha256(64 位十六进制)。
  - `total_steps(cfg) -> int`、`phase_at(step, cfg) -> "sft" | "grpo_opd"`(越界抛 `ValueError`)。
  - `load_config_from_directive(env: Envelope) -> TrainingConfig`:读 `payload["training_config"]`,缺键或缺字段抛 `InvalidTrainingConfigError`。

**设计要点(为什么这样校验):**
- **`groups_per_step <= len(scenario_ids)`**:同一 scenario 在一步里出现两次,两批 rollout 会因 `group_id` 相同被并成一组,组内基线就混了两批样本。
- **非训练角色位必须带非空 `model_ref`**:事后追查 reward hacking 要靠它,不记对手是谁就无法判断是不是练偏了。
- **`min_group_size >= 2`**:一组只有 1 条时优势恒为 0,训练会静默地什么都学不到。
- **`teacher_model_ref` / `base_checkpoint_ref` 必须是 64 位小写十六进制 sha256 的 `BigRef`**:spec §5.1 要求精确锚定,字符串模型名不够。
- **`gamma` 的判断写成 `not (0 < gamma <= 1)`**:NaN 会自然落进拒绝分支。
- **校验顺序固定**:先通用字段,再 stage 清单,再奖励,最后 interact 专用。测试里每条违规用例各自只触发一条规则,所以顺序不会让用例互相遮蔽。
- 配置是 frozen dataclass,`to_dict` 对嵌套可变结构深拷贝,防止别名修改。

- [ ] **Step 1: 写失败测试**

`tests/test_mt_config.py`:

```python
import dataclasses
import json

import pytest
import yaml

from controlplane.envelope import BigRef, Envelope, OwnerRef
from mindtrainer.training_config import (
    InvalidTrainingConfigError, RewardConfig, TrainingConfig, config_hash,
    ensure_valid_config, load_config_from_directive, phase_at, total_steps,
    validate_training_config,
)
from simulator.datamodel import RoleBinding


def _ref(ch: str) -> BigRef:
    return BigRef(uri=f"file:///ckpt/{ch}", sha256=ch * 64, size_bytes=1)


def _reward(**kw) -> RewardConfig:
    base = dict(
        components=["goal", "norm"], attribution="outcome_split", combination="sum",
        outcome_rules={"goal": ["agreed"], "norm": ["please"]}, gamma=0.9,
    )
    base.update(kw)
    return RewardConfig(**base)


def _judge_reward(**kw) -> RewardConfig:
    base = dict(
        components=["goal"], attribution="judge", combination="product",
        judge_model_id="external-judge", rubric_version="rubric-v1",
    )
    base.update(kw)
    return RewardConfig(**base)


def _trained() -> RoleBinding:
    return RoleBinding("a", "pa", "self_play")


def _opponent() -> RoleBinding:
    return RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")


def _interact(**kw) -> TrainingConfig:
    base = dict(
        mode="interact", trained_slot="a",
        base_checkpoint_ref=_ref("a"), teacher_model_ref=_ref("b"),
        reward=_reward(),
        sft_steps=0, grpo_steps=4, sft_batch_size=2, groups_per_step=2, group_size=3,
        grpo_config={"learning_rate": 1e-5, "clip": {"low": 0.8, "high": 1.2}},
        opd_coefficient=0.1, checkpoint_every_steps=2, allow_resume=True,
        scenario_ids=["s1", "s2"], bindings=[_trained(), _opponent()],
        max_turns_per_episode=6,
    )
    base.update(kw)
    return TrainingConfig(**base)


def _batch(**kw) -> TrainingConfig:
    base = dict(
        mode="batch", trained_slot="a",
        base_checkpoint_ref=_ref("a"), teacher_model_ref=_ref("b"),
        reward=_reward(),
        sft_steps=2, grpo_steps=2, sft_batch_size=2, groups_per_step=2, group_size=3,
        grpo_config={"learning_rate": 1e-5}, opd_coefficient=0.1,
        checkpoint_every_steps=2, allow_resume=True,
        stage_manifest_refs={"sft": "dsm-run-1-sft", "grpo_opd": "dsm-run-1-grpo_opd"},
    )
    base.update(kw)
    return TrainingConfig(**base)


# ---------------- 合法配置 ----------------

def test_valid_interact_and_batch_configs_pass():
    assert validate_training_config(_interact()) is None
    assert validate_training_config(_batch()) is None


def test_judge_reward_with_model_and_rubric_passes():
    assert validate_training_config(_interact(reward=_judge_reward())) is None


def test_batch_mode_ignores_the_interact_only_fields():
    cfg = _batch(scenario_ids=[], bindings=[], max_turns_per_episode=0)
    assert validate_training_config(cfg) is None


def test_sft_only_batch_needs_no_grpo_manifest():
    cfg = _batch(sft_steps=3, grpo_steps=0, stage_manifest_refs={"sft": "dsm-run-1-sft"})
    assert validate_training_config(cfg) is None


# ---------------- 违规配置:每条一个独立用例 ----------------

_a_frozen = RoleBinding("a", "pa", "frozen_reference", model_ref="x")

INVALID = [
    ("bad_mode", lambda: _interact(mode="online"), "mode must be one of"),
    ("empty_trained_slot", lambda: _interact(trained_slot=""), "trained_slot must be non-empty"),
    ("negative_sft", lambda: _interact(sft_steps=-1), "sft_steps must be >= 0"),
    ("negative_grpo", lambda: _interact(grpo_steps=-1), "grpo_steps must be >= 0"),
    ("no_steps", lambda: _interact(sft_steps=0, grpo_steps=0), "at least one training step"),
    ("sft_batch_zero", lambda: _interact(sft_batch_size=0), "sft_batch_size must be >= 1"),
    ("groups_per_step_zero", lambda: _interact(groups_per_step=0), "groups_per_step must be >= 1"),
    ("min_group_size_one", lambda: _interact(min_group_size=1), "min_group_size must be >= 2"),
    ("group_below_min", lambda: _interact(group_size=2, min_group_size=3), "is below min_group_size"),
    ("checkpoint_every_zero", lambda: _interact(checkpoint_every_steps=0), "checkpoint_every_steps must be >= 1"),
    ("opd_negative", lambda: _interact(opd_coefficient=-0.1), "opd_coefficient must be"),
    ("opd_nan", lambda: _interact(opd_coefficient=float("nan")), "opd_coefficient must be"),
    ("opd_inf", lambda: _interact(opd_coefficient=float("inf")), "opd_coefficient must be"),
    ("negative_retries", lambda: _interact(max_retries_per_episode=-1), "max_retries_per_episode must be >= 0"),
    ("negative_resume_attempts", lambda: _interact(max_resume_attempts=-1), "max_resume_attempts must be >= 0"),
    ("base_sha_not_hex", lambda: _interact(base_checkpoint_ref=BigRef("u", "xyz", 1)), "base_checkpoint_ref.sha256"),
    ("teacher_sha_uppercase", lambda: _interact(teacher_model_ref=BigRef("u", "A" * 64, 1)), "teacher_model_ref.sha256"),
    ("unknown_stage", lambda: _interact(stage_manifest_refs={"sftt": "x"}), "unknown stage"),
    ("sft_without_manifest", lambda: _interact(sft_steps=2, stage_manifest_refs={}), "stage_manifest_refs['sft']"),
    ("batch_grpo_without_manifest", lambda: _batch(stage_manifest_refs={"sft": "m"}), "stage_manifest_refs['grpo_opd']"),
    # interact 专用
    ("no_scenarios", lambda: _interact(scenario_ids=[]), "scenario_ids must be non-empty and unique"),
    ("dup_scenarios", lambda: _interact(scenario_ids=["s1", "s1"]), "scenario_ids must be non-empty and unique"),
    ("groups_exceed_scenarios", lambda: _interact(groups_per_step=3), "exceeds number of scenario_ids"),
    ("max_turns_zero", lambda: _interact(max_turns_per_episode=0), "max_turns_per_episode must be >= 1"),
    ("no_bindings", lambda: _interact(bindings=[]), "bindings must be non-empty"),
    ("dup_slot", lambda: _interact(bindings=[_trained(), _trained()]), "duplicate slot_id"),
    ("trained_not_bound", lambda: _interact(trained_slot="ghost"), "is not among bound slots"),
    ("trained_not_self_play", lambda: _interact(bindings=[_a_frozen, _opponent()]), "must be self_play"),
    ("trained_has_ref", lambda: _interact(bindings=[RoleBinding("a", "pa", "self_play", model_ref="x"), _opponent()]), "must not carry model_ref"),
    ("opp_self_play", lambda: _interact(bindings=[_trained(), RoleBinding("b", "pb", "self_play")]), "must not be self_play"),
    ("opp_no_ref", lambda: _interact(bindings=[_trained(), RoleBinding("b", "pb", "frozen_reference")]), "needs a non-empty model_ref"),
    ("opp_empty_ref", lambda: _interact(bindings=[_trained(), RoleBinding("b", "pb", "frozen_reference", model_ref="")]), "needs a non-empty model_ref"),
    ("opp_unknown_backend", lambda: _interact(bindings=[_trained(), RoleBinding("b", "pb", "magic", model_ref="x")]), "frozen_reference or external_api"),
    # 奖励
    ("no_components", lambda: _interact(reward=_reward(components=[], outcome_rules={})), "reward.components must be non-empty and unique"),
    ("dup_components", lambda: _interact(reward=_reward(components=["goal", "goal"])), "reward.components must be non-empty and unique"),
    ("bad_attribution", lambda: _interact(reward=_reward(attribution="vibes")), "reward.attribution must be one of"),
    ("bad_combination", lambda: _interact(reward=_reward(combination="avg")), "reward.combination must be one of"),
    ("rules_missing_component", lambda: _interact(reward=_reward(outcome_rules={"goal": ["x"]})), "keys must equal components"),
    ("rules_extra_component", lambda: _interact(reward=_reward(outcome_rules={"goal": ["x"], "norm": ["y"], "extra": ["z"]})), "keys must equal components"),
    ("rules_empty_keywords", lambda: _interact(reward=_reward(outcome_rules={"goal": [], "norm": ["y"]})), "needs a non-empty keyword list"),
    ("rules_blank_keyword", lambda: _interact(reward=_reward(outcome_rules={"goal": [""], "norm": ["y"]})), "needs a non-empty keyword list"),
    ("gamma_zero", lambda: _interact(reward=_reward(gamma=0.0)), "reward.gamma must be in (0, 1]"),
    ("gamma_over_one", lambda: _interact(reward=_reward(gamma=1.5)), "reward.gamma must be in (0, 1]"),
    ("gamma_nan", lambda: _interact(reward=_reward(gamma=float("nan"))), "reward.gamma must be in (0, 1]"),
    ("judge_missing_model", lambda: _interact(reward=_judge_reward(judge_model_id=None)), "reward.judge_model_id is required"),
    ("judge_missing_rubric", lambda: _interact(reward=_judge_reward(rubric_version=None)), "reward.rubric_version is required"),
]


@pytest.mark.parametrize("build,needle", [(b, n) for _, b, n in INVALID], ids=[i for i, _, _ in INVALID])
def test_invalid_config_is_rejected_with_the_specific_reason(build, needle):
    reason = validate_training_config(build())
    assert reason is not None, "应当被拒绝"
    assert needle in reason, reason


def test_judge_fields_are_not_required_for_outcome_split():
    assert validate_training_config(_interact(reward=_reward(judge_model_id=None, rubric_version=None))) is None


def test_ensure_valid_config_returns_the_config_or_raises_with_the_reason():
    cfg = _interact()
    assert ensure_valid_config(cfg) is cfg
    with pytest.raises(InvalidTrainingConfigError, match="trained_slot 'ghost'"):
        ensure_valid_config(_interact(trained_slot="ghost"))


# ---------------- config_hash ----------------

def test_config_hash_is_64_hex_and_stable():
    h = config_hash(_interact())
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    assert h == config_hash(_interact())


def test_config_hash_ignores_dict_key_order():
    one = _interact(grpo_config={"a": 1, "b": 2}, reward=_reward(outcome_rules={"goal": ["x"], "norm": ["y"]}))
    two = _interact(grpo_config={"b": 2, "a": 1}, reward=_reward(outcome_rules={"norm": ["y"], "goal": ["x"]}))
    assert config_hash(one) == config_hash(two)


@pytest.mark.parametrize(
    "changed",
    [
        lambda: _interact(opd_coefficient=0.2),
        lambda: _interact(bindings=[_trained(), RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v2")]),
        lambda: _interact(reward=_reward(gamma=0.8)),
        lambda: _interact(teacher_model_ref=_ref("c")),
        lambda: _interact(scenario_ids=["s2", "s1"]),
        lambda: _interact(stage_manifest_refs={"sft": "other"}),
    ],
    ids=["opd", "opponent_model_ref", "gamma", "teacher", "scenario_order", "manifests"],
)
def test_config_hash_changes_when_anything_that_matters_changes(changed):
    assert config_hash(changed()) != config_hash(_interact())


# ---------------- 阶段 ----------------

def test_phase_at_switches_exactly_at_sft_steps():
    cfg = _batch(sft_steps=2, grpo_steps=2)
    assert total_steps(cfg) == 4
    assert [phase_at(s, cfg) for s in range(4)] == ["sft", "sft", "grpo_opd", "grpo_opd"]


def test_phase_at_with_no_sft_starts_in_grpo():
    cfg = _interact(sft_steps=0, grpo_steps=3)
    assert [phase_at(s, cfg) for s in range(3)] == ["grpo_opd"] * 3


@pytest.mark.parametrize("step", [-1, 4])
def test_phase_at_rejects_out_of_range_steps(step):
    with pytest.raises(ValueError, match="out of range"):
        phase_at(step, _batch(sft_steps=2, grpo_steps=2))


# ---------------- 序列化 ----------------

@pytest.mark.parametrize("build", [_interact, _batch, lambda: _interact(reward=_judge_reward())])
def test_to_dict_round_trip(build):
    cfg = build()
    assert TrainingConfig.from_dict(cfg.to_dict()) == cfg


def test_to_dict_is_plain_data_that_survives_json_and_yaml():
    cfg = _interact()
    d = cfg.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert yaml.safe_load(yaml.safe_dump(d)) == d
    assert TrainingConfig.from_dict(yaml.safe_load(yaml.safe_dump(d))) == cfg


def test_optional_keys_fall_back_to_defaults():
    d = _interact().to_dict()
    for key in ("max_retries_per_episode", "min_group_size", "max_resume_attempts"):
        del d[key]
    cfg = TrainingConfig.from_dict(d)
    assert (cfg.max_retries_per_episode, cfg.min_group_size, cfg.max_resume_attempts) == (2, 2, 3)


def test_to_dict_does_not_alias_mutable_state():
    cfg = _interact()
    cfg.to_dict()["grpo_config"]["clip"]["low"] = 999
    assert cfg.grpo_config["clip"]["low"] == 0.8


def test_config_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _interact().mode = "batch"


# ---------------- 从 directive 读取 ----------------

def _directive(payload):
    return Envelope(
        id="run-1", type="directive", version=1, created_at="2026-10-03T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1, payload=payload,
    )


def test_load_config_from_directive_reads_the_training_config_key():
    cfg = _interact()
    env = _directive({"target_dimension": "tom", "recipe_ref": "", "training_config": cfg.to_dict()})
    assert load_config_from_directive(env) == cfg


def test_directive_without_training_config_is_rejected():
    with pytest.raises(InvalidTrainingConfigError, match="training_config"):
        load_config_from_directive(_directive({"target_dimension": "tom"}))


def test_directive_with_a_missing_field_names_the_field():
    d = _interact().to_dict()
    del d["opd_coefficient"]
    with pytest.raises(InvalidTrainingConfigError, match="opd_coefficient"):
        load_config_from_directive(_directive({"training_config": d}))
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run --extra dev pytest tests/test_mt_config.py -q`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindtrainer.training_config'`。

- [ ] **Step 3: 写实现**

`src/mindtrainer/training_config.py`:

```python
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field

from controlplane.envelope import BigRef, Envelope
from simulator.datamodel import RoleBinding

MODES = ("batch", "interact")
STAGES = ("sft", "grpo_opd")
ATTRIBUTIONS = ("outcome_split", "judge")
COMBINATIONS = ("sum", "product")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class InvalidTrainingConfigError(ValueError):
    """训练配置缺字段或违反校验规则。"""


@dataclass(frozen=True)
class RewardConfig:
    """奖励配置(设计 §4)。不含任何凭证字段:judge 只存模型标识与 rubric 版本。"""

    components: list[str]
    attribution: str  # "outcome_split" | "judge"
    combination: str  # "sum" | "product"
    # outcome_split 专用:分量 -> 命中关键词列表
    outcome_rules: dict[str, list[str]] = field(default_factory=dict)
    gamma: float = 1.0  # outcome_split 专用:1.0 各训练轮同分;<1 越靠近结尾越高
    # judge 专用
    judge_model_id: str | None = None
    rubric_version: str | None = None  # judge 的 rubric prompt 版本,必填

    def to_dict(self) -> dict:
        return {
            "components": list(self.components),
            "attribution": self.attribution,
            "combination": self.combination,
            "outcome_rules": {k: list(v) for k, v in self.outcome_rules.items()},
            "gamma": self.gamma,
            "judge_model_id": self.judge_model_id,
            "rubric_version": self.rubric_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RewardConfig":
        return cls(
            components=list(d["components"]),
            attribution=d["attribution"],
            combination=d["combination"],
            outcome_rules={k: list(v) for k, v in d.get("outcome_rules", {}).items()},
            gamma=d.get("gamma", 1.0),
            judge_model_id=d.get("judge_model_id"),
            rubric_version=d.get("rubric_version"),
        )


@dataclass(frozen=True)
class TrainingConfig:
    """训练配置(设计 §2.1,spec §5.1 要求精确锚定的项)。内联在 directive payload 的
    "training_config" 键下;RecipeStore 落地后改成 recipe_ref 引用,结构不变。"""

    mode: str  # "batch" | "interact"
    trained_slot: str  # 被训练的角色位;两种模式都必须明确
    base_checkpoint_ref: BigRef  # 起点权重
    teacher_model_ref: BigRef  # OPD 教师;必须是带 sha256 的 BigRef,不是字符串
    reward: RewardConfig
    sft_steps: int  # 0 = 跳过冷启动
    grpo_steps: int
    sft_batch_size: int
    groups_per_step: int  # 每个 GRPO step 吃几个完整组
    group_size: int  # 每组样本数;interact 下即每个 scenario 连续 rollout 的条数
    grpo_config: dict  # 学习率等,透传给执行器
    opd_coefficient: float  # >= 0,spec §3.2 明确要求可调
    checkpoint_every_steps: int
    allow_resume: bool
    # 以下均有默认值
    stage_manifest_refs: dict[str, str] = field(default_factory=dict)  # {"sft": id, "grpo_opd": id}
    scenario_ids: list[str] = field(default_factory=list)  # interact 专用
    bindings: list[RoleBinding] = field(default_factory=list)  # interact 专用
    max_turns_per_episode: int = 0  # interact 专用
    max_retries_per_episode: int = 2  # interact 专用
    min_group_size: int = 2
    max_resume_attempts: int = 3

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "trained_slot": self.trained_slot,
            "base_checkpoint_ref": self.base_checkpoint_ref.to_dict(),
            "teacher_model_ref": self.teacher_model_ref.to_dict(),
            "reward": self.reward.to_dict(),
            "sft_steps": self.sft_steps,
            "grpo_steps": self.grpo_steps,
            "sft_batch_size": self.sft_batch_size,
            "groups_per_step": self.groups_per_step,
            "group_size": self.group_size,
            "grpo_config": copy.deepcopy(self.grpo_config),
            "opd_coefficient": self.opd_coefficient,
            "checkpoint_every_steps": self.checkpoint_every_steps,
            "allow_resume": self.allow_resume,
            "stage_manifest_refs": dict(self.stage_manifest_refs),
            "scenario_ids": list(self.scenario_ids),
            "bindings": [b.to_dict() for b in self.bindings],
            "max_turns_per_episode": self.max_turns_per_episode,
            "max_retries_per_episode": self.max_retries_per_episode,
            "min_group_size": self.min_group_size,
            "max_resume_attempts": self.max_resume_attempts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TrainingConfig":
        return cls(
            mode=d["mode"],
            trained_slot=d["trained_slot"],
            base_checkpoint_ref=BigRef.from_dict(d["base_checkpoint_ref"]),
            teacher_model_ref=BigRef.from_dict(d["teacher_model_ref"]),
            reward=RewardConfig.from_dict(d["reward"]),
            sft_steps=d["sft_steps"],
            grpo_steps=d["grpo_steps"],
            sft_batch_size=d["sft_batch_size"],
            groups_per_step=d["groups_per_step"],
            group_size=d["group_size"],
            grpo_config=copy.deepcopy(d["grpo_config"]),
            opd_coefficient=d["opd_coefficient"],
            checkpoint_every_steps=d["checkpoint_every_steps"],
            allow_resume=d["allow_resume"],
            stage_manifest_refs=dict(d.get("stage_manifest_refs", {})),
            scenario_ids=list(d.get("scenario_ids", [])),
            bindings=[RoleBinding.from_dict(b) for b in d.get("bindings", [])],
            max_turns_per_episode=d.get("max_turns_per_episode", 0),
            max_retries_per_episode=d.get("max_retries_per_episode", 2),
            min_group_size=d.get("min_group_size", 2),
            max_resume_attempts=d.get("max_resume_attempts", 3),
        )


def _validate_reward(r: RewardConfig) -> str | None:
    if not r.components or len(set(r.components)) != len(r.components) or not all(r.components):
        return "reward.components must be non-empty and unique"
    if r.attribution not in ATTRIBUTIONS:
        return f"reward.attribution must be one of {list(ATTRIBUTIONS)}, got {r.attribution!r}"
    if r.combination not in COMBINATIONS:
        return f"reward.combination must be one of {list(COMBINATIONS)}, got {r.combination!r}"
    if r.attribution == "outcome_split":
        if set(r.outcome_rules) != set(r.components):
            return (
                f"reward.outcome_rules keys must equal components "
                f"{sorted(r.components)}, got {sorted(r.outcome_rules)}"
            )
        for name, words in r.outcome_rules.items():
            if not words or not all(isinstance(w, str) and w for w in words):
                return f"reward.outcome_rules[{name!r}] needs a non-empty keyword list"
        if not (0 < r.gamma <= 1):  # NaN 也会落到这里
            return f"reward.gamma must be in (0, 1], got {r.gamma}"
    if r.attribution == "judge":
        if not r.judge_model_id:
            return "reward.judge_model_id is required for judge attribution"
        if not r.rubric_version:
            return "reward.rubric_version is required for judge attribution"
    return None


def _validate_interact(cfg: TrainingConfig) -> str | None:
    if not cfg.scenario_ids or len(set(cfg.scenario_ids)) != len(cfg.scenario_ids):
        return "scenario_ids must be non-empty and unique"
    if cfg.groups_per_step > len(cfg.scenario_ids):
        # 同一 scenario 在一步里出现两次,两批 rollout 会因 group_id 相同被并成一组
        return (
            f"groups_per_step {cfg.groups_per_step} exceeds number of scenario_ids "
            f"{len(cfg.scenario_ids)}"
        )
    if cfg.max_turns_per_episode < 1:
        return f"max_turns_per_episode must be >= 1, got {cfg.max_turns_per_episode}"
    if not cfg.bindings:
        return "bindings must be non-empty"
    slots = [b.slot_id for b in cfg.bindings]
    duplicated = sorted({s for s in slots if slots.count(s) > 1})
    if duplicated:
        return f"duplicate slot_id in bindings: {duplicated}"
    by_slot = {b.slot_id: b for b in cfg.bindings}
    trained = by_slot.get(cfg.trained_slot)
    if trained is None:
        return f"trained_slot {cfg.trained_slot!r} is not among bound slots {sorted(by_slot)}"
    if trained.model_backend != "self_play":
        return f"trained slot {cfg.trained_slot!r} must be self_play, got {trained.model_backend!r}"
    if trained.model_ref is not None:
        return f"trained slot {cfg.trained_slot!r} must not carry model_ref {trained.model_ref!r}"
    for b in cfg.bindings:
        if b.slot_id == cfg.trained_slot:
            continue
        if b.model_backend == "self_play":
            return f"non-trained slot {b.slot_id!r} must not be self_play"
        if b.model_backend not in ("frozen_reference", "external_api"):
            return (
                f"non-trained slot {b.slot_id!r} model_backend must be "
                f"frozen_reference or external_api, got {b.model_backend!r}"
            )
        if not b.model_ref:
            # 事后追查 reward hacking 要靠它:不记对手是谁,就无法判断是不是练偏了
            return f"non-trained slot {b.slot_id!r} needs a non-empty model_ref"
    return None


def validate_training_config(cfg: TrainingConfig) -> str | None:
    """返回第一条违规的可读原因;全部合法返回 None。纯函数。"""
    if cfg.mode not in MODES:
        return f"mode must be one of {list(MODES)}, got {cfg.mode!r}"
    if not cfg.trained_slot:
        return "trained_slot must be non-empty"
    if cfg.sft_steps < 0:
        return f"sft_steps must be >= 0, got {cfg.sft_steps}"
    if cfg.grpo_steps < 0:
        return f"grpo_steps must be >= 0, got {cfg.grpo_steps}"
    if cfg.sft_steps + cfg.grpo_steps < 1:
        return "need at least one training step (sft_steps + grpo_steps >= 1)"
    if cfg.sft_batch_size < 1:
        return f"sft_batch_size must be >= 1, got {cfg.sft_batch_size}"
    if cfg.groups_per_step < 1:
        return f"groups_per_step must be >= 1, got {cfg.groups_per_step}"
    if cfg.min_group_size < 2:
        # 一组只有 1 条时优势恒为 0,训练会静默地什么都学不到
        return f"min_group_size must be >= 2, got {cfg.min_group_size}"
    if cfg.group_size < cfg.min_group_size:
        return f"group_size {cfg.group_size} is below min_group_size {cfg.min_group_size}"
    if cfg.checkpoint_every_steps < 1:
        return f"checkpoint_every_steps must be >= 1, got {cfg.checkpoint_every_steps}"
    if not (math.isfinite(cfg.opd_coefficient) and cfg.opd_coefficient >= 0):
        return f"opd_coefficient must be a finite number >= 0, got {cfg.opd_coefficient}"
    if cfg.max_retries_per_episode < 0:
        return f"max_retries_per_episode must be >= 0, got {cfg.max_retries_per_episode}"
    if cfg.max_resume_attempts < 0:
        return f"max_resume_attempts must be >= 0, got {cfg.max_resume_attempts}"
    for name, ref in (
        ("base_checkpoint_ref", cfg.base_checkpoint_ref),
        ("teacher_model_ref", cfg.teacher_model_ref),
    ):
        if not _SHA256.match(ref.sha256):
            return f"{name}.sha256 must be 64 lowercase hex chars, got {ref.sha256!r}"
    unknown = sorted(set(cfg.stage_manifest_refs) - set(STAGES))
    if unknown:
        return f"stage_manifest_refs has unknown stage(s) {unknown}; allowed {list(STAGES)}"
    if cfg.sft_steps > 0 and not cfg.stage_manifest_refs.get("sft"):
        return "stage_manifest_refs['sft'] is required when sft_steps > 0"
    if cfg.mode == "batch" and cfg.grpo_steps > 0 and not cfg.stage_manifest_refs.get("grpo_opd"):
        return "stage_manifest_refs['grpo_opd'] is required for batch mode when grpo_steps > 0"
    reason = _validate_reward(cfg.reward)
    if reason is not None:
        return reason
    if cfg.mode == "interact":
        return _validate_interact(cfg)
    return None


def ensure_valid_config(cfg: TrainingConfig) -> TrainingConfig:
    reason = validate_training_config(cfg)
    if reason is not None:
        raise InvalidTrainingConfigError(reason)
    return cfg


def config_hash(cfg: TrainingConfig) -> str:
    """配置的 sha256:规范化 JSON(键排序、无空白),不依赖字典顺序或 YAML 排版。"""
    text = json.dumps(cfg.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def total_steps(cfg: TrainingConfig) -> int:
    return cfg.sft_steps + cfg.grpo_steps


def phase_at(step: int, cfg: TrainingConfig) -> str:
    """全局 step -> 阶段。阶段不另存,由它推出,避免两处不一致。
    sft_steps == 0 时从第一步起就是 grpo_opd。越界抛 ValueError,免得差一错误悄悄滑过。"""
    if not (0 <= step < total_steps(cfg)):
        raise ValueError(f"step {step} out of range [0, {total_steps(cfg)})")
    return "sft" if step < cfg.sft_steps else "grpo_opd"


def load_config_from_directive(env: Envelope) -> TrainingConfig:
    """从 directive 产物读出训练配置。缺键或缺字段抛 InvalidTrainingConfigError;
    不替调用方校验,校验见 validate_training_config / ensure_valid_config。"""
    raw = env.payload.get("training_config")
    if raw is None:
        raise InvalidTrainingConfigError(
            f"directive {env.id} has no 'training_config' in its payload"
        )
    try:
        return TrainingConfig.from_dict(raw)
    except KeyError as exc:
        raise InvalidTrainingConfigError(
            f"directive {env.id}: training_config is missing field {exc}"
        ) from exc
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run --extra dev pytest tests/test_mt_config.py -q`
Expected: 74 passed。

- [ ] **Step 5: 全量回归**

Run: `uv run --extra dev pytest -q`
Expected: 409 passed,无警告。

- [ ] **Step 6: 提交**

```bash
git add src/mindtrainer/training_config.py tests/test_mt_config.py
git commit -m "feat: mindtrainer training config with fail-fast validation, hash and phase derivation

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: 统一轨迹格式

**Files:**
- Create: `src/mindtrainer/datamodel.py`
- Test: `tests/test_mt_datamodel.py`

**Interfaces:**
- Consumes: `simulator.datamodel.Episode`。
- Produces:
  - `TrajectoryBatch(episodes: list[Episode], metadata: dict = {})`:`to_dict()` / `from_dict(d)`。`metadata` 约定键:batch 来源为 `manifest_id`、`manifest_hash`、`episode_count`;interact 来源为 `episode_ids`、`failed_attempts`。
  - `RewardedTrajectory(episode, group_id, per_turn_rewards, combined_reward)`:构造时校验 `len(per_turn_rewards) == len(episode.turns)`,不一致抛 `ValueError`(消息含 episode id);`from_dict` 走同一校验;`to_dict()` / `from_dict(d)`。
  - 下半段会往这个文件加 `StepResult`、`TrainingRun`。

**设计要点:**
- 两种轨迹源产出同一个 `TrajectoryBatch`,下游(奖励管线、分组)不关心来源,这是设计的核心原则。
- `per_turn_rewards` 与 `episode.turns` 逐轮对齐,`None` 表示该轮不是被训练角色位的发言。对齐在构造时强制,下游就不必再防御。
- `combined_reward` 是训练轮逐轮奖励的**均值**,不取总和,免得奖励拖长对话。
- `metadata` 用 `default_factory`,两个实例不共享同一个字典;`to_dict` 深拷贝,不泄漏内部引用。

- [ ] **Step 1: 写失败测试**

`tests/test_mt_datamodel.py`:

```python
import json

import pytest
import yaml

from mindtrainer.datamodel import RewardedTrajectory, TrajectoryBatch
from simulator.datamodel import Episode, RoleAssignment, RoleBinding, TurnRecord


def _episode(ep_id="ep-1", n_turns=4):
    assignment = RoleAssignment(
        "s1",
        [RoleBinding("a", "pa", "self_play"),
         RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")],
        max_turns=n_turns,
    )
    turns = [
        TurnRecord(
            i, "a" if i % 2 == 0 else "b", f"u{i}",
            inner_thought="thought" if i % 2 == 0 else None,
        )
        for i in range(n_turns)
    ]
    return Episode(ep_id, "s1", assignment, turns, "max_turns")


# ---------------- TrajectoryBatch ----------------

def test_batch_round_trip():
    batch = TrajectoryBatch(
        [_episode("ep-1"), _episode("ep-2")],
        {"manifest_id": "dsm-run-1-sft", "episode_count": 2},
    )
    assert TrajectoryBatch.from_dict(batch.to_dict()) == batch


def test_batch_metadata_defaults_to_empty_and_is_not_shared_between_instances():
    a, b = TrajectoryBatch([_episode()]), TrajectoryBatch([_episode()])
    a.metadata["x"] = 1
    assert b.metadata == {}


def test_batch_to_dict_does_not_alias_metadata():
    batch = TrajectoryBatch([_episode()], {"failed_attempts": [{"rollout": 0}]})
    batch.to_dict()["metadata"]["failed_attempts"].append({"rollout": 99})
    assert batch.metadata["failed_attempts"] == [{"rollout": 0}]


def test_batch_to_dict_is_plain_data_that_survives_json_and_yaml():
    d = TrajectoryBatch([_episode()], {"manifest_hash": "ab" * 32}).to_dict()
    assert json.loads(json.dumps(d)) == d
    assert yaml.safe_load(yaml.safe_dump(d)) == d


# ---------------- RewardedTrajectory ----------------

def test_rewarded_round_trip_keeps_none_for_non_trained_turns():
    rewarded = RewardedTrajectory(_episode(), "g1", [0.5, None, 1.0, None], 0.75)
    back = RewardedTrajectory.from_dict(rewarded.to_dict())
    assert back == rewarded
    assert back.per_turn_rewards == [0.5, None, 1.0, None]


def test_rewarded_to_dict_survives_json_with_none_as_null():
    d = RewardedTrajectory(_episode(), "g1", [0.5, None, 1.0, None], 0.75).to_dict()
    assert json.loads(json.dumps(d)) == d
    assert yaml.safe_load(yaml.safe_dump(d)) == d


@pytest.mark.parametrize("rewards", [[0.5, None, 1.0], [0.5, None, 1.0, None, 0.1]], ids=["too_few", "too_many"])
def test_rewarded_rejects_rewards_not_aligned_with_turns(rewards):
    with pytest.raises(ValueError, match="ep-1"):
        RewardedTrajectory(_episode(), "g1", rewards, 0.5)


def test_from_dict_enforces_the_same_alignment():
    d = RewardedTrajectory(_episode(), "g1", [0.5, None, 1.0, None], 0.75).to_dict()
    d["per_turn_rewards"].pop()
    with pytest.raises(ValueError, match="per_turn_rewards"):
        RewardedTrajectory.from_dict(d)


def test_rewarded_to_dict_does_not_alias_the_reward_list():
    rewarded = RewardedTrajectory(_episode(), "g1", [0.5, None, 1.0, None], 0.75)
    rewarded.to_dict()["per_turn_rewards"][0] = 99.0
    assert rewarded.per_turn_rewards[0] == 0.5
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run --extra dev pytest tests/test_mt_datamodel.py -q`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindtrainer.datamodel'`。

- [ ] **Step 3: 写实现**

`src/mindtrainer/datamodel.py`:

```python
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from simulator.datamodel import Episode


@dataclass
class TrajectoryBatch:
    """统一轨迹格式(spec §3.3)。两种轨迹源产出同一格式,下游不关心来源。

    metadata 约定键:
    - batch:    manifest_id、manifest_hash、episode_count
    - interact: episode_ids、failed_attempts
    """

    episodes: list[Episode]
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "episodes": [e.to_dict() for e in self.episodes],
            "metadata": copy.deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TrajectoryBatch":
        return cls(
            episodes=[Episode.from_dict(e) for e in d["episodes"]],
            metadata=copy.deepcopy(d.get("metadata", {})),
        )


@dataclass
class RewardedTrajectory:
    """带奖励轨迹(spec §3.3)。奖励管线的输出。

    per_turn_rewards 与 episode.turns 逐轮对齐;None 表示该轮不是被训练角色位的发言。
    combined_reward 是训练轮逐轮奖励的均值(不取总和,免得奖励拖长对话)。
    """

    episode: Episode
    group_id: str  # = simulator.dataset.episode_group_id(episode)
    per_turn_rewards: list[float | None]
    combined_reward: float

    def __post_init__(self) -> None:
        if len(self.per_turn_rewards) != len(self.episode.turns):
            raise ValueError(
                f"per_turn_rewards has {len(self.per_turn_rewards)} entries but episode "
                f"{self.episode.id} has {len(self.episode.turns)} turns"
            )

    def to_dict(self) -> dict:
        return {
            "episode": self.episode.to_dict(),
            "group_id": self.group_id,
            "per_turn_rewards": list(self.per_turn_rewards),
            "combined_reward": self.combined_reward,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RewardedTrajectory":
        return cls(
            episode=Episode.from_dict(d["episode"]),
            group_id=d["group_id"],
            per_turn_rewards=list(d["per_turn_rewards"]),
            combined_reward=d["combined_reward"],
        )
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run --extra dev pytest tests/test_mt_datamodel.py -q`
Expected: 10 passed。

- [ ] **Step 5: 全量回归**

Run: `uv run --extra dev pytest -q`
Expected: 419 passed,无警告。

- [ ] **Step 6: 提交**

```bash
git add src/mindtrainer/datamodel.py tests/test_mt_datamodel.py
git commit -m "feat: mindtrainer unified trajectory batch and rewarded trajectory

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Batch 轨迹源与确定性分批

**Files:**
- Create: `src/mindtrainer/trajectory_source.py`
- Test: `tests/test_mt_trajectory_batch.py`

**Interfaces:**
- Consumes: `simulator.dataset.verify_manifest(store, manifest) -> list[Episode]`、`DatasetManifest`、`DatasetIntegrityError`;`controlplane.gitstore.GitStore`;任务 4 的 `TrajectoryBatch`。测试里用 `simulator.dataset.produce_grouped_dataset` 造数据集。
- Produces(任务 6 与下半段依赖这些名字):
  - `TrajectorySource`(Protocol):`collect(self) -> TrajectoryBatch`。下游只依赖它。
  - `BatchTrajectorySource(store, manifest)`:`collect()` 返回 `TrajectoryBatch`,`metadata` 带 `manifest_id`、`manifest_hash`、`episode_count`。
  - `batch_for_step(items, step, size) -> list`:`[items[(step*size + i) % n] for i in range(size)]`,返回新列表。**放在本文件**,因为任务 6 的 Interact 轨迹源要用它轮转 scenario;下半段的续训逻辑也从这里 import,不要另写一份。

**设计要点:**
- **直接调 `verify_manifest`,不重写校验**:它逐条读 episode、用规范化 JSON 重算 sha256、再重算整体 `manifest_hash`,不依赖 YAML 排版。任何不一致(清单被改、episode 被改或丢失、清单为空)抛 `DatasetIntegrityError`,绝不静默跳过。
- **本类不判断组大小**:SFT 数据集每组一条是正常的。GRPO 阶段启动前的组大小校验属于任务 9 的 `validate_groups`。
- **每次 `collect()` 都重新读盘并重新校验**,所以返回的 episode 彼此独立,调用方改动不会影响下一次 `collect()`(有测试)。
- **`batch_for_step` 是 step 的纯函数**:要让下半段的"续训等价"可测,每个 step 用的数据必须只取决于 step。这只对 Batch 成立;Interact 的数据是实时 rollout,不是 step 的函数。

- [ ] **Step 1: 写失败测试**

`tests/test_mt_trajectory_batch.py`:

```python
import dataclasses

import pytest

from controlplane.gitstore import GitStore
from controlplane.paths import episode_file
from mindtrainer.trajectory_source import BatchTrajectorySource, batch_for_step
from simulator.backends import ScriptedBackend
from simulator.dataset import (
    DatasetIntegrityError, DatasetManifest, episode_group_id, produce_grouped_dataset,
    read_manifest,
)
from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
from simulator.store_adapter import read_episode, write_episode


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _bindings():
    return [
        RoleBinding("a", "pa", "self_play"),
        RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1"),
    ]


def _scenario(sid):
    return Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")


def _personas():
    return {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}


def _scenarios(*ids):
    return [(_scenario(s), RoleAssignment(s, _bindings(), max_turns=4)) for s in ids]


def _varied(scenario, k):
    return {
        "a": ScriptedBackend({"a": [f"{scenario.id}-a{k}-1", f"{scenario.id}-a{k}-2"]}),
        "b": ScriptedBackend({"b": [f"{scenario.id}-b{k}-1", f"{scenario.id}-b{k}-2"]}),
    }


def _produce(store, name="grpo_opd", group_size=2, ids=("s1", "s2")):
    return produce_grouped_dataset(
        store, "run-1", name, scenarios=_scenarios(*ids), personas=_personas(),
        backends_for_rollout=_varied, group_size=group_size,
    )


# ---------------- 正常路径 ----------------

def test_collect_returns_manifest_episodes_in_manifest_order_with_provenance(store):
    manifest = _produce(store)
    batch = BatchTrajectorySource(store, manifest).collect()
    assert [e.id for e in batch.episodes] == [r["id"] for r in manifest.episode_refs]
    assert batch.metadata == {
        "manifest_id": manifest.id,
        "manifest_hash": manifest.manifest_hash,
        "episode_count": 4,
    }


def test_collected_episodes_fall_into_one_group_per_scenario(store):
    batch = BatchTrajectorySource(store, _produce(store)).collect()
    groups = {}
    for episode in batch.episodes:
        groups.setdefault(episode_group_id(episode), []).append(episode.id)
    assert sorted(len(members) for members in groups.values()) == [2, 2]


def test_a_manifest_read_back_from_the_store_works_the_same(store):
    manifest = _produce(store)
    from_disk = read_manifest(store, "run-1", "grpo_opd")
    assert BatchTrajectorySource(store, from_disk).collect() == BatchTrajectorySource(store, manifest).collect()


def test_a_single_episode_per_group_is_fine_here_group_size_is_checked_elsewhere(store):
    manifest = _produce(store, name="sft", group_size=1)
    assert len(BatchTrajectorySource(store, manifest).collect().episodes) == 2


def test_each_collect_reads_afresh_so_batches_are_independent(store):
    source = BatchTrajectorySource(store, _produce(store))
    first, second = source.collect(), source.collect()
    first.episodes[0].turns[0].utterance = "tampered by the caller"
    assert second.episodes[0].turns[0].utterance != "tampered by the caller"
    assert source.collect().episodes[0].turns[0].utterance != "tampered by the caller"


# ---------------- 完整性:失败是显式的 ----------------

def test_a_modified_episode_is_detected(store):
    manifest = _produce(store)
    target = manifest.episode_refs[0]["id"]
    episode = read_episode(store, "run-1", target)
    episode.turns[0].utterance = "rewritten after the manifest was built"
    write_episode(store, "run-1", episode)
    with pytest.raises(DatasetIntegrityError, match=f"episode {target} sha256 mismatch"):
        BatchTrajectorySource(store, manifest).collect()


def test_a_missing_episode_is_detected(store, tmp_path):
    manifest = _produce(store)
    target = manifest.episode_refs[1]["id"]
    (tmp_path / episode_file("run-1", target)).unlink()
    with pytest.raises(DatasetIntegrityError, match=f"episode {target} is missing"):
        BatchTrajectorySource(store, manifest).collect()


def test_a_forged_manifest_hash_is_detected(store):
    manifest = _produce(store)
    forged = dataclasses.replace(manifest, manifest_hash="0" * 64)
    with pytest.raises(DatasetIntegrityError, match="manifest_hash mismatch"):
        BatchTrajectorySource(store, forged).collect()


def test_dropping_a_ref_without_recomputing_the_hash_is_detected(store):
    manifest = _produce(store)
    shrunk = dataclasses.replace(manifest, episode_refs=manifest.episode_refs[:-1])
    with pytest.raises(DatasetIntegrityError, match="manifest_hash mismatch"):
        BatchTrajectorySource(store, shrunk).collect()


def test_an_empty_manifest_is_refused(store):
    empty = DatasetManifest("dsm-run-1-sft", "run-1", "sft", [], "0" * 64)
    with pytest.raises(DatasetIntegrityError, match="is empty"):
        BatchTrajectorySource(store, empty).collect()


# ====================== batch_for_step ======================

def test_batch_for_step_rotates_in_fixed_order_and_wraps():
    items = ["a", "b", "c"]
    assert batch_for_step(items, 0, 2) == ["a", "b"]
    assert batch_for_step(items, 1, 2) == ["c", "a"]
    assert batch_for_step(items, 2, 2) == ["b", "c"]


def test_batch_for_step_may_repeat_items_when_size_exceeds_the_pool():
    assert batch_for_step(["a", "b"], 0, 5) == ["a", "b", "a", "b", "a"]


def test_batch_for_step_is_a_pure_function_of_its_arguments():
    items = ["a", "b", "c"]
    assert batch_for_step(items, 7, 2) == batch_for_step(items, 7, 2)
    assert items == ["a", "b", "c"]


def test_batch_for_step_returns_a_new_list():
    items = ["a", "b", "c"]
    batch_for_step(items, 0, 2).append("zzz")
    assert items == ["a", "b", "c"]


@pytest.mark.parametrize(
    "items,step,size,needle",
    [([], 0, 1, "non-empty"), (["a"], 0, 0, "size must be >= 1"), (["a"], -1, 1, "step must be >= 0")],
    ids=["empty", "size_zero", "negative_step"],
)
def test_batch_for_step_rejects_bad_arguments(items, step, size, needle):
    with pytest.raises(ValueError, match=needle):
        batch_for_step(items, step, size)
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run --extra dev pytest tests/test_mt_trajectory_batch.py -q`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindtrainer.trajectory_source'`。

- [ ] **Step 3: 写实现**

`src/mindtrainer/trajectory_source.py`:

```python
from __future__ import annotations

from typing import Protocol

from controlplane.gitstore import GitStore
from mindtrainer.datamodel import TrajectoryBatch
from simulator.dataset import DatasetManifest, verify_manifest


class TrajectorySource(Protocol):
    """下游(奖励管线、分组)只依赖这一个方法,不关心轨迹来自哪里。"""

    def collect(self) -> TrajectoryBatch: ...


def batch_for_step(items: list, step: int, size: int) -> list:
    """按固定顺序轮转取:[items[(step*size + i) % n] for i in range(size)]。

    每个 step 用的数据是 step 的纯函数,续训等价才可测。回绕时同一批内样本可能重复
    (size > len(items) 时)。返回新列表,改它不会影响 items。"""
    if not items:
        raise ValueError("items must be non-empty")
    if size < 1:
        raise ValueError(f"size must be >= 1, got {size}")
    if step < 0:
        raise ValueError(f"step must be >= 0, got {step}")
    n = len(items)
    return [items[(step * size + i) % n] for i in range(size)]


class BatchTrajectorySource:
    """BatchTrain 的轨迹源:读带 sha256 清单的数据集。

    直接调 simulator.dataset.verify_manifest:逐条读 episode,用规范化 JSON 重算
    sha256 比对清单,再重算整体 manifest_hash,不依赖 YAML 排版。任何不一致
    (清单被改、episode 被改或丢失、清单为空)抛 DatasetIntegrityError,不静默跳过。

    本类不判断组大小:SFT 数据集每组一条是正常的。GRPO 阶段启动前的组大小校验
    属于 grouping.validate_groups。每次 collect() 都重新读盘并重新校验,
    所以返回的 episode 彼此独立,调用方改动不会影响下一次 collect()。
    """

    def __init__(self, store: GitStore, manifest: DatasetManifest) -> None:
        self._store = store
        self._manifest = manifest

    def collect(self) -> TrajectoryBatch:
        episodes = verify_manifest(self._store, self._manifest)
        return TrajectoryBatch(
            episodes=episodes,
            metadata={
                "manifest_id": self._manifest.id,
                "manifest_hash": self._manifest.manifest_hash,
                "episode_count": len(episodes),
            },
        )
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run --extra dev pytest tests/test_mt_trajectory_batch.py -q`
Expected: 17 passed。

- [ ] **Step 5: 全量回归**

Run: `uv run --extra dev pytest -q`
Expected: 436 passed,无警告。

- [ ] **Step 6: 提交**

```bash
git add src/mindtrainer/trajectory_source.py tests/test_mt_trajectory_batch.py
git commit -m "feat: mindtrainer batch trajectory source (manifest-verified) and deterministic batch_for_step

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Interact 轨迹源

**Files:**
- Modify: `src/mindtrainer/trajectory_source.py`(替换文件顶部的 import 区,并在文件末尾追加)
- Test: `tests/test_mt_trajectory_interact.py`

**Interfaces:**
- Consumes:
  - 任务 2 的 `EnvironmentServiceClient.turns` 与 `.abort()`;Plan 4 的 `create_session` / `submit_turn` / `drain_until_end` / `closed`、`SessionRejectedError(reason, detail)`、`create_server(...)`。
  - 任务 3 的 `TrainingConfig`、`ensure_valid_config`;任务 5 的 `batch_for_step`、`TrajectoryBatch`。
  - `simulator.store_adapter.read_scenario` / `read_persona`;`simulator.backends.ModelBackend`(策略)。
- Produces:
  - `RolloutFailedError(scenario_id, rollout, reason, detail, attempts, failed_attempts)`:某条 rollout 失败且不再重试。`failed_attempts` 带着截至失败时本次 `collect()` 的**全部**失败尝试,包括更早 rollout 的瞬态失败。
  - `InteractTrajectorySource(client_factory, policy, store, config, *, start_step=0)`:`collect() -> TrajectoryBatch`,`metadata` 带 `episode_ids`、`failed_attempts`。构造时校验配置且要求 `mode == "interact"`。
    - **`start_step` 是已完成的 GRPO step 数,不是全局 step**:续训时编排器传 `global_step - sft_steps`(不小于 0),scenario 轮转才能接着走。

**设计要点:**
- **一条 gRPC 流只承载一次会话**(Plan 4),所以每次尝试用 `client_factory()` 新建客户端。
- **策略的异常不放进任何 `except`**:它穿过 `finally`(先 `abort()`)原样上抛,不包装、不重试——那是被训模型的 bug,不是瞬态服务问题。测试里专门用 `RuntimeError` 做策略异常,防止它被"客户端协议错误"那条 `except RuntimeError` 误吞。
- **必须用 `abort()` 而不是 `end_session()`**:`abort()` 不发 `EndSession`,服务端按断连判 FAILED、不经 `on_session_closed` 落盘。改发 `end_session` 会让半截 episode 被当成正常结束写进 ControlPlane,污染训练数据。有一条对真实服务端的回归测试专门守这一点(先断言落盘钩子没被调用,Task 2 里已有正向对照证明钩子确实会触发)。
- **只有 `terminated_reason ∈ {max_turns, goal_reached}` 的 episode 进 batch**;超时、后端失败、断连产生的半截 episode 一律不进。
- **瞬态(T1)= 服务端报告的 `timeout`、`role_backend_timeout`,加上传输层 `UNAVAILABLE`**,按 `max_retries_per_episode` 原样整条重跑。其余传输错误(例如并发上限触发的 `RESOURCE_EXHAUSTED`)、`invalid_session`、`role_backend_failure` 一律不重试。`role_backend_failure` 在服务端同时覆盖"后端确定性异常"和"工厂/解析器故障",光看 reason 分不出 T1 还是 T2,所以保守地不重试——这是设计里已记录的含糊点。
- **失败的 `collect()` 不推进轮转**,下次重做同一批 scenario。
- **每次尝试新建 `RoleAssignment`**(`from_dict(to_dict())`),同组 episode 内容相同但不共享同一个可变对象。
- 测试分两组:对真实 loopback 服务端(`127.0.0.1`,与 `run_episode` 直接对拍、分组、落盘、客户端后说、`inner_thought` 隔离、策略异常后无半截落盘、服务端停止),以及对替身客户端(失败分类、重试上限、轮转、`start_step`)。

- [ ] **Step 1: 写失败测试**

`tests/test_mt_trajectory_interact.py`:

```python
import time
from types import SimpleNamespace

import grpc
import pytest

from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from mindtrainer.training_config import (
    InvalidTrainingConfigError, RewardConfig, TrainingConfig,
)
from mindtrainer.trajectory_source import (
    InteractTrajectorySource, RolloutFailedError,
)
from simulator.backends import ScriptedBackend
from simulator.dataset import episode_group_id
from simulator.datamodel import (
    Episode, Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario, TurnRecord,
)
from simulator.realtime.client import EnvironmentServiceClient, SessionRejectedError
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server
from simulator.runner import run_episode
from simulator.store_adapter import read_scenario, write_persona, write_scenario


def _persona(pid):
    return Persona(pid, ["t"], "m", "s", ["v"])


def _scenario(sid):
    return Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")


def _trained():
    return RoleBinding("a", "pa", "self_play")


def _opponent():
    return RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")


def _cfg(**kw):
    base = dict(
        mode="interact", trained_slot="a",
        base_checkpoint_ref=BigRef("file:///ckpt/a", "a" * 64, 1),
        teacher_model_ref=BigRef("file:///ckpt/b", "b" * 64, 1),
        reward=RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]}),
        sft_steps=0, grpo_steps=4, sft_batch_size=2, groups_per_step=2, group_size=2,
        grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=2, allow_resume=True,
        scenario_ids=["s1", "s2"], bindings=[_trained(), _opponent()],
        max_turns_per_episode=4,
    )
    base.update(kw)
    return TrainingConfig(**base)


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _shape(episode):
    return [(t.speaker_slot, t.utterance) for t in episode.turns], episode.terminated_reason


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    for sid in ("s1", "s2", "s3"):
        write_scenario(s, _scenario(sid))
    for pid in ("pa", "pb"):
        write_persona(s, _persona(pid))
    return s


@pytest.fixture
def live(store):
    channels, servers = [], []

    def start(opponent_lines=("b1", "b2", "b3", "b4"), turn_timeout_s=2.0):
        closed = []

        def factory(scenario, assignment, personas, client_slot):
            return {
                s: ScriptedBackend({s: list(opponent_lines)})
                for s in assignment.slot_ids() - {client_slot}
            }

        server = create_server(
            factory, port=0, turn_timeout_s=turn_timeout_s, on_session_closed=closed.append,
            scenario_resolver=lambda sid: read_scenario(store, sid),
        )
        server.start()
        servers.append(server)

        def client_factory():
            channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
            channels.append(channel)
            return EnvironmentServiceClient(pb2_grpc.EnvironmentServiceStub(channel))

        return SimpleNamespace(server=server, closed=closed, client_factory=client_factory)

    yield start
    for channel in channels:
        channel.close()
    for server in servers:
        server.stop(0)


# ====================== 对真实 loopback 服务端 ======================

def test_rollouts_match_what_run_episode_produces_for_the_same_scripts(store, live):
    env = live()
    policy = ScriptedBackend({"a": ["a1", "a2", "a3", "a4"]})
    cfg = _cfg(scenario_ids=["s1"], groups_per_step=1, group_size=2)
    batch = InteractTrajectorySource(env.client_factory, policy, store, cfg).collect()

    personas = {pid: _persona(pid) for pid in ("pa", "pb")}
    expected = []
    for k, lines in enumerate((["a1", "a2"], ["a3", "a4"])):
        assignment = RoleAssignment("s1", [_trained(), _opponent()], 4)
        backends = {
            "a": ScriptedBackend({"a": lines}),
            "b": ScriptedBackend({"b": ["b1", "b2"]}),
        }
        expected.append(run_episode(read_scenario(store, "s1"), assignment, personas, backends, f"direct-{k}"))
    assert [_shape(e) for e in batch.episodes] == [_shape(e) for e in expected]
    assert all(e.terminated_reason == "max_turns" for e in batch.episodes)


def test_each_scenario_forms_one_group_of_group_size(store, live):
    env = live()
    policy = ScriptedBackend({"a": [f"a{i}" for i in range(20)]})
    batch = InteractTrajectorySource(env.client_factory, policy, store, _cfg(group_size=3)).collect()
    assert [e.scenario_id for e in batch.episodes] == ["s1"] * 3 + ["s2"] * 3
    gids = [episode_group_id(e) for e in batch.episodes]
    assert len(set(gids[:3])) == 1 and len(set(gids[3:])) == 1 and gids[0] != gids[3]
    assert len({e.id for e in batch.episodes}) == 6
    assert batch.metadata["episode_ids"] == [e.id for e in batch.episodes]
    assert batch.metadata["failed_attempts"] == []


def test_the_server_persists_exactly_the_episodes_returned(store, live):
    env = live()
    policy = ScriptedBackend({"a": [f"a{i}" for i in range(20)]})
    batch = InteractTrajectorySource(env.client_factory, policy, store, _cfg()).collect()
    assert _wait_until(lambda: len(env.closed) == 4)
    assert {e.id for e in env.closed} == {e.id for e in batch.episodes}


def test_trained_slot_speaking_second_sees_the_opponents_opening_in_its_history(store, live):
    env = live()
    calls, seen = [], []

    class Recorder:
        def respond(self, persona, scenario, history, speaker_slot):
            calls.append((persona.id, scenario.id, speaker_slot))
            seen.append([(t.speaker_slot, t.utterance) for t in history])
            return f"a{len(seen)}", None

    cfg = _cfg(bindings=[_opponent(), _trained()], scenario_ids=["s1"], groups_per_step=1)
    batch = InteractTrajectorySource(env.client_factory, Recorder(), store, cfg).collect()
    assert calls[0] == ("pa", "s1", "a")
    assert seen[0] == [("b", "b1")]
    assert seen[1] == [("b", "b1"), ("a", "a1"), ("b", "b2")]
    assert [t.speaker_slot for t in batch.episodes[0].turns] == ["b", "a", "b", "a"]


def test_inner_thoughts_reach_the_server_side_episode_only_for_the_trained_slot(store, live):
    env = live()

    class Thinker:
        n = 0

        def respond(self, persona, scenario, history, speaker_slot):
            self.n += 1
            return f"a{self.n}", f"thought-{self.n}"

    cfg = _cfg(scenario_ids=["s1"], groups_per_step=1)
    InteractTrajectorySource(env.client_factory, Thinker(), store, cfg).collect()
    assert _wait_until(lambda: len(env.closed) == 2)
    for episode in env.closed:
        trained = [t for t in episode.turns if t.speaker_slot == "a"]
        opponent = [t for t in episode.turns if t.speaker_slot == "b"]
        assert trained and all((t.inner_thought or "").startswith("thought-") for t in trained)
        assert all(t.inner_thought is None for t in opponent)


class _Boom(Exception):
    pass


def test_a_policy_exception_aborts_the_session_and_no_half_episode_is_persisted(store, live):
    env = live()

    class Exploding:
        calls = 0

        def respond(self, persona, scenario, history, speaker_slot):
            self.calls += 1
            if self.calls == 2:  # 已经提交过一轮,会话进行到一半
                raise _Boom("model bug")
            return "a1", None

    clients = []

    def tracked():
        client = env.client_factory()
        clients.append(client)
        return client

    with pytest.raises(_Boom, match="model bug"):
        InteractTrajectorySource(tracked, Exploding(), store, _cfg()).collect()
    env.server.stop(2).wait()  # 等在途会话收尾,再断言"没有落盘"
    assert env.closed == []
    assert len(clients) == 1 and clients[0].closed


def test_a_stopped_server_is_a_transient_transport_failure_retried_then_reported(store, live):
    env = live()
    env.server.stop(0).wait()
    cfg = _cfg(scenario_ids=["s1"], groups_per_step=1, max_retries_per_episode=1)
    source = InteractTrajectorySource(
        env.client_factory, ScriptedBackend({"a": ["x"] * 4}), store, cfg
    )
    with pytest.raises(RolloutFailedError) as exc:
        source.collect()
    assert exc.value.reason == "transport_unavailable"
    assert exc.value.attempts == 2
    assert [a["attempt"] for a in exc.value.failed_attempts] == [1, 2]


# ====================== 对替身客户端(不走网络) ======================

class _Unavailable(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

    def details(self):
        return "connection reset"


class _Exhausted(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.RESOURCE_EXHAUSTED

    def details(self):
        return "too many sessions"


class _FakeClient:
    """客户端先说,每次提交后对方回一句;凑够 max_turns 轮后以 end_reason 结束。"""

    def __init__(self, ep_id, *, create_error=None, submit_error=None,
                 end_reason="max_turns", max_turns=4):
        self._ep_id = ep_id
        self._create_error = create_error
        self._submit_error = submit_error
        self._end_reason = end_reason
        self._max_turns = max_turns
        self._turns = []
        self.closed = False
        self.aborted = False
        self.created = None

    @property
    def turns(self):
        return list(self._turns)

    def create_session(self, *, scenario_id, role_assignment, personas, purpose, client_driven_slot):
        self.created = dict(
            scenario_id=scenario_id, role_assignment=role_assignment, personas=personas,
            purpose=purpose, slot=client_driven_slot,
        )
        if self._create_error is not None:
            self.closed = True
            raise self._create_error
        return "sess"

    def submit_turn(self, utterance, inner_thought=None):
        if self._submit_error is not None:
            self.closed = True
            raise self._submit_error
        self._turns.append(TurnRecord(len(self._turns), self.created["slot"], utterance, inner_thought))
        self._turns.append(TurnRecord(len(self._turns), "b", "opp"))
        if len(self._turns) >= self._max_turns:
            self.closed = True
        return []

    def drain_until_end(self):
        return Episode(
            self._ep_id, self.created["scenario_id"], self.created["role_assignment"],
            self.turns, self._end_reason,
        )

    def abort(self):
        self.aborted = True
        self.closed = True


class _FakeFactory:
    def __init__(self, *plans):
        self._plans = list(plans)
        self.clients = []

    def __call__(self):
        plan = self._plans.pop(0) if self._plans else {}
        client = _FakeClient(f"ep-fake-{len(self.clients)}", **plan)
        self.clients.append(client)
        return client


def _source(store, factory, policy=None, **cfg_kw):
    policy = policy or ScriptedBackend({"a": [f"a{i}" for i in range(50)]})
    start_step = cfg_kw.pop("start_step", 0)
    return InteractTrajectorySource(factory, policy, store, _cfg(**cfg_kw), start_step=start_step)


_ONE_GROUP = dict(scenario_ids=["s1"], groups_per_step=1)


@pytest.mark.parametrize(
    "plan,reason",
    [
        ({"end_reason": "timeout"}, "timeout"),
        ({"end_reason": "role_backend_timeout"}, "role_backend_timeout"),
        ({"create_error": _Unavailable()}, "transport_unavailable"),
        ({"submit_error": _Unavailable()}, "transport_unavailable"),
    ],
    ids=["timeout", "role_backend_timeout", "unavailable_on_create", "unavailable_on_submit"],
)
def test_a_transient_failure_is_retried_and_recorded_and_the_half_episode_is_not_kept(store, plan, reason):
    factory = _FakeFactory(plan)
    batch = _source(store, factory, **_ONE_GROUP).collect()
    assert len(batch.episodes) == 2
    assert all(e.terminated_reason == "max_turns" for e in batch.episodes)
    assert "ep-fake-0" not in batch.metadata["episode_ids"]
    assert batch.metadata["failed_attempts"] == [
        {"scenario_id": "s1", "rollout": 0, "attempt": 1, "reason": reason,
         "detail": batch.metadata["failed_attempts"][0]["detail"]}
    ]
    assert len(factory.clients) == 3  # 失败一次 + 两条成功


def test_retries_are_bounded_and_the_error_carries_every_attempt(store):
    factory = _FakeFactory({"end_reason": "timeout"}, {"end_reason": "timeout"})
    source = _source(store, factory, max_retries_per_episode=1, **_ONE_GROUP)
    with pytest.raises(RolloutFailedError) as exc:
        source.collect()
    err = exc.value
    assert (err.scenario_id, err.rollout, err.reason, err.attempts) == ("s1", 0, "timeout", 2)
    assert [a["attempt"] for a in err.failed_attempts] == [1, 2]
    assert "rollout 0" in str(err) and "'s1'" in str(err)
    assert len(factory.clients) == 2


def test_zero_retries_means_a_single_attempt(store):
    factory = _FakeFactory({"end_reason": "timeout"})
    with pytest.raises(RolloutFailedError) as exc:
        _source(store, factory, max_retries_per_episode=0, **_ONE_GROUP).collect()
    assert exc.value.attempts == 1 and len(factory.clients) == 1


@pytest.mark.parametrize(
    "plan,reason",
    [
        ({"create_error": SessionRejectedError("invalid_session", "bad persona")}, "invalid_session"),
        ({"create_error": SessionRejectedError("role_backend_failure", "factory exploded")}, "role_backend_failure"),
        ({"end_reason": "role_backend_failure"}, "role_backend_failure"),
        ({"end_reason": "client_disconnected"}, "client_disconnected"),
        ({"create_error": _Exhausted()}, "transport_resource_exhausted"),
        ({"submit_error": RuntimeError("server closed the stream without sending EndSession")}, "client_protocol_error"),
    ],
    ids=["invalid_session", "factory_fault", "backend_failure", "disconnected", "exhausted", "protocol_error"],
)
def test_non_transient_failures_are_not_retried(store, plan, reason):
    factory = _FakeFactory(plan)
    with pytest.raises(RolloutFailedError) as exc:
        _source(store, factory, **_ONE_GROUP).collect()
    assert exc.value.reason == reason
    assert exc.value.attempts == 1
    assert len(factory.clients) == 1
    assert len(exc.value.failed_attempts) == 1


def test_a_failure_carries_the_earlier_transient_failures_of_the_same_collect(store):
    factory = _FakeFactory(
        {"end_reason": "timeout"},                                      # rollout 0,第 1 次
        {},                                                             # rollout 0,第 2 次成功
        {"create_error": SessionRejectedError("invalid_session", "x")}, # rollout 1,不可重试
    )
    with pytest.raises(RolloutFailedError) as exc:
        _source(store, factory, **_ONE_GROUP).collect()
    assert [(a["rollout"], a["attempt"], a["reason"]) for a in exc.value.failed_attempts] == [
        (0, 1, "timeout"), (1, 1, "invalid_session"),
    ]


def test_a_policy_exception_is_never_retried_wrapped_or_swallowed_even_if_it_is_a_runtimeerror(store):
    class Broken:
        def respond(self, persona, scenario, history, speaker_slot):
            raise RuntimeError("model bug")

    factory = _FakeFactory()
    with pytest.raises(RuntimeError, match="model bug") as exc:
        _source(store, factory, policy=Broken(), **_ONE_GROUP).collect()
    assert not isinstance(exc.value, RolloutFailedError)
    assert len(factory.clients) == 1 and factory.clients[0].aborted


def test_scenarios_rotate_with_the_step_and_start_step_resumes_the_rotation(store):
    rotating = dict(scenario_ids=["s1", "s2", "s3"], groups_per_step=2)
    factory = _FakeFactory()
    source = _source(store, factory, **rotating)
    source.collect()
    source.collect()
    used = [c.created["scenario_id"] for c in factory.clients]
    assert used == ["s1", "s1", "s2", "s2", "s3", "s3", "s1", "s1"]

    resumed = _FakeFactory()
    _source(store, resumed, start_step=1, **rotating).collect()
    assert [c.created["scenario_id"] for c in resumed.clients] == ["s3", "s3", "s1", "s1"]


def test_a_failed_collect_does_not_advance_the_rotation(store):
    factory = _FakeFactory({"create_error": SessionRejectedError("invalid_session", "x")})
    source = _source(store, factory)
    with pytest.raises(RolloutFailedError):
        source.collect()
    source.collect()
    assert [c.created["scenario_id"] for c in factory.clients[1:]] == ["s1", "s1", "s2", "s2"]


def test_create_session_receives_the_configured_assignment_personas_and_purpose(store):
    factory = _FakeFactory()
    _source(store, factory, **_ONE_GROUP).collect()
    first, second = factory.clients[0].created, factory.clients[1].created
    assert first["purpose"] == "training" and first["slot"] == "a"
    assert set(first["personas"]) == {"pa", "pb"}
    assignment = first["role_assignment"]
    assert assignment.max_turns == 4
    assert [b.to_dict() for b in assignment.bindings] == [_trained().to_dict(), _opponent().to_dict()]
    # 内容相同(同组),但每次是独立对象
    assert assignment.to_dict() == second["role_assignment"].to_dict()
    assert assignment is not second["role_assignment"]


def test_constructor_rejects_the_wrong_mode_an_invalid_config_and_a_negative_start(store):
    batch_cfg = dict(
        mode="batch", sft_steps=0, grpo_steps=2, stage_manifest_refs={"grpo_opd": "m"},
        scenario_ids=[], bindings=[], max_turns_per_episode=0,
    )
    with pytest.raises(ValueError, match="mode='interact'"):
        _source(store, _FakeFactory(), **batch_cfg)
    with pytest.raises(InvalidTrainingConfigError, match="trained_slot 'ghost'"):
        _source(store, _FakeFactory(), trained_slot="ghost")
    with pytest.raises(ValueError, match="start_step must be >= 0"):
        _source(store, _FakeFactory(), start_step=-1)
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run --extra dev pytest tests/test_mt_trajectory_interact.py -q`
Expected: 收集阶段报错 `ImportError: cannot import name 'InteractTrajectorySource' from 'mindtrainer.trajectory_source'`。

- [ ] **Step 3: 写实现**

先把 `src/mindtrainer/trajectory_source.py` 顶部的 import 区(从 `from __future__ import annotations` 到 `from simulator.dataset import DatasetManifest, verify_manifest` 为止)整体替换为:

```python
from __future__ import annotations

from typing import Callable, Protocol

import grpc

from controlplane.gitstore import GitStore
from mindtrainer.datamodel import TrajectoryBatch
from mindtrainer.training_config import TrainingConfig, ensure_valid_config
from simulator.backends import ModelBackend
from simulator.dataset import DatasetManifest, verify_manifest
from simulator.datamodel import Episode, RoleAssignment
from simulator.realtime.client import EnvironmentServiceClient, SessionRejectedError
from simulator.store_adapter import read_persona, read_scenario
```

再在文件末尾追加:

```python
# 只有这两种终止原因的 episode 进 batch。超时、后端失败、断连产生的半截 episode 一律不进。
_ACCEPTED_REASONS = frozenset({"max_turns", "goal_reached"})
# T1 瞬态:服务端报告的两种超时,加上传输层 UNAVAILABLE(见 _classify_client_error)。
_TRANSIENT_REASONS = frozenset({"timeout", "role_backend_timeout"})


class RolloutFailedError(Exception):
    """某条 rollout 失败且不再重试(不可重试的原因,或瞬态重试用尽)。

    failed_attempts 带着截至失败时本次 collect 的全部失败尝试(含更早 rollout 的瞬态
    失败):失败是数据,调用方应把它记下来,不要丢。"""

    def __init__(
        self,
        *,
        scenario_id: str,
        rollout: int,
        reason: str,
        detail: str,
        attempts: int,
        failed_attempts: list[dict],
    ) -> None:
        tail = f": {detail}" if detail else ""
        super().__init__(
            f"rollout {rollout} of scenario {scenario_id!r} failed after "
            f"{attempts} attempt(s): {reason}{tail}"
        )
        self.scenario_id = scenario_id
        self.rollout = rollout
        self.reason = reason
        self.detail = detail
        self.attempts = attempts
        self.failed_attempts = [dict(a) for a in failed_attempts]


class _AttemptFailed(Exception):
    def __init__(self, reason: str, detail: str, transient: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.transient = transient


def _classify_client_error(exc: Exception) -> _AttemptFailed:
    """客户端调用抛出的传输/协议错误 -> 一次失败的尝试。只有 UNAVAILABLE 算瞬态。"""
    if isinstance(exc, grpc.RpcError):
        code = exc.code() if callable(getattr(exc, "code", None)) else None
        detail = exc.details() if callable(getattr(exc, "details", None)) else str(exc)
        if code == grpc.StatusCode.UNAVAILABLE:
            return _AttemptFailed("transport_unavailable", detail or "", True)
        name = code.name.lower() if code is not None else "unknown"
        return _AttemptFailed(f"transport_{name}", detail or "", False)
    return _AttemptFailed("client_protocol_error", str(exc), False)


class InteractTrajectorySource:
    """InteractTrain 的轨迹源:作为客户端驱动 Plan 4 的实时环境服务。

    - 一条 gRPC 流只承载一次会话,所以每条 rollout 用 client_factory() 新建客户端。
    - policy 复用 simulator.backends.ModelBackend 协议。
    - 每个 scenario 连续 rollout group_size 条,共享同一个 RoleAssignment 的内容,
      所以 group_id 相同。每次 collect() 取 groups_per_step 个 scenario,按 scenario_ids
      轮转(batch_for_step)。V1 顺序执行。
    - episode 由服务端的 on_session_closed 钩子落盘(调用方配置);本类不写 episode,
      只在 metadata 里记 episode id 列表。服务端的 scenario_resolver 必须读与本类
      同一个 ControlPlane,否则两边对 scenario 的理解会分歧(接线计划负责)。

    失败处理(补充 E.4:失败会话整条重跑,不续接):
    - 只有 terminated_reason ∈ {max_turns, goal_reached} 的 episode 进 batch。
    - T1 瞬态(timeout、role_backend_timeout、传输层 UNAVAILABLE)原样重跑,上限
      max_retries_per_episode,每次失败记入 metadata["failed_attempts"]。
    - 其余(invalid_session、role_backend_failure 等)不重试,抛 RolloutFailedError。
      role_backend_failure 在服务端同时覆盖"后端确定性异常"和"工厂/解析器故障",
      光看 reason 分不出 T1 还是 T2,所以保守地不重试。
    - 策略自己抛的异常不重试、不包装:会话先 abort()(服务端按断连判 FAILED、不落盘),
      原异常原样向上抛——那是被训模型的 bug,不是瞬态服务问题。
    """

    def __init__(
        self,
        client_factory: Callable[[], EnvironmentServiceClient],
        policy: ModelBackend,
        store: GitStore,
        config: TrainingConfig,
        *,
        start_step: int = 0,
    ) -> None:
        ensure_valid_config(config)
        if config.mode != "interact":
            raise ValueError(f"InteractTrajectorySource needs mode='interact', got {config.mode!r}")
        if start_step < 0:
            raise ValueError(f"start_step must be >= 0, got {start_step}")
        self._client_factory = client_factory
        self._policy = policy
        self._store = store
        self._cfg = config
        # 已完成的 GRPO step 数:续训时由编排器传入,让 scenario 轮转接着走
        self._next_step = start_step
        self._trained_persona_id = next(
            b.persona_id for b in config.bindings if b.slot_id == config.trained_slot
        )

    def collect(self) -> TrajectoryBatch:
        cfg = self._cfg
        scenario_ids = batch_for_step(cfg.scenario_ids, self._next_step, cfg.groups_per_step)
        episodes: list[Episode] = []
        failed_attempts: list[dict] = []
        for scenario_id in scenario_ids:
            scenario = read_scenario(self._store, scenario_id)
            personas = {
                b.persona_id: read_persona(self._store, b.persona_id) for b in cfg.bindings
            }
            for rollout in range(cfg.group_size):
                episodes.append(
                    self._rollout(scenario, personas, rollout, failed_attempts)
                )
        self._next_step += 1  # 失败(抛异常)时不前进,下次 collect 重做同一批
        return TrajectoryBatch(
            episodes=episodes,
            metadata={
                "episode_ids": [e.id for e in episodes],
                "failed_attempts": failed_attempts,
            },
        )

    def _assignment(self, scenario_id: str) -> RoleAssignment:
        # 每次尝试都新建一份,免得同组 episode 共享同一个可变对象
        return RoleAssignment.from_dict(
            {
                "scenario_id": scenario_id,
                "bindings": [b.to_dict() for b in self._cfg.bindings],
                "max_turns": self._cfg.max_turns_per_episode,
            }
        )

    def _rollout(self, scenario, personas, rollout: int, failed_attempts: list[dict]) -> Episode:
        budget = 1 + self._cfg.max_retries_per_episode
        for attempt in range(1, budget + 1):
            try:
                return self._attempt(scenario, personas)
            except _AttemptFailed as failure:
                failed_attempts.append(
                    {
                        "scenario_id": scenario.id,
                        "rollout": rollout,
                        "attempt": attempt,
                        "reason": failure.reason,
                        "detail": failure.detail,
                    }
                )
                if not failure.transient or attempt == budget:
                    raise RolloutFailedError(
                        scenario_id=scenario.id,
                        rollout=rollout,
                        reason=failure.reason,
                        detail=failure.detail,
                        attempts=attempt,
                        failed_attempts=failed_attempts,
                    ) from failure
        raise AssertionError("unreachable: the loop always returns or raises")

    def _attempt(self, scenario, personas) -> Episode:
        slot = self._cfg.trained_slot
        client = self._client_factory()
        try:
            try:
                client.create_session(
                    scenario_id=scenario.id,
                    role_assignment=self._assignment(scenario.id),
                    personas=personas,
                    purpose="training",
                    client_driven_slot=slot,
                )
            except SessionRejectedError as exc:
                raise _AttemptFailed(exc.reason, exc.detail, False) from exc
            except (grpc.RpcError, RuntimeError) as exc:
                raise _classify_client_error(exc) from exc
            persona = personas[self._trained_persona_id]
            while not client.closed:
                # 策略的异常不在任何 except 里:它穿过下面的 finally(先 abort)原样上抛
                utterance, thought = self._policy.respond(persona, scenario, client.turns, slot)
                try:
                    client.submit_turn(utterance, thought)
                except (grpc.RpcError, RuntimeError) as exc:
                    raise _classify_client_error(exc) from exc
            try:
                episode = client.drain_until_end()
            except (grpc.RpcError, RuntimeError) as exc:
                raise _classify_client_error(exc) from exc
        finally:
            if not client.closed:
                # 不发 EndSession:服务端按断连判 FAILED,不经 on_session_closed 落盘。
                # 若改发 end_session,半截 episode 会被当成正常结束写进 ControlPlane。
                client.abort()
        if episode.terminated_reason in _ACCEPTED_REASONS:
            return episode
        raise _AttemptFailed(
            episode.terminated_reason, "", episode.terminated_reason in _TRANSIENT_REASONS
        )
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run --extra dev pytest tests/test_mt_trajectory_interact.py tests/test_mt_trajectory_batch.py -q`
Expected: 42 passed(Interact 25 + Batch 17)。

- [ ] **Step 5: 全量回归**

Run: `uv run --extra dev pytest -q`
Expected: 461 passed,无警告。

- [ ] **Step 6: 提交**

```bash
git add src/mindtrainer/trajectory_source.py tests/test_mt_trajectory_interact.py
git commit -m "feat: mindtrainer interact trajectory source (abort on policy error, bounded T1 retry, failures as data)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: 奖励归因后端(规则式与 judge 式)

**Files:**
- Create: `src/mindtrainer/reward_pipeline.py`
- Test: `tests/test_mt_reward_pipeline.py`

**Interfaces:**
- Consumes: 任务 3 的 `RewardConfig`;`simulator.datamodel.Episode`。
- Produces(任务 8 与下半段依赖这些名字):
  - `AttributionError(ValueError)`、`SelfEvalBiasError(Exception)`。
  - `AttributionBackend`(Protocol):`attribute(episode, trained_slot, components) -> dict[str, list[float | None]]`,返回每个分量逐轮的分数,非训练角色位的轮次为 `None`。
  - `TurnJudge`(Protocol):`model_id: str`;`score_turn(episode, turn_index, component) -> float`。
  - `OutcomeSplitAttribution(rules, gamma=1.0)`:规则式。
  - `MockTurnJudge(model_id, table)`:测试替身,键是 `(episode_id, turn_index, component)`,缺键直接 `KeyError`。
  - `JudgeAttribution(judge, trained_model_id)`:构造时做自评偏差守卫。
  - `build_attribution_backend(config, *, judge=None, trained_model_id=None) -> AttributionBackend`:按 `RewardConfig` 组装后端。

**设计要点:**
- **规则归因**:每个分量先算一个结果分 `o`——训练角色位**任一发言**含**任一**关键词则 1.0,否则 0.0(纯子串、区分大小写,沿用 Plan 3 动态轨 `goal_keyword` 的匹配约定);只看 `utterance`,不看对手,也不看 `inner_thought`。第 k 个训练轮(共 K 个)得 `o * gamma ** (K-1-k)`,越靠近结尾越高,`gamma=1.0` 时各训练轮同分。**不归一化**,所以仍在 [0,1]:归一化后的份额相乘没有意义,逐轮分数相乘才是门控。
- **刻意不做 `terminated_reason == goal_reached` 的规则**:Interact 的 episode 永远不会以 `goal_reached` 结束(Plan 4 里 driver 接管了 `stop_predicate`),那条规则恒为 0,是个陷阱。**也不做客观答案类**(如 HiToM):`Episode` 没有参考答案字段,这是 SocialSimulator 侧的数据问题。
- **自评偏差守卫**(spec §3 没有明文要求,是设计里**我加的**):训练奖励比诊断更危险,模型会被直接优化去讨好自己的 judge。构造 `JudgeAttribution` 时,若 judge 与被训模型相同或互为前缀(**双向**,"x-sft" 评 "x" 与 "x" 评 "x-sft" 同样有偏)就拒绝。这个函数只有一行,MindTrainer 里自己写一份,**不 import `diagnostic.scorer` 的私有函数**(MindTrainer 不依赖 diagnostic);两份可能漂移,所以测试用同一组双向用例。`SelfEvalBiasError` 也是 MindTrainer 自己的类,与 `diagnostic.scorer.SelfEvalBiasError` 同名但互不相干。它只是保守的前缀匹配,不是真正的模型血缘判断。
- **空 model id 必须拒绝**:空串是任何字符串的前缀,不拦的话守卫会失效或误伤。
- **`build_attribution_backend` 核对"配置里写的 judge"与"实际传入的 judge"一致**:配置被哈希锚定,实际用了别的 judge 就无法按配方复现。`outcome_split` 不接受 judge。
- **judge 返回的分数不在这里校验**,统一由任务 8 的 `validate_attribution` 把关。
- 本任务的测试文件只含归因后端的用例;任务 8 会给同一个文件追加校验、组合与管线入口的用例。

- [ ] **Step 1: 写失败测试**

`tests/test_mt_reward_pipeline.py`:

```python
import pytest

from mindtrainer.reward_pipeline import (
    AttributionError, JudgeAttribution, MockTurnJudge, OutcomeSplitAttribution,
    SelfEvalBiasError, build_attribution_backend,
)
from mindtrainer.training_config import RewardConfig
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
    with pytest.raises(ValueError, match="gamma must be in"):
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
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run --extra dev pytest tests/test_mt_reward_pipeline.py -q`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindtrainer.reward_pipeline'`。

- [ ] **Step 3: 写实现**

`src/mindtrainer/reward_pipeline.py`:

```python
from __future__ import annotations

from typing import Protocol

from mindtrainer.training_config import RewardConfig
from simulator.datamodel import Episode


class AttributionError(ValueError):
    """归因结果不合法,或无法产生归因。确定性失败:不静默跳过、不重试。"""


class SelfEvalBiasError(Exception):
    """judge 与被训模型相同或互为前缀。训练奖励比诊断更危险:模型会被直接优化去
    讨好自己的 judge,所以构造时就拒绝。"""


class AttributionBackend(Protocol):
    def attribute(
        self, episode: Episode, trained_slot: str, components: list[str]
    ) -> dict[str, list[float | None]]:
        """返回每个分量逐轮的分数,取值 [0,1];非训练角色位的轮次为 None。"""
        ...


class TurnJudge(Protocol):
    model_id: str

    def score_turn(self, episode: Episode, turn_index: int, component: str) -> float: ...


# ---------------- 规则归因 ----------------

class OutcomeSplitAttribution:
    """规则式归因。每个分量先算一个结果分 o:训练角色位任一发言含任一关键词则 1.0,
    否则 0.0(纯子串、区分大小写)。只看训练角色位的 utterance,对手的话和
    inner_thought 都不算。

    第 k 个训练轮(共 K 个)的得分是 o * gamma ** (K-1-k),即越靠近结尾越高;
    gamma=1.0 时各训练轮同分。**不归一化**,所以仍在 [0,1]:归一化后的份额相乘
    没有意义,逐轮分数相乘才是门控。

    刻意不做 terminated_reason == goal_reached 的规则:Interact 的 episode 永远不会
    以 goal_reached 结束(driver 接管了 stop_predicate),那条规则恒为 0。"""

    def __init__(self, rules: dict[str, list[str]], gamma: float = 1.0) -> None:
        if not (0 < gamma <= 1):  # NaN 也会落到这里
            raise ValueError(f"gamma must be in (0, 1], got {gamma}")
        self._rules = {name: list(words) for name, words in rules.items()}
        self._gamma = gamma

    def attribute(
        self, episode: Episode, trained_slot: str, components: list[str]
    ) -> dict[str, list[float | None]]:
        trained = [i for i, t in enumerate(episode.turns) if t.speaker_slot == trained_slot]
        last = len(trained) - 1
        out: dict[str, list[float | None]] = {}
        for name in components:
            keywords = self._rules.get(name)
            if not keywords:
                raise AttributionError(f"no outcome rule for component {name!r}")
            hit = any(kw in episode.turns[i].utterance for i in trained for kw in keywords)
            outcome = 1.0 if hit else 0.0
            scores: list[float | None] = [None] * len(episode.turns)
            for k, i in enumerate(trained):
                scores[i] = outcome * self._gamma ** (last - k)
            out[name] = scores
        return out


# ---------------- judge 归因 ----------------

def _is_near_relative(a: str, b: str) -> bool:
    # 双向前缀:"x-sft" 评 "x" 与 "x" 评 "x-sft" 同样有偏。相同的情形被它包含。
    # 这只是保守的前缀匹配,不是真正的模型血缘判断。故意不 import
    # diagnostic.scorer 的私有函数:MindTrainer 不依赖 diagnostic,两份各自测试。
    return a.startswith(b) or b.startswith(a)


class MockTurnJudge:
    """固定表 judge。表缺键直接 KeyError,与 Plan 3 的 mock 一致:漏配就响,不给默认分。
    键是 (episode_id, turn_index, component)。"""

    def __init__(self, model_id: str, table: dict[tuple[str, int, str], float]) -> None:
        self.model_id = model_id
        self._table = dict(table)

    def score_turn(self, episode: Episode, turn_index: int, component: str) -> float:
        return self._table[(episode.id, turn_index, component)]


class JudgeAttribution:
    """judge 归因:只对训练角色位的轮次调用 judge,其余填 None。judge 返回的分数
    不在这里校验——统一由 validate_attribution 把关。"""

    def __init__(self, judge: TurnJudge, trained_model_id: str) -> None:
        if not judge.model_id or not trained_model_id:
            raise ValueError("judge.model_id and trained_model_id must be non-empty")
        if _is_near_relative(judge.model_id, trained_model_id):
            raise SelfEvalBiasError(
                f"judge model {judge.model_id!r} is too close to the trained model "
                f"{trained_model_id!r} (self-eval bias risk)"
            )
        self._judge = judge

    def attribute(
        self, episode: Episode, trained_slot: str, components: list[str]
    ) -> dict[str, list[float | None]]:
        return {
            name: [
                self._judge.score_turn(episode, t.turn_index, name)
                if t.speaker_slot == trained_slot
                else None
                for t in episode.turns
            ]
            for name in components
        }


def build_attribution_backend(
    config: RewardConfig,
    *,
    judge: TurnJudge | None = None,
    trained_model_id: str | None = None,
) -> AttributionBackend:
    """按配置组装归因后端,并核对"配置里写的 judge"与"实际传入的 judge"一致——
    配置被哈希锚定,实际用了别的 judge 就无法按配方复现。"""
    if config.attribution == "outcome_split":
        if judge is not None:
            raise ValueError("a judge was supplied but reward.attribution is outcome_split")
        return OutcomeSplitAttribution(config.outcome_rules, config.gamma)
    if config.attribution == "judge":
        if judge is None:
            raise ValueError("reward.attribution is judge but the call requires a judge")
        if trained_model_id is None:
            raise ValueError("judge attribution needs trained_model_id for the self-eval guard")
        if judge.model_id != config.judge_model_id:
            raise ValueError(
                f"judge.model_id {judge.model_id!r} does not match "
                f"reward.judge_model_id {config.judge_model_id!r}"
            )
        return JudgeAttribution(judge, trained_model_id)
    raise ValueError(f"unknown attribution {config.attribution!r}")
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run --extra dev pytest tests/test_mt_reward_pipeline.py -q`
Expected: 27 passed。

- [ ] **Step 5: 全量回归**

Run: `uv run --extra dev pytest -q`
Expected: 488 passed,无警告。

- [ ] **Step 6: 提交**

```bash
git add src/mindtrainer/reward_pipeline.py tests/test_mt_reward_pipeline.py
git commit -m "feat: mindtrainer attribution backends (outcome split, judge with bidirectional self-eval guard)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: 归因校验、组合与奖励管线入口

**Files:**
- Modify: `src/mindtrainer/reward_pipeline.py`(替换文件顶部的 import 区,并在文件末尾追加)
- Modify: `tests/test_mt_reward_pipeline.py`(替换文件顶部的 import 区,并在文件末尾追加)

**Interfaces:**
- Consumes: 任务 7 的 `AttributionBackend`、`AttributionError`;任务 4 的 `TrajectoryBatch`、`RewardedTrajectory`;任务 3 的 `RewardConfig`、`COMBINATIONS`;`simulator.dataset.episode_group_id`。
- Produces(下半段依赖这些名字):
  - `validate_attribution(episode, trained_slot, components, scores) -> None`:纯函数,不通过抛 `AttributionError`(消息含 episode id)。
  - `combine(scores, components, combination) -> list[float | None]`:纯函数,`combination` 取 `"sum"` 或 `"product"`。
  - `run_reward_pipeline(batch, config, backend, *, trained_slot) -> list[RewardedTrajectory]`
  - `summarize_rewards(rewarded) -> {"mean": float, "n": int}`

**设计要点:**
- **`validate_attribution` 校验**:分量名集合与配置完全一致;训练角色位已绑定;每个列表长度等于 `len(turns)`;`None` 恰好落在非训练角色位的轮次上;训练轮的分数是 [0,1] 内的有限数(拒绝 NaN、inf、字符串、**bool**——`True` 在 Python 里是 `int`,不单独拦会被当成 1)。**训练角色位一个发言都没有的 episode 抛错,不静默跳过**:静默跳过会让数据集悄悄变小。
- **`combine` 不做权重**——分量都在 [0,1] 上,保持最小。按 `components` 的顺序累加,结果与字典顺序无关。各分量对"哪些轮是训练轮"必须一致,否则抛 `ValueError`。
- **`combined_reward` 取训练轮的均值,不取总和**(用 `math.fsum`)。总和随 episode 变长而变大,会奖励拖长对话。**这是我的选择,spec 没规定**;有一条测试用长短两条 episode 直接守住"无长度偏置"。
- **`group_id` 来自 `simulator.dataset.episode_group_id`**,不自己再算一份:换了对手模型就不是同一组。
- `summarize_rewards` 的结果写进训练曲线摘要。spec §2.5.6 的"训练奖励 vs 诊断分背离"检测靠这条曲线,MindTrainer 只记录,**不读诊断**。空输入报错而不是返回 NaN。
- `run_reward_pipeline` 对空 batch 返回 `[]`(Interact 的某一步可能没有可用样本,由调用方决定怎么处理)。

- [ ] **Step 1: 追加失败测试**

把 `tests/test_mt_reward_pipeline.py` 顶部的 import 区(从第一行 `import pytest` 到 `from simulator.datamodel import Episode, RoleAssignment, RoleBinding, TurnRecord` 为止)整体替换为:

```python
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
```

再在文件末尾追加:

```python
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
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run --extra dev pytest tests/test_mt_reward_pipeline.py -q`
Expected: 收集阶段报错 `ImportError: cannot import name 'combine' from 'mindtrainer.reward_pipeline'`。

- [ ] **Step 3: 写实现**

先把 `src/mindtrainer/reward_pipeline.py` 顶部的 import 区(从 `from __future__ import annotations` 到 `from simulator.datamodel import Episode` 为止)整体替换为:

```python
from __future__ import annotations

import math
from typing import Protocol

from mindtrainer.datamodel import RewardedTrajectory, TrajectoryBatch
from mindtrainer.training_config import COMBINATIONS, RewardConfig
from simulator.datamodel import Episode
from simulator.dataset import episode_group_id
```

再在文件末尾追加:

```python
# ---------------- 校验与组合 ----------------

def validate_attribution(
    episode: Episode,
    trained_slot: str,
    components: list[str],
    scores: dict[str, list[float | None]],
) -> None:
    """纯函数,不通过抛 AttributionError。训练角色位一个发言都没有的 episode 抛错,
    不静默跳过——静默跳过会让数据集悄悄变小。"""
    eid = episode.id
    if set(scores) != set(components):
        raise AttributionError(
            f"episode {eid}: attribution components {sorted(scores)} "
            f"!= configured {sorted(components)}"
        )
    if trained_slot not in episode.role_assignment.slot_ids():
        raise AttributionError(f"episode {eid}: slot {trained_slot!r} is not bound")
    trained = [t.speaker_slot == trained_slot for t in episode.turns]
    if not any(trained):
        raise AttributionError(f"episode {eid}: no turns from trained slot {trained_slot!r}")
    for name in components:
        values = scores[name]
        if len(values) != len(episode.turns):
            raise AttributionError(
                f"episode {eid}: component {name!r} has {len(values)} scores "
                f"for {len(episode.turns)} turns"
            )
        for i, (value, is_trained) in enumerate(zip(values, trained)):
            if not is_trained:
                if value is not None:
                    raise AttributionError(
                        f"episode {eid}: component {name!r} turn {i}: "
                        f"non-trained turn must be None, got {value!r}"
                    )
                continue
            if value is None:
                raise AttributionError(
                    f"episode {eid}: component {name!r} turn {i}: trained turn has no score"
                )
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not (0.0 <= value <= 1.0)
            ):
                raise AttributionError(
                    f"episode {eid}: component {name!r} turn {i}: "
                    f"score {value!r} is not a finite number in [0, 1]"
                )


def combine(
    scores: dict[str, list[float | None]], components: list[str], combination: str
) -> list[float | None]:
    """逐轮把各分量按 sum 或 product 合成。不做权重——分量都在 [0,1] 上,保持最小。
    按 components 的顺序累加,结果与字典顺序无关。"""
    if combination not in COMBINATIONS:
        raise ValueError(f"combination must be one of {list(COMBINATIONS)}, got {combination!r}")
    if not components:
        raise ValueError("components must be non-empty")
    lengths = {len(scores[name]) for name in components}
    if len(lengths) != 1:
        raise ValueError(f"components have different lengths: {sorted(lengths)}")
    out: list[float | None] = []
    for i in range(lengths.pop()):
        values = [scores[name][i] for name in components]
        if all(v is None for v in values):
            out.append(None)
        elif any(v is None for v in values):
            raise ValueError(f"turn {i}: components disagree on whether it is a trained turn")
        elif combination == "sum":
            out.append(sum(values))
        else:
            out.append(math.prod(values))
    return out


# ---------------- 入口 ----------------

def run_reward_pipeline(
    batch: TrajectoryBatch,
    config: RewardConfig,
    backend: AttributionBackend,
    *,
    trained_slot: str,
) -> list[RewardedTrajectory]:
    """批到批的变换,每次训练迭代都可重跑。combined_reward 取训练轮的**均值**,
    不取总和:总和随 episode 变长而变大,会奖励拖长对话。"""
    components = list(config.components)
    out: list[RewardedTrajectory] = []
    for episode in batch.episodes:
        scores = backend.attribute(episode, trained_slot, components)
        validate_attribution(episode, trained_slot, components, scores)
        per_turn = combine(scores, components, config.combination)
        trained_rewards = [r for r in per_turn if r is not None]
        out.append(
            RewardedTrajectory(
                episode=episode,
                group_id=episode_group_id(episode),
                per_turn_rewards=per_turn,
                combined_reward=math.fsum(trained_rewards) / len(trained_rewards),
            )
        )
    return out


def summarize_rewards(rewarded: list[RewardedTrajectory]) -> dict:
    """写进训练曲线摘要。"训练奖励 vs 诊断分背离"的检测靠这条曲线,
    MindTrainer 只记录,不读诊断。"""
    if not rewarded:
        raise ValueError("cannot summarize zero trajectories")
    return {
        "mean": math.fsum(r.combined_reward for r in rewarded) / len(rewarded),
        "n": len(rewarded),
    }
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run --extra dev pytest tests/test_mt_reward_pipeline.py -q`
Expected: 59 passed(任务 7 的 27 + 本任务新增 32)。

- [ ] **Step 5: 全量回归**

Run: `uv run --extra dev pytest -q`
Expected: 520 passed,无警告。

- [ ] **Step 6: 提交**

```bash
git add src/mindtrainer/reward_pipeline.py tests/test_mt_reward_pipeline.py
git commit -m "feat: mindtrainer reward pipeline (attribution validation, sum/product combine, mean reward)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: GRPO 分组与优势

**Files:**
- Create: `src/mindtrainer/grouping.py`
- Test: `tests/test_mt_grouping.py`

**Interfaces:**
- Consumes: 任务 4 的 `RewardedTrajectory`。
- Produces(下半段的执行器与编排器依赖这些名字):
  - `DegenerateGroupError(Exception)`:属性 `group_ids: list[str]`、`min_group_size: int`。
  - `group_by_id(rewarded) -> dict[str, list[RewardedTrajectory]]`:组的顺序是首次出现的顺序,组内保持输入顺序。
  - `group_advantages(groups: dict[str, list[float]]) -> dict[str, list[float]]`:优势 = 奖励 − 组内均值。
  - `trajectory_advantages(rewarded) -> list[float]`:与输入顺序逐条对齐的优势(不同组的样本可以交错)。
  - `count_degenerate_groups(groups) -> int`:组内零方差的组数,供 `n_degenerate_groups` 指标。
  - `validate_groups(groups, min_group_size) -> None`:有组成员数不足就抛 `DegenerateGroupError`。

**设计要点(设计 §5.1):**
- **基线取组内均值,不除标准差**——那是真实执行器通过 `grpo_config` 自己决定的事。各组基线互相独立。
- **两种退化要分开对待**:
  - **组太小**(成员数 < `min_group_size`,含只有 1 条)时优势恒为 0,训练会静默地什么都学不到,所以是**错误**:`validate_groups` 抛 `DegenerateGroupError`,**列出全部不合格组的 id**(字典序),不静默训练。
  - **组内零方差**(成员数够、奖励全相同)是数据的正常情形,**不报错**,只由 `count_degenerate_groups` 计数,供观察。
- **零方差组的优势必须精确为 0.0**:验证时发现,三个 0.7 的均值是 `0.7 + 1.1e-16`,不吸附的话舍入误差会被当成梯度信号喂给执行器。判定容差 `_EPS = 1e-12`,让 `0.1 + 0.2` 与 `0.3` 这类等价写法也算零方差。均值用 `math.fsum`。
- **NaN/inf 与空组抛 `ValueError`**:NaN 会毒化整组均值。
- **`min_group_size < 2` 本身就没有意义**,`validate_groups` 直接抛 `ValueError`(配置校验已拦过一次,这里是兜底)。
- **调用时机属于下半段的编排器**:Batch 在 GRPO 阶段启动前对整份数据集校验一次;Interact 每步对刚收集的组校验。本任务只提供纯函数。

- [ ] **Step 1: 写失败测试**

`tests/test_mt_grouping.py`:

```python
import math

import pytest

from mindtrainer.datamodel import RewardedTrajectory
from mindtrainer.grouping import (
    DegenerateGroupError, count_degenerate_groups, group_advantages, group_by_id,
    trajectory_advantages, validate_groups,
)
from simulator.datamodel import Episode, RoleAssignment, RoleBinding, TurnRecord


def _rt(group_id, reward, ep_id):
    assignment = RoleAssignment(
        "s1",
        [RoleBinding("a", "pa", "self_play"),
         RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")],
        max_turns=2,
    )
    episode = Episode(
        ep_id, "s1", assignment, [TurnRecord(0, "a", "u"), TurnRecord(1, "b", "v")], "max_turns",
    )
    return RewardedTrajectory(episode, group_id, [reward, None], reward)


# ---------------- group_by_id ----------------

def test_group_by_id_keeps_first_seen_group_order_and_in_group_order():
    items = [_rt("g2", 0.1, "e1"), _rt("g1", 0.2, "e2"), _rt("g2", 0.3, "e3"), _rt("g1", 0.4, "e4")]
    groups = group_by_id(items)
    assert list(groups) == ["g2", "g1"]
    assert [r.episode.id for r in groups["g2"]] == ["e1", "e3"]
    assert [r.episode.id for r in groups["g1"]] == ["e2", "e4"]


def test_group_by_id_of_nothing_is_empty():
    assert group_by_id([]) == {}


# ---------------- group_advantages ----------------

def test_advantage_is_reward_minus_the_group_mean():
    assert group_advantages({"g": [1.0, 0.0, 0.5]}) == {"g": [0.5, -0.5, 0.0]}


def test_each_group_has_its_own_independent_baseline():
    out = group_advantages({"g1": [1.0, 0.0], "g2": [0.2, 0.4]})
    assert out["g1"] == [0.5, -0.5]
    assert out["g2"] == pytest.approx([-0.1, 0.1])


@pytest.mark.parametrize("rewards", [[0.3, 0.9, 0.1, 0.5], [2.0, 2.0], [1, 0, 0]])
def test_advantages_within_a_group_sum_to_zero(rewards):
    assert sum(group_advantages({"g": rewards})["g"]) == pytest.approx(0.0, abs=1e-12)


def test_identical_rewards_give_all_zero_advantages():
    assert group_advantages({"g": [0.7, 0.7, 0.7]}) == {"g": [0.0, 0.0, 0.0]}


def test_zero_variance_advantages_are_exactly_zero_for_equivalent_float_spellings():
    assert group_advantages({"g": [0.1 + 0.2, 0.3]}) == {"g": [0.0, 0.0]}


@pytest.mark.parametrize(
    "bad,needle",
    [([], "empty"), ([0.5, math.nan], "non-finite"), ([math.inf, 0.5], "non-finite")],
    ids=["empty", "nan", "inf"],
)
def test_group_advantages_rejects_unusable_groups(bad, needle):
    with pytest.raises(ValueError, match=needle):
        group_advantages({"g": bad})


# ---------------- trajectory_advantages ----------------

def test_trajectory_advantages_line_up_with_input_order_when_groups_interleave():
    items = [
        _rt("g1", 1.0, "e1"), _rt("g2", 0.0, "e2"),
        _rt("g1", 0.0, "e3"), _rt("g2", 0.5, "e4"),
    ]
    assert trajectory_advantages(items) == [0.5, -0.25, -0.5, 0.25]


def test_trajectory_advantages_of_nothing_is_empty():
    assert trajectory_advantages([]) == []


# ---------------- count_degenerate_groups ----------------

def test_zero_variance_groups_are_counted():
    groups = {"flat": [1.0, 1.0, 1.0], "varied": [0.0, 1.0]}
    assert count_degenerate_groups(groups) == 1


def test_equivalent_floating_point_spellings_count_as_zero_variance():
    assert count_degenerate_groups({"g": [0.1 + 0.2, 0.3]}) == 1


def test_a_single_member_group_is_trivially_zero_variance():
    assert count_degenerate_groups({"g": [0.4]}) == 1


def test_no_degenerate_groups_counts_zero():
    assert count_degenerate_groups({"g1": [0.0, 1.0], "g2": [0.2, 0.9]}) == 0


def test_count_degenerate_groups_rejects_an_empty_group():
    with pytest.raises(ValueError, match="empty"):
        count_degenerate_groups({"g": []})


# ---------------- validate_groups ----------------

def test_groups_of_exactly_min_size_pass():
    validate_groups({"g1": [1, 2], "g2": [1, 2, 3]}, min_group_size=2)


def test_too_small_groups_are_listed_by_id_in_sorted_order():
    with pytest.raises(DegenerateGroupError) as exc:
        validate_groups({"gb": [1], "ga": [1], "gc": [1, 2]}, min_group_size=2)
    assert exc.value.group_ids == ["ga", "gb"]
    assert exc.value.min_group_size == 2
    assert "ga" in str(exc.value) and "gb" in str(exc.value) and "gc" not in str(exc.value)


def test_a_larger_min_group_size_flags_groups_that_were_fine_at_two():
    groups = {"g1": [1, 2], "g2": [1, 2, 3]}
    validate_groups(groups, min_group_size=2)
    with pytest.raises(DegenerateGroupError) as exc:
        validate_groups(groups, min_group_size=3)
    assert exc.value.group_ids == ["g1"]


def test_zero_variance_does_not_trigger_the_size_check():
    validate_groups({"g": [0.5, 0.5]}, min_group_size=2)


@pytest.mark.parametrize("bad", [1, 0, -1])
def test_min_group_size_below_two_is_itself_rejected(bad):
    with pytest.raises(ValueError, match="min_group_size must be >= 2"):
        validate_groups({"g": [1, 2]}, min_group_size=bad)
```

- [ ] **Step 2: 运行,确认失败**

Run: `uv run --extra dev pytest tests/test_mt_grouping.py -q`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindtrainer.grouping'`。

- [ ] **Step 3: 写实现**

`src/mindtrainer/grouping.py`:

```python
from __future__ import annotations

import math

from mindtrainer.datamodel import RewardedTrajectory

# 零方差判定的容差。奖励由同一套确定性计算得出,相同输入得到逐位相同的浮点数;
# 容差只是为了 0.1+0.2 与 0.3 这类等价写法不被误判成"有差异"。
_EPS = 1e-12


class DegenerateGroupError(Exception):
    """有组的成员数不足 min_group_size。一组只有 1 条时优势恒为 0,训练会静默地
    什么都学不到,所以这是错误,不是警告。group_ids 列出全部不合格的组。"""

    def __init__(self, group_ids: list[str], min_group_size: int) -> None:
        super().__init__(
            f"{len(group_ids)} group(s) smaller than min_group_size={min_group_size}: "
            f"{list(group_ids)}"
        )
        self.group_ids = list(group_ids)
        self.min_group_size = min_group_size


def group_by_id(
    rewarded: list[RewardedTrajectory],
) -> dict[str, list[RewardedTrajectory]]:
    """按 group_id 分组。组的顺序是首次出现的顺序,组内顺序保持输入顺序。"""
    groups: dict[str, list[RewardedTrajectory]] = {}
    for r in rewarded:
        groups.setdefault(r.group_id, []).append(r)
    return groups


def _checked(group_id: str, rewards: list[float]) -> list[float]:
    if not rewards:
        raise ValueError(f"group {group_id!r} is empty")
    if not all(math.isfinite(r) for r in rewards):
        raise ValueError(f"group {group_id!r} has a non-finite reward: {rewards}")
    return rewards


def group_advantages(groups: dict[str, list[float]]) -> dict[str, list[float]]:
    """组内优势:基线取该组 combined_reward 的均值,优势 = reward - mean。

    不除标准差——那是真实执行器通过 grpo_config 自己决定的事。各组的基线互相独立。
    零方差组(奖励在 _EPS 内相同)的优势精确为 0.0。
    空组或含 NaN/inf 的组抛 ValueError(NaN 会毒化整组均值)。"""
    out: dict[str, list[float]] = {}
    for group_id, rewards in groups.items():
        _checked(group_id, rewards)
        if max(rewards) - min(rewards) <= _EPS:
            # 零方差组的优势必须精确为 0:三个 0.7 的均值是 0.7+1e-16,
            # 不吸附的话舍入误差会被当成梯度信号喂给执行器。
            out[group_id] = [0.0] * len(rewards)
            continue
        baseline = math.fsum(rewards) / len(rewards)
        out[group_id] = [r - baseline for r in rewards]
    return out


def trajectory_advantages(rewarded: list[RewardedTrajectory]) -> list[float]:
    """与输入顺序逐条对齐的优势列表(不同组的样本可以交错出现)。"""
    groups = group_by_id(rewarded)
    advantages = group_advantages(
        {gid: [r.combined_reward for r in members] for gid, members in groups.items()}
    )
    cursors = {gid: iter(values) for gid, values in advantages.items()}
    return [next(cursors[r.group_id]) for r in rewarded]


def count_degenerate_groups(groups: dict[str, list[float]]) -> int:
    """组内零方差(奖励全相同)的组数,供训练曲线的 n_degenerate_groups 指标。

    这不是错误:成员数够、只是奖励恰好一样,是数据的正常情形,只计数不报错。
    单成员组天然零方差,也会被计入(但 validate_groups 应先把它们拦下)。"""
    return sum(
        1
        for group_id, rewards in groups.items()
        if max(_checked(group_id, rewards)) - min(rewards) <= _EPS
    )


def validate_groups(groups: dict[str, list], min_group_size: int) -> None:
    """有组成员数不足 min_group_size 就抛 DegenerateGroupError(列出全部不合格组 id,
    按字典序)。零方差组不触发它。min_group_size < 2 本身就没有意义,抛 ValueError。"""
    if min_group_size < 2:
        raise ValueError(f"min_group_size must be >= 2, got {min_group_size}")
    too_small = sorted(gid for gid, members in groups.items() if len(members) < min_group_size)
    if too_small:
        raise DegenerateGroupError(too_small, min_group_size)
```

- [ ] **Step 4: 运行,确认通过**

Run: `uv run --extra dev pytest tests/test_mt_grouping.py -q`
Expected: 26 passed。

- [ ] **Step 5: 全量回归**

Run: `uv run --extra dev pytest -q`
Expected: 546 passed,无警告。

- [ ] **Step 6: 提交**

```bash
git add src/mindtrainer/grouping.py tests/test_mt_grouping.py
git commit -m "feat: mindtrainer GRPO grouping (group-mean advantages, loud on undersized groups, count zero-variance)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## 上半段完成标准与交接

**完成标准**

- `uv run --extra dev pytest -q` 为 546 passed,无警告。
- `src/mindtrainer/` 下有 `__init__.py`、`training_config.py`、`datamodel.py`、`trajectory_source.py`、`reward_pipeline.py`、`grouping.py`;`src/simulator/realtime/client.py` 多了 `turns` 与 `abort()`;`src/controlplane/paths.py` 多了 `training_run_file` 与 `candidate_checkpoint_file`;`pyproject.toml` 的 wheel 包列表含 `src/mindtrainer`。
- 本计划里的代码在编写时已在隔离副本上逐任务做过红绿验证,并对 33 处关键断言做过变异验证(逐处植入回归,确认对应测试变红),全部被测试捕获。执行时若某个新测试在"实现尚不存在"时意外通过,先怀疑它是不是空转。

**下半段依赖的名字(签名以本计划为准)**

| 来源 | 名字 |
|---|---|
| `mindtrainer.training_config` | `TrainingConfig`、`RewardConfig`、`validate_training_config`、`ensure_valid_config`、`InvalidTrainingConfigError`、`config_hash`、`total_steps`、`phase_at`、`load_config_from_directive` |
| `mindtrainer.datamodel` | `TrajectoryBatch`、`RewardedTrajectory`(下半段往同一文件加 `StepResult`、`TrainingRun`) |
| `mindtrainer.trajectory_source` | `TrajectorySource`、`BatchTrajectorySource`、`InteractTrajectorySource(..., start_step=0)`、`RolloutFailedError`、`batch_for_step` |
| `mindtrainer.reward_pipeline` | `AttributionBackend`、`build_attribution_backend`、`run_reward_pipeline`、`summarize_rewards`、`AttributionError`、`SelfEvalBiasError` |
| `mindtrainer.grouping` | `group_by_id`、`trajectory_advantages`、`group_advantages`、`count_degenerate_groups`、`validate_groups`、`DegenerateGroupError` |
| `controlplane.paths` | `training_run_file`、`candidate_checkpoint_file` |

**交接时要注意的三点**

1. `batch_for_step` 放在 `trajectory_source.py`(Interact 轨迹源要用它轮转 scenario),下半段的续训测试与编排器从那里 import,不要另写一份。
2. `InteractTrajectorySource` 的 `start_step` 是**已完成的 GRPO step 数**,不是全局 step。续训时编排器传 `global_step - sft_steps`(不小于 0)。
3. `SelfEvalBiasError` 是 `mindtrainer.reward_pipeline` 自己的类,与 `diagnostic.scorer.SelfEvalBiasError` 同名但互不相干;MindTrainer 全程不 import `diagnostic.*`。
