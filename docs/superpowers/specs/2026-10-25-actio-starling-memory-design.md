# Actio 与记忆选型设计:智能体协议可扩展、记忆换成 Starling

状态:草案,待审阅。日期:2026-10-25。
前置设计:`2026-10-09-actio.md` 对应的 Actio 工作包、`2026-10-09-mindagent.md`。
配套设计:`2026-10-24-interact-train-wiring-design.md`(场景二接通)。两者互不依赖,可分别落地。
权威文档集是 `docs/design/`。本稿审定后并入其中的总览、SocialSimulator、Actio、MindAgent、MindTrainer 五篇,再写施工计划。

## 0. 这份文档做什么

三件事:

1. 把 `simulator` 依赖的"角色智能体"从一个具体包改成一个协议。本项目仍以 `src/actio` 为唯一实现,但以后接第二、第三个智能体系统只加实现,模拟器不改。
2. 把 `actio` 内部的记忆换成 Starling(`ruijieguo/starling`),每个 agent 一份独立记忆,记忆在 `actio` 内部,`simulator` 看不到 Starling。
3. 让记忆在训练里可复现:回合开始时取记忆指纹,写进训练记录并并入 `config_hash`。

明确不考虑:对 Actio-RSI(`ChaunceyKung/Actio-RSI-infra`)的兼容适配。它是未完工项目,接口与架构可能大改。

不做:改模型权重、把 Starling 的语义检索用于诊断、跨租户记忆共享、把记忆库放进 git 产物树。

## 1. 事实依据

### 1.1 本仓库

1. `actio` 的记忆现在是内存元组:`MemoryEntry` 冻结数据类加词面重叠检索(`src/actio/memory.py:14,109-133`),`ActioInstance.decide` 每次调用 `retrieve(self.memories, query, limit=…)`(`src/actio/instance.py:108-113`),指导语由 `render_guidance` 拼成 `[memory:<id>] 主体: 内容`(`:71-88`)。
2. 记忆条目由 MindAgent 从树里读出后传进来:`mindagent.library.load_library` 读 `skill_library/`、`memory_library/` 与 `library_state.yaml`(`src/mindagent/library.py:60-77`),`actio.instantiate(memories=…)` 接收元组(`src/actio/instance.py:134-152`)。
3. 记忆提案经审核门②收录:`mindagent.admit` 把 `increments` 里的 `kind: memory` 写成 `MemoryEntry` 并落进 `memory_library/`(`src/mindagent/admit.py:27-46,79-103`)。
4. `actio` 六个模块导入 `simulator.datamodel`;设计文档明令 `simulator` 不得导入 `actio`(`docs/design/40-actio.md:223`),而总览的上下文图已画了 `SocialSimulator → Actio`(`docs/design/00-overview.md:120`)。文档与代码分层不一致。
5. `run_episode` 每轮按 `binding.slot_id` 取后端,后端返回二元组或三元组都接受(`src/simulator/runner.py:60-110`);`ActioInstance.respond` 返回三元组(`src/actio/instance.py:117-136`),所以实例可以直接当后端用。
6. 引用 `MemoryEntry` 的文件共 8 个:`actio/instance.py`、`actio/memory.py`、`mindagent/admit.py`、`mindagent/library.py`,以及四个测试文件,合计 40 处。

### 1.2 Starling(读过仓库,版本 0.0.1)

7. Python 门面(`python/starling/memory.py`):

```python
Memory.open(db_path, *, agent="self", tenant_id="default", llm=None, extraction=None)
remember(text, *, holder=None, interlocutor=None, now=None) -> RememberResult
recall(query, *, perspective="first_person", k=10, mode="semantic") -> list[{"row": StatementRow, "score": float}]
query(text="", *, intent=…, perspective=…, …) -> {entries, context_pack, abstained, …, receipt}
render_working_set(interlocutor, *, goal=None, token_budget=2000) -> ContextBlock
tick(now) -> TickStats
close()
```

8. `remember` 必须配 LLM:没有适配器时抛 `LLMNotConfigured`(`python/starling/_memory_core.py:183-190`)。抽取走三条管线(信念 / 情节 / 一般事实),由 C++ 统一执行。
9. `record` 的语义单位是 Statement:`holder`、`subject`、`predicate`、`object`、`modality`、`polarity`、`confidence`、`observed_at`、`nesting_depth` 等;`recall` 返回的 `StatementRow` 在 Python 侧暴露 `id`、`tenant_id`、`holder_id`、`holder_perspective`、`subject_kind`、`subject_id`、`predicate`、`object_kind`、`object_value`、`modality`、`polarity`、`confidence`、`observed_at`、`consolidation_state`、`review_status`、`evidence_json`、`affect_json`、`nesting_depth`(`bindings/python/bind_05_retrieval.cpp:237-260`、`include/starling/retrieval/statement_row.hpp`)。
10. `ContextBlock` 与 `WorkingBlock` 已绑定,`ContextBlock.render()` 输出 prompt 文本;分区固定为 `pending_commitments > persona > common_ground > relevant_memories > affect`(`include/starling/hippocampus/working_set.hpp`、`bindings/python/bind_09_brain_dynamics.cpp`)。
11. 多 agent 隔离靠 `holder_id` 加 `tenant_id`:`recall` 的候选 SQL 带 `holder_id = ?`(`_memory_core.py:250-260`)。同一 DB 内多 holder 是设计内的;但该库的核心注释写"Command core over ONE runtime/WAL adapter",README 强调 single-adapter 纪律,多实例开同一文件的用法未见文档。
12. 持久层是 SQLite,开 `PRAGMA journal_mode = WAL` 与 `busy_timeout = 5000`(`src/persistence/connection.cpp:23-25`)。**没有快照/导出接口**:`export_dump` 只出现在图数据库后端的接口(`docs/design/subsystems_design/04_substrate.md:279,333`),SQLite 运行时没有。
13. 默认嵌入器是确定性 stub(8 维),写在门面里(`python/starling/memory.py:147-148`);真正的语义检索要自己注入 OpenAI 嵌入适配器(`bindings/python/bind_10_embedding.cpp`)。
14. 构建:scikit-build-core + CMake ≥3.27 + Ninja + pybind11,要求 C++ 编译器与 SQLite ≥3.46(`pyproject.toml`、`requirements-build.txt`)。本机有 Apple clang 21 与 SQLite 3.51,缺 `cmake`、`ninja`(两者都有 PyPI 轮子)。
15. **未发布到 PyPI**:`https://pypi.org/pypi/starling-memory/json` 返回 404。
16. **仓库根目录没有 LICENSE 文件**,README 结尾仍写"尚未选择许可证,保留所有权利"(`README.md:195-197`)。

## 2. 裁决总表

| 编号 | 裁决 | 备选 | 理由 | 判错的代价 |
|---|---|---|---|---|
| M1 | `simulator` 依赖"角色智能体"协议,不导入 `actio`;`actio` 是第一个实现 | 让 `simulator` 直接依赖 `actio` | 你要的是可扩展:第二、第三个智能体系统只加实现 | 协议多一层;实例构造挪到组合根 |
| M2 | 公共数据模型下沉:把会被协议两边共享的类型留在 `simulator.datamodel`,不新建包 | 新建 `socialmodel` 包 | 新包要改 36 个导入点与所有边界测试;协议化本身并不需要它 | 跨包耦合仍在,以后要抽再抽 |
| M3 | 记忆在 `actio` 内部,`simulator` 看不到 Starling | 记忆放在模拟器侧统一管理 | 你明确要求:记忆是个体 agent 的,不是环境的一部分 | `actio` 多一个外部依赖 |
| M4 | 一个 Starling 实例、多 holder:进程内只开一个 `Memory`,`agent` 固定,按 `holder=<agent_id>` 读写 | 每个 agent 一个实例 | Starling 的 single-adapter 纪律只描述了单实例;多实例开同一库未验证 | 每个 agent 的"第一人称视角"靠参数而非实例维持 |
| M5 | 记忆库在**树外**(本机路径,配置给),不进 git | 放 `control-plane/memory/` | 它是可变的原生库,不适合当 git 产物 | 记忆不可从产物树追溯,只能靠指纹 |
| M6 | 回合开始取记忆指纹,写进训练记录并入 `config_hash` | 只记录不进哈希 | 记忆变了行为就变,不钉进哈希就复现不了、续训门也失效 | 任一 agent 写入都会改整库指纹,复现粒度是整库 |
| M7 | 记忆指纹由智能体侧给出(协议可选方法 `fingerprint()`),组合根调用 | 组合根直接读 Starling 文件 | 模拟器与组合根都不该认识 Starling | 协议多一个可选方法 |
| M8 | 记忆参与的时机:每轮 `recall`,轮次后 `remember` | 只 recall;回合后一次性 remember | 你要"每轮 recall + remember" | 每轮多一次抽取调用,延迟与成本上升 |
| M9 | `actio` 现有的 `MemoryEntry` 检索降为测试替身,生产路径走 Starling | 删掉,全部换 Starling | 单元测试不该依赖原生扩展 | 两套实现要各自测试 |
| M10 | MindAgent 的 `kind: memory` 增量改为"把增量文本交给 `remember`",不再写 `memory_library/` | 保留树内记忆库 | 记忆的唯一权威是 Starling,不能有两份 | 门②的证据形态要跟着改 |
| M11 | Starling 作为可选 extra(`starling`),默认走内存替身 | 硬依赖 | 本地与 CI 都要能编 C++,门槛高 | 未装时功能退化,要显式报"记忆为替身" |
| M12 | 前置条件:Starling 仓库补 LICENSE(Apache 2.0)与本仓库能取到的安装源 | 直接依赖未授权代码 | 许可未定前不能声明依赖 | 本设计在这两项完成前不能施工 |

## 3. 分层与智能体协议(M1、M2)

### 3.1 目标分层

```
simulator.datamodel   纯数据模型(Persona / Scenario / RoleSlot / RoleBinding / RoleAssignment / TurnRecord / Episode)
         ↑
   ┌─────┴──────┐
actio         simulator     simulator 只经协议调用智能体,不导入 actio
  └─ starling(记忆在 actio 内部)
         ↑
mindtrainer · mindagent · factory(组合根)
```

`actio` 仍导入 `simulator.datamodel`;`simulator` 不导入 `actio`。两边都只依赖数据模型,协议是模拟器侧定义的结构性协议。这条保持了现有的单向依赖,不需要新建包。

### 3.2 协议

模拟器侧新增一个后端协议文件(可以就放在 `simulator/backends.py` 旁):

```python
class CharacterAgent(Protocol):
    def respond(self, persona, scenario, history, speaker_slot) -> tuple[str, str | None] | tuple[str, str | None, dict]: ...
    def fingerprint(self) -> dict | None: ...   # 可选:{"memory": <sha256>, "skills": <sha256>}
```

- `respond` 就是现有的 `ModelBackend`,不动签名;返回三元组时可带激活轨迹。
- `fingerprint` 是新增的可选方法。有它就取,没有就当"无状态"(mock 后端、外部 API 后端都返回 `None`)。
- 模拟器只认这两个方法。`ActioInstance` 已经满足 `respond`,`fingerprint` 在 `actio` 里补。

### 3.3 实例构造留在组合根

模拟器不构造实例,只在运行时按角色位取"已经构造好的对象"。构造发生在 `factory` 的装配处:读库、开 Starling、`instantiate(...)`。这样模拟器不导入 `actio`,也不读记忆。

## 4. Starling 接入(M3、M4、M5、M8、M9)

### 4.1 位置

```
src/actio/memory_starling.py     Starling 适配器:实现 actio 的记忆端口
src/actio/memory.py              MemoryEntry + 词面检索,降为测试替身
```

`actio` 内部定义一个记忆端口,只要两种能力:

```python
class MemoryBackend(Protocol):
    def recall(self, *, holder: str, query: str, limit: int) -> list[str]   # 已渲染好的记忆句
    def remember(self, *, holder: str, text: str, interlocutor: str | None) -> None
    def fingerprint(self) -> str
```

- Starling 适配器:`recall` 调 `Memory.recall(query, k=limit)` 后按 `StatementRow` 渲染成句;`remember` 调 `Memory.remember(text, holder=holder, interlocutor=…)`;`fingerprint` 对库文件(含 WAL)算 sha256。
- 替身适配器:现有 `MemoryEntry` + `retrieve`,供单元测试用。
- `ActioInstance` 从"持有元组"改为"持有一个 `MemoryBackend`",`decide` 里改成 `recall(holder=self.agent_id, query=…)`。

### 4.2 渲染

`StatementRow` 能拿到主体、谓词、对象、置信度、模态、二阶嵌套深度。指导语沿用现有格式并扩展:

```
[memory:<id>] <subject_id> <predicate> <object_value> (confidence=0.9, modality=BELIEVES)
```

嵌套深度大于 0 的条目前缀 `[memory:depth=<n>]`,让模型看得见"我 believe that you believe"这一层。也可以改用 `render_working_set(...).render()` 直接取五源上下文块,但那个块包含 persona / common_ground / 承诺等分区,粒度比 Actio 现在需要的粗。**取前者**,理由是可以只把记忆这一源喂给 `render_guidance`,与技能、经验并列,不引入第二套 prompt 骨架。

### 4.3 时机(M8)

- 每轮 `respond` 之前:`recall(holder=<本角色位 agent>, query=<对方最后一句>, limit=3)`。
- 每轮之后:把本轮对方发言交给 `remember(holder=…, text=…, interlocutor=<对方 agent>)`。
- `remember` 需要 LLM:由装配处注入,凭证只从环境变量读。抽取失败不挡训练:记一次失败、这一轮不留记忆,不抛到 `respond` 之外。

### 4.4 落盘(M5)

记忆库在树外,例如 `~/.rismforge/memory/<tenant>.db`,路径写进配置。为什么不在树里:它是原生 SQLite,有 WAL 与后台线程,每次写入都提交 git 会污染产物树,也让训练变成不可控的写者。

代价:记忆不可从产物树追溯。补偿就是 M6 的指纹。

## 5. 记忆指纹(M6、M7)

```python
# 回合开始,组合根调用一次
fp = agent.fingerprint()          # {"memory": "<sha256>", "skills": "<sha256>"} 或 None
```

- `actio` 的实现:对 Starling 的 db 文件加 `-wal` 一起算 sha256;技能部分对库条目算哈希。
- 组合根把它写进训练记录的 `agent_fingerprints` 块,并按 `{角色位: fp}` 并入 `config_hash`。
- 共用单库时,`memory` 指纹是整库的:任一 agent 写入都会改它。这一点写进记录的字段名(`memory: "shared"`)与文档,避免读的人以为是各自的。
- 指纹读取是只读的,不影响任何状态。

## 6. MindAgent 的记忆提案(M10)

现在门②收录 `kind: memory` 的增量时把它写成 `MemoryEntry` 落进 `memory_library/`(`src/mindagent/admit.py:41-43`)。记忆的唯一权威移到 Starling 之后:

- 提案里的 `definition` 仍是结构化条目(主体、内容、时间);
- 收录处理器改为调 `memory_backend.remember(holder=<bound agent>, text=<渲染后的陈述>)`,不写树;
- 失败时写一条 T2 失败记录,提案留在 `approved`(与现在 `repro` 缺失的处理同一形状);
- 门②的 `review` 仍然显示提案内容,证据形态不变。

这条改动会动到 `tests/test_ma_admit.py` 里两个记忆用例。

## 7. 前置条件与阻碍(M12)

按顺序,前两项不解决就不能施工:

1. **LICENSE。** `ruijieguo/starling` 根目录没有 LICENSE 文件,README 仍写未选许可。你确认按 Apache 2.0 授权,需要在该仓库补 `LICENSE`(与 `NOTICE`,如有第三方代码),并把 README 的许可段落改掉。完成前,本仓库不能声明对它的依赖。
2. **安装源。** 未上 PyPI。二选一:发布到 PyPI(最优),或本仓库的 `starling` extra 指向 git 地址/本地路径。
3. **构建门槛。** 要 C++ 编译器、CMake ≥3.27、Ninja、SQLite ≥3.46。本机缺 cmake/ninja,CI 也要能编。写进 `docs/design` 的依赖一节。
4. **`remember` 依赖 LLM。** 抽取出错、成本、凭证都要定;V1 建议默认关闭写记忆(只 recall),写记忆要显式打开。

## 8. 失败与可观测

| 失败点 | 类 | 处理 |
|---|---|---|
| 未装 Starling,但配置要求它 | 配置错误 | 装配期退出码 2,并说明"记忆为替身或未安装" |
| 记忆库打不开、迁移失败 | T2 | 指令 `failed`,不静默退回替身 |
| `remember` 抽取失败 | 不分类 | 记一次失败,这一轮不留该条记忆,不抛到 `respond` 之外 |
| `recall` 抛异常 | T2 | 指令 `failed`(记忆是角色行为的一部分,静默降级会让行为不可解释) |
| 指纹算不出 | T2 | 指令 `failed`(复现前提不成立) |
| Starling 装了两个版本 | 配置错误 | 装配期核对 `starling.__version__`,与记录的版本比对 |

## 9. 测试策略

| 层次 | 对象 | 替身 |
|---|---|---|
| 边界 | `simulator` 不导入 `actio`、不导入 `starling`;`actio` 只在 `memory_starling` 里导入 `starling` | 扫导入语句 |
| 单元 | 记忆端口:替身适配器的 recall / remember / fingerprint | 无 |
| 单元 | `StatementRow` 到指导语句的渲染:主体、谓词、对象、置信度、嵌套深度 | 构造行对象 |
| 集成 | Starling 适配器:真实 `Memory.open` 一次 recall 与 remember(按 extra 是否安装跳过) | 真库,不需要 LLM 的 stub 抽取器 |
| 集成 | 指纹:写入前后哈希变化;共用库时任一 holder 写入都会改指纹 | 真库 |
| 集成 | `ActioInstance` 用 Starling 适配器驱动一场会话 | loopback 服务端 |
| 单元 | MindAgent 记忆提案改写为新路径;失败留 T2 记录 | 临时仓库 |
| 端到端 | 场景二用 Starling 记忆跑一轮,指纹进训练记录 | 假 vLLM、真 Starling(extra 装好时) |

没有装 Starling 的环境只跑替身路径;需要真 Starling 的用例标 `slow` 或按 extra 缺失跳过。

## 10. 实施顺序

1. 前置:Starling 补 LICENSE、决定安装源。**不完成则后续为草案**。
2. `actio` 抽出记忆端口;现有实现降为替身适配器;`ActioInstance` 改持端口。
3. `actio/memory_starling.py`:recall / remember / fingerprint。
4. 指纹:训练记录与 `config_hash` 接入。
5. 模拟器侧 `CharacterAgent` 协议(只是把结构写清,`ModelBackend` 已满足);`actio` 补 `fingerprint`。
6. MindAgent 记忆提案改路。
7. 装配:读配置、开 Starling、注入 LLM、构造实例。
8. 并入 `docs/design/` 五篇。

第 2 步可以不依赖 Starling 先做,它把 `actio` 与具体记忆实现解耦。

## 11. 待你确认

1. **M5 的树外路径。** 记忆库放 `~/.rismforge/memory/<tenant>.db` 这样的本机路径,还是放到计算层主机上的一个固定目录?前者简单,后者更接近"记忆是 agent 的私有状态、不在开发机上"。
2. **M8 的写记忆默认关不关。** 每轮 `remember` 会翻倍外部调用。建议 V1 默认只 recall,写记忆显式打开;你要不要一开始就全开?
3. **M10 的门②形态。** 记忆提案的证据从"树里的条目文件"变成"Starling 里的一条陈述",`review` 时怎么展示最合适:显示提案原文,还是显示收录后从 Starling 读回的那条?
