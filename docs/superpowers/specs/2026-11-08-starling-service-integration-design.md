# 设计四：rise 接 Starling 记忆服务

状态：设计稿，第 16 节已全部拍板，待复审。日期：2026-11-08 初稿，2026-11-09 修改。基线：设计依据的 rise 代码是 `1c2e364`，初稿提交为 `6961f80`；Starling `cff319d`（记忆服务第 1 至 4 期已合并）。
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
- **改后**：rise 用纯 Python 客户端 `starling_client` 连 Starling 记忆服务，缺省是用户本机的看板服务（6.1）。世界、快照、分叉都按名字和 id 交给服务。rise 不再按路径打开任何世界文件（答复 `:55`）。
- **不变**：`MemoryBackend` 五个方法、替身后端、部署驱动器与门② 的流程、世界锁、`wait_closed` 的顺序约束。

本稿用到的名字：

| 名字 | 含义 |
|---|---|
| 世界 id | rise 内部的世界标识。部署世界是 `scenario_id`，与今天相同 |
| 世界名 | Starling 注册表里的名字，由世界 id 加前缀拼成（5.1） |
| 前缀 | 本机记忆配置里的 `world_prefix`，区分 rise 的世界与同一个服务上的其他世界 |
| 服务会话 | rise 进程里当前在用的那个 `starling_client.Client`，带一个代号（6.3） |
| 看板服务 | 用户本机已在跑的 `scripts/run_dashboard.py`，127.0.0.1:8787，数据目录 `~/.starling`，带前端 |
| 外部模式、拉起模式 | rise 连一个已在跑的服务（缺省），或 rise 自己拉起一个服务（6.1） |
| rise 设置 | rise 传给 `create_world`、`fork`、`open` 的那份完整世界设置（7.1） |
| 在途记录 | 记忆根下的 `.rec.json` 文件，记一份未提交快照属于哪个世界、快照 id 是什么（4.2） |

## 1. 已定事项

| 事项 | 决定 | 来源 |
|---|---|---|
| 接法 | rise 用 Starling 的纯 Python 客户端 `starling_client` 连 `starling-serve`；不再进程内 `import starling` | 用户 2026-11-02 |
| 服务从哪来 | 配置给了服务地址就连；没给就由 rise 按钉住的版本在本机以子进程拉起，等就绪，rise 退出时 SIGTERM | 用户 2026-11-02 |
| 看板 | 缺省连看板服务：记忆配置写 `url` 与 `token_file`，rise 不管它的起停，靠 `world_prefix` 与标签和用户别的世界区分。拉起模式保留，只给测试和没有看板的机器用，这种模式没有页面（6.1） | 用户 2026-11-09 |
| 孤儿 held 快照 | 接受，由人在看板里删。快照时把 episode id（验证快照写 round 与 scenario）写进 `note`；rise 侧不加任何清理（4.2） | 用户 2026-11-09 |
| extra `memory` | 一个 extra，客户端与服务端两行都装（第 9 节） | 用户 2026-11-09 |
| 有内容的种子 | rise 加建种子的命令 `factory seed`，从部署世界分叉（5.6） | 用户 2026-11-09 |
| 拉起模式的残留服务 | 核对后自动停掉，核对不过就拒绝启动（6.2） | 用户 2026-11-09 |
| 拉起的服务中途退出 | 自动重启：退避 1、4、16 秒，按 rise 进程生命周期计，至多 3 次；旧会话等端口关完再关（6.4） | 用户 2026-11-09 |
| 开句柄时的 `world_held` | 只在本进程旧会话的 60 秒排空窗口内判瞬态，其余永久（第 8 节） | 用户 2026-11-09 |
| 世界与主体 | 一个模拟世界 = 一个 Starling 世界，人格是 holder。一个情境一个部署世界；每条 rollout 从情境的种子世界分叉一个平行世界 | 用户 2026-11-03（设计三已落地，只换后端） |
| 看板里看全部世界 | 用户要在 Starling 看板里查看所有世界，含训练中的 rollout 世界；rollout 世界结束后留着复盘 | 用户 2026-11-02 |
| 时机 | 第 4 期合并后对接，跳过单独接第 2 期 | 用户 2026-11-06 |
| 旧数据 | 不迁移旧 `.db`；新系统从空开始（本机旧验证树与记忆根已于 11-06 删除） | 需求 §9 |
| rollout 世界的清理 | rise 分叉时传 `reclaimable=True` 并打 run、scenario、episode、seed 标签；第 6 期自动清理会收走它们，过渡期接受堆积；不设计按标签清世界的命令或兜底清理 | 用户 2026-11-09 |
| 旧 run 判 T3 | 第 12 节只说明哪些 run 判 T3、为什么，不写新起 run 的操作步骤 | 用户 2026-11-09 |

## 2. 目标与非目标

目标：

1. rise 的全部记忆都在看板服务的注册表里，看板按 `repo=<前缀>` 等标签找得到。
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

今天 `_outcome` 只看 `extraction_failed`（`src/simple_actio/memory_starling.py:110-119`），所以 `no_store`、`rejected` 被当成成功、返回空语句。答复要求同时看 `outcome`（答复 `:108`）。C++ 给的其他值有 `no_store`、`rejected`、`source_missing`（`S:src/memory/memory_ops.cpp:51-56`、`:96-102`）。

| 结果 | rise 的处理 |
|---|---|
| `extraction_failed` 为真 | 照旧：`RememberOutcome((), True, category)` |
| `outcome` 是 `accepted` 或 `idempotent` | 照旧：换内容 id，`missing` 判永久失败 |
| 其他 `outcome` | 改为永久 `MemoryFailureError`，消息写 `remember: outcome <值>` |

`no_store` 来自自污染守卫或生产方声明不存（`S:src/bus/bus.cpp:83-93`、`:336-347`）。rise 的事件会不会触发它没有核实（第 17 节）。这一改动对写入结果的影响见第 12 节。

客户端超时后重放同一个 key 时，两次调用都会拿到先提交那次的语句 id（答复 `:110-111`）。rise 按 key 记语句，不受影响。

### 3.3 关闭

`RemoteMemory.close(timeout)` 先等本地在途调用、再请服务端等（`S:clients/python/starling_client/remote_memory.py:116-160`）。超时抛 `TimeoutError`、句柄保持打开，与今天一致。另有三种情形把**本地端口**当作已关（置位、调 `on_close`、计数减一）：

| 情形 | 怎么认出 | 服务端此时的状态 |
|---|---|---|
| 关闭时收到 `SessionExpired` | 异常类型 | 过期会话的句柄成为墓碑，墓碑不算活句柄；但它还没结束的调用照样计入世界的在途数和写占用，直到 `call_exit`（`S:include/starling/governance/session_table.hpp:19-31`） |
| 关闭时收到 `MemoryClosed` | 异常类型 | 只在 rise 退出、服务会话已 `close()` 之后出现 |
| 端口的会话代号不是当前代号 | 端口打开时记下 `ServiceSession` 的代号（6.3），关闭前比较 | 同第一行：旧会话已过期或已被换掉 |

本地计数归零不等于服务端已排空。所以过期之后紧接着恢复，服务端先查活句柄（墓碑不算，`S:python/starling/dashboard/world_manager.py:1607-1610`），再在 30 秒内等租约排干，排不干报 `world_busy`（`:489-499`；常量 `:160`）。4.3 把恢复时的 `world_busy` 判瞬态、轮次延后，正好接住这种情况。抛 `SessionExpired` 之前本地在途数已归零（`remote_memory.py:149-152`），没有本地调用还在跑。

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

今天 `backup_to` 是打开库再 `VACUUM INTO`，`restore_from` 是"先删日志后换文件"（`src/simple_actio/memory_starling.py:498-557`）。改成服务的 held 快照：

```python
@dataclass(frozen=True)
class SnapshotRef:
    snapshot_id: str
    digest: str

def snapshot(self, world_id: str, *, note: str) -> SnapshotRef: ...
def restore(self, world_id: str, snapshot_id: str) -> str: ...
def delete_snapshot(self, world_id: str, snapshot_id: str) -> None: ...
```

| 方法 | Starling 实现 | 依据 |
|---|---|---|
| `snapshot` | `ensure_world`（4.4），再 `client.snapshot(world, note=note)` | 返回 `{snapshot_id, digest}`，kind 是 `held`（`S:clients/python/starling_client/client.py:170-172`） |
| `restore` | `client.restore(world, snapshot_id)` | 返回恢复后的摘要；与快照记录的不等时报 `restore_digest_mismatch`（答复 `:53`） |
| `delete_snapshot` | `client.delete_snapshot` | held 快照不参与修剪，由调用方显式删（服务设计 `:171`） |

`note` 让人在看板的快照列表里认得出它（列表路由 `S:python/starling/dashboard/routes/worlds.py:462-466`，页面显示 note，`S:dashboard/web/src/routes/worlds/+page.svelte:721`）：起点快照写 `rise episode <episode_id>`，验证快照写 `rise verify <round_id> <scenario_id>`。

崩溃安全交给 Starling：恢复是复制、校验、硬链接归档、一步改名（服务设计 `:175`）。rise 删掉 `copy_library`、`library_digest`、`_require_library_file` 与"先删日志后换文件"那一套。替身同样实现这三个方法：快照内容存进替身根下 `held/<world_id>/<snapshot_id>.json`，恢复按 id 读回，`note` 存进同名记录。

### 4.2 在途记录取代快照文件与 `.world` 旁注

客户端没有列快照的接口（`S:clients/python/starling_client/client.py:147-197`），所以快照 id 由 rise 自己记，记在记忆根下：

| 路径（记忆根下） | 内容 | 生命周期 |
|---|---|---|
| `snapshots/<round_id>/deploy-<episode_id>.rec.json` | 起点快照的在途记录：`world_id`、`snapshot_id`、`digest` | 删除就是这条 episode 的提交 |
| `snapshots/<round_id>/verify-<scenario_id>.rec.json` | 验证快照的在途记录 | 验证收尾时删 |

记录用"临时文件加改名"写，内容自带世界 id，`.world` 旁注不再需要。两个后端的记录格式相同。`MemoryLayout`（`src/mindagent/memory_layout.py:56-158`）改动如下：

| 项 | 改后 |
|---|---|
| 识别规则 | 文件名以 `deploy-` 起头、以 `.rec.json` 结尾才是在途记录。替身旧快照 `deploy-*.json` 不带 `.rec`，分得开 |
| `round_snapshots`、`uncommitted_snapshots`、`any_uncommitted_snapshots`、`world_of` | 签名不变，改为读记录；`world_of` 读记录里的 `world_id`，过 `PERSONA_ID_PATTERN` |
| `snapshot`、`verify_snapshot` | 返回记录路径；`write_world_tag`、`tagged_snapshots`、`verify_scratch` 删掉 |
| 读不出的记录 | 按今天坏旁注的口径：装配期拒绝启动，门② 延后，回滚判 `RestoreFailedError` |
| `suffix` | 删掉；旧布局的判定见 7.6 |

记录与 held 快照这一对由世界锁保护（`src/mindagent/memory_layout.py:60-65`）。顺序与崩溃点：

| 动作 | 持锁与顺序 | 崩在中间的后果 |
|---|---|---|
| 开 episode | 取世界锁 → `snapshot` → 写记录 → `on_episode_start(eid, digest)` | 快照之后、记录之前：孤儿 held 快照；重入时重拍 |
| 提交 | 取世界锁 → 删记录 → `delete_snapshot` | 删记录之后：孤儿 held 快照。反过来先删快照再崩，重入会拿一个不存在的 id 去恢复已提交的 episode。`delete_snapshot` 失败只记 warning（只报码），不影响提交 |
| 回滚 | 取世界锁 → `restore` → 删记录 → `delete_snapshot` | 恢复之后：重入再恢复一次同一份快照，结果相同 |
| 门② 查在途 | 在它取到的世界锁内读记录（`src/mindagent/admit.py:160-185` 的现有顺序） | 只读 |

部署轮次在 `_WorldLocks` 里持本轮全部情境的锁（`src/mindagent/rounds.py:315-317`），驱动器的开 episode 与提交都在锁内，所以上表的"取世界锁"不新增代码。

孤儿 held 快照只在崩溃时出现，第 6 期也不会清（held 快照不参与修剪，服务设计 `:171`）。用户已定：由人在看板的快照列表里按 `note` 认出并删掉（第 1 节），rise 侧不清理。

### 4.3 `wait_closed` 与 `world_held`、`world_busy`

服务端恢复的前提是这个世界没有活句柄、没有在途调用。有活句柄回 `world_held`（`S:python/starling/dashboard/world_manager.py:1607-1610`）；在途调用 30 秒内排不干回 `world_busy`（`:489-499`，常量 `:160`）。rise 的 `wait_closed` 保留，两者分工不同：

| 机制 | 管什么 | 为什么都要 |
|---|---|---|
| `wait_closed`（rise 进程内计数） | 本进程开的端口，含服务端线程上的对手位端口 | 对手位端口在另一条线程上收尾，最长 60 秒（设计三 4.5）。不等就去恢复，服务端一定回 `world_held` |
| 服务端检查 | 所有会话的句柄与在途调用 | 兜底，挡住本进程看不见的写者与过期会话还没结束的调用 |

`wait_closed` 返回真之后，恢复收到的码按下表处理（`src/mindagent/deployment.py:166-181` 的 `_restore` 与 `src/mindagent/rounds.py:337-372` 的回滚都照此改）：

| 码 | 含义 | 处理 |
|---|---|---|
| `world_busy` | 在途调用或看板的检视租约没排干，含 3.3 说的过期会话的调用 | 瞬态：`DeferredError`，轮次留 pending，重入时先恢复 |
| `world_held` | 本进程端口已全关，还有别的会话的活句柄 | 确定性：`RestoreFailedError`，快照与记录都保留等人 |
| `snapshot_not_found`、`restore_digest_mismatch` 及其他 | 快照没了或被改过 | 确定性：`RestoreFailedError` |

### 4.4 部署世界的建立与读

Starling 对注册表里没有的世界一律回 `world_not_found`，不会自动建（`S:python/starling/dashboard/world_manager.py:371-384`）。今天进程内打开不存在的库会建库（`src/simple_actio/memory_starling.py:374-375`），这一行为改由 rise 的 `ensure_world` 做：

1. `create_world(世界名, labels=..., settings=rise 设置)`。
2. 收到 `world_exists`：`list_worlds(labels={repo, kind, scenario_id})` 找这个名字。找不到，或 `seed`、`read_only` 为真，或标签 `kind`、`repo` 不对，判永久失败，原因写"同名世界不是 rise 建的"。对得上就当作已建。
3. 设置是否一致由随后的 `open` 比较，不同报 `world_settings_mismatch`（7.1）。

| 操作 | 世界不存在时 |
|---|---|
| `open`、`remember_event`、`snapshot` | 先 `ensure_world` |
| `digest` | 返回空种子的摘要（5.4），不建世界 |
| `pending_events` | 返回 `[]` |
| `abandon_event` | 永久失败（没有这个 key） |

空种子的摘要等于新建空部署世界的摘要（2026-11-09 实测，同一份 rise 设置下两者都是 `940476f73d477c76…`），所以 `digest` 这一行成立。`remember_event`（门② 用）在 holder 的句柄上写、嵌入、关闭，与今天的短连接同口径（`src/simple_actio/memory_starling.py:459-475`）。`abandon_event` 同样开 holder 的句柄。`pending_events`、`digest` 用不开句柄的管理调用（`S:clients/python/starling_client/client.py:181-186`）。

### 4.5 验证重放：从快照分叉可回收世界

今天：每个情境拍验证快照，再 `restore_from` 进一个临时目录里的 store（`src/mindagent/rounds.py:466-469`、`:483`）。改后不再造临时 store，管理面多三个方法：

```python
def fork_replay(self, world_id: str, snapshot_id: str, *, round_id: str) -> str: ...
def release_replay(self, replay_world_id: str) -> None: ...
def open(self, world_id: str, holder: str, *, read_only: bool = False) -> MemoryBackend: ...
```

| 步 | 改后 |
|---|---|
| 验证快照 | 每个情境 `snapshot(scenario_id, note=...)`，写 `verify-<scenario_id>.rec.json`。记录已在（上次崩溃留下）就先 `delete_snapshot` 旧的 |
| 实例工厂 | `fork_replay(scenario_id, snapshot_id, round_id=...)` 得到重放世界 id，`open(..., read_only=True)` 开智能体位主体的只读端口 |
| 分叉参数 | `reclaimable=True`，`retention_group=round_id`，标签见 5.1。可回收世界从不跑后台任务（答复 `:116`） |
| 清理 | finally 里关实例、`release_replay`、删验证快照与记录 |

`release_replay` 在 Starling 上是 `close_world`：客户端没有删世界的接口（`S:clients/python/starling_client/client.py:147-197`；管理操作表 `S:python/starling/dashboard/routes/client.py:411-422` 里也没有），重放世界留给第 6 期。替身的 `fork_replay` 在同一个 `FileTupleStore` 根下建 `verify.<round_id>.<scenario_id>.<后缀>.json`，`release_replay` 删掉它。

只读句柄够用：重放实例 `replay_read_only=True`，只调 `recall`、`digest` 与 `close`（`src/simple_actio/instance.py:166`、`:172`、`:178-179`、`:238`），这些在服务端都不算写（`S:python/starling/dashboard/routes/client.py:200-209`）。替身的只读端口在写入时抛永久失败，与 Starling 的 `world_read_only` 同类，契约测试钉住。

`scratch_store_factory` 整条链去掉：

| 位置 | 今天 | 改后 |
|---|---|---|
| `src/factory/memory_runtime.py:101-110` | `DeploymentMemory` 有 `scratch_store_factory` 字段 | 删字段；两个后端的构造处（`:167-168`、`:760-765`）同删 |
| `src/factory/handlers.py:122` | 把它传给 `make_start_round_handler` | 删这个关键字参数 |
| `src/mindagent/rounds.py:294`、`:303` | 处理器收 `scratch_store_factory` | 删参数；验证改调 `memory_store.fork_replay`、`release_replay` |
| `src/mindagent/rounds.py:463-506` | 每个实例造一个临时 store，`restore_from` 后开端口；finally 里 `rmtree` 临时目录 | 每个情境的基线与候选各 `fork_replay` 一次、只读 `open`；finally 里 `release_replay` |
| 测试夹具 | `tests/test_ma_rounds.py`、`tests/test_ma_e2e.py` 传 `scratch_store_factory=FileTupleStore`；`tests/test_factory_handlers.py` 断言它被传下去 | 删参数与断言；替身管理面自带 `fork_replay` |

## 5. 训练世界、种子与起点摘要

### 5.1 世界名与标签

Starling 的世界名规则：首字符字母或数字，其余 `[A-Za-z0-9._-]`，1 到 128 个字符，区分大小写（`S:src/governance/world_store.cpp:47-59`）。rise 的世界名一律以前缀开头，前缀与种类之间用 `.` 分隔：

| 世界 | 世界名 | 标签（都带 `repo=<前缀>`） | 可回收 |
|---|---|---|---|
| 部署世界 | `<前缀>.deploy.<scenario_id>` | `kind=deploy`、`scenario_id` | 否 |
| 空种子 | `<前缀>.seed-empty` | `kind=seed-empty`（不带 `seed_id`，免得和人起的种子 id 撞） | 否（种子） |
| 种子 | `<前缀>.seed.<seed_id>.<scenario_id>.v<n>`，由 `factory seed` 生成（5.6） | `kind=seed`、`seed_id`、`scenario_id`、`version=<n>` | 否（种子） |
| rollout 世界 | `<前缀>.ro.<episode_id>.<8 位十六进制>` | `kind=rollout`、`run_id`、`scenario_id`、`episode_id`、`seed_id`、`attempt` | 是，`retention_group=run_id` |
| 重放世界 | `<前缀>.verify.<round_id>.<scenario_id>.<8 位十六进制>` | `kind=verify`、`round_id`、`scenario_id` | 是，`retention_group=round_id` |

- 前缀的格式是 `[A-Za-z0-9][A-Za-z0-9_-]{0,15}`，不含 `.`，所以世界名能无歧义地拆开。缺省 `rise`。看板服务的数据目录 `~/.starling` 里还有用户别的世界；rise 只建、只开、只关名字以 `<前缀>.` 开头且带 `repo=<前缀>` 标签的世界，不碰默认世界与其他世界。
- 逻辑 episode id 是 `{run_id}-grpo{step}-{scenario_id}-r{k}`（`src/mindtrainer/trajectory_source.py:31-34`），例如 `run-1-grpo3-scen-auto-1-r1`。`run_id` 形如 `run-<数字>`（`src/controlplane/reconcile.py:115`），生成的情境 id 形如 `scen-auto-<seed>`（`src/simulator/backends.py:97`）。三者都只用 `[A-Za-z0-9._-]`，合规。
- 拼出的名字超过 128 个字符时，把中间那段换成它的 sha256 前 24 位，标签里照旧是完整的 id。rollout 与重放世界名带随机后缀：世界删不掉，同一个 episode 的重试必须换名（5.2）。
- 标签建好不能改（服务设计 `:99`）；空键被拒，空值可以（`S:src/governance/registry_db.cpp:342-349`）。

### 5.2 `StarlingTrainingWorlds`

表逻辑 `_RolloutWorlds`（`src/factory/memory_runtime.py:351-542`）不变：客户端建、服务端按 episode id 加入、客户端等服务端端口关完再收尾。三个子类钩子改实现：

| 钩子 | 今天 | 改后 |
|---|---|---|
| `_create` | 删旧文件，从 `seeds/<seed>/<scenario>.db` 复制（`memory_runtime.py:618-639`） | `client.fork(种子世界名, 新世界名, reclaimable=True, retention_group=run_id, labels=..., settings=rise 设置)` |
| `_port` | `StarlingMemory.open(path, holder=...)` | `client.open(世界名, holder=..., settings=rise 设置)` 包成 `StarlingMemory` |
| `_discard` | 删库文件 | `close_world`，只关不删 |
| `sweep` | 删 `runs/` 下全部子项（`memory_runtime.py:650-673`） | 去掉：没有本地文件要清；`MemoryWiring.sweep` 为 `None` |

- **起点核对**：`bind(run_id, seed, digests)` 多收一个参数，是派发器解出的 `memory.digests`。调用处 `src/factory/interact_policy.py:393` 改传 `cfg.memory["digests"]`（策略工厂拿到的已是解析后的配置，`src/mindtrainer/dispatch.py:401-412`）。`bind` 为每个情境在候选种子里取摘要等于 `digests[scenario_id]` 的版本最大的一份，钉住它的世界名，这个 run 的 rollout 都从它分叉；解析之后有人用 `factory seed --new-version` 建了新版本，也不会换掉在跑的 run 的起点。一份都对不上就抛永久失败。`_create` 再比一次 `fork` 返回的 `digest`，作第二道。`fork` 返回 `{world, digest}`、摘要等于来源，2026-11-09 已实测。
- **重试与命名**：世界删不掉，同一个 episode 的每次 `_create` 都换一个 8 位随机后缀，`attempt` 标签从 1 递增（进程内按 episode id 计）。保留表与"端口没关上就判瞬态"的规则不变。
- **`fork` 失败**：`world_exists` 是永久码（`S:src/governance/error_catalog.cpp:17`；`S:src/governance/world_store.cpp:2008-2012`），收到就换后缀再分叉，至多 3 次。`ServiceTimeout` 时服务端不一定没做：先按 `list_worlds(labels={repo, kind=rollout, episode_id, attempt})` 查，查到就用它，查不到再用新后缀重试。物理世界名写进端口与 warning 日志。
- **`_discard` 的异常**：`close_rollout` 只接 `OSError`（`memory_runtime.py:507-512`）。`_discard` 内部把 `starling_client` 的异常译成 `MemoryFailureError` 并吸收，只记 warning（只报码），公共收尾逻辑不改。
- **settings 传给 fork**：分叉的设置是部分设置，叠在来源的设置上（`S:python/starling/dashboard/world_manager.py:1335-1336`）。传完整的 rise 设置，rollout 世界的行为就由 rise 的配置决定，与种子是谁建的无关。
- **服务端加入**：`join_rollout` 照旧查进程内的表拿到世界名，再在当前服务会话里开对手位句柄（`src/factory/agent.py:361`）。

### 5.3 种子

种子是 `seed=true` 的世界。`seed` 蕴含只读，不跑后台，不参与清理，打开时不迁移（答复 `:47`）。

| 项 | 决定 |
|---|---|
| 谁建 | 有内容的种子由人用 `factory seed` 从部署世界分叉（5.6，用户 11-09 定）；空种子由 rise 在装配期自动建（5.4） |
| 怎么找 | `list_worlds(labels={repo, kind=seed, seed_id, scenario_id}, with_digest=True)`，翻完所有页（缺省每页 50，`S:clients/python/starling_client/client.py:191-197`；上限 500，`S:src/governance/registry_db.cpp:37`），只取 `seed` 为真的项。取 `version` 标签最大的；同版本有几份（升级出来的，见下一行）取 `created_at` 最大的，再相同取名字最大的 |
| 升级 | 本期不做命令，由人在看板上调升级路由（`S:python/starling/dashboard/routes/worlds.py:414`）。升级出的新种子照抄旧种子的标签，含 `version`（`S:src/governance/world_store.cpp:2154`），所以按上一行的规则被选中。`created_at` 精确到秒（`S:python/starling/dashboard/world_manager.py:207-210`） |
| 旧布局 `seeds/<seed_id>/<scenario_id>.db` | 不再支持。记忆根下有 `seeds/` 目录就在装配期拒绝启动（退出码 2），与设计三 4.8 的旧布局同一口径。今天还没有任何种子（需求 §4.4） |
| 种子落后于当前 schema | 分叉返回 `world_schema_behind`（`S:src/governance/world_store.cpp:1950-1958`），rise 判永久失败，派发器 T2。原因里写"需要人在看板里升级种子" |

### 5.4 没有种子时

`seed: null` 或某个情境没有种子时，rollout 世界从空种子 `<前缀>.seed-empty` 分叉。

- 不用 `create_world` 建 rollout 世界：`create_world` 没有 `reclaimable` 参数（`S:clients/python/starling_client/client.py:152-155`），建出来的世界永远不会被第 6 期收走。
- 空种子由 rise 在装配期建：不存在就 `create_world(name, seed=True, labels=..., settings=rise 设置)`。
- 同组相同：同组的每条 rollout 都从同一个空种子分叉，分叉保证摘要等于来源（答复 `:45`）。

### 5.5 起点摘要

解析器 `starling_memory_resolver`（`memory_runtime.py:242-294`）的签名不变，实现改为：

| 情形 | 今天 | 改后 |
|---|---|---|
| `seed` 为 null | 每个情境 `empty_digest()` | 每个情境取空种子的摘要。rise 不再自己算空库摘要，`empty_digest()` 删掉 |
| 情境有种子 | 拷文件到临时目录再算（`library_digest`） | `list_worlds(with_digest=True)` 里该种子的 `digest` |
| 情境没有种子 | 空库摘要 | 空种子的摘要 |
| 这个 `seed_id` 一份种子都没有 | 目录不在时 `DirectiveFailedError` | 同，原因写种子 id |
| 服务瞬态失败 | 不会发生（只读本地文件） | 抛 `DeferredError`，指令留 pending |

派发器今天把解析器抛的一切异常译成 `DirectiveFailedError`（`src/mindtrainer/dispatch.py:287-290`），瞬态的服务失败也会变成 T2。所以 `_resolve_memory` 加一条：`DeferredError` 原样上抛。

### 5.6 建种子的命令 `factory seed`

用户 11-09 定由 rise 建种子。命令放在 `src/factory/cli.py` 的子命令里，写法照 `import-sombench`（`src/factory/cli.py:72-77`、`:125-139`）：参数解析在 `build_parser`，实现放新模块 `src/factory/seed.py`，惰性导入，`FactoryConfigError` 译成退出码 2。

```
factory seed <repo_root> --seed-id <seed_id> --scenario <scenario_id> [--scenario ...] [--snapshot <id>] [--new-version]
```

| 项 | 决定 |
|---|---|
| 种子 id | `--seed-id` 必填，人给，过 `PERSONA_ID_PATTERN`（`src/simple_actio/memory_port.py:13`）。指令里的 `memory.seed` 填同一个值 |
| 情境 | `--scenario` 至少一个，必须在 `agent.yaml` 的 `scenario_ids` 里。逐个处理，按 id 排序 |
| 来源 | 缺省是该情境部署世界的当前状态。`--snapshot` 给的是人在看板快照列表里看到的快照 id，只能配一个 `--scenario`。rise 不为建种子拍快照：持着世界锁时没有 rise 的写者，从开着的世界分叉读的是它最近一次提交（`S:python/starling/dashboard/world_manager.py:1355-1368`），拍一份临时快照只多一个留下孤儿的窗口 |
| 世界锁 | 每个情境先取世界锁（`src/mindagent/memory_layout.py:63-65`），被占就退出码 2，原因写"部署轮次或门② 正在用这个世界"。锁内查 `uncommitted_snapshots(scenario_id)`，有在途 episode 也退出码 2：那条 episode 会被回滚，种子不能拍进它的写入 |
| 部署世界不存在 | 退出码 2。空种子已由 rise 自动建（5.4） |
| 已有同 `seed_id` 与 `scenario_id` 的种子 | 缺省拒绝，退出码 2，打印已有的世界名与版本。加 `--new-version` 才建 v(n+1)，n 是这组标签下最大的 `version`；首份是 v1 |
| 分叉参数 | `fork(部署世界名, 种子世界名, snapshot_id=..., seed=True, labels={repo, kind=seed, seed_id, scenario_id, version}, settings=rise 设置)` |
| 输出 | 每个情境一行：`<世界名> <摘要>`，摘要取 `fork` 返回的 `digest` |
| 两种模式 | 都能用。命令按 7.4 的第 1 至 6 步打开服务、算 rise 设置，不造部署与训练对象；用完关会话，拉起模式下停掉服务。命令不写控制面的树，不取仓库锁：外部模式下守护进程在跑时也能用，冲突由世界锁挡住；拉起模式下守护进程在跑时归属锁被占（6.2），命令退出码 2，要先停守护进程 |
| 替身 | 退出码 2：替身没有种子 |
| 升级 | 不做命令，由人在看板上做（5.3）。升级只在 Starling 迁移版本变了之后才需要，次数少 |

崩溃与超时：Starling 的分叉在复制、校验都过了之后才登记，失败不留文件也不留注册项（答复 `:45`），所以崩在任何一步，种子要么完整存在，要么不存在。重跑时已存在就按"已有种子"那一行拒绝并打印它。`fork` 超时（`ServiceTimeout`）时先按世界名查 `list_worlds`，查到就当作成功打印，查不到退出码 2，提示重跑。

对实验的影响：`--new-version` 之后，派发器为引用这个 `seed_id` 的指令解出的摘要换成新版本的，续训中的 run 因此判 T3（第 12 节）。所以缺省拒绝，改版本要显式。

## 6. 服务进程与会话

### 6.1 两种模式

| 模式 | 什么时候用 | 配置 | 有没有页面 |
|---|---|---|---|
| 外部（缺省） | 本机日常使用 | `url` 与 `token_file` 都给 | 有：看板服务带前端 |
| 拉起 | 测试；没有看板的机器 | 两者都不给 | 没有 |

**外部模式**连看板服务（`S:scripts/run_dashboard.py:36-51`）。它与 `starling-serve` 走同一个入口，带数据目录锁、会话表与客户端路由（`S:python/starling/service/serve.py:20-22`、`S:python/starling/dashboard/app.py:128-133`），数据目录是 `~/.starling`，token 在 `~/.starling/starling.json`（`S:python/starling/dashboard/config.py:138-153`）。rise 不管它的起停。对用户的要求：

| 要求 | 为什么 |
|---|---|
| 人从 `starling.json` 抽出 `token` 字段，写成一个只有属主可读写的文件，路径填进 `token_file`。rise 不读 `starling.json` | `starling.json` 里还有各 provider 的密钥，rise 不该拿到 |
| 看板服务跑在 rise 钉住的提交上。它跑在 Starling 源码树里（`S:scripts/run_dashboard.py:20`），用户升级 Starling 源码树时要同步升级 rise 的钉住版本，否则 rise 拒绝启动（7.4 第 6 步） | 客户端与服务必须同一提交（答复 `:69`）。`commit` 是构建时编进去的 `git rev-parse HEAD`（`S:cmake/StarlingBuildInfo.cmake:1-20`）；本机 Starling 是 editable 安装，源码树里没提交的改动这一检查看不出来 |
| 看板服务的进程环境里有 `OPENAI_API_KEY` | rise 显式给端点，密钥取服务进程的环境（7.2）；rise 查不到它 |

数据目录与用户别的世界共用，rise 只动带自己前缀的世界（5.1）。看板页面在 `http://127.0.0.1:8787/`，首次打开用 `#token=…` 登录（`S:scripts/run_dashboard.py:50`；前端读 URL 片段后从地址栏删掉，`S:dashboard/web/src/lib/token.ts:12-21`）；世界页按 `label.repo=<前缀>` 等标签过滤（`S:dashboard/web/src/lib/worlds.ts:211-217`）。顶栏选世界、按世界取数已在 `cff319d` 里（Starling `e7db61c`）。前端 build 在 Starling 源码树的 `dashboard/web/build`，缺了由 `run_dashboard.py` 用 npm 构建（`S:scripts/run_dashboard.py:23-33`）。

**拉起模式**没有页面：前端只在源码树里有 `dashboard/web/build` 时才挂上（`S:python/starling/dashboard/app.py:479-490`），git 安装的 wheel 只打包 `python/starling`（`S:pyproject.toml:28`）；端口随机、token 在 ready 后删掉，人也拿不到。只用 API。

### 6.2 拉起模式

| 项 | 决定 | 依据 |
|---|---|---|
| 命令 | `[sys.executable, "-m", "starling.service", "--data-dir", D, "--host", "127.0.0.1", "--port", "0", "--token-file", F]` | `python -m starling.service` 等同于 `starling-serve`（`S:python/starling/service/__main__.py:1-5`）；用 rise 自己的解释器，服务端与客户端来自同一个 venv |
| 数据目录 D | `<记忆根>/service` | 记忆根已由 `.owner` 归属一个仓库（`src/factory/memory_config.py:227-264`）。所有文件都在 D 下（`S:python/starling/dashboard/config.py:158-181`） |
| 子进程环境 | 父进程环境去掉 `STARLING_*`，`HOME` 指到 `<记忆根>/service/home`，`OPENAI_API_KEY` 照传 | 照 Starling 测试夹具（`S:tests/python/fakes/service_proc.py:35-147`），不读用户的 `~/.starling`；显式端点的密钥取服务进程环境（答复 `:118`） |
| 端口 | 0，从 stdout 的 `starling-serve listening on http://127.0.0.1:<端口>` 读回 | `S:python/starling/service/serve.py:12-13`、`:186` |
| token | 每次启动 `secrets.token_urlsafe(32)`：先删上次遗留的 `<记忆根>/service.token`，再写临时文件（0600）、改名到这个固定路径，看到 ready 后删掉 | 服务只在启动时读一次（`S:python/starling/service/serve.py:219-227`）。不进命令行、日志与异常 |
| 就绪 | 等 stdout 出现 `starling-serve ready`，上限 60 秒 | 就绪之前一切接口回 503（`S:python/starling/service/serve.py:12-16`）。2026-11-09 实测：空数据目录下 listening 0.31 秒、ready 0.36 秒 |
| stdout、stderr | stdout 由一条线程读完；stderr 追加到 `<记忆根>/service.log` | 访问日志多，管道会写满（`S:tests/python/fakes/service_proc.py:4-5`） |
| 归属锁 | 拉起服务之前对 `<记忆根>/service.owner.lock` 取独占非阻塞锁（`src/controlplane/lock.py:81-93` 的 `hold_file_lock`），持到 rise 退出；描述符不传给子进程 | rise 进程死掉（含 SIGKILL）时内核释放它，所以锁取得到就说明没有活着的 rise 在用拉起的服务。Python 3.4 起新建的描述符缺省不继承（PEP 446） |
| pid 文件 | rise 写 `<记忆根>/service.pid`（临时文件加改名，0600），内容是 JSON：`pid` 与完整 `argv`；正常停止后删 | Starling 不写 pid 文件，只持 `D/.lock`（`S:python/starling/service/serve.py:10-11`）。残留服务的核对用它（下文） |

**残留服务**（用户 11-09 定：核对后自动停掉）。只在拉起模式下、子进程以退出码 3（`data_dir_in_use`，`S:python/starling/service/serve.py:61-74`）退出时走；外部模式下 rise 绝不碰服务进程。按序核对，任一项对不上就拒绝启动、报出那一项，不杀：

| 序 | 核对 | 怎么查 | 对不上的例子 |
|---|---|---|---|
| 1 | 归属锁已在本进程手里 | 拉起之前已取（上表） | 另一个 rise 进程（守护进程、`reconcile`、`factory seed`）正在用这个服务 |
| 2 | `service.pid` 在、读得出 | 读 JSON | 文件缺失、内容坏 |
| 3 | 那个 pid 活着 | `os.kill(pid, 0)` | `ProcessLookupError` |
| 4 | 进程属于当前用户 | macOS：`ps -o uid= -p <pid>`；Linux：`os.stat("/proc/<pid>").st_uid`；与 `os.getuid()` 比 | 别的用户 |
| 5 | 命令行除第一项（解释器路径）外等于 pid 文件记的 `argv[1:]`，其中是 `-m starling.service`、`--data-dir` 后面是本次的 D；第一项的文件名以 `python` 开头（不分大小写） | macOS：`ps -ww -o command= -p <pid>`（不加 `-ww` 会按终端宽度截断），去掉开头的解释器路径后与 `" ".join(argv[1:])` 逐字比；Linux：`/proc/<pid>/cmdline` 按 NUL 拆开，与 `argv[1:]` 逐项比 | pid 被复用；命令行读不到（权限、进程刚退出） |
| 6 | `D/.lock` 确实被持有 | 对它试独占非阻塞锁，与服务同一取法（`S:python/starling/service/serve.py:62-68`） | 拿得到：残留服务已经退了，立即释放，不杀、直接重试启动 |

全部对上才 SIGTERM，等至多 40 秒（服务 drain 30 秒，`S:python/starling/service/serve.py:17-18`，加 10 秒余量）看 `D/.lock` 释放；还没释放就 SIGKILL，再等至多 5 秒。然后删 `service.pid`，重试启动一次；再报 `data_dir_in_use` 就拒绝启动，不再杀。每一步记一条 warning，写 pid 与核对结果，不写 token。

不比解释器路径：macOS 上 venv 的 `python` 会换成框架里的 `Python.app/Contents/MacOS/Python` 再执行，`ps` 显示的第一项与 `sys.executable` 不同（2026-11-09 实测）。D 只用不含空格的路径，否则 macOS 上逐字比会有歧义，D 含空格时这一项判对不上。核对 6 只证明有人持着锁，证明不了持锁的就是这个 pid（macOS 没有按进程查 flock 的接口）。核对 5 要求命令行就是 rise 为这个 D 拉起的那条，两条合起来防误杀。核对 1 防的是另一种误杀：两个 rise 进程同时用拉起模式，第二个看到的服务五项全对，却是第一个正在用的。

启动失败的出口，全部是装配期 `FactoryConfigError`（退出码 2），文字写 `service.log` 的相对路径：

| 情形 | 依据 |
|---|---|
| 归属锁被占 | 文字写"另一个 rise 进程正在用拉起的服务"。拉起模式下守护进程在跑时，第二个 `run` 与 `factory seed` 都停在这里（`reconcile` 先撞上仓库锁，`src/factory/wire.py:530-531`） |
| 残留服务核对不过，或停掉后重试仍是 `data_dir_in_use` | 文字写对不上的那一项与 pid，提示人确认后手动停掉、再删 `service.pid` |
| 退出码 1：注册表或默认世界开不了，或端口绑不上 | `S:python/starling/service/serve.py:12-16`、`:163-167` |
| 60 秒内没有 ready | 杀掉子进程再退出 |

### 6.3 服务会话

- **一个当前会话**：组合根造一个 `ServiceSession`，持当前 `Client` 与代号（从 1 递增），部署管理面、训练世界、解析器、实时服务端共用。`Client` 声明线程安全（`S:clients/python/starling_client/client.py:47-48`）。
- **为什么不能同时用两个**：写占用按会话算，另一个会话开可写句柄回 `world_held`（服务设计 `:259`）。训练位（客户端线程）与对手位（服务端线程）写同一个 rollout 世界；同一个会话可以在一个世界里开多个句柄（服务设计 `:258`）。
- **端口记代号**：每个端口记下打开时的代号，并让那个代号的引用计数加一；端口关闭（含 3.3 的三种"当作已关"）时减一。
- **租约**：30 秒，心跳每 10 秒一次，不可配（`S:include/starling/governance/session_table.hpp:58`、`:75-76`；答复 `:152`）。只有心跳续约（`session_table.hpp:10-12`），心跳在客户端后台线程里跑（`client.py:110-123`），所以几个小时不调用也不会过期。过期只在心跳连续 30 秒到不了服务时发生：服务卡死或退出，或 rise 进程整个停住。

### 6.4 会话过期与重连

过期后客户端不自动重开句柄，每次调用抛 `SessionExpired`（`client.py:7-10`、`:87-108`）。rise 的处理：

| 情形 | 处理 |
|---|---|
| 某次调用收到 `SessionExpired` | 译成瞬态失败（第 8 节）；记下"代号 n 已过期"及它在哪些世界上有可写句柄 |
| 下一次需要服务（开句柄、管理调用） | 新建 `Client`，代号加一，换成当前会话；记一条 warning |
| 旧 `Client` | 不立即关：等它的引用计数归零再 `close()`。别的线程上旧端口的调用照样抛 `SessionExpired`，不会撞上已关的 transport。过期的 `Client` 关闭时不发 disconnect（`client.py:201-218`） |
| 重连后开句柄回 `world_held` | 见第 8 节的窗口规则 |
| 外部服务连不上 | `ServiceUnavailable` 是瞬态，走延后通道 |

重连而不是一失效就退出：部署轮次的尝试与训练 rollout 都已按"瞬态就重跑"设计，重跑前会恢复快照或换新世界。重连之后的首个可写打开由服务端判写占用，过期期间是否被别人占了世界不会被掩盖（答复 `:136`）。

**拉起模式下服务退出**：下一次需要服务时重新拉起（新端口、新 token），按上表换会话。退避 1、4、16 秒；一个 rise 进程的生命周期内至多重启 3 次，第 4 次起判永久失败。已提交写入在重启之后都在（`S:tests/python/test_acceptance_service.py:429-466`）；在途调用丢了，按瞬态重跑。在途的 `snapshot` 不会写进记录，留下的是孤儿 held 快照（4.2）；在途的 `fork` 按 5.2 先查后建。

### 6.5 停止

`Assembly.close` 逆序调收尾函数（`src/factory/wire.py:97-111`）。服务的收尾最先登记、最后执行：

1. 运行时与端口先关（已登记的收尾）。
2. 当前与尚未关掉的旧 `Client` 逐个 `close()`：停心跳、断开会话、释放句柄（`client.py:201-218`）。
3. 拉起模式：SIGTERM，等至多 40 秒（服务 drain 30 秒，`S:python/starling/service/serve.py:17-18`，再加 10 秒余量），仍没退出就 SIGKILL；删 `service.pid`，最后释放归属锁。2026-11-09 实测 SIGTERM 退出码 0。rise 自己被 SIGKILL 时子进程留下，下次启动按 6.2 核对后停掉它。

## 7. 世界设置与本机记忆配置

### 7.1 rise 设置

`open` 比较整份设置（两边补齐默认值），不同就抛 `world_settings_mismatch`（答复 `:114`；`S:python/starling/dashboard/world_manager.py:577-580`）。rise 在服务会话建好后算一次 rise 设置，之后所有建世界、分叉、开句柄都传同一份。

`info()["default_settings"]` 的顶层键只有 `background`、`chat`、`extraction`、`gist_thresholds`、`source_lane`、`vector_backend`，没有 `embedding` 与 `consolidation`（2026-11-09 实测）。所以 rise 不在默认值上逐项覆盖，而是自己写出全部顶层键：

```python
settings = {
    "extraction": {"provider": "openai", "provider_name": "", "base_url": ..., "model": ...,
                   "dim": 0, "max_tokens": 0, "timeout_ms": ..., "max_retries": ...,
                   "thinking_type": ""},
    "embedding": {"provider": "openai", "provider_name": "", "base_url": ..., "model": ...,
                  "dim": ..., "max_tokens": 0, "timeout_ms": ..., "max_retries": ...,
                  "thinking_type": ""},
    "chat": None, "consolidation": None, "gist_thresholds": None,
    "vector_backend": "sqlite", "source_lane": {"enabled": False},
    "background": {"tick_interval_s": 0, "ingest": False,
                   "auto_snapshot": {"on_session": False, "interval_hours": 0}},
}
```

| 项 | 取值 | 理由与依据 |
|---|---|---|
| 抽取、嵌入端点 | rise 写出整段，`provider_name` 为空，`max_tokens`、`dim`（抽取）写 0、`thinking_type` 写空串 | 端点是记忆运行时身份的一部分，rise 自己定才钉得住。建世界与分叉都在默认世界或来源的设置上按叶子合并（`S:src/governance/registry_db.cpp:527-529`、`S:src/governance/world_settings.cpp:56-101`），rise 没写的叶子会继承别人的值：留着 `provider_name` 时密钥改取那个 provider 的（`S:python/starling/dashboard/engine.py:271-283`）。`enable_thinking` 合并删不掉：`default_settings` 的抽取段里有它时，rise 照抄同一个值进 rise 设置，否则建出的世界存着它而 rise 设置没有，第一次 `open` 就报 `world_settings_mismatch` |
| `chat`、`consolidation` | `None` | rise 不用。`None` 删掉该端点（`S:src/governance/world_settings.cpp:92-103`）；`consolidation` 在默认设置里本来就没有，写 `None` 与不写等价，写出来是为了不依赖默认世界 |
| `gist_thresholds` | `None`，即 C++ 默认阈值 | 同上（`S:src/governance/world_settings.cpp:193-197`） |
| 来源 lane | 关 | 进程内 `Memory` 没有它；Starling 的门面一致性验收为此关掉它（`S:tests/python/test_acceptance_service.py:74-88`）。不关就改了召回语义，违反非目标。其余子键取 C++ 默认（`S:include/starling/governance/world_settings.hpp:40-49`），关着时不生效 |
| 后台 | 全关 | 默认世界的后台开着（2026-11-09 实测：tick 30 秒、摄入开、自动快照 24 小时），不关的话部署世界会自己写，摘要漂移（答复 `:116`）。种子与只读世界由 C++ 强制全关（`S:src/governance/world_settings.cpp:339-351`），可回收世界从不跑后台（`S:python/starling/dashboard/world_manager.py:121`）。三类世界用同一份设置，比较才对得上 |
| 嵌入 | rise 调 `embed_pending` 驱动，时刻取事件时钟 | 与今天相同（`src/simple_actio/memory_starling.py:122-129`） |

用这份设置建世界、分叉、开句柄都成功（2026-11-09 实测）。

**装配期核对**：对配置里每个情境已存在的部署世界，带 rise 设置开一次只读句柄再关上。设置不同在这里就抛 `world_settings_mismatch`，装配期退出码 2，文字写出世界名。开句柄时的设置比较在只读分支之前（`S:python/starling/dashboard/world_manager.py:577-584`），所以只读打开也会比。

### 7.2 密钥

| 模式 | 检查 |
|---|---|
| 拉起 | 装配期查 rise 自己的 `OPENAI_API_KEY` 非空（`src/factory/memory_config.py:191-194`），传进子进程环境 |
| 外部 | rise 查不到。`key_set` 只按默认世界的各角色报（`S:python/starling/dashboard/routes/client.py:265-279`），不覆盖 rise 显式给的端点。服务环境没有密钥时，首次抽取、嵌入或召回抛 `endpoint_key_missing`（答复 `:118`），按永久失败处理。看板服务的进程环境带不带密钥列入第 17 节 |

### 7.3 本机记忆配置的键

今天的键在 `src/factory/memory_config.py:44-48`。没有废弃的键。

| 键 | 今天 | 改后 |
|---|---|---|
| `root` | 必填，库根目录 | 必填。放世界锁、在途记录、`.owner`；拉起模式下另放服务的数据目录与日志 |
| `extraction_base_url`、`extraction_model` | 必填 | 必填，进 rise 设置的 `extraction` |
| `embedding_base_url`、`embedding_model`、`embedding_dim` | 必填 | 必填，进 rise 设置的 `embedding` |
| `timeout_ms`、`max_retries` | 可选，乘积不超过 4000 毫秒（`memory_config.py:179-183`） | 不变，两个端点都用 |
| `url` | 无 | 新增，外部模式必填，例如 `http://127.0.0.1:8787`；必须是回环地址的 http URL |
| `token_file` | 无 | 新增，与 `url` 同给同不给：绝对路径、仓库外、0600，与配置文件同一套检查（`memory_config.py:69-79`）。内容是看板服务的 token（6.1） |
| `world_prefix` | 无 | 新增，可选，缺省 `rise`，格式见 5.1 |

`url` 限回环地址：本期不换机器（服务设计 `:27`），客户端对回环地址不走代理，bearer token 不经过代理（答复 `:82`）。客户端请求超时取 30 秒（`connect` 的缺省，`S:clients/python/starling_client/client.py:29`）。服务端一次 `remember_event` 至多 3 遍抽取，每遍至多 4 秒，远低于 30 秒，所以客户端超时只在服务卡住时出现。外层时限（`memory_config.py:37-42`）不用改。

### 7.4 装配顺序

`memory_wiring`（`src/factory/memory_runtime.py:715-767`）的检查改为，按序：

1. 读配置（第 7.3 节的键）。
2. 安装来源：`starling-client` 已装、不是 editable、`direct_url.json` 有 `vcs_info.commit_id`（沿用 `memory_config.py:280-321` 的取法，换包名）。拉起模式下 `starling-memory` 也要满足，且两个提交相同。今天接受的 `record:` 来源不再接受：没有提交就无法与 `info()["commit"]` 比对。
3. 拉起模式下 `OPENAI_API_KEY` 非空。
4. 认领记忆根；旧布局检查（第 7.6 节）。
5. 拉起或连接服务。`ProtocolMismatch`、`Unauthorized` 在这里抛（`client.py:29-35`；`S:clients/python/starling_client/errors.py:51-54`）。
6. `info()["commit"]` 等于第 2 步的提交，不等就拒绝（答复 `:69`）。两种模式都查；外部模式下文字写出两个提交，提示同步看板服务的源码树或 rise 的钉住版本。
7. 算 rise 设置与运行时身份（7.5）；`ensure_world` 空种子；核对已有部署世界的设置。

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
- `world_settings` 放进 `extraction` 段而不另立顶层键：顶层键一变，替身身份也得跟着变。客户端读不到世界存着的设置（`create_world` 与 `list_worlds` 的项里没有，`S:python/starling/dashboard/routes/client.py:311-328`），所以身份摘 rise 设置；每次 `open` 都带 rise 设置比较，世界实际存的与它不同就报错（7.1），身份因此描述得了实际行为。
- 安装来源（`memory_config.py:280-321`）不再进身份，只在装配期与 `info()["commit"]` 比对（7.4 第 2、6 步）。

### 7.6 旧布局

记忆根下有以下任一项就拒绝启动，文字列出至多 5 条（沿用 `check_memory_layout`，`src/factory/memory_runtime.py:121-157`）：`deploy/*.db`、`runs/`、`seeds/`、`snapshots/*/` 下的 `.db` 文件、`.world` 旁注、不带 `.rec` 的 `deploy-*.json`（替身旧快照）。`deploy/` 下替身的 `<scenario_id>.json` 世界文件与 `.lock` 世界锁照旧合法。`legacy_entries` 的这几条规则取代今天按 `suffix` 的判法（`src/mindagent/memory_layout.py:147-158`）。

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
| `world_held`（开句柄时，排空窗口内） | `WorldError` | `MemoryFailureError` | 是（改判） | 延后或重跑 |
| `world_held`（开句柄时，其余情形） | `WorldError` | `MemoryFailureError` | 否 | T2 或 T3，原因写 world is held by another session |
| `world_held`（恢复时） | `WorldError` | `MemoryFailureError` | 否 | `RestoreFailedError` |
| `endpoint_key_missing`、`endpoint_changed`、`llm_not_configured` | `WorldError` | `MemoryFailureError` | 否 | T2 或 T3 |
| `world_settings_mismatch` | `WorldError` | 装配期 `FactoryConfigError`；运行中 `MemoryFailureError` | 否 | 退出码 2 |
| `world_schema_behind`、`world_read_only`、`fork_digest_mismatch`、`restore_digest_mismatch`、`snapshot_not_found` | `WorldError` 或 `ServiceError` | `MemoryFailureError` | 否 | T2 或 T3 |
| `world_not_found`、`world_exists`、`invalid_*` | `WorldError` | `MemoryFailureError` | 否 | 程序错误；`world_exists` 只在 4.4 并发建时被吃掉 |
| `unauthorized`、`protocol_mismatch` | `Unauthorized`、`ProtocolMismatch` | 装配期 `FactoryConfigError` | 否 | 退出码 2 |
| 其他 | 按类 | `MemoryFailureError` | 取 `exc.transient` | 按瞬态与否 |

码与瞬态的原值来自 C++ 错误码目录（`S:src/governance/error_catalog.cpp:14-67`）。rise 改判的只有两处：

- `session_expired` 原值非瞬态（`:35`）。rise 会重连（6.4），重连后的下一次尝试与过期无关。
- 开句柄时的 `world_held` 原值非瞬态（`:19`）。过期会话还没结束的调用仍占着写入（`S:include/starling/governance/session_table.hpp:19-22`），重连后紧接着开句柄会撞上它。所以只在**排空窗口**内判瞬态：本进程有一个旧会话已过期，过期时它在这个世界上有可写句柄（6.4 记下的），且距过期不到 60 秒（服务端排空 30 秒，加 30 秒余量）。其余一律永久：别的进程或人长期占着时，立刻暴露，不靠连续延后。

瞬态与否往下的走法不变：瞬态 → 尝试重跑或 `DeferredError`；永久 → 训练指令 T2，部署轮次经 `_memory_op` 判 T3（`src/mindagent/deployment.py:142-155`）。

## 9. 依赖与打包

| 项 | 今天 | 改后 |
|---|---|---|
| extra `memory` | `starling-memory @ git+https://github.com/ruijieguo/starling@730e415…`（`pyproject.toml:15`） | `starling-client @ git+https://github.com/ruijieguo/starling@<提交>#subdirectory=clients/python` 与 `starling-memory[dashboard] @ git+https://github.com/ruijieguo/starling@<提交>`，同一个提交 |
| 客户端的依赖 | 无 | 只有 `httpx>=0.27`（`S:clients/python/pyproject.toml:10`） |
| 服务端 | 无 | `starling-memory` 带 C++ 扩展与 `starling-serve` 入口（`S:pyproject.toml:14-15`）；服务依赖 FastAPI 与 uvicorn，在 extra `dashboard` 里（`S:pyproject.toml:19-24`） |
| 两者同一提交 | 无 | 一条测试读 `pyproject.toml`，断言两行的提交相同；运行期由 7.4 第 2、6 步核对 |
| `src/` 里导入 | `simple_actio/memory_starling.py` 导入 `starling` | 改为导入 `starling_client`；全仓库不再导入 `starling` |

客户端包不含服务（`S:pyproject.toml:28` 只打包 `python/starling`）。外部模式只用到客户端那一行；拉起模式与测试两行都要。用户已定一个 extra 两行都装（第 1 节）：两行钉同一个提交，一处改。

`StarlingEndpoints`（`memory_starling.py:53-76`）不再造 C++ 适配器，改成只装端点字段，供 rise 设置与运行时身份用。

## 10. 关键决策

| 编号 | 决策 | 备选 | 理由 | 代价 |
|---|---|---|---|---|
| D1 | 一个 rise 进程同时只用一个服务会话；重连时换新会话，旧会话等端口关完再关 | 部署与训练各一个会话；重连时立即关旧会话 | 写占用按会话算，两个会话会互相 `world_held`；立即关旧会话，别的线程上的旧端口会撞上已关的 transport | 要给端口记代号、按代号计数 |
| D2 | 快照 id 记在记忆根下的在途记录里，快照 `note` 写 episode id | 记进轮次产物；向服务列快照 | 客户端没有列快照的接口；在途记录沿用今天的路径与提交点，MindAgent 的流程不改 | 崩溃会留下孤儿 held 快照，由人在看板里按 `note` 删（用户已定） |
| D3 | 提交时先删记录、后删快照 | 先删快照 | 先删快照再崩，重入会用不存在的快照恢复一条已提交的 episode | 同 D2 |
| D4 | rollout 世界名带随机后缀，标签带 `attempt`；`world_exists` 换后缀，超时先查后建 | 用重试序号；先删后建 | 客户端不能删世界；序号在进程重启后会撞上旧世界；`fork` 没有幂等键 | 世界名不能由 episode id 推出，靠标签找 |
| D5 | 无种子时从 rise 建的空种子分叉 | 每条 rollout `create_world` | `create_world` 建不出可回收世界，第 6 期收不走 | 多一个常驻的种子世界 |
| D6 | rise 显式给全部会影响记忆的设置，关来源 lane | 继承服务默认世界 | 身份要描述实际行为；进程内没有来源 lane；外部服务的默认世界不归 rise 管，有人会改 | 看板对话页在 rise 的世界里不可用（`chat` 被删） |
| D7 | `wait_closed` 保留，服务端检查兜底 | 只靠服务端的 `world_held` | 对手位端口在另一条线程上收尾，不等就恢复必然被拒 | 进程内计数仍要求组合根只造一个管理面 |
| D8 | `session_expired` 判瞬态；开句柄时的 `world_held` 只在本进程旧会话的 60 秒排空窗口内判瞬态 | 按 C++ 原值判永久；一律判瞬态 | 重连后几秒内必然撞上旧会话的在途调用；窗口外的占用是真冲突，要立刻暴露 | 要记每个过期会话在哪些世界上有可写句柄 |
| D9 | rise 加 `factory seed` 命令，从部署世界分叉种子（用户已定） | 人在看板里分叉并手打标签 | 标签与世界名由 rise 按 5.1 生成，不靠人手写；命令在世界锁内查在途 episode，种子停在已提交状态上 | 多一个子命令；升级种子仍由人在看板做 |
| D12 | 拉起模式的残留服务核对后自动停掉（用户已定） | 拒绝启动、由人停 | rise 被 SIGKILL 后每次都要人手动停服务 | 核对规则要防误杀（6.2），读命令行在 macOS 与 Linux 各一套 |
| D10 | 拉起模式下服务端与客户端装在同一个 venv，用 `sys.executable -m starling.service` 拉起 | 另一个环境里的 `starling-serve` 命令 | 一处钉提交，装配期能核对两个包的提交 | rise 的 venv 仍要编译 Starling 的 C++ |
| D11 | 缺省连用户已在跑的看板服务（用户已定） | 缺省拉起 | 拉起的服务没有页面，人拿不到端口与 token；看板服务有前端 | 看板服务的提交要跟 rise 的钉住版本同步；数据目录与用户别的世界共用 |

## 11. 失败处理

| 情形 | 判定 | 去向 |
|---|---|---|
| 服务起不来、协议或提交不一致（含看板服务没跟上钉住版本）、token 错 | 确定性 | 装配期退出码 2 |
| 拉起模式遇到残留服务 | 六项核对都对上 | 停掉它、重试启动一次（6.2） |
| 残留服务核对不过、归属锁被占、停掉后仍 `data_dir_in_use` | 确定性 | 装配期退出码 2，不杀 |
| `factory seed`：世界锁被占、有在途 episode、已有同 id 种子且没给 `--new-version`、部署世界不存在 | 确定性 | 命令退出码 2，不建种子 |
| 已有部署世界的设置与 rise 设置不同 | 确定性 | 装配期退出码 2，文字写世界名 |
| 服务连不上、请求超时、`world_busy` | 瞬态 | 部署：`DeferredError`；训练：`role_memory_unavailable` 重跑 |
| 会话过期 | 瞬态 | 当前尝试失败重跑；下一次调用前换新会话 |
| 开句柄时 `world_held`，不在排空窗口内 | 确定性 | T2 或 T3，原因写 world is held by another session |
| 同名世界不是 rise 建的（`ensure_world`） | 确定性 | T2 或 T3 |
| 拉起的服务退出 | 瞬态，退避 1、4、16 秒，至多重启 3 次 | 第 4 次起永久失败 |
| 恢复时 `world_busy` | 瞬态 | `DeferredError`，重入先恢复 |
| 恢复时 `world_held`、快照没了、摘要不等 | 确定性 | `RestoreFailedError`，记录与快照保留等人 |
| 分叉出的摘要不等于本 run 记下的起点摘要 | 确定性 | 训练位记忆失败，T2 |
| 种子 schema 落后 | 确定性 | T2，原因写"需要升级种子" |
| 解析器遇到服务瞬态失败 | 瞬态 | `DeferredError`，指令留 pending |
| 外部服务没有 `OPENAI_API_KEY` | 确定性 | 首次调用时 T2 或 T3 |
| `delete_snapshot`、`close_world` 失败；崩溃留下孤儿 held 快照 | 不影响结果 | warning，只报码；孤儿快照由人在看板里删 |
| 记忆根是旧布局 | 确定性 | 装配期退出码 2 |

可观测：rise 的失败记录与日志带 code 与物理世界名；看板服务的世界页按 `repo`、`kind`、`run_id` 等标签过滤（6.1）；拉起模式下 `service.log` 是服务的 stderr。

## 12. 对实验的影响

**判 T3 的 run**：所有记忆后端是 Starling 的 interact 训练 run，续训时都判 T3。

- 原因：续训时派发器重新解出起点摘要与运行时身份，再算 `config_hash`（`src/mindtrainer/dispatch.py:399-401`）。哈希一变，`resume_or_run` 抛 `NeedsHumanError`（`src/mindtrainer/orchestrator.py:203-207`）。
- 从施工第 1 步起就判 T3：第 1 步把进程内 Starling 升到 `cff319d`，库第一次打开就迁到 0039，新增的 `holder_id` 列进摘要，有事件行的库摘要会变（答复 `:32`）；`starling_version` 里的安装来源也变了。
- 第 4 步再变一次：`starling_version` 换成服务报出的版本与提交，`extraction` 多了设置摘要，种子从文件变成服务里的世界。
- 为什么可以接受：旧记忆不迁移，旧 run 的记忆起点已不存在（本机记忆根 11-06 已删）。静默续训会把两套记忆行为拼进同一个实验。

**不受影响**：替身 run。替身的运行时身份是常量（`memory_runtime.py:78-84`），替身摘要的格式不变（3.5），哈希不变。

**部署轮次**：轮次产物的 `memory_runtime` 只记录、不参与复用判据（`src/mindagent/deployment.py:125-137`）。新部署世界从空开始。树里若有旧后端下已提交的部署 episode，它会被复用，它的记忆不会写进新世界。本机旧验证树已于 11-06 删除（第 1 节“旧数据”一行）。

**种子换版本**：`factory seed --new-version` 之后，解析器为这个 `seed_id` 解出新版本的摘要，引用它的 run 续训判 T3（5.6）。

**写入结果**：`remember_event` 的 `outcome` 是 `no_store` 或 `rejected` 时，今天当成功、记零条语句；改后判永久失败（3.2），部署轮次判 T3、训练 rollout 判 T2。rise 的事件会不会得到这两个值没有核实（第 17 节）。

**堆积**：每个训练步分叉 `groups_per_step × group_size` 个 rollout 世界，每个部署轮次每个情境一个重放世界。第 6 期合并前它们都留在服务的数据目录里。rise 不对可回收世界拍快照，所以第 6 期“带 held 快照的世界不删”这一条（服务设计 `:222`）挡不住它们。

## 13. 测试

| 层 | 内容 |
|---|---|
| 契约（部署管理面） | 两个后端各过一遍：同 key 两主体各写一次；按主体放弃，用错主体判永久；`pending_events` 带主体；快照、写、恢复后摘要等于快照时；快照带 `note`；端口开着时恢复被拒；只读端口写入被拒；`fork_replay` 的摘要等于快照；同名世界标签不对时 `ensure_world` 判永久 |
| 契约（训练世界） | 两个后端各过一遍：同 rollout 两主体互不召回；同组两条 rollout 起点摘要相同、互不可见；`bind` 给的摘要与分叉结果不等时判永久；同一 episode 重试换新世界、`attempt` 递增；`fork` 回 `world_exists` 时换后缀；`close_world` 失败只记 warning |
| 适配器 | 召回显式传 `strict`、`include_volatile`；`outcome` 为 `no_store`、`rejected` 时判永久（今天判成功）；关闭时 `SessionExpired` 与代号过期当作已关；错误码表逐行 |
| 服务进程 | 拉起、读端口、等 ready、SIGTERM 退出码 0；遗留的 token 文件不挡下次启动；子进程被杀后按退避重启；token 不出现在命令行、日志、异常里 |
| 残留服务 | 拉起后 SIGKILL 掉 rise 进程，再启动时核对通过、停掉残留、启动成功。以下各一条，都拒绝启动且残留进程还活着：pid 文件缺失；pid 指向一个无关进程（模拟 pid 复用）；命令行的 `--data-dir` 不同；归属锁被另一个进程持有。另一条：残留已退出、`D/.lock` 没人持，直接启动、不发信号。读命令行在 macOS 与 Linux 各测一遍 |
| `factory seed` | 外部与拉起两种模式各跑一遍：建 v1，世界名、标签、摘要等于部署世界；再跑不带 `--new-version` 被拒；带了建 v2，解析器取 v2；`--snapshot` 从看板快照分叉；世界锁被占、有在途记录、部署世界不存在、替身，各一条退出码 2；`bind` 钉住解析时那一版，中途建 v3 不影响在跑的 run |
| 会话 | 一个会话里训练位与对手位同写一个世界；会话过期后换新会话，旧端口在别的线程上调用照样抛 `SessionExpired` 而不是连接已关；旧会话的端口关完后旧 `Client` 才关；排空窗口内的 `world_held` 判瞬态，窗口外判永久 |
| 设置 | 已有部署世界的设置被改后，装配期退出码 2；默认世界开着后台时，部署世界摘要静置不变 |
| 装配 | 客户端与服务提交不一致、协议不一致、editable 安装、旧布局（含不带 `.rec` 的 `deploy-*.json`），各一条退出码 2；`url` 与 `token_file` 只给一个时退出码 2；没设 `RISE_MEMORY_CONFIG` 时不导入 `starling_client` |
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
| 1 | 钉住的提交升到 `cff319d`，仍进程内；端口的 `abandon_event` 带主体、`pending_events` 返回 `PendingKey`；替身按 `(holder, key)`；调用点见第 15 节。从这一步起 Starling run 续训判 T3（第 12 节） | 进程内，按 holder 开连接放弃 |
| 2 | 端口改按快照 id，加 `fork_replay`、`release_replay`、只读 `open`；`MemoryLayout` 改读 `.rec.json`；部署驱动器、回滚、验证、门② 跟着改；`scratch_store_factory` 整条链删掉（4.5）；替身实现 | 进程内管理面用按 id 命名的备份文件实现同一接口（第 4 步删掉） |
| 3 | 新模块：服务拉起与停止、`ServiceSession`（代号、引用计数、排空窗口）、rise 设置、`ensure_world`、错误映射、运行时身份；带真 `starling-serve` 的测试；不接线 | 不变 |
| 4 | 接线：`StarlingMemory`、部署管理面、训练世界、解析器改走客户端；配置新键；装配顺序；旧布局；残留服务的核对与停止；extra `memory` 换成两行；删进程内代码 | 改走服务 |
| 5 | `factory seed` 子命令与 `src/factory/seed.py`；`bind` 按摘要钉住种子版本 | 新增 |

回退：第 4 步是一次提交，回退它就回到进程内 `cff319d`。第 1 到 3 步不改变 Starling 路径的外部行为。没装 extra `memory`、没设 `RISE_MEMORY_CONFIG` 时：`memory_wiring` 直接返回替身（`memory_runtime.py:724-730`），不读配置、不导入客户端、不拉起服务。替身只在第 1、2 步改了接口形状，由契约测试钉住与 Starling 同语义。

## 15. 调用点迁移

按 `grep -rn 'pending_events\|abandon_event\|backup_to\|restore_from\|digest_of\|wait_closed\|scratch_store_factory\|library_digest\|copy_library\|empty_digest\|write_world_tag\|world_of' src tests` 核过。`tests` 下 16 个文件命中，随所属模块的步骤一起改。

| 位置 | 今天 | 改成 | 步 |
|---|---|---|---|
| `src/simple_actio/memory_port.py:97-135` | `DeploymentMemoryStore` 有 `backup_to`、`restore_from`，`abandon_event(world, key)`，`pending_events` 返回 `list[str]` | 换成 3.4、4.1、4.5 的签名；加 `PendingKey`、`SnapshotRef` | 1、2 |
| `src/simple_actio/memory.py:461-603` | `FileTupleStore` 按 key 去重，`backup_to`、`restore_from` 写读整份账本 | 3.5 的 `(holder, key)`；4.1 的 held 快照；4.5 的 `fork_replay`、只读 `open` | 1、2 |
| `src/simple_actio/memory_starling.py` | 进程内 `StarlingMemory` 与 `StarlingDeploymentStore` | 第 1、2 步按新接口改；第 4 步改为包 `starling_client` | 1、2、4 |
| `src/mindagent/admit.py:200-210` | `abandon_event(sid, key)` | `abandon_event(sid, pid, key)` | 1 |
| `src/mindagent/deployment.py:454-459` | 按前缀过滤 key 再放弃 | 按 `PendingKey.key` 过滤，`abandon_event(world, p.holder, p.key)` | 1 |
| `src/mindagent/deployment.py:392-396` | 建快照目录、写 `.world`、`backup_to` | `snapshot(world, note=...)`，写 `.rec.json` | 2 |
| `src/mindagent/deployment.py:166-181`、`:461` | `_restore` 调 `restore_from(world, snap)`；提交时删快照与旁注 | `restore(world, rec.snapshot_id)`，按 4.3 分码；提交按 4.2 先删记录后 `delete_snapshot` | 2 |
| `src/mindagent/rounds.py:337-372` | 回滚读旁注找世界，`restore_from`，删快照与旁注 | 读记录找世界，`restore`，删记录，`delete_snapshot` | 2 |
| `src/mindagent/rounds.py:463-506`、`:294`、`:303` | 验证快照 `backup_to`，临时 store `restore_from` | 4.5 | 2 |
| `src/mindagent/memory_layout.py:56-158` | 快照文件与 `.world` 旁注 | 4.2、7.6 | 2 |
| `src/factory/handlers.py:113-123` | 传 `scratch_store_factory` | 删这个参数 | 2 |
| `src/factory/agent.py:360-364` | 部署对手位 `deploy.open(scenario.id, persona_id)` | 签名不变；Starling 时 `open` 内部 `ensure_world` 再开句柄 | 4 |
| `src/factory/wire.py:195-205`、`:447-459` | 把 `memory.deployment.store`、`memory.training_worlds` 注入运行时 | 不变；`memory_wiring` 另返回 `ServiceSession` 的收尾函数，`Assembly.closers` 最先登记它（6.5） | 4 |
| `src/factory/interact_policy.py:393` | `training_worlds.bind(run_id, seed)` | `bind(run_id, seed, cfg.memory["digests"])`（5.2） | 4 |
| `src/factory/memory_runtime.py` | 解析器、`StarlingTrainingWorlds`、`memory_wiring`、`check_memory_layout` | 5.2、5.5、7.4、7.6；`StarlingLayout` 的 `seed_dir`、`seed`、`rollout_world`、`runs_dir` 删掉 | 2、4 |
| `src/mindtrainer/dispatch.py:287-290` | 解析器的一切异常译成 `DirectiveFailedError` | `DeferredError` 原样上抛（5.5） | 4 |
| `src/factory/cli.py:56-86`、`:105-162` | 子命令 `run`、`reconcile`、`install-defaults`、`import-sombench`、`trace`、`cost`、`logs` | 加 `seed`（5.6），实现在新模块 `src/factory/seed.py` | 5 |

## 16. 需用户拍板

已全部拍板，见第 1 节。

## 17. 未核实项

| 项 | 怎么核实 |
|---|---|
| 看板服务（8787）的进程环境里有没有 `OPENAI_API_KEY`，它的 `info()["commit"]` 是什么 | 用 `token_file` 连上，调 `info()`；建一个 rise 设置的空世界、写一条事件，看是否报 `endpoint_key_missing` |
| 从 git URL 安装（uv）时 `info()["commit"]` 是否为 `unknown`。它来自构建时的 `git -C <源码目录> rev-parse HEAD`（`S:cmake/StarlingBuildInfo.cmake:1-20`）；2026-11-09 在源码树内构建时是完整的 `cff319d…` | 按第 9 节的两行装一次，起服务比对；是 `unknown` 时拉起模式改为只比客户端提交 |
| uv 能否锁住同一仓库、同一提交的两个 git 依赖（一个带 `subdirectory`），`httpx2` 能否从 PyPI 装上，C++ 编译要多久 | 改 `pyproject.toml` 后跑 `uv lock` 与 `uv sync --extra memory`，计时 |
| rise 的事件会不会得到 `no_store` 或 `rejected`（3.2） | 部署与训练各跑一轮真实对话，统计 `outcome` |
| rollout 世界与重放世界的堆积速度，以及数据目录的增长（含孤儿 held 快照与恢复留下的 `pre-rollback` 归档） | 跑一个真实训练 run 与一个部署轮次，按标签数世界、量 `~/.starling/worlds` 的大小，每天记一次，直到第 6 期合并 |
| 关掉来源 lane 后，服务的召回与摘要是否逐项等于进程内 `Memory` | 照 Starling 验收第 6 条的写法，用 rise 设置在服务与进程内各跑一遍，比较 |
| 看板正在看某个 rollout 世界时，`close_world` 会不会阻塞到 30 秒 | 看板打开一个世界的页面，同时调 `close_world`，计时 |
| 本机睡眠唤醒后，服务会话是否过期 | 开一个会话，合盖 5 分钟后调用一次 |

已知限制：崩溃留下的孤儿 held 快照只能在看板的快照列表里按 `note` 人工删（4.2），第 6 期也不会清；拉起模式没有页面（6.1）。
