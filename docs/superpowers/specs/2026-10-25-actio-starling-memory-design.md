# 设计二：每个角色位都是带记忆的智能体，记忆用 Starling

状态：修订稿第七版，待审阅。日期：2026-10-26（第一稿 2026-10-25）。
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
| F24 | 部署 episode 在一个线程里按情境、序号顺序逐条跑；轮次由守护进程的单一调和循环串行派发 | `src/mindagent/deployment.py:144-146` |
| F25 | 服务端一条会话只有一个驱动器线程调后端，每个角色位同一时刻至多一次在途调用 | `src/simulator/realtime/driver.py:63-64`、`src/simulator/runner.py` |
| F26 | 派发器在进入编排器之前就调 `validate_training_config`，不过就判 T2；编排器里 `config_hash` 的比较在校验之前 | `src/mindtrainer/dispatch.py:331-333`、`src/mindtrainer/orchestrator.py:177-192` |

### 1.2 Starling（`48e9f3b`，代码核对加本机探针）

| 编号 | 事实 | 实测或位置 |
|---|---|---|
| S1 | 从源码 `uv pip install` 可装：scikit-build-core 构建，本机一次 179 秒（环境测量，不作预算依据） | 实测 |
| S2 | 公开 `Memory.recall` 不收 `holder`；私有 `MemoryCore.recall` 收 | `python/starling/memory.py:168`、`_memory_core.py:250` |
| S3 | `remember` 要求注入 LLM，否则抛 `LLMNotConfigured`；`make_openai_llm` 的密钥由 C++ 读 `OPENAI_API_KEY` | `_memory_core.py:183-187`、`memory.py:48-70` |
| S4 | `remember` 写入的语句是 `volatile`，嵌入后仍不会被向量召回命中：候选只取 `consolidated` 与 `archived`（`sqlite_blob_vector_index.cpp:67`），只有 `tick` 的回放会把它巩固。嵌入本身由 `tick` 或 `EmbeddingWorker.tick_one_batch(now_iso)` 执行。所以不跑 `tick` 时，召回要显式放宽到 `volatile`（第 8 节 `recall(include_volatile=True)`），review 与 holder 守卫不变 | 实测（施工计划第 6 步的探针：只嵌入召回 0 条，改成 `consolidated` 后 1 条）；`bindings/python/bind_10_embedding.cpp:132-138` |
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
| S16 | 嵌入适配器用 `base_url + "/embeddings"` 拼请求地址，路径有意义；它按批发请求（有 `batch_calls` 计数） | `src/embedding/openai_embedding_adapter.cpp:64`、`bindings/python/bind_10_embedding.cpp:71-72` |
| S17 | `remember` 的抽取（网络）在 SQLite 事务之外执行，落库在之后的一个 `TransactionGuard` 里提交 | `src/memory/memory_ops.cpp:196-215,241-273` |
| S18 | 情感重排器用语句的 `activation` 列参与打分 | `src/retrieval/affect_reranker.cpp:79-83` |
| S19 | 同文再写返回 `idempotent` 时 `statement_ids` 为空，库里语句数不变（探针：两次写后召回仍 1 条） | 实测 |
| S20 | 一次 `remember` 顺序发三次 LLM 调用：信念、一般事实、情节，各自独立受 `timeout_ms` 约束 | `src/memory/memory_ops.cpp:176-200` |
| S21 | 语义 `recall` 先对查询词调一次嵌入（走网络），再做向量检索 | `python/starling/_memory_core.py:250-270`、`src/retrieval/semantic_retriever.cpp:27` |

## 2. 裁决总表

| 编号 | 裁决 | 理由 | 判错的代价 |
|---|---|---|---|
| M1 | 训练路的每个角色位都是 Actio 实例；底座按角色位的后端定 | 用户裁定：训练位与对手都是智能体，对手要靠记忆扮演个体 | 每轮多一次 recall 与 remember，会话变慢 |
| M2 | `simulator` 仍只依赖 `ModelBackend`；新增的是后端工厂的会话身份参数，不是协议方法 | Actio 实例本来就满足 `respond`；会话身份是服务端本来就缺的 | 后端工厂签名改一次 |
| M3 | 记忆在 Actio 内部，经记忆端口访问；只有 `actio/memory_starling.py` 导入 `starling` | 记忆属于个体，不属于环境 | Actio 多一个可选依赖 |
| M4 | 一个智能体一个 Starling 库文件，库的 `agent` 与 holder 都是该人格 | 个体隔离靠文件，不靠 holder 过滤；同时多开文件已实测 | 文件多 |
| M5 | 记忆库在树外，根目录由树外配置给 | 原生 SQLite 带 WAL，不适合当 git 产物 | 不能从产物树追溯，靠摘要补 |
| M6 | 记忆状态的身份是逻辑摘要：语句表、向量表、事件表的**全部列**按排除清单去掉纯簿记列后算 sha256，与文件字节无关；召回、指导语与轨迹里的记忆 id 是内容 id（规范化行的哈希），不是 UUID | 文件字节不稳定（S9）；按包含清单列字段会漏（S18）；UUID 不进摘要却进 prompt，会让摘要相同而 prompt 不同 | 摘要要读三张表；加列要维护排除清单 |
| M7 | 默认每轮都读写：回复前 recall，对方每句话 remember，之后只做嵌入，不做后台维护 | 用户裁定每轮读写；后台维护有随机性与负载门控（S12），不可复现 | 训练里没有 TTL、回放与巩固 |
| M8 | 训练 run 的起点记忆与记忆运行时身份一起进 `config_hash`；interact 配置的 `memory` 字段**必填**，训练里每轮必写，没有关写的开关 | 记忆变了行为就变；换 Starling 或换抽取模型行为也变；M1、M7 不允许"不用记忆"或"不写记忆"的旁路 | 本设计落地前的 pending interact run 在派发器的配置校验处判 T2（F26），指令 `failed`，要人工重开 |
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
| M19 | 服务端的准入检查、会话登记、滞留计数在同一把锁里完成；逻辑 episode id 要过白名单，且同一时刻只许一个活动会话用它，撞上时判瞬态 `role_backend_unavailable` | 不在一把锁里，并发 `CreateSession` 会突破上限；客户端给的 id 进目录名与主键 | 多一把锁 |
| M20 | 部署库按人格加文件锁：一个轮次独占它绑定的每个人格库，episode 逐条顺序跑（F24） | 两条 episode 同时写一个人格库，回滚会抹掉对方的写入 | 两个轮次不能并行，现状本就如此 |
| M21 | 记忆调用的失败映射是穷举的：Starling 类别 `timeout`、`transport_failure` 是瞬态，其余已知类别、未知类别、非 Starling 异常都是确定性 | 映射不穷举，新类别会落进错误的桶 | 新的瞬态类别出现前先按 T2 处理 |

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
    def embed_pending(self) -> int: ...          # 只做嵌入,返回成功条数;有失败就抛
    def digest(self) -> str: ...                 # 逻辑摘要,sha256 十六进制
    def close(self) -> None: ...                 # 只关库,不放弃事件
```

| 类型 | 字段 | 说明 |
|---|---|---|
| `MemoryHit` | `id`、`text` | `id` 是内容 id：`mem-` 加规范化语句行（不含 UUID 与簿记列）sha256 的前 16 位（F9）；同样内容在任何库里得到同样的 id，`text` 是渲染好的一句 |
| `MemoryEvent` | `key`、`text`、`interlocutor` | `key` 是事件的稳定身份。没有 `seq` 字段：序号由库在写事件行时分配（4.3 节时钟一条），实例不计数 |
| `MemoryEventConflict` | 异常 | 同 `key` 已存在且文本不同、或该 key 已放弃时抛出；是 `MemoryFailureError` 的子类，`transient` 为假 |
| `MemoryClosedError` | 异常 | 端口关闭后再调任何方法时抛出；是 `MemoryFailureError` 的子类，`transient` 为假 |
| `RememberOutcome` | `stored`、`failed`、`category` | `category` 取 Starling 的 `failure_category`（S13） |

一个端口实例只属于一个智能体，所以签名里没有 holder。`pending` 事件的放弃不进端口签名，也不在 `close()` 里做：放弃要等调用方知道这条 episode 最终是成功还是失败（4.3 节"谁放弃"）。训练副本随后被删，从不放弃；部署库由 MindAgent 经管理面放弃。门② 不经 Actio 实例，直接调 Starling 的 `remember_event` 与 `abandon_event`。

| 实现 | 文件 | 用途 |
|---|---|---|
| `TupleMemory` | `actio/memory.py` | 包现有 `MemoryEntry` 元组与 `retrieve`；`remember` 追加一条 `MemoryEntry`，`subject` 是 `interlocutor`。`recall` 返回的 `MemoryHit.id` 一律是内容 id：`mem-` 加 `subject`、`content` 规范化后 sha256 的前 16 位，不用条目自带的 `id`。所以同一内容换个 `id` 进来，prompt 与轨迹不变，与 Starling 一侧的内容 id 约束一致。代价是 `memories=` 简写实例的 `memory_hits` 与今天不同；既有断言 `mem-1` 之类字面量的测试随第 1 步改写 |
| `StarlingMemory` | `actio/memory_starling.py` | 包一个 `starling.Memory`；唯一导入 `starling` 的文件 |

部署记忆另有一个管理面，只给 MindAgent 用，训练路不碰。它与端口同在 `actio`，所以 MindAgent 不导入 `starling`：

```python
class DeploymentMemoryStore(Protocol):     # 一个人格一个库,按人格 id 打开
    def open(self, persona_id: str) -> MemoryBackend: ...
    def remember_event(self, persona_id, key, text, *, interlocutor) -> RememberOutcome: ...
    def abandon_event(self, persona_id, key) -> None: ...
    def pending_events(self, persona_id) -> list[str]: ...
    def backup_to(self, persona_id, path) -> str: ...     # 返回副本的逻辑摘要
    def restore_from(self, persona_id, path) -> str: ...  # 先确认该人格没有打开的端口,再覆盖
    def digest(self, persona_id) -> str: ...
```

`restore_from` 在该人格还有打开的端口时抛 `MemoryFailureError`（`transient` 为假）：覆盖正被打开的 SQLite 文件会写坏库。实现有两个：`StarlingDeploymentStore`（`actio/memory_starling.py`）与 `FileTupleStore`（`actio/memory.py`，把条目与事件表存成 JSON 文件，`backup_to` 是复制文件）。替身因此也落盘，提交点协议在两种实现上一样成立；不设记忆配置的部署路用 `FileTupleStore`，根目录取 `control-plane/` 之外的临时目录，不进树。

两种实现过同一套契约测试与规范样例（M17）。样例是一份 YAML：固定的事件序列、每步之后的 recall 预期命中 id 顺序、每步之后的 digest、同 key 同文本重写的结果、同 key 不同文本抛 `MemoryEventConflict`、close 后再调抛 `MemoryClosedError`。替身与 Starling 对同一份样例必须给出同样的命中顺序与摘要变化曲线（摘要值本身两边不同）。事件 key 的去重由端口实现自己维护：`StarlingMemory` 在库旁的 `events` 表（第 8 节 PR 新增）里记 `key → 文本 sha256、语句 id 列表`，查表早于 `remember`；`TupleMemory` 用字典。

### 4.2 实例改持端口

| 改动 | 现状 | 改后 |
|---|---|---|
| 字段 | `memories: tuple[MemoryEntry, ...]` | `memory: MemoryBackend`、`replay_read_only: bool`（只给 MindAgent 的验证重放用，训练路永远为假） |
| `decide` | `retrieve(self.memories, q, limit)` | `self.memory.recall(q, limit=self.memory_limit)` |
| `respond` 之后 | 无 | 对新近的对方发言逐条 `remember`，再 `embed_pending`；`replay_read_only` 为真时跳过 |
| `instantiate` | `memories=` | `memory=`、`replay_read_only=`、`episode_id=`（逻辑 episode id，事件 key 的前缀）；另收 `interlocutors=`：`{角色位 id: 人格 id}`，事件文本与 `interlocutor` 都用它把说话方的角色位换成人格 id，缺省为空映射、查不到时用角色位 id 本身；调用方都持有角色分配（服务端工厂有 `SessionInfo.assignment`，训练位有 `cfg.bindings`，MindAgent 有 `config.bindings`）。保留 `memories=` 作为 `TupleMemory` 的简写，与 `memory=` 恰好给一个。只给 `memories=` 时 `replay_read_only` 默认为真：今天的调用方都是这样构造的，它们不期望实例往记忆里写东西，也给不出 `episode_id`。给了 `memory=` 且 `replay_read_only` 为假而缺 `episode_id`，抛 `ActioDataError`（`ValueError` 的子类） |
| `close` | 无 | 转发给 `memory.close()` |

`render_guidance` 改收 `list[MemoryHit]`，记忆行写成 `[memory:<id>] <text>`。

### 4.3 读写时机（M7）

- **读**：每次 `respond` 前，查询词是对方最后一句（现有 `_memory_query`），`limit` 默认 3。
- **写**：每次 `respond` 后，把上次回复之后对方说的每句话按顺序交给 `remember`，然后 `embed_pending` 一次。不调 `tick`：后台维护的随机性与负载门控会让同一起点得到不同状态（S12）。
- **事件身份**：`key` 是 `<逻辑 episode id>#<turn_index>`；部署的事件 key 前面再加轮次 id，写成 `<round_id>/deploy-{model}-{scenario}-{k}#<turn_index>`，因为部署库跨轮次持久，而 `deploy-{model}-{scenario}-{k}` 在下一轮会重复出现，不加就撞上上一轮已 `committed` 的同 key。轮次前缀只进事件 key：`instantiate` 的 `episode_id` 参数收 `<round_id>/deploy-...`，交给服务端的 `CreateSession.episode_id` 与落盘文件名仍是不带轮次的 `deploy-...`（白名单不含 `/`）。`text` 是 `<说话方人格 id>：<utterance>`。逻辑 episode id 由客户端定（F21）：训练是 `{run}-{stage}-{scenario}-r{k}`，部署是 `deploy-{model}-{scenario}-{k}`，重试不变；它经 `CreateSession` 新增的 `episode_id` 字段传给服务端，服务端落盘的 episode 也用它当 id。`key` 作为前缀写进 Starling 的文本：`[<key>] <text>`，所以不同 episode 的同一句话是不同事件；同 key 的重写由 `events` 表拦下，不再调 `remember`。
- **时钟**：`now` 由 `seq` 推出：`2026-01-01T00:00:00Z` 加 `seq` 秒。`seq` 存在 `events` 表所在的同一个 SQLite 库里，与写事件同一事务递增；训练副本从 0 起，部署库跨轮次单调。
- **自己的话**：不写。它已在对话历史里。
- **episode 内重试**：`remember` 的抽取失败在同一次 `respond` 里不重试；同一 episode 的下一次 `respond` 会把上次失败的那几句连同新发言一起再交给 `remember_event`（同 key 同文本，`pending` 行被推进）。`close()` 只关库，不放弃任何事件：放弃是终态，同 key 再写会抛 `EventConflict`，放早了就堵死重试。
- **谁放弃**：训练副本用完即弃，从不放弃。部署库由 MindAgent 在一条 episode **最终**结束时放弃：成功提交后对这条 episode 留下的 `pending` 行逐个 `abandon_event`；最终失败（重试用尽）前先用起点副本 `restore_from`，回滚后库里本来就没有这条 episode 的事件行。尝试之间的重试也先 `restore_from`，所以下一次尝试面对的是起点状态，同 key 不会撞上上一次尝试留下的行。
- **哪些失败只计数、哪些抛**：`remember` 返回 `failed` 为真（Starling 给了 `failure_category`）一律只计数、不抛，不论类别是不是 `timeout`；`recall` 与 `embed_pending` 的任何失败、`remember` 本身抛出的异常都包成 `MemoryFailureError` 上抛。理由：写失败只少一条记忆，回复照样成立；读失败或库坏了，这一轮的指导语已经不可信。
- **抽取失败**：`RememberOutcome.failed` 为真时这一轮不留该条记忆，实例计数加一，不抛到 `respond` 之外；计数在激活轨迹里（第 4.6 节）。事件行只在抽取成功后才变成 `committed`（第 8 节 `remember_event`），所以失败的事件下次重试还会再抽一次，不会被当成幂等丢掉。训练副本用完即弃，不会带着 `pending` 事件行进下一条 rollout。部署库里的 `pending` 行各有主人：对话事件的主人是产生它的 episode，门② 事件的主人是提案；谁写的谁处理，没有"开始前统一补齐"。对话事件在 episode 内重试一次就算（重试仍失败则留 `pending`，计数进轨迹）。放弃由 MindAgent 在 episode 最终结束时做（见"谁放弃"），不由实例的 `close()` 做；所以部署 episode 结束后库里没有它的 `pending` 行，起点快照也不会带着别人的 `pending` 行。
- **`recall`、`remember`、`embed_pending` 抛异常**：包成 `MemoryFailureError` 上抛，由调用方按第 9 节分类。

### 4.4 渲染（S6）

`StarlingMemory.recall` 把每个 `{row, score}` 渲染成：

```
[memory:<内容 id>] <subject_id> <predicate> <object_value>（<modality>，置信度 <confidence>）
```

否定语句（`polarity` 为负）在谓词前加“不”。`StatementRow` 没有 `nesting_depth`，不渲染嵌套层次。

### 4.5 逻辑摘要（M6）

`digest()` 调 Starling 新增的公开 `Memory.digest()`（第 8 节）。输入是本 tenant 下所有语句行的规范化 JSON，覆盖每一个影响召回候选与排序的字段：

| 进摘要 | 不进摘要 |
|---|---|
| `statements`、`statement_vectors`、`events` 三张表的全部列，减去排除清单；`events` 里 `abandoned` 行只计 `key` 与 `state` | 排除清单：`id`、`stmt_id`、`created_at`、`updated_at`、`last_accessed`、`access_count`、`last_replayed`、`replay_count`、`last_replay_batch_id`、`retry_count`、`last_attempt_at`、`embedded_at`、`inferred_at`；`supersedes_id`、`derived_from_json` 里的 UUID 换成被指向行的内容 id；其他所有表 |

按排除清单而不是包含清单：加列默认进摘要，漏掉只会让摘要过敏感，不会让它漏变化。`activation` 进摘要（S18：重排器用它）。所有引用语句 UUID 的列（`supersedes_id`、`derived_from_json`、`events.statement_ids_json`、`statement_vectors.stmt_id` 的归属）在规范化时一律换成被指向语句的内容 id；指向不存在的语句时换成固定串 `missing`。每行按列名排序后规范化成 JSON，行之间按内容 id 排序，整体算 sha256。空库的摘要是固定值。向量进摘要是因为同一个模型名在外部端点上不保证给出同一向量。`TupleMemory.digest()` 对 `snapshot(entries)` 的结果（召回实际看到的条目，不是原始列表）逐条 `to_dict()` 去掉 `id` 后的排序列表加事件表算同样形状的 sha256。

### 4.6 激活轨迹

轨迹模式升到 `actio.trace.v2`，在八个键之外加 `memory`：`{"digest_before": sha, "remembered": "n", "extraction_failed": "n"}`。`digest_before` 是这一轮 recall 之前的摘要；两个计数是本实例构造以来的累计值。凡是带记忆端口构造的实例都产出 v2（包括 `memories=` 简写构造的替身）；v1 只剩旧的落盘 episode。两个计数是十进制字符串，与 `library_version` 同一规矩（F20），经 `Struct` 往返不失真。校验：v2 的键集合是九个，`memory` 的三个值都是非空字符串，计数必须是 ASCII 十进制。消费方（分析器、`_reusable`）对 v1 与 v2 都接受；`memory_hits` 的校验不变。抽取失败的计数因此随 episode 落盘，不需要 `Episode` 加元数据（F16）。

## 5. 记忆库的位置与配置

### 5.1 树外配置（M5、M12）

环境变量 `RISMFORGE_MEMORY_CONFIG` 给出 YAML 文件的绝对路径，文件必须在仓库外、权限 0600，校验照 `eval_config.py` 另写一份（F15）。

| 键 | 必填 | 含义 |
|---|---|---|
| `root` | 是 | 记忆库根目录，绝对路径，在仓库外 |
| `extraction_base_url`、`extraction_model` | 是 | 抽取用的 OpenAI 兼容端点与模型 |
| `embedding_base_url`、`embedding_model`、`embedding_dim` | 是 | 嵌入用的端点、模型与维度 |
| `timeout_ms` | 否 | 抽取与嵌入的单次上限，默认 4000 |
| `max_retries` | 否 | 抽取与嵌入的重试次数，默认 0 |

配置里不放密钥。启动时检查 `OPENAI_API_KEY` 非空，报错只写变量名。“配置不存在”只指环境变量 `RISMFORGE_MEMORY_CONFIG` 未设置：这时训练路与部署路都用替身，并在启动日志里写 `memory backend: stand-in`。变量设置了而文件不在、权限不是 0600、在仓库内、缺键或多键，一律装配期退出码 2，与评测配置同一做法。

### 5.2 记忆运行时身份

`MemoryRuntimeIdentity` 是一个字典，所有记录与哈希都用它：

| 键 | 取值 |
|---|---|
| `backend` | `starling` 或 `stand-in` |
| `starling_version` | `starling.__version__` 加安装来源：git 安装取 `direct_url.json` 的 `vcs_info.commit_id`；非 editable 的源码目录安装取已安装包 `RECORD` 里全部文件哈希的 sha256；editable 安装（`direct_url.json` 有 `dir_info.editable`）装配期拒绝 |
| `extraction` | `{base_url, model, timeout_ms, max_retries, prompts: Starling ExtractionConfig 的摘要}` |
| `embedding` | `{base_url, model, dim, timeout_ms, max_retries}` |
| `trace_schema` | `actio.trace.v2` |

`base_url` 是规范化后的完整地址（小写 scheme 与主机、去掉末尾斜杠、含路径），不含 userinfo；配置读取时已拒绝带 `user:password@` 的地址。替身时 `extraction` 与 `embedding` 为 `null`。

### 5.3 目录布局

```
<root>/
  deploy/<persona_id>.db                            部署记忆,持久,跨轮次累积(seq 与事件表在库内)
  seeds/<seed_id>/<persona_id>.db                   训练起点种子库,只读
  runs/<run_id>/<session_id>/<slot_id>.db           服务端对手位的私有副本,会话终态即删
  runs/<run_id>/client/<episode_id>/<slot_id>.db    客户端训练位的私有副本,rollout 结束即删
  snapshots/<round_id>/<episode_id>-<persona_id>.db 部署 episode 的起点副本,跨重启保留,删除即提交
  snapshots/<round_id>/verify-<persona_id>.db       部署轮次的验证快照,轮次结束即删
```

库文件名用人格 id，因为人格才是个体。同一个人格在两个角色位同时出现时，两个位各开一份副本，互不可见。

一个记忆根目录只属于一个控制面仓库：根目录下的 `.owner` 文件记该仓库的绝对路径，首次使用时以 `O_CREAT | O_EXCL` 原子创建，`FileExistsError` 时读回再比；读到的路径与当前仓库不同就退出码 2。两个仓库同时首次使用，只有一个能创建，另一个读到对方的路径而退出。否则两个仓库共用一个根目录时，部署记忆会混写，启动清扫会删掉对方在途会话的副本。

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
| `CreateSession` | 新增可选字段 `episode_id`（逻辑 episode id，F21）；给了就用它当落盘 episode 的 id，没给沿用 `ep-<session_id>`。校验：只许 `[A-Za-z0-9._-]`、1 到 128 字节、不是 `.` 或 `..`；同一个 `episode_id` 已有活动会话时拒绝，终止原因 `role_backend_unavailable`（T1）：客户端 abort 后旧会话要等泄流线程与在途调用退出才进终态，立刻重试会撞上它，这是暂时的；格式不合白名单才是 `invalid_session`。"活动"从准入登记起，到 servicer 的 `finally` 收尾完（或移交滞留表）为止 |
| 后端工厂 | 签名改为 `(SessionInfo) -> SessionResources`，`SessionInfo` 含 `scenario`、`assignment`、`personas`、`client_slot`、`session_id`、`episode_id` |
| 落盘钩子 | 签名改为 `on_session_closed(session_id, episode)`；`AgentRuntime` 按 `session_id` 查归属 |
| 登记表 | 策略上下文登记“情境 id → run id”只用于判定会话归属；工厂造资源时把 `session_id → run id` 记进第二张表，终态时删除；落盘只查第二张表 |
| 记忆目录 | `runs/<run_id>/<session_id>/<slot_id>.db` |

现有的“情境 id 不得同时属于训练与部署”检查（Ruling 24）保留。落盘不再按情境 id 路由，所以策略上下文注销情境时，仍在跑的会话照样能落到自己的 run。

### 6.2 SessionResources

服务端每条会话一个 `SessionResources`，由后端工厂返回并交给驱动器：

这三个类型在新模块 `simulator/realtime/resources.py`：`SessionInfo`、`SessionResources`、`SessionClosingError`。

```python
class SessionResources:
    backends: dict[str, ModelBackend]      # 角色位 -> 未套期限的后端;servicer 拿到后再套 TimeoutBackend
    def quiesce(self) -> None: ...         # 拒绝新调用,之后的 respond 抛 SessionClosingError
    def join(self, timeout_s: float) -> bool: ...   # 等所有在途后端调用退出
    def close(self) -> None: ...           # 对每个智能体直接调 close(),不经包装层转发;再删副本目录
```

- 所有权：工厂造出 `SessionResources` 交给 servicer；servicer 把 `backends` 给驱动器，自己持有资源对象直到流结束。驱动器只调后端，不持有资源。工厂返回之后、驱动器启动之前的任何失败（校验、缺后端、`start` 抛）都由 servicer 在同一个 `try` 里对资源做 `quiesce`、`join`、`close`；工厂自己抛异常时由工厂保证不留半开的库（先建所有库再返回，中途失败先关已开的）。
- 准入：`CreateSession` 的处理在运行时的一把锁里做完三件事：检查“活动数加滞留数小于 `max_workers`”且 `episode_id` 没有活动会话，登记会话，然后才调工厂（M19）。工厂在锁外跑，失败时在同一把锁里撤销登记。
- 谁套期限：servicer 拿到资源后给每个后端套 `TimeoutBackend` 再交驱动器，与现状同一处（`servicer.py:286`）。资源自己持有未套的智能体，所以 `close()` 直接调它们，包装层（期限、记忆异常转换）都不需要转发 `close`。
- `TimeoutBackend` 改为登记在途线程：`respond` 开始时加入 `SessionResources` 的在途集合，结束时移出；超时只是调用方放弃等待，线程本身仍在集合里。
- 驱动器进入终态（`CLOSED` 或 `FAILED`，含断开）后，由 servicer 在流结束的 `finally` 里按顺序调 `quiesce`、`join(timeout_s=60)`、`close`。
- `join` 超时：**不 close、不删**。资源对象移交给 servicer 的滞留表（`EnvironmentServicer` 持有，准入检查就在 servicer 里做，回环服务端不经组合根就能测），并继续占住一个会话名额：servicer 的准入检查“活动会话数加滞留数小于 `max_workers`”不过就拒绝 `CreateSession`，终止原因 `role_backend_unavailable`（T1，客户端按条重跑）。滞留表每 60 秒重试一次 `join(0)`，成功后 close 并删目录。
- 清扫：`run` 与 `reconcile` 取到仓库排他锁之后、构造实时服务端之前，删掉记忆根目录下 `runs/` 的所有子目录；`deploy/`、`seeds/`、`snapshots/` 不碰。`reconcile` 在装配前取锁，清扫放在装配里即可；`run` 现在是装配之后才在 `run_daemon` 里取锁（`wire.py:383`、`daemon.py:517`），所以清扫不放进装配，而是作为 `run_daemon` 取锁之后、第一个 tick 之前调用的启动钩子：`run_daemon` 新增 keyword-only 参数 `on_locked: Callable[[], None] | None = None`，取到锁后、建退避表之前调一次，抛异常则守护进程不进第一个 tick、带原因退出。实时服务端本来就在第一次需要会话时才起（`AgentRuntime.client_factory`），晚于这个钩子。此时本进程还没有会话，别的进程被仓库锁挡在外面，根目录又只属于这个仓库（5.3 节），所以没有在途副本会被误删。运行中只删滞留表 join 成功的目录，不扫目录。
- 在途集合的操作持一把锁；`join` 不持锁等待。

### 6.3 并发上限

每条会话最多 `len(bindings) - 1` 个在途后端线程，加驱动器线程与泄流线程各一个。gRPC 的 `max_workers` 限制并发会话数（`server.py:38-46`），滞留的资源也占会话名额（6.2 节），所以进程里的后端线程总数不超过 `max_workers × (len(bindings) + 1)`。代价是挂死的后端会一直占着名额：滞留数达到 `max_workers` 时服务端拒绝所有新会话，守护进程的日志每分钟报一次滞留数，由人重启进程。Python 的 daemon 线程杀不掉，这是能给出的最强保证。

### 6.4 时间预算（F17）

| 预算 | 现值 | 改后 |
|---|---|---|
| 客户端每轮等待（流 deadline 按轮折算） | 30 秒 | 90 秒 |
| 服务端等客户端提交一轮（`turn_timeout_s`，`--interact` 传入） | 30 秒 | 90 秒 |
| 服务端后端调用上限 | 60 秒 | 60 秒 |
| 对手 HTTP 上限 | 25 秒 | 25 秒 |
| 记忆单次网络调用上限（`timeout_ms`） | 无 | 4 秒 |
| 一轮里记忆网络调用的最坏合计 | 无 | 20 秒：recall 的查询嵌入 1 次、remember 的抽取 3 次、embed_pending 的嵌入 1 批，每次 4 秒 |
| 训练位策略 HTTP 上限 | 120 秒 | 60 秒 |

一轮对手发言的最坏耗时是 HTTP 25 秒加记忆 20 秒，约 45 秒，低于服务端 60 秒；训练位一轮的最坏耗时是 HTTP 60 秒加记忆 20 秒，约 80 秒，低于服务端等提交的 90 秒。两个 90 秒方向相反、互不嵌套：服务端等的是客户端的一次提交，客户端等的是相邻两条服务端消息之间的间隔，每条对手发言都是一条消息，所以一轮里有几个对手都不叠加。

部署路（MindAgent，`--enable agent`）的口径同样要随记忆放宽。它今天用 `AgentRuntime` 的默认值：服务端等提交 5 秒、整条流 15 秒（`agent.py:93,124`），是按罐头后端定的。部署实例接上记忆后每轮最坏多 20 秒，所以：记忆配置在场时，部署会话的服务端等提交改为 40 秒、整条流按 `max_turns × 40` 秒折算；罐头后端在 1 秒内返回，40 秒只放宽、不会让现有测试变慢。不设记忆配置（替身，本地文件）时保持现值。`--interact` 与 `--enable agent` 同开时两路共用一个 `AgentRuntime`（`wire.py:140-160`），服务端等提交只有一份取值：取两者较大的 90 秒，部署会话只是多等。整条流的 deadline 今天也只有一份（`agent.py:245` 的 `DeadlineStub` 用运行时属性），所以改为按调用方给：`AgentRuntime.client_factory(*, deadline_s: float | None = None)`，没给时用运行时的值。轨迹源与部署驱动器各自按自己的 `max_turns × 90` 与 `max_turns × 40` 传；运行时属性 `interact_deadline_s` 保留给既有调用。真实模型接进部署路时再按它的延迟加。

现在客户端只有整条流的 deadline（F22），没有逐轮限时。改法：客户端收消息的 `_next_msg` 加等待上限 90 秒，计的是相邻两条服务端消息之间的间隔（在接收线程里用带超时的队列取消息），超时视作 `timeout`（T1，按条重跑）并 abort 会话；整条流的 deadline 仍按 `max_turns × 90` 折算当兜底。

记忆的最坏合计按调用次数算，不另设总预算：Starling 的 `timeout_ms` 配 4000、`max_retries` 配 0（两者进运行时身份），每次网络调用至多 4 秒。一轮里有五次：`recall` 嵌入查询词一次（S21），`remember` 顺序抽取三次（S20），`embed_pending` 嵌入一批（S16）。对方一轮只说一句，所以 `remember` 只调一次，不处理积压。训练位 HTTP 降到 60 秒，正是为了让 60 加 20 落在 90 秒以内。训练副本每轮都嵌入、从不积压；部署库若有积压（上次失败留下的），在 episode 开始前先嵌完再开始，这段时间不计入任何一轮。SQLite 的 `busy_timeout` 是 5 秒，但每个库只有一个写者（M4、M20），不会等锁。

## 7. 训练路

### 7.1 起点记忆与哈希（M8）

`TrainingConfig` 增加可选字段 `memory`：

| 字段 | 含义 |
|---|---|
| `memory.seed` | 种子 id，`null` 表示每个人格都是空库 |
| `memory.digests` | `{persona_id: 起点摘要}`，派发时算出并写回 |
| `memory.runtime` | `MemoryRuntimeIdentity`，派发时写回 |

- interact 配置的 `memory` **必填**（M8）：`validate_training_config` 对 `mode == "interact"` 且没有 `memory` 的配置给出原因。batch 配置不许有它。没有"不用记忆"的开关，也没有"不写记忆"的开关：所有 interact 角色位都是带记忆的 Actio 实例，每轮读写（M1、M7）。
- 本设计落地前写下的 pending interact run：派发器在算哈希之前先做配置校验（F26），缺 `memory` 的旧配置在那里判 T2，指令 `failed`，原因写明"interact config has no memory section (written before memory support)"。由人重开。这是有意的：旧 run 的 rollout 没有记忆，续上去的 rollout 有，不是同一个实验。
- 训练默认值 `training_defaults.yaml` 的 `interact` 段增加键 `memory`：`{seed}`，`seed` 必填（可为 `null`）。调度器写进配置时 `digests` 与 `runtime` 留空。
- 派发器进入策略上下文之前，按 `seed` 打开每个绑定人格的起点库、算摘要、取运行时身份，写回 `memory.digests` 与 `memory.runtime`。之后才算 `config_hash`。`mindtrainer` 不导入 `actio` 与 `starling`，所以这一步靠组合根注入的可调用对象：`make_mindtrainer_dispatcher` 新增 keyword-only 参数 `memory_resolver: Callable[[str | None, tuple[str, ...]], tuple[dict[str, str], dict]]`，入参是种子 id 与绑定人格 id，返回每个人格的起点摘要与运行时身份。interact 指令没注入它时判 T2，原因点名 `--interact`；它抛的异常一律 T2。
- 续训时重新算摘要与身份；种子库、Starling 版本、抽取或嵌入模型任一变了，`config_hash` 不同，升 T3（F13）。
- 替身路径的 `digests` 是替身摘要、`runtime.backend` 是 `stand-in`，所以换成真 Starling 也会改哈希。
- 替身没有记忆根目录，也就没有种子库：未设 `RISMFORGE_MEMORY_CONFIG` 时 `memory.seed` 必须是 `null`，否则 `memory_resolver` 抛错、指令 T2，原因写明种子需要记忆配置。

### 7.2 每条 rollout 的私有副本（M9）

| 时刻 | 动作 |
|---|---|
| 会话开始 | 对每个角色位：从起点库 `backup_to` 一份私有副本（空起点就新建空库），打开它，构造 Actio 实例 |
| 每轮 | 该位实例 recall、回复、remember、embed_pending |
| 会话终态 | `SessionResources` 按 6.2 节收尾 |

- 训练位的实例在客户端（轨迹源一侧），由策略工厂每条 rollout 新造一个，见 7.3。它的副本目录是 `runs/<run_id>/client/<逻辑 episode id>/<trained_slot>.db`。重试在同一线程里顺序发生：上一次尝试的上下文已在 `finally` 里退出（`close` 等到 Starling 的工作线程退出，第 8 节），所以同名目录先删后建是安全的；`close` 超时则这条 rollout 判 `role_memory_failure`，不重试。
- 对手位的实例在服务端，由后端工厂每个会话新造。
- 同组 `group_size` 条 rollout 的每个角色位都从同一份记忆出发。

### 7.3 训练位

| 部件 | 改动 |
|---|---|
| `VllmPolicy`、`ExternalApiBackend` | 新增 `respond_guided(persona, scenario, history, slot, guidance)`：指导语非空时追加到唯一的 system 消息末尾，以“当前要点：”起头，一行一条（M16） |
| 策略工厂 | 交给编排器的不再是一个后端，而是 `policy_for_rollout(episode_id) -> ContextManager[ModelBackend]`（入参是逻辑 episode id）：每条 rollout 造一个 Actio 实例，退出时关实例 |
| `InteractTrajectorySource` | 参数 `policy` 改为 `policy_for_rollout(episode_id) -> ContextManager[ModelBackend]`；`_attempt` 在 `create_session` 之前进入上下文（副本目录按逻辑 episode id 命名），`finally` 里退出。现有测试里的固定后端改用 `nullcontext` 包一层传入 |
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
| `Memory.recall(..., holder=None)` | 转发到 `MemoryCore.recall` 的 `holder`；另加 `include_volatile=False` 参数，为真时候选含 `volatile` 行（S4：本仓库只嵌入不 `tick`，不放宽就召回不到本轮写的记忆；代价是这些行没经回放去重，召回里可能有近重复）；再加 `strict=False` 参数，为真时 `SemanticResult.degraded`（嵌入查询词失败，`semantic_retriever.cpp:19-31` 今天吞掉异常、返回空结果）改为抛 `RecallDegradedError`。本仓库的适配器永远传 `strict=True, include_volatile=True` |
| `Memory.open(..., embedder=None)` | 注入嵌入器；`None` 维持现状（stub） |
| `make_openai_embedder(*, model, base_url, dim, timeout_ms)` | 构造 `OpenAIEmbeddingAdapter`，与 `make_openai_llm` 同形 |
| `Memory.embed_pending(now) -> EmbedStats` | 只跑嵌入阶段（`EmbeddingWorker.tick_one_batch`），不跑后台维护。返回 `embedded` 与 `failed` 两个计数：嵌入失败今天只在向量表里标 `failed` 并计数（`embedding_worker.cpp:287-293`），不抛 |
| `Memory.digest() -> str` | 第 4.5 节的逻辑摘要，覆盖语句行、向量行与事件表 |
| `Memory.backup_to(path)` | `VACUUM INTO` 加 `PRAGMA integrity_check`，得到自洽的单文件副本 |
| `Memory.close()` | 关闭 SQLite 连接并等待自己的工作线程退出 |
| `Memory.remember_event(key, text, *, interlocutor) -> RememberResult` | 三段：① 短事务读 `events` 表（新迁移，列：`key`、`text`、`interlocutor`、`text_sha256`、`engram_ref`、`statement_ids_json`、`seq`、`state`），同 key 同文本且 `state="committed"` 直接返回 `outcome="idempotent"` 且 `statement_ids` 取事件行里存的那一批，不抽取；同 key 不同文本抛 `EventConflict`；新 key 则在同一事务里写一行 `state="pending"` 的事件行（含原文与 `interlocutor`，`seq` 此时就加一）；② 事务之外调 `remember`（抽取走网络，S17）；③ 抽取成功后短事务把该行改成 `committed` 并填 `engram_ref` 与 `statement_ids`。抽取失败或 ③ 之前崩溃，事件行留在 `pending`，并把这次的 `failure_category` 记在行里。同 key 同文本再调 `remember_event` 时：先按 `engram_ref` 查 Starling 的 engram 表（`remember` 返回的 `engram_ref` 由文本决定，S7），已有语句就直接补成 `committed` 并返回那批 id，没有就用行里存的原文再抽一次。**没有自动恢复**：`pending` 行只在调用方再次对同一 key 调 `remember_event` 时才推进。不存在"语句在库里却没有收据"的终态。与裸 `remember` 的 `idempotent`（`statement_ids` 为空，S19）不同：事件表就是为了把这批 id 存下来 |
| `Memory.next_seq() -> int` | 读 `events` 表的当前 `seq` |
| `Memory.pending_events() -> list[PendingEvent]` | `state="pending"` 的行（`key`、`text`、`interlocutor`、`seq`、`failure_category`），只读，供调用方决定重试还是放弃 |
| `Memory.abandon_event(key) -> None` | 把 `pending` 行改成 `abandoned`（终态，不进摘要的内容部分，只进事件计数）；`committed` 行不许放弃，抛 `EventConflict`。放弃的 key 再 `remember_event` 同样抛 `EventConflict` |

合入后本仓库的 `pyproject.toml` 增加 `memory = ["starling-memory @ git+https://github.com/ruijieguo/starling@<合并提交>"]`。推送分支与开 PR 前先征得用户同意。

## 9. MindAgent（M11）

| 改动 | 内容 |
|---|---|
| 部署记忆 | 每个绑定人格一个 `deploy/<persona_id>.db`，跨轮次持久；`seq` 与事件表在库内。经 `DeploymentMemoryStore`（4.1 节）访问。轮次开始时对每个绑定人格拿 `deploy/<persona_id>.lock` 的独占文件锁，轮次结束释放；拿不到锁的轮次判 `AgentRoundFailedError`（M20）。门② 收录写该人格的库前也拿同一把锁（见下） |
| 实例生命周期 | 部署驱动器不再接收一个现成的实例，改收一个实例工厂 `make_instance(episode_id, memory) -> ActioInstance`。每条 episode 的每次尝试：打开该人格的端口，造实例（`episode_id` 是逻辑 id），跑会话，`close()` 实例（端口随之关闭）。覆盖部署库只在端口关闭之后做：顺序是关实例与端口、`restore_from`、重开端口、新造实例 |
| 轮次产物 | `agent_round` 载荷增加 `memory_runtime` 与 `episode_digests: {episode_id: {persona_id: 起点摘要}}`，每条 episode 开始前写回一次（轮次产物本来就逐步回写） |
| episode 起点与重试 | 每条部署 episode 开始前：把该人格的部署库 `backup_to` 到 `snapshots/<round_id>/<episode_id>-<persona_id>.db` 作起点，记摘要进 `episode_digests` 与首个智能体轮次的轨迹 `memory.digest_before`。该 episode 的任一次尝试失败要重试时，先用起点副本覆盖部署库再重试（M18）。成功时先落盘 episode，再删起点副本；**删起点副本是这条 episode 的提交点**。回滚安全靠 M20：轮次独占人格库、episode 顺序跑（F24），覆盖时没有别的写者 |
| 轮次失败收尾 | 轮次判 `failed`（T3）时，除"覆盖失败"之外，先对在途 episode 用起点副本 `restore_from`，再删副本，再写失败记录；所以失败的轮次不会把半截记忆留给下一轮。覆盖失败时库与副本都保留 |
| 崩溃后重入 | 轮次重入时按 episode 顺序检查：某条 episode 的起点副本还在，说明它在途时进程崩溃（不论 episode 文件有没有落盘），先用起点副本覆盖部署库、核对摘要等于 `episode_digests` 里记的值，再重跑这条 episode，不重新备份起点。起点副本不在而 episode 文件在，说明它已提交 |
| episode 复用 | `_reusable` 今天比较整个 `library_version` 字典（`deployment.py:70-78`），改为只比 `skill` 键，否则一次记忆收录就让全部已提交 episode 重跑。除此之外还要求：该 episode 的起点副本不在（已提交），且首个智能体轮次的 `digest_before` 等于 `episode_digests[episode_id]` 里该人格的摘要。不比较部署库当前摘要：已提交的 episode 写过记忆，库摘要本来就变了 |
| 已提交 episode 不再重跑 | 库版本因记忆收录而变时，已提交的 episode 仍然复用，不重跑：它的事件已经 `committed` 在库里，用同一逻辑 id 重跑会让对手的新发言撞上 `MemoryEventConflict`，召回还会看到自己"未来"的记忆。只有技能版本变了才重跑，且重跑用新的逻辑 id `deploy-{model}-{scenario}-{k}-s{技能版本}`，事件 key 随之不同 |
| 覆盖失败 | 用起点副本覆盖部署库失败（文件系统错误、摘要对不上）时轮次判 `AgentRoundFailedError`，T3；部署库与起点副本都保留，等人处理 |
| 验证重放 | 每个人格从部署库 `backup_to` 到 `snapshots/<round_id>/verify-<persona_id>.db`，基线与候选各开一份快照副本，`replay_read_only=True` |
| 门② 与在途轮次 | 收录写某人格的库之前，先拿该人格的库锁，并检查 `snapshots/` 下有没有该人格未提交的起点副本。锁被占或有未提交副本，就抛 `DeferredError`：守护进程不写树、按退避重试（SYS-23）。否则轮次重入时会用起点副本覆盖部署库，把刚收录的记忆抹掉，而提案已是 `effective`、不会再收录 |
| 门② `kind: memory` | 收录时经 `DeploymentMemoryStore.remember_event` 写该人格的库，`key` 是 `<proposal_id>#<k>`。收据是三段写入（第 8 节）留下的事件行，崩在第三段之前也能按 `engram_ref` 补，所以"写成功但收据没落树"的重入不会二次抽取：重入时再调一次，得到 `idempotent` 与事件行里存的同一批语句 id。语句 id 写进提案载荷的 `admitted_statements` 只作证据，不作幂等依据；真正的收据在 Starling 的事件表里 |
| 门② 失败 | `remember_event` 返回 `failed`（抽取失败）时：先对该 key 调 `abandon_event`（事件行进终态 `abandoned`，以后不会被任何路径推进），再写一条 T2 失败记录，提案改为 `rejected` 并带原因；审核类状态机增加 `approved → rejected` 这一条边（F23），只许收录处理器走。`remember_event` 抛异常（库打不开、③ 写失败）时不改提案状态、不放弃事件，只写失败记录，留在 `approved` 让下一个 tick 重入：重入再调同 key 的 `remember_event`，按 `pending` 行的恢复规则补收据，不会二次抽取，也不会留下没有收据的语句。被拒的提案不会复活：它的事件行已是 `abandoned`。一份提案有多条记忆增量、前几条已 `committed` 而后一条抽取失败时，不转 `rejected`（已提交的事件放弃不了），也不放弃这一条：它留在 `pending`，写一条 T2 失败记录（`partial admission`），处理器抛 `DeferredError`，守护进程不写树、按退避重试；重入时前几条幂等返回收据，失败的那条再抽一次，成功就照常收录完 |
| 记忆增量的形状 | `definition` 是 `{persona_id, text, interlocutor}` 三个必填键，不再是 `MemoryEntry`：`persona_id` 指明写进哪个人格的库；`text` 原样作为事件文本；`interlocutor` 是这条记忆关于谁，缺省为空串表示一般事实。收录前按这三个键严格校验，多键少键都是 T2 |
| 旧数据 | `memory_library/` 必须为空（第 0 节），`load_library` 不再读它 |

## 10. 失败与可观测

| 失败点 | 分类 | 处理 |
|---|---|---|
| 配置要求 Starling 但未安装 | 配置错误 | 装配期退出码 2，信息说明 `pip install '.[memory]'` |
| 记忆根目录的 `.owner` 指向别的仓库 | 配置错误 | 装配期退出码 2，信息写两个路径 |
| `OPENAI_API_KEY` 为空 | 配置错误 | 装配期退出码 2，只写变量名 |
| `memory_library/` 非空 | 配置错误 | 装配期退出码 2，要求人工处理 |
| 起点库或种子打不开、迁移失败、摘要算不出 | T2 | 指令 `failed`，原因只报类名 |
| 续训时起点摘要或运行时身份变了 | T3 | 现有的 `config_hash` 门 |
| 对手位 `MemoryFailureError`，`transient` 为真 | `role_memory_unavailable`，T1 | 驱动器新增分支；轨迹源与部署按条重跑（加进两边的瞬态集合） |
| 对手位 `MemoryFailureError`，`transient` 为假 | `role_memory_failure`，T2 | 驱动器新增分支；轨迹源不重跑；策略上下文把它加进确定性集合，指令判 `failed` |

跨层传递：`simulator` 与 `mindtrainer` 都不导入 `actio`，所以它们不认 `actio.MemoryFailureError`。接缝与现有的对手 API 异常同一做法：

- `simulator/realtime/backend_timeout.py` 新增两个基类 `RoleMemoryUnavailableError`（瞬态）与 `RoleMemoryFailureError`（确定性），驱动器按它们分出两个终止原因。
- 组合根造的每个 Actio 实例（训练位与对手位）外面套一层转换后端（新模块 `factory/memory_translate.py`）：接住 `actio.MemoryFailureError`，按 `transient` 改抛 `FactoryMemoryUnavailableError`（继承 `RoleMemoryUnavailableError`）或 `FactoryMemoryFailureError`（继承 `RoleMemoryFailureError`）。`actio` 的依赖不变。
- 训练位的轨迹源接的是同样两个 `simulator` 基类，所以 `mindtrainer` 也不需要导入 `actio`。瞬态的在 `_attempt` 里译成按条重跑。确定性的不接，让它照现状原样穿出轨迹源（`trajectory_source.py:259`）；编排器的 `_DETERMINISTIC` 元组加上 `RoleMemoryFailureError`，于是训练记录写 `failed` 后重抛（`orchestrator.py:315`）。不加的话，它落不进任何确定性分支，记录保持 `pending`，被当成可续训而反复延后。训练位副本 `close()` 最多等 60 秒（与服务端 `join` 同值），超时按确定性处理。
- MindAgent 自己造部署实例、本来就导入 `actio`，直接接 `actio.MemoryFailureError`；对手位的记忆失败经 `EndSession` 的终止原因字符串到达，按字符串分类。

`MemoryFailureError.transient` 由适配器按穷举映射给出（M21）：

| 来源 | `transient` |
|---|---|
| `recall` 抛 `RecallDegradedError`；`embed_pending` 返回的 `failed` 大于 0 | 真（嵌入端点暂时不可用是最常见的原因；一轮里查询词嵌不出就没有可信的指导语，嵌入失败就让下一轮召回少一条，两者都不能静默） |
| Starling 抛的其余异常、`sqlite3` 异常、`OSError`、任何其他异常 | 假 |
| `MemoryEventConflict`、`MemoryClosedError`（都是 `MemoryFailureError` 的子类） | 假 |

`remember` 返回的 `failure_category` 不进这张表：它只计数（4.3 节），不变成异常。转换后端只接 `MemoryFailureError` 一个基类，所以两个子类也被正确映射，不会落进驱动器的 `role_backend_failure`。
| 训练位 `MemoryFailureError`，瞬态类别 | 按条重跑 | 轨迹源在 `_attempt` 里接住，译成瞬态 `_AttemptFailed`，会话 abort |
| 训练位 `MemoryFailureError`，其余 | 策略异常，T2 | 会话 abort；策略上下文译成 `DirectiveFailedError`，不延后 |
| 抽取失败（`failed` 为真） | 不分类 | 计数进轨迹；这一轮不留记忆 |
| 准入时活动加滞留会话数达上限 | `role_backend_unavailable`，T1 | 客户端按条重跑；日志每分钟报滞留数。现有两个客户端（`trajectory_source.py:253-254`、`deployment.py:93-94`）对创建被拒一律判不可重跑，要改成按终止原因查各自的瞬态集合，否则这条 T1 实际不会重跑 |
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
| 集成 | 部署 episode 第一次尝试留下 `pending` 行后失败，第二次尝试（先 `restore_from`）用同一逻辑 id 成功，不抛 `EventConflict`；成功后库里没有这条 episode 的 `pending` 行 | 替身 `FileTupleStore`、抽取失败注入 |
| 集成 | 第二轮部署用同一 `deploy-{model}-{scenario}-{k}`，事件 key 带轮次前缀，不撞上一轮已 committed 的 key | 替身 `FileTupleStore` |
| 单元 | 嵌入查询词失败时 `StarlingMemory.recall` 抛瞬态 `MemoryFailureError`，不返回空命中；`embed_pending` 有失败也抛 | 假 Starling、真 Starling 各一 |
| 单元 | 两个仓库并发首次使用同一记忆根目录，只有一个创建 `.owner`，另一个以退出码 2 结束 | 两个子进程 |
| 单元 | 部署驱动器与轨迹源同时从一个 `AgentRuntime` 取客户端，各自的流 deadline 是自己传的值 | loopback 服务端 |
| 集成 | 部署实例按 episode 新造：第二条 episode 的事件 key 前缀是它自己的逻辑 id；`restore_from` 在端口未关时抛错 | 替身 `FileTupleStore` |
| 集成 | 门② 与在途轮次：该人格有未提交起点副本时收录抛 `DeferredError`、库不变；副本删掉后下一 tick 收录成功 | 替身 `FileTupleStore`、故障注入 |
| 集成 | 同一逻辑 episode id 仍有活动会话时，第二个 `CreateSession` 以 `role_backend_unavailable` 被拒、客户端按条重跑成功 | loopback 服务端 |
| 集成 | 部署崩溃重入：episode 中途崩溃（起点副本在）重入时先覆盖再重跑；episode 已落盘但起点副本没删时同样重跑；已提交的 episode 被复用、不重跑 | 故障注入、替身端口 |
| 集成 | 同组两条 rollout 从同一起点出发，会话结束副本被删；`session_id` 出现在目录名里 | loopback 服务端、替身端口 |
| 集成 | 起点摘要与运行时身份进 `config_hash`；续训时种子或模型变了升 T3；落地前写下的 pending interact run 在派发器判 T2，原因点名缺 `memory` | 临时仓库 |
| 集成 | `role_memory_failure` 让指令判 `failed` 而不是延后；`role_memory_unavailable` 按条重跑 | 抛异常的假端口 |
| 单元 | interact 配置缺 `memory` 时 `validate_training_config` 给出原因；batch 配置带 `memory` 同样拒绝 | 无 |
| 单元 | 内容 id：同一语句在两个库里得到同一个 `mem-` id；改任一进摘要的列即变 | 真 Starling 或替身 |
| 单元 | `remember_event`：抽取失败事件行留 `pending` 且存原文，重调用原文再抽；同 key 同文本返回 `idempotent` 且 `statement_ids` 与首次相同；同 key 异文本抛 `EventConflict`；语句已落库但 ③ 没写成（模拟崩溃）时重调不再抽取、补成 `committed` 并返回同一批 id；`pending_events` 列出带原文的未完成行；`abandon_event` 后同 key 再调抛 `EventConflict`，`committed` 行不许放弃 | 真 Starling（PR 内） |
| 单元 | 门②：抽取失败后提案 `rejected` 且事件 `abandoned`，之后任何路径都不会把它写进记忆；③ 写失败后提案留 `approved`，重入补收据不二次抽取 | 替身端口 |
| 集成 | 部署 episode 结束后库里没有它的 `pending` 行；下一条 episode 的起点快照里没有别人的 `pending` 行 | 真 Starling 或替身 |
| 单元 | 摘要对 `events.statement_ids_json` 里的 UUID 不敏感：同一逻辑记忆换一批 UUID，摘要不变 | 真 Starling |
| 单元 | 失败映射：每个 `failure_category` 与每类异常都有确定的 `transient` 值 | 假 Starling |
| 集成 | 准入在一把锁里：并发 `CreateSession` 不突破上限；同一 `episode_id` 第二个会话被拒 | loopback 服务端 |
| 集成 | 轮次对人格库加锁：第二个轮次拿不到锁判失败 | 临时目录 |
| 单元 | 门② 记忆提案改走 `remember`；重入不重复写；失败转 `rejected` | 替身端口 |
| 端到端 | `--interact` 一条 run：所有角色位都是带记忆的 Actio，episode 每轮带 v2 轨迹，记录里有 `memory` 块 | 本机 HTTP 替身 vLLM、替身端口 |

需要真 Starling 的用例按 extra 缺失跳过。真抽取与真嵌入不进测试。

## 12. 施工顺序

| 步 | 内容 | 依赖 Starling PR |
|---|---|---|
| 1 | 记忆端口与契约测试、`TupleMemory`、`FileTupleStore`、实例改持端口、每轮读写、轨迹 v2 | 否 |
| 2 | `respond_guided`；后端工厂加 `session_id`；`SessionResources` 与 `TimeoutBackend` 登记；驱动器与 servicer 的终态收尾；`role_memory_failure` | 否 |
| 3 | 训练路换成 Actio：`policy_for_rollout`、对手工厂包 Actio、轨迹上行、时间预算 | 否 |
| 4 | `TrainingConfig.memory`、运行时身份、`config_hash`、`policy_backend.memory`、清扫 | 否（替身） |
| 5 | MindAgent：部署记忆、轮次摘要、快照重放、门② 改路（经 `DeploymentMemoryStore`，先在 `FileTupleStore` 上跑通） | 否（替身） |
| 6 | Starling PR：第 8 节的接口 | 是（在 Starling 仓库） |
| 7 | `StarlingMemory`、`StarlingDeploymentStore`、树外记忆配置、`memory` extra | 是 |
| 8 | 文档与图的收尾 | 否 |

第 1 到 5 步只用替身就能跑通，可以先合：管理面有了落盘的 `FileTupleStore`，MindAgent 一侧不再等 Starling。第 6 步合入后再做第 7 步。

接口变更清单（每一项都在上面的某一步里，列在一处便于对照）：

| 接口 | 变更 | 步 |
|---|---|---|
| `actio.instance.instantiate` | `memories=` 之外加 `memory=`、`replay_read_only=`、`episode_id=`、`interlocutors=`；`ActioInstance.close()` | 1 |
| `actio.trace` | 新增 `actio.trace.v2`，`validate_trace` 接受 v1 与 v2 | 1 |
| `CreateSession` proto | 新增可选 `episode_id` | 2 |
| `BackendsFactory` | `(SessionInfo) -> SessionResources` | 2 |
| `on_session_closed` | `(session_id, episode)` | 2 |
| `TimeoutBackend` | 构造时收 `SessionResources`，登记在途线程 | 2 |
| `EnvironmentServicer` | 准入检查、终态收尾、滞留表 | 2 |
| `SessionDriver` | 新增 `role_memory_unavailable`、`role_memory_failure` 分支 | 2 |
| `EnvironmentServiceClient` | 相邻消息等待上限 | 2 |
| `simulator.realtime.resources` | 新模块：`SessionInfo`、`SessionResources`、`SessionClosingError` | 2 |
| `simulator.realtime.backend_timeout` | 新增 `RoleMemoryUnavailableError`、`RoleMemoryFailureError` 两个基类 | 2 |
| `factory` 的 interact 装配 | `turn_timeout_s` 改传 90 秒；每个 Actio 实例外套记忆异常转换后端 | 3 |
| `InteractTrajectorySource`、`mindagent.deployment` | 创建被拒按终止原因查瞬态集合，不再一律不可重跑 | 3、5 |
| `VllmPolicy`、`ExternalApiBackend` | 新增 `respond_guided` | 2 |
| `InteractTrajectorySource` | `policy` 改为 `policy_for_rollout`；三元组上行 | 3 |
| `make_policy_factory` | 返回 `policy_for_rollout` 工厂 | 3 |
| `make_mindtrainer_dispatcher` | 新增 keyword-only 参数 `memory_resolver` | 4 |
| `controlplane.daemon.run_daemon` | 新增 keyword-only 参数 `on_locked`，取锁后调一次 | 3 |
| `TrainingConfig` | interact 必填 `memory` | 4 |
| `training_defaults.interact` | 必填 `memory` | 4 |
| `TrainingRun.policy_backend` | `memory` 块 | 4 |
| `controlplane.states` | `approved → rejected`，只许收录处理器走 | 5 |
| `cli/src/commands.ts` 的 `REVIEW_TRANSITIONS` | 不加这条边：命令行的 `reject` 仍只能从 `pending_review` 出发，人不能把已批准的提案改回拒绝。两份迁移表从此有意不同，`controlplane.states` 里写明这一条是 Python 独有的 | 5 |
| `agent_round` 载荷 | `memory_runtime`、`episode_digests` | 5 |
| 记忆提案 `definition` | `{persona_id, text, interlocutor}` 三个必填键 | 5 |

## 13. 已定与未核实

已定：

1. 对手位与训练位都是带记忆的 Actio 实例（M1）。
2. 每轮都读写（M7），每条 rollout 同一起点、用完即弃（M9）。
3. 抽取与嵌入走 OpenAI 兼容端点，密钥 `OPENAI_API_KEY`（M12）。
4. Starling 的缺口经 PR 补（M14）。

未核实：

| 项 | 核实办法 |
|---|---|
| 真抽取与真嵌入的单次延迟是否在 4 秒内 | 接真端点跑 10 轮，记每次 recall、remember（分三段）、embed_pending 的耗时；超时的会落成抽取失败或 `role_memory_unavailable` |
| 真嵌入下 `recall` 的命中质量 | 用 SoMBench 之外的手写情境，看召回句是否相关 |
| `VACUUM INTO` 副本与原库在 Starling 迁移版本不同时能否打开 | 在 PR 里加用例：旧版本库备份后用新版本打开 |
| 每会话开一份库对 GRPO 一步的总时长影响 | 一步 `groups_per_step × group_size` 条 rollout，计开库、备份、删除总耗时 |
| 不跑 `tick` 时承诺、共同点等分区不更新，对召回质量的影响 | 同一组对话分别跑 `tick` 与只嵌入，比较召回 |
| 外部端点同模型名换了向量时，摘要会变、续训升 T3，但不能复现旧向量；要复现需缓存向量 | 先观察一个月内同一端点的向量是否稳定，再决定要不要加向量缓存 |
