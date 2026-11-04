# 记忆按情境世界组织 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 记忆从"一个人格一份库"改成"一个模拟世界一份库，人格是其中的主体"：部署一个情境一个世界（智能体位与对手位都积累），训练一条 rollout 一个世界，种子按情境。

**Architecture:** 端口在打开时绑定 `(世界, 主体)`，`MemoryBackend` 五个方法不变；部署管理面 `DeploymentMemoryStore` 的方法改为按世界 id，替身 `FileTupleStore` 与 `StarlingDeploymentStore` 两个实现过同一套契约测试。训练副本 `TrainingCopies` 换成 `TrainingWorlds`：客户端建 rollout 世界、服务端按 episode id 加入。MindAgent 改用世界锁、整世界快照、按情境的验证重放；起点摘要与 `memory.digests` 按情境。

**Tech Stack:** Python 3.12，pytest（`uv run --offline pytest`），Starling（`starling-memory @ 730e415`，可选 extra `memory`），gRPC 回环服务端。

**Spec:** `docs/superpowers/specs/2026-11-04-scenario-world-memory-design.md`；权威设计在 `docs/design/`（00、20、30、40、50、90 篇，新内容标 [计划中]）。执行者两者都读，冲突以 `docs/design/` 为准。

## Global Constraints

- 面向用户的文字、注释与提交信息用中文；代码、标识符原样。注释密度与命名照周围代码。
- `world_id` 与 `holder` 都过 `PERSONA_ID_PATTERN`（`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`，`fullmatch`）。
- 事件 key：`<前缀>#<turn_index>@<主体>`。训练前缀是逻辑 episode id；部署前缀是 `<round_id>/<部署 episode id>`；门② 的 key 不变：`<proposal_id>#<k>`。
- `SESSION_CLOSE_WAIT_S = 70.0`（服务端 `resource_join_timeout_s` 默认 60 秒加 10 秒）；轮询间隔 0.2 秒。
- 布局（替身后缀 `.json`，Starling 后缀 `.db`）：
  - `deploy/<scenario_id><后缀>`：部署世界；`deploy/<scenario_id>.lock`：世界锁；
  - `snapshots/<round_id>/deploy-<episode_id><后缀>` 加同名 `.world` 旁注（内容是 `scenario_id`）：起点快照；
  - `snapshots/<round_id>/verify-<scenario_id><后缀>`：验证快照；`snapshots/<round_id>/verify/`：验证临时世界；
  - `seeds/<seed_id>/<scenario_id>.db`：种子世界；`runs/<run_id>/<逻辑 episode id>.db`：rollout 世界。
- 只有 `src/actio/memory_starling.py` 导入 `starling`；`mindagent`、`mindtrainer`、`simulator` 不导入 `factory`（`tests/test_boundaries*.py` 的 ast 扫描守着）。
- 失败文字只报类名与 id，不含记忆根路径与端点地址。
- 每个任务结束时，它 Step 4 列出的文件要通过：`uv run --offline pytest -q -m 'not slow' -p no:cacheprovider <文件>`。最后一个任务跑全量。基线是 `4596 passed, 3 skipped`；总数会变，但不许有失败。
- Task 1、3、5 改了 `mindagent` 调用的管理面与布局接口。所以从 Task 1 起，`tests/test_ma_*.py` 与部署相关的 `tests/test_factory_handlers.py` 用例会失败，到 Task 6、7 结束时恢复。中间的任务不要为了让它们通过去加兼容层。
- `docs/design/` 里本计划落地的行，状态从 [计划中] 改成 [已落地]，在最后一个任务统一做；提交前跑 `python3 docs/design/tools/sync_docs.py`、`check_docs.py`、`check_consistency.py`，0 错误。

## Review Focus

- **三个及以上角色位的情境**：现有夹具都是两位。同一轮被两个主体各写一次，两条记忆都要在、key 不撞；首轮 `digest_before` 不等于起点时，已提交的 episode 仍复用。Task 2 与 Task 6 各加一条三位用例。
- **服务端收尾慢于客户端**：客户端 rollout 结束时服务端端口还开着（join 最长 60 秒）。客户端要等端口关完才删世界，等不到要留目录进保留表、rollout 照常返回。Task 9 加一条"服务端端口晚 0.5 秒关"的用例。
- **部署对手位没登记就开会话**：绕过部署驱动器直接开的部署会话查不到 round_id，对手位用空 `TupleMemory` 并记 warning，不抛。Task 7 加用例。
- **同一个情境世界被两个轮次碰**：轮次 R1 回滚失败留下快照，轮次 R2 开 episode 时要延后，原因里写出 R1 的 round id。Task 6 加用例（快照目录里预放一份别的 round id 的快照）。
- **旧布局的记忆根**：`deploy/` 下有 `pa.json` 这类不是已知情境名的文件，或 `snapshots/` 下有 `deploy-...-pa.json` 旧名，装配期退出码 2，信息写出要清空的目录。Task 10 加用例。

---

### Task 1: 替身账本带主体，端口绑定 (世界, 主体)

**Files:**
- Modify: `src/actio/memory.py`（`_Ledger`、`TupleMemory`、`FileTupleStore`、`_FilePort`）
- Modify: `src/actio/memory_port.py:95-126`（`DeploymentMemoryStore`）
- Test: `tests/test_actio_file_tuple_store.py`、`tests/test_actio_memory_port.py`、`tests/test_actio_memory_contract.py`

**Interfaces:**
- Produces:
  - `_Ledger.recall(query, *, limit, holder: str | None = None)`：`holder` 非空时只返回该主体名下的条目。
  - `_Ledger.begin(event, *, holder: str)`；事件行多一个 `holder` 键；条目 `MemoryEntry` 不改 schema，账本另存 `holders: dict[entry_id, holder]`（`to_json`/`from_json` 带上，缺省为空表，读旧账本时所有条目属于 `""`）。
  - `TupleMemory.world(holder: str) -> TupleMemory`：同一账本上绑定另一主体的端口视图（训练替身的 rollout 世界用）。`TupleMemory()` 不带主体时 `holder=""`，行为与今天一致。
  - `DeploymentMemoryStore`（协议）八个方法：`open(world_id, holder)`、`remember_event(world_id, holder, key, text, *, interlocutor)`、`abandon_event(world_id, key)`、`pending_events(world_id)`、`backup_to(world_id, path) -> str`、`restore_from(world_id, path) -> str`、`digest(world_id) -> str`、`wait_closed(world_id, timeout_s: float) -> bool`。
  - `FileTupleStore` 实现上述八个方法：文件 `<root>/<world_id>.json`；`_open: dict[world_id, int]` 由 `threading.Lock` 守护（端口会在服务端线程关）；`wait_closed` 用 `threading.Condition`，计数归零时 `notify_all`。

- [x] **Step 1: 写失败的测试**（`tests/test_actio_file_tuple_store.py`，新增，旧的按人格用例同步改成按世界）

```python
def test_two_holders_in_one_world_recall_only_their_own(tmp_path):
    store = FileTupleStore(tmp_path)
    a, b = store.open("s1", "pa"), store.open("s1", "pb")
    a.remember(MemoryEvent("e#1@pa", "pb：预算砍一半", "pb"))
    b.remember(MemoryEvent("e#0@pb", "pa：我们先谈预算", "pa"))
    assert [h.text for h in a.recall("预算", limit=5)] and all("砍一半" in h.text for h in a.recall("预算", limit=5))
    assert all("先谈" in h.text for h in b.recall("预算", limit=5))

def test_the_world_digest_covers_every_holder(tmp_path):
    store = FileTupleStore(tmp_path)
    before = store.digest("s1")
    store.remember_event("s1", "pb", "k#1@pb", "pa：你好", interlocutor="pa")
    assert store.digest("s1") != before

def test_restore_is_refused_while_any_holder_port_of_the_world_is_open(tmp_path):
    store = FileTupleStore(tmp_path)
    snap = tmp_path / "snap.json"
    store.backup_to("s1", snap)
    port = store.open("s1", "pb")
    with pytest.raises(MemoryFailureError) as exc:
        store.restore_from("s1", snap)
    assert exc.value.transient is False
    port.close()
    store.restore_from("s1", snap)

def test_another_worlds_open_port_does_not_block_a_restore(tmp_path):
    store = FileTupleStore(tmp_path)
    snap = tmp_path / "snap.json"
    store.backup_to("s1", snap)
    other = store.open("s2", "pa")
    store.restore_from("s1", snap)
    other.close()

def test_wait_closed_returns_when_a_port_closes_on_another_thread(tmp_path):
    store = FileTupleStore(tmp_path)
    port = store.open("s1", "pa")
    threading.Timer(0.1, port.close).start()
    assert store.wait_closed("s1", 2.0) is True
    store.open("s1", "pa")
    assert store.wait_closed("s1", 0.05) is False

def test_world_ids_and_holders_are_checked_before_any_path_is_built(tmp_path):
    store = FileTupleStore(tmp_path)
    for bad in ("", "../x", "a/b", ".hidden", "p1\n"):
        with pytest.raises(ActioDataError):
            store.open(bad, "pa")
        with pytest.raises(ActioDataError):
            store.open("s1", bad)
```

`tests/test_actio_memory_port.py::test_both_protocols_are_checked_by_method_names` 改成断言协议含 `wait_closed`。`tests/test_actio_memory_contract.py` 的两个实现各加 `holder` 参数后照旧过。

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_actio_file_tuple_store.py tests/test_actio_memory_port.py`
Expected: FAIL（`open()` 参数个数不对、没有 `wait_closed`）

- [x] **Step 3: 实现**

`_Ledger.begin` 的去重键仍是 `key`（key 已带主体）；`recall` 在 `holder` 非空时先按 `holders` 过滤条目再打分。`digest()` 把 `holders` 并进条目字典（`{"holder": ..., **entry_without_id}`）后排序，所以主体不同的同内容条目摘要不同。`TupleMemory.world(holder)` 返回共享 `_ledger` 的新端口对象，各自有 `closed` 状态。

- [x] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_actio_file_tuple_store.py tests/test_actio_memory_port.py tests/test_actio_memory_contract.py tests/test_actio_instance.py`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add src/actio/memory.py src/actio/memory_port.py tests/test_actio_file_tuple_store.py tests/test_actio_memory_port.py tests/test_actio_memory_contract.py
git commit -m "feat(actio): 替身账本带主体,部署管理面按世界 id,加 wait_closed"
```

### Task 2: 事件 key 带主体

**Files:**
- Modify: `src/actio/instance.py:193-212`（`_remember_new`）、`instantiate`
- Modify: 三个调用方，传入它们已经知道的人格：
  - `src/factory/memory_translate.py:61-79`：`memory_role_backend` 加 keyword-only 参数 `holder: str`；
  - `src/factory/agent.py:309-312`：传 `binding.persona_id`；
  - `src/factory/interact_policy.py:376-378`：传 `interlocutors[cfg.trained_slot]`；
  - `src/mindagent/rounds.py` 里造 `make_instance` 的那个闭包：`MakeInstance` 的签名改成 `(episode_id, port, holder)`，部署驱动器传智能体位人格。
- Test: `tests/test_actio_instance.py`、`tests/test_factory_memory_translate.py`

**Interfaces:**
- Consumes: Task 1 的 `TupleMemory.world(holder)`。
- Produces: `ActioInstance` 多一个 keyword-only 构造参数 `holder: str`（可写端口时必填，与 `episode_id` 同一处在 `instantiate` 校验，缺了抛 `ActioDataError`）；事件 key `f"{episode_id}#{turn_index}@{holder}"`。`instantiate(..., holder=...)` 透传。

- [x] **Step 1: 写失败的测试**

```python
def test_event_keys_carry_the_writing_holder():
    world = TupleMemory()
    inst = instantiate(StubBackend(), memory=world.world("pb"), episode_id="ep", holder="pb",
                       interlocutors={"a": "pa", "b": "pb"})
    inst.respond(PERSONA, SCENARIO, [TurnRecord(0, "a", "你好")], "b")
    assert world._live().events.keys() == {"ep#0@pb"}

def test_three_slots_write_the_same_turn_once_per_listener():
    world = TupleMemory()
    b = instantiate(StubBackend(), memory=world.world("pb"), episode_id="ep", holder="pb", interlocutors=SLOTS3)
    c = instantiate(StubBackend(), memory=world.world("pc"), episode_id="ep", holder="pc", interlocutors=SLOTS3)
    history = [TurnRecord(0, "a", "预算砍一半")]
    b.respond(PERSONA, SCENARIO, history, "b")
    c.respond(PERSONA, SCENARIO, history, "c")
    assert set(world._live().events) == {"ep#0@pb", "ep#0@pc"}
    assert world.world("pb").recall("预算", limit=3) and world.world("pc").recall("预算", limit=3)

def test_a_writable_port_without_a_holder_is_refused():
    with pytest.raises(ActioDataError):
        instantiate(StubBackend(), memory=TupleMemory(), episode_id="ep", interlocutors={"a": "pa"})
```

`SLOTS3 = {"a": "pa", "b": "pb", "c": "pc"}`。已有用例里断言 key 字面量 `ep#3` 的改成 `ep#3@<holder>`。

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_actio_instance.py`
Expected: FAIL（`holder` 未知参数）

- [x] **Step 3: 实现** 让 `instantiate` 接收 `holder` 参数，`_remember_new` 按新格式拼 key。`replay_read_only=True` 的实例不要求传 `holder`。三个调用方按上面的 Files 一节改。

- [x] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_actio_instance.py tests/test_actio_memory_experience.py tests/test_factory_memory_translate.py`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add src/actio/instance.py src/factory/memory_translate.py src/factory/agent.py src/factory/interact_policy.py src/mindagent/rounds.py src/mindagent/deployment.py tests/test_actio_instance.py tests/test_factory_memory_translate.py
git commit -m "feat(actio): 事件 key 带写入方主体,可写端口必须给 holder"
```

### Task 3: Starling 部署管理面按世界

**Files:**
- Modify: `src/actio/memory_starling.py:344-500`（`StarlingDeploymentStore`），`StarlingMemory.open`（已有 `persona_id` 参数改名 `holder`，作 Starling 的 `agent=`）
- Test: `tests/test_actio_starling_store.py`、`tests/test_actio_deployment_store_contract.py`

**Interfaces:**
- Consumes: Task 1 的协议签名。
- Produces: `StarlingDeploymentStore` 的八个方法，签名同 Task 1；文件 `<root>/<world_id>.db`；`_open` 计数加锁、`wait_closed` 同 Task 1 的 `Condition` 写法；`remember_event(world_id, holder, ...)` 以 `agent=holder` 开短连接。

- [x] **Step 1: 写失败的测试**：把 `tests/test_actio_deployment_store_contract.py` 的六个参数化用例改成按 `("s1", holder)` 调用，并加入 Task 1 Step 1 的前五条（同名，参数化到两个实现）。`tests/test_actio_starling_store.py` 里 `_lib(tmp_path, persona)` 改成 `_lib(tmp_path, world)`。

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_actio_deployment_store_contract.py tests/test_actio_starling_store.py`
Expected: FAIL（Starling 一侧签名不对）

- [x] **Step 3: 实现**：照 Task 1 的语义改 `StarlingDeploymentStore`；`restore_from` 的守卫读 `_open[world_id]`；`digest(world_id)` 不存在时照旧 `empty_digest()`。

- [x] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_actio_deployment_store_contract.py tests/test_actio_starling_store.py tests/test_actio_memory_starling.py`
Expected: PASS（未装 Starling 时这些文件按现有 `importorskip` 跳过，本机已装）

- [x] **Step 5: 提交**

```bash
git add src/actio/memory_starling.py tests/test_actio_deployment_store_contract.py tests/test_actio_starling_store.py tests/test_actio_memory_starling.py
git commit -m "feat(actio): Starling 部署管理面按世界 id,端口以 agent=<主体> 打开"
```

### Task 4: 同一人格占两个角色位拒绝

**Files:**
- Modify: `src/mindtrainer/training_config.py`（`validate_training_config`）
- Modify: `src/factory/agent.py:75`（`read_agent_settings`）
- Modify: `src/mindtrainer/dispatch.py`（合成绑定之后、`_resolve_memory` 之前再验一次，复用 `validate_training_config` 即可）
- Test: `tests/test_mt_config.py`、`tests/test_factory_handlers.py`、`tests/test_mt_dispatch_synthesis.py`

**Interfaces:**
- Produces: 原因文字固定为 `bindings: persona {pid!r} is bound to more than one slot`（训练与部署两处同一句）。

- [x] **Step 1: 写失败的测试**

```python
# tests/test_mt_config.py
def test_a_persona_bound_to_two_slots_is_refused():
    cfg = _interact_config(bindings=[_b("a", "pa"), _b("b", "pa")])
    assert validate_training_config(cfg) == "bindings: persona 'pa' is bound to more than one slot"

# tests/test_factory_handlers.py
def test_an_agent_config_binding_one_persona_twice_exits_2(tmp_path):
    store = _store_with_agent_yaml(tmp_path, bindings=[("a", "pa", "self_play"), ("b", "pa", "scripted")])
    with pytest.raises(FactoryConfigError, match="bound to more than one slot"):
        read_agent_settings(store)
```

`tests/test_mt_dispatch_synthesis.py` 加一条：合成出的绑定若人格重复（夹具强制），指令判 T2、原因同上。

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_mt_config.py tests/test_factory_handlers.py tests/test_mt_dispatch_synthesis.py -k "two_slots or twice or more_than_one"`
Expected: FAIL

- [x] **Step 3: 实现**：两处各加一次 `len(set(pids)) != len(pids)` 判断，取第一个重复的人格写进原因。

- [x] **Step 4: 跑测试确认通过**

Run: 同 Step 2，去掉 `-k`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add src/mindtrainer/training_config.py src/mindtrainer/dispatch.py src/factory/agent.py tests/test_mt_config.py tests/test_factory_handlers.py tests/test_mt_dispatch_synthesis.py
git commit -m "feat: 同一人格占两个角色位时拒绝(训练 T2,部署配置退出码 2)"
```

### Task 5: MemoryLayout 与世界锁

**Files:**
- Modify: `src/mindagent/memory_layout.py`
- Test: `tests/test_ma_memory_layout.py`

**Interfaces:**
- Produces（`MemoryLayout` 的方法，旧的按人格方法删掉）：
  - `world_lock(scenario_id) -> Path`（`deploy/<scenario_id>.lock`）、`hold_world(scenario_id)`（上下文管理器，`hold_file_lock`）
  - `snapshot(round_id, episode_id) -> Path`（`snapshots/<round>/deploy-<episode><后缀>`）
  - `write_world_tag(snapshot: Path, scenario_id)`、`world_of(snapshot: Path) -> str`（读 `.world` 旁注；缺失抛 `MindAgentDataError`）
  - `verify_snapshot(round_id, scenario_id) -> Path`、`verify_scratch(round_id) -> Path`（不变）
  - `round_snapshots(round_id) -> list[Path]`（只列 `deploy-*<后缀>`，不含 `.world`）
  - `uncommitted_snapshots(scenario_id) -> list[Path]`（任何轮次里 `.world` 内容等于 `scenario_id` 的起点快照）
  - `any_uncommitted_snapshots() -> list[Path]`（不变语义）
  - `legacy_entries() -> list[str]`（Task 10 用）：`deploy/` 下不是 `.lock` 的文件名里 stem 不在 `known_scenarios` 的、`snapshots/*/` 下没有同名 `.world` 的 `deploy-*` 文件；参数 `known_scenarios: Iterable[str]`。

- [x] **Step 1: 写失败的测试**（重写 `tests/test_ma_memory_layout.py` 的六条）

```python
def test_paths_follow_the_world_layout(tmp_path):
    lay = MemoryLayout(tmp_path)
    assert lay.world_lock("s1") == tmp_path / "deploy" / "s1.lock"
    assert lay.snapshot("r1", "deploy-m-s1-1") == tmp_path / "snapshots" / "r1" / "deploy-deploy-m-s1-1.json"
    assert lay.verify_snapshot("r1", "s1") == tmp_path / "snapshots" / "r1" / "verify-s1.json"

def test_uncommitted_snapshots_are_found_by_their_world_tag_not_their_name(tmp_path):
    lay = MemoryLayout(tmp_path)
    for rid, eid, sid in (("r1", "deploy-m-s1-1", "s1"), ("r2", "deploy-m-s2-1", "s2")):
        p = lay.snapshot(rid, eid); p.parent.mkdir(parents=True, exist_ok=True); p.write_text("{}")
        lay.write_world_tag(p, sid)
    assert [p.parent.name for p in lay.uncommitted_snapshots("s1")] == ["r1"]

def test_the_world_lock_is_exclusive_and_released(tmp_path): ...   # 照旧用例改成 hold_world("s1")

def test_legacy_entries_name_old_persona_files_and_untagged_snapshots(tmp_path):
    lay = MemoryLayout(tmp_path)
    (tmp_path / "deploy").mkdir(); (tmp_path / "deploy" / "pa.json").write_text("{}")
    (tmp_path / "deploy" / "s1.json").write_text("{}")
    old = tmp_path / "snapshots" / "r1" / "deploy-m-s1-1-pa.json"; old.parent.mkdir(parents=True); old.write_text("{}")
    assert lay.legacy_entries(known_scenarios=["s1"]) == ["deploy/pa.json", "snapshots/r1/deploy-m-s1-1-pa.json"]
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_ma_memory_layout.py`
Expected: FAIL

- [x] **Step 3: 实现**：`scenario_id` 过 `_check_segment`；`.world` 旁注与快照同名、后缀 `.world`；`legacy_entries` 返回相对记忆根的 POSIX 路径，排序。

- [x] **Step 4: 跑测试确认通过**

Run: 同 Step 2
Expected: PASS（调用方还没改，`tests/test_ma_*` 其余文件此时会失败，由 Task 6 一并改）

- [x] **Step 5: 提交**

```bash
git add src/mindagent/memory_layout.py tests/test_ma_memory_layout.py
git commit -m "feat(mindagent): 记忆布局按情境世界:世界锁、整世界快照与 .world 旁注、旧布局识别"
```

### Task 6: 部署轮次按世界

**Files:**
- Modify: `src/mindagent/deployment.py:261-414`（`run_deployment_episodes`、`_restore`、`_reusable`）
- Modify: `src/mindagent/rounds.py`（`_PersonaLocks` → `_WorldLocks`，`_binding_payload`，`_rollback_with`，`_run` 的验证段，`make_start_round_handler`）
- Modify: `src/mindagent/verify.py:66-134`
- Test: `tests/test_ma_deployment.py`、`tests/test_ma_rounds.py`、`tests/test_ma_verify.py`、`tests/test_ma_deployment_starling.py`、`tests/test_ma_e2e.py`

**Interfaces:**
- Consumes: Task 1/3 的管理面，Task 5 的布局。
- Produces:
  - `run_deployment_episodes(..., episode_digests: Mapping[str, str], on_episode_start: Callable[[str, str], None], register_episode: Callable[[str, str], None] = lambda eid, rid: None, unregister_episode: Callable[[str], None] = lambda eid: None, close_wait_s: float = SESSION_CLOSE_WAIT_S, ...)`；其余参数不变。
  - `SESSION_CLOSE_WAIT_S = 70.0` 定义在 `src/mindagent/deployment.py`。
  - 轮次产物 `episode_digests: {episode_id: digest}`；重入时旧形状（值是 dict）当作没记过（`recorded()` 返回 None），episode 重跑。
  - `verify(draft, findings, episodes, *, base_instance_factory: Callable[[tuple[Skill, ...], str], object], ...)`：第二个参数是 `scenario_id`；每个情境的基线与候选实例在一次 `verify()` 里各造一次、复用。
  - `make_start_round_handler(..., register_episode=..., unregister_episode=...)` 透传给驱动器。
- 每个部署 episode 的顺序（替代 `deployment.py:331-414` 的循环体）：
  1. `world = scenario_id`；`others = [p for p in layout.uncommitted_snapshots(world) if p.parent.name != round_id]` 非空 → `DeferredError(f"world {world} has an uncommitted start snapshot of round {others[0].parent.name}; restore or delete it by hand")`。
  2. 快照已经存在时：先 `wait_closed`，再 `_restore(store, world, snap, expected, eid)`。快照不存在时，按这个顺序做：`write_world_tag(snap, world)`、`backup_to(world, snap)`、`on_episode_start(eid, digest)`。旁注要先写：如果在两步之间崩溃，留下的只是一个旁注；`round_snapshots` 只列快照文件，不会把它当成在途的快照；Task 5 的旧布局检查也不会因为"有快照却没有旁注"误判成旧格式。
  3. 每次尝试：`register_episode(eid, round_id)` → `open(world, agent_persona)` → `make_instance(f"{round_id}/{eid}", port, holder=agent_persona)` → 会话 → 关实例与端口 → `unregister_episode(eid)` → `wait_closed(world, close_wait_s)`；超时：要恢复时抛 `RestoreFailedError`，否则抛 `DeferredError`。
  4. 失败：`_restore(...)`；成功：落盘、按前缀 `f"{round_id}/{eid}#"` 放弃 `pending_events(world)`、删快照与 `.world`。
  5. 复用判据 `_reusable(episode, agent_slot, skill_version, recorded_digest)`：有智能体轮次、`recorded_digest is not None`、每个智能体轮次技能版本等于当前的；**不再**比 `digest_before`。
- `_WorldLocks.hold(scenario_ids)`：按 `sorted` 取 `layout.hold_world`；被占抛 `DeferredError("world library locked: ...")`。轮次处理器在写任何东西之前 `hold(config.scenario_ids)`；回滚时 `hold` 轮次产物里的 `scenario_ids`。
- 回滚 `_rollback_with`：对 `round_snapshots(env.id)` 的每个快照 `world_of(snap)` 找世界，`wait_closed` 后 `restore_from(world, snap)`，删快照与旁注。
- 验证段：所有 episode 跑完后对 `config.scenario_ids` 每个 `backup_to(sid, verify_snapshot(env.id, sid))`；`instance_with(extra, scenario_id)` 从该情境的验证快照恢复一份临时世界、`open(scenario_id, agent_pid)`、`instantiate(..., replay_read_only=True)`。

- [x] **Step 1: 写失败的测试**（在 `tests/test_ma_deployment.py`；夹具 `mem` 改成按世界，`_run` 的 `on_start(eid, digest)`）

```python
def test_a_failed_attempt_rolls_back_both_slots_writes(tmp_path, mem): ...
    # 对手位经 register_episode 拿到的 round_id 写入 "r/deploy-...#0@pb";失败后 digest(world) 等于起点

def test_another_rounds_uncommitted_snapshot_defers_the_episode(tmp_path, mem):
    store, layout = mem
    p = layout.snapshot("agent-round-zengen-v1", "deploy-zengen-v1-s1-1")
    p.parent.mkdir(parents=True); p.write_text("{}"); layout.write_world_tag(p, "s1")
    with pytest.raises(DeferredError, match="round agent-round-zengen-v1"):
        _run(tmp_path, mem, round_id="agent-round-zengen-v2")

def test_a_committed_episode_is_reused_when_a_second_opponent_spoke_before_the_agent(tmp_path, mem):
    # 三位情境,智能体位第三个发言;首轮 digest_before != 起点摘要;第二次 _run 复用,不开会话

def test_the_driver_waits_for_server_ports_before_restoring(tmp_path, mem):
    # 一个 holder=pb 的端口在另一线程 0.3 秒后才关;尝试失败后 restore 成功,不抛

def test_a_server_port_that_never_closes_fails_the_restore_and_keeps_the_snapshot(tmp_path, mem):
    # close_wait_s=0.2,端口不关:RestoreFailedError,快照与 .world 都在

def test_pending_rows_of_deployment_opponents_are_abandoned_on_commit(tmp_path, mem):
    # 对手位留一条 pending(抽取失败注入):提交后 pending_events(world) 为空
```

`tests/test_ma_rounds.py`：`test_the_round_records_the_memory_runtime_and_each_episodes_start_digest` 改断言 `isinstance(d, str)`；`test_a_round_that_cannot_lock_a_persona_library_is_deferred` 改名 `..._a_world_library...`、锁 `s1`；`test_verification_replays_on_read_only_snapshot_copies` 改断言 `verify-s1` 存在过；其余回滚类用例照新签名改。

`tests/test_ma_verify.py` 加：

```python
def test_a_finding_across_two_scenarios_replays_each_episode_in_its_own_world():
    made = []
    def factory(extra, scenario_id):
        made.append((len(extra), scenario_id)); return _ReplayInstance(scenario_id)
    verify(DRAFT, [FINDING_S1_S2], EPISODES_S1_S2, base_instance_factory=factory, personas=P, scenarios=S)
    assert sorted(made) == [(0, "s1"), (0, "s2"), (1, "s1"), (1, "s2")]
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_ma_deployment.py tests/test_ma_rounds.py tests/test_ma_verify.py`
Expected: FAIL

- [x] **Step 3: 实现** 按上面的 Interfaces。`_restore` 签名改为 `(store, world_id, snap, expected, episode_id, close_wait_s)`。

- [x] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_ma_deployment.py tests/test_ma_rounds.py tests/test_ma_verify.py tests/test_ma_deployment_starling.py tests/test_ma_e2e.py tests/test_ma_memory_layout.py`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add src/mindagent/deployment.py src/mindagent/rounds.py src/mindagent/verify.py tests/test_ma_deployment.py tests/test_ma_rounds.py tests/test_ma_verify.py tests/test_ma_deployment_starling.py tests/test_ma_e2e.py
git commit -m "feat(mindagent): 部署轮次按情境世界:世界锁、整世界快照、等端口全关、按情境验证、新复用判据"
```

### Task 7: 部署对手位进部署世界

**Files:**
- Modify: `src/factory/agent.py`（`AgentRuntime.__init__`、`_backends`；新方法 `register_deployment_episode`、`unregister_deployment_episode`）
- Modify: `src/factory/memory_translate.py:61-79`（`memory_role_backend` 透传 `holder`）
- Modify: `src/factory/handlers.py:90-125`、`src/factory/wire.py:186-200`（把同一个管理面交给运行时；登记回调交给轮次处理器）
- Test: `tests/test_factory_handlers.py`

**Interfaces:**
- Consumes: Task 6 的 `register_episode` / `unregister_episode` 回调签名。
- Produces:
  - `AgentRuntime(..., deployment_store: DeploymentMemoryStore | None = None)`。
  - `AgentRuntime.register_deployment_episode(episode_id: str, round_id: str) -> None`、`unregister_deployment_episode(episode_id: str) -> None`，由 `_routes_lock` 守护，不取 `_lock`。
  - `memory_role_backend(base, *, memory, episode_id, interlocutors, holder)`。
  - `_backends` 对没登记给训练 run 的会话：`round_id = 登记表.get(info.episode_id)`；有且 `deployment_store` 非空 → 每个非客户端位 `deployment_store.open(scenario.id, binding.persona_id)`，实例 `episode_id=f"{round_id}/{info.episode_id}"`、`holder=binding.persona_id`；端口随 `SessionResources.close` 关。查不到 → 空 `TupleMemory()`，`logger.warning("deployment session %s has no registered round; opponents get an empty memory", info.episode_id)`。

- [x] **Step 1: 写失败的测试**（`tests/test_factory_handlers.py`）

```python
def test_a_registered_deployment_session_opens_opponents_in_the_scenario_world(tmp_path):
    store = FileTupleStore(tmp_path / "deploy")
    rt = AgentRuntime(_tree(tmp_path), ("嗯",), deployment_store=store)
    rt.register_deployment_episode("deploy-m-s1-1", "agent-round-m")
    res = rt._backends(_info(episode_id="deploy-m-s1-1", scenario="s1", client_slot="a"))
    res.backends["b"].respond(PERSONA, SCENARIO, [TurnRecord(0, "a", "你好")], "b")
    assert "agent-round-m/deploy-m-s1-1#0@pb" in store.pending_events("s1") + _committed_keys(store, "s1")
    res.close()
    assert store.wait_closed("s1", 1.0)

def test_an_unregistered_deployment_session_keeps_empty_opponents_and_warns(tmp_path, caplog): ...

def test_round_and_admit_handlers_and_the_runtime_share_one_management_plane(tmp_path): ...
    # 改写现有 test_round_and_admit_handlers_share_one_stand_in_deployment_memory,再断言 runtime 的 deployment_store 是同一个对象
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_factory_handlers.py`
Expected: FAIL

- [x] **Step 3: 实现**：`wire` 在启用 `start_agent_round` 时把 `memory.deployment.store` 传给 `AgentRuntime`；`handlers._agent` 传 `register_episode=runtime.register_deployment_episode`、`unregister_episode=runtime.unregister_deployment_episode`。

- [x] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_factory_handlers.py tests/test_factory_memory_translate.py tests/test_factory_memory_wiring.py`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add src/factory/agent.py src/factory/memory_translate.py src/factory/handlers.py src/factory/wire.py tests/test_factory_handlers.py
git commit -m "feat(factory): 部署对手位开部署世界的端口,经登记表拿轮次前缀;管理面三方共用"
```

### Task 8: 门② 增量带 scenario_id

**Files:**
- Modify: `src/mindagent/admit.py:51-203`
- Modify: `src/factory/handlers.py:121-125`（注入绑定对）
- Test: `tests/test_ma_admit.py`、`tests/test_ma_deployment_starling.py`

**Interfaces:**
- Produces:
  - `MEMORY_DEFINITION_KEYS = frozenset({"scenario_id", "persona_id", "text", "interlocutor"})`。
  - `make_admit_proposal_handler(*, memory_store, memory_layout, bound_pairs: Callable[[], frozenset[tuple[str, str]]])`：每次调用时读 `agent.yaml` 的 `(scenario_id, persona_id)` 组合（组合根用 `read_agent_settings` 的 `scenario_ids × bindings` 生成）。
  - 不在 `bound_pairs()` 里的对：T2，原因 `memory increment {key}: ({sid!r}, {pid!r}) is not a bound pair of the deployment config`。
  - 锁：`hold_world` 涉及的每个 `scenario_id`；`uncommitted_snapshots(sid)` 非空即延后；全局 `any_uncommitted_snapshots()` 等待保留。
  - 写入：`memory_store.remember_event(sid, pid, key, text, interlocutor=...)`；`abandon_event(sid, key)`。

- [x] **Step 1: 写失败的测试**：`MEMORY`、`SECOND` 两个定义加 `"scenario_id": "s1"`；新增

```python
def test_a_memory_increment_outside_the_bound_pairs_is_t2_and_touches_nothing(tmp_path): ...
def test_a_locked_world_defers_admission(tmp_path): ...   # 改写 test_a_locked_persona_library_defers_admission
def test_memory_definition_keys_are_the_four_required_keys():
    assert MEMORY_DEFINITION_KEYS == {"scenario_id", "persona_id", "text", "interlocutor"}
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_ma_admit.py`
Expected: FAIL

- [x] **Step 3: 实现**：`scenario_id` 过 `_check_segment` 同一份白名单。

- [x] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_ma_admit.py tests/test_ma_deployment_starling.py tests/test_factory_handlers.py`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add src/mindagent/admit.py src/factory/handlers.py tests/test_ma_admit.py tests/test_ma_deployment_starling.py
git commit -m "feat(mindagent): 门② 记忆增量带 scenario_id,校验绑定对,按世界取锁"
```

### Task 9: 训练 rollout 世界

**Files:**
- Modify: `src/factory/memory_runtime.py:144-394`（`StarlingLayout`、`TrainingCopies` → `TrainingWorlds`，加替身实现 `StandInTrainingWorlds`）
- Modify: `src/factory/interact_policy.py:353-421`、`src/factory/agent.py:252-338`、`src/factory/wire.py`
- Modify: `src/mindtrainer/trajectory_source.py:300-305`（`_attempt` 进入期的 `RoleMemoryUnavailableError`）、`src/mindtrainer/dispatch.py`（`PolicyForRollout` 的文档串）
- Modify: `src/actio/memory_starling.py`（`StarlingMemory.open` 加 keyword-only 参数 `on_close: Callable[[], None] | None = None`，关库成功后调一次）
- Test: `tests/test_factory_training_copies.py`（`git mv` 成 `tests/test_factory_training_worlds.py`）、`tests/test_factory_interact_wire.py`、`tests/test_mt_trajectory_interact.py`、`tests/test_mt_orchestrator.py`（这两个文件里的 `policy_for_rollout` 假实现改成收两个参数）

**Interfaces:**
- Produces（`TrainingWorlds` 协议，`StarlingTrainingWorlds` 与 `StandInTrainingWorlds` 两个实现）：
  - `bind(run_id, seed: str | None)`、`unbind(run_id)`、`seed_of(run_id)`（同今天）
  - `open_rollout(run_id, scenario_id, episode_id, holder) -> MemoryBackend`：同名世界在保留表里 → 抛 `MemoryFailureError(transient=True)`；否则先删后建（Starling：复制 `seeds/<seed>/<scenario>.db`，没有就建空库；替身：新 `TupleMemory()`），登记进 `episode_id → world` 表，开 `holder` 端口。
  - `join_rollout(episode_id, holder) -> MemoryBackend`：表里没有 → `MemoryFailureError(transient=False)`。
  - `close_rollout(episode_id, ports, *, wait_s: float = SESSION_CLOSE_WAIT_S) -> bool`：`SESSION_CLOSE_WAIT_S` 从 `mindagent.deployment` 导入（组合根可以导入 `mindagent`，反方向不行）：关 `ports`，`wait` 至该世界端口计数归零，删世界、摘表项、返回真；等不到 → 登记保留表、记 warning、返回假，不抛。
  - `release_session(episode_id, ports) -> None`：服务端关自己的端口（计数减一，唤醒等待者）。
  - `sweep() -> int`（Starling；替身返回 0）
  - `StarlingLayout.rollout_world(run_id, episode_id) -> Path`（`runs/<run_id>/<episode_id>.db`），`StarlingLayout.seed(seed_id, scenario_id)`。
- `PolicyForRollout` 的签名改成 `Callable[[str, str], AbstractContextManager[ModelBackend]]`，两个参数依次是 `(episode_id, scenario_id)`。`trajectory_source._attempt` 传 `scenario.id`。
- `interact_policy.policy_for_rollout(episode_id, scenario_id)` 调 `open_rollout(run_id, scenario_id, episode_id, trained_persona)`。遇到 `MemoryFailureError`，`transient` 为真时译成 `FactoryMemoryUnavailableError`，为假时照今天译成 `FactoryMemoryFailureError`。
- `AgentRuntime._backends` 训练会话：`join_rollout(info.episode_id, binding.persona_id)`；失败按今天"造后端失败"收尾。
- `trajectory_source._attempt`：`with self._policy_for_rollout(episode_id, scenario.id) as policy:` 外层捕获进入期的 `RoleMemoryUnavailableError` → `_AttemptFailed("role_memory_unavailable", type(exc).__name__, True)`。

- [x] **Step 1: 写失败的测试**

```python
# tests/test_factory_training_worlds.py,参数化到两个实现
def test_the_trained_and_opponent_slots_of_one_rollout_share_a_world_but_not_their_recall(worlds): ...
def test_two_rollouts_of_a_group_start_from_the_same_seed_and_do_not_see_each_other(worlds): ...
def test_a_server_that_cannot_find_the_rollout_world_is_a_permanent_memory_failure(worlds):
    with pytest.raises(MemoryFailureError) as exc:
        worlds.join_rollout("run-1-grpo0-s1-r0", "pb")
    assert exc.value.transient is False

def test_the_client_waits_for_a_server_port_that_closes_late(worlds):
    a = worlds.open_rollout("run-1", "s1", "run-1-grpo0-s1-r0", "pa")
    b = worlds.join_rollout("run-1-grpo0-s1-r0", "pb")
    threading.Timer(0.5, lambda: worlds.release_session("run-1-grpo0-s1-r0", [b])).start()
    assert worlds.close_rollout("run-1-grpo0-s1-r0", [a], wait_s=3.0) is True

def test_a_retry_while_the_world_is_still_held_is_transient(worlds):
    a = worlds.open_rollout("run-1", "s1", "run-1-grpo0-s1-r0", "pa")
    worlds.join_rollout("run-1-grpo0-s1-r0", "pb")          # 不关
    assert worlds.close_rollout("run-1-grpo0-s1-r0", [a], wait_s=0.1) is False
    with pytest.raises(MemoryFailureError) as exc:
        worlds.open_rollout("run-1", "s1", "run-1-grpo0-s1-r0", "pa")
    assert exc.value.transient is True
```

`tests/test_factory_memory_runtime.py`：`test_layout_paths_follow_section_5_3` 改成新路径。轨迹源测试加：

```python
def test_a_transient_memory_failure_while_entering_the_policy_context_is_retried():
    # policy_for_rollout 第一次进入抛 FactoryMemoryUnavailableError,第二次正常:episode 返回,failed_attempts 有一条 role_memory_unavailable
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_factory_training_worlds.py tests/test_factory_memory_runtime.py tests/test_factory_interact_wire.py`
Expected: FAIL

- [x] **Step 3: 实现**：两个实现共享 `episode_id → (world, open_count, Condition)` 的表逻辑，放在 `memory_runtime.py` 的一个私有基类里；Starling 的端口用 `StarlingMemory.open(path, holder=..., on_close=...)`，`on_close` 减计数。

- [x] **Step 4: 跑测试确认通过**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_factory_training_worlds.py tests/test_factory_memory_runtime.py tests/test_factory_interact_wire.py tests/test_factory_interact_e2e.py tests/test_mt_dispatch.py tests/test_mt_trajectory_interact.py tests/test_mt_orchestrator.py`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add src/factory/memory_runtime.py src/factory/interact_policy.py src/factory/agent.py src/factory/wire.py src/mindtrainer/trajectory_source.py src/mindtrainer/dispatch.py tests/test_factory_training_worlds.py tests/test_factory_memory_runtime.py tests/test_factory_interact_wire.py tests/test_mt_trajectory_interact.py tests/test_mt_orchestrator.py
git commit -m "feat(factory): 训练一条 rollout 一个世界,客户端建删、服务端加入;进入期瞬态记忆失败按条重跑"
```

### Task 10: 起点摘要按情境，旧布局拒绝启动

**Files:**
- Modify: `src/factory/memory_runtime.py`（两个 resolver）、`src/mindtrainer/dispatch.py:269-295`、`src/mindtrainer/training_config.py:257-279,426-440`
- Modify: `src/factory/memory_runtime.py::memory_wiring`（旧布局检查）
- Modify: `tests/memory_fixtures.py`
- Test: `tests/test_mt_config.py`、`tests/test_mt_dispatch.py`、`tests/test_factory_memory_runtime.py`、`tests/test_factory_memory_wiring.py`

**Interfaces:**
- Produces:
  - `MemoryResolver = Callable[[str | None, tuple[str, ...]], tuple[dict[str, str], dict]]`，第二个参数是 `scenario_ids`；`_resolve_memory` 传 `tuple(dict.fromkeys(cfg.scenario_ids))`。
  - `_validate_memory_against_bindings` 改名 `_validate_memory_against_scenarios`：`set(digests) == set(cfg.scenario_ids)`，原因 `memory.digests must name exactly the configured scenarios`。
  - `starling_memory_resolver` 读 `layout.seed(seed, scenario_id)`；`stand_in_memory_resolver` 给每个情境空世界摘要。
  - `memory_wiring` 在 `claim_memory_root` 之后、导入 `actio.memory_starling` 之前调 `MemoryLayout(config.root, suffix).legacy_entries(known_scenarios=agent_scenarios)`；替身在 `stand_in_deployment_memory` 里做同样检查。非空 → `FactoryConfigError(f"memory root has the old per-persona layout ({entries[0]} and {len(entries) - 1} more); clear deploy/ and snapshots/ under the memory root")`。`agent_scenarios` 来自 `agent.yaml`（不在场时为空集，此时 `deploy/` 下任何库都算旧布局）。
  - `tests/memory_fixtures.py`：`resolved_memory(scenario_ids)`、`fake_memory_resolver` 返回 `{s: f"digest-{s}"}`。

- [x] **Step 1: 写失败的测试**

```python
# tests/test_mt_config.py
def test_a_resolved_memory_section_must_name_exactly_the_configured_scenarios(): ...
# tests/test_mt_dispatch.py
def test_the_memory_is_resolved_per_scenario_before_the_policy_context_is_entered(): ...
# tests/test_factory_memory_wiring.py
def test_an_old_per_persona_memory_root_exits_2_naming_the_directories_to_clear(tmp_path):
    root = tmp_path / "mem"; (root / "deploy").mkdir(parents=True); (root / "deploy" / "pa.db").write_bytes(b"")
    with pytest.raises(FactoryConfigError, match=r"old per-persona layout \(deploy/pa.db"):
        memory_wiring(repo, {MEMORY_CONFIG_ENV: str(_config(root))}, deployment=True)
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run --offline pytest -q -p no:cacheprovider tests/test_mt_config.py tests/test_mt_dispatch.py tests/test_factory_memory_runtime.py tests/test_factory_memory_wiring.py`
Expected: FAIL

- [x] **Step 3: 实现** 按 Interfaces。`config_hash` 不用改代码：键变了，哈希自然变。

- [x] **Step 4: 跑测试确认通过**

Run: 同 Step 2，再加 `tests/test_factory_interact_wire.py tests/test_factory_interact_e2e.py tests/test_mt_dispatch_synthesis.py tests/test_ts_resolve.py`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add src/factory/memory_runtime.py src/mindtrainer/dispatch.py src/mindtrainer/training_config.py tests/
git commit -m "feat: 起点摘要与种子按情境,memory.digests 键改为情境;记忆根是旧布局时拒绝启动"
```

### Task 11: 文档状态与全量验证

**Files:**
- Modify: `docs/design/00-overview.md`、`20-socialsimulator.md`、`30-mindtrainer.md`、`40-actio.md`、`50-mindagent.md`、`90-factory-and-cli.md`、`docs/design/_shared/context-block.md`
- Modify: 本计划（勾选）

- [x] **Step 1: 跑全量**

Run: `uv run --offline pytest -q -m 'not slow' -p no:cacheprovider`
Expected: 0 failed

- [x] **Step 2: 改文档状态**：上面十个任务落地的每一行，[计划中] 改 [已落地]，"今天……[已落地];改为……[计划中]"的成对写法改成只写现在的行为。被取代的旧决策（SYS-27、SS-22、MT-22、MT-23、AC-14、AC-18、AC-24、MA-16、MA-22、MA-30、FC-27 等）的"将由 X 取代 [计划中]"改成"已由 X 取代"。共享区块同样改。第 9 章测试表填实测的文件与用例数（`grep -c "def test_"`）。

- [x] **Step 3: 跑文档检查**

Run: `python3 docs/design/tools/sync_docs.py && python3 docs/design/tools/check_docs.py && python3 docs/design/tools/check_consistency.py`
Expected: 三者都是 0 个错误；`TrainingWorlds`、`wait_closed` 的符号告警消失

- [x] **Step 4: 提交**

```bash
git add docs/design docs/superpowers/plans/2026-11-04-scenario-world-memory.md
git commit -m "docs(design): 记忆按情境世界组织标为已落地"
```
