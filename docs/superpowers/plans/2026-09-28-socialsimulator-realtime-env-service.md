# SocialSimulator 实时环境服务(§2.6 gRPC)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 Rise SocialSimulator 的**实时环境服务模块**(spec §2.6)——一个常驻 gRPC 双向流服务,把已合入 main 的纯函数 Simulation Runner 核心(`run_episode`)包装成支持多轮交互、会话隔离、异常检测的实时服务,供未来 MindTrainer 的 InteractTrain(受限接口①)与 MindAgent 的经验采集(受限接口②)调用。产出:完整的 gRPC 消息协议 + 会话状态机 + 会话驱动器 + 服务端 + 客户端包装器,端到端在真实本地 socket 上验证,复用同一个 `run_episode` 核心(不重新实现轮次驱动逻辑)。

**Architecture:** 计算层子系统,Python 侧。协议层用 Protocol Buffers + gRPC 双向流(spec 补充 C.1 已定技术选型)。核心设计:服务端为每个会话在后台线程里驱动 `run_episode`(本计划扩展其签名新增 `on_turn` 回调,向后兼容,与 Plan 3 扩展 `stop_predicate` 同一模式),客户端驱动的角色位(如被训练模型的自博弈角色)由一个新的 `ClientDrivenBackend` 实现 `ModelBackend` Protocol——其 `respond()` 阻塞等待 gRPC 主循环转发来的客户端消息,从而让 `run_episode` 的轮次驱动逻辑**完全不变**即可支持"部分角色由跨进程客户端实时驱动"。服务端主循环(`Interact` 方法)是一个简单的"收一条 ClientMessage → 路由到会话驱动器 → 转发驱动器产出的 ServerMessage"循环,不做自己的轮次计数,所有轮次语义都在 `run_episode` 内部。

**Tech Stack:**
- Python 3.11+ / uv / pytest — 与既有 ControlPlane/SocialSimulator/DiagnosticEval 同侧
- **gRPC 双向流**(`grpcio` + `protobuf`,`grpcio-tools` 仅作为 dev 依赖用于生成代码,生成产物 `_pb2.py`/`_pb2_grpc.py`/`_pb2.pyi` 提交入库,测试/运行时不依赖 `grpcio-tools`)——spec 补充 C.1 已定技术选型,本计划是首次引入 gRPC,故为唯一新增运行时依赖
- `google.protobuf.Struct` 仅用于 `activation_trace`(天然无固定 schema 的探索性字段,§2.6.3);所有其他字段(角色分配、人格、轮次记录等)用**强类型 proto message**,不套 `Struct`——`Struct` 会把整数字段静默转成浮点数(本计划已验证并在 codec 测试中作回归防护),强类型字段没有这个问题
- 标准库 `threading`/`queue`/`uuid` 做会话驱动的线程桥接,零新第三方并发依赖

**Spec:**
- `docs/superpowers/specs/2026-09-24-rise-subsystem-design.md`(七子系统详细设计,本计划实现其 **§2.6 实时环境服务接口协议设计** + 补充 C.1/C.2)
- `docs/superpowers/specs/2026-09-22-rise-architecture-design.md`(总体架构)

**依赖的既有实现(Plan 1 ControlPlane + Plan 2 SocialSimulator 批量模块,均已合入 main):**
- `simulator.datamodel`:`Persona(id, traits, motivation, speech_style, values, source="auto")`、`RoleSlot(slot_id, description)`、`Scenario(id, background, role_slots, conflict, norms, purpose, source="auto")`、`RoleBinding(slot_id, persona_id, model_backend)`、`RoleAssignment(scenario_id, bindings, max_turns)`(`.slot_ids() -> set[str]`)、`TurnRecord(turn_index, speaker_slot, utterance, inner_thought=None, activation_trace=None)`、`Episode(id, scenario_id, role_assignment, turns, terminated_reason)`。均带对称 `to_dict`/`from_dict`。
- `simulator.runner.run_episode(scenario, assignment, personas, backends, episode_id, stop_predicate=None) -> Episode`:纯函数,round-robin 驱动 `assignment.bindings`,每轮调 `backend.respond(persona, scenario, history, speaker_slot) -> (utterance, inner_thought)`,`stop_predicate(turns) -> bool` 在每轮 append 后判定,触发则 `terminated_reason="goal_reached"` 提前返回;否则跑满 `max_turns` 返回 `terminated_reason="max_turns"`。校验:未知 slot 抛 `RoleSlotMismatchError`,空 bindings 抛同一异常,缺失 persona 抛 `KeyError`。**本计划新增第二个可选参数 `on_turn`**(见 Task 3)。
- `simulator.backends.ModelBackend` Protocol:`respond(persona, scenario, history, speaker_slot) -> tuple[str, str|None]`。`ScriptedBackend(scripted: dict[str, list[str]])` 为 V1 确定性实现,按 slot_id 出队罐头话语。
- `simulator.store_adapter.write_episode(store: GitStore, run_id: str, episode: Episode) -> str`:把 Episode 包成 `type="episode"` 信封写入 `episode_file(run_id, episode.id)`。`read_episode(store, run_id, episode_id) -> Episode` 读回。
- `controlplane.gitstore.GitStore(repo_root)`:`.init()`、`.write_product`、`.read_product`、`.exists`、`.list_products`,均幂等/走 `migration.load_product`。
- `controlplane.paths`:`episode_file(run_id, episode_id)`、`episodes_dir(run_id)`、`run_dir(run_id)` 已存在,本计划不新增路径。

---

## Global Constraints

每个任务的要求都隐含包含本节。以下值直接取自 spec,不可改动:

- **协议选型**:实时环境服务用 **gRPC 双向流**(spec 补充 C.1),不用 REST/WebSocket——理由是高频、低延迟、需要在多轮对话中维持会话状态,且强类型 schema 天然契合"角色-模型后端映射参数必须显式"的既定约束。
- **共享同一个 Simulation Runner 核心**(spec §2.1、补充 C.2):实时服务**复用** `run_episode`,不重新实现轮次驱动逻辑。批量模块与实时模块是**分离的操作系统进程**,只共享代码库,不共享运行时状态——本计划的服务进程与既有批量模块(`simulator.stubs`/`simulator.generators`)各自独立,不共享任何可变全局状态。
- **角色-模型后端映射必须由调用方显式指定**(spec §2.2 关键接口约定):`CreateSession` 必须携带完整 `RoleAssignment`,不由服务内部写死。
- **会话生命周期状态机**(spec §2.6.1):`CREATED → ACTIVE → COMPLETING → CLOSED`,异常路径 `→ FAILED`(可从 CREATED/ACTIVE/COMPLETING 进入)。CREATED 到 ACTIVE 之间**无额外确认步骤**——`CreateSession` 校验通过即直接 ACTIVE,不做"预检+确认"两次往返。
- **TurnUpdate 每轮完成后立即推送**(spec §2.6.2 硬约束),不是攒够整段对话再返回——这是本计划扩展 `run_episode` 新增 `on_turn` 回调的直接依据。
- **激活轨迹字段协议层强制项**(spec §2.6.3):`TurnRecordProto` 必须为 `activation_trace` 预留结构化字段;裸模型(V1 全部角色)时该字段**语义上为空**(proto3 `optional` 未设置,`HasField()` 返回 `False`),区别于"激活轨迹为空列表"这种不同语义(留给 SimpleActio 计划落地时用)。
- **并发会话隔离**(spec §2.6.4 硬约束):每个会话有独立 `session_id` 和独立服务端状态,会话间**不共享任何可变状态**。
- **异常路径判定必须显式区分**(spec §2.6.5):服务端侧异常(角色后端调用失败)主动发 `EndSession`(带终止原因),不是简单断连;客户端侧异常(进程崩溃/连接中断)靠流迭代器异常/结束检测,标记会话 FAILED,不需要客户端配合;超过最大轮次仍未终止由 `run_episode` 自身的 `max_turns` 语义兜底。
- **可测试性**(spec §2.6.8):会话状态机是纯状态转移逻辑,可完全脱离真实 gRPC 连接单测。并发隔离用构造的多会话并发场景测试状态不串。异常路径可用 mock/受控输入触发,不需要真实拉起远程服务——本计划的失败路径测试用真实 `127.0.0.1` 本地 socket(轻量、确定性、不依赖外部网络),不算"远程服务"。
- **纯函数优先**:会话状态机(`session.py`)、codec 转换(`codec.py`)全部纯函数;有副作用的只有 `ClientDrivenBackend`(线程间队列)、`SessionDriver`(后台线程)、`EnvironmentServicer`(gRPC I/O)、可选的 ControlPlane 持久化钩子。
- **依赖最小化**:本计划是全项目**首次**引入运行时第三方依赖(`grpcio`/`protobuf`)——这是 spec 补充 C.1 明确选型的直接后果,不可用 stdlib 替代(gRPC 协议本身就是选型)。`grpcio-tools` 仅进 dev 依赖,生成产物提交入库,CI/测试/运行时不需要重新生成。
- **不做范围**:本计划**不**实现 spec §2.2 提到的"开发者工具接口"(人工 CLI 交互式调试,非流水线一环)——这是独立的 dev-only 功能,推迟到需要时再做。**不**实现 spec 补充 E 的完整失败分类学自动重试机制(T1 有限次重试)——本计划正确**分类并上报** T1(超时)/T2(角色后端失败)/客户端断连三类异常,但不做自动重试,重试策略留给未来调用方(MindTrainer/MindAgent)或专门的失败处理计划。**不**实现 `FailureRecord` 产物持久化(spec 补充 E.5)——这是跨子系统的横切设计,超出本计划边界;FAILED 会话只在协议层正确上报,不写入 ControlPlane。

---

## File Structure

**Python 包 `src/simulator/realtime/`(实时环境服务,新增)**
- `proto/realtime_env.proto` — gRPC 消息与服务定义(手写,唯一非生成源文件)
- `proto/realtime_env_pb2.py`、`proto/realtime_env_pb2_grpc.py`、`proto/realtime_env_pb2.pyi` — **生成代码,提交入库**(见 Task 1 生成命令)
- `session.py` — 会话状态机(§2.6.1):纯函数,`SessionState` 常量 + 转移表 + `can_transition`/`next_states`/`assert_transition`
- `codec.py` — dataclass ↔ proto message 双向转换:`Persona`/`RoleBinding`/`RoleAssignment`/`TurnRecord` 各自的 `*_to_proto`/`*_from_proto`
- `backend.py` — `ClientDrivenBackend`(实现 `ModelBackend` Protocol,阻塞等待跨进程客户端消息)
- `driver.py` — `SessionDriver`:后台线程驱动 `run_episode(..., on_turn=..., stop_predicate=...)`,用 `inbox`/`outbox` 队列与 gRPC 主循环解耦
- `servicer.py` — `EnvironmentServicer`(`EnvironmentServiceServicer` 子类):`Interact` 方法,会话注册表,路由 `ClientMessage` → 对应 `SessionDriver`
- `server.py` — `create_server(backends_factory, *, port=0, turn_timeout_s=30.0, on_session_closed=None) -> grpc.Server` 服务端组装
- `client.py` — `EnvironmentServiceClient`:同步直接 `next()` 的客户端包装器,累积 `TurnRecord` 并在 `EndSession` 时在本地重建完整 `Episode`

**扩展既有(Plan 2,已合入 main)**
- `src/simulator/runner.py` — `run_episode` 新增可选参数 `on_turn: Callable[[TurnRecord], None] | None = None`,每轮 append 后、`stop_predicate` 判定前调用(向后兼容:不传则行为完全不变)

**开发工具**
- `scripts/gen_realtime_proto.sh` — 重新生成 `proto/*_pb2*.py` 的一行脚本,记录确切 protoc 命令,避免命令细节遗失

**依赖变更**
- `pyproject.toml`:`dependencies` 新增 `grpcio>=1.84.0`、`protobuf>=7.35.1,<8.0.0`;`optional-dependencies.dev` 新增 `grpcio-tools>=1.84.0`(仅 codegen 用,`tool.hatch.build.targets.wheel.packages` 无需改动——`src/simulator` 已在列表中,hatchling 自动收纳新子包,已验证)

**测试**
- `tests/test_realtime_session.py` — 状态机纯函数
- `tests/test_realtime_codec.py` — proto 转换往返 + 整数不被 `Struct` 转成浮点数的回归测试
- `tests/test_realtime_runner_on_turn.py` — `run_episode` 的 `on_turn` 回调 + 向后兼容回归保护
- `tests/test_realtime_backend.py` — `ClientDrivenBackend` 阻塞/超时行为(纯 Python 队列,不起 gRPC)
- `tests/test_realtime_driver.py` — `SessionDriver` 进程内驱动(不经真实 socket)
- `tests/test_realtime_servicer_e2e.py` — 真实 `127.0.0.1` gRPC socket:正常会话、超时、后端失败、客户端主动结束、断连、并发隔离
- `tests/test_realtime_client.py` — 客户端包装器 + Episode 重建与 `run_episode` 直接输出的一致性(parity)
- `tests/test_realtime_persistence.py` — 会话关闭后持久化钩子写入 ControlPlane(复用既有 `write_episode`)

---

### Task 1: gRPC 消息协议定义 + 生成代码 + 依赖接线

**Files:** `src/simulator/realtime/__init__.py`(空)、`src/simulator/realtime/proto/__init__.py`(空)、`src/simulator/realtime/proto/realtime_env.proto`、`src/simulator/realtime/proto/realtime_env_pb2.py`(生成)、`src/simulator/realtime/proto/realtime_env_pb2_grpc.py`(生成)、`src/simulator/realtime/proto/realtime_env_pb2.pyi`(生成)、`scripts/gen_realtime_proto.sh`、`pyproject.toml`(修改)、`tests/test_realtime_proto_smoke.py`

**Interfaces:**
- Consumes: 无(首个任务)
- Produces: `realtime_env_pb2` 模块导出 `CreateSession`/`SessionReady`/`SubmitTurn`/`TurnRecordProto`/`RoleBindingProto`/`RoleAssignmentProto`/`PersonaProto`/`TurnUpdate`/`EndSession`/`ClientMessage`/`ServerMessage` 消息类;`realtime_env_pb2_grpc` 导出 `EnvironmentServiceServicer`/`EnvironmentServiceStub`/`add_EnvironmentServiceServicer_to_server`

- [ ] **Step 1: 写协议定义**

`src/simulator/realtime/proto/realtime_env.proto`:
```protobuf
syntax = "proto3";

package rise.realtime;

import "google/protobuf/struct.proto";

// 人格模板(镜像 simulator.datamodel.Persona)
message PersonaProto {
  string id = 1;
  repeated string traits = 2;
  string motivation = 3;
  string speech_style = 4;
  repeated string values = 5;
  string source = 6;
}

// 角色绑定(镜像 simulator.datamodel.RoleBinding)
message RoleBindingProto {
  string slot_id = 1;
  string persona_id = 2;
  string model_backend = 3;  // "self_play" | "frozen_reference" | "external_api"
}

// 角色分配(镜像 simulator.datamodel.RoleAssignment)
message RoleAssignmentProto {
  string scenario_id = 1;
  repeated RoleBindingProto bindings = 2;
  int32 max_turns = 3;
}

// 轮次记录(镜像 simulator.datamodel.TurnRecord)。activation_trace 用 Struct——
// 唯一天然无固定 schema 的字段(§2.6.3);裸模型时不设置该字段(HasField=false)。
message TurnRecordProto {
  int32 turn_index = 1;
  string speaker_slot = 2;
  string utterance = 3;
  optional string inner_thought = 4;
  optional google.protobuf.Struct activation_trace = 5;
}

// 客户端 → 服务端:发起会话。personas 覆盖 RoleAssignment 涉及的全部 persona_id。
// client_driven_slot 指定由客户端实时驱动的角色位(V1 每会话恰好一个,
// 对应 MindTrainer 自博弈角色 或 MindAgent 自身角色);其余角色位由服务端
// 的 backends_factory 提供的 ModelBackend 驱动。
message CreateSession {
  string scenario_id = 1;
  RoleAssignmentProto role_assignment = 2;
  map<string, PersonaProto> personas = 3;
  string purpose = 4;  // "training" | "deployment_simulation"
  string client_driven_slot = 5;
}

// 服务端 → 客户端:会话已进入 ACTIVE。
message SessionReady {
  string session_id = 1;
}

// 客户端 → 服务端:为 client_driven_slot 当前这一轮提交话语。
message SubmitTurn {
  string utterance = 1;
  optional string inner_thought = 2;
}

// 服务端 → 客户端:某一轮完成,立即推送(§2.6.2 硬约束,不攒批)。
message TurnUpdate {
  TurnRecordProto turn = 1;
}

// 双向:会话终止。episode_id 是摘要引用(§2.6.2"最终Episode摘要引用")——
// 客户端已通过逐轮 TurnUpdate 拿到全部 TurnRecord,可在本地用
// episode_id + reason 重建完整 Episode,协议不重复内联整个 Episode。
message EndSession {
  string reason = 1;  // "max_turns" | "goal_reached" | "client_terminated"
                       // | "role_backend_failure" | "timeout" | "invalid_session"
  string episode_id = 2;
  optional string failure_detail = 3;
}

message ClientMessage {
  oneof payload {
    CreateSession create_session = 1;
    SubmitTurn submit_turn = 2;
    EndSession end_session = 3;  // 客户端主动终止
  }
}

message ServerMessage {
  oneof payload {
    SessionReady session_ready = 1;
    TurnUpdate turn_update = 2;
    EndSession end_session = 3;  // 服务端终止或确认终止
  }
}

service EnvironmentService {
  // 双向流:一条连接承载一次完整会话(§2.6 补充 C.1)。
  rpc Interact(stream ClientMessage) returns (stream ServerMessage);
}
```

- [ ] **Step 2: 写依赖声明**

在 `pyproject.toml` 的 `dependencies` 列表末尾追加(保持既有两项不变):
```toml
    "grpcio>=1.84.0",
    "protobuf>=7.35.1,<8.0.0",
```

在 `[project.optional-dependencies]` 的 `dev` 列表末尾追加:
```toml
dev = ["pytest>=8.0", "grpcio-tools>=1.84.0"]
```

- [ ] **Step 3: 写生成脚本**

`scripts/gen_realtime_proto.sh`:
```bash
#!/usr/bin/env bash
# 重新生成 src/simulator/realtime/proto/ 下的 _pb2*.py。
# 生成产物提交入库；仅 schema 变更时需要重跑本脚本（需要 dev extra）。
set -euo pipefail
cd "$(dirname "$0")/.."
uv run --extra dev python -m grpc_tools.protoc \
  -I src \
  --python_out=src \
  --grpc_python_out=src \
  --pyi_out=src \
  src/simulator/realtime/proto/realtime_env.proto
echo "generated: src/simulator/realtime/proto/realtime_env_pb2*.py"
```

Run: `chmod +x scripts/gen_realtime_proto.sh`

- [ ] **Step 4: 建包骨架并生成代码**

```bash
mkdir -p src/simulator/realtime/proto
touch src/simulator/realtime/__init__.py src/simulator/realtime/proto/__init__.py
uv sync --extra dev
./scripts/gen_realtime_proto.sh
```

Expected: `src/simulator/realtime/proto/` 下出现 `realtime_env_pb2.py`、`realtime_env_pb2_grpc.py`、`realtime_env_pb2.pyi`。

- [ ] **Step 5: 写冒烟测试**

`tests/test_realtime_proto_smoke.py`:
```python
from simulator.realtime.proto import realtime_env_pb2 as pb2
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc


def test_turn_record_proto_round_trip():
    msg = pb2.TurnRecordProto(turn_index=1, speaker_slot="a", utterance="hi")
    data = msg.SerializeToString()
    back = pb2.TurnRecordProto.FromString(data)
    assert back.turn_index == 1
    assert isinstance(back.turn_index, int)
    assert back.speaker_slot == "a"
    assert back.HasField("inner_thought") is False
    assert back.HasField("activation_trace") is False


def test_client_message_oneof_selects_create_session():
    cs = pb2.CreateSession(scenario_id="s1", purpose="training", client_driven_slot="a")
    msg = pb2.ClientMessage(create_session=cs)
    assert msg.WhichOneof("payload") == "create_session"


def test_grpc_servicer_base_classes_present():
    assert hasattr(pb2_grpc, "EnvironmentServiceServicer")
    assert hasattr(pb2_grpc, "EnvironmentServiceStub")
    assert hasattr(pb2_grpc, "add_EnvironmentServiceServicer_to_server")
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run --extra dev pytest tests/test_realtime_proto_smoke.py -v`
Expected: PASS(3 passed)

- [ ] **Step 7: 全量回归**

Run: `uv run --extra dev pytest -q`
Expected: 178 + 3 = 181 passed(既有测试全部保持绿)

- [ ] **Step 8: 提交**

```bash
git add pyproject.toml src/simulator/realtime/ scripts/gen_realtime_proto.sh tests/test_realtime_proto_smoke.py
git commit -m "feat: realtime environment service gRPC protocol (§2.6) + codegen"
```

- [ ] Task 1 完成

---

### Task 2: 会话状态机(纯函数,§2.6.1)

**Files:** `src/simulator/realtime/session.py`、`tests/test_realtime_session.py`

**Interfaces:**
- Consumes: 无
- Produces: `SessionState`(字符串常量:`CREATED`/`ACTIVE`/`COMPLETING`/`CLOSED`/`FAILED`)、`SESSION_TRANSITIONS: dict[str, set[str]]`、`can_transition(frm, to) -> bool`、`next_states(frm) -> set[str]`、`assert_transition(frm, to) -> None`、`InvalidSessionTransitionError(Exception)`

- [ ] **Step 1: 写失败测试**

`tests/test_realtime_session.py`:
```python
import pytest

from simulator.realtime.session import (
    SessionState,
    can_transition,
    next_states,
    assert_transition,
    InvalidSessionTransitionError,
)


def test_created_to_active():
    assert can_transition(SessionState.CREATED, SessionState.ACTIVE)


def test_active_to_completing_to_closed():
    assert can_transition(SessionState.ACTIVE, SessionState.COMPLETING)
    assert can_transition(SessionState.COMPLETING, SessionState.CLOSED)


def test_failed_reachable_from_created_active_completing():
    assert can_transition(SessionState.CREATED, SessionState.FAILED)
    assert can_transition(SessionState.ACTIVE, SessionState.FAILED)
    assert can_transition(SessionState.COMPLETING, SessionState.FAILED)


def test_closed_and_failed_are_terminal():
    assert next_states(SessionState.CLOSED) == set()
    assert next_states(SessionState.FAILED) == set()


def test_illegal_transition_rejected():
    assert not can_transition(SessionState.CREATED, SessionState.COMPLETING)
    assert not can_transition(SessionState.CLOSED, SessionState.ACTIVE)


def test_assert_transition_raises_with_states_named():
    with pytest.raises(InvalidSessionTransitionError) as exc:
        assert_transition(SessionState.CLOSED, SessionState.ACTIVE)
    assert "CLOSED" in str(exc.value)
    assert "ACTIVE" in str(exc.value)


def test_assert_transition_allows_legal():
    assert_transition(SessionState.CREATED, SessionState.ACTIVE)  # 不抛异常
```

- [ ] **Step 2: 确认失败**

Run: `uv run --extra dev pytest tests/test_realtime_session.py -v`
Expected: FAIL(`ModuleNotFoundError: simulator.realtime.session`)

- [ ] **Step 3: 实现**

`src/simulator/realtime/session.py`:
```python
from __future__ import annotations


class SessionState:
    """会话生命周期状态常量(§2.6.1)。"""

    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    COMPLETING = "COMPLETING"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


# §2.6.1:CREATED→ACTIVE 无额外确认步骤；ACTIVE→COMPLETING→CLOSED 正常收尾；
# CREATED/ACTIVE/COMPLETING 任一阶段均可因异常转入 FAILED（§2.6.5）。
SESSION_TRANSITIONS: dict[str, set[str]] = {
    SessionState.CREATED: {SessionState.ACTIVE, SessionState.FAILED},
    SessionState.ACTIVE: {SessionState.COMPLETING, SessionState.FAILED},
    SessionState.COMPLETING: {SessionState.CLOSED, SessionState.FAILED},
    SessionState.CLOSED: set(),
    SessionState.FAILED: set(),
}


class InvalidSessionTransitionError(Exception):
    """会话状态迁移不合法。"""


def next_states(frm: str) -> set[str]:
    return set(SESSION_TRANSITIONS.get(frm, set()))


def can_transition(frm: str, to: str) -> bool:
    return to in next_states(frm)


def assert_transition(frm: str, to: str) -> None:
    if not can_transition(frm, to):
        raise InvalidSessionTransitionError(f"session: {frm} -> {to} not allowed")
```

- [ ] **Step 4: 确认通过 + 全回归**

Run: `uv run --extra dev pytest tests/test_realtime_session.py -v`
Expected: PASS(7 passed)

Run: `uv run --extra dev pytest -q`
Expected: 181 + 7 = 188 passed

- [ ] **Step 5: 提交**

```bash
git add src/simulator/realtime/session.py tests/test_realtime_session.py
git commit -m "feat: realtime session lifecycle state machine (§2.6.1)"
```

- [ ] Task 2 完成

---

### Task 3: 扩展 `run_episode` 支持逐轮回调(§2.6.2,修改 Plan 2 冻结核心)

**Files:** `src/simulator/runner.py`(扩展)、`tests/test_realtime_runner_on_turn.py`

> ⚠️ 本任务修改 Plan 2 已合入 main 的纯函数核心 `run_episode`(第二次扩展,`stop_predicate` 已于 Plan 3 加入)。硬性要求:**向后兼容**(不传 `on_turn` 时行为逐字节等价于当前),且 Plan 2/3 现有全部 runner 测试保持绿。

- [ ] **Step 1: 写失败测试**

`tests/test_realtime_runner_on_turn.py`:
```python
from simulator.datamodel import (
    Scenario, RoleSlot, Persona, RoleBinding, RoleAssignment,
)
from simulator.backends import ScriptedBackend
from simulator.runner import run_episode


def _fixture():
    scenario = Scenario(
        id="s1", background="bg",
        role_slots=[RoleSlot("a", "role a"), RoleSlot("b", "role b")],
        conflict="c", norms=["n"], purpose="training",
    )
    personas = {"pa": Persona("pa", ["t"], "m", "s", ["v"]),
                "pb": Persona("pb", ["t"], "m", "s", ["v"])}
    assignment = RoleAssignment(
        scenario_id="s1",
        bindings=[RoleBinding("a", "pa", "self_play"),
                  RoleBinding("b", "pb", "frozen_reference")],
        max_turns=4,
    )
    backends = {"a": ScriptedBackend({"a": ["a1", "a2"]}),
                "b": ScriptedBackend({"b": ["b1", "b2"]})}
    return scenario, assignment, personas, backends


def test_backward_compatible_no_callback_unchanged():
    scenario, assignment, personas, backends = _fixture()
    ep = run_episode(scenario, assignment, personas, backends, "e1")
    assert len(ep.turns) == 4
    assert ep.terminated_reason == "max_turns"


def test_on_turn_called_once_per_turn_immediately():
    scenario, assignment, personas, backends = _fixture()
    seen = []
    ep = run_episode(scenario, assignment, personas, backends, "e1",
                     on_turn=lambda t: seen.append((t.turn_index, t.speaker_slot)))
    assert seen == [(0, "a"), (1, "b"), (2, "a"), (3, "b")]
    assert len(ep.turns) == 4


def test_on_turn_receives_same_turn_record_appended_to_episode():
    scenario, assignment, personas, backends = _fixture()
    seen = []
    ep = run_episode(scenario, assignment, personas, backends, "e1",
                     on_turn=lambda t: seen.append(t))
    assert seen == ep.turns


def test_on_turn_and_stop_predicate_compose():
    scenario, assignment, personas, backends = _fixture()
    seen = []

    def stop(turns):
        return len(turns) >= 2

    ep = run_episode(scenario, assignment, personas, backends, "e1",
                     stop_predicate=stop,
                     on_turn=lambda t: seen.append(t.turn_index))
    assert seen == [0, 1]  # on_turn 在 stop_predicate 判定前对每轮都调用
    assert ep.terminated_reason == "goal_reached"
    assert len(ep.turns) == 2


def test_on_turn_none_is_noop():
    scenario, assignment, personas, backends = _fixture()
    ep = run_episode(scenario, assignment, personas, backends, "e1", on_turn=None)
    assert len(ep.turns) == 4
```

- [ ] **Step 2: 确认失败**

Run: `uv run --extra dev pytest tests/test_realtime_runner_on_turn.py -v`
Expected: FAIL(`TypeError: run_episode() got an unexpected keyword argument 'on_turn'`)

- [ ] **Step 3: 实现**

在 `src/simulator/runner.py` 中,把签名扩展为(新增末位可选参数,既有参数与逻辑完全不动):

```python
def run_episode(
    scenario: Scenario,
    assignment: RoleAssignment,
    personas: dict[str, Persona],
    backends: dict[str, ModelBackend],
    episode_id: str,
    stop_predicate: "Callable[[list], bool] | None" = None,
    on_turn: "Callable[[TurnRecord], None] | None = None",
) -> Episode:
```

在轮次循环内,`turns.append(...)` 之后、`if stop_predicate is not None and stop_predicate(turns):` 判定之前插入:

```python
        turns.append(
            TurnRecord(
                turn_index=turn_index,
                speaker_slot=binding.slot_id,
                utterance=utterance,
                inner_thought=inner_thought,
                activation_trace=None,  # V1 裸模型：无激活轨迹（§2.6.3）
            )
        )
        if on_turn is not None:
            on_turn(turns[-1])
        if stop_predicate is not None and stop_predicate(turns):
```

(其余代码不变:签名的类型注解字符串写法与既有 `stop_predicate` 一致,保持 `from __future__ import annotations` 已生效下的一致风格。)

- [ ] **Step 4: 确认通过 + 全回归**

Run: `uv run --extra dev pytest tests/test_realtime_runner_on_turn.py -v`
Expected: PASS(5 passed)

**必须**确认 Plan 2/3 原有 `tests/test_sim_runner.py`、`tests/test_runner_stop_predicate.py`、`tests/test_sim_integration.py`、`tests/test_diag_dynamic_track.py` 全绿(回归保护)。

Run: `uv run --extra dev pytest -q`
Expected: 188 + 5 = 193 passed

- [ ] **Step 5: 提交**

```bash
git add src/simulator/runner.py tests/test_realtime_runner_on_turn.py
git commit -m "feat: run_episode on_turn callback for streaming turn updates (§2.6.2)"
```

- [ ] Task 3 完成

---

### Task 4: Codec——dataclass ↔ proto message 双向转换

**Files:** `src/simulator/realtime/codec.py`、`tests/test_realtime_codec.py`

依据 Global Constraints"强类型字段不套 `Struct`,`activation_trace` 例外"。本任务的核心回归价值:证明整数字段(如 `max_turns`/`turn_index`)经强类型 proto 字段往返后**保持 Python `int` 类型**,不会像塞进 `Struct` 那样被静默转成 `float`。

- [ ] **Step 1: 写失败测试**

`tests/test_realtime_codec.py`:
```python
from simulator.datamodel import Persona, RoleBinding, RoleAssignment, TurnRecord
from simulator.realtime.codec import (
    persona_to_proto, persona_from_proto,
    role_assignment_to_proto, role_assignment_from_proto,
    turn_record_to_proto, turn_record_from_proto,
)


def _persona():
    return Persona(id="pa", traits=["t1", "t2"], motivation="m", speech_style="s",
                   values=["v1"], source="auto")


def test_persona_round_trip():
    p = _persona()
    assert persona_from_proto(persona_to_proto(p)) == p


def test_role_assignment_round_trip_preserves_int_max_turns():
    ra = RoleAssignment(
        scenario_id="s1",
        bindings=[RoleBinding("a", "pa", "self_play"),
                  RoleBinding("b", "pb", "frozen_reference")],
        max_turns=4,
    )
    proto = role_assignment_to_proto(ra)
    assert proto.max_turns == 4
    back = role_assignment_from_proto(proto)
    assert back == ra
    assert isinstance(back.max_turns, int)  # 强类型字段回归防护：不是 Struct 里的 float


def test_turn_record_round_trip_bare_model_no_activation_trace():
    t = TurnRecord(turn_index=2, speaker_slot="a", utterance="hi")
    proto = turn_record_to_proto(t)
    assert proto.HasField("activation_trace") is False
    back = turn_record_from_proto(proto)
    assert back == t
    assert isinstance(back.turn_index, int)


def test_turn_record_round_trip_with_inner_thought():
    t = TurnRecord(turn_index=0, speaker_slot="b", utterance="hey", inner_thought="thinking")
    proto = turn_record_to_proto(t)
    assert proto.HasField("inner_thought") is True
    back = turn_record_from_proto(proto)
    assert back == t


def test_turn_record_round_trip_with_activation_trace():
    t = TurnRecord(
        turn_index=1, speaker_slot="a", utterance="x",
        activation_trace={"triggered_skills": ["s1"], "selected_skill": "s1", "memory_hits": []},
    )
    proto = turn_record_to_proto(t)
    assert proto.HasField("activation_trace") is True
    back = turn_record_from_proto(proto)
    assert back == t
```

- [ ] **Step 2: 确认失败**

Run: `uv run --extra dev pytest tests/test_realtime_codec.py -v`
Expected: FAIL(`ModuleNotFoundError: simulator.realtime.codec`)

- [ ] **Step 3: 实现**

`src/simulator/realtime/codec.py`:
```python
from __future__ import annotations

from simulator.datamodel import Persona, RoleBinding, RoleAssignment, TurnRecord
from simulator.realtime.proto import realtime_env_pb2 as pb2


def persona_to_proto(p: Persona) -> pb2.PersonaProto:
    return pb2.PersonaProto(
        id=p.id, traits=list(p.traits), motivation=p.motivation,
        speech_style=p.speech_style, values=list(p.values), source=p.source,
    )


def persona_from_proto(proto: pb2.PersonaProto) -> Persona:
    return Persona(
        id=proto.id, traits=list(proto.traits), motivation=proto.motivation,
        speech_style=proto.speech_style, values=list(proto.values), source=proto.source,
    )


def role_binding_to_proto(b: RoleBinding) -> pb2.RoleBindingProto:
    return pb2.RoleBindingProto(
        slot_id=b.slot_id, persona_id=b.persona_id, model_backend=b.model_backend,
    )


def role_binding_from_proto(proto: pb2.RoleBindingProto) -> RoleBinding:
    return RoleBinding(
        slot_id=proto.slot_id, persona_id=proto.persona_id, model_backend=proto.model_backend,
    )


def role_assignment_to_proto(ra: RoleAssignment) -> pb2.RoleAssignmentProto:
    return pb2.RoleAssignmentProto(
        scenario_id=ra.scenario_id,
        bindings=[role_binding_to_proto(b) for b in ra.bindings],
        max_turns=ra.max_turns,
    )


def role_assignment_from_proto(proto: pb2.RoleAssignmentProto) -> RoleAssignment:
    return RoleAssignment(
        scenario_id=proto.scenario_id,
        bindings=[role_binding_from_proto(b) for b in proto.bindings],
        max_turns=proto.max_turns,
    )


def turn_record_to_proto(t: TurnRecord) -> pb2.TurnRecordProto:
    proto = pb2.TurnRecordProto(
        turn_index=t.turn_index, speaker_slot=t.speaker_slot, utterance=t.utterance,
    )
    if t.inner_thought is not None:
        proto.inner_thought = t.inner_thought
    if t.activation_trace is not None:
        proto.activation_trace.update(t.activation_trace)
    return proto


def turn_record_from_proto(proto: pb2.TurnRecordProto) -> TurnRecord:
    return TurnRecord(
        turn_index=proto.turn_index,
        speaker_slot=proto.speaker_slot,
        utterance=proto.utterance,
        inner_thought=proto.inner_thought if proto.HasField("inner_thought") else None,
        activation_trace=(
            dict(proto.activation_trace) if proto.HasField("activation_trace") else None
        ),
    )
```

> 注:`dict(proto.activation_trace)` 对 `google.protobuf.Struct` 有效(它实现了 mapping 接口),等价于 `MessageToDict` 但更轻量,已在本计划设计阶段验证其保留嵌套 list/dict/None 结构。

- [ ] **Step 4: 确认通过 + 全回归**

Run: `uv run --extra dev pytest tests/test_realtime_codec.py -v`
Expected: PASS(5 passed)

Run: `uv run --extra dev pytest -q`
Expected: 193 + 5 = 198 passed

- [ ] **Step 5: 提交**

```bash
git add src/simulator/realtime/codec.py tests/test_realtime_codec.py
git commit -m "feat: realtime codec — dataclass <-> proto message conversions"
```

- [ ] Task 4 完成

---

### Task 5: `ClientDrivenBackend`(阻塞等待跨进程客户端消息)

**Files:** `src/simulator/realtime/backend.py`、`tests/test_realtime_backend.py`

依据 spec §2.2"角色-模型后端映射由调用方指定"+ §2.6.5(超时判定)。`ClientDrivenBackend` 实现 `ModelBackend` Protocol,但其 `respond()` 不在本地计算,而是从一个线程安全队列里取出跨进程客户端刚提交的话语——这是"实时服务复用 `run_episode`、不重写轮次循环"的关键桥接点。

**Interfaces:**
- Consumes: `simulator.backends.ModelBackend` Protocol(结构性,无需显式继承)
- Produces: `ClientDrivenBackend(slot_id: str, timeout_s: float)`、`.submit(utterance: str, inner_thought: str | None) -> None`(供外部线程调用,把话语放进队列)、`.respond(...)`(供 `run_episode` 调用,阻塞取话语)、`TurnTimeoutError(Exception)`

- [ ] **Step 1: 写失败测试**

`tests/test_realtime_backend.py`:
```python
import threading
import time

import pytest

from simulator.datamodel import Persona, Scenario, RoleSlot
from simulator.realtime.backend import ClientDrivenBackend, TurnTimeoutError


def _persona_scenario():
    persona = Persona("pa", ["t"], "m", "s", ["v"])
    scenario = Scenario("s1", "bg", [RoleSlot("a", "role a")], "c", ["n"], "training")
    return persona, scenario


def test_submit_then_respond_returns_submitted_utterance():
    backend = ClientDrivenBackend(slot_id="a", timeout_s=1.0)
    persona, scenario = _persona_scenario()
    backend.submit("hello from client", None)
    utterance, inner_thought = backend.respond(persona, scenario, [], "a")
    assert utterance == "hello from client"
    assert inner_thought is None


def test_submit_with_inner_thought_passthrough():
    backend = ClientDrivenBackend(slot_id="a", timeout_s=1.0)
    persona, scenario = _persona_scenario()
    backend.submit("hi", "thinking hard")
    utterance, inner_thought = backend.respond(persona, scenario, [], "a")
    assert inner_thought == "thinking hard"


def test_respond_blocks_until_submit_from_another_thread():
    backend = ClientDrivenBackend(slot_id="a", timeout_s=2.0)
    persona, scenario = _persona_scenario()
    result = {}

    def responder():
        result["value"] = backend.respond(persona, scenario, [], "a")

    t = threading.Thread(target=responder)
    t.start()
    time.sleep(0.1)  # respond() 应仍在阻塞
    assert "value" not in result
    backend.submit("late arrival", None)
    t.join(timeout=2.0)
    assert result["value"] == ("late arrival", None)


def test_respond_raises_on_timeout():
    backend = ClientDrivenBackend(slot_id="a", timeout_s=0.05)
    persona, scenario = _persona_scenario()
    with pytest.raises(TurnTimeoutError):
        backend.respond(persona, scenario, [], "a")


def test_multiple_submits_are_consumed_in_order():
    backend = ClientDrivenBackend(slot_id="a", timeout_s=1.0)
    persona, scenario = _persona_scenario()
    backend.submit("first", None)
    backend.submit("second", None)
    u1, _ = backend.respond(persona, scenario, [], "a")
    u2, _ = backend.respond(persona, scenario, [], "a")
    assert (u1, u2) == ("first", "second")
```

- [ ] **Step 2: 确认失败**

Run: `uv run --extra dev pytest tests/test_realtime_backend.py -v`
Expected: FAIL(`ModuleNotFoundError: simulator.realtime.backend`)

- [ ] **Step 3: 实现**

`src/simulator/realtime/backend.py`:
```python
from __future__ import annotations

import queue

from simulator.datamodel import Persona, Scenario, TurnRecord


class TurnTimeoutError(Exception):
    """等待客户端提交本轮话语超时（§2.6.5 T1 类异常，本模块只报告不重试）。"""


class ClientDrivenBackend:
    """ModelBackend 的实时实现：respond() 不在本地计算话语，而是阻塞等待
    跨进程客户端通过 submit() 提交的话语（§2.2"角色-模型后端映射由调用方
    指定"的实时落地）。这是 run_episode 轮次循环能在不改动一行代码的情况下
    支持"部分角色由实时客户端驱动"的桥接点——对 run_episode 而言，
    ClientDrivenBackend 只是又一个普通 ModelBackend。"""

    def __init__(self, slot_id: str, timeout_s: float) -> None:
        self.slot_id = slot_id
        self._timeout_s = timeout_s
        self._inbox: "queue.Queue[tuple[str, str | None]]" = queue.Queue()

    def submit(self, utterance: str, inner_thought: str | None) -> None:
        """由 gRPC 主循环线程调用：把客户端刚提交的话语放进队列。"""
        self._inbox.put((utterance, inner_thought))

    def respond(
        self,
        persona: Persona,
        scenario: Scenario,
        history: list[TurnRecord],
        speaker_slot: str,
    ) -> tuple[str, str | None]:
        """由 run_episode 的后台驱动线程调用：阻塞直到 submit() 到来或超时。"""
        try:
            return self._inbox.get(timeout=self._timeout_s)
        except queue.Empty:
            raise TurnTimeoutError(
                f"no SubmitTurn received for slot {self.slot_id!r} within {self._timeout_s}s"
            )
```

- [ ] **Step 4: 确认通过 + 全回归**

Run: `uv run --extra dev pytest tests/test_realtime_backend.py -v`
Expected: PASS(5 passed)

Run: `uv run --extra dev pytest -q`
Expected: 198 + 5 = 203 passed

- [ ] **Step 5: 提交**

```bash
git add src/simulator/realtime/backend.py tests/test_realtime_backend.py
git commit -m "feat: ClientDrivenBackend — ModelBackend bridging to cross-process client turns"
```

- [ ] Task 5 完成

---

### Task 6: `SessionDriver`(后台线程驱动 `run_episode`,进程内测试)

**Files:** `src/simulator/realtime/driver.py`、`tests/test_realtime_driver.py`

依据 spec §2.6.2(TurnUpdate 每轮立即推送)+ §2.6.5(角色后端失败/超时的显式终止原因)。`SessionDriver` 把 `run_episode` 放进后台线程跑,用 `outbox` 队列把每轮 `TurnRecord`(经 `on_turn` 回调)和最终结果转发出来,让 gRPC 主循环(Task 7)只需要"从 outbox 里取事件、yield 出去"而不直接接触线程细节。本任务**不涉及 gRPC**,用纯 Python 队列模拟客户端消息,验证驱动器本身正确。

**Interfaces:**
- Consumes: `run_episode`(Task 3 扩展签名)、`ClientDrivenBackend`(Task 5)、`SessionState`(Task 2)
- Produces:
  - `DriverEvent`(`kind: str`——`"turn"`/`"closed"`/`"failed"`,`payload: TurnRecord | Episode | str`)
  - `SessionDriver(scenario, assignment, personas, backends, client_driven_slot, episode_id, turn_timeout_s)`
  - `.state -> str`(当前 `SessionState`)
  - `.start() -> None`(起后台线程,状态 CREATED→ACTIVE)
  - `.submit_turn(utterance, inner_thought) -> None`(转发给 `client_driven_slot` 对应的 `ClientDrivenBackend`)
  - `.poll_event(timeout_s) -> DriverEvent | None`(从 outbox 取下一个事件)
  - `.request_end(reason) -> None`(客户端主动终止:唤醒阻塞中的 backend 并强制收尾)

- [ ] **Step 1: 写失败测试**

`tests/test_realtime_driver.py`:
```python
from simulator.datamodel import (
    Scenario, RoleSlot, Persona, RoleBinding, RoleAssignment,
)
from simulator.backends import ScriptedBackend
from simulator.realtime.driver import SessionDriver, DriverEvent
from simulator.realtime.session import SessionState


def _fixture(max_turns=4):
    scenario = Scenario(
        id="s1", background="bg",
        role_slots=[RoleSlot("a", "候选"), RoleSlot("b", "对手")],
        conflict="c", norms=["n"], purpose="training",
    )
    personas = {"pa": Persona("pa", ["t"], "m", "s", ["v"]),
                "pb": Persona("pb", ["t"], "m", "s", ["v"])}
    assignment = RoleAssignment(
        scenario_id="s1",
        bindings=[RoleBinding("a", "pa", "self_play"),
                  RoleBinding("b", "pb", "frozen_reference")],
        max_turns=max_turns,
    )
    return scenario, assignment, personas


def test_driver_streams_turn_events_then_closed():
    scenario, assignment, personas = _fixture(max_turns=2)
    opponent_backend = ScriptedBackend({"b": ["opp1"]})
    driver = SessionDriver(
        scenario, assignment, personas,
        server_backends={"b": opponent_backend},
        client_driven_slot="a", episode_id="ep-1", turn_timeout_s=2.0,
    )
    assert driver.state == SessionState.CREATED
    driver.start()
    assert driver.state == SessionState.ACTIVE

    driver.submit_turn("candidate turn 1", None)
    ev1 = driver.poll_event(timeout_s=2.0)
    assert ev1.kind == "turn"
    assert ev1.payload.speaker_slot == "a"
    assert ev1.payload.utterance == "candidate turn 1"

    ev2 = driver.poll_event(timeout_s=2.0)
    assert ev2.kind == "turn"
    assert ev2.payload.speaker_slot == "b"
    assert ev2.payload.utterance == "opp1"

    ev3 = driver.poll_event(timeout_s=2.0)
    assert ev3.kind == "closed"
    assert ev3.payload.terminated_reason == "max_turns"
    assert len(ev3.payload.turns) == 2
    assert driver.state == SessionState.CLOSED


def test_driver_timeout_transitions_to_failed():
    scenario, assignment, personas = _fixture(max_turns=2)
    driver = SessionDriver(
        scenario, assignment, personas,
        server_backends={"b": ScriptedBackend({"b": ["opp1"]})},
        client_driven_slot="a", episode_id="ep-2", turn_timeout_s=0.05,
    )
    driver.start()
    # 不调用 submit_turn，等待后台线程因超时失败
    ev = driver.poll_event(timeout_s=2.0)
    assert ev.kind == "failed"
    assert ev.payload == "timeout"
    assert driver.state == SessionState.FAILED


def test_driver_role_backend_failure_transitions_to_failed():
    class BoomBackend:
        def respond(self, persona, scenario, history, speaker_slot):
            raise RuntimeError("backend exploded")

    scenario, assignment, personas = _fixture(max_turns=2)
    driver = SessionDriver(
        scenario, assignment, personas,
        server_backends={"b": BoomBackend()},
        client_driven_slot="a", episode_id="ep-3", turn_timeout_s=2.0,
    )
    driver.start()
    driver.submit_turn("candidate turn 1", None)
    ev1 = driver.poll_event(timeout_s=2.0)
    assert ev1.kind == "turn"  # 候选先手成功
    ev2 = driver.poll_event(timeout_s=2.0)
    assert ev2.kind == "failed"
    assert ev2.payload == "role_backend_failure"
    assert driver.state == SessionState.FAILED


def test_driver_request_end_stops_driver_promptly():
    scenario, assignment, personas = _fixture(max_turns=100)
    driver = SessionDriver(
        scenario, assignment, personas,
        server_backends={"b": ScriptedBackend({"b": ["opp"] * 100})},
        client_driven_slot="a", episode_id="ep-4", turn_timeout_s=2.0,
    )
    driver.start()
    driver.submit_turn("only turn", None)
    ev1 = driver.poll_event(timeout_s=2.0)
    assert ev1.kind == "turn"
    driver.request_end("client_terminated")
    ev2 = driver.poll_event(timeout_s=2.0)
    # 对手轮先出（已在途），随后是 client_terminated 收尾
    kinds = [ev2.kind]
    payload = ev2.payload
    if ev2.kind == "turn":
        ev3 = driver.poll_event(timeout_s=2.0)
        kinds.append(ev3.kind)
        payload = ev3.payload
    assert "closed" in kinds
    assert payload.terminated_reason == "client_terminated"
```

- [ ] **Step 2: 确认失败**

Run: `uv run --extra dev pytest tests/test_realtime_driver.py -v`
Expected: FAIL(`ModuleNotFoundError: simulator.realtime.driver`)

- [ ] **Step 3: 实现**

`src/simulator/realtime/driver.py`:
```python
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any

from simulator.datamodel import Episode, Persona, Scenario, RoleAssignment, TurnRecord
from simulator.backends import ModelBackend
from simulator.runner import run_episode
from simulator.realtime.backend import ClientDrivenBackend, TurnTimeoutError
from simulator.realtime.session import SessionState, assert_transition


@dataclass(frozen=True)
class DriverEvent:
    """SessionDriver 经 outbox 转发给 gRPC 主循环的事件。
    kind="turn" → payload: TurnRecord；kind="closed" → payload: Episode；
    kind="failed" → payload: str（终止原因，如 "timeout"/"role_backend_failure"）。"""

    kind: str
    payload: Any


class SessionDriver:
    """在后台线程里驱动 run_episode，把每轮结果与最终结果经 outbox 队列
    转发出来。client_driven_slot 对应的 backend 换成 ClientDrivenBackend，
    其余角色位使用调用方传入的 server_backends（真实/罐头模型）。"""

    def __init__(
        self,
        scenario: Scenario,
        assignment: RoleAssignment,
        personas: dict[str, Persona],
        *,
        server_backends: dict[str, ModelBackend],
        client_driven_slot: str,
        episode_id: str,
        turn_timeout_s: float,
    ) -> None:
        self._scenario = scenario
        self._assignment = assignment
        self._personas = personas
        self._episode_id = episode_id
        self.client_driven_slot = client_driven_slot
        self._client_backend = ClientDrivenBackend(client_driven_slot, turn_timeout_s)
        self._backends: dict[str, ModelBackend] = dict(server_backends)
        self._backends[client_driven_slot] = self._client_backend
        self._outbox: "queue.Queue[DriverEvent]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self.state = SessionState.CREATED
        self._end_requested = threading.Event()

    def start(self) -> None:
        assert_transition(self.state, SessionState.ACTIVE)
        self.state = SessionState.ACTIVE
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit_turn(self, utterance: str, inner_thought: str | None) -> None:
        self._client_backend.submit(utterance, inner_thought)

    def request_end(self, reason: str) -> None:
        """客户端主动终止：置位标志，并投喂一条哨兵话语唤醒可能正阻塞的
        ClientDrivenBackend.respond()，让后台线程尽快检查标志并收尾。"""
        self._end_requested.set()
        self._client_backend.submit("", None)

    def poll_event(self, timeout_s: float) -> DriverEvent | None:
        try:
            return self._outbox.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def _stop_predicate(self, turns: list[TurnRecord]) -> bool:
        return self._end_requested.is_set()

    def _on_turn(self, turn: TurnRecord) -> None:
        self._outbox.put(DriverEvent(kind="turn", payload=turn))

    def _run(self) -> None:
        try:
            episode = run_episode(
                self._scenario, self._assignment, self._personas, self._backends,
                self._episode_id,
                stop_predicate=self._stop_predicate,
                on_turn=self._on_turn,
            )
        except TurnTimeoutError:
            assert_transition(self.state, SessionState.FAILED)
            self.state = SessionState.FAILED
            self._outbox.put(DriverEvent(kind="failed", payload="timeout"))
            return
        except Exception:
            assert_transition(self.state, SessionState.FAILED)
            self.state = SessionState.FAILED
            self._outbox.put(DriverEvent(kind="failed", payload="role_backend_failure"))
            return

        assert_transition(self.state, SessionState.COMPLETING)
        self.state = SessionState.COMPLETING
        if self._end_requested.is_set():
            episode = Episode(
                id=episode.id, scenario_id=episode.scenario_id,
                role_assignment=episode.role_assignment, turns=episode.turns,
                terminated_reason="client_terminated",
            )
        assert_transition(self.state, SessionState.CLOSED)
        self.state = SessionState.CLOSED
        self._outbox.put(DriverEvent(kind="closed", payload=episode))
```

> 设计说明:`request_end` 用一条空字符串哨兵话语唤醒 `respond()`——若此刻恰好轮到对手位说话(非 client_driven_slot),该哨兵会被搁在 `ClientDrivenBackend` 的队列里,等下一次真正轮到 `client_driven_slot` 时才被消费为一次(空)发言,`stop_predicate` 随即在那一轮后触发终止。这对 V1 可接受——`request_end` 的准确语义是"尽快"而非"立即",且不会导致驱动器卡死(队列有界等待被 `turn_timeout_s` 兜底)。

- [ ] **Step 4: 确认通过 + 全回归**

Run: `uv run --extra dev pytest tests/test_realtime_driver.py -v`
Expected: PASS(4 passed)

Run: `uv run --extra dev pytest -q`
Expected: 203 + 4 = 207 passed

- [ ] **Step 5: 提交**

```bash
git add src/simulator/realtime/driver.py tests/test_realtime_driver.py
git commit -m "feat: SessionDriver — background-thread run_episode driver with event outbox"
```

- [ ] Task 6 完成

---

### Task 7: `EnvironmentServicer` + `create_server`(真实 gRPC socket,首个端到端正常会话)

**Files:** `src/simulator/realtime/servicer.py`、`src/simulator/realtime/server.py`、`tests/test_realtime_servicer_e2e.py`

依据 spec §2.6.2(消息路由)+ §2.6.4(并发会话隔离)。本任务把 Task 6 的 `SessionDriver` 接到真实 gRPC `Interact` 双向流上,第一次跑通真实 `127.0.0.1` socket 端到端。

**Interfaces:**
- Consumes: `SessionDriver`/`DriverEvent`(Task 6)、`codec.*`(Task 4)、`realtime_env_pb2`/`realtime_env_pb2_grpc`(Task 1)
- Produces:
  - `EnvironmentServicer(backends_factory, *, turn_timeout_s=30.0, on_session_closed=None)`(`EnvironmentServiceServicer` 子类)
  - `create_server(backends_factory, *, port=0, turn_timeout_s=30.0, on_session_closed=None) -> grpc.Server`(`server.add_insecure_port` 已调用,`server.bound_port` 属性携带实际端口;调用方负责 `.start()`/`.stop()`)
  - `backends_factory: Callable[[Scenario, RoleAssignment, dict[str, Persona], str], dict[str, ModelBackend]]`(给定 scenario/assignment/personas/client_driven_slot,返回**除** client_driven_slot 外每个角色位的 `ModelBackend`)

- [ ] **Step 1: 写失败测试**

`tests/test_realtime_servicer_e2e.py`:
```python
import queue

import grpc
import pytest

from simulator.backends import ScriptedBackend
from simulator.realtime.proto import realtime_env_pb2 as pb2
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.codec import (
    role_assignment_to_proto, persona_to_proto, turn_record_from_proto,
)
from simulator.realtime.server import create_server
from simulator.datamodel import Persona, RoleBinding, RoleAssignment


def _opponent_backends_factory(scenario, assignment, personas, client_driven_slot):
    other_slots = assignment.slot_ids() - {client_driven_slot}
    return {slot: ScriptedBackend({slot: ["opp1", "opp2", "opp3", "opp4"]}) for slot in other_slots}


@pytest.fixture
def server_address():
    server = create_server(_opponent_backends_factory, port=0, turn_timeout_s=2.0)
    server.start()
    yield f"127.0.0.1:{server.bound_port}"
    server.stop(0)


def _create_session_msg(max_turns=4):
    assignment = RoleAssignment(
        scenario_id="s1",
        bindings=[RoleBinding("a", "pa", "self_play"), RoleBinding("b", "pb", "frozen_reference")],
        max_turns=max_turns,
    )
    personas = {
        "pa": persona_to_proto(Persona("pa", ["t"], "m", "s", ["v"])),
        "pb": persona_to_proto(Persona("pb", ["t"], "m", "s", ["v"])),
    }
    cs = pb2.CreateSession(
        scenario_id="s1",
        role_assignment=role_assignment_to_proto(assignment),
        personas=personas,
        purpose="training",
        client_driven_slot="a",
    )
    return pb2.ClientMessage(create_session=cs)


def test_full_session_happy_path(server_address):
    channel = grpc.insecure_channel(server_address)
    stub = pb2_grpc.EnvironmentServiceStub(channel)

    q: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()

    def gen():
        while True:
            item = q.get()
            if item is None:
                return
            yield item

    resp_iter = stub.Interact(gen())
    q.put(_create_session_msg(max_turns=4))
    ready = next(resp_iter)
    assert ready.WhichOneof("payload") == "session_ready"
    session_id = ready.session_ready.session_id
    assert session_id

    turns = []
    for i in range(2):  # 候选驱动 2 轮（a 在 turn 0/2），对手自动应答 b 在 turn 1/3
        q.put(pb2.ClientMessage(submit_turn=pb2.SubmitTurn(utterance=f"cand-{i}")))
        while True:
            msg = next(resp_iter)
            kind = msg.WhichOneof("payload")
            if kind == "turn_update":
                turns.append(turn_record_from_proto(msg.turn_update.turn))
                if turns[-1].speaker_slot == "a":
                    break  # 候选轮已确认，进入下一次候选提交
            elif kind == "end_session":
                turns_final = msg
                break

    # 排空剩余事件直到 end_session
    end_msg = None
    while end_msg is None:
        msg = next(resp_iter)
        if msg.WhichOneof("payload") == "turn_update":
            turns.append(turn_record_from_proto(msg.turn_update.turn))
        elif msg.WhichOneof("payload") == "end_session":
            end_msg = msg

    q.put(None)
    channel.close()

    assert end_msg.end_session.reason == "max_turns"
    assert [t.speaker_slot for t in turns] == ["a", "b", "a", "b"]
    assert [t.utterance for t in turns] == ["cand-0", "opp1", "cand-1", "opp2"]
```

- [ ] **Step 2: 确认失败**

Run: `uv run --extra dev pytest tests/test_realtime_servicer_e2e.py -v`
Expected: FAIL(`ModuleNotFoundError: simulator.realtime.servicer`)

- [ ] **Step 3: 实现**

`src/simulator/realtime/servicer.py`:
```python
from __future__ import annotations

import uuid
from typing import Callable

from simulator.datamodel import Persona, RoleAssignment, Scenario, RoleSlot
from simulator.backends import ModelBackend
from simulator.realtime.codec import (
    persona_from_proto, role_assignment_from_proto, turn_record_to_proto,
)
from simulator.realtime.driver import SessionDriver
from simulator.realtime.proto import realtime_env_pb2 as pb2
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc

BackendsFactory = Callable[
    [Scenario, RoleAssignment, dict[str, Persona], str], dict[str, ModelBackend]
]


class EnvironmentServicer(pb2_grpc.EnvironmentServiceServicer):
    """gRPC 双向流服务实现（§2.6.2）。每个 Interact 调用对应一个会话：
    先收 CreateSession 起 SessionDriver，随后把 SubmitTurn 转发给驱动器、
    把驱动器 outbox 里的事件转成 ServerMessage yield 出去。会话间除各自
    的 SessionDriver 实例外不共享任何可变状态（§2.6.4）。"""

    def __init__(
        self,
        backends_factory: BackendsFactory,
        *,
        turn_timeout_s: float = 30.0,
        on_session_closed=None,
    ) -> None:
        self._backends_factory = backends_factory
        self._turn_timeout_s = turn_timeout_s
        self._on_session_closed = on_session_closed

    def Interact(self, request_iterator, context):
        driver: SessionDriver | None = None
        session_id = ""
        try:
            for msg in request_iterator:
                kind = msg.WhichOneof("payload")
                if kind == "create_session":
                    session_id = f"sess-{uuid.uuid4().hex[:12]}"
                    driver, ready = self._handle_create(msg.create_session, session_id)
                    yield ready
                elif kind == "submit_turn":
                    driver.submit_turn(
                        msg.submit_turn.utterance,
                        msg.submit_turn.inner_thought
                        if msg.submit_turn.HasField("inner_thought") else None,
                    )
                elif kind == "end_session":
                    driver.request_end("client_terminated")

                if driver is not None:
                    yield from self._drain_ready_events(driver)
        finally:
            if driver is not None and driver.state not in ("CLOSED", "FAILED"):
                driver.request_end("client_terminated")

    def _handle_create(self, create_session, session_id: str):
        role_assignment = role_assignment_from_proto(create_session.role_assignment)
        personas = {
            pid: persona_from_proto(p) for pid, p in create_session.personas.items()
        }
        scenario = Scenario(
            id=create_session.scenario_id,
            background="",
            role_slots=[RoleSlot(slot_id=s, description="") for s in role_assignment.slot_ids()],
            conflict="", norms=[], purpose=create_session.purpose,
        )
        server_backends = self._backends_factory(
            scenario, role_assignment, personas, create_session.client_driven_slot,
        )
        driver = SessionDriver(
            scenario, role_assignment, personas,
            server_backends=server_backends,
            client_driven_slot=create_session.client_driven_slot,
            episode_id=f"ep-{session_id}",
            turn_timeout_s=self._turn_timeout_s,
        )
        driver.start()
        ready = pb2.ServerMessage(session_ready=pb2.SessionReady(session_id=session_id))
        return driver, ready

    def _drain_ready_events(self, driver: SessionDriver):
        """把驱动器 outbox 里已就绪的事件全部转发出去（非阻塞轮询，
        一次 poll_event(0) 抽干当前积压，避免每条 ClientMessage 只对应
        恰好一条 ServerMessage 的强绑定——TurnUpdate 需要在轮次完成后
        立即推送，可能一次提交触发多条服务端消息）。"""
        while True:
            event = driver.poll_event(timeout_s=0.01)
            if event is None:
                return
            if event.kind == "turn":
                yield pb2.ServerMessage(
                    turn_update=pb2.TurnUpdate(turn=turn_record_to_proto(event.payload))
                )
            elif event.kind == "closed":
                episode = event.payload
                if self._on_session_closed is not None:
                    self._on_session_closed(episode)
                yield pb2.ServerMessage(
                    end_session=pb2.EndSession(
                        reason=episode.terminated_reason, episode_id=episode.id,
                    )
                )
                return
            elif event.kind == "failed":
                yield pb2.ServerMessage(
                    end_session=pb2.EndSession(
                        reason=event.payload, episode_id="", failure_detail=event.payload,
                    )
                )
                return
```

`src/simulator/realtime/server.py`:
```python
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import grpc

from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.servicer import BackendsFactory, EnvironmentServicer


def create_server(
    backends_factory: BackendsFactory,
    *,
    port: int = 0,
    turn_timeout_s: float = 30.0,
    on_session_closed=None,
    max_workers: int = 16,
) -> grpc.Server:
    """组装并绑定（不启动）gRPC 服务端。调用方负责 .start()/.stop()。
    port=0 让操作系统分配空闲端口，实际端口经 server.bound_port 取得。"""
    server = grpc.server(ThreadPoolExecutor(max_workers=max_workers))
    servicer = EnvironmentServicer(
        backends_factory, turn_timeout_s=turn_timeout_s, on_session_closed=on_session_closed,
    )
    pb2_grpc.add_EnvironmentServiceServicer_to_server(servicer, server)
    bound_port = server.add_insecure_port(f"127.0.0.1:{port}")
    server.bound_port = bound_port
    return server
```

- [ ] **Step 4: 确认通过 + 全回归**

Run: `uv run --extra dev pytest tests/test_realtime_servicer_e2e.py -v`
Expected: PASS(1 passed)

Run: `uv run --extra dev pytest -q`
Expected: 207 + 1 = 208 passed

- [ ] **Step 5: 提交**

```bash
git add src/simulator/realtime/servicer.py src/simulator/realtime/server.py tests/test_realtime_servicer_e2e.py
git commit -m "feat: EnvironmentServicer + create_server — real gRPC socket happy-path e2e"
```

- [ ] Task 7 完成

---

### Task 8: 失败路径——超时 / 角色后端失败 / 客户端主动终止(真实 gRPC socket)

**Files:** `tests/test_realtime_servicer_e2e.py`(追加)

依据 spec §2.6.5(异常路径判定必须显式区分)。复用 Task 7 已验证的 `server_address` fixture 模式,追加三条失败路径测试。

- [ ] **Step 1: 写失败测试(追加到 `tests/test_realtime_servicer_e2e.py`)**

```python
def test_timeout_ends_session_with_explicit_reason():
    server = create_server(_opponent_backends_factory, port=0, turn_timeout_s=0.1)
    server.start()
    address = f"127.0.0.1:{server.bound_port}"
    channel = grpc.insecure_channel(address)
    stub = pb2_grpc.EnvironmentServiceStub(channel)
    q: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()

    def gen():
        while True:
            item = q.get()
            if item is None:
                return
            yield item

    resp_iter = stub.Interact(gen())
    q.put(_create_session_msg(max_turns=4))
    ready = next(resp_iter)
    assert ready.WhichOneof("payload") == "session_ready"
    # 不提交任何 SubmitTurn，等待服务端因超时主动结束
    end_msg = next(resp_iter)
    assert end_msg.WhichOneof("payload") == "end_session"
    assert end_msg.end_session.reason == "timeout"
    q.put(None)
    channel.close()
    server.stop(0)


def test_role_backend_failure_ends_session_with_explicit_reason():
    class BoomBackend:
        def respond(self, persona, scenario, history, speaker_slot):
            raise RuntimeError("boom")

    def boom_factory(scenario, assignment, personas, client_driven_slot):
        other = assignment.slot_ids() - {client_driven_slot}
        return {slot: BoomBackend() for slot in other}

    server = create_server(boom_factory, port=0, turn_timeout_s=2.0)
    server.start()
    address = f"127.0.0.1:{server.bound_port}"
    channel = grpc.insecure_channel(address)
    stub = pb2_grpc.EnvironmentServiceStub(channel)
    q: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()

    def gen():
        while True:
            item = q.get()
            if item is None:
                return
            yield item

    resp_iter = stub.Interact(gen())
    q.put(_create_session_msg(max_turns=4))
    next(resp_iter)  # session_ready
    q.put(pb2.ClientMessage(submit_turn=pb2.SubmitTurn(utterance="cand-0")))
    next(resp_iter)  # turn_update for slot a
    end_msg = next(resp_iter)  # 对手 backend 抛异常 -> role_backend_failure
    assert end_msg.WhichOneof("payload") == "end_session"
    assert end_msg.end_session.reason == "role_backend_failure"
    q.put(None)
    channel.close()
    server.stop(0)


def test_client_initiated_end_terminates_promptly(server_address):
    channel = grpc.insecure_channel(server_address)
    stub = pb2_grpc.EnvironmentServiceStub(channel)
    q: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()

    def gen():
        while True:
            item = q.get()
            if item is None:
                return
            yield item

    resp_iter = stub.Interact(gen())
    q.put(_create_session_msg(max_turns=100))
    next(resp_iter)  # session_ready
    q.put(pb2.ClientMessage(submit_turn=pb2.SubmitTurn(utterance="only-turn")))
    next(resp_iter)  # turn_update for slot a
    q.put(pb2.ClientMessage(end_session=pb2.EndSession(reason="client_terminated")))

    end_msg = None
    for _ in range(5):  # 对手轮可能先到，之后才是 end_session
        msg = next(resp_iter)
        if msg.WhichOneof("payload") == "end_session":
            end_msg = msg
            break
    assert end_msg is not None
    assert end_msg.end_session.reason == "client_terminated"
    q.put(None)
    channel.close()


def test_abrupt_disconnect_does_not_hang_server(server_address):
    channel = grpc.insecure_channel(server_address)
    stub = pb2_grpc.EnvironmentServiceStub(channel)
    q: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()

    def gen():
        while True:
            item = q.get()
            if item is None:
                return
            yield item

    resp_iter = stub.Interact(gen())
    q.put(_create_session_msg(max_turns=100))
    next(resp_iter)  # session_ready
    q.put(None)  # 立即关闭客户端流，不发 end_session（模拟进程崩溃）
    with pytest.raises(grpc.RpcError):
        for _ in resp_iter:
            pass
    channel.close()
    # 服务端未挂起、进程仍可继续接受新连接，用一次全新的正常会话验证
    channel2 = grpc.insecure_channel(server_address)
    stub2 = pb2_grpc.EnvironmentServiceStub(channel2)
    q2: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()

    def gen2():
        while True:
            item = q2.get()
            if item is None:
                return
            yield item

    resp_iter2 = stub2.Interact(gen2())
    q2.put(_create_session_msg(max_turns=2))
    ready2 = next(resp_iter2)
    assert ready2.WhichOneof("payload") == "session_ready"
    q2.put(None)
    channel2.close()
```

- [ ] **Step 2: 确认失败**

Run: `uv run --extra dev pytest tests/test_realtime_servicer_e2e.py -v -k "timeout or failure or client_initiated or abrupt"`
Expected: FAIL 或部分行为不符(取决于 Task 7 实现是否已巧合覆盖;预期新增断言暴露 gap)

- [ ] **Step 3: 修正实现(如需)**

若 Task 7 的 `Interact`/`_drain_ready_events` 已能正确处理这些路径(超时/后端失败经 `SessionDriver._run` 的 `except` 分支产生 `"failed"` 事件、`request_end` 已支持客户端主动终止、`finally` 块已保证异常断连不挂起),本任务**无需修改生产代码**,只需确认测试全部通过——这是预期结果,因为 Task 6/7 的设计从一开始就覆盖了这四条路径。若测试暴露任何 gap,在 `servicer.py`/`driver.py` 做最小修正后重跑。

- [ ] **Step 4: 确认通过 + 全回归**

Run: `uv run --extra dev pytest tests/test_realtime_servicer_e2e.py -v`
Expected: PASS(5 passed:Task 7 的 1 条 + 本任务 4 条)

Run: `uv run --extra dev pytest -q`
Expected: 208 + 4 = 212 passed

- [ ] **Step 5: 提交**

```bash
git add tests/test_realtime_servicer_e2e.py
git commit -m "test: realtime failure paths — timeout / backend failure / client end / disconnect (§2.6.5)"
```

- [ ] Task 8 完成

---

### Task 9: 并发会话隔离(真实 gRPC socket,多会话并行)

**Files:** `tests/test_realtime_servicer_e2e.py`(追加)

依据 spec §2.6.4(硬约束):会话间不共享任何可变状态。用多线程并行跑 N 个独立会话,断言互不串话。

- [ ] **Step 1: 写失败测试(追加)**

```python
def test_concurrent_sessions_do_not_cross_talk(server_address):
    import threading

    def run_one_session(tag, out):
        channel = grpc.insecure_channel(server_address)
        stub = pb2_grpc.EnvironmentServiceStub(channel)
        q: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()

        def gen():
            while True:
                item = q.get()
                if item is None:
                    return
                yield item

        resp_iter = stub.Interact(gen())
        q.put(_create_session_msg(max_turns=4))
        ready = next(resp_iter)
        session_id = ready.session_ready.session_id
        seen = [f"session:{session_id}"]
        for i in range(2):
            q.put(pb2.ClientMessage(submit_turn=pb2.SubmitTurn(utterance=f"{tag}-{i}")))
            while True:
                msg = next(resp_iter)
                if msg.WhichOneof("payload") == "turn_update":
                    seen.append(msg.turn_update.turn.utterance)
                    if msg.turn_update.turn.speaker_slot == "a":
                        break
        end_msg = None
        while end_msg is None:
            msg = next(resp_iter)
            if msg.WhichOneof("payload") == "turn_update":
                seen.append(msg.turn_update.turn.utterance)
            elif msg.WhichOneof("payload") == "end_session":
                end_msg = msg
        q.put(None)
        channel.close()
        out[tag] = (session_id, seen)

    results: dict[str, tuple] = {}
    tags = [f"client-{i}" for i in range(5)]
    threads = [threading.Thread(target=run_one_session, args=(tag, results)) for tag in tags]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 5
    session_ids = [sid for sid, _ in results.values()]
    assert len(set(session_ids)) == 5  # 每会话唯一 session_id
    for tag in tags:
        session_id, seen = results[tag]
        cand_utterances = [u for u in seen if u.startswith(tag)]
        assert cand_utterances == [f"{tag}-0", f"{tag}-1"]  # 只看到自己提交的候选话语
        other_tags = [t for t in tags if t != tag]
        assert not any(u.startswith(other) for u in seen for other in other_tags)
```

- [ ] **Step 2: 确认失败**

Run: `uv run --extra dev pytest tests/test_realtime_servicer_e2e.py -v -k concurrent`
Expected: 若实现正确应直接 PASS(本任务是对既有并发安全设计的验证,`EnvironmentServicer` 每次 `Interact` 调用都是独立的局部变量 `driver`,`SessionDriver` 各自独立线程+队列,天然隔离);若因 gRPC `ThreadPoolExecutor` worker 数不足导致挂起,提高 Task 7 `create_server` 的 `max_workers` 默认值。

- [ ] **Step 3: 确认通过 + 全回归**

Run: `uv run --extra dev pytest tests/test_realtime_servicer_e2e.py -v`
Expected: PASS(6 passed)

Run: `uv run --extra dev pytest -q`
Expected: 212 + 1 = 213 passed

- [ ] **Step 4: 提交**

```bash
git add tests/test_realtime_servicer_e2e.py
git commit -m "test: concurrent session isolation — 5 parallel sessions, no cross-talk (§2.6.4)"
```

- [ ] Task 9 完成

---

### Task 10: 客户端包装器 `EnvironmentServiceClient` + Episode 重建一致性(parity)

**Files:** `src/simulator/realtime/client.py`、`tests/test_realtime_client.py`

依据 spec §2.6.2(客户端经逐轮 TurnUpdate 已拿到全部数据,可在本地重建完整 Episode,不需要服务端内联发送)。这是未来 MindTrainer/MindAgent 计划会直接复用的接口。**核心验证**:用同一份 `ScriptedBackend` 剧本,分别跑(a)直接调用 `run_episode`、(b)经真实 gRPC 走完整会话再本地重建 Episode——两者结果一致(parity),证明"共享同一个 Simulation Runner 核心"这条硬约束成立。

**Interfaces:**
- Consumes: `realtime_env_pb2_grpc.EnvironmentServiceStub`、`codec.*`
- Produces: `EnvironmentServiceClient(stub)`、`.create_session(scenario_id, role_assignment, personas, purpose, client_driven_slot) -> str`(返回 session_id)、`.submit_turn(utterance, inner_thought=None) -> TurnRecord | None`(提交并返回下一条**对手**轮次,若会话已结束返回 `None`)、`.end_session(reason="client_terminated") -> Episode`(主动结束并取回重建的 Episode)、`.drain_until_end() -> Episode`(不主动提交,持续拉取直到会话自然终止,用于纯观察场景)

- [ ] **Step 1: 写失败测试**

`tests/test_realtime_client.py`:
```python
import grpc
import pytest

from simulator.datamodel import (
    Scenario, RoleSlot, Persona, RoleBinding, RoleAssignment,
)
from simulator.backends import ScriptedBackend
from simulator.runner import run_episode
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.client import EnvironmentServiceClient
from simulator.realtime.server import create_server


def _scenario():
    return Scenario(
        id="s1", background="bg",
        role_slots=[RoleSlot("a", "候选"), RoleSlot("b", "对手")],
        conflict="c", norms=["n"], purpose="training",
    )


def _assignment(max_turns):
    return RoleAssignment(
        scenario_id="s1",
        bindings=[RoleBinding("a", "pa", "self_play"), RoleBinding("b", "pb", "frozen_reference")],
        max_turns=max_turns,
    )


def _personas():
    return {"pa": Persona("pa", ["t"], "m", "s", ["v"]),
            "pb": Persona("pb", ["t"], "m", "s", ["v"])}


def _opponent_factory(opponent_lines):
    def factory(scenario, assignment, personas, client_driven_slot):
        other = assignment.slot_ids() - {client_driven_slot}
        return {slot: ScriptedBackend({slot: list(opponent_lines)}) for slot in other}
    return factory


@pytest.fixture
def client_and_server():
    opponent_lines = ["opp1", "opp2"]
    server = create_server(_opponent_factory(opponent_lines), port=0, turn_timeout_s=2.0)
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
    stub = pb2_grpc.EnvironmentServiceStub(channel)
    client = EnvironmentServiceClient(stub)
    yield client, opponent_lines
    channel.close()
    server.stop(0)


def test_client_reconstructs_episode_matching_direct_run_episode(client_and_server):
    client, opponent_lines = client_and_server
    candidate_lines = ["cand1", "cand2"]

    session_id = client.create_session(
        scenario_id="s1", role_assignment=_assignment(4), personas=_personas(),
        purpose="training", client_driven_slot="a",
    )
    assert session_id

    for line in candidate_lines:
        client.submit_turn(line)
    episode = client.drain_until_end()

    # 直接调 run_episode 产出的"参照答案"：用同样的两条剧本（候选走
    # ScriptedBackend，与客户端逐轮提交语义一致）
    reference = run_episode(
        _scenario(), _assignment(4), _personas(),
        backends={
            "a": ScriptedBackend({"a": candidate_lines}),
            "b": ScriptedBackend({"b": opponent_lines}),
        },
        episode_id=episode.id,  # episode_id 由服务端生成，对齐后比较其余字段
    )

    assert [t.to_dict() for t in episode.turns] == [t.to_dict() for t in reference.turns]
    assert episode.terminated_reason == reference.terminated_reason == "max_turns"


def test_client_end_session_returns_episode_with_client_terminated_reason(client_and_server):
    client, _ = client_and_server
    client.create_session(
        scenario_id="s1", role_assignment=_assignment(100), personas=_personas(),
        purpose="training", client_driven_slot="a",
    )
    client.submit_turn("only-turn")
    episode = client.end_session()
    assert episode.terminated_reason == "client_terminated"
    assert len(episode.turns) >= 1
```

- [ ] **Step 2: 确认失败**

Run: `uv run --extra dev pytest tests/test_realtime_client.py -v`
Expected: FAIL(`ModuleNotFoundError: simulator.realtime.client`)

- [ ] **Step 3: 实现**

`src/simulator/realtime/client.py`:
```python
from __future__ import annotations

import queue

from simulator.datamodel import Episode, Persona, RoleAssignment, TurnRecord
from simulator.realtime.codec import (
    persona_to_proto, role_assignment_to_proto, turn_record_from_proto,
)
from simulator.realtime.proto import realtime_env_pb2 as pb2


class EnvironmentServiceClient:
    """EnvironmentService 的同步客户端包装器（未来 MindTrainer InteractTrain /
    MindAgent 经验采集直接复用的接口）。用直接 next() 拉取响应，不起消费者
    线程——每次调用严格对应"发一条 ClientMessage → 拉直到下一个边界事件"。
    逐轮累积 TurnRecord，在 EndSession 到达时于本地重建完整 Episode
    （§2.6.2："最终Episode摘要引用"，协议不重复内联整个 Episode）。"""

    def __init__(self, stub) -> None:
        self._stub = stub
        self._send_q: "queue.Queue[pb2.ClientMessage | None]" = queue.Queue()
        self._resp_iter = stub.Interact(self._request_gen())
        self._session_id = ""
        self._scenario_id = ""
        self._role_assignment: RoleAssignment | None = None
        self._episode_id = ""
        self._turns: list[TurnRecord] = []
        self._closed = False

    def _request_gen(self):
        while True:
            item = self._send_q.get()
            if item is None:
                return
            yield item

    def create_session(
        self,
        *,
        scenario_id: str,
        role_assignment: RoleAssignment,
        personas: dict[str, Persona],
        purpose: str,
        client_driven_slot: str,
    ) -> str:
        cs = pb2.CreateSession(
            scenario_id=scenario_id,
            role_assignment=role_assignment_to_proto(role_assignment),
            personas={pid: persona_to_proto(p) for pid, p in personas.items()},
            purpose=purpose,
            client_driven_slot=client_driven_slot,
        )
        self._send_q.put(pb2.ClientMessage(create_session=cs))
        ready = next(self._resp_iter)
        self._session_id = ready.session_ready.session_id
        self._scenario_id = scenario_id
        self._role_assignment = role_assignment
        self._episode_id = f"ep-{self._session_id}"
        return self._session_id

    def submit_turn(self, utterance: str, inner_thought: str | None = None) -> TurnRecord | None:
        """提交本轮候选话语，拉取直到下一次轮到候选（或会话已结束）。
        返回期间收到的最后一条对手轮 TurnRecord，会话已结束则返回 None。"""
        submit = pb2.SubmitTurn(utterance=utterance)
        if inner_thought is not None:
            submit.inner_thought = inner_thought
        self._send_q.put(pb2.ClientMessage(submit_turn=submit))
        last_opponent_turn: TurnRecord | None = None
        while True:
            msg = next(self._resp_iter)
            kind = msg.WhichOneof("payload")
            if kind == "turn_update":
                turn = turn_record_from_proto(msg.turn_update.turn)
                self._turns.append(turn)
                last_opponent_turn = turn
                if len(self._turns) % len(self._role_assignment.bindings) == 0:
                    return last_opponent_turn
            elif kind == "end_session":
                self._finish(msg.end_session)
                return None

    def drain_until_end(self) -> Episode:
        """不主动提交，持续拉取直到会话自然终止（超时/后端失败/max_turns）。"""
        while not self._closed:
            msg = next(self._resp_iter)
            kind = msg.WhichOneof("payload")
            if kind == "turn_update":
                self._turns.append(turn_record_from_proto(msg.turn_update.turn))
            elif kind == "end_session":
                self._finish(msg.end_session)
        return self._build_episode(self._last_reason)

    def end_session(self, reason: str = "client_terminated") -> Episode:
        self._send_q.put(pb2.ClientMessage(end_session=pb2.EndSession(reason=reason)))
        while not self._closed:
            msg = next(self._resp_iter)
            kind = msg.WhichOneof("payload")
            if kind == "turn_update":
                self._turns.append(turn_record_from_proto(msg.turn_update.turn))
            elif kind == "end_session":
                self._finish(msg.end_session)
        return self._build_episode(self._last_reason)

    def _finish(self, end_session_proto) -> None:
        self._closed = True
        self._last_reason = end_session_proto.reason
        self._send_q.put(None)

    def _build_episode(self, reason: str) -> Episode:
        return Episode(
            id=self._episode_id,
            scenario_id=self._scenario_id,
            role_assignment=self._role_assignment,
            turns=list(self._turns),
            terminated_reason=reason,
        )
```

> 说明:`submit_turn` 的"返回最后一条对手轮"依赖角色数为 2(候选+单一对手)这一常见场景——每次候选提交后恰好再收一轮对手回应即回到候选。这与 spec 场景一/二"候选 vs 单一对手/对手池中单个对手"的用例一致;若未来出现 3+ 角色位的会话,调用方改用 `drain_until_end()`/直接消费 `poll` 式接口即可,`submit_turn` 的这条边界假设在文档字符串中已注明。

- [ ] **Step 4: 确认通过 + 全回归**

Run: `uv run --extra dev pytest tests/test_realtime_client.py -v`
Expected: PASS(2 passed)

Run: `uv run --extra dev pytest -q`
Expected: 213 + 2 = 215 passed

- [ ] **Step 5: 提交**

```bash
git add src/simulator/realtime/client.py tests/test_realtime_client.py
git commit -m "feat: EnvironmentServiceClient — sync wrapper + Episode reconstruction parity with run_episode"
```

- [ ] Task 10 完成

---

### Task 11: 会话关闭持久化钩子(复用既有 `write_episode`)

**Files:** `tests/test_realtime_persistence.py`

依据 spec 补充 D.3(会话结束后产生的聚合结果回写 ControlPlane 时必须带 `run_id`,把计算层内部直连重新缝回全链路)。`EnvironmentServicer` 的 `on_session_closed` 钩子(Task 7 已预留)是接入点——本任务不新增生产代码,只验证"把 `write_episode` 接到 `create_server(on_session_closed=...)`"这条组装路径在真实 gRPC 会话下正确工作,证明钩子契约可用。

**Interfaces:**
- Consumes: `create_server(..., on_session_closed=...)`(Task 7)、`simulator.store_adapter.write_episode`(Plan 2,已合入 main)、`controlplane.gitstore.GitStore`(Plan 1)

- [ ] **Step 1: 写失败测试**

`tests/test_realtime_persistence.py`:
```python
import grpc
import pytest

from controlplane.gitstore import GitStore
from controlplane.paths import episode_file
from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleBinding, RoleAssignment
from simulator.realtime.codec import persona_to_proto, role_assignment_to_proto
from simulator.realtime.proto import realtime_env_pb2 as pb2
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server
from simulator.store_adapter import write_episode, read_episode


def _opponent_factory(scenario, assignment, personas, client_driven_slot):
    other = assignment.slot_ids() - {client_driven_slot}
    return {slot: ScriptedBackend({slot: ["opp1", "opp2"]}) for slot in other}


def test_on_session_closed_hook_persists_episode_to_controlplane(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    run_id = "run-realtime-1"

    def persist(episode):
        write_episode(store, run_id, episode)

    server = create_server(_opponent_factory, port=0, turn_timeout_s=2.0, on_session_closed=persist)
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
    stub = pb2_grpc.EnvironmentServiceStub(channel)

    import queue
    q: "queue.Queue" = queue.Queue()

    def gen():
        while True:
            item = q.get()
            if item is None:
                return
            yield item

    resp_iter = stub.Interact(gen())
    assignment = RoleAssignment(
        scenario_id="s1",
        bindings=[RoleBinding("a", "pa", "self_play"), RoleBinding("b", "pb", "frozen_reference")],
        max_turns=4,
    )
    personas = {"pa": persona_to_proto(Persona("pa", ["t"], "m", "s", ["v"])),
                "pb": persona_to_proto(Persona("pb", ["t"], "m", "s", ["v"]))}
    q.put(pb2.ClientMessage(create_session=pb2.CreateSession(
        scenario_id="s1", role_assignment=role_assignment_to_proto(assignment),
        personas=personas, purpose="training", client_driven_slot="a",
    )))
    ready = next(resp_iter)
    session_id = ready.session_ready.session_id
    episode_id = f"ep-{session_id}"

    end_msg = None
    for i in range(2):
        q.put(pb2.ClientMessage(submit_turn=pb2.SubmitTurn(utterance=f"cand-{i}")))
        while True:
            msg = next(resp_iter)
            if msg.WhichOneof("payload") == "turn_update":
                if msg.turn_update.turn.speaker_slot == "a":
                    break
            elif msg.WhichOneof("payload") == "end_session":
                end_msg = msg
                break
        if end_msg:
            break
    while end_msg is None:
        msg = next(resp_iter)
        if msg.WhichOneof("payload") == "end_session":
            end_msg = msg

    q.put(None)
    channel.close()
    server.stop(0)

    assert store.exists(episode_file(run_id, episode_id))
    persisted = read_episode(store, run_id, episode_id)
    assert persisted.id == episode_id
    assert persisted.terminated_reason == "max_turns"
    assert [t.speaker_slot for t in persisted.turns] == ["a", "b", "a", "b"]
```

- [ ] **Step 2: 确认失败**

Run: `uv run --extra dev pytest tests/test_realtime_persistence.py -v`
Expected: 若 Task 7 的 `on_session_closed` 钩子接线正确,预期**直接 PASS**(本任务是对既有钩子契约的验证性测试,不新增生产代码)。若失败,说明 Task 7 的钩子调用时机/参数有误,回到 `servicer.py` 的 `_drain_ready_events` 做最小修正(钩子已在该方法的 `"closed"` 分支调用,`episode` 对象已是完整 `Episode` 实例)。

- [ ] **Step 3: 确认通过 + 全回归**

Run: `uv run --extra dev pytest tests/test_realtime_persistence.py -v`
Expected: PASS(1 passed)

Run: `uv run --extra dev pytest -q`
Expected: 215 + 1 = 216 passed

- [ ] **Step 4: 提交**

```bash
git add tests/test_realtime_persistence.py
git commit -m "test: on_session_closed hook persists realtime Episode via existing write_episode (补充 D.3)"
```

- [ ] Task 11 完成

---

## Self-Review

### Spec 覆盖表(§2.6 + 补充 C)

| Spec 条目 | 需求 | 落地 Task | 落地方式 |
|---|---|---|---|
| 补充 C.1 | gRPC 双向流技术选型 | 1 | `realtime_env.proto` 定义 `stream ClientMessage → stream ServerMessage` |
| 补充 C.2 | 批量/实时共享 Simulation Runner 核心,分离进程 | 3, 6 | `run_episode` 扩展 `on_turn`(不重写循环);`SessionDriver` 在独立线程调用同一函数;服务进程(`server.py`)与既有批量模块无共享状态 |
| §2.6.1 | 会话生命周期状态机 | 2 | `session.py` 纯函数状态机,`CREATED→ACTIVE→COMPLETING→CLOSED`,`→FAILED` 分支 |
| §2.6.1 | CREATED→ACTIVE 无预检确认 | 7 | `_handle_create` 校验通过即 `driver.start()` 直接 ACTIVE,同一次 `Interact` 调用内完成 |
| §2.6.2 | 五类消息(CreateSession/SessionReady/TurnRequest/TurnUpdate/EndSession) | 1 | proto 定义;`TurnRequest` 具体化为 `SubmitTurn`(携带话语,见下方设计说明) |
| §2.6.2 | TurnUpdate 每轮完成后立即推送 | 3, 6, 7 | `on_turn` 回调 + `outbox` 队列 + `_drain_ready_events` 非阻塞轮询转发 |
| §2.6.3 | activation_trace 协议层强制项,区分"无"与"空列表" | 1, 4 | `optional google.protobuf.Struct activation_trace`;`HasField()` 语义已验证;codec 测试覆盖三态(未设置/None/实际字典) |
| §2.6.4 | 并发会话隔离,不共享可变状态 | 6, 7, 9 | 每会话独立 `SessionDriver` 实例(局部变量,无模块级共享注册表);Task 9 五会话并发实测无串话 |
| §2.6.5 | 服务端侧异常显式 EndSession,不简单断连 | 6, 8 | `role_backend_failure`/`timeout` 两条显式终止原因 + 真实 socket 测试 |
| §2.6.5 | 客户端侧异常靠连接检测,不需客户端配合 | 7, 8 | `Interact` 的 `finally` 块检测异常退出的 `request_iterator`,标记会话结束;断连后服务端不挂起(验证) |
| §2.6.5 | 超过最大轮次强制终止 | 3, 7 | `run_episode` 既有 `max_turns` 语义直接复用,无需额外实现 |
| §2.6.6 | RoleAssignment 落地 | 1, 4, 7 | `CreateSession.role_assignment` 强类型消息,`codec.role_assignment_to/from_proto` |
| §2.6.6 | 最终 Episode 摘要格式与 §2.5 动态诊断轨同一套 | 6, 10 | 全程用 `simulator.datamodel.Episode`/`TurnRecord`,零重新定义;客户端本地重建,不在协议内联整份 Episode(引用式,§2.6.2"摘要引用") |
| §2.6.7 | Session/TurnRecord/SessionOutcome 数据模型 | 2, 4, 6 | `SessionState` + `TurnRecord`(复用既有)+ `DriverEvent`/`EndSession.reason` 承担 SessionOutcome 语义 |
| §2.6.8 | 状态机纯函数可脱离真实 gRPC 单测 | 2 | `test_realtime_session.py` 零 gRPC 依赖 |
| §2.6.8 | 并发隔离用构造场景测试 | 9 | 5 会话并行,断言互不串话 |
| §2.6.8 | 异常路径可控制触发,不需真实远程服务 | 8 | 全部用 `127.0.0.1` 本地 socket,确定性、无外部依赖 |
| §2.2 | 环境服务接口必须显式接受角色-模型后端映射 | 1, 7 | `CreateSession.role_assignment` 必填;`backends_factory` 由调用方(`create_server` 的调用方)注入,服务内部不写死 |

### TurnRequest 设计偏离说明

spec §2.6.2 的消息表把 `TurnRequest` 描述为"请求驱动第N轮"(读起来像无payload的拉取信号)。本计划把它具体化为携带话语的 `SubmitTurn`,理由:协议设计止于"消息结构和会话状态机"(spec §2.6 末段原话),字段级设计明确留给 writing-plans 阶段。经技术验证(本计划设计阶段的会话驱动分析):MindTrainer/MindAgent 的角色由**调用方自己的模型**驱动,该模型运行在调用方进程/GPU 上,SocialSimulator 服务端并不持有它——所以"驱动第N轮"这个动作在物理上必须携带调用方已经算出的话语,而非服务端主动计算后等客户端"拉取"。`SubmitTurn` 是这条物理约束下最直接的字段设计,不改变消息表的五类角色划分(仍是同一条 `TurnRequest` 位置的消息,只是补full了字段)。

### 占位符扫描

无 TBD/TODO;每个代码步骤含完整可运行代码;无"类似 Task N"式引用(各任务代码自包含,即便交叉引用其他任务的函数签名也给出完整调用点)。所有 protoc 生成代码路径、字段名、异常类型均在设计阶段用真实本地环境验证过(见计划撰写过程中的沙箱验证,非凭空假设)——包括:proto3 `optional` 字段 `HasField()` 语义、`Struct.update()` 的类型强制转换陷阱(已用强类型字段规避)、嵌套包路径下 protoc 生成代码的 import 语句正确性、真实 socket 上的并发会话隔离、abrupt disconnect 的 `finally` 块检测、同步 `next()` 客户端模式。

### 类型一致性

- `TurnRecord`/`RoleAssignment`/`RoleBinding`/`Persona` 全程复用 `simulator.datamodel` 既有定义,不重新定义子集或影子类型——`codec.py` 只做单向映射,任何一侧字段增删都会在 `*_round_trip` 测试里立刻暴露。
- `run_episode` 新签名(`on_turn` 为第七个、末位参数)与 Plan 3 已加入的 `stop_predicate`(第六个参数)共存,两者都是可选关键字,互不冲突(Task 3 `test_on_turn_and_stop_predicate_compose` 显式覆盖组合场景)。
- `SessionState` 常量字符串(`"CREATED"`/`"ACTIVE"`/...)与 `EndSession.reason` 的字符串值(`"max_turns"`/`"goal_reached"`/`"client_terminated"`/`"role_backend_failure"`/`"timeout"`)是两套独立的标识符空间,不混用——前者是会话状态,后者是终止原因,`driver.py`/`servicer.py` 中两者各自locals 命名清晰区分(`self.state` vs `episode.terminated_reason`/`DriverEvent.payload` 字符串)。
- `DriverEvent.kind` 三态(`"turn"`/`"closed"`/`"failed"`)在 Task 6 定义、Task 7 `_drain_ready_events` 三分支全部消费,无遗漏分支。
- `EnvironmentServicer.Interact` 与 `EnvironmentServiceClient` 对 `ClientMessage`/`ServerMessage` 的 `oneof` 字段名(`create_session`/`submit_turn`/`end_session` / `session_ready`/`turn_update`/`end_session`)双端完全对齐,Task 7/10 的测试均实际跑通真实序列化/反序列化,非签名层面的假设一致。

### 依赖顺序(实现顺序即任务号)

Task 1(proto+依赖)是唯一无依赖基石。2(状态机)、3(run_episode 扩展)、4(codec)均只依赖 Task 1 的生成代码或 Plan 2 既有 `datamodel`,可并行起步。5(`ClientDrivenBackend`)依赖 3(需要 `run_episode` 的 `on_turn` 签名已就位,虽然 backend 本身不直接调用 `run_episode`,但设计意图上是为它准备的驱动组件)。6(`SessionDriver`)依赖 2+3+5。7(servicer+server)依赖 1+4+6,是第一次接触真实 gRPC socket。8(失败路径)、9(并发隔离)依赖 7,复用其 fixture。10(客户端包装器)依赖 1+4+7。11(持久化钩子验证)依赖 7(`on_session_closed` 已预留)+ Plan 2 既有 `store_adapter`。建议线性 1→11。

---

## Execution Handoff

**执行方式:** 用 superpowers **subagent-driven-development**(推荐)逐任务执行:每任务 fresh implementer(haiku,机械 TDD)→ 独立验证该 commit(`git show --stat` 核范围 + 全回归)→ review package → task reviewer(sonnet)→ 评审通过才记 ledger → 下一任务。Task 7 起涉及真实 gRPC socket 与多线程,implementer 需严格按本计划给出的完整代码落地(避免自由发挥引入竞态条件);task reviewer 对 Task 6-9 应额外核查:线程是否 daemon(避免测试进程挂起)、`queue.Queue` 的 `timeout` 参数是否总有值(避免无超时阻塞)、`grpc.Server` 是否在每个测试结束前 `.stop(0)`(避免端口泄漏累积导致后续任务的 CI 变慢)。

**分支:** 从合入了 Plan 3 的 `main`(ddca9f9)新建 `plan4-realtime-env-service` worktree。

**依赖调用:** `uv sync --extra dev` 后 `uv run --extra dev pytest -q`。基线 178 passed(Plan 3 收尾状态),每任务后应递增,Task 11 完成后应为 216 passed。

**验证基线:** 全部 11 任务完成后,`uv run --extra dev pytest -q` 全绿;Plan 2/3 既有全部 runner/diagnostic 测试在 Task 3 后仍绿(回归保护是 Task 3 的验收硬条件,与 Plan 3 Task 8 扩展 `stop_predicate` 时的验收标准同规格)。

**已知后续依赖本计划的工作(不在本计划范围内)**:MindTrainer 的 InteractTrain Trajectory Source 实现(会是 `EnvironmentServiceClient` 的第一个真实调用方,需要真实的"自博弈模型"提供 `submit_turn` 的话语来源,即真实 LLM 推理,而非 `ScriptedBackend`)、MindAgent 的模拟部署驱动器(第二个调用方,角色互换:MindAgent 自身是 `client_driven_slot`,由完整 SimpleActio 实例而非裸模型驱动)、SimpleActio 的 `ActivationTrace` 真正写入 `TurnRecord.activation_trace`(本计划协议层已预留字段,填充逻辑属于 SimpleActio 计划)、spec §2.2 的"开发者工具接口"(人工 CLI 交互式调试,独立 dev-only 功能)、spec 补充 E 的 `FailureRecord` 持久化与 T1 自动重试(跨子系统横切设计)。

**收尾:** whole-branch review → fix-wave(如需)→ finishing-a-development-branch(合入 main;因 session git-fenced,ff-only merge 在 main checkout 终端执行)。

**Ledger:** `.superpowers/sdd/2026-09-28-realtime-env-service/progress.md`。

---

## 实施后修订(Post-implementation amendments)

本计划的 11 个任务落地并合入分支后,最终评审与后续补强修订了下列接口。**以下修订优先于上文任务里的代码块与 Interfaces 描述**;上文保留原样,作为当时的计划记录。

- **`EnvironmentServiceClient.submit_turn`(Task 10)**:返回类型由 `TurnRecord | None` 改为 `list[TurnRecord]`——从客户端提交起,到"再次轮到客户端"之前收到的全部对方轮次,**不含**客户端自己那一轮的回显。客户端按 `bindings[turn_index % n]` 推算轮到谁,不再假定只有 2 个角色、也不再假定客户端先说。新增 `opening_turns`(客户端后说时,`create_session` 返回前收到的对方开场轮)、`closed` 属性与 `SessionRejectedError`(会话在 create 阶段被拒绝)。这是破坏性变更,仓库内无其他调用方。
- **CreateSession 校验(Task 7)**:`_handle_create` 在启动 driver 之前经 `validation.validate_create_session` 校验;违规、重复 `create_session`、create 之前收到 `submit_turn`/`end_session`、迟迟不发 create,一律以 `EndSession(reason="invalid_session")` 显式拒绝并结束流。服务端自身依赖故障(`backends_factory` 抛异常或漏返回后端、`scenario_resolver` 抛非 `LookupError`)不冒充客户端输入错误,以 `role_backend_failure` 拒绝(可能瞬态,T1)。
- **新增可选参数(Task 7)**:`create_server(..., scenario_resolver, max_turns_cap, create_timeout_s, backend_timeout_s, keepalive_time_ms, keepalive_timeout_ms)`;`maximum_concurrent_rpcs` 与 `max_workers` 对齐。服务端后端调用经新增的 `TimeoutBackend` 包装,超时以独立原因 `role_backend_timeout` 结束会话。
- **新增终止原因**:`client_disconnected`(客户端流未发 `end_session` 即结束,判 FAILED、不落盘)、`role_backend_timeout`。
- **`SessionDriver`(Task 6)**:新增 `request_abort`(与优雅结束的 `request_end` 区分,收尾为 `failed`,且首个原因为准);`ClientDrivenBackend` 新增 `request_end()` 与 `SessionEndRequested`,唤醒哨兵不再被当作一句真实(空)发言写进 Episode。
- **仍未处理**:`Interact` 的 `finally` 兜底无条件按断连中止、不区分客户端是否已发 `end_session`(极窄的取消竞态,最坏是把一次已优雅结束但尚未收尾的会话记为 FAILED);keepalive 只设置未验证;超时的后端线程无法强杀,会跑到自然结束后丢弃结果。
