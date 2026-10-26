# 设计二：每个角色位都是带记忆的智能体，记忆用 Starling

状态：修订稿，待审阅。日期：2026-10-26（第一稿写于 2026-10-25）。
基线：rise `705236c`，Starling `48e9f3b`（`ruijieguo/starling` main，公开仓库，Apache-2.0）。
权威文档集是 `docs/design/`。本稿审定后并入 00、20、30、40、50、90 六篇，然后写施工计划。

## 0. 这份文档做什么

场景二（实时交互训练）里，**每个角色位都是一个智能体**：一个 Actio 实例包着一个底座模型，带技能、经验和记忆。

- 训练位智能体的底座是正在训练的模型，由 MindTrainer 训练。
- 对手位智能体的底座按驱动来：固定对话、站内 vLLM、外部 OpenAI 兼容 API。
- 记忆是个体的。每个人格（persona）一份第一人称记忆，存在 Starling 里。
- 记忆让对手位能准确扮演某种人格的个体：它记得刚才发生的事，形成信念，并据此回应。

MindAgent 是另一条线：底座不动参数，演化技能库与记忆库。它与训练没有关系，两条线共用同一套 Actio 与记忆端口。

本稿管四件事：

1. Actio 抽出记忆端口，Starling 是生产实现，现有 `MemoryEntry` 是替身。
2. 训练路的所有角色位都换成 Actio 实例，每轮读记忆、写记忆。
3. 记忆参与训练时可复现：起点记忆钉进 `config_hash`，每条 rollout 从同一起点出发。
4. Starling 补齐本仓库需要的公开接口（经 PR 合入 Starling）。

不做：

- 对 Actio-RSI（`ChaunceyKung/Actio-RSI-infra`）的兼容。
- 把记忆库放进 git 产物树。
- 跨租户共享记忆。
- 把 Starling 的语义检索用于诊断评测。
- 诊断评测的动态轨与批量数据集生产（`simulator.dataset`）换成智能体。两者仍用裸后端，单列为后续工作。

## 1. 事实依据

### 1.1 本仓库（`705236c`）

| 编号 | 事实 | 位置 |
|---|---|---|
| F1 | Actio 的记忆是内存元组 `MemoryEntry` 加词面检索；`decide` 每次调 `retrieve(self.memories, …)` | `src/actio/memory.py`、`src/actio/instance.py:108-113` |
| F2 | Actio 实例只在 MindAgent 里构造：部署轮次与验证重放 | `src/mindagent/rounds.py:244` |
| F3 | 训练位是裸的 `VllmPolicy`，只有 `respond`；轨迹源调 `policy.respond` 后 `submit_turn(utterance, thought)`，不带激活轨迹 | `src/factory/vllm_policy.py:31-60`、`src/mindtrainer/trajectory_source.py:260-262` |
| F4 | 对手位是裸后端：`ScriptedBackend`、`VllmPolicy`、`ExternalApiBackend`，由 `AgentRuntime._backends` 每个会话新造 | `src/factory/agent.py:200-224` |
| F5 | `as_guided` 给只有 `respond` 的后端套 `IgnoreGuidance`，指导语被丢弃 | `src/actio/instance.py:60-67` |
| F6 | 服务端给每个对手后端套 `TimeoutBackend`（默认 60 秒），它只认 `respond` 并原样返回结果；运行器接受二元组或三元组 | `src/simulator/realtime/servicer.py:286-288`、`src/simulator/runner.py:21-33` |
| F7 | 服务端下发给客户端的对手轮次抹掉 `inner_thought` 与 `activation_trace`；落盘的 episode 保留完整记录 | `src/simulator/realtime/servicer.py:312-316` |
| F8 | 会话正常结束触发 `on_session_closed(episode)`；中途断开不触发 | `src/simulator/realtime/servicer.py:109,327` |
| F9 | 激活轨迹校验要求 `memory_hits` 每项以 `mem-` 开头、不重复 | `src/actio/trace.py:79` |
| F10 | `simulator` 不导入 `actio`，已成立 | `grep` 为零 |
| F11 | MindAgent 从不生成记忆提案；`kind: memory` 只来自手写提案 | `src/mindagent/proposals.py:6` |
| F12 | 门②收录 `kind: memory` 时写 `memory_library/` | `src/mindagent/admit.py:41-43` |
| F13 | 续训时 `config_hash` 不同即升 `NeedsHumanError`（T3） | `src/mindtrainer/orchestrator.py:183-187` |
| F14 | 部署 episode 复用的判据是每个智能体轮次的 `library_version` 与当前相同 | `src/mindagent/deployment.py:70-78` |
| F15 | 树外本机配置的现成做法：路径由环境变量给，必须在仓库外且权限 0600，报错不回显取值 | `src/factory/eval_config.py` |

### 1.2 Starling（`48e9f3b`，本地构建实测）

| 编号 | 事实 | 实测或位置 |
|---|---|---|
| S1 | `uv pip install <checkout>` 用 scikit-build-core 构建，自动拉 cmake 与 ninja，本机 179 秒 | 实测 |
| S2 | 公开 `Memory.recall(query, *, perspective, k, mode)` 不收 `holder`；私有 `MemoryCore.recall` 收 | `python/starling/memory.py:168`、`_memory_core.py:250` |
| S3 | `remember` 要求注入 LLM，否则抛 `LLMNotConfigured`；`make_openai_llm` 的密钥由 C++ 读 `OPENAI_API_KEY` | `_memory_core.py:183-187`、`memory.py:48-70` |
| S4 | `remember` 写入的语句要等 `tick()` 嵌入后才能被 `recall` 命中 | 实测：tick 前 0 条，tick 后 1 条 |
| S5 | 门面固定用 8 维 stub 嵌入器，命中分数为 -0.19，语义检索无意义；真嵌入器是 `OpenAIEmbeddingAdapter`，密钥同样读 `OPENAI_API_KEY` | `memory.py:147-148`、`src/embedding/openai_embedding_adapter.cpp:35` |
| S6 | `StatementRow` 暴露 `id`（UUID）、`holder_id`、`subject_id`、`predicate`、`object_value`、`modality`（小写，如 `believes`）、`polarity`、`confidence` 等；**没有** `nesting_depth`（表里有该列） | 实测 |
| S7 | `remember` 的幂等只看文本：同一段文字再写，换了 `now`、`interlocutor`、`holder` 都返回 `outcome="idempotent"`，不新增语句 | 实测 |
| S8 | `recall` 不改语句行（`access_count`、`activation`、`updated_at` 不变） | 实测 |
| S9 | 库文件字节不稳定：每次新建空库字节不同；`close` 不合并 WAL | 实测 |
| S10 | `VACUUM INTO` 在库打开时可执行，得到自洽的单文件副本（约 0.6 MB，3 毫秒）；副本可直接 `Memory.open` 且记忆完整 | 实测 |
| S11 | 两个 `Memory` 分别开两个文件，互不干扰 | 实测 |
| S12 | 空库 `Memory.open` 约 20 毫秒；替身抽取下 `remember` 4 毫秒、`tick` 2 毫秒 | 实测 |
| S13 | Starling 自己的世界快照用 `VACUUM INTO` 加 `integrity_check`（C++ `take_snapshot`），只服务看板 | `src/governance/world_store.cpp:828` |

## 2. 裁决总表

| 编号 | 裁决 | 理由 | 判错的代价 |
|---|---|---|---|
| M1 | 训练路的每个角色位都是 Actio 实例；底座按角色位的后端定 | 用户裁定：训练位与对手都是智能体，对手要靠记忆扮演个体 | 每轮多一次 recall 与 remember，会话变慢 |
| M2 | `simulator` 仍只依赖 `ModelBackend`，不新增协议方法，不导入 `actio` | Actio 实例本来就满足 `respond`；构造在组合根 | 无 |
| M3 | 记忆在 Actio 内部，经记忆端口访问；只有 `actio/memory_starling.py` 导入 `starling` | 记忆属于个体，不属于环境 | Actio 多一个可选依赖 |
| M4 | 一个智能体一个 Starling 库文件，库的 `agent` 与 holder 都是该人格 | 个体隔离靠文件，不靠 holder 过滤；同时多开文件已实测 | 文件多 |
| M5 | 记忆库在树外，根目录由树外配置给 | 原生 SQLite 带 WAL，不适合当 git 产物 | 不能从产物树追溯，靠摘要补 |
| M6 | 记忆状态的身份是**逻辑摘要**：对规范化的语句行算 sha256，与文件字节无关 | 文件字节不稳定（S9） | 摘要不覆盖嵌入向量与看板表 |
| M7 | 默认每轮都读写：回复前 recall，对方每句话 remember，之后 tick | 用户裁定（维持原 M8） | 每轮多一次抽取调用 |
| M8 | 训练 run 有起点记忆，默认每个人格是空库；起点摘要进 `config_hash` | 记忆变了行为就变；不钉住续训门就失效 | 换种子库就要新开 run |
| M9 | 每条 rollout 在起点的私有副本上跑，用完即弃 | 用户裁定：同组 rollout 的起点相同，组内优势只反映策略 | 记忆不在一条 run 内累积 |
| M10 | 部署（MindAgent）的记忆是持久的，跨轮次累积；验证重放在快照副本上只读 | 部署记忆是演化对象；重放要让基线与候选看到同一份记忆 | 重放与线上行为不完全一致 |
| M11 | MindAgent 的 `kind: memory` 收录改为调 `remember`，不写 `memory_library/` | 记忆只有一个权威来源 | 门②的证据要从 Starling 读回 |
| M12 | 抽取与嵌入都走 OpenAI 兼容端点，`base_url` 与模型名写配置；密钥是 Starling 固定读的 `OPENAI_API_KEY` | 用户裁定；Starling 只认这个变量（S3、S5） | 不能为抽取和嵌入配不同的密钥 |
| M13 | Starling 作为可选 extra `memory`，依赖钉 git 提交；未装时用替身并显式报告 | C++ 构建门槛；仓库未上 PyPI | 未装环境只测替身路径 |
| M14 | Starling 缺的公开接口在 Starling 里补，经 PR 合入，本仓库钉合并提交 | 用户裁定；本仓库不碰 Starling 私有接口与表结构 | 依赖 Starling 先合 PR |

## 3. 分层

```
simulator.datamodel          纯数据模型
   ↑            ↑
actio        simulator       simulator 只认 ModelBackend
  └ memory_starling → starling（可选 extra）
   ↑
mindtrainer（只认 ModelBackend）· mindagent · factory（组合根，构造 Actio 实例）
```

- `mindtrainer` 不导入 `actio`：轨迹源只知道策略返回二元组或三元组（第 6.3 节）。
- `factory` 构造所有训练路的 Actio 实例：训练位交给轨迹源，对手位交给服务端的后端工厂。
- `mindagent` 继续自己构造部署实例，改为经记忆端口取记忆。

## 4. Actio 的记忆端口

### 4.1 端口

```python
class MemoryBackend(Protocol):
    def recall(self, query: str, *, limit: int) -> list[MemoryHit]: ...
    def remember(self, text: str, *, interlocutor: str | None, now: str) -> RememberOutcome: ...
    def digest(self) -> str: ...          # 逻辑摘要,sha256 十六进制
```

`MemoryHit` 是 `(id, text)`：`id` 以 `mem-` 开头（满足 F9），`text` 是渲染好的一句。`RememberOutcome` 是 `(stored, failed, category)`。一个端口实例只属于一个智能体，所以签名里没有 holder。

| 实现 | 文件 | 用途 |
|---|---|---|
| `TupleMemory` | `actio/memory.py` | 包现有 `MemoryEntry` 元组与 `retrieve`；`remember` 只追加到内存列表 |
| `StarlingMemory` | `actio/memory_starling.py` | 包一个 `starling.Memory`；唯一导入 `starling` 的文件 |

`TupleMemory` 也是部署时未装 Starling 的退化路径，见第 9 节。

### 4.2 实例改持端口

| 改动 | 现状 | 改后 |
|---|---|---|
| 字段 | `memories: tuple[MemoryEntry, ...]` | `memory: MemoryBackend` |
| `decide` | `retrieve(self.memories, q, limit)` | `self.memory.recall(q, limit=self.memory_limit)` |
| `respond` 之后 | 无 | `memory_write=True` 时对新近的对方发言 `remember`，再 `tick` |
| `instantiate` | `memories=` | `memory=`；保留 `memories=` 作为 `TupleMemory` 的简写 |

`render_guidance` 改收 `list[MemoryHit]`，记忆行写成 `[memory:<id>] <text>`。

### 4.3 读写时机（M7）

- **读**：每次 `respond` 前，查询词是对方最后一句（现有 `_memory_query`），`limit` 默认 3。
- **写**：每次 `respond` 后，把上次回复之后对方说的每句话按顺序交给 `remember`，`interlocutor` 是说话方的人格 id；然后 `tick` 一次。
- **写入的文本**：`<scenario_id>#<turn_index> <说话方人格 id>：<utterance>`。Starling 的幂等只看文本（S7），带上来源后，同一句话在别的情境或别的轮次出现时照样记下；同一会话重跑同一轮则幂等，不重复写入。
- **自己的话**：不写。它已在对话历史里，Starling 的抽取面向别人的言行。
- **抽取失败**：`RememberOutcome.failed` 为真时这一轮不留该条记忆，计数加一，不抛到 `respond` 之外。
- **`recall` 或 `remember` 抛异常**：原样上抛，由调用方按角色位后端故障处理（第 9 节）。

`now` 由实例按“情境时钟”给出：固定起点 `2026-01-01T00:00:00Z` 加轮次序号乘一秒。同一条 rollout 的时间戳与墙钟无关，摘要里的 `observed_at` 因而可复现。

### 4.4 渲染（S6）

`StarlingMemory.recall` 把每个 `{row, score}` 渲染成：

```
[memory:mem-<row.id>] <subject_id> <predicate> <object_value>（<modality>，置信度 <confidence>）
```

否定语句（`polarity` 为负）在谓词前加“不”。`StatementRow` 没有 `nesting_depth`，不渲染嵌套层次；“我认为你认为”由 Starling 的谓词与主体表达。

### 4.5 逻辑摘要（M6）

`digest()` 调 Starling 新增的公开 `Memory.digest()`（第 7 节）。摘要的输入是本 tenant 下所有语句行的规范化 JSON：

| 进摘要 | 不进摘要 |
|---|---|
| `holder_id`、`subject_kind`、`subject_id`、`predicate`、`object_kind`、`object_value`、`modality`、`polarity`、`confidence`、`observed_at`、`valid_from`、`valid_to`、`review_status` | `id`（UUID，每次新造）、`created_at`、`updated_at`、`access_count`、`activation`、嵌入向量 |

行按规范化后的 JSON 排序，再整体算 sha256。空库的摘要是固定值。`TupleMemory.digest()` 对条目 `to_dict()` 的排序列表算同样形状的 sha256。

## 5. 记忆库的位置与配置

### 5.1 树外配置（M5、M12）

环境变量 `RISMFORGE_MEMORY_CONFIG` 给出 YAML 文件的绝对路径，文件必须在仓库外、权限 0600，做法同评测配置（F15）。

| 键 | 必填 | 含义 |
|---|---|---|
| `root` | 是 | 记忆库根目录，绝对路径，在仓库外 |
| `extraction_base_url`、`extraction_model` | 是 | 抽取用的 OpenAI 兼容端点与模型 |
| `embedding_base_url`、`embedding_model`、`embedding_dim` | 是 | 嵌入用的端点、模型与维度 |
| `timeout_ms` | 否 | 抽取与嵌入的单次上限，默认 20000 |

配置里不放密钥。启动时检查 `OPENAI_API_KEY` 非空，报错只写变量名。配置不存在时，训练路与部署路都用替身，并在启动日志与 `training_run.policy_backend` 里写 `memory: "stand-in"`。

### 5.2 目录布局

```
<root>/
  deploy/<persona_id>.db                    部署记忆,持久,跨轮次累积
  seeds/<seed_id>/<persona_id>.db           训练起点种子库,只读
  runs/<run_id>/<slot_id>-<attempt>.db      一条 rollout 的私有副本,会话结束即删
  snapshots/<round_id>/<persona_id>.db      部署轮次的验证快照,轮次结束即删
```

库文件名用人格 id，因为人格才是“个体”。同一个人格在两个角色位同时出现时，两个位各开一份副本，互不可见。

### 5.3 部署形态

| 形态 | 实例在哪 | 库在哪 |
|---|---|---|
| 现状：全部组件一台机器 | 开发机 | 开发机上 `root` 指的目录 |
| 目标：模拟器在计算层 | GPU 主机 | 同一台主机上 `root` 指的目录 |

库必须和实例同机，否则每轮 recall 都跨机往返。

## 6. 训练路

### 6.1 起点记忆（M8）

`TrainingConfig` 增加可选字段 `memory`：

| 字段 | 含义 |
|---|---|
| `memory.seed` | 种子 id，`null` 表示每个人格都是空库 |
| `memory.digests` | `{persona_id: 起点摘要}`，派发时算出并写回 |
| `memory.write` | 是否每轮写，默认 `true` |

- `memory` 为 `None` 时 `to_dict` 不写这个键，旧配置的 `config_hash` 逐字节不变。
- 训练默认值 `training_defaults.yaml` 的 `interact` 段增加可选键 `memory_seed`，调度器照搬进配置。
- 派发器进入策略上下文之前，按 `seed` 打开每个绑定人格的起点库、算摘要、写回 `memory.digests`。之后 `config_hash` 包含摘要。
- 续训时重新算摘要；种子库被改过，摘要不同，`config_hash` 不同，升 T3（F13）。

### 6.2 每条 rollout 的私有副本（M9）

| 时刻 | 动作 |
|---|---|
| 会话开始 | 对每个角色位：从起点库 `backup_to` 一份私有副本（空起点就新建空库），打开它，构造 Actio 实例 |
| 每轮 | 该位实例 recall、回复、remember、tick |
| 会话结束（正常、失败、断开都一样） | 关闭副本并删除 |

- 训练位的实例在客户端（轨迹源一侧），由策略工厂每条 rollout 新造一个，见 6.3。
- 对手位的实例在服务端，由 `AgentRuntime._backends` 每个会话新造。会话结束时服务端调后端的可选 `close()`（第 6.4 节）。
- 副本用完即弃，所以一条 run 内记忆不累积。同组 `group_size` 条 rollout 的每个角色位都从同一份记忆出发。

### 6.3 训练位

| 部件 | 改动 |
|---|---|
| `VllmPolicy` | 新增 `respond_guided(persona, scenario, history, slot, guidance)`：指导语非空时作为第二条 system 消息放在人格消息之后 |
| 策略工厂 | 交给编排器的策略从“一个 `VllmPolicy`”改为“每条 rollout 造一个 Actio 实例”的工厂 |
| `InteractTrajectorySource` | 收 `policy_for_rollout: Callable[[Scenario, int], ContextManager[ModelBackend]]`；每条 rollout 进一次上下文 |
| 提交 | 策略返回三元组时，第三项作为 `activation_trace` 随 `submit_turn` 上行 |

`mindtrainer` 不导入 `actio`：它只认 `ModelBackend` 的二元组或三元组。旧的单一 `policy` 参数保留，内部包成“每条 rollout 返回同一个后端”的工厂，现有测试不改。

### 6.4 对手位

| 驱动 | 底座 | 改后的后端 |
|---|---|---|
| `scripted` | `ScriptedBackend` | Actio 实例包 `ScriptedBackend`；记忆照读照写，台词不受影响 |
| `vllm` | `VllmPolicy`（适配器 = `model_ref`） | Actio 实例包 `VllmPolicy`，指导语经 `respond_guided` 进 prompt |
| `external_api` | `ExternalApiBackend` | Actio 实例包它；`ExternalApiBackend` 同样新增 `respond_guided` |

- 对手位的技能与经验在本稿里为空：现阶段只有 MindAgent 产出技能，训练路不读技能库。
- 部署会话（没登记给训练 run）的对手位同样换成 Actio 实例，底座仍是原来的 `ScriptedBackend`，记忆是每会话一份空库，用完即弃。部署里演化的是客户端位的智能体，对手的记忆不跨会话留存。
- `ScriptedBackend` 只有 `respond`，套上 Actio 后指导语被忽略（F5），台词与现在逐字相同；变化只是对手轮次多了激活轨迹，`_reusable` 与分析器只看智能体位，不受影响。
- 服务端：`TimeoutBackend` 新增 `close()` 转发；会话驱动器在终态（`CLOSED`、`FAILED`）时调每个后端的 `close()`。`close()` 只在后端有该方法时调，异常记日志后吞掉。
- 对手的激活轨迹随落盘 episode 保存（F7），不下发给训练位。

### 6.5 训练记录

`training_run.policy_backend` 增加 `memory` 块：`{"backend": "starling" | "stand-in", "starling_commit": "<sha>", "seed": <id 或 null>, "digests": {persona_id: sha}}`。读记录的人不必回到配置就知道记忆从哪来。

## 7. Starling 的 PR（M14）

在 `ruijieguo/starling` 开分支，只改 Python 门面与绑定，每项带测试：

| 接口 | 作用 |
|---|---|
| `Memory.recall(..., holder=None)` | 转发到 `MemoryCore.recall` 的 `holder` |
| `Memory.open(..., embedder=None)` | 注入嵌入器；`None` 维持现状（stub） |
| `make_openai_embedder(*, model, base_url, dim, timeout_ms)` | 构造 `OpenAIEmbeddingAdapter`，与 `make_openai_llm` 同形 |
| `Memory.digest() -> str` | 第 4.5 节的逻辑摘要 |
| `Memory.backup_to(path)` | `VACUUM INTO` 加 `PRAGMA integrity_check`，得到自洽的单文件副本 |

合入后本仓库的 `pyproject.toml` 增加 `memory = ["starling-memory @ git+https://github.com/ruijieguo/starling@<合并提交>"]`。推送分支与开 PR 前先征得用户同意。

## 8. MindAgent

| 改动 | 内容 |
|---|---|
| 部署记忆 | 每个绑定人格一个 `deploy/<persona_id>.db`，跨轮次持久；轮次开始时记下每个库的摘要 |
| 轮次产物 | `agent_round` 载荷增加 `memory_digests: {persona_id: sha}` |
| episode 复用 | `_reusable` 除库版本外，还要求 episode 的 `memory_digests` 与当前相同；不同就重跑 |
| 验证重放 | 每个人格从部署库 `backup_to` 到 `snapshots/<round_id>/`，基线与候选各开一份快照副本，`memory_write=False` |
| 门② `kind: memory` | 收录时对该人格的部署库 `remember(<渲染后的陈述>)`，并 `tick`；不再写 `memory_library/` |
| 门② 失败 | `remember` 抛异常或 `failed` 时写一条 T2 失败记录，提案留在 `approved`（与现有数据错误同一形状） |
| 旧数据 | `memory_library/` 里已有的条目由 `load_library` 读出，在部署库为空时作为种子 `remember` 一次 |

## 9. 失败与可观测

| 失败点 | 分类 | 处理 |
|---|---|---|
| 配置要求 Starling 但未安装 | 配置错误 | 装配期退出码 2，信息说明 `pip install '.[memory]'` |
| `OPENAI_API_KEY` 为空 | 配置错误 | 装配期退出码 2，只写变量名 |
| 起点库或种子打不开、迁移失败 | T2 | 指令 `failed`，原因只报类名 |
| 摘要算不出 | T2 | 指令 `failed`：复现前提不成立 |
| 续训时起点摘要变了 | T3 | 现有的 `config_hash` 门 |
| 对手位 `recall` 或 `remember` 抛异常 | 归 `role_backend_failure` | 服务端现有分支；轨迹源不重跑 |
| 训练位 `recall` 或 `remember` 抛异常 | 策略异常 | 会话 abort，原样上抛，按 Ruling 27 译成延后 |
| 抽取失败（`failed` 为真） | 不分类 | 计数，这一轮不留记忆；计数写进 episode 元数据 |
| 副本删除失败 | 不分类 | 记日志；下次派发前清扫 `runs/<run_id>/` |

异常信息不含端点地址与密钥：适配器只报异常类名与 `failure_category`。

## 10. 测试策略

| 层次 | 对象 | 替身 |
|---|---|---|
| 边界 | `simulator`、`mindtrainer` 不导入 `actio` 与 `starling`；`actio` 只在 `memory_starling` 导入 `starling` | 扫导入、子进程导入检查 |
| 单元 | `TupleMemory` 的 recall、remember、digest | 无 |
| 单元 | `Actio` 每轮读写：recall 在回复前，remember 只收对方发言，抽取失败不抛 | 记录调用的假端口 |
| 单元 | `VllmPolicy.respond_guided` 与 `ExternalApiBackend.respond_guided` 的消息形状 | 本机 HTTP 替身 |
| 单元 | 摘要：同一逻辑内容不同文件字节时相同；改一条语句即变 | 真 Starling（未装时跳过） |
| 集成 | `StarlingMemory`：remember、tick、recall、渲染 | 真 Starling，`make_stub_llm` 抽取，stub 嵌入 |
| 集成 | 同组两条 rollout 从同一起点出发；会话结束副本被删 | loopback 服务端、替身端口 |
| 集成 | 起点摘要进 `config_hash`；续训时种子变了升 T3 | 临时仓库 |
| 集成 | 服务端会话终态调对手后端的 `close()`，含断开路径 | loopback 服务端 |
| 单元 | 门② 记忆提案改走 `remember`；失败留 T2 | 替身端口 |
| 端到端 | `--interact` 一条 run：所有角色位都是带记忆的 Actio，episode 每轮带激活轨迹，记录里有 `memory` 块 | 本机 HTTP 替身 vLLM、替身端口 |

需要真 Starling 的用例按 extra 缺失跳过。真抽取与真嵌入不进测试。

## 11. 施工顺序

| 步 | 内容 | 依赖 Starling PR |
|---|---|---|
| 1 | Actio 记忆端口、`TupleMemory`、实例改持端口、每轮读写、`render_guidance` 改形 | 否 |
| 2 | `VllmPolicy` 与 `ExternalApiBackend` 的 `respond_guided`；服务端 `close()` 钩子 | 否 |
| 3 | 训练路换成 Actio：策略按 rollout 造实例、对手工厂包 Actio、轨迹上行；替身记忆 | 否 |
| 4 | `TrainingConfig.memory`、起点摘要进 `config_hash`、`policy_backend.memory` | 否（替身摘要） |
| 5 | Starling PR：五个公开接口 | 是（在 Starling 仓库） |
| 6 | `StarlingMemory`、树外记忆配置、`memory` extra、私有副本与清扫 | 是 |
| 7 | MindAgent：部署记忆、轮次摘要、快照重放、门② 改路 | 是 |
| 8 | 文档与图的收尾 | 否 |

第 1 到 4 步只用替身就能跑通，可以先合。第 5 步合入后再做 6、7。

## 12. 已定与未核实

已定：

1. 对手位与训练位都是带记忆的 Actio 实例（M1）。
2. 每轮都读写（M7），每条 rollout 同一起点、用完即弃（M9）。
3. 抽取与嵌入走 OpenAI 兼容端点，密钥 `OPENAI_API_KEY`（M12）。
4. Starling 的缺口经 PR 补（M14）。

未核实：

| 项 | 核实办法 |
|---|---|
| 真抽取的延迟：每轮 `remember` 会不会让一轮超过 30 秒的客户端限时或 60 秒的后端限时 | 接真端点跑 10 轮，记每轮 remember 与 tick 的耗时 |
| 真嵌入下 `recall` 的命中质量 | 用 SoMBench 之外的手写情境，看召回句是否相关 |
| `VACUUM INTO` 副本与原库在 Starling 迁移版本不同时能否打开 | 在 PR 里加用例：旧版本库备份后用新版本打开 |
| 每会话开一份库对 GRPO 一步的总时长影响 | 一步 `groups_per_step × group_size` 条 rollout，计开库、备份、删除总耗时 |
