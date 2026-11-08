# 设计四：rise 接 Starling 记忆服务

状态：设计稿，待审。日期：2026-11-08。基线：rise `1c2e364`，Starling `cff319d`（记忆服务第 1 至 4 期已合并）。
权威文档集是 `docs/design/`。本稿审定后并入 00、20、30、40、50、90 六篇，另写施工计划；并入后本稿只作历史来源。

引用写法：

| 写法 | 指的是 |
|---|---|
| `路径:行` | rise 仓库里的文件 |
| `S:路径:行` | Starling 仓库（`cff319d`）里的文件 |
| 答复 `:行` | `S:docs/superpowers/specs/2026-11-06-memory-service-reply-to-rise.md` |
| 服务设计 `:行` | `S:docs/superpowers/specs/2026-11-03-memory-service-design.md` |
| 需求 `§n` | `docs/superpowers/specs/2026-11-02-starling-service-requirements.md` |
| 设计三 | `docs/superpowers/specs/2026-11-04-scenario-world-memory-design.md` |

## 0. 这份文档做什么

把 rise 的 Starling 后端从进程内的库换成 Starling 记忆服务。设计三定下的世界协议不变，只换后端。

- **今天**：`simple_actio/memory_starling.py` 是全仓库唯一 `import starling` 的文件（`memory_starling.py:27`）。每个世界一个 `.db`，由 rise 自己复制、备份、换文件（`memory_starling.py:498-557`）。
- **改后**：rise 用纯 Python 客户端 `starling_client` 连 `starling-serve`。世界、快照、分叉都按名字和 id 交给服务。rise 不再按路径打开任何世界文件（答复 `:55`）。
- **不变**：`MemoryBackend` 五个方法、替身后端、部署驱动器与门② 的流程、世界锁、`wait_closed` 的顺序约束。

本稿用到的名字：

| 名字 | 含义 |
|---|---|
| 世界 id | rise 内部的世界标识。部署世界是 `scenario_id`，与今天相同 |
| 世界名 | Starling 注册表里的名字，由世界 id 加前缀拼成（5.1） |
| 前缀 | 本机记忆配置里的 `world_prefix`，区分共用一个服务的几个仓库 |
| 服务会话 | 一个 rise 进程里唯一的 `starling_client.Client`（6.3） |
| rise 设置 | rise 传给 `create_world`、`fork`、`open` 的那份完整世界设置（7.1） |
| 在途记录 | 记忆根下的小 JSON 文件，记一份未提交快照属于哪个世界、快照 id 是什么（4.2） |

## 1. 已定事项

| 事项 | 决定 | 来源 |
|---|---|---|
| 接法 | rise 用 Starling 的纯 Python 客户端 `starling_client` 连 `starling-serve`；不再进程内 `import starling` | 用户 2026-11-02 |
| 服务从哪来 | 配置给了服务地址就连；没给就由 rise 按钉住的版本在本机以子进程拉起，等就绪，rise 退出时 SIGTERM | 用户 2026-11-02 |
| 世界与主体 | 一个模拟世界 = 一个 Starling 世界，人格是 holder。一个情境一个部署世界；每条 rollout 从情境的种子世界分叉一个平行世界 | 用户 2026-11-03（设计三已落地，只换后端） |
| 看板 | 用户要在 Starling 看板里查看所有世界，含训练中的 rollout 世界；rollout 世界结束后留着复盘 | 用户 2026-11-02 |
| 时机 | 第 4 期合并后对接，跳过单独接第 2 期 | 用户 2026-11-06 |
| 旧数据 | 不迁移旧 `.db`；新系统从空开始（本机旧验证树与记忆根已于 11-06 删除） | 需求 §9 |
| rollout 世界的清理 | rise 分叉时传 `reclaimable=True` 并打 run、scenario、episode、seed 标签；第 6 期自动清理会收走它们，过渡期接受堆积；不设计按标签清世界的命令或兜底清理 | 用户 2026-11-09 |
| 旧 run 判 T3 | 第 12 节只说明哪些 run 判 T3、为什么，不写新起 run 的操作步骤 | 用户 2026-11-09 |

## 2. 目标与非目标

目标：

1. rise 的全部记忆都在 Starling 服务的注册表里，看板能按标签找到。
2. 两个后端（替身与 Starling 服务）语义不分叉，过同一套契约测试。
3. 起点摘要、快照恢复后的摘要都取服务给的值，不再拷文件算。
4. 记忆运行时身份改用服务报出的版本、提交与抽取配置；客户端与服务提交不一致时拒绝启动。
5. 没装 extra `memory`、没设 `RISE_MEMORY_CONFIG` 时，替身路径一行不受影响。

非目标：

- 第 5 期（看板前端）、第 6 期（自动清理）的内容。rise 只在它们合并前做过渡。
- 换机器。服务只听回环地址（服务设计 `:27`）。
- 改记忆语义。抽取、嵌入、召回、摘要都照旧；为此 rise 显式关掉来源 lane（7.1）。
- 共享感知、跨情境共享记忆（设计三非目标，照旧）。

## 3. 端口与适配器

### 3.1 `StarlingMemory` 包 `RemoteMemory`

`StarlingMemory`（`memory_starling.py:149-243`）的内部对象从 `starling.Memory` 换成 `RemoteMemory`。方法逐一对应：

| `MemoryBackend` 方法 | 今天调用 | 改后调用 | 差别 |
|---|---|---|---|
| `recall` | `memory.recall(q, k=limit, mode="semantic", strict=True, include_volatile=True)`（`memory_starling.py:189`） | 同一行，参数照旧显式传 | 客户端缺省也是 `strict=True, include_volatile=True`（`S:clients/python/starling_client/remote_memory.py:73-74`）。照旧显式传，理由同 `EPOCH` 的注释（`memory_starling.py:40-41`）：缺省哪天改了，行为不能静默漂移 |
| `remember` | `remember_event(key, text, interlocutor=, epoch=EPOCH)` | 同 | 成功判据改严，见 3.2 |
| `embed_pending` | `next_seq()` 加 `embed_pending(now)` | 同 | 无 |
| `digest` | `memory.digest()` | 同 | 无 |
| `close` | `memory.close(timeout=60)` | 同 | 三种情形当作已关，见 3.3 |

`recall` 的命中是 dict，`row` 是只读的 `StatementRow`，字段与进程内相同（`S:clients/python/starling_client/types.py:38-62`）。`_render` 用到的五个字段都在里面，所以渲染不改。

### 3.2 写入成功的判据

今天 `_outcome` 只看 `extraction_failed`（`memory_starling.py:110-119`）。答复要求同时看 `outcome`（答复 `:108`）。C++ 还会给 `no_store`、`rejected`、`source_missing`（`S:src/memory/memory_ops.cpp:54`、`:96-102`）。

| 结果 | rise 的处理 |
|---|---|
| `extraction_failed` 为真 | 照旧：`RememberOutcome((), True, category)` |
| `outcome` 是 `accepted` 或 `idempotent` | 照旧：换内容 id，`missing` 判永久失败 |
| 其他 `outcome` | 永久 `MemoryFailureError`，消息写 `remember: outcome <值>` |

客户端超时后重放同一个 key 时，两次调用都会拿到先提交那次的语句 id（答复 `:110-111`）。rise 按 key 记语句，不受影响。

### 3.3 关闭

`RemoteMemory.close(timeout)` 先等本地在途调用、再请服务端等（`S:clients/python/starling_client/remote_memory.py:116-160`）。超时抛 `TimeoutError`、句柄保持打开，与今天一致。另加两条：

| 关闭时收到 | 处理 | 理由 |
|---|---|---|
| `SessionExpired` | 当作已关：置位、调 `on_close` | 会话过期时服务端已释放它的全部句柄（服务设计 `:261`）。不当作已关的话，端口计数永不归零，`close_rollout` 与 `wait_closed` 都要等到期限。抛它之前本地在途数已归零（`remote_memory.py:149-152`），没有本地调用还在跑 |
| `MemoryClosed`（客户端已关） | 当作已关 | 只在 rise 退出时出现，服务会话已断开 |
| 端口所属的服务会话已被 rise 换掉（6.4 的重连或重启之后） | 不发请求，直接当作已关 | 旧会话在服务端已经失效；不这样做，关闭会因连不上旧服务而失败，计数不归零，恢复等满 70 秒后判 `RestoreFailedError` |

### 3.4 部署管理面的事件接口改按主体

Starling 第 2 期起，`abandon_event` 只作用于句柄自己那个 holder 名下的事件（答复 `:9`）；同一个 key 在两个 holder 名下是两条事件（答复 `:7`）。今天 rise 以 `agent="world"` 的短连接放弃（`memory_starling.py:477-482`、`:50`），接上服务后会抛 `event_not_found`。

```python
@dataclass(frozen=True)
class PendingKey:
    key: str
    holder: str

def abandon_event(self, world_id: str, holder: str, key: str) -> None: ...
def pending_events(self, world_id: str) -> list[PendingKey]: ...
```

| 调用处 | 传的 holder |
|---|---|
| `src/mindagent/admit.py:205`、`:210` | 提案增量里的 `persona_id`（`pid`），手里本来就有 |
| `src/mindagent/deployment.py:457` | 这一项 `PendingKey.holder` |

`holder` 为空串只出现在从 0038 升级上来的库里（答复 `:26`）。rise 的世界都从 `create_world` 或分叉新建，不会有这种行。所以 rise 见到空 holder 就抛永久失败，消息写出 key。不照答复 `:20` 去解析 key 里的 `@holder`：那条退路只为旧库而设，rise 走不到，留着反而掩盖真问题。

### 3.5 替身跟着改

替身的事件表今天按 key 去重（`src/simple_actio/memory.py:256-276`），放弃不看主体（`memory.py:309-314`、`:554-558`）。改成与 Starling 同一语义：

| 项 | 改后 |
|---|---|
| 事件表的键 | `(holder, key)`，同 key 在两个主体名下是两行 |
| `abandon_event(world, holder, key)` | 只找 `(holder, key)`；找不到抛永久 `MemoryFailureError`，与 Starling 的 `event_not_found` 同类 |
| `pending_events` | 返回 `PendingKey`，按 `seq` 排序（今天的顺序） |
| 摘要 | 行的序列化不变（行里本来就带 `holder`，`memory.py:271-273`），所以替身摘要不变 |

契约测试新增：同 key 两个主体各写一次两条都在；用错主体放弃判永久失败；`pending_events` 带主体。两个后端各跑一遍（`tests/test_simple_actio_deployment_store_contract.py:31-44` 的参数化夹具）。

## 4. 部署管理面

### 4.1 快照与恢复改按快照 id

今天 `backup_to` 是打开库再 `VACUUM INTO`，`restore_from` 是"先删日志后换文件"（`memory_starling.py:498-557`）。改成服务的 held 快照：

```python
@dataclass(frozen=True)
class SnapshotRef:
    snapshot_id: str
    digest: str

def snapshot(self, world_id: str) -> SnapshotRef: ...
def restore(self, world_id: str, snapshot_id: str) -> str: ...
def delete_snapshot(self, world_id: str, snapshot_id: str) -> None: ...
```

| 方法 | Starling 实现 | 依据 |
|---|---|---|
| `snapshot` | 世界不存在先建（4.4），再 `client.snapshot(world)` | 返回 `{snapshot_id, digest}`，kind 是 `held`（`S:python/starling/dashboard/routes/client.py:355-357`） |
| `restore` | `client.restore(world, snapshot_id)` | 返回恢复后的摘要；与快照记录的不等时报 `restore_digest_mismatch`（答复 `:53`） |
| `delete_snapshot` | `client.delete_snapshot` | held 快照不参与修剪，由调用方显式删（服务设计 `:171`） |

崩溃安全交给 Starling：恢复是复制、校验、硬链接归档、一步改名（服务设计 `:175`）。rise 删掉 `copy_library`、`library_digest`、`_require_library_file` 与"先删日志后换文件"那一套。

替身同样实现这三个方法：快照内容存进替身根下 `held/<world_id>/<snapshot_id>.json`，恢复按 id 读回。

### 4.2 在途记录取代快照文件与 `.world` 旁注

客户端没有列快照的接口（`S:clients/python/starling_client/client.py:147-197`），所以快照 id 由 rise 自己记。记在记忆根下，沿用今天的路径形状（`src/mindagent/memory_layout.py:70-79`）：

| 路径（记忆根下） | 内容 | 生命周期 |
|---|---|---|
| `snapshots/<round_id>/deploy-<episode_id>.json` | 起点快照的在途记录：`world_id`、`snapshot_id`、`digest` | 删除就是这条 episode 的提交 |
| `snapshots/<round_id>/verify-<scenario_id>.json` | 验证快照的在途记录 | 验证收尾时删 |

记录用"临时文件加改名"写，内容自带世界 id，所以 `.world` 旁注不再需要。`MemoryLayout` 的 `round_snapshots`、`uncommitted_snapshots`、`any_uncommitted_snapshots`、`world_of` 保持签名，改为读记录。两个后端的记录一律是 `.json`；`suffix` 只留给旧布局检查。读不出的记录按今天坏旁注的口径处理：装配期拒绝启动，门② 延后。

各步顺序与崩溃点：

| 动作 | 顺序 | 崩在中间的后果 |
|---|---|---|
| 开 episode | `snapshot` → 写记录 → `on_episode_start(eid, digest)` | 快照之后、记录之前：留一份孤儿 held 快照，重入时重拍。只占磁盘 |
| 提交 | 删记录 → `delete_snapshot` | 删记录之后：孤儿快照，同上。反过来先删快照再崩，重入会拿一个不存在的 id 去恢复已提交的 episode。`delete_snapshot` 失败只记 warning（只报码），不影响提交 |
| 回滚 | `restore` → 删记录 → `delete_snapshot` | 恢复之后：重入再恢复一次同一份快照，结果相同 |

### 4.3 `wait_closed` 与 `world_held`、`world_busy`

服务端恢复的前提是这个世界没有活句柄、没有在途调用，否则返回 `world_held` 或 `world_busy`（服务设计 `:177`；`S:python/starling/dashboard/world_manager.py:1605-1610`）。rise 的 `wait_closed` 保留，两者分工不同：

| 机制 | 管什么 | 为什么都要 |
|---|---|---|
| `wait_closed`（rise 进程内计数） | 本进程开的端口，含服务端线程上的对手位端口 | 对手位端口在另一条线程上收尾，最长 60 秒（设计三 4.5）。不等就去恢复，服务端一定回 `world_held` |
| 服务端检查 | 所有会话的句柄与在途调用 | 兜底，挡住本进程看不见的写者 |

`wait_closed` 返回真之后，恢复收到的码按下表处理（`src/mindagent/deployment.py:166-181` 的 `_restore` 与 `src/mindagent/rounds.py:337-372` 的回滚都照此改）：

| 码 | 含义 | 处理 |
|---|---|---|
| `world_busy` | 在途调用或看板的检视租约 30 秒内没排干（`S:python/starling/dashboard/world_manager.py:160`） | 瞬态：`DeferredError`，轮次留 pending，重入时先恢复 |
| `world_held` | 本进程端口已全关，还有别的会话的活句柄 | 确定性：`RestoreFailedError`，快照与记录都保留等人 |
| `snapshot_not_found`、`restore_digest_mismatch` 及其他 | 快照没了或被改过 | 确定性：`RestoreFailedError` |

### 4.4 部署世界的建立与读

今天打开不存在的库就建库（`memory_starling.py:374-375`）。改后：

| 操作 | 世界不存在时 |
|---|---|
| `open`、`remember_event`、`snapshot` | 先 `create_world(世界名, labels=..., settings=rise 设置)`；收到 `world_exists`（并发建）就当作已建 |
| `digest` | 返回空种子的摘要（5.4），不建世界 |
| `pending_events` | 返回 `[]` |
| `abandon_event` | 永久失败（没有这个 key） |

`remember_event`（门② 用）在 holder 的句柄上写、嵌入、关闭，与今天的短连接同口径（`memory_starling.py:459-475`）。`abandon_event` 同样开 holder 的句柄。`pending_events`、`digest` 用不开句柄的管理调用（`S:clients/python/starling_client/client.py:181-186`）。

### 4.5 验证重放：从快照分叉可回收世界

今天：每个情境拍验证快照，再 `restore_from` 进一个临时目录里的 store（`src/mindagent/rounds.py:466-469`、`:483`）。改后不再造临时 store：

```python
def fork_replay(self, world_id: str, snapshot_id: str, replay_id: str) -> str: ...
def release_replay(self, replay_world_id: str) -> None: ...
def open(self, world_id: str, holder: str, *, read_only: bool = False) -> MemoryBackend: ...
```

| 步 | 改后 |
|---|---|
| 验证快照 | 每个情境 `snapshot(scenario_id)`，写 `verify-<scenario_id>.json`。记录已在（上次崩溃留下）就先 `delete_snapshot` 旧的 |
| 实例工厂 | `fork_replay(scenario_id, snapshot_id, replay_id)` 得到重放世界，`open(..., read_only=True)` 开智能体位主体的只读端口 |
| 分叉参数 | `reclaimable=True`，`retention_group=round_id`，标签见 5.1。可回收世界从不跑后台任务（答复 `:116`） |
| 清理 | finally 里关实例、`release_replay`、删验证快照与记录 |

`release_replay` 在 Starling 上是 `close_world`：客户端没有删世界的接口（`S:clients/python/starling_client/client.py:147-197`；管理操作表 `S:python/starling/dashboard/routes/client.py:411-422` 里也没有）。重放世界和 rollout 世界一样留给第 6 期。替身上它删掉世界文件。`DeploymentMemory.scratch_store_factory`（`src/factory/memory_runtime.py:110`）随之去掉。

只读句柄够用：重放实例 `replay_read_only=True`，只调 `recall`、`digest` 与 `close`（`src/simple_actio/instance.py:166`、`:172`、`:178-179`、`:238`），两者在服务端都不算写（`S:python/starling/dashboard/routes/client.py:200-209`）。替身的只读端口在写入时抛永久失败，与 Starling 的 `world_read_only` 同类，契约测试钉住。

## 5. 训练世界、种子与起点摘要

### 5.1 世界名与标签

Starling 的世界名规则：首字符字母或数字，其余 `[A-Za-z0-9._-]`，1 到 128 个字符，区分大小写（`S:src/governance/world_store.cpp:47-59`）。rise 的世界名一律以前缀开头，前缀与种类之间用 `.` 分隔：

| 世界 | 世界名 | 标签（都带 `repo=<前缀>`） | 可回收 |
|---|---|---|---|
| 部署世界 | `<前缀>.deploy.<scenario_id>` | `kind=deploy`、`scenario_id` | 否 |
| 空种子 | `<前缀>.seed-empty` | `kind=seed`、`seed_id=none` | 否（种子） |
| 种子 | 人定，建议 `<前缀>.seed.<seed_id>.<scenario_id>.v<n>` | `kind=seed`、`seed_id`、`scenario_id` | 否（种子） |
| rollout 世界 | `<前缀>.ro.<episode_id>.<8 位十六进制>` | `kind=rollout`、`run_id`、`scenario_id`、`episode_id`、`seed_id` | 是，`retention_group=run_id` |
| 重放世界 | `<前缀>.verify.<round_id>.<scenario_id>.<8 位十六进制>` | `kind=verify`、`round_id`、`scenario_id` | 是，`retention_group=round_id` |

- 前缀的格式是 `[A-Za-z0-9][A-Za-z0-9_-]{0,15}`，不含 `.`，所以世界名能无歧义地拆开。缺省 `rise`。
- 逻辑 episode id 是 `{run_id}-grpo{step}-{scenario_id}-r{k}`（`src/mindtrainer/trajectory_source.py:31-34`），例如 `run-1-grpo3-scen-auto-1-r1`。`run_id` 形如 `run-<数字>`（`src/controlplane/reconcile.py:115`），生成的情境 id 形如 `scen-auto-<seed>`（`src/simulator/backends.py:97`）。三者都只用 `[A-Za-z0-9._-]`，合规。
- 拼出的名字超过 128 个字符时，把中间那段换成它的 sha256 前 24 位，标签里照旧是完整的 id。rollout 与重放世界名带随机后缀：世界删不掉，同一个 episode 的重试必须换名（5.2）。
- 标签建好不能改（服务设计 `:99`）；空键被拒，空值可以（`S:src/governance/registry_db.cpp:342-349`）。

### 5.2 `StarlingTrainingWorlds`

表逻辑 `_RolloutWorlds`（`src/factory/memory_runtime.py:351-542`）不变：客户端建、服务端按 episode id 加入、客户端等服务端端口关完再收尾。三个子类钩子改实现：

| 钩子 | 今天 | 改后 |
|---|---|---|
| `_create` | 删旧文件，从 `seeds/<seed>/<scenario>.db` 复制（`memory_runtime.py:618-639`） | `client.fork(种子世界名, 新世界名, reclaimable=True, retention_group=run_id, labels=..., settings=rise 设置)` |
| `_port` | `StarlingMemory.open(path, holder=...)` | `client.open(世界名, holder=..., settings=rise 设置)` 包成 `StarlingMemory` |
| `_discard` | 删库文件 | `close_world`，失败只记 warning。只关不删 |
| `sweep` | 删 `runs/` 下全部子项（`memory_runtime.py:650-673`） | 去掉：没有本地文件要清；`MemoryWiring.sweep` 为 `None` |

- **起点核对**：`bind(run_id, seed, digests)` 多收一个参数，是派发器解出的 `memory.digests`。调用处 `src/factory/interact_policy.py:393` 改传 `cfg.memory["digests"]`（策略工厂拿到的已是解析后的配置，`src/mindtrainer/dispatch.py:401-412`）。`_create` 比较 `fork` 返回的 `digest` 与 `digests[scenario_id]`，不等就抛永久失败：种子在 run 中途被换掉了。
- **重试**：今天同名重试"先删后建"（`memory_runtime.py:430-472`）。世界删不掉，所以每次 `_create` 换一个随机后缀。保留表与"端口没关上就判瞬态"的规则不变。
- **settings 传给 fork**：分叉的设置是部分设置，叠在来源的设置上（`S:python/starling/dashboard/world_manager.py:1335-1336`）。传完整的 rise 设置，rollout 世界的行为就由 rise 的配置决定，与种子是谁建的无关。
- **服务端加入**：`join_rollout` 照旧查进程内的表拿到世界名，再在同一个服务会话里开对手位句柄（`src/factory/agent.py:361`）。

### 5.3 种子

种子是 `seed=true` 的世界。`seed` 蕴含只读，不跑后台，不参与清理，打开时不迁移（答复 `:47`）。

| 项 | 决定 |
|---|---|
| 谁建 | rise 不建有内容的种子。人用 Starling 的分叉路由从部署世界或它的快照分叉，`seed=true`，打上 5.1 的标签（看板路由 `S:python/starling/dashboard/routes/worlds.py:405`，分叉体带 `labels`、`seed`，`:67-76`）。rise 只自动建空种子（5.4） |
| 怎么找 | `list_worlds(labels={repo, kind=seed, seed_id, scenario_id}, with_digest=True)`，只取 `seed` 为真的项 |
| 升级后新旧同标签 | 新种子照抄旧种子的标签（答复 `:47`），取 `created_at` 最大的一份，相同时取名字最大的。`created_at` 精确到秒，同一秒建的两份相同（`S:python/starling/dashboard/world_manager.py:207-210`） |
| 旧布局 `seeds/<seed_id>/<scenario_id>.db` | 不再支持。记忆根下有 `seeds/` 目录就在装配期拒绝启动（退出码 2），与设计三 4.8 的旧布局同一口径。今天还没有任何种子（需求 §4.4） |
| 种子落后于当前 schema | 分叉返回 `world_schema_behind`（答复 `:45`），rise 判永久失败，派发器 T2。原因里写"需要人显式升级种子" |

### 5.4 没有种子时

`seed: null` 或某个情境没有种子时，rollout 世界从空种子 `<前缀>.seed-empty` 分叉。

- 不用 `create_world` 建 rollout 世界：`create_world` 没有 `reclaimable` 参数（`S:clients/python/starling_client/client.py:152-155`），建出来的世界永远不会被第 6 期收走。
- 空种子由 rise 在装配期建：不存在就 `create_world(name, seed=True, labels=..., settings=rise 设置)`。
- 同组相同：同组的每条 rollout 都从同一个空种子分叉，分叉保证摘要等于来源（答复 `:45`）。

### 5.5 起点摘要

解析器 `starling_memory_resolver`（`memory_runtime.py:242-294`）的签名不变，实现改为：

| 情形 | 今天 | 改后 |
|---|---|---|
| `seed` 为 null | 每个情境 `empty_digest()` | 每个情境取空种子的摘要 |
| 情境有种子 | 拷文件到临时目录再算（`library_digest`） | `list_worlds(with_digest=True)` 里该种子的 `digest` |
| 情境没有种子 | 空库摘要 | 空种子的摘要 |
| 这个 `seed_id` 一份种子都没有 | 目录不在时 `DirectiveFailedError` | 同，原因写种子 id |
| 服务瞬态失败 | 不会发生（只读本地文件） | 抛 `DeferredError`，指令留 pending |

派发器今天把解析器抛的一切异常译成 `DirectiveFailedError`（`src/mindtrainer/dispatch.py:287-290`），瞬态的服务失败也会变成 T2。所以 `_resolve_memory` 加一条：`DeferredError` 原样上抛。

## 6. 服务进程与会话

### 6.1 两种来源

给了 `url`：读 `token_file` 再 `connect`，服务的起停 rise 不管。没给 `url`：按 6.2 拉起子进程、等就绪、`connect`，起、停、重启都由 rise 管。

### 6.2 rise 拉起的服务

| 项 | 决定 | 依据 |
|---|---|---|
| 命令 | `[sys.executable, "-m", "starling.service", "--data-dir", D, "--host", "127.0.0.1", "--port", "0", "--token-file", F]` | `python -m starling.service` 等同于 `starling-serve`（`S:python/starling/service/__main__.py:1-5`）；用 rise 自己的解释器，服务端与客户端必然来自同一个 venv 的同一次安装 |
| 数据目录 D | `<记忆根>/service` | 记忆根已由 `.owner` 归属一个仓库（`src/factory/memory_config.py:227-264`）。所有文件都在 D 下，服务不读 `~/.starling`（`S:python/starling/dashboard/config.py:158-181`） |
| 端口 | 0，由系统分配；从 stdout 的 `starling-serve listening on http://127.0.0.1:<端口>` 读回 | `S:python/starling/service/serve.py:186`；Starling 测试夹具同一写法（`S:tests/python/fakes/service_proc.py:22`） |
| token | 每次启动 `secrets.token_urlsafe(32)`，写进 `<记忆根>/service.token`（`O_EXCL`，0600），看到 ready 后删掉 | 服务只在启动时读一次 token 文件（`S:python/starling/service/serve.py:197-200`、`:219-227`）。不进命令行、日志与异常 |
| 子进程环境 | 父进程环境去掉 `STARLING_*`；`OPENAI_API_KEY` 照传 | 显式端点的密钥取服务进程的 `OPENAI_API_KEY`（答复 `:118`） |
| 就绪 | 等 stdout 出现 `starling-serve ready`，上限 60 秒 | 就绪之前一切接口回 503（`S:python/starling/service/serve.py:12-15`）；60 秒与 Starling 夹具相同（`S:tests/python/fakes/service_proc.py:24`） |
| stdout、stderr | stdout 由一条线程读完；stderr 追加到 `<记忆根>/service.log` | 访问日志多，管道会写满（`S:tests/python/fakes/service_proc.py:4-5`） |
| pid | 写 `<记忆根>/service.pid`，正常停止后删 | 只给人看，rise 不据此杀进程 |

启动失败的出口，全部是装配期 `FactoryConfigError`（退出码 2），文字写 `service.log` 的相对路径：

| 情形 | 依据 |
|---|---|
| 退出码 3（`data_dir_in_use`）：D 被另一个服务占着 | `S:python/starling/service/serve.py:10-11`、`:117-120`。上次 rise 被 SIGKILL 会留下这种孤儿；文字给出 `service.pid` 里记的 pid |
| 退出码 1：注册表或默认世界开不了，或端口绑不上 | `S:python/starling/service/serve.py:48-51`、`:163-167` |
| 60 秒内没有 ready | 杀掉子进程再退出 |

### 6.3 一个进程一个服务会话

- **一个 `Client`**：组合根造一个服务会话，部署管理面、训练世界、解析器、实时服务端共用。`Client` 声明线程安全（`S:clients/python/starling_client/client.py:47-48`）。
- **为什么不能两个**：写占用按会话算，一个世界同一时刻只能被一个会话写，另一个会话开可写句柄回 `world_held`（服务设计 `:259`）。训练时训练位（客户端线程）与对手位（服务端线程）写同一个 rollout 世界；同一个会话可以在一个世界里开多个句柄（服务设计 `:258`），所以一个会话够用，两个反而会撞。
- **租约**：30 秒，心跳每 10 秒一次，不可配（`S:include/starling/governance/session_table.hpp:58`、`:75-76`；答复 `:152`）。只有心跳续约，调用不续约（`session_table.hpp:10-12`）。心跳在客户端后台线程里跑（`client.py:110-123`），所以几个小时不调用也不会过期。过期只在心跳连续 30 秒到不了服务时发生：服务卡死或退出，或 rise 进程整个停住。

### 6.4 会话过期与服务挂掉

过期后客户端不自动重开句柄，每次调用抛 `SessionExpired`（`client.py:7-10`）。rise 的处理：

| 情形 | 处理 |
|---|---|
| 某次调用收到 `SessionExpired` | 译成瞬态失败（第 8 节）；服务会话对象标为失效 |
| 下一次需要服务（开句柄、管理调用） | 关掉旧 `Client`，重新 `connect`，记一条 warning |
| 旧会话下开的端口 | 调用照旧抛 `SessionExpired`；关闭当作已关（3.3），计数归零 |
| 重连后别的会话占了世界 | 开句柄回 `world_held`，见第 8 节 |
| 拉起的子进程已退出 | 下一次需要服务时重新拉起（新端口、新 token），再连。一个 rise 进程至多重启 3 次，第 4 次起判永久失败 |
| 外部服务连不上 | `ServiceUnavailable` 是瞬态，走延后通道 |

重连而不是一失效就退出：部署轮次的尝试与训练 rollout 都已按"瞬态就重跑"设计，重跑前会恢复快照或换新世界。重连之后的首个可写打开由服务端判写占用，过期期间是否被别人占了世界不会被掩盖（答复 `:136`）。

重启是安全的：已提交写入在 SIGTERM 与重启之后都在（`S:tests/python/test_acceptance_service.py:429-466`）；在途调用丢了，按瞬态重跑。

### 6.5 停止

`Assembly.close` 逆序调收尾函数（`src/factory/wire.py:97-111`）。服务的收尾最先登记、最后执行：

1. 运行时与端口先关（已登记的收尾）。
2. `client.close()`：停心跳、断开会话、释放句柄（`client.py:201-218`）。
3. 拉起的子进程：SIGTERM，等至多 40 秒（服务 drain 30 秒，`S:python/starling/service/serve.py:17-18`，再加 10 秒余量），仍没退出就 SIGKILL；删 `service.pid`。rise 自己被 SIGKILL 时子进程留下，下次启动报 `data_dir_in_use`（6.2）。


## 7. 世界设置与本机记忆配置

### 7.1 rise 设置

`open` 比较整份设置（两边补齐默认值），不同就抛 `world_settings_mismatch`（答复 `:114`；`S:python/starling/dashboard/world_manager.py:577-580`）。rise 在服务会话建好后算一次 rise 设置，之后所有建世界、分叉、开句柄都传同一份：

```python
settings = deep_merge(dict(client.info()["default_settings"]), {
    "extraction": {"provider": "openai", "provider_name": "", "base_url": ..., "model": ...,
                   "timeout_ms": ..., "max_retries": ...},
    "embedding": {"provider": "openai", "provider_name": "", "base_url": ..., "model": ...,
                  "dim": ..., "timeout_ms": ..., "max_retries": ...},
    "chat": None, "consolidation": None, "gist_thresholds": None,
    "vector_backend": "sqlite", "source_lane": {"enabled": False},
    "background": {"tick_interval_s": 0, "ingest": False,
                   "auto_snapshot": {"on_session": False, "interval_hours": 0}},
})
```

| 项 | 取值 | 理由与依据 |
|---|---|---|
| 抽取、嵌入端点 | rise 显式给，`provider_name` 显式置空 | 端点是记忆运行时身份的一部分，rise 自己定才钉得住。合并按叶子覆盖（`S:src/governance/world_settings.cpp:56-89`），不置空就会留下默认世界的 `provider_name`，密钥改取那个 provider 的（`S:python/starling/dashboard/engine.py:271-283`） |
| `chat`、`consolidation` | `None` | rise 不用。`None` 删掉该端点（`S:src/governance/world_settings.cpp:92-103`），这样不继承默认世界的地址，比较也不受默认世界改动影响 |
| `gist_thresholds` | `None`，即 C++ 默认阈值 | 同上（`S:src/governance/world_settings.cpp:193-197`） |
| 来源 lane | 关 | 进程内 `Memory` 没有它；Starling 的门面一致性验收为此关掉它（`S:tests/python/test_acceptance_service.py:74-88`）。不关就改了召回语义，违反非目标 |
| 后台 | 全关 | 默认世界的后台开着（tick 30 秒、摄入开、24 小时快照），不关的话部署世界会自己写，摘要漂移（答复 `:116`）。种子与只读世界由 C++ 强制全关（`S:src/governance/world_settings.cpp:339-351`），可回收世界从不跑后台（`S:python/starling/dashboard/world_manager.py:121`）。三类世界用同一份设置，比较才对得上 |
| 嵌入 | rise 调 `embed_pending` 驱动，时刻取事件时钟 | 与今天相同（`memory_starling.py:122-129`） |

`default_settings` 不含 `key_set`，原样传回不会被拒（答复 `:114`）。**装配期核对**：对配置里每个情境已存在的部署世界，带 rise 设置开一次只读句柄再关上。设置不同在这里就抛 `world_settings_mismatch`，装配期退出码 2，文字写出世界名。开句柄时的设置比较在只读分支之前（`S:python/starling/dashboard/world_manager.py:577-584`），所以只读打开也会比。外部服务的默认世界被人改过时，会在这里暴露，而不是在部署轮次中途。

### 7.2 密钥

| 来源 | 检查 |
|---|---|
| 拉起的服务 | 装配期查 rise 自己的 `OPENAI_API_KEY` 非空（`src/factory/memory_config.py:191-194`），传进子进程环境 |
| 外部服务 | rise 查不到。`key_set` 只按默认世界的各角色报（`S:python/starling/dashboard/routes/client.py:265-279`），不覆盖 rise 显式给的端点。服务环境没有密钥时，首次抽取、嵌入或召回抛 `endpoint_key_missing`（答复 `:118`），按永久失败处理。所以 `key_set` 不用来拦截 |

### 7.3 本机记忆配置的键

今天的键在 `src/factory/memory_config.py:44-48`。没有废弃的键。

| 键 | 今天 | 改后 |
|---|---|---|
| `root` | 必填，库根目录 | 必填。放世界锁、在途记录、`.owner`、拉起服务的数据目录与日志 |
| `extraction_base_url`、`extraction_model` | 必填 | 必填，进 rise 设置的 `extraction` |
| `embedding_base_url`、`embedding_model`、`embedding_dim` | 必填 | 必填，进 rise 设置的 `embedding` |
| `timeout_ms`、`max_retries` | 可选，乘积不超过 4000 毫秒（`memory_config.py:179-183`） | 不变，两个端点都用 |
| `url` | 无 | 新增，可选。给了就连外部服务；必须是回环地址的 http URL |
| `token_file` | 无 | 新增。给了 `url` 时必填：绝对路径、仓库外、0600，与配置文件同一套检查（`memory_config.py:69-79`） |
| `world_prefix` | 无 | 新增，可选，缺省 `rise`，格式见 5.1 |

`url` 限回环地址：本期不换机器（服务设计 `:27`），客户端对回环地址不走代理，bearer token 不经过代理（答复 `:82`）。客户端请求超时取 30 秒（`connect` 的缺省，`S:clients/python/starling_client/client.py:29`）。服务端一次 `remember_event` 至多 3 遍抽取，每遍至多 4 秒，远低于 30 秒，所以客户端超时只在服务卡住时出现。外层时限（`memory_config.py:37-42`）不用改：HTTP 往返是毫秒级。

### 7.4 装配顺序

`memory_wiring`（`src/factory/memory_runtime.py:715-767`）的检查改为，按序：

1. 读配置（第 7.3 节的键）。
2. 安装来源：`starling-client` 已装、不是 editable、`direct_url.json` 有 `vcs_info.commit_id`（沿用 `memory_config.py:280-321` 的取法，换包名）。拉起服务时 `starling-memory` 也要满足，且两个提交相同。今天接受的 `record:` 来源不再接受：没有提交就无法与 `info()["commit"]` 比对。
3. 拉起服务时 `OPENAI_API_KEY` 非空。
4. 认领记忆根；旧布局检查（第 7.6 节）。
5. 拉起或连接服务。`ProtocolMismatch`、`Unauthorized` 在这里抛（`client.py:29-35`；`S:clients/python/starling_client/errors.py:51-54`）。
6. `info()["commit"]` 等于第 2 步的提交，不等就拒绝（答复 `:69`）。
7. 算 rise 设置与运行时身份（7.5）；建空种子；核对已有部署世界的设置。

每一步失败都是 `FactoryConfigError`，退出码 2，文字不含端点地址与 token。

### 7.5 记忆运行时身份

身份仍是五个顶层键（`src/mindtrainer/training_config.py:22`）。顶层键不变，替身的身份常量（`src/factory/memory_runtime.py:78-84`）就不用动，替身 run 的哈希不变。

| 键 | 今天（`memory_runtime.py:173-193`） | 改后 |
|---|---|---|
| `backend` | `starling` | 不变 |
| `starling_version` | `starling.__version__` 加安装来源 | `<info.version>+git:<info.commit>+protocol:<info.protocol>` |
| `extraction` | 地址、模型、超时、重试、`prompts` | 同上，`prompts` 改为 `info()["extraction_config"]` 规范化 JSON 的 sha256；另加 `world_settings`：rise 设置规范化 JSON 的 sha256 |
| `embedding` | 地址、模型、维度、超时、重试 | 不变 |
| `trace_schema` | `simple_actio.trace.v2` | 不变 |

- `extraction_config` 是服务引擎实际用的抽取配置（`S:python/starling/dashboard/routes/client.py:282-290`），取代今天在 rise 进程里摘的默认值（`memory_starling.py:344-355`）。
- `world_settings` 放进 `extraction` 段而不另立顶层键：顶层键一变，替身身份也得跟着变。
- 安装来源（`memory_config.py:280-321`）不再进身份，只在装配期与 `info()["commit"]` 比对（7.4 第 2、6 步）。

### 7.6 旧布局

记忆根下有以下任一项就拒绝启动，文字列出至多 5 条（沿用 `check_memory_layout`，`memory_runtime.py:121-157`）：`deploy/*.db`、`runs/`、`seeds/`、`snapshots/*/` 下的 `.db` 文件或 `.world` 旁注。替身的旧快照（`deploy-*.json` 加 `.world`）靠旁注认出。

## 8. 错误分类

`translate_failure`（`memory_starling.py:79-94`）改成按客户端异常的 `code` 与 `transient` 映射。客户端所有异常都带这两个属性（答复 `:128`），消息就是 code，不带 `detail`（`S:clients/python/starling_client/errors.py:79-98`）。rise 的消息写成 `starling <code> (<类名>)`，进日志与失败记录。服务端的 `detail` 只给码（`S:python/starling/dashboard/routes/client.py:11-12`），所以答复 `:12` 那条点名主人的消息到不了 rise，这条无从保留。

| code | 客户端异常 | rise 的异常 | 瞬态 | 典型去向 |
|---|---|---|---|---|
| `event_conflict` | `EventConflict` | `MemoryEventConflict` | 否 | 门② 已有分支 |
| `event_not_found` | `EventNotFound` | `MemoryFailureError` | 否 | 只在放弃时出现 |
| `recall_degraded` | `RecallDegradedError` | `MemoryFailureError` | 是 | 尝试重跑 |
| `handle_closed` | `MemoryClosed` | `MemoryClosedError` | 否 | 程序错误 |
| `session_expired` | `SessionExpired` | `MemoryFailureError` | 是（改判） | 6.4 重连 |
| `world_busy`、`worlds_unavailable` | `ServiceError` | `MemoryFailureError` | 是 | 延后或重跑 |
| `service_unavailable`、`service_timeout` | 传输层异常 | `MemoryFailureError` | 是 | 延后或重跑 |
| `world_held`（开句柄时） | `WorldError` | `MemoryFailureError` | 是（改判） | 延后或重跑 |
| `world_held`（恢复时） | `WorldError` | `MemoryFailureError` | 否 | `RestoreFailedError` |
| `endpoint_key_missing`、`endpoint_changed`、`llm_not_configured` | `WorldError` | `MemoryFailureError` | 否 | T2 或 T3 |
| `world_settings_mismatch` | `WorldError` | 装配期 `FactoryConfigError`；运行中 `MemoryFailureError` | 否 | 退出码 2 |
| `world_schema_behind`、`world_read_only`、`fork_digest_mismatch`、`restore_digest_mismatch`、`snapshot_not_found` | `WorldError` 或 `ServiceError` | `MemoryFailureError` | 否 | T2 或 T3 |
| `world_not_found`、`world_exists`、`invalid_*` | `WorldError` | `MemoryFailureError` | 否 | 程序错误；`world_exists` 只在 4.4 并发建时被吃掉 |
| `unauthorized`、`protocol_mismatch` | `Unauthorized`、`ProtocolMismatch` | 装配期 `FactoryConfigError` | 否 | 退出码 2 |
| 其他 | 按类 | `MemoryFailureError` | 取 `exc.transient` | 按瞬态与否 |

码与瞬态的原值来自 C++ 错误码目录（`S:src/governance/error_catalog.cpp:14-67`）。rise 改判的只有两处：

- `session_expired` 原值非瞬态。rise 会重连（6.4），重连后的下一次尝试与过期无关。
- 开句柄时的 `world_held` 原值非瞬态。会话过期时旧会话的在途写入仍占着写入（`S:include/starling/governance/session_table.hpp:19-22`），重连后紧接着开句柄会撞上它，几秒后就好。真有别的写者常驻时，延后会连续累积，守护进程连续延后 20 次后退出（`src/controlplane/daemon.py:60`），人照样能看到。

瞬态与否往下的走法不变：瞬态 → 尝试重跑或 `DeferredError`；永久 → 训练指令 T2，部署轮次经 `_memory_op` 判 T3（`src/mindagent/deployment.py:142-155`）。

## 9. 依赖与打包

| 项 | 今天 | 改后 |
|---|---|---|
| extra `memory` | `starling-memory @ git+https://github.com/ruijieguo/starling@730e415…`（`pyproject.toml:15`） | `starling-client @ git+https://github.com/ruijieguo/starling@<提交>#subdirectory=clients/python` 与 `starling-memory[dashboard] @ git+https://github.com/ruijieguo/starling@<提交>`，同一个提交 |
| 客户端的依赖 | 无 | 只有 `httpx>=0.27`（`S:clients/python/pyproject.toml:10`） |
| 服务端 | 无 | `starling-memory` 带 C++ 扩展与 `starling-serve` 入口（`S:pyproject.toml:14-15`）；服务依赖 FastAPI 与 uvicorn，在 extra `dashboard` 里（`S:pyproject.toml:19-24`） |
| 两者同一提交 | 无 | 一条测试读 `pyproject.toml`，断言两行的提交相同；运行期由 7.4 第 2、6 步核对 |
| `src/` 里导入 | `simple_actio/memory_starling.py` 导入 `starling` | 改为导入 `starling_client`；全仓库不再导入 `starling` |

客户端包不含服务（`S:pyproject.toml:28` 只打包 `python/starling`），所以两行都要。给了 `url` 时用不到服务端那一行，是否拆开见第 15 节。

`StarlingEndpoints`（`memory_starling.py:53-76`）不再造 C++ 适配器，改成只装端点字段，供 rise 设置与运行时身份用。

## 10. 关键决策

| 编号 | 决策 | 备选 | 理由 | 代价 |
|---|---|---|---|---|
| D1 | 一个 rise 进程一个服务会话 | 部署与训练各一个会话 | 写占用按会话算，训练位与对手位在同一个 rollout 世界里，两个会话会互相 `world_held` | 会话过期影响全部记忆调用，靠 6.4 重连 |
| D2 | 快照 id 记在记忆根下的在途记录里 | 记进轮次产物；向服务列快照 | 客户端没有列快照的接口；在途记录沿用今天的路径与提交点，MindAgent 的流程不改 | 快照之后、记录之前崩溃会留下孤儿 held 快照 |
| D3 | 提交时先删记录、后删快照 | 先删快照 | 先删快照再崩，重入会用不存在的快照恢复一条已提交的 episode | 孤儿快照占磁盘 |
| D4 | rollout 世界名带随机后缀 | 用重试序号；先删后建 | 客户端不能删世界；序号在进程重启后会撞上旧世界 | 世界名不能由 episode id 推出，靠标签找 |
| D5 | 无种子时从 rise 建的空种子分叉 | 每条 rollout `create_world` | `create_world` 建不出可回收世界，第 6 期收不走 | 多一个常驻的种子世界 |
| D6 | rise 显式给全部会影响记忆的设置，关来源 lane | 继承服务默认世界 | 身份要描述实际行为；进程内没有来源 lane；外部服务的默认世界不归 rise 管，有人会改 | 看板对话页在 rise 的世界里不可用（`chat` 被删） |
| D7 | `wait_closed` 保留，服务端检查兜底 | 只靠服务端的 `world_held` | 对手位端口在另一条线程上收尾，不等就恢复必然被拒 | 进程内计数仍要求组合根只造一个管理面 |
| D8 | 开句柄时的 `world_held` 与 `session_expired` 判瞬态 | 按 C++ 原值判永久 | 两者都会在重连后的几秒里自然消失；常驻的冲突仍会因延后上限暴露 | 真正的写者冲突要 20 次延后后才退出 |
| D9 | 种子由人在 Starling 一侧分叉并打标签，rise 只读 | rise 加建种子的命令 | 现在没有任何种子；分叉路由已带标签与 `seed` | 要人按标签约定手写请求 |
| D10 | 服务端与客户端装在同一个 venv，用 `sys.executable -m starling.service` 拉起 | 另一个环境里的 `starling-serve` 命令 | 一处钉提交，装配期能核对两个包的提交 | rise 的 venv 仍要编译 Starling 的 C++ |

## 11. 失败处理

| 情形 | 判定 | 去向 |
|---|---|---|
| 服务起不来、`data_dir_in_use`、协议或提交不一致、token 错 | 确定性 | 装配期退出码 2 |
| 已有部署世界的设置与 rise 设置不同 | 确定性 | 装配期退出码 2，文字写世界名 |
| 服务连不上、请求超时、`world_busy` | 瞬态 | 部署：`DeferredError`；训练：`role_memory_unavailable` 重跑 |
| 会话过期 | 瞬态 | 当前尝试失败重跑；下一次调用前重连 |
| 拉起的服务退出 | 瞬态，至多重启 3 次 | 第 4 次起永久失败 |
| 恢复时 `world_busy` | 瞬态 | `DeferredError`，重入先恢复 |
| 恢复时 `world_held`、快照没了、摘要不等 | 确定性 | `RestoreFailedError`，记录与快照保留等人 |
| 分叉出的摘要不等于本 run 记下的起点摘要 | 确定性 | 训练位记忆失败，T2 |
| 种子 schema 落后 | 确定性 | T2，原因写"需要升级种子" |
| 解析器遇到服务瞬态失败 | 瞬态 | `DeferredError`，指令留 pending |
| 外部服务没有 `OPENAI_API_KEY` | 确定性 | 首次调用时 T2 或 T3 |
| `delete_snapshot`、`close_world` 失败 | 不影响结果 | warning，只报码 |
| 记忆根是旧布局 | 确定性 | 装配期退出码 2 |

可观测：`service.log` 是服务的 stderr；rise 的失败记录与日志带 code；世界在看板里按 `repo`、`kind`、`run_id` 等标签过滤。按世界的看板页面依赖第 5 期前端，过渡期 rise 不做别的事。

## 12. 对实验的影响

**判 T3 的 run**：所有记忆后端是 Starling 的 interact 训练 run，续训时都判 T3。

- 原因：续训时派发器重新解出起点摘要与运行时身份，再算 `config_hash`（`src/mindtrainer/dispatch.py:399-401`）。哈希一变，`resume_or_run` 抛 `NeedsHumanError`（`src/mindtrainer/orchestrator.py:203-207`）。
- 哪几项变：`starling_version` 从 `730e415` 的版本与安装来源变成服务报出的版本与提交；`extraction` 多了设置摘要；种子从文件变成服务里的世界。任一项变，哈希就变。
- 为什么可以接受：旧记忆不迁移，旧 run 的记忆起点已不存在（本机记忆根 11-06 已删）。Starling 从 `730e415` 到 `cff319d` 跨了迁移 0039，有事件行的库摘要会变（答复 `:32`）。静默续训会把两套记忆行为拼进同一个实验。

**不受影响**：替身 run。替身的运行时身份是常量（`memory_runtime.py:78-84`），替身摘要的格式不变（3.5），哈希不变。

**部署轮次**：轮次产物的 `memory_runtime` 只记录、不参与复用判据（`src/mindagent/deployment.py:125-137`）。新部署世界从空开始。树里若有旧后端下已提交的部署 episode，它会被复用，它的记忆不会写进新世界。本机旧验证树已于 11-06 删除（第 1 节“旧数据”一行）。

**堆积**：每个训练步分叉 `groups_per_step × group_size` 个 rollout 世界，每个部署轮次每个情境一个重放世界。第 6 期合并前它们都留在服务的数据目录里。rise 不对可回收世界拍快照，所以第 6 期“带 held 快照的世界不删”这一条（服务设计 `:222`）挡不住它们。

## 13. 测试

| 层 | 内容 |
|---|---|
| 契约（部署管理面） | 两个后端各过一遍：同 key 两主体各写一次；按主体放弃，用错主体判永久；`pending_events` 带主体；快照、写、恢复后摘要等于快照时；端口开着时恢复被拒；只读端口写入被拒；`fork_replay` 的摘要等于快照 |
| 契约（训练世界） | 两个后端各过一遍：同 rollout 两主体互不召回；同组两条 rollout 起点摘要相同、互不可见；`bind` 给的摘要与分叉结果不等时判永久；同一 episode 重试换新世界 |
| 适配器 | 召回显式传 `strict`、`include_volatile`；`outcome` 不在两值内判永久；关闭时 `SessionExpired` 当作已关；错误码表逐行 |
| 服务进程 | 拉起、读端口、等 ready、SIGTERM 退出码 0；`data_dir_in_use` 译成退出码 2；子进程被杀后重启；token 不出现在命令行、日志、异常里 |
| 会话 | 一个会话里训练位与对手位同写一个世界；会话过期后重连，下一次尝试成功 |
| 设置 | 已有部署世界的设置被改后，装配期退出码 2；默认世界开着后台时，部署世界摘要静置不变 |
| 装配 | 客户端与服务提交不一致、协议不一致、editable 安装、旧布局，各一条退出码 2；没设 `RISE_MEMORY_CONFIG` 时不导入 `starling_client` |
| 打包 | 两条依赖钉同一个提交 |

替身与夹具：

- `tests/starling_fakes.py` 的假端点不变。`FakeOpenAI.endpoints()`（`tests/starling_fakes.py:335-343`）改为给出 rise 设置用的端点字段。
- 真 Starling 的测试起真的 `starling-serve` 子进程，照 Starling 的 `ServiceProc` 写法（`S:tests/python/fakes/service_proc.py:35-147`）：数据目录与 HOME 在 tmp 下，环境里只放占位的 `OPENAI_API_KEY`，模块级共用一个服务。
- 没装客户端或服务端时，Starling 一侧的参数用 `importorskip` 跳过，与今天 `importorskip("starling")` 的做法相同（`tests/test_simple_actio_deployment_store_contract.py:39`）。
- `tests/test_memory_live.py` 改为连 rise 拉起的服务；假端点模块文档里的 GIL 说明（`tests/starling_fakes.py:14-17`）改写：抽取与嵌入现在在服务进程里。

## 14. 施工分步与回退

每一步结束时全套测试绿，替身路径与 Starling 路径都能跑。

| 步 | 内容 | Starling 路径 |
|---|---|---|
| 1 | 钉住的提交升到 `cff319d`，仍进程内；端口的 `abandon_event` 带主体、`pending_events` 返回 `PendingKey`；替身按 `(holder, key)`；三个调用处 | 进程内，按 holder 开连接放弃 |
| 2 | 端口改按快照 id，加 `fork_replay`、`release_replay`、只读 `open`；`MemoryLayout` 改读在途记录；部署驱动器、回滚、验证、门② 跟着改；替身实现 | 进程内管理面用按 id 命名的备份文件实现同一接口（第 4 步删掉） |
| 3 | 新模块：服务拉起与停止、服务会话与重连、rise 设置、错误映射、运行时身份；带真 `starling-serve` 的测试；不接线 | 不变 |
| 4 | 接线：`StarlingMemory`、部署管理面、训练世界、解析器改走客户端；配置新键；装配顺序；旧布局；extra `memory` 换成两行；删进程内代码 | 改走服务 |

回退：第 4 步是一次提交，回退它就回到进程内 `cff319d`。第 1 到 3 步不改变 Starling 路径的外部行为。没装 extra `memory`、没设 `RISE_MEMORY_CONFIG` 时：`memory_wiring` 直接返回替身（`memory_runtime.py:724-730`），不读配置、不导入客户端、不拉起服务。替身只在第 1、2 步改了接口形状，由契约测试钉住与 Starling 同语义。

## 15. 需用户拍板

| 问题 | 推荐 | 理由 |
|---|---|---|
| extra `memory` 是一个还是两个（客户端与服务端分开） | 一个，两行都装 | 本期服务总在本机，一处装、一处钉；拆分留到搬 GPU 主机时 |
| 有内容的种子由谁建 | 本期不加 rise 命令，人在 Starling 一侧分叉并按 5.1 打标签 | 现在没有种子；分叉路由已有；命令等有了用例再加 |
| 遇到上次留下的孤儿服务（`data_dir_in_use`） | 拒绝启动，报出 `service.pid` 里的 pid，由人停掉 | 自动杀进程不可逆，记下的 pid 不保证还是那个服务 |
| 拉起的服务中途退出 | rise 自动重启，至多 3 次 | 已提交写入都在，在途调用本就按瞬态重跑；不重启的话守护进程要连续延后 20 次才退出 |
| 开句柄时的 `world_held` 判瞬态还是永久 | 瞬态 | 会话过期后重连的几秒里一定会撞上；常驻冲突仍会因延后上限暴露 |

## 16. 未核实项

| 项 | 怎么核实 |
|---|---|
| git 安装的 `starling-memory` 的 `info()["commit"]` 是否等于 `direct_url.json` 的 `commit_id`。它来自构建时的 `git rev-parse HEAD`，不在 git 仓库里构建时是 `unknown`（`S:cmake/StarlingBuildInfo.cmake:1-20`） | 按第 9 节的两行装一次，起服务比对；是 `unknown` 时第 7.4 节第 6 步改为只比客户端提交 |
| 空种子的摘要是否等于新建空部署世界的摘要（4.4 的 `digest` 依赖它） | 建一个空世界、一个空种子，比较 `client.digest` |
| rollout 世界与重放世界的堆积速度，以及数据目录的增长（含孤儿 held 快照与恢复留下的 `pre-rollback` 归档） | 跑一个真实训练 run 与一个部署轮次，按标签数世界、量 `service/` 的大小，每天记一次，直到第 6 期合并 |
| `starling-serve` 从启动到 ready 的耗时（影响 `reconcile` 一次性命令与测试夹具） | 本机起 20 次，记 listening 与 ready 两行的时刻 |
| uv 能否锁住同一仓库、同一提交的两个 git 依赖（一个带 `subdirectory`），`starling-memory[dashboard]` 里的 `httpx2` 能否从 PyPI 装上 | 改 `pyproject.toml` 后跑 `uv lock` 与 `uv sync --extra memory` |
| 关掉来源 lane 后，服务的召回与摘要是否逐项等于进程内 `Memory` | 照 Starling 验收第 6 条的写法，用 rise 设置在服务与进程内各跑一遍，比较 |
| 看板正在看某个 rollout 世界时，`close_world` 会不会阻塞到 30 秒 | 看板打开一个世界的页面，同时调 `close_world`，计时 |
| 本机睡眠唤醒后，服务会话是否过期 | 开一个会话，合盖 5 分钟后调用一次 |
| 外部服务的默认世界若有显式的 `chat` 端点，rise 设置把它置空后，建世界与开句柄是否照常 | 起一个带显式 `chat` 端点的服务，建世界、开句柄 |
