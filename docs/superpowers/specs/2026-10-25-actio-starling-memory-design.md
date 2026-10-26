# 设计二：每个角色位都是带记忆的智能体，记忆用 Starling

状态：修订稿第三版，待审阅。日期：2026-10-26（第一稿 2026-10-25）。
基线：rise `705236c`，Starling `48e9f3b`（`ruijieguo/starling` main，公开仓库，Apache-2.0）。
权威文档集是 `docs/design/`。本稿审定后并入 00、20、30、40、50、90 六篇，然后写施工计划。

## 0. 这份文档做什么

场景二（实时交互训练）里，**每个角色位都是一个智能体**：一个 Actio 实例包着一个底座模型，带技能、经验和记忆。

- 训练位智能体的底座是正在训练的模型，由 MindTrainer 训练。
- 对手位智能体的底座按驱动来：固定对话、站内 vLLM、外部 OpenAI 兼容 API。
- 记忆是个体的。每个人格（persona）一份第一人称记忆，存在 Starling 里。
- 记忆让对手位能准确扮演某种人格的个体：它记得发生过的事，形成信念，并据此回应。

MindAgent 是另一条线：底座不动参数，演化技能库与记忆库。它与训练没有关系，两条线共用同一套 Actio 与记忆端口。

本稿管五件事：

1. Actio 抽出记忆端口，Starling 是生产实现，现有 `MemoryEntry` 是替身；两种实现过同一套契约测试。
2. 训练路的所有角色位都换成 Actio 实例，每轮读记忆、写记忆。
3. 记忆参与训练时可复现：起点记忆与记忆运行时的身份一起钉进 `config_hash`，每条 rollout 从同一起点出发。
4. 会话资源有身份、有归属、有序关闭：每条会话一个 `rollout_id`，所有角色位的记忆与后端由一个对象持有，终态时先停调用、再等线程、再关库、再删文件。
5. Starling 补齐本仓库需要的公开接口（经 PR 合入 Starling）。

不做：

- 对 Actio-RSI（`ChaunceyKung/Actio-RSI-infra`）的兼容。
- 把记忆库放进 git 产物树。
- 跨租户共享记忆。
- 把 Starling 的语义检索用于诊断评测。
- 诊断评测的动态轨与批量数据集生产（`simulator.dataset`）换成智能体。两者仍用裸后端，单列为后续工作。
- 迁移旧的 `memory_library/`：它现在为空（MindAgent 从不生成记忆提案），施工前检查为空，非空则装配期拒绝启动并要求人工处理。

## 1. 事实依据

### 1.1 本仓库（`705236c`）

| 编号 | 事实 | 位置 |
|---|---|---|
| F1 | Actio 的记忆是内存元组 `MemoryEntry` 加词面检索；`decide` 每次调 `retrieve(self.memories, …)` | `src/actio/memory.py`、`src/actio/instance.py:108-113` |
| F2 | 生产代码里 Actio 实例只在 MindAgent 构造：部署轮次与验证重放。`instantiate` 是公开函数，测试也直接调 | `src/mindagent/rounds.py:244` |
| F3 | 训练位是裸的 `VllmPolicy`，只有 `respond`；轨迹源只解包二元组，`submit_turn(utterance, thought)` 不带激活轨迹 | `src/factory/vllm_policy.py:31-60`、`src/mindtrainer/trajectory_source.py:258-262` |
| F4 | 对手位是裸后端。`AgentRuntime._backends` 每个会话调一次工厂；scripted 与 vllm 每次新造，但 `external_api` 分支返回同一个 `ExternalApiBackend` 实例（它无状态，所以现在无害） | `src/factory/agent.py:200-224`、`src/factory/interact_policy.py:255-265` |
| F5 | `as_guided` 给只有 `respond` 的后端套 `IgnoreGuidance`，指导语被丢弃 | `src/actio/instance.py:60-67` |
| F6 | 服务端给每个对手后端套 `TimeoutBackend`（默认 60 秒）：每次调用起一个 daemon 线程，超时后线程继续跑到自然结束；运行器接受二元组或三元组 | `src/simulator/realtime/backend_timeout.py:29-72`、`src/simulator/realtime/servicer.py:286-288`、`src/simulator/runner.py:21-33` |
| F7 | 服务端下发给客户端的对手轮次抹掉 `inner_thought` 与 `activation_trace`；落盘的 episode 保留完整记录 | `src/simulator/realtime/servicer.py:312-316` |
| F8 | 会话正常结束触发 `on_session_closed(episode)`；中途断开走 `request_abort`，驱动器发 `failed` 事件，不触发钩子 | `src/simulator/realtime/servicer.py:109,327`、`src/simulator/realtime/driver.py:82-91` |
| F9 | 激活轨迹校验要求 `memory_hits` 每项以 `mem-` 开头、不重复；键集合固定为八个 | `src/actio/trace.py:13-16,79` |
| F10 | `simulator` 不导入 `actio`，已成立 | `grep` 为零 |
| F11 | MindAgent 的提案生成器只产 `kind: skill`；`admit` 接受任何来源的 `kind: memory` 提案并写 `memory_library/` | `src/mindagent/proposals.py:6`、`src/mindagent/admit.py:41-43` |
| F12 | 后端工厂的签名是 `(Scenario, RoleAssignment, personas, client_slot)`，没有会话身份；`AgentRuntime` 的登记表按情境 id 索引 | `src/simulator/realtime/servicer.py:27-29`、`src/factory/agent.py:138,161-169` |
| F13 | 续训时 `config_hash` 不同即升 `NeedsHumanError`（T3） | `src/mindtrainer/orchestrator.py:183-187` |
| F14 | 部署 episode 复用的判据：智能体位的每个轮次都带着与当前相同的 `library_version` | `src/mindagent/deployment.py:70-78` |
| F15 | 树外本机配置的现成做法：路径由环境变量给，必须在仓库外且权限 0600，报错不回显取值。记忆配置照此另写一份 | `src/factory/eval_config.py` |
| F16 | `Episode` 只有 `id`、`scenario_id`、`role_assignment`、`turns`、`terminated_reason`，没有元数据字段 | `src/simulator/datamodel.py:203-219` |
| F17 | 时间限制有四个：客户端每轮等待 `PER_TURN_DEADLINE_S` 30 秒、服务端后端调用 60 秒、训练位策略 HTTP 120 秒、对手 HTTP 25 秒 | `src/factory/interact_policy.py:70-71`、`src/simulator/realtime/servicer.py:65`、`src/factory/vllm_policy.py:28` |
| F18 | 驱动器把后端异常分四类：超时、不可用、拒绝各有专门的终止原因，其余一律 `role_backend_failure`；轨迹源对它不重跑，策略上下文把它译成延后 | `src/simulator/realtime/driver.py:140-163`、`src/factory/interact_policy.py:338-349` |
| F19 | 分析器与 `_reusable` 都只读智能体位的轨迹，对手位的轨迹不参与 | `src/mindagent/analysis.py:101-108`、`src/mindagent/deployment.py:73` |
| F20 | 激活轨迹里不能有数字：protobuf `Struct` 把整数存成 double，所以叶子只许 str、None、bool | `src/actio/trace.py:3-4,96-98` |
| F21 | 服务端的 episode id 是 `ep-<session_id>`，每次重试都是新的；逻辑 id（训练 `{run}-{stage}-{scenario}-r{k}`、部署 `deploy-{model}-{scenario}-{k}`）只在客户端 | `src/simulator/realtime/servicer.py:294`、`src/mindagent/deployment.py:66-67` |
| F22 | 客户端没有逐轮超时，只有整条流的 gRPC deadline（`DeadlineStub`，由 `max_turns × PER_TURN_DEADLINE_S` 折算）；服务端对客户端位有逐轮超时 `turn_timeout_s` | `src/factory/agent.py:97-107`、`src/simulator/realtime/backend.py:54-67` |
| F23 | 审核类状态机：`approved` 只能去 `effective`；`rejected` 只能从 `pending_review` 来 | `src/controlplane/states.py:8-15` |

### 1.2 Starling（`48e9f3b`，代码核对加本机探针）

| 编号 | 事实 | 实测或位置 |
|---|---|---|
| S1 | 从源码 `uv pip install` 可装：scikit-build-core 构建，本机一次 179 秒（环境测量，不作预算依据） | 实测 |
| S2 | 公开 `Memory.recall` 不收 `holder`；私有 `MemoryCore.recall` 收 | `python/starling/memory.py:168`、`_memory_core.py:250` |
| S3 | `remember` 要求注入 LLM，否则抛 `LLMNotConfigured`；`make_openai_llm` 的密钥由 C++ 读 `OPENAI_API_KEY` | `_memory_core.py:183-187`、`memory.py:48-70` |
| S4 | `remember` 写入的语句要等嵌入后才能被 `recall` 命中；嵌入由 `tick` 或 `EmbeddingWorker.tick_one_batch(now_iso)` 执行 | 实测；`bindings/python/bind_10_embedding.cpp:132-138` |
| S5 | 门面固定用 8 维 stub 嵌入器；真嵌入器是 `OpenAIEmbeddingAdapter`，密钥同样读 `OPENAI_API_KEY` | `memory.py:147-148`、`src/embedding/openai_embedding_adapter.cpp:35` |
| S6 | `StatementRow` 暴露 `id`（UUID）、`tenant_id`、`holder_id`、`holder_perspective`、`subject_kind`、`subject_id`、`predicate`、`object_kind`、`object_value`、`modality`（小写）、`polarity`、`confidence`、`observed_at`、`valid_from`、`valid_to`、`consolidation_state`、`review_status`、`evidence_json`、`affect_json`、`semantic_claim_json`；没有 `nesting_depth`（表里有该列） | 实测 |
| S7 | `remember` 的幂等只看文本：同文再写返回 `outcome="idempotent"`，不新增原文记录；但 `should_extract` 仍为真，抽取会再跑一次 | 实测；`src/memory/memory_ops.cpp:88` |
| S8 | `recall` 不改语句行 | 实测 |
| S9 | 库文件字节不稳定：每次新建空库字节不同；`close` 只置空连接，不合并 WAL，不等待任何线程 | 实测；`_memory_core.py:475-478` |
| S10 | `VACUUM INTO` 在库打开时可执行，得到自洽的单文件副本；副本可直接 `Memory.open` 且记忆完整。公开的 `backup_to` 尚不存在 | 实测 |
| S11 | 两个 `Memory` 分别开两个文件，互不干扰 | 实测 |
| S12 | `tick` 有 9 个阶段（嵌入、承诺、共同点、回放三段、TTL 清扫、投影、派发），每段受运行时健康门控，负载高时跳过；回放调度用 `random_device` 播种 | `src/memory/memory_ops.cpp:688-742`、`src/replay/replay_scheduler.cpp:48` |
| S13 | `RememberResult` 带 `extraction_failed`、`failure_category`（`timeout`、`transport_failure`、`extraction_failure`、`persistence_failure` 等）、`failure_detail` | `memory.py:98-116`、`memory_ops.cpp:134-144,310-354` |
| S14 | Starling 自己的世界快照用 `VACUUM INTO` 加 `integrity_check`，只服务看板 | `src/governance/world_store.cpp:828` |
| S15 | 向量存在 `statement_vectors` 表：`stmt_id`、`raw_embedding`（float32 blob）、`index_vector`、`dim`、`model`、`status` | `migrations/0016_statement_vectors.sql` |
| S16 | 嵌入适配器用 `base_url + "/embeddings"` 拼请求地址，路径有意义 | `src/embedding/openai_embedding_adapter.cpp:64` |

## 2. 裁决总表

| 编号 | 裁决 | 理由 | 判错的代价 |
|---|---|---|---|
| M1 | 训练路的每个角色位都是 Actio 实例；底座按角色位的后端定 | 用户裁定：训练位与对手都是智能体，对手要靠记忆扮演个体 | 每轮多一次 recall 与 remember，会话变慢 |
| M2 | `simulator` 仍只依赖 `ModelBackend`；新增的是后端工厂的会话身份参数，不是协议方法 | Actio 实例本来就满足 `respond`；会话身份是服务端本来就缺的 | 后端工厂签名改一次 |
| M3 | 记忆在 Actio 内部，经记忆端口访问；只有 `actio/memory_starling.py` 导入 `starling` | 记忆属于个体，不属于环境 | Actio 多一个可选依赖 |
| M4 | 一个智能体一个 Starling 库文件，库的 `agent` 与 holder 都是该人格 | 个体隔离靠文件，不靠 holder 过滤；同时多开文件已实测 | 文件多 |
| M5 | 记忆库在树外，根目录由树外配置给 | 原生 SQLite 带 WAL，不适合当 git 产物 | 不能从产物树追溯，靠摘要补 |
| M6 | 记忆状态的身份是逻辑摘要：对所有影响召回的语句字段加每条语句的向量 blob 算 sha256，与文件字节无关 | 文件字节不稳定（S9）；同模型名不保证同向量，所以向量本身进摘要（S15） | 摘要要读向量表，算得慢一些 |
| M7 | 默认每轮都读写：回复前 recall，对方每句话 remember，之后只做嵌入，不做后台维护 | 用户裁定每轮读写；后台维护有随机性与负载门控（S12），不可复现 | 训练里没有 TTL、回放与巩固 |
| M8 | 训练 run 的起点记忆与记忆运行时身份一起进 `config_hash`；`memory` 字段缺省表示不用记忆，且角色位仍是裸后端、轨迹仍是 v1 | 记忆变了行为就变；换 Starling 或换抽取模型行为也变；旧配置的产物形状也不能变 | 换种子、换模型都要新开 run |
| M9 | 每条 rollout 在起点的私有副本上跑，用完即弃 | 用户裁定：同组 rollout 的起点相同，组内优势只反映策略 | 记忆不在一条 run 内累积 |
| M10 | 每条会话一个 `SessionResources`，持有服务端所有角色位的记忆与后端；终态时先停调用、再等线程、再关库、再删文件。等不到线程退出的资源整个保留并占住一个会话名额，不关不删 | 超时线程还在跑时关库或删库会写坏文件（F6、S9）；daemon 线程杀不掉 | 挂死的后端会永久占用会话名额，直到进程重启 |
| M11 | 部署（MindAgent）的记忆持久、跨轮次累积，用持久的单调事件序号当时钟；每条 episode 记起点摘要；验证重放在快照副本上只读 | 固定时钟在持久库里会倒退；重放要让基线与候选看到同一份记忆 | 重放与线上行为不完全一致 |
| M12 | 抽取与嵌入都走 OpenAI 兼容端点，`base_url` 与模型名写配置；密钥是 Starling 固定读的 `OPENAI_API_KEY` | 用户裁定；Starling 只认这个变量（S3、S5） | 不能为抽取和嵌入配不同的密钥 |
| M13 | Starling 作为可选 extra `memory`，依赖钉 git 提交；未装时用替身并显式报告 | C++ 构建门槛；仓库未上 PyPI | 未装环境只测替身路径 |
| M14 | Starling 缺的公开接口在 Starling 里补，经 PR 合入，本仓库钉合并提交 | 用户裁定；本仓库不碰 Starling 私有接口与表结构 | 依赖 Starling 先合 PR |
| M15 | 记忆异常按 Starling 的 `failure_category` 分两个终止原因：`role_memory_unavailable`（`timeout`、`transport_failure`，T1，按条重跑）与 `role_memory_failure`（其余，T2） | 混进 `role_backend_failure` 会被译成延后反复重试（F18）；抽取端点的网络抖动不该升人工 | 分类依赖 Starling 的类别名，它改名要跟着改 |
| M16 | 指导语合并进唯一的那条 system 消息 | 连续两条 system 消息不是所有兼容端点都接受 | 无 |
| M17 | 替身与 Starling 过同一套契约测试，端口语义与规范样例先定后实现 | 先做替身再接 Starling 会长出两套语义 | 契约测试要先写 |
| M18 | 事件 key 用逻辑 episode id，不用传输层的 session id；部署重试前先把该人格的库回滚到 episode 起点 | 重试会换 session id（F21），部署库会重复记忆 | 回滚要多一次备份与恢复 |

## 3. 分层

```
simulator.datamodel          纯数据模型
   ↑            ↑
actio        simulator       simulator 只认 ModelBackend 与后端工厂
  └ memory_starling → starling（可选 extra）
   ↑
mindtrainer（只认 ModelBackend）· mindagent · factory（组合根，构造 Actio 实例）
```

- `mindtrainer` 不导入 `actio`：轨迹源只知道策略返回二元组或三元组（第 7.3 节）。
- `factory` 构造所有训练路的 Actio 实例：训练位交给轨迹源，对手位交给服务端的后端工厂。
- `mindagent` 继续自己构造部署实例，改为经记忆端口取记忆。

## 4. Actio 的记忆端口

### 4.1 端口

```python
class MemoryBackend(Protocol):
    def recall(self, query: str, *, limit: int) -> list[MemoryHit]: ...
    def remember(self, event: MemoryEvent) -> RememberOutcome: ...
    def embed_pending(self) -> int: ...          # 只做嵌入,返回条数
    def digest(self) -> str: ...                 # 逻辑摘要,sha256 十六进制
    def close(self) -> None: ...
```

| 类型 | 字段 | 说明 |
|---|---|---|
| `MemoryHit` | `id`、`text` | `id` 以 `mem-` 开头（F9），`text` 是渲染好的一句 |
| `MemoryEvent` | `key`、`text`、`interlocutor`、`seq` | `key` 是事件的稳定身份；`seq` 是该记忆库里单调递增的事件序号，决定 `now` |
| `MemoryEventConflict` | 异常 | 同 `key` 已存在且文本不同时抛出 |
| `RememberOutcome` | `stored`、`failed`、`category` | `category` 取 Starling 的 `failure_category`（S13） |

一个端口实例只属于一个智能体，所以签名里没有 holder。

| 实现 | 文件 | 用途 |
|---|---|---|
| `TupleMemory` | `actio/memory.py` | 包现有 `MemoryEntry` 元组与 `retrieve`；`remember` 追加一条 `MemoryEntry`，`subject` 是 `interlocutor` |
| `StarlingMemory` | `actio/memory_starling.py` | 包一个 `starling.Memory`；唯一导入 `starling` 的文件 |

两种实现过同一套契约测试与规范样例（M17）。样例是一份 YAML：固定的事件序列、每步之后的 recall 预期命中 id 顺序、每步之后的 digest、同 key 同文本重写的结果、同 key 不同文本抛 `MemoryEventConflict`、close 后再调抛 `MemoryClosedError`。替身与 Starling 对同一份样例必须给出同样的命中顺序与摘要变化曲线（摘要值本身两边不同）。事件 key 的去重由端口实现自己维护：`StarlingMemory` 在库旁的 `events` 表（第 8 节 PR 新增）里记 `key → 文本 sha256、语句 id 列表`，查表早于 `remember`；`TupleMemory` 用字典。

### 4.2 实例改持端口

| 改动 | 现状 | 改后 |
|---|---|---|
| 字段 | `memories: tuple[MemoryEntry, ...]` | `memory: MemoryBackend`、`memory_write: bool` |
| `decide` | `retrieve(self.memories, q, limit)` | `self.memory.recall(q, limit=self.memory_limit)` |
| `respond` 之后 | 无 | `memory_write` 为真时对新近的对方发言逐条 `remember`，再 `embed_pending` |
| `instantiate` | `memories=` | `memory=`；保留 `memories=` 作为 `TupleMemory` 的简写 |
| `close` | 无 | 转发给 `memory.close()` |

`render_guidance` 改收 `list[MemoryHit]`，记忆行写成 `[memory:<id>] <text>`。

### 4.3 读写时机（M7）

- **读**：每次 `respond` 前，查询词是对方最后一句（现有 `_memory_query`），`limit` 默认 3。
- **写**：每次 `respond` 后，把上次回复之后对方说的每句话按顺序交给 `remember`，然后 `embed_pending` 一次。不调 `tick`：后台维护的随机性与负载门控会让同一起点得到不同状态（S12）。
- **事件身份**：`key` 是 `<逻辑 episode id>#<turn_index>`，`text` 是 `<说话方人格 id>：<utterance>`。逻辑 episode id 由客户端定（F21）：训练是 `{run}-{stage}-{scenario}-r{k}`，部署是 `deploy-{model}-{scenario}-{k}`，重试不变；它经 `CreateSession` 新增的 `episode_id` 字段传给服务端，服务端落盘的 episode 也用它当 id。`key` 作为前缀写进 Starling 的文本：`[<key>] <text>`，所以不同 episode 的同一句话是不同事件；同 key 的重写由 `events` 表拦下，不再调 `remember`。
- **时钟**：`now` 由 `seq` 推出：`2026-01-01T00:00:00Z` 加 `seq` 秒。`seq` 存在 `events` 表所在的同一个 SQLite 库里，与写事件同一事务递增；训练副本从 0 起，部署库跨轮次单调。
- **自己的话**：不写。它已在对话历史里。
- **抽取失败**：`RememberOutcome.failed` 为真时这一轮不留该条记忆，实例计数加一，不抛到 `respond` 之外；计数在激活轨迹里（第 4.6 节）。
- **`recall`、`remember`、`embed_pending` 抛异常**：包成 `MemoryFailureError` 上抛，由调用方按第 9 节分类。

### 4.4 渲染（S6）

`StarlingMemory.recall` 把每个 `{row, score}` 渲染成：

```
[memory:mem-<row.id>] <subject_id> <predicate> <object_value>（<modality>，置信度 <confidence>）
```

否定语句（`polarity` 为负）在谓词前加“不”。`StatementRow` 没有 `nesting_depth`，不渲染嵌套层次。

### 4.5 逻辑摘要（M6）

`digest()` 调 Starling 新增的公开 `Memory.digest()`（第 8 节）。输入是本 tenant 下所有语句行的规范化 JSON，覆盖每一个影响召回候选与排序的字段：

| 进摘要 | 不进摘要 |
|---|---|
| 语句行：`holder_id`、`holder_perspective`、`subject_kind`、`subject_id`、`predicate`、`object_kind`、`object_value`、`canonical_object_hash`、`modality`、`polarity`、`confidence`、`salience`、`observed_at`、`valid_from`、`valid_to`、`nesting_depth`、`consolidation_state`、`review_status`、`supersedes_id` 指向行的规范化 JSON 的 sha256、`affect_json`、`semantic_claim_json`、`evidence_json`；向量行（S15）：`raw_embedding`、`dim`、`model`、`status`；事件表：`key`、文本 sha256 | `id`（UUID）、`created_at`、`updated_at`、`access_count`、`last_accessed`、`activation`、`retry_count`、`last_attempt_at`、`embedded_at`、看板表 |

语句按规范化 JSON 排序，向量按所属语句的位置排，整体算 sha256。空库的摘要是固定值。向量进摘要是因为同一个模型名在外部端点上不保证给出同一向量；向量变了召回就变，摘要必须跟着变。`TupleMemory.digest()` 对条目 `to_dict()` 去掉 `id` 后的排序列表加事件表算同样形状的 sha256。

### 4.6 激活轨迹

轨迹模式升到 `actio.trace.v2`，在八个键之外加 `memory`：`{"digest_before": sha, "remembered": "n", "extraction_failed": "n"}`。两个计数是十进制字符串，与 `library_version` 同一规矩（F20），经 `Struct` 往返不失真。校验：v2 的键集合是九个，`memory` 的三个值都是非空字符串，计数必须是 ASCII 十进制。消费方（分析器、`_reusable`）对 v1 与 v2 都接受；`memory_hits` 的校验不变。抽取失败的计数因此随 episode 落盘，不需要 `Episode` 加元数据（F16）。

## 5. 记忆库的位置与配置

### 5.1 树外配置（M5、M12）

环境变量 `RISMFORGE_MEMORY_CONFIG` 给出 YAML 文件的绝对路径，文件必须在仓库外、权限 0600，校验照 `eval_config.py` 另写一份（F15）。

| 键 | 必填 | 含义 |
|---|---|---|
| `root` | 是 | 记忆库根目录，绝对路径，在仓库外 |
| `extraction_base_url`、`extraction_model` | 是 | 抽取用的 OpenAI 兼容端点与模型 |
| `embedding_base_url`、`embedding_model`、`embedding_dim` | 是 | 嵌入用的端点、模型与维度 |
| `timeout_ms` | 否 | 抽取与嵌入的单次上限，默认 8000 |

配置里不放密钥。启动时检查 `OPENAI_API_KEY` 非空，报错只写变量名。配置不存在时训练路与部署路都用替身，并在启动日志里写 `memory backend: stand-in`。

### 5.2 记忆运行时身份

`MemoryRuntimeIdentity` 是一个字典，所有记录与哈希都用它：

| 键 | 取值 |
|---|---|
| `backend` | `starling` 或 `stand-in` |
| `starling_version` | `starling.__version__` 加安装来源：git 安装取 `direct_url.json` 的 `vcs_info.commit_id`；源码目录安装取已安装包 `RECORD` 里全部文件哈希的 sha256（装配期算一次） |
| `extraction` | `{base_url, model, prompts: Starling ExtractionConfig 的摘要}` |
| `embedding` | `{base_url, model, dim}` |
| `trace_schema` | `actio.trace.v2` |

`base_url` 是规范化后的完整地址（小写 scheme 与主机、去掉末尾斜杠、含路径），不含 userinfo；配置读取时已拒绝带 `user:password@` 的地址。替身时 `extraction` 与 `embedding` 为 `null`。

### 5.3 目录布局

```
<root>/
  deploy/<persona_id>.db                            部署记忆,持久,跨轮次累积(seq 与事件表在库内)
  seeds/<seed_id>/<persona_id>.db                   训练起点种子库,只读
  runs/<run_id>/<session_id>/<slot_id>.db           服务端对手位的私有副本,会话终态即删
  runs/<run_id>/client/<episode_id>/<slot_id>.db    客户端训练位的私有副本,rollout 结束即删
  snapshots/<round_id>/<persona_id>.db              部署轮次的验证快照,轮次结束即删
```

库文件名用人格 id，因为人格才是个体。同一个人格在两个角色位同时出现时，两个位各开一份副本，互不可见。

### 5.4 部署形态

| 形态 | 实例在哪 | 库在哪 |
|---|---|---|
| 现状：全部组件一台机器 | 开发机 | 开发机上 `root` 指的目录 |
| 目标：模拟器在计算层 | GPU 主机 | 同一台主机上 `root` 指的目录 |

库必须和实例同机，否则每轮 recall 都跨机往返。

## 6. 会话资源与生命周期（M10）

### 6.1 会话身份

服务端在 `CreateSession` 时生成 `session_id`（已有，`sess-<12 位>`）。它就是 `rollout_id`，贯穿四处：

| 处 | 改动 |
|---|---|
| `CreateSession` | 新增可选字段 `episode_id`（逻辑 episode id，F21）；给了就用它当落盘 episode 的 id，没给沿用 `ep-<session_id>` |
| 后端工厂 | 签名改为 `(SessionInfo) -> SessionResources`，`SessionInfo` 含 `scenario`、`assignment`、`personas`、`client_slot`、`session_id`、`episode_id` |
| 落盘钩子 | 签名改为 `on_session_closed(session_id, episode)`；`AgentRuntime` 按 `session_id` 查归属 |
| 登记表 | 策略上下文登记“情境 id → run id”只用于判定会话归属；工厂造资源时把 `session_id → run id` 记进第二张表，终态时删除；落盘只查第二张表 |
| 记忆目录 | `runs/<run_id>/<session_id>/<slot_id>.db` |

现有的“情境 id 不得同时属于训练与部署”检查（Ruling 24）保留。落盘不再按情境 id 路由，所以策略上下文注销情境时，仍在跑的会话照样能落到自己的 run。

### 6.2 SessionResources

服务端每条会话一个 `SessionResources`，由后端工厂返回并交给驱动器：

```python
class SessionResources:
    backends: dict[str, ModelBackend]      # 角色位 -> 已套 TimeoutBackend 的后端
    def quiesce(self) -> None: ...         # 拒绝新调用,之后的 respond 抛 SessionClosingError
    def join(self, timeout_s: float) -> bool: ...   # 等所有在途后端调用退出
    def close(self) -> None: ...           # 关每个智能体的记忆,删副本目录
```

- 所有权：工厂造出 `SessionResources` 交给 servicer；servicer 把 `backends` 给驱动器，自己持有资源对象直到流结束。驱动器只调后端，不持有资源。
- `TimeoutBackend` 改为登记在途线程：`respond` 开始时加入 `SessionResources` 的在途集合，结束时移出；超时只是调用方放弃等待，线程本身仍在集合里。
- 驱动器进入终态（`CLOSED` 或 `FAILED`，含断开）后，由 servicer 在流结束的 `finally` 里按顺序调 `quiesce`、`join(timeout_s=60)`、`close`。
- `join` 超时：**不 close、不删**。资源对象移交给运行时的滞留表（`AgentRuntime.lingering`），并继续占住一个会话名额：servicer 的准入检查“活动会话数加滞留数小于 `max_workers`”不过就拒绝 `CreateSession`，终止原因 `role_backend_unavailable`（T1，客户端按条重跑）。滞留表每 60 秒重试一次 `join(0)`，成功后 close 并删目录。
- 清扫：守护进程启动时删掉 `runs/` 下所有目录（启动时没有活动会话、也没有滞留线程）；运行中只删滞留表 join 成功的目录，不扫目录。
- 在途集合的操作持一把锁；`join` 不持锁等待。

### 6.3 并发上限

每条会话最多 `len(bindings) - 1` 个在途后端线程，加驱动器线程与泄流线程各一个。gRPC 的 `max_workers` 限制并发会话数（`server.py:38-46`），滞留的资源也占会话名额（6.2 节），所以进程里的后端线程总数不超过 `max_workers × (len(bindings) + 1)`。代价是挂死的后端会一直占着名额：滞留数达到 `max_workers` 时服务端拒绝所有新会话，守护进程的日志每分钟报一次滞留数，由人重启进程。Python 的 daemon 线程杀不掉，这是能给出的最强保证。

### 6.4 时间预算（F17）

| 预算 | 现值 | 改后 |
|---|---|---|
| 客户端每轮等待（流 deadline 按轮折算） | 30 秒 | 90 秒 |
| 服务端后端调用上限 | 60 秒 | 60 秒 |
| 对手 HTTP 上限 | 25 秒 | 25 秒 |
| 记忆单次调用上限（抽取、嵌入各一次） | 无 | 8 秒 |
| 训练位策略 HTTP 上限 | 120 秒 | 60 秒 |

一轮对手发言的最坏耗时是 recall（本地）加 HTTP 25 秒加 remember 8 秒加嵌入 8 秒，约 41 秒，低于服务端 60 秒；训练位一轮的最坏耗时是 HTTP 60 秒加记忆 16 秒，低于 90 秒。

现在客户端只有整条流的 deadline（F22），没有逐轮限时。改法：客户端收消息的 `_next_msg` 加逐轮等待上限 90 秒（在接收线程里用带超时的队列取消息），超时视作 `timeout`（T1，按条重跑）并 abort 会话；整条流的 deadline 仍按 `max_turns × 90` 折算当兜底。记忆的 8 秒是每次调用的上限：`embed_pending` 一次只嵌入本轮新写的几条，不处理积压，因为训练副本每轮都嵌入、从不积压；部署库若有积压（上次失败留下的），在 episode 开始前先嵌完再开始。

## 7. 训练路

### 7.1 起点记忆与哈希（M8）

`TrainingConfig` 增加可选字段 `memory`：

| 字段 | 含义 |
|---|---|
| `memory.seed` | 种子 id，`null` 表示每个人格都是空库 |
| `memory.digests` | `{persona_id: 起点摘要}`，派发时算出并写回 |
| `memory.runtime` | `MemoryRuntimeIdentity`，派发时写回 |
| `memory.write` | 是否每轮写，默认 `true` |

- `memory` 缺省表示**不用记忆**：角色位仍是现在的裸后端，不套 Actio，轨迹仍是 v1（对手位没有轨迹，训练位不上行轨迹），落盘的 episode 与现在逐字节同形。旧配置的 `config_hash` 不变，语义与产物也不变。只有 `memory` 存在时才走 Actio 路径。
- 训练默认值 `training_defaults.yaml` 的 `interact` 段增加可选键 `memory`：`{seed, write}`。调度器写进配置时 `digests` 与 `runtime` 留空。
- 派发器进入策略上下文之前，按 `seed` 打开每个绑定人格的起点库、算摘要、取运行时身份，写回 `memory.digests` 与 `memory.runtime`。之后才算 `config_hash`。
- 续训时重新算摘要与身份；种子库、Starling 版本、抽取或嵌入模型任一变了，`config_hash` 不同，升 T3（F13）。
- 替身路径的 `digests` 是替身摘要、`runtime.backend` 是 `stand-in`，所以换成真 Starling 也会改哈希。

### 7.2 每条 rollout 的私有副本（M9）

| 时刻 | 动作 |
|---|---|
| 会话开始 | 对每个角色位：从起点库 `backup_to` 一份私有副本（空起点就新建空库），打开它，构造 Actio 实例 |
| 每轮 | 该位实例 recall、回复、remember、embed_pending |
| 会话终态 | `SessionResources` 按 6.2 节收尾 |

- 训练位的实例在客户端（轨迹源一侧），由策略工厂每条 rollout 新造一个，见 7.3。它的副本目录是 `runs/<run_id>/client/<逻辑 episode id>/<trained_slot>.db`，重试时同名目录先删后建。
- 对手位的实例在服务端，由后端工厂每个会话新造。
- 同组 `group_size` 条 rollout 的每个角色位都从同一份记忆出发。

### 7.3 训练位

| 部件 | 改动 |
|---|---|
| `VllmPolicy`、`ExternalApiBackend` | 新增 `respond_guided(persona, scenario, history, slot, guidance)`：指导语非空时追加到唯一的 system 消息末尾，以“当前要点：”起头，一行一条（M16） |
| 策略工厂 | 交给编排器的不再是一个后端，而是 `policy_for_rollout(session_id) -> ContextManager[ModelBackend]`：每条 rollout 造一个 Actio 实例，退出时关实例 |
| `InteractTrajectorySource` | 新参数 `policy_for_rollout(episode_id)`；`_attempt` 在 `create_session` 之前进入上下文（副本目录按逻辑 episode id 命名），`finally` 里退出。旧参数 `policy` 保留，只许在 `cfg.memory` 缺省时用；两者都传或 `memory` 存在却传 `policy`，构造期抛 `ValueError` |
| 提交 | 策略返回三元组时，第三项作为 `activation_trace` 随 `submit_turn` 上行；二元组照旧 |
| 续训 | 不变：`make_sources(start)` 每次构造轨迹源，`policy_for_rollout` 在每条 rollout 内进出，和 `start` 无关 |

`mindtrainer` 不导入 `actio`：它只认 `ModelBackend` 的二元组或三元组。

### 7.4 对手位

| 驱动 | 底座 | 改后的后端 |
|---|---|---|
| `scripted` | `ScriptedBackend` | Actio 实例包 `ScriptedBackend`；记忆照读照写，台词不受影响 |
| `vllm` | `VllmPolicy`（适配器 = `model_ref`） | Actio 实例包 `VllmPolicy`，指导语经 `respond_guided` 进 prompt |
| `external_api` | `ExternalApiBackend` | Actio 实例包它；`ExternalApiBackend` 每个会话新造一个（F4 的共享实例改掉） |

- 对手位的技能与经验在本稿里为空：现阶段只有 MindAgent 产出技能，训练路不读技能库。
- 部署会话（没登记给训练 run）的对手位同样换成 Actio 实例，底座仍是原来的 `ScriptedBackend`，记忆是每会话一份空的 `TupleMemory`。部署里演化的是客户端位的智能体，对手的记忆不跨会话留存。
- `ScriptedBackend` 只有 `respond`，套上 Actio 后指导语被忽略（F5），台词与现在逐字相同；变化只是对手轮次多了激活轨迹，分析器与 `_reusable` 只看智能体位（F19），不受影响。
- 对手的激活轨迹随落盘 episode 保存（F7），不下发给训练位。

### 7.5 训练记录

`training_run.policy_backend` 增加 `memory` 块，内容就是配置里的 `memory`（含 `digests` 与 `runtime`）。读记录的人不必回到配置就知道记忆从哪来。

## 8. Starling 的 PR（M14）

在 `ruijieguo/starling` 开分支，只改 Python 门面与绑定，每项带测试：

| 接口 | 作用 |
|---|---|
| `Memory.recall(..., holder=None)` | 转发到 `MemoryCore.recall` 的 `holder` |
| `Memory.open(..., embedder=None)` | 注入嵌入器；`None` 维持现状（stub） |
| `make_openai_embedder(*, model, base_url, dim, timeout_ms)` | 构造 `OpenAIEmbeddingAdapter`，与 `make_openai_llm` 同形 |
| `Memory.embed_pending(now) -> int` | 只跑嵌入阶段（`EmbeddingWorker.tick_one_batch`），不跑后台维护 |
| `Memory.digest() -> str` | 第 4.5 节的逻辑摘要，覆盖语句行、向量行与事件表 |
| `Memory.backup_to(path)` | `VACUUM INTO` 加 `PRAGMA integrity_check`，得到自洽的单文件副本 |
| `Memory.close()` | 关闭 SQLite 连接并等待自己的工作线程退出 |
| `Memory.remember_event(key, text, *, interlocutor, now) -> RememberResult` | 查库内 `events` 表（新迁移）：同 key 同文本直接返回 `idempotent` 且不抽取；同 key 不同文本抛 `EventConflict`；新 key 则写事件行、递增 `seq`、调 `remember`，三者同一事务 |
| `Memory.next_seq() -> int` | 读 `events` 表的当前 `seq` |

合入后本仓库的 `pyproject.toml` 增加 `memory = ["starling-memory @ git+https://github.com/ruijieguo/starling@<合并提交>"]`。推送分支与开 PR 前先征得用户同意。

## 9. MindAgent（M11）

| 改动 | 内容 |
|---|---|
| 部署记忆 | 每个绑定人格一个 `deploy/<persona_id>.db`，跨轮次持久；`seq` 与事件表在库内 |
| 轮次产物 | `agent_round` 载荷增加 `memory_runtime` 与 `episode_digests: {episode_id: {persona_id: 起点摘要}}`，每条 episode 开始前写回一次（轮次产物本来就逐步回写） |
| episode 起点与重试 | 每条部署 episode 开始前：把该人格的部署库 `backup_to` 到 `snapshots/<round_id>/<episode_id>-<persona_id>.db` 作起点，记摘要进 `episode_digests` 与首个智能体轮次的轨迹 `memory.digest_before`。该 episode 的任一次尝试失败要重试时，先用起点副本覆盖部署库再重试（M18）；成功后删起点副本 |
| episode 复用 | `_reusable` 除库版本外，还要求首个智能体轮次的 `digest_before` 等于 `episode_digests[episode_id]` 里该人格的摘要，且该摘要等于部署库当前摘要（即它之后没有别的 episode 写过）；三者不全等就重跑 |
| 验证重放 | 每个人格从部署库 `backup_to` 到 `snapshots/<round_id>/verify-<persona_id>.db`，基线与候选各开一份快照副本，`memory_write=False` |
| 门② `kind: memory` | 收录时对该人格的部署库调 `remember_event(key="<proposal_id>#<k>")`。key 去重在 Starling 库内与写入同一事务，所以“写成功但收据没落树”的重入不会二次抽取：重入时再调一次，得到 `idempotent` 与同一批语句 id。语句 id 写进提案载荷的 `admitted_statements` 只作证据，不作幂等依据 |
| 门② 失败 | `remember_event` 抛异常或 `failed` 时写一条 T2 失败记录，提案改为 `rejected` 并带原因；审核类状态机增加 `approved → rejected` 这一条边（F23），只许收录处理器走 |
| 人格归属 | 记忆提案的 `definition` 增加必填 `persona_id`，指明写进哪个人格的库 |
| 旧数据 | `memory_library/` 必须为空（第 0 节），`load_library` 不再读它 |

## 10. 失败与可观测

| 失败点 | 分类 | 处理 |
|---|---|---|
| 配置要求 Starling 但未安装 | 配置错误 | 装配期退出码 2，信息说明 `pip install '.[memory]'` |
| `OPENAI_API_KEY` 为空 | 配置错误 | 装配期退出码 2，只写变量名 |
| `memory_library/` 非空 | 配置错误 | 装配期退出码 2，要求人工处理 |
| 起点库或种子打不开、迁移失败、摘要算不出 | T2 | 指令 `failed`，原因只报类名 |
| 续训时起点摘要或运行时身份变了 | T3 | 现有的 `config_hash` 门 |
| 对手位 `MemoryFailureError`，类别是 `timeout` 或 `transport_failure` | `role_memory_unavailable`，T1 | 驱动器新增分支；轨迹源与部署按条重跑（加进两边的瞬态集合） |
| 对手位 `MemoryFailureError`，其余类别或非 Starling 异常 | `role_memory_failure`，T2 | 驱动器新增分支；轨迹源不重跑；策略上下文把它加进确定性集合，指令判 `failed` |
| 训练位 `MemoryFailureError`，瞬态类别 | 按条重跑 | 轨迹源在 `_attempt` 里接住，译成瞬态 `_AttemptFailed`，会话 abort |
| 训练位 `MemoryFailureError`，其余 | 策略异常，T2 | 会话 abort；策略上下文译成 `DirectiveFailedError`，不延后 |
| 抽取失败（`failed` 为真） | 不分类 | 计数进轨迹；这一轮不留记忆 |
| 准入时活动加滞留会话数达上限 | `role_backend_unavailable`，T1 | 客户端按条重跑；日志每分钟报滞留数 |
| `join` 超时 | 不分类 | 资源进滞留表，不关不删，占一个会话名额（6.2 节） |
| 副本删除失败 | 不分类 | 记日志；清扫兜底 |

异常信息不含端点地址与密钥：适配器只报异常类名与 `failure_category`。

## 11. 测试策略

| 层次 | 对象 | 替身 |
|---|---|---|
| 边界 | `simulator`、`mindtrainer` 不导入 `actio` 与 `starling`；`actio` 只在 `memory_starling` 导入 `starling` | 扫导入、子进程导入检查 |
| 契约 | 4.1 节的契约测试，对 `TupleMemory` 与 `StarlingMemory` 各跑一遍 | Starling 未装时只跑替身 |
| 单元 | Actio 每轮读写：recall 在回复前，remember 只收对方发言、带 key 前缀，抽取失败不抛且进轨迹 | 记录调用的假端口 |
| 单元 | `respond_guided` 只产生一条 system 消息 | 本机 HTTP 替身 |
| 单元 | 摘要：同一逻辑内容不同文件字节时相同；改任一进摘要的字段即变 | 真 Starling |
| 单元 | `SessionResources`：quiesce 后 respond 抛；join 等到超时线程退出；close 删目录；join 超时则进滞留表、目录仍在、名额被占 | 阻塞的假后端 |
| 单元 | 轨迹 v2：计数是字符串；经 `Struct` 往返后 `validate_trace` 仍通过 | 无 |
| 单元 | 客户端逐轮等待超时译成 `timeout` 并 abort | 不回消息的假服务端 |
| 集成 | 服务端会话终态（正常、超时、断开）都走 quiesce、join、close；副本被删 | loopback 服务端 |
| 集成 | 滞留数达上限时 `CreateSession` 被拒为 `role_backend_unavailable`；滞留线程退出后名额释放 | loopback 服务端、可解除阻塞的假后端 |
| 集成 | 部署 episode 重试：第二次尝试前部署库被回滚到起点，摘要相同 | 真 Starling 或替身 |
| 集成 | 同组两条 rollout 从同一起点出发，会话结束副本被删；`session_id` 出现在目录名里 | loopback 服务端、替身端口 |
| 集成 | 起点摘要与运行时身份进 `config_hash`；续训时种子或模型变了升 T3；`memory` 缺省时哈希、落盘 episode 与现在逐字节相同 | 临时仓库 |
| 集成 | `role_memory_failure` 让指令判 `failed` 而不是延后；`role_memory_unavailable` 按条重跑 | 抛异常的假端口 |
| 集成 | `memory` 存在却传旧 `policy` 参数时构造期拒绝 | 无 |
| 单元 | 门② 记忆提案改走 `remember`；重入不重复写；失败转 `rejected` | 替身端口 |
| 端到端 | `--interact` 一条 run：所有角色位都是带记忆的 Actio，episode 每轮带 v2 轨迹，记录里有 `memory` 块 | 本机 HTTP 替身 vLLM、替身端口 |

需要真 Starling 的用例按 extra 缺失跳过。真抽取与真嵌入不进测试。

## 12. 施工顺序

| 步 | 内容 | 依赖 Starling PR |
|---|---|---|
| 1 | 记忆端口与契约测试、`TupleMemory`、实例改持端口、每轮读写、轨迹 v2 | 否 |
| 2 | `respond_guided`；后端工厂加 `session_id`；`SessionResources` 与 `TimeoutBackend` 登记；驱动器与 servicer 的终态收尾；`role_memory_failure` | 否 |
| 3 | 训练路换成 Actio：`policy_for_rollout`、对手工厂包 Actio、轨迹上行、时间预算 | 否 |
| 4 | `TrainingConfig.memory`、运行时身份、`config_hash`、`policy_backend.memory`、清扫 | 否（替身） |
| 5 | Starling PR：第 8 节七个接口 | 是（在 Starling 仓库） |
| 6 | `StarlingMemory`、树外记忆配置、`memory` extra | 是 |
| 7 | MindAgent：部署记忆、轮次摘要、快照重放、门② 改路 | 是 |
| 8 | 文档与图的收尾 | 否 |

第 1 到 4 步只用替身就能跑通，可以先合。第 5 步合入后再做 6、7。

接口变更清单（每一项都在上面的某一步里，列在一处便于对照）：

| 接口 | 变更 | 步 |
|---|---|---|
| `actio.instance.instantiate` | `memories=` 之外加 `memory=`、`memory_write=` | 1 |
| `actio.trace` | 新增 `actio.trace.v2`，`validate_trace` 接受 v1 与 v2 | 1 |
| `CreateSession` proto | 新增可选 `episode_id` | 2 |
| `BackendsFactory` | `(SessionInfo) -> SessionResources` | 2 |
| `on_session_closed` | `(session_id, episode)` | 2 |
| `TimeoutBackend` | 构造时收 `SessionResources`，登记在途线程 | 2 |
| `EnvironmentServicer` | 准入检查、终态收尾、滞留表 | 2 |
| `SessionDriver` | 新增 `role_memory_unavailable`、`role_memory_failure` 分支 | 2 |
| `EnvironmentServiceClient` | 逐轮等待上限 | 2 |
| `VllmPolicy`、`ExternalApiBackend` | 新增 `respond_guided` | 2 |
| `InteractTrajectorySource` | 新参数 `policy_for_rollout`；三元组上行 | 3 |
| `make_policy_factory` | 返回 `policy_for_rollout` 工厂 | 3 |
| `TrainingConfig` | 可选 `memory` | 4 |
| `training_defaults.interact` | 可选 `memory` | 4 |
| `TrainingRun.policy_backend` | `memory` 块 | 4 |
| `controlplane.states` | `approved → rejected` | 7 |
| `agent_round` 载荷 | `memory_runtime`、`episode_digests` | 7 |
| 记忆提案 `definition` | 必填 `persona_id` | 7 |

## 13. 已定与未核实

已定：

1. 对手位与训练位都是带记忆的 Actio 实例（M1）。
2. 每轮都读写（M7），每条 rollout 同一起点、用完即弃（M9）。
3. 抽取与嵌入走 OpenAI 兼容端点，密钥 `OPENAI_API_KEY`（M12）。
4. Starling 的缺口经 PR 补（M14）。

未核实：

| 项 | 核实办法 |
|---|---|
| 真抽取与真嵌入的延迟是否在 8 秒内 | 接真端点跑 10 轮，记每次 remember 与 embed_pending 的耗时 |
| 真嵌入下 `recall` 的命中质量 | 用 SoMBench 之外的手写情境，看召回句是否相关 |
| `VACUUM INTO` 副本与原库在 Starling 迁移版本不同时能否打开 | 在 PR 里加用例：旧版本库备份后用新版本打开 |
| 每会话开一份库对 GRPO 一步的总时长影响 | 一步 `groups_per_step × group_size` 条 rollout，计开库、备份、删除总耗时 |
| 不跑 `tick` 时承诺、共同点等分区不更新，对召回质量的影响 | 同一组对话分别跑 `tick` 与只嵌入，比较召回 |
