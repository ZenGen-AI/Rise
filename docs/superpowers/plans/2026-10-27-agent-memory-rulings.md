# 计划拼接时的裁定

## 第 1 步(写作者交回 2026-10-27)
- R1 接受新增公开名 `actio.memory.content_id(subject, content) -> str`、私有 `_Ledger`、`FileTupleStore(root, *, extraction_failure=None)` 故障注入口。
- R2 接受 `TupleMemory.recall` 按 (重叠降序, 置信度降序, content_id 升序) 排序并按 content_id 去重,不直接调 `retrieve`。
- R3 接受查询词为空串时实例不调 `recall`(与今天 `retrieve` 对空查询无命中一致)。
- R4 接受 `episode_id` 必填按"会写库"判断(`replay_read_only` 为假即要求)。
- R5 接受实例把端口抛出的非 `MemoryFailureError` 异常包成 `MemoryFailureError(类名, transient=False)`:未知异常判确定性失败,宁可 T2 不无限重试;已知的瞬态异常由各适配器自己映射(计划 B 的 StarlingMemory 穷举映射)。
- R6 人格 id 字符集统一为 `PERSONA_ID_PATTERN = ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`,定义在 `actio/memory_port.py`,FileTupleStore 与 StarlingDeploymentStore 拼路径前都校验,不合法抛 `ActioDataError`。写作者原拟的 `[A-Za-z0-9][A-Za-z0-9_-]*` 改为此式(与 episode_id 白名单同形,允许点号,首字符排除 `.`)。仓库没有人格 id 的现成校验,生成路的实际格式未核对,列入 Review Focus。
- R7 接受端口 `close()` 幂等、替身不加锁(MindAgent 在人格库锁下单线程用,训练副本每会话一份)。
- R8 `TupleMemory.digest()` 改为对 `snapshot(entries)` 的结果算,不对原始条目列表:写作者指出的缺口成立(两库去 id 后多重集相同、版本归属不同时召回不同而摘要相同)。已改 step-1.md 并加钉住测试,设计源 4.5 节同步。
- R9 `TRACE_SCHEMA` 改名 `TRACE_SCHEMA_V1` 不留别名(只有两处引用);导入白名单另加 `pathlib`、`datetime`。

## 第 2 步(写作者交回 2026-10-27)
- R10 新终止原因登记进三处集合(两份瞬态集合与 `_DETERMINISTIC_ROLLOUT_REASONS`)留在 S2-5;第 3 步只给编排器 `_DETERMINISTIC` 元组加 `RoleMemoryFailureError`。已告知第 3 步写作者。
- R11 S2-9(AgentRuntime 按 session_id 路由落盘的第二张表)留在第 2 步:设计源 6.1 与施工顺序第 2 步"后端工厂加会话身份"一致。
- R12 `AgentRuntime.client_factory(deadline_s=)` 归第 3 步(与第 3 步派发一致)。
- R13 接受客户端相邻消息超时时本地合成 `EndSession(reason="timeout")` 走既有收尾:否则 `_classify_client_error` 把 `RuntimeError` 判成不可重跑的 `client_protocol_error`,与设计源"按 timeout 处理(T1)"相反。合同已改。
- R14 接受 `create_server` 把 `max_workers` 透传给 servicer;接受新增 `resource_join_timeout_s`、`linger_retry_s`(默认 60)、`lingering_count()`、`grpc.Server.servicer` 属性、`validation.validate_episode_id`、`SessionClosedHook` 别名。
- R15 接受 `TimeoutBackend` 在调用方线程 `enter_call`(quiesce 之后直接抛 `SessionClosingError`,不起线程)、daemon 线程退出时 `exit_call`:比合同字面"在 daemon 线程里登记"少一个竞态窗口(线程起了还没登记就被 quiesce 漏掉)。
- R16 S2-8 准入用例里固定的 `time.sleep(0.2)` 改为有上限的轮询:`_try_open` 遇 `rpc_resource_exhausted` 时重试,最长 3 秒。固定等待在慢机器上会偶发失败。已改 step-2.md。
- R17 接受 S2-3 的停止条件:生成器版本与入库 pb2 头部 `Protobuf Python Version: 7.35.1` 不同就停下报告,不提交产物。
- 不符记录:没有独立的"simulator 终止原因白名单",只有上述三处集合与若干注释。

## 第 4 步(写作者交回 2026-10-27)
- R18 解出的 `digests`、`runtime` 只进本次派发的 `cfg`,不写回 `directive.yaml`。理由同写作者:续训要重新算(7.1 节),写回会多一笔提交、还会盖掉 T3 比较的原值;记录里的 `training_run.policy_backend.memory` 已经让读者看到记忆从哪来。设计源"写回"一词指写回本次派发用的配置,第 8 步收尾时把 30 篇与设计源的措辞改成"写进本次派发的配置,不回写指令"。
- R19 严格校验放行"未解析"的 memory 段(digests 与 runtime 都为空),半填(只有一个非空)拒绝;解析后再跑一次 `validate_training_config`,摘要的人格集合要等于绑定人格集合、runtime 键集合要等于 5.2 节五个键。
- R20 接受新名:`_resolve_memory`、`factory.memory_runtime`(`STAND_IN_MEMORY_RUNTIME`、`stand_in_memory_resolver`;计划 B 第 7 步在同一模块加 Starling 那一路)、`InteractDefaults.memory_seed`(标量)、`MISSING_MEMORY_REASON`、测试辅助 `tests/memory_fixtures.py`。
- R21 解析器异常的原因:`DirectiveFailedError` 保留它自己的 `reason`(替身拒绝种子要说清原因),其余异常只报类名。7.1 节"一律 T2"与第 10 节"只报类名"由此一致。
- R22 第 4 步写作者没核对的两处测试辅助名已由控制者核对改正:`test_mt_dispatch_synthesis.py` 用现有的 `_directive(store, cfg=)` 与 `_interact_cfg()`,`_commit_count` 照 `test_mt_dispatch.py:799` 新加;e2e 断言改用该用例已有的 `persona_ids` 并放到它定义之后。
- 不符记录:仓库没有入库的 `training_defaults.yaml`,只改测试夹具;70 篇约 237 行的 YAML 样例留给第 8 步。`_TOP_FIELDS` 自动派生会让 `memory` 变成可扰动键,S4-4 把它列为锚定键。

## 第 3 步(写作者交回 2026-10-27)
- R23 接受 `InteractTrajectorySource(..., *, run_id: str)` 由派发器传入;逻辑 id `logical_episode_id(run_id, step, scenario_id, k) = f"{run_id}-grpo{step}-{scenario_id}-r{k}"`:同一情境在不同 GRPO step 反复出现,只写阶段名会撞。同一 step 内 `groups_per_step <= len(scenario_ids)` 已由校验保证不重复情境,组内 k 区分 rollout。续训从同一 step 重跑时 id 与上次相同,同名 episode 文件被覆盖,批的 `episode_ids` 只指向本次产出,这是要的语义(批只认本次);上一次失败尝试的 episode 本来就不落盘。
- R24 角色位 Actio 实例的 `library_version` 取 `{"skill": "0", "memory": "0"}`(训练路不读技能库,7.4 节)。
- R25 训练位副本 `close()` 失败按 7.2 节判 `role_memory_failure`、不重试,即使 rollout 本身已结束:close 失败说明副本库状态不可信,第 7 步还要删它。
- R26 删 `factory.interact_policy.PER_TURN_DEADLINE_S` 与它对运行时 `interact_deadline_s` 的改写,流 deadline 由轨迹源 `mindtrainer.trajectory_source.PER_TURN_DEADLINE_S = 90.0` 算、经 `client_factory(deadline_s=)` 传:原改写会让同开时的部署会话借用训练的 deadline,与 6.4 节相反。合同里的 `PER_TURN_DEADLINE_S=90` 指的就是这个新常量。
- R27a S3-5 与 S3-6 合成一次提交:两者之间 `test_factory_interact_wire.py` 会红,违反"每个任务结束时全套仍绿"。拼接时把 S3-6 的实现步并进 S3-5,S3-6 只留它自己的新测试。
- R27b S2-4 钉死客户端属性名 `self._message_timeout_s`,S3-3 的断言读它(已改两段)。

## 第 5 步(写作者交回 2026-10-27)
- R27 门② 多条记忆增量的部分失败(写作者没覆盖,控制者补):前几条已 committed、后一条抽取失败时,提案**不**转 `rejected`(已提交事件放弃不了,转了就是"被拒提案在库里留着记忆");这一条 abandon,写 T2 失败记录"partial admission",提案留 `approved` 等人处理。一条都没提交时才整份拒绝。已改 S5-6 并加测试。
- R28 替身部署记忆根目录 `<git 目录>/rise-memory`(与 `rise.lock` 同处,不入树、跨重启保留),`lock._git_dir` 公开为 `git_dir`。不用 `mkdtemp`:每次重启换空库会让崩溃重入与门② 收录失效。设计源 4.1 节"临时目录"的措辞第 8 步改。
- R29 成功路径的顺序 `write_episode` → 放弃本 episode 的 pending 行 → 删起点副本(提交点)。写作者把放弃放在删副本之前是对的:崩在放弃与删副本之间,重入会先用起点副本覆盖(覆盖后 abandoned 行也一起回到起点状态)再重跑,不会撞已放弃的 key。设计源 4.3 节"成功提交后放弃"第 8 步改成"提交前、落盘后"。
- R30 起点副本删除失败上抛、轮次留 pending:副本留着就是未提交。第 10 节"副本删除失败只记日志"只指 `runs/` 下的会话副本。
- R31 `episode_digests` 只记智能体位人格(部署对手位每会话一份空 TupleMemory,7.4 节);轮次锁覆盖所有绑定人格,保守,不改。
- R32 门② 混合提案(记忆加技能)记忆整份抽取失败时整份转 `rejected`、技能条目也不落:记忆先写就是为了这个。
- R33 门② `remember_event` 抛瞬态 `MemoryFailureError` 写 T1,其余 T2;提案都留 `approved` 等重入。
- R34 新一轮第一次运行用不带后缀的 base id;只有 base 已提交且技能版本旧了才用 `-s<版本>`。
- R35 接受 `run_deployment_episodes` 新增参数(`memory_layout`、`round_id`、`library_version`、`episode_digests`、`on_episode_start`、`stream_deadline_s`)、`mindagent/memory_layout.py` 的 `MemoryLayout(root, suffix=".json")`、`controlplane/lock.py` 的 `hold_file_lock`/`FileLockBusyError`、`RestoreFailedError`、`make_admit_proposal_handler`、`check_memory_library_empty`。轮次拿不到人格库锁判 T3(设计源 9 节 M20 写的是 `AgentRoundFailedError`)。
- 名字确认:第 4 步替身运行时身份常量就叫 `factory.memory_runtime.STAND_IN_MEMORY_RUNTIME`;测试夹具 `tests/memory_fixtures.py` 里的 `STAND_IN_RUNTIME` 应 import 它,不另写一份(拼接时统一)。

## 第 6 步(写作者交回 2026-10-27)
- R36 **S4 不成立(设计缺口,影响用户可见行为)**:Starling 新写的语句是 `volatile`,向量召回只要 `consolidated/archived`(`sqlite_blob_vector_index.cpp:67`,控制者已复核);只有 `tick` 的回放把它巩固。照"只 embed_pending、不 tick",本轮写的记忆永远召回不到。裁定走"召回时显式放宽到 volatile"(S6-4 的 `recall(include_volatile=True)`,review 与 holder 守卫不变),不走"嵌入后跑确定性巩固":后者改 `activation` 等进摘要的列、语义更重、且把 tick 的一部分拉进每轮。代价:volatile 行没经过回放去重,召回里可能有近重复语句。设计源 4.3、§8、§13 与 40 篇第 8 步改。
- R37 摘要口径:Starling 侧全行进摘要(被取代的行改为 archived 仍是召回候选;volatile 与 pending_review 行影响后续写入的冲突探测),接受。TupleMemory 的 snapshot 口径不改:替身没有冲突探测,旧版本行对召回与写入都不起作用,两边的原则一致("影响召回或后续写入的行进摘要"),只是适用到不同实现。
- R38 接受业务逻辑进 C++(Starling 的 CLAUDE.md 规定状态机与算法进 C++),§8 "只改门面与绑定"第 8 步改。
- R39 接受排除清单的增补:engram UUID 换 `engram:<content_hash>`、`confidence_history_json` 的 `ts` 规范化时去掉、`observed_at` 在写入端改取 engram 的 `created_at`(S6-8)。
- R40 接受 §8 签名增补:`remember_event(..., epoch=)`、events 表加 `tenant_id`、`failure_category` 两列且主键 `(tenant_id, key)`、`make_openai_embedder(max_retries=)`、`[key] ` 前缀在 Starling 侧加。
- R41 `abandon_event` 未知 key 抛 `KeyError`(本仓库适配器映射为 `MemoryFailureError(transient=False)`),已放弃再放弃为空操作,与替身一致。
- R42 `close(timeout=)` 等门面在途调用返回后合并 WAL 并关连接,关后调用抛 `MemoryClosed`。
- R43 Starling 远端 main 已前进到 `be5ccf8`,S6-1 建分支前先 `git pull`,S6-8 对照 #107 改过的 `extractor.cpp`。

## 第 8 步(写作者交回 2026-10-27)
- R44 "对应实施计划"行写不带日期的短名 `agent-memory-standin`、`agent-memory-starling` 加工作包口径,完整路径进提交信息:检查器的 FORBIDDEN 规则与 STYLE.md 第 66 行都禁止写计划路径,不为此改检查器。
- R45 docs/design 落后设计源(f053cd0 之后 128eefe、a31af64、70d0d1a、d1b0bcb 及改了设计措辞的裁定)。开工前先单独追平一笔(只改设计措辞,不摘状态标记),不留到第 8 步:docs/design 是权威文档集,施工与评审期间它不能和设计源说两套话。追平后第 8 步只剩状态标记、测试数与第 7 步之后的写回,S8-1 到 S8-8 里已追平的条目删掉。
- R46 第 8 步分两次:第 7 步合入前先做"必改"部分,合入后做"依第 7 步"部分;第一次不删 `.parts/`,不摘 00 篇路线图第 8 步。
- R47 台账编号不进文档,写回只写措辞。
- R48 接受 70 篇按实际结构改(样例一行、规则表一行、TS-4 半句、488 行 `memory.seed`)、`RestoreFailedError` 继承 `AgentRoundFailedError`、R26 与 R23 在 30、20 篇的改写、两张图的改动(属目标系统变化,不是进展标记)。
- R49 `.parts/` 只在两份计划拼好入库之后删;拼接后它不再是权威来源。

## 第 7 步(写作者交回 2026-10-27)
- R50 S6-9 已有 C++ `memoryops::content_ids` 与绑定 `memory_content_ids`,只差公开门面:在 S6-9 补 `Memory.content_ids(statement_ids: list[str]) -> dict[str, str]`(Produces 与一条测试),S7-2 依赖它。已改 step-6。
- R51 适配器丢掉 `score <= 0` 的命中:Starling 向量召回无分数下限,按余弦取 top-k 会补满 k 条无关行(`sqlite_blob_vector_index.cpp:95-103`,控制者已复核)。只在本仓库适配器做,不改 Starling 语义。
- R52 改 S6-1:`make_openai_embedder` 无条件覆盖 `max_retries`(默认 0 照写),不照 `make_openai_llm` 的"非零才覆盖";`make_openai_llm` 同一 PR 一并改成无条件覆盖并加测试。理由:C++ 默认 3(`openai_embedding_adapter.hpp:21`,控制者已复核;`memory.py:66、90` 的 `if max_retries:`),配置写 0 会静默变成重试 3 次。S7-2 因此用这两个工厂函数,不绕开直接写 C++ 配置。已改 step-6;S7-2 实施时照此。
- R53 训练副本从种子库用文件复制(`.db` 加 `-wal`),不用 `Memory.open` 后 `backup_to`:后者跑迁移、写 WAL,种子是只读的。`digest_of` 同理在临时副本上算。设计源 7.2 节第 8 步改措辞。复制前确认种子库没有打开的连接(种子由人放进 `seeds/`,运行期只读,不会并发写)。
- R54 S7-3 管理面每次调用开短命连接、端口开着时同一库两条连接:前提成立。门② 在有未提交起点副本时延后(Task 34),轮次期间人格库锁由轮次持有;同一时刻对一个人格库只有一个写者。另一条连接只读(digest、pending_events)或在端口已关之后(restore_from 先确认没有打开的端口)。
- R55 接受第 7 步的增补:`base_url` 带 query 或 fragment 拒绝;`.owner` 读回最多等 2 秒;运行时身份不含根目录路径;种子缺某人格从空库起;`tests/conftest.py` 清掉 `RISE_MEMORY_CONFIG`。
- R56 S7-8 部署用例的函数体照第 5 步测试文件的实际夹具写,实施时决定参数化还是新建文件。
- 不符记录:本机装 Starling 要从源码编 C++(CMake ≥3.27、Ninja、编译器),本机目前缺 cmake 与 ninja(见记忆 interact-design-status)。计划 B 开工前要先装。

## 用户裁定(2026-10-27)
- U1 接受 R36:召回包含未巩固的 volatile 记忆,代价是可能有近重复。
- U2 设计二分支合并回 main 前,改写提交信息去掉 Co-Authored-By 行(`8dba5dd`..`a31af64` 共 20 笔)。
- U3 cmake/ninja 用 pip 轮子,不装系统包;Starling 仓库 `.venv` 已有 cmake 4.3.2、ninja 1.13。
