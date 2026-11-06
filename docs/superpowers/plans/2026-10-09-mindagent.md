# MindAgent:部署驱动、经验分析、技能提案、提案验证、打包,以及轮次与收录处理器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增顶层包 `mindagent`,把 AgentRSI 路径接成两个可注入的处理器:`start_agent_round` 的处理器绑定最新晋级模型与当前正式库,让完整 SimpleActio 实例经实时环境服务跑一批部署 episode,跨 episode 分析出行为缺口,生成技能提案,离线重放验证,打包成待审核的 `skill_memory_proposal`;`admit_proposal` 的处理器把人工批准的提案收录进正式技能库与记忆库,并递增库版本。调和循环一侧的触发与注入归计划 8,本计划的处理器可以脱离调和循环直接调用。

**Architecture:** 依赖方向 `mindagent → controlplane、simple_actio、inference_bridge、simulator(datamodel、store_adapter、realtime.client)`;`controlplane` 不导入 `mindagent`(有测试)。部署驱动器复用 `EnvironmentServiceClient` 的"客户端后说"能力(SimpleActio 实例占 `self_play` 角色位,排在对手之后),每轮用 `instance.respond` 取三元组,经计划 6 的 `submit_turn(..., activation_trace=...)` 提交;会话结束时用客户端重建的 Episode 以 `deploy-<model>-<scenario>-<k>` 为 id 写进绑定模型所在 run 的 `episodes/` 目录。分析器、提案生成器、验证器是纯函数(验证器只调罐头后端,不起服务端);打包器、两个处理器读写都走 `GitStore` + `Envelope`。确定性失败写轮次 `failed` 并经 `controlplane.ledger.record_failure` 留一条 T3 记录;非桥接的瞬态异常原样上抛,轮次保持 `pending`,按 R19 由调和循环在下一个 tick 重新触发,处理器重入幂等。

**Tech Stack:** Python 3.11+ / uv / pytest;**无新增第三方依赖**。gRPC 测试用真实 loopback 服务端(`127.0.0.1`,`port=0`);桥接一律用 `ScriptedBridge`。

**Spec:**
- `docs/superpowers/specs/2026-10-09-closed-loop-integration-design.md`:§3.2(`start_agent_round`、`admit_proposal` 两行)、§4.1(`agent_round`、`skill_memory_proposal`、`skill`、`memory`、`library_state` 五类产物)、§4.2(技能提案载荷)、§5(桥接与 `ledger`)、§8.3(本计划主体)、§11(契约,名字逐字照用)、R11、R13、R18、R19。R1 至 R21 已定,本计划不推翻。
- 上游:`docs/superpowers/specs/2026-09-24-rise-subsystem-design.md` §4(MindAgent)、补充 A(SimpleActio)、补充 E.4(失败会话整条重跑)、场景三(AgentRSI 路径)。
- 前置计划:**计划 1** `2026-10-09-bridge-and-foundations.md`(路径函数、`ledger`、`inference_bridge`)与 **计划 6** `2026-10-09-simple-actio.md`(`simple_actio` 包、`SubmitTurn.activation_trace`、runner 三元组)必须已合并。计划 2 至 5 是否已合并不影响本计划,只影响基线测试数 B(见 Global Constraints)。

**依赖已有产出的名字(撰写时逐个读源码核实)**

| 来源 | 名字 |
|---|---|
| `controlplane.paths`(计划 1) | `CONTROL_PLANE_ROOT`、`agent_round_file(model_id)`、`proposal_file(proposal_id)`、`skill_file(skill_id)`、`memory_file(mem_id)`、`library_state_file()`、`failure_record_file(run_id, task_id)`、`model_file(n)`、`episode_file(run_id, episode_id)`、`episodes_dir(run_id)`、`scenario_file(id)`、`persona_file(id)`;`SUBDIRS` 含 `skill_library`、`memory_library`、`agent_rounds` |
| `controlplane.ledger`(计划 1) | `record_failure(store, run_id, *, task_id, category, point, retries, reason, log_ref=None) -> str`(同一 `(run_id, task_id)` 只写一次);`run_trace_file` 由 `record_cost` 写 |
| `controlplane.states` | `assert_transition`:非审核类 `pending → executed \| failed`;审核类 `approved → effective` |
| `controlplane.reconcile` | `Action(kind, target_id, detail)` |
| `controlplane.envelope` / `gitstore` | `Envelope`、`OwnerRef`、`BigRef`;`GitStore.exists / read_product / write_product / list_products / _git`(写入内容不变时不提交) |
| `controlplane.daemon.promote_candidate` | 写 `models/zengen-v<N>.yaml`:`links == [候选 id, run id]`,`payload.checkpoint_ref` 来自候选(候选没有时不写) |
| `inference_bridge`(计划 1) | `BridgeError(category, reason="", *, attempts=0, log_ref=None)`(`category ∈ {T1, T2}`)、`BridgeResult.text`、`ScriptedBridge(script, *, model="scripted", store=None)`(`.calls`、`.remaining`;传了 `store` 时每次成功调用记一条 `external_inference`) |
| `simple_actio`(计划 6) | 见下文"对外契约:对计划 6 的假设" |
| `simulator.realtime.client` | `EnvironmentServiceClient`:`create_session(*, scenario_id, role_assignment, personas, purpose, client_driven_slot)`、`turns`(深拷贝)、`closed`、`submit_turn(utterance, inner_thought=None, *, activation_trace=None)`(计划 6)、`drain_until_end()`、`abort()`;`SessionRejectedError(reason, detail)` |
| `simulator.realtime.server` | `create_server(backends_factory, *, port, turn_timeout_s, on_session_closed=None, scenario_resolver=None, ...)`(只在测试里用) |
| `simulator.store_adapter` | `write_episode(store, run_id, episode)`(id 即文件名,`links=[run_id, scenario_id]`)、`read_episode`、`read_scenario`、`read_persona`、`write_scenario`、`write_persona` |
| `simulator.datamodel` | `Episode`、`TurnRecord`、`RoleAssignment`、`RoleBinding`(`model_backend == "self_play"` 时不得带 `model_ref`)、`Persona`、`Scenario` |

**设计 §8.3 与任务说明的各项,本计划怎么处理**

| 条目 | 处理 |
|---|---|
| 部署驱动器 `run_deployment_episodes` | **做**(任务 81)。签名在设计之外加 keyword-only 的 `model_id`、`max_turns`、`episodes_per_scenario`、`max_retries_per_episode`(Ruling 4) |
| 经验分析器 `analyze` | **做**(任务 82)。加 keyword-only 的 `skills`、`registry`、`signals`(Ruling 6) |
| 技能提案生成器 `propose` | **做**(任务 83):规则模板 + 可选桥接 |
| 记忆增量生成器 | **不做**(Ruling 10)。`kind: memory` 的增量在数据模型与收录处理器里可用,收录用手写的记忆提案测试 |
| 提案验证器 `verify`(R13) | **做**(任务 84):`replay_behavior`,可选 judge 单列 `quality_notes` |
| 提案打包器 `package` | **做**(任务 85) |
| 收录处理器 `handle_admit_proposal` | **做**(任务 86) |
| 轮次处理器 `make_start_round_handler` | **做**(任务 87) |
| 端到端(真实 loopback 服务端) | **做**(任务 88) |
| 调和循环里的触发、`handlers` 注入、组合根 | 不做:计划 8(`reconcile` 扩展)与计划 9(`factory`) |
| 树内持久化的 agent 配置 | 不做:`AgentConfig` 注入处理器工厂(Ruling 3) |
| AgentRSI 配方的提名 | 不做(设计 §15)。轮次产物记下配方需要的锚定字段,供日后人工写配方候选 |
| SimpleActio 实例"长期存活、新版本到来中止重启" | 部分:每轮重新 `instantiate`;新模型晋级时旧的 `pending` 轮次置 `failed`(superseded)。正在跑的轮次不会被中途打断(同步执行) |

**已核实的事实(实现者不要再猜)**

以下全部在一份临时拷贝里(基线 + 计划 1 + 计划 6 的最终文本)真实跑过:

- **服务端落盘的 episode id 是 `ep-<session_id>`**(`servicer` 构造 `SessionDriver(episode_id=f"ep-{session_id}")`),客户端 `_build_episode` 用同一个 id 重建。所以 `deploy-<model>-...` 的 id 只能由调用方改写。服务端的 `on_session_closed` 钩子拿不到模型 id、情境内序号与 run id,改写只能在客户端一侧做(Ruling 1)。
- **客户端重建的 episode 与服务端记录逐轮相同,只差对手的内心独白与轨迹**:服务端回显客户端自己那一轮时原样带回 `inner_thought` 与 `activation_trace`,对手轮次两者都被抹掉(`servicer._drain_ready_events`)。任务 81 的 `test_the_server_side_ep_copy_is_left_to_the_caller_and_the_deploy_copy_matches_it` 断言两者 `to_dict()` 逐轮相等(对手是不产生内心独白的 `ScriptedBackend`)。
- **激活轨迹经 gRPC 往返后逐字段相等**:计划 6 的轨迹模式没有数字,`library_version` 是两个字符串。所以落盘轨迹里的 `library_version` 可以直接与 `instance.library_version`(`normalize_library_version` 规范化过)用 `==` 比较。
- **`EnvironmentServiceClient.create_session` 在客户端后说时已把对手开场轮收进 `turns`**;SimpleActio 实例第一次 `respond` 时 `history` 里已有对手第一句(任务 81 的 `test_the_agent_sees_the_opponents_opening_turn`)。
- **计划 6 的 `Skill.from_dict` 要求 id 以 `skill-` 开头、`MemoryEntry.from_dict` 要求以 `mem-` 开头**,前缀后非空。候选技能的 id 因此用 `skill-candidate-<signal>`,收录后的 id 用 `skill-<提案 id 去掉 proposal->-<k>`。
- **`simple_actio.triggers.referenced_signals` 只算偶数层 `not` 之下的信号**:`{"not": {"signal": "emotion_high"}}` 不算"针对 `emotion_high`"(任务 82 有测试)。
- **`no_new_information` 在对方把同一句话说两遍时也为真**:同一 episode 里对方重复怒语,两个信号都会触发(任务 82 的测试因此对单信号用例显式传 `signals=("emotion_high",)`)。
- **`record_failure` 同一 `(run_id, task_id)` 只写一次**:同一模型的轮次第二次失败不会覆盖第一份记录(轮次 `failed` 是终态,不会有第二次)。
- **`GitStore.write_product` 内容不变时不提交**:重入的"空操作"可以用 `git rev-parse HEAD` 前后相等来断言。
- **`tests/test_mt_packaging.py` 比对 `src/` 下的顶层包与 wheel 列表**:新增 `src/mindagent` 必须同时加进 `pyproject.toml`。

## 本计划的裁决

设计 R1 至 R21 不动。下面是设计没规定、或与现有代码有出入的细节(要改的话改这里,任务按此排):

1. **Ruling: 部署 episode 由驱动器落盘,不由服务端的 `on_session_closed` 钩子落盘。驱动器在会话结束后,用客户端 `drain_until_end()` 重建的 Episode 换上 `deploy-<model>-<scenario>-<k>` 的 id(`k` 从 1 起,是该情境内的序号),经 `write_episode` 写到 `runs/<产生绑定模型的 run>/episodes/`。服务端若另配了落盘钩子,它写的 `ep-<session>` 副本与本计划无关。** —— 原因:钩子只收到 `Episode`,拿不到模型 id、情境内序号与目标 run,要它改名就得在服务端与 MindAgent 之间另建一条按 `session_id` 对账的通道;而客户端重建的 Episode 与服务端记录逐轮相同(自己轮次是服务端回显,带激活轨迹),只差对手的内心独白与轨迹——那两项本来就不该进 MindAgent 的分析(对手不是被观察对象)。确定性的 id 还给了重入一个天然的幂等键。 —— 判错的代价:落盘的部署 episode 里看不到对手的内心独白(对手是外部 API 时它本来也没有);如果以后要它,得改成在服务端钩子里按 `session_id` 关联,重写 `deployment.py` 的落盘段。
2. **Ruling: 已落盘的部署 episode 只在"每个 agent 轮次都带着与当前实例相同 `library_version` 的轨迹"时复用;否则按同一个 id 重新生成并覆盖。** —— 原因:设计要求 pending 轮次重入时不重复生成;但轮次绑定的库版本是在重入时重读的,若两次之间有提案被收录,旧 episode 用的是旧库,拿来分析会把"已修好的缺口"再报一遍。 —— 判错的代价:库在轮次中途变化时会多跑一遍部署;库不变时零额外开销。
3. **Ruling: MindAgent 的配置(情境清单、角色绑定、每情境 episode 数、重跑上限、分析阈值、验证阈值、解析重试上限)是冻结数据类 `AgentConfig`,由组合根注入 `make_start_round_handler`,不落进 git 树;实际用到的值逐项写进轮次产物的载荷(`scenario_ids`、`persona_ids`、`bindings`、`analyzer_config`、`verifier_config`、`generator_version`、`uses_bridge`、`prompt_template_version`)。** —— 原因:补充契约规定配置文件一律走 `read_config` / `write_config`,而路径函数全部由计划 1 产出、本计划不得新增;计划 1 没有 agent 配置的路径。把用过的值写进轮次产物,配方复现(计划 3 的 agent_rsi 校验)所需的锚定字段仍然在树里。 —— 判错的代价:改阈值要重启守护进程;以后要树内配置,由计划 1 补一个路径函数,再在组合根里 `read_config` 后构造 `AgentConfig`,本包不用改。
4. **Ruling: `run_deployment_episodes` 在设计签名之外加 keyword-only 的 `model_id`、`max_turns`、`episodes_per_scenario=1`、`max_retries_per_episode=1`;SimpleActio 实例占的角色位是 `bindings` 里唯一一个 `model_backend == "self_play"` 的绑定,不满足就抛 `ValueError`。** —— 原因:设计签名推不出 episode id 里的模型、每条会话的轮数与重跑上限;"被部署的就是 self_play 那一位"沿用 `RoleBinding` 现有语义(`self_play` 不得带 `model_ref`,校验器已强制),不必再加一个"客户端角色位"参数。 —— 判错的代价:想让 SimpleActio 扮演 `frozen_reference` 位要改 `agent_slot_of`。
5. **Ruling: 部署会话的失败分类照搬 `InteractTrajectorySource`(本包不导入 `mindtrainer`,判定在 `mindagent.deployment` 里另写一份):终止原因 `max_turns`、`goal_reached` 接受;`timeout`、`role_backend_timeout`、传输层 `UNAVAILABLE` 是 T1,整条重跑,上限 `max_retries_per_episode`;其余(`invalid_session`、`role_backend_failure`、`client_disconnected`、其他 gRPC 状态码、协议错乱)不重跑。两类都以 `DeploymentFailedError(episode_id, reason, detail, attempts, transient, failed_attempts)` 结束;轮次处理器对 `transient=True` 原样上抛(轮次保持 `pending`),对 `False` 判 T3。SimpleActio 实例自己的异常不在任何 `except` 里:会话先 `abort()`(服务端判 FAILED、不落盘),原异常原样上抛,不重跑、不包装。** —— 原因:补充 E.4;`role_backend_failure` 分不出 T1/T2,与 MindTrainer 同样保守。 —— 判错的代价:两份分类表要同步维护;服务端偶发的 `role_backend_failure` 会让整轮 `failed`。
6. **Ruling: "对方最后一句触发了该信号"按 agent 每次发言前、它能看到的历史求信号(与 runner 给后端的 `history` 同一条抹除规则);默认只看读对方话语的两个内置信号 `emotion_high`、`no_new_information`,`first_turn` 不在其列。一个 episode 受影响 = 至少有一个 agent 轮次信号为真、且轨迹里 `selected` 非空、且 `referenced_signals(选中技能.trigger)` 不含该信号;`ratio = 受影响数 / 有 agent 轮次的 episode 数`;`support < min_support` 或 `ratio < min_ratio` 不产生发现(两个阈值都含等号)。轨迹为 `None`(裸模型)或 `selected` 为 `None`(库里没有命中的技能)的轮次不算受影响。`analyze` 加 keyword-only 的 `skills`(这批 episode 所用的技能库,用来查选中技能的触发条件)、`registry`、`signals`;轨迹选中了 `skills` 里没有的技能抛 `MindAgentDataError`。对每个非 `None` 的轨迹先调用计划 6 的 `validate_trace`,不合模式转成 `MindAgentDataError`(信息带 episode id 与轮次)。** —— 原因:计划 6 要求消费方读落盘 episode 时校验轨迹;信号要用分析时的注册表重新求值,不读轨迹里的 `signals` 键,这样 `analyze` 对任何来源的 episode 都给同一个答案;`selected` 为空表示"没有路由",谈不上"路由错了",所以第一轮空库必然零发现,与设计"第一轮沉默是预期"一致。 —— 判错的代价:空库永远不会自己长出第一条技能,第一条技能要人手放(任务 88 正是这么做的);想让空库也产出"缺一条处理 X 的技能",把 `selected is None` 也算受影响即可(`analysis.py` 一行)。
7. **Ruling: `BehaviorFinding` 在任务说明的字段之外多一个 `signal`;`id = "finding-<signal>-" + sha256("<signal>\n" + 排序后的 episode id 逐行)[:12]`。提案载荷的 `finding` 只写设计 §4.2 的五个键(`id、pattern、support、ratio、episode_refs`),不写 `signal`。** —— 原因:提案生成器要知道发现是哪个信号,从 `pattern` 文本里解析太脆;`id` 由信号与 episode 集合推出,同一批数据重跑得到同一 id。 —— 判错的代价:无。
8. **Ruling: 规则模板的候选技能 = `{"id": "skill-candidate-<signal>", "version": 1, "level": "micro", "priority": 100, "trigger": {"signal": <signal>}, "policy": {"instructions": [模板文本]}, "description": ...}`;用桥接时外部模型只写 `instructions` 与 `description`,触发条件、层级、优先级仍由发现决定。外部模型必须只输出 `{"instructions": [非空字符串...], "description": 字符串}` 一个对象,多一个键也算不合格;不合格带着错误信息重新提问,总次数 `1 + max_parse_retries`,耗尽抛 `ProposalParseError`。第 i 条发现的第 n 次尝试用 `task_id = "<task_id>-<i>-attempt<n>"`(`attempt_task_id`,照计划 4 的做法),所以每次尝试各记一条成本。传了桥接时 `run_id` 必须非空,否则在调用桥接之前抛 `MindAgentDataError`;`verify` 传了 judge 时同理。不用桥接的路径 `run_id` 可以保持缺省 `""`。** —— 原因:计划 1 的账本对同一 `(task_id, cost_type)` 只记一次,复用 task_id 会把重试的成本静默丢掉;账本还拒绝空的、含分隔符的、`.`/`..` 的 `run_id`,在入口挡住比在记账处抛错清楚。计划 6 的 `Skill.from_dict` 要求 `skill-` 前缀;优先级 100 让候选在路由里压过现有的默认优先级 0 的技能,验证器才能看到它被选中;让外部模型改触发条件会让"候选是否针对这个信号"无法保证。 —— 判错的代价:库里已有优先级高于 100 的技能时,候选在验证里不会被选中、会被筛掉(任务 87 的 `test_dropped_drafts_are_recorded_in_the_round` 正是这种情形),要人调优先级。
9. **Ruling: R18 落地:`propose` 对桥接的 `BridgeError`(T1、T2)一律原样上抛,由轮次处理器升级为 T3(轮次 `failed` + 失败记录,`retries` 取 `max(BridgeError.attempts - 1, 0)`,即已重试次数,与计划 1 的口径和本文件另外两条路径一致)。解析耗尽的 `ProposalParseError` 同样判 T3,`retries` 为已重试次数。** —— 原因:设计 R18 与 §5.1"调用方的升级规则":桥接自己已退避重试过,留 `pending` 会让守护循环无限空转外部推理。任务说明里"桥接的 T1 原样上抛"指的是 `propose` 这一层;轮次处理器按 R18 收口。 —— 判错的代价:外部工具短暂不可用也会让这一轮 `failed`,下一个模型晋级前不会再自动开轮(人可以删掉轮次产物重开)。
10. **Ruling: 记忆增量生成器 V1 不做;`propose` 只产出 `kind: skill` 的增量。`kind: memory` 的路径在数据模型(`increments[].kind`)、收录处理器、`load_library` 里全部可用,任务 86 用手写的记忆提案测试。** —— 原因:从发现里抽取"对方反复声明的事实"需要语义理解,规则模板做不出有意义的记忆条目。 —— 判错的代价:记忆库只能靠人手写提案增长。
11. **Ruling: 验证器的度量口径:只重放发现引用的动机 episode;对方轮次照抄保存下来的原话(`inner_thought`、`activation_trace` 抹掉),agent 轮次由实例重新生成;基线实例 = 当前库,候选实例 = 当前库 + 草案技能(两者都由 `base_instance_factory(extra_skills)` 新建)。"动机轮次"是重放里该信号在 agent 发言前为真的 agent 轮次;`fired_on_motivating` = 候选实例在动机轮次上选中草案技能的比例;`fired_elsewhere` = 候选实例在动机 episode 其余 agent 轮次上选中它的比例;分母为 0 时记 0.0。`notes` 写明两个分母、基线在动机轮次上选中草案技能的次数,以及固定的过拟合说明。传入 `judge` 时把各动机轮次的"基线回复 / 候选回复"拼成一个提示问一次,回答原样存进 `quality_notes`;不传就不写这个键。** —— 原因:R13 只度量"行为是否按预期改变";`fired_elsewhere` 不重放非动机 episode,是因为 V1 的验证数据就是触发提案的那一批(设计已知限制),额外重放别的 episode 也只是同一批数据。 —— 判错的代价:`fired_elsewhere` 低估了候选在完全不同的对话里的误触发;审核人看到的 `notes` 写明了这一点。
12. **Ruling: 打包前按 `AgentConfig` 的阈值筛选:`fired_on_motivating < min_fired_on_motivating`(默认 0.8)或 `fired_elsewhere > max_fired_elsewhere`(默认 0.2)的草案不打包,记进轮次产物的 `dropped`(`{finding_id, reason, fired_on_motivating, fired_elsewhere}`);通过的草案按原顺序编号 `proposal-<round_id>-<n>`(`n` 从 1 连续编号,被筛掉的不占号)。提案载荷在设计 §4.2 的四个键之外多写 `run_id`、`bound_model_id`、`generator`;`links = [round_id, finding_id]`。重入时:同 id 的提案已不是 `pending_review`(人已审过)就原样保留,是 `pending_review` 且载荷相同就不重写,载荷不同就就地覆盖。** —— 原因:编号连续让审核人看到的序号就是提案序号;`run_id` 让收录处理器的失败记录有处可挂;审过的提案不能被机器改写。 重入后若本次通过的草案比上一次少,编号超出本次数目、仍为 `pending_review` 的旧提案(`stale_proposals`)原样留在树里不删不改,但写进轮次产物的 `dropped`(`{proposal_id, reason: "stale: left by an earlier run of this round, not produced again; do not approve"}`),提醒审核人。 —— 原因:编号连续让审核人看到的序号就是提案序号;`run_id` 让收录处理器的失败记录有处可挂;审过的提案不能被机器改写;删除审核类产物没有状态迁移可用。 —— 判错的代价:同一轮重入时若筛选结果变了(库变化导致),编号会挪位;旧的多余提案仍可能被人误批,审批前请核对 `dropped` 与提案所属的 `round_id`。
13. **Ruling: 轮次处理器的流程与终态:**
    - 轮次产物已 `executed` 或 `failed`:空操作(不写、不提交)。
    - 先把绑定模型版本低于"当前最大 effective 版本"、仍为 `pending` 的轮次置 `failed`,`payload.failure = {"category": "superseded", "reason": "superseded by zengen-v<N>", "point": "mindagent.rounds.supersede"}`;动作自己的模型若也不是最新(过期动作),它的轮次同样置 superseded,不跑。superseded 不写失败记录(不是失败,是被取代)。
    - 轮次产物文件在但读不出(Ruling 20):用新信封整份重写为 `failed`,原因 `unreadable round product`,类别 T3,按 Ruling 21 的守卫记一次失败记录;不部署、不再往下走。
    - 模型产物不存在、不是 `effective`、`links` 不足两项:轮次 `failed`(T3),原因写在轮次产物里;**不写失败记录**,因为没有 run 可挂。
    - 其余确定性错误(情境或人格不在树里、库条目不合法、轨迹引用了库外技能、不可重跑的会话失败、解析耗尽、任何 `BridgeError`):轮次 `failed`,`payload.failure = {category: "T3", reason, point}`,并经 Ruling 21 的守卫 `fail_once(store, <绑定模型的 run>, task_id="agent-<model>", category="T3", point, retries, reason)` 记一次失败记录。
    - 非桥接的瞬态(`DeploymentFailedError.transient`)与其余异常:原样上抛,轮次保持 `pending`。**瞬态失败后轮次保持 `pending`,调和循环会在下一个 tick 重新触发 `start_agent_round`(R19:触发条件是"该模型没有轮次产物,或轮次产物为 `pending`"),处理器重入必须幂等。** 本处理器的幂等键:已落盘且库版本一致的部署 episode 不再新建会话(Ruling 2);提案 id 由轮次 id 与编号确定(Ruling 12);终态(`executed`、`failed`)直接返回。
    - 成功:写 `episode_ids`、`findings`(含 `signal`)、`proposals`、`dropped`(筛掉的草案,加上 Ruling 12 的过期提案),轮次置 `executed`。
    —— 原因:设计 §3.3"处理器自己负责写终态记录"、§8.3 与 R19;`failure.category` 用 `superseded` 区分"被取代"与"失败"。 —— 判错的代价:模型产物坏了时只有轮次产物里有原因,`observe.trace` 看不到失败记录;R19 下一个反复瞬态失败的轮次会被每个 tick 重新唤醒(每次都先复用已落盘的 episode,只补跑缺的会话)。
14. **Ruling: 轮次产物 `agent_round`:id `agent-round-<model>`,`type: agent_round`,`links = [model_id, run_id]`,载荷键 `model_id、model_version、run_id、checkpoint_ref(模型有时)、library_version{skill, memory}、scenario_ids、persona_ids、bindings、analyzer_config{min_support, min_ratio}、verifier_config{method, min_fired_on_motivating, max_fired_elsewhere}、generator_version、uses_bridge、prompt_template_version(仅用桥接时)、episode_ids、findings、proposals、dropped、failure`。重入时整份按当前绑定重写(保留首次的 `created_at`)。** —— 原因:设计 §4.2 的 agent_rsi 配方键(`bound_model_id、scenario_ids、persona_ids、analyzer_config、verifier_config、generator_version、prompt_template_version`)与计划 3 Ruling 9 新增的 `uses_bridge` 都能从轮次产物里取到,日后写 agent_rsi 配方候选不用回头猜。 —— 判错的代价:无。
15. **Ruling: 正式库:`load_library(store) -> LibraryState(skills, memories, version)` 读 `skill_library/`、`memory_library/` 下状态为 `effective` 的条目(按 id 排序),版本取 `library_state` 的 `skill_version`、`memory_version`,经计划 6 的 `normalize_library_version` 规范成字符串;`library_state` 不存在时是 `{"skill": "0", "memory": "0"}`。`library_state` 产物:id `library-state`,`type: library_state`,状态 `executed`,载荷 `{skill_version: str, memory_version: str, admitted: [{proposal_id, skill_version, memory_version, entries: [条目 id...]}]}`,信封 `version` 每次收录加一,`links = [最近一次收录的提案 id]`。** **正式库的两个版本必须是十进制数字串**(`"0"`、`"12"`;手写的非负整数也接受并规范成字符串):`read_library_versions` 校验,不合法(如计划 6 允许的 `"lib-3"`、空串、`"3.0"`、全角数字、`bool`、负数)抛 `MindAgentDataError`。收录处理器把它当作确定性失败走现有路径(写 `admit-<提案 id>` 失败记录、提案留在 `approved`),`ValueError` 不会逃出处理器;轮次处理器把它判 T3。 —— 原因:设计 §8.3 修订后规定库版本一律存成字符串,与激活轨迹同型;收录要把它按整数加一,非数字的标识无法递增,不在处理器入口挡住就会在 `int()` 处抛出,让守护进程每个 tick 都崩。`admitted` 既是"记录来源提案 id",也是收录的幂等键。 —— 判错的代价:想用 `lib-2026-10-09` 这类非数字库版本标识的人,要改 `read_library_versions` 与收录的递增规则。手写的整数版本也能读,但收录写回时变成字符串。
16. **Ruling: 收录处理器:提案不是 `approved` 时空操作。先把全部增量校验一遍(不合法则什么库条目都不写);再按增量顺序写条目——技能 id `skill_id_for(提案 id, k)` = `skill-<提案 id 去掉 proposal- 前缀>-<k>`,记忆 id `mem-<同上>-<k>`(`k` 是增量在列表里的位置,从 1 起),定义里的 `id` 被替换成它,状态 `effective`,`links = [提案 id]`,已存在就不重写;然后若 `admitted` 里还没有该提案,技能库版本在有技能增量时 +1、记忆库版本在有记忆增量时 +1,追加一条 `admitted`;最后提案 `approved → effective`。增量不合法时:审核类状态机里 `approved` 只能去 `effective`,无处可放"失败",所以经 Ruling 21 的守卫记一次 **T2** 失败记录(`task_id = admit-<提案 id>`,挂在提案载荷的 `run_id` 下),提案原样留在 `approved` 等人处理;此后每个 tick 重入只读不写。类别按设计 §11 的约定:T3 表示闭环因此停在 `failed`、等人处理(规划决策、agent 轮次);T2 表示确定性数据错误、处理器空操作并只记一次(收录处理器,与计划 3 的配方收录一致)。轮次层面的失败仍是 T3。** —— 原因:三步之间崩溃,重跑时条目已在就跳过、`admitted` 已记就不再递增,所以收敛且不重复计数(任务 86 对每个崩溃点都有故障注入测试);两个收录处理器同类错误记同一个类别,读失败记录的人不必区分来源。 —— 判错的代价:一条坏提案会让 `admit_proposal` 每个 tick 都被调和循环产生一次(处理器空转,不写任何东西),直到人把提案改好或改状态。T2 与 T3 只是留痕分类,不改变任何行为:处理器怎么走、写什么,都与类别无关。
17. **Ruling: 部署时 SimpleActio 实例的 `profile` 是 `{"model_id", "round_id"}`;模型后端由注入的 `backend_factory(model_id, checkpoint_ref)` 每次新建(部署一份、验证时基线与候选各一份);人格由注入的 `personas_loader(store, persona_ids)` 提供;情境从树里读。** —— 原因:真实后端如何从 checkpoint 加载属于计算层,不在本期;测试注入回显指导语的罐头后端。 —— 判错的代价:无。
18. **Ruling: 本包的处理器类型写成字面的 `Callable[[GitStore, Action], None]`(`mindagent.rounds.Handler` 只是本模块内的别名),不从 `controlplane.daemon` 导入。** —— 原因:设计 §11 修订:`Handler` 别名由计划 8 定义,计划 7 排在它之前执行。 —— 判错的代价:无。
19. **Ruling: 与设计 §8.3 签名的三处偏差:**
    - **`verify(draft, findings, episodes, *, base_instance_factory)` 加 keyword-only 的 `personas`、`scenarios`、`judge=None`、`run_id=""`、`task_id="mindagent-judge"`。** 原因:重放要调 `instance.respond(persona, scenario, ...)`,两者都不在 episode 里(episode 只有 id);judge 是设计里"可选传入桥接做质量评分"的入口;`run_id`、`task_id` 是桥接记账必需的。判错的代价:调用方多传两个字典;按设计签名调用会报缺参。
    - **`propose(findings, *, bridge=None)` 加 keyword-only 的 `run_id=""`、`task_id="mindagent-propose"`(以及 `max_parse_retries=2`)。** 原因:桥接每次调用都要 `run_id` 与 `task_id` 记账;解析重试上限要可配。判错的代价:无,不用桥接时按设计签名调用照样成立。
    - **设计里的 `handle_start_agent_round(...)` 改成工厂 `make_start_round_handler(*, config, client_factory, backend_factory, personas_loader, bridge=None)`,返回 `Callable[[GitStore, Action], None]`。** 原因:处理器的统一签名只有 `(store, action)`,而轮次要用的配置、客户端工厂、模型后端工厂、人格加载器、桥接都得从外面注入;用闭包工厂把它们绑进去,注册时只交出那个二参函数。`handle_admit_proposal` 不需要注入任何东西,保持设计里的名字与形状。判错的代价:组合根(计划 9)要先调工厂再注册;计划 9 的假设核对脚本按 `mindagent.rounds.make_start_round_handler` 这个名字与位置读,不要改名。
20. **Ruling: 轮次产物读不出时由处理器自己写 `failed`,不抛。** "读不出"与计划 8 的 `_agent_round_status` 同一组异常(`mindagent.failures.UNREADABLE`):YAML 解析失败(含合并冲突标记)、`KeyError`、`TypeError`、`AttributeError`、`EnvelopeValidationError`、`UnknownSchemaVersionError`;`OSError` 不在其中,原样上抛(瞬态)。处理器、`supersede_stale_rounds`、`_record` 读轮次产物一律走 `read_or_none`,不直接 `store.read_product`。处理器遇到"文件在但读不出":用新信封把轮次整份重写为 `failed`,`payload.failure = {"category": "T3", "reason": "unreadable round product", "point": "mindagent.rounds.read"}`,run id 取绑定模型产物 `links[1]`(模型也读不出时为空),按 Ruling 21 记一次失败记录(`task_id = agent-<model>`);之后它是终态,再触发是空操作。`supersede_stale_rounds` 遇到读不出的条目跳过,不抛,留给该模型自己的 `start_agent_round` 处理。 —— 原因:计划 8 的契约(其"轮次处理器的重入契约"与 Ruling 20)是轮次产物不存在、读不出或为 `pending` 都会触发 `start_agent_round`,读不出的由处理器写 `failed`;处理器若照常 `read_product`,会在坏文件上抛异常、守护进程退出、重启后再触发,形成崩溃循环。 —— 判错的代价:人手改坏的轮次产物会被机器整份覆盖成 `failed`,原文留在 git 历史里;想恢复要从历史里取回再改。
21. **Ruling: 失败记录前先校验 run id,守卫照计划 3 的 `_fail_once`(`mindagent.failures.fail_once` 与同名辅助 `_single_segment`)。** run id 不是非空字符串、不是单个路径段(含 `/`、`\`,或为 `.`、`..`)、或 `runs/<run>/` 目录不存在,都不写失败记录,原因只留在产物上(轮次产物的 `payload.failure`;收录时提案原样留在 `approved`);同一 `(run, task)` 已记过也不再写。两个处理器的全部失败记录都经它写,不直接调 `record_failure`。 —— 原因:run id 来自树里的数据(提案载荷的 `run_id`、模型产物的 `links[1]`),含斜杠时 `record_failure` 抛 `ValueError` 逃出处理器;指向不存在的 run 时会凭空新建一个只有失败记录的 run 目录。 —— 判错的代价:run id 坏掉的失败只在产物上可见,`observe.trace` 看不到。

## 对外契约:对计划 6 的假设

本计划用到的每个 `simple_actio` 名字与激活轨迹字段如下。撰写结束时已与 `docs/superpowers/plans/2026-10-09-simple-actio.md` 的最终文本逐条核对(把该计划的全部代码块原样抽出、按其任务顺序应用到拷贝里,再跑本计划的全部测试),**全部一致**;核对出的一处差异(id 前缀规则)已在本计划里改正,见下表末行。

| 名字 / 字段 | 本计划怎么用 | 核对 |
|---|---|---|
| `simple_actio.instance.instantiate(model_backend, *, skills, memories, library_version, profile, experiences=(), registry=None, memory_limit=3) -> SimpleActioInstance` | 部署与验证时建实例 | 一致 |
| `SimpleActioInstance.respond(persona, scenario, history, speaker_slot) -> (utterance, inner_thought, trace)` | 部署驱动器每轮调用;验证器重放 | 一致 |
| `SimpleActioInstance.library_version`(规范化后的 `{"skill": str, "memory": str}`)、`SimpleActioInstance.registry` | 部署 episode 复用判定;验证器求信号 | 一致 |
| `GuidedBackend.respond_guided(persona, scenario, history, speaker_slot, guidance) -> (utterance, inner_thought)`;只有 `respond` 的后端被 `IgnoreGuidance` 包装 | 测试的罐头后端回显指导语 | 一致 |
| `simple_actio.skills.Skill`(`id、version、level、trigger、policy_instructions、priority、description`;`to_dict` / `from_dict` 形状见计划 6"对外契约") | 候选技能定义、收录、读库 | 一致 |
| `simple_actio.memory.MemoryEntry`(`from_dict` 必填 `id、version、subject、content、timestamp、confidence`,可选 `keywords、source_episode`) | 收录记忆增量、读库 | 一致 |
| `simple_actio.errors.SimpleActioDataError(ValueError)` | 读库、收录、生成器的确定性错误 | 一致 |
| `simple_actio.signals.default_registry()`、`SignalRegistry.get(name)`、`in` | 分析器与验证器重新求信号 | 一致 |
| 内置信号 `emotion_high`、`no_new_information`、`first_turn`,签名 `(history, speaker_slot) -> bool` | 分析器默认看前两个 | 一致 |
| `simple_actio.triggers.referenced_signals(trigger) -> frozenset[str]`(偶数层 `not` 之下) | "技能是否针对某信号" | 一致 |
| `simple_actio.trace.validate_trace(trace)`、`normalize_library_version(lv)` | 测试校验落盘轨迹;读库时规范化版本 | 一致 |
| 轨迹键 `schema、signals、candidates、selected、conflict、memory_hits、experience、library_version`;`selected == candidates[0]` 或 `None`;`library_version` 是两个字符串;没有数字 | 分析器只读 `selected`;复用判定读 `library_version`;端到端断言 `candidates`、`conflict` | 一致 |
| "没有轨迹"= `TurnRecord.activation_trace is None`;对手轮次落盘恒为 `None` | 分析器把 `None` 当裸模型 | 一致 |
| `EnvironmentServiceClient.submit_turn(utterance, inner_thought=None, *, activation_trace=None)`;服务端回显客户端自己的轮次时保留轨迹 | 部署驱动器 | 一致 |
| runner 接受三元组;`ClientDrivenBackend.submit(..., *, activation_trace=None)`;`SessionDriver.submit_turn(..., *, activation_trace=None)`;`SubmitTurn.activation_trace = 3` | 端到端经真实服务端 | 一致(与计划 6 的差异块逐文件相同) |
| `SKILL_ID_PREFIX == "skill-"`、`MEMORY_ID_PREFIX == "mem-"`,`from_dict` 与直接构造都检查 | 候选技能 id、收录后的条目 id | **撰写中途改正**:初稿的候选 id 是 `cand-<signal>`,会被拒;改为 `skill-candidate-<signal>`(Ruling 8),收录 id 本来就带前缀 |

每个任务的 Step 0 用一条 `python -c` 断言上表的名字可导入;不通过就停下,报告缺的是哪一个,不要往下做。

**经调和循环的重入(R19):** 本计划只直接调用处理器测重入(崩在各步之间、瞬态失败后再调一次)。"`pending` 的轮次会被调和循环重新派发"这一半,由计划 8 的 `agent_round_status`(`start_agent_round` 在轮次产物为 `pending` 时也触发)与计划 9 的任务 108 经调和循环验证;本计划先于计划 8 执行,写不了经 `reconcile_once(handlers=...)` 的测试。

## 跨语言约定:提案载荷里被 TypeScript 命令行读取的键

计划 8 的 `cli/src/commands.ts`(`summarizeProposal` 与 `approve` 的证据检查)读下面这些键,本计划的 `mindagent.packaging.proposal_payload` 是唯一写方。键名与嵌套层次不得改动:

| 键 | 类型 | 命令行怎么用 |
|---|---|---|
| `payload.round_id` | str | `review` 第一行 |
| `payload.finding.id`、`.pattern` | str | `review` |
| `payload.finding.support` | int | `review` |
| `payload.finding.ratio` | float | `review`(格式化为小数) |
| `payload.finding.episode_refs` | list[str] | `review` 打印其长度 |
| `payload.increments` | list | `review` 逐条打印;空列表打印 `none` |
| `payload.increments[].kind` | `"skill"` \| `"memory"` | `review` |
| `payload.increments[].definition` | dict | `review` 打印其 JSON |
| `payload.verification` | 非空 dict | `approve` 缺它就拒绝批准 |
| `payload.verification.method` | str(`"replay_behavior"`) | `review` |
| `payload.verification.fired_on_motivating`、`.fired_elsewhere` | float | `review` |
| `payload.verification.episodes` | list[str] | `review` 打印其长度 |
| `payload.verification.notes` | str | `review`(非空才打印) |

命令行不读、但本计划额外写的键:`payload.run_id`、`payload.bound_model_id`、`payload.generator`、`payload.verification.quality_notes`(仅传了 judge 时)。已与计划 8 的文本(其"对外契约"表里"提案载荷"一行,以及其 `summarizeProposal` 源码)逐键核对,一致。

## Global Constraints

每个任务的要求都隐含包含本节。

- **`src/` 下每个源文件以 `from __future__ import annotations` 开头**(模块 docstring 之后的第一条语句也算;任务 80 有静态测试对 `src/mindagent/*.py` 强制)。
- **不引入新第三方依赖。** `pyproject.toml` 只改 wheel 包列表一行:加 `src/mindagent`(`tests/test_mt_packaging.py` 强制检查)。
- **依赖方向:** `mindagent` 只导入 `controlplane`、`simple_actio`、`inference_bridge`、`simulator`(`datamodel`、`store_adapter`、`realtime.client`)、`grpc`、`yaml`(已有依赖,`failures.py` 用它判读取异常)与标准库;不导入 `mindtrainer`、`diagnostic`;`controlplane` 不导入 `mindagent`(任务 80 有测试)。
- **凭证绝不进 git 树。** 本计划不读写任何凭证;桥接只经注入的对象调用。
- **读写产物走 `GitStore` + `Envelope`。** 不直接碰文件系统读写产物。
- **不改的文件:** `src/controlplane/`、`src/simulator/`、`src/simple_actio/`、`src/inference_bridge/`、`src/mindtrainer/`、`src/diagnostic/` 下的一切;`schemas/`;既有测试;`cli/` 下的一切,尤其 **不要碰** `cli/package.json`、`cli/package-lock.json`。
- **`git add` 逐个文件**,不用 `git add .` / `-A`。
- **提交信息不加 `Co-Authored-By` 行**(用户明确要求)。
- **测试命令**一律是 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`(下文简写为"全量命令";单文件时在其后加文件路径)。
- **脱离真实环境可测**:不需要 GPU、外部网络、真实模型;gRPC 测试用真实 loopback 服务端;桥接用 `ScriptedBridge`;全部测试用 `tmp_path` 隔离。
- **测试基线 B**:开工时在 `main`(计划 1 与计划 6 已合并)上跑一次全量命令得到。本计划各任务的增量依次为 **+27、+19、+19、+20、+8、+8、+22、+28、+1**(任务 80 至 88),任务 89 为 +0,共 **+152**;各任务结束后应为 B+27 → B+46 → B+65 → B+85 → B+93 → B+101 → B+123 → B+151 → B+152 → B+152,不得出现警告。撰写时以拷贝里的 B = 1197 实测为 1224 → 1243 → 1262 → 1282 → 1290 → 1298 → 1320 → 1348 → 1349 → 1349,详见"未验证项"第 1 条。

## File Structure

| 文件 | 动作 | 任务 | 职责 |
|---|---|---|---|
| `pyproject.toml` | 修改(1 行) | 80 | wheel 包列表加 `src/mindagent` |
| `src/mindagent/__init__.py` | 新建(4 行) | 80 | 包说明 |
| `src/mindagent/errors.py` | 新建(24 行) | 80 | `MindAgentDataError`、`ProposalParseError`、`AgentRoundFailedError` |
| `src/mindagent/config.py` | 新建(56 行) | 80 | `AgentConfig`、`agent_slot_of` |
| `src/mindagent/library.py` | 新建(84 行) | 80 | `LibraryState`、`load_library`、`read_library_versions`、`skill_id_for`、`memory_id_for` |
| `tests/test_ma_config_library.py` | 新建(27 个用例) | 80 | 含依赖方向与 `__future__` 的静态检查 |
| `src/mindagent/deployment.py` | 新建(181 行) | 81 | `run_deployment_episodes`、`DeploymentFailedError`、`deployment_episode_id` |
| `tests/test_ma_deployment.py` | 新建(19 个用例) | 81 | 真实 loopback 服务端 + 替身客户端 |
| `src/mindagent/analysis.py` | 新建(139 行) | 82 | `analyze`、`BehaviorFinding`、`finding_id`、`visible_history`、`signal_turns` |
| `tests/test_ma_analysis.py` | 新建(19 个用例) | 82 | |
| `src/mindagent/proposals.py` | 新建(146 行) | 83 | `propose`、`ProposalDraft`、`parse_strategy`、`attempt_task_id` |
| `tests/test_ma_proposals.py` | 新建(20 个用例) | 83 | |
| `src/mindagent/verify.py` | 新建(134 行) | 84 | `verify`、`replay`、`Verification` |
| `tests/test_ma_verify.py` | 新建(8 个用例) | 84 | |
| `src/mindagent/packaging.py` | 新建(141 行) | 85 | `package`、`screen`、`stale_proposals`、`Thresholds`、`proposal_payload`、`proposal_id_for` |
| `tests/test_ma_packaging.py` | 新建(8 个用例) | 85 | |
| `src/mindagent/failures.py` | 新建(48 行) | 86 | `UNREADABLE`、`read_or_none`、`_single_segment`、`fail_once` |
| `src/mindagent/admit.py` | 新建(107 行) | 86 | `handle_admit_proposal` |
| `tests/test_ma_admit.py` | 新建(22 个用例) | 86 | 含故障注入、坏 run id |
| `src/mindagent/rounds.py` | 新建(294 行) | 87 | `make_start_round_handler`、`supersede_stale_rounds` |
| `tests/test_ma_rounds.py` | 新建(28 个用例) | 87 | 含故障注入、读不出的轮次产物、坏 run id |
| `tests/test_ma_e2e.py` | 新建(1 个用例) | 88 | 真实 loopback 服务端上的完整 AgentRSI 环路 |
| `docs/superpowers/plans/2026-10-09-mindagent.md` | 新建 | — | 本计划 |

**打包说明:** `pyproject.toml` 只改 wheel 包列表一行。计划 1、计划 6 已经把 `src/inference_bridge`、`src/simple_actio` 加进列表;本计划在列表末尾追加 `"src/mindagent"`。

---

### Task 80: 包骨架、`AgentConfig` 与正式库读取

**Files:**
- Modify: `pyproject.toml`(wheel 包列表追加 `src/mindagent`)
- Create: `src/mindagent/__init__.py`、`src/mindagent/errors.py`、`src/mindagent/config.py`、`src/mindagent/library.py`
- Test: `tests/test_ma_config_library.py`

**Interfaces:**
- Consumes: `simple_actio.skills.Skill.from_dict`、`simple_actio.memory.MemoryEntry.from_dict`、`simple_actio.trace.normalize_library_version`;`controlplane.paths.library_state_file`、`CONTROL_PLANE_ROOT`;`simulator.datamodel.RoleBinding`。
- Produces(任务 81 至 88 依赖):
  - `MindAgentDataError(ValueError)`、`ProposalParseError(reason, *, attempts)`(`MindAgentDataError` 的子类)、`AgentRoundFailedError(reason, *, point)`。
  - `AgentConfig(scenario_ids, bindings, max_turns=6, episodes_per_scenario=1, max_retries_per_episode=1, min_support=3, min_ratio=0.5, min_fired_on_motivating=0.8, max_fired_elsewhere=0.2, max_parse_retries=2)`(冻结,`agent_slot` 属性);`agent_slot_of(bindings) -> str`。
  - `LibraryState(skills: tuple[Skill, ...], memories: tuple[MemoryEntry, ...], version: {"skill": str, "memory": str})`;`load_library(store)`;`read_library_versions(store) -> dict`;`LIBRARY_STATE_ID = "library-state"`;`skill_id_for(proposal_id, k)`、`memory_id_for(proposal_id, k)`。

**设计要点:**
- `AgentConfig` 不落进树(Ruling 3),构造时就校验:情境非空不重复、`bindings` 里恰好一个 `self_play`、整数下限、比例在 `[0, 1]`(`bool` 不算数)。
- `load_library` 只收 `effective` 的条目,按 id 排序;版本先经 `read_library_versions` 校验为十进制数字串,再经 `normalize_library_version` 规范成字符串(Ruling 15),手写的非负整数版本也能读。
- 收录后的条目 id 由提案 id 与位置推出(Ruling 16),`proposal-` 前缀缺失或其后为空抛 `ValueError`。
- 静态测试:`src/mindagent/*.py` 全部以 `from __future__ import annotations` 开头、不导入 `mindtrainer`、`diagnostic`、`recipestore`、`trainplanner`、`trainscheduler`、`factory`;`src/controlplane/*.py` 不出现 `mindagent`。
- 库版本必须是十进制数字串(Ruling 15):`read_library_versions` 对 `"lib-3"`、空串、`"-1"`、`"3.0"`、`None`、`True`、`-1`、全角数字都抛 `MindAgentDataError`(8 个参数化反例)。

- [ ] **Step 0: 前置检查(计划 1、计划 6 已合并)**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev python -c "import inspect; from simple_actio.instance import instantiate; from simple_actio.skills import Skill, SKILL_ID_PREFIX; from simple_actio.memory import MemoryEntry, MEMORY_ID_PREFIX; from simple_actio.trace import validate_trace, normalize_library_version; from simple_actio.triggers import referenced_signals; from simple_actio.signals import default_registry; from inference_bridge import BridgeError, ScriptedBridge; from controlplane.paths import agent_round_file, proposal_file, skill_file, memory_file, library_state_file, failure_record_file; from controlplane.ledger import record_failure; from simulator.realtime.client import EnvironmentServiceClient; assert 'activation_trace' in inspect.signature(EnvironmentServiceClient.submit_turn).parameters; assert SKILL_ID_PREFIX == 'skill-' and MEMORY_ID_PREFIX == 'mem-'; print('prereqs ok')"
```

Expected: 最后一行 `prereqs ok`。任何 `ImportError`、`AttributeError` 或 `AssertionError` 都说明计划 1 或计划 6 没有按其文本合并:**停下**,把报错原样报告,不要往下做。

- [ ] **Step 0b: 记下开工基点 BASE**

```bash
BASE=$(git rev-parse HEAD); echo "BASE=$BASE"
```

把输出的 `BASE=<sha>` 原样记进执行记录(任务 89 的改动范围检查要用;换了终端会话 shell 变量就没了,所以必须记下来)。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_ma_config_library.py`:

```python
import ast
from pathlib import Path

import pytest

from simple_actio.skills import Skill
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import library_state_file, memory_file, skill_file
from mindagent.config import AgentConfig, agent_slot_of
from mindagent.errors import MindAgentDataError
from mindagent.library import LibraryState, load_library, memory_id_for, read_library_versions, skill_id_for
from simulator.datamodel import RoleBinding

SRC = Path(__file__).resolve().parents[1] / "src"
AGENT = RoleBinding("a", "pa", "self_play")
OPP = RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _env(id_, type_, status, payload):
    return Envelope(id=id_, type=type_, version=1, created_at="2026-10-09T00:00:00Z",
                    created_by=OwnerRef(), status=status, schema_version=1, payload=payload)


def _skill(sid, **kw):
    d = {"id": sid, "version": 1, "level": "micro", "trigger": {"signal": "emotion_high"},
         "policy": {"instructions": ["x"]}}
    d.update(kw)
    return d


def _memory(mid):
    return {"id": mid, "version": 1, "subject": "pb", "content": "对方住在北京",
            "timestamp": "2026-10-01T00:00:00Z", "confidence": 0.8}


def test_config_defaults_and_agent_slot():
    cfg = AgentConfig(scenario_ids=("s1",), bindings=(OPP, AGENT))
    assert cfg.agent_slot == "a"
    assert (cfg.max_turns, cfg.episodes_per_scenario, cfg.max_retries_per_episode) == (6, 1, 1)
    assert (cfg.min_support, cfg.min_ratio) == (3, 0.5)
    assert (cfg.min_fired_on_motivating, cfg.max_fired_elsewhere, cfg.max_parse_retries) == (0.8, 0.2, 2)
    with pytest.raises(AttributeError):
        cfg.min_support = 1


@pytest.mark.parametrize("kw,match", [
    ({"scenario_ids": ()}, "scenario_ids"),
    ({"scenario_ids": ("s1", "s1")}, "must not repeat"),
    ({"bindings": (OPP,)}, "exactly one self_play"),
    ({"bindings": (AGENT, RoleBinding("c", "pc", "self_play"))}, "exactly one self_play"),
    ({"max_turns": 0}, "max_turns"),
    ({"episodes_per_scenario": 0}, "episodes_per_scenario"),
    ({"max_retries_per_episode": -1}, "max_retries_per_episode"),
    ({"min_support": 0}, "min_support"),
    ({"min_support": True}, "min_support"),
    ({"min_ratio": 1.5}, "min_ratio"),
    ({"max_fired_elsewhere": -0.1}, "max_fired_elsewhere"),
    ({"max_parse_retries": -1}, "max_parse_retries"),
])
def test_config_rejects_nonsense(kw, match):
    base = {"scenario_ids": ("s1",), "bindings": (OPP, AGENT)}
    base.update(kw)
    with pytest.raises(ValueError, match=match):
        AgentConfig(**base)


def test_agent_slot_of_needs_exactly_one_self_play():
    assert agent_slot_of([AGENT, OPP]) == "a"
    with pytest.raises(ValueError):
        agent_slot_of([OPP])


def test_an_empty_tree_is_an_empty_library_at_version_zero(store):
    lib = load_library(store)
    assert lib == LibraryState(skills=(), memories=(), version={"skill": "0", "memory": "0"})
    assert read_library_versions(store) == {"skill_version": "0", "memory_version": "0", "admitted": []}


def test_load_library_reads_effective_entries_sorted_and_the_versions(store):
    store.write_product(skill_file("skill-b"), _env("skill-b", "skill", "effective", _skill("skill-b")), "m")
    store.write_product(skill_file("skill-a"), _env("skill-a", "skill", "effective", _skill("skill-a")), "m")
    store.write_product(skill_file("skill-old"), _env("skill-old", "skill", "deprecated", _skill("skill-old")), "m")
    store.write_product(memory_file("mem-a"), _env("mem-a", "memory", "effective", _memory("mem-a")), "m")
    store.write_product(library_state_file(), _env("library-state", "library_state", "executed",
                        {"skill_version": "3", "memory_version": "1", "admitted": []}), "m")
    lib = load_library(store)
    assert [s.id for s in lib.skills] == ["skill-a", "skill-b"]
    assert isinstance(lib.skills[0], Skill)
    assert [m.id for m in lib.memories] == ["mem-a"]
    assert lib.version == {"skill": "3", "memory": "1"}


def test_integer_versions_written_by_hand_are_normalized_to_strings(store):
    store.write_product(library_state_file(), _env("library-state", "library_state", "executed",
                        {"skill_version": 2, "memory_version": 0, "admitted": []}), "m")
    assert load_library(store).version == {"skill": "2", "memory": "0"}


@pytest.mark.parametrize("bad", ["lib-3", "", "-1", "3.0", None, True, -1, "３"])
def test_a_non_decimal_library_version_is_a_data_error(store, bad):
    store.write_product(library_state_file(), _env("library-state", "library_state", "executed",
                        {"skill_version": bad, "memory_version": "0", "admitted": []}), "m")
    with pytest.raises(MindAgentDataError, match="skill_version must be a decimal string"):
        read_library_versions(store)
    with pytest.raises(MindAgentDataError):
        load_library(store)


def test_entry_ids_are_derived_from_the_proposal_id():
    assert skill_id_for("proposal-agent-round-zengen-v3-1", 1) == "skill-agent-round-zengen-v3-1-1"
    assert memory_id_for("proposal-agent-round-zengen-v3-1", 2) == "mem-agent-round-zengen-v3-1-2"
    with pytest.raises(ValueError):
        skill_id_for("agent-round-zengen-v3-1", 1)
    with pytest.raises(ValueError):
        skill_id_for("proposal-", 1)


def test_every_mindagent_source_starts_with_future_annotations_and_controlplane_does_not_import_it():
    for path in sorted((SRC / "mindagent").glob("*.py")):
        body = ast.parse(path.read_text(encoding="utf-8")).body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        assert isinstance(body[0], ast.ImportFrom) and body[0].module == "__future__", path.name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mods = {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        mods |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not mods & {"mindtrainer", "diagnostic", "recipestore", "trainplanner", "trainscheduler", "factory"}, path.name
    for path in sorted((SRC / "controlplane").glob("*.py")):
        assert "mindagent" not in path.read_text(encoding="utf-8"), path.name
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ma_config_library.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindagent'`,末行 `1 error in ...`。

- [ ] **Step 3: 写实现**

`pyproject.toml` 的 wheel 包列表末尾追加 `"src/mindagent"`(不论前面已有哪些包,只加这一项):

```bash
python3 - <<'EOF'
import pathlib, re
p = pathlib.Path("pyproject.toml")
s = p.read_text(encoding="utf-8")
line = re.search(r'^packages = \[.*\]$', s, re.M).group(0)
if '"src/mindagent"' in line:
    print("already listed")
else:
    p.write_text(s.replace(line, line[:-1] + ', "src/mindagent"]'), encoding="utf-8")
EOF
grep -n '^packages' pyproject.toml
```

Expected: `grep` 输出的那一行以 `"src/mindagent"]` 结尾(前面有哪些包、什么顺序都不管)。重跑这段脚本时它打印 `already listed` 并正常退出,文件不变。

新建 `src/mindagent/__init__.py`:

```python
"""MindAgent:部署驱动、经验分析、技能提案、提案验证、打包,以及轮次与收录两个处理器
(闭环集成设计 §8.3)。依赖 controlplane、simple_actio、inference_bridge 与 simulator 的实时客户端;
controlplane 不导入本包。"""
from __future__ import annotations
```

新建 `src/mindagent/errors.py`:

```python
"""mindagent 的确定性错误。轮次处理器把它们判成"轮次 failed + T3 失败记录",不重试。"""
from __future__ import annotations


class MindAgentDataError(ValueError):
    """输入数据确定性地不对(轨迹引用了库里没有的技能、提案增量不合法等),重跑无益。"""


class ProposalParseError(MindAgentDataError):
    """外部模型的输出解析失败,且重试次数已用尽(T2)。attempts 是总尝试次数。"""

    def __init__(self, reason: str, *, attempts: int) -> None:
        super().__init__(f"{reason} (after {attempts} attempt(s))")
        self.reason = reason
        self.attempts = attempts


class AgentRoundFailedError(Exception):
    """这一轮确定性地做不成(配置、树里的数据不对),重跑无益。point 是失败点(子系统.模块)。"""

    def __init__(self, reason: str, *, point: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.point = point
```

新建 `src/mindagent/config.py`:

```python
"""AgentConfig:注入轮次处理器工厂的冻结配置。不落进 git 树(树内持久化的 agent 配置不在本期)。"""
from __future__ import annotations

from dataclasses import dataclass

from simulator.datamodel import RoleBinding


def agent_slot_of(bindings: list[RoleBinding] | tuple[RoleBinding, ...]) -> str:
    """SimpleActio 实例占的角色位:唯一一个 model_backend 为 self_play 的绑定(被部署的就是它)。"""
    slots = [b.slot_id for b in bindings if b.model_backend == "self_play"]
    if len(slots) != 1:
        raise ValueError(f"bindings must contain exactly one self_play binding (the agent), got {slots}")
    return slots[0]


def _check_int(name: str, value: object, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an int >= {minimum}, got {value!r}")


def _check_ratio(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be a number in [0, 1], got {value!r}")


@dataclass(frozen=True)
class AgentConfig:
    scenario_ids: tuple[str, ...]
    bindings: tuple[RoleBinding, ...]
    max_turns: int = 6
    episodes_per_scenario: int = 1
    max_retries_per_episode: int = 1
    min_support: int = 3
    min_ratio: float = 0.5
    min_fired_on_motivating: float = 0.8
    max_fired_elsewhere: float = 0.2
    max_parse_retries: int = 2

    def __post_init__(self) -> None:
        if not self.scenario_ids or any(not isinstance(s, str) or not s for s in self.scenario_ids):
            raise ValueError(f"scenario_ids must be a non-empty tuple of ids, got {self.scenario_ids!r}")
        if len(set(self.scenario_ids)) != len(self.scenario_ids):
            raise ValueError(f"scenario_ids must not repeat, got {self.scenario_ids!r}")
        agent_slot_of(self.bindings)
        _check_int("max_turns", self.max_turns, 1)
        _check_int("episodes_per_scenario", self.episodes_per_scenario, 1)
        _check_int("max_retries_per_episode", self.max_retries_per_episode, 0)
        _check_int("min_support", self.min_support, 1)
        _check_int("max_parse_retries", self.max_parse_retries, 0)
        for name in ("min_ratio", "min_fired_on_motivating", "max_fired_elsewhere"):
            _check_ratio(name, getattr(self, name))

    @property
    def agent_slot(self) -> str:
        return agent_slot_of(self.bindings)
```

新建 `src/mindagent/library.py`:

```python
"""正式技能库与记忆库的读取,以及库条目 id 的确定性推导(闭环集成设计 R11)。"""
from __future__ import annotations

from dataclasses import dataclass

from simple_actio.memory import MemoryEntry
from simple_actio.skills import Skill
from simple_actio.trace import normalize_library_version
from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT, library_state_file
from mindagent.errors import MindAgentDataError

LIBRARY_STATE_ID = "library-state"
_PROPOSAL_PREFIX = "proposal-"


@dataclass(frozen=True)
class LibraryState:
    skills: tuple[Skill, ...]
    memories: tuple[MemoryEntry, ...]
    version: dict  # {"skill": str, "memory": str},与激活轨迹的 library_version 同型;空库是 "0"


def _decimal(value: object, what: str) -> str:
    """库版本必须是十进制数字串(手写的非负整数也接受,规范成字符串)。收录要按整数递增它,
    计划 6 允许的 "lib-3" 之类标识在正式库里不合法,抛 MindAgentDataError(确定性错误)。"""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    if isinstance(value, str) and value.isdecimal() and value.isascii():
        return value
    raise MindAgentDataError(f"library_state.{what} must be a decimal string, got {value!r}")


def read_library_versions(store: GitStore) -> dict:
    """library_state 的载荷;文件不存在时是空库 {skill_version: "0", memory_version: "0", admitted: []}。
    两个版本都校验为十进制数字串并规范成字符串;不合法抛 MindAgentDataError。"""
    rel = library_state_file()
    if not store.exists(rel):
        return {"skill_version": "0", "memory_version": "0", "admitted": []}
    state = dict(store.read_product(rel).payload)
    for key in ("skill_version", "memory_version"):
        state[key] = _decimal(state.get(key), key)
    if not isinstance(state.get("admitted", []), list):
        raise MindAgentDataError(f"library_state.admitted must be a list, got {state.get('admitted')!r}")
    state["admitted"] = list(state.get("admitted", []))
    return state


def load_library(store: GitStore) -> LibraryState:
    """读当前正式库:skill_library/、memory_library/ 下状态为 effective 的条目,按 id 排序;
    版本取自 library_state。条目结构不合法抛 simple_actio 的 SimpleActioDataError(确定性错误)。"""
    skills = []
    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/skill_library"):
        env = store.read_product(rel)
        if env.status == "effective":
            skills.append(Skill.from_dict(env.payload))
    memories = []
    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/memory_library"):
        env = store.read_product(rel)
        if env.status == "effective":
            memories.append(MemoryEntry.from_dict(env.payload))
    state = read_library_versions(store)
    return LibraryState(
        skills=tuple(sorted(skills, key=lambda s: s.id)),
        memories=tuple(sorted(memories, key=lambda m: m.id)),
        # 设计 §8.3:版本一律是字符串,经 simple_actio 的 normalize_library_version 规范化(整数 3 → "3")
        version=normalize_library_version({"skill": state["skill_version"], "memory": state["memory_version"]}),
    )


def _stem(proposal_id: str) -> str:
    if not proposal_id.startswith(_PROPOSAL_PREFIX) or len(proposal_id) == len(_PROPOSAL_PREFIX):
        raise ValueError(f"proposal id must start with {_PROPOSAL_PREFIX!r}, got {proposal_id!r}")
    return proposal_id[len(_PROPOSAL_PREFIX):]


def skill_id_for(proposal_id: str, k: int) -> str:
    """提案里第 k 个(从 1 起)增量收录后的技能 id:proposal-X → skill-X-k。"""
    return f"skill-{_stem(proposal_id)}-{k}"


def memory_id_for(proposal_id: str, k: int) -> str:
    """提案里第 k 个(从 1 起)增量收录后的记忆 id:proposal-X → mem-X-k。"""
    return f"mem-{_stem(proposal_id)}-{k}"
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ma_config_library.py`
Expected: `27 passed`,无警告。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+27 passed`(B 为开工时的基线),无警告。再跑 `git status --short`:除开工前就在的 ` M cli/package-lock.json`、` M cli/package.json` 外,只应有 `pyproject.toml`、`src/mindagent/`(四个文件)与 `tests/test_ma_config_library.py`。

- [ ] **Step 6: 提交**

```bash
git add pyproject.toml
git add src/mindagent/__init__.py
git add src/mindagent/errors.py
git add src/mindagent/config.py
git add src/mindagent/library.py
git add tests/test_ma_config_library.py
git commit -m "feat: mindagent package skeleton (AgentConfig, formal library reader, deterministic library entry ids)"
```

---

### Task 81: 部署驱动器 `run_deployment_episodes`

**Files:**
- Create: `src/mindagent/deployment.py`(181 行)
- Test: `tests/test_ma_deployment.py`

**Interfaces:**
- Consumes: `EnvironmentServiceClient`(`create_session`、`turns`、`closed`、`submit_turn(..., *, activation_trace=...)`、`drain_until_end`、`abort`)、`SessionRejectedError`;`simulator.store_adapter.write_episode / read_episode / read_scenario`;`controlplane.paths.episode_file`;`mindagent.config.agent_slot_of`;SimpleActio 实例的 `respond` 与 `library_version`。
- Produces(任务 87 依赖):
  - `run_deployment_episodes(store, instance, client_factory, *, run_id, scenario_ids, personas, bindings, model_id, max_turns, episodes_per_scenario=1, max_retries_per_episode=1) -> list[Episode]`。
  - `DeploymentFailedError`(属性 `episode_id、reason、detail、attempts、transient、failed_attempts`)。
  - `deployment_episode_id(model_id, scenario_id, k) -> "deploy-<model>-<scenario>-<k>"`;`PURPOSE = "deployment_simulation"`;`ACCEPTED_REASONS`、`TRANSIENT_REASONS`。

**设计要点:**
- `purpose` 固定为 `deployment_simulation`;SimpleActio 实例占 `self_play` 位(Ruling 4)。客户端后说时,`create_session` 返回前对手开场已在 `client.turns` 里,循环直接从 `respond` 开始。
- 每轮 `instance.respond(persona, scenario, client.turns, slot)` 取三元组,`submit_turn(utterance, thought, activation_trace=trace)` 提交。
- 落盘归驱动器(Ruling 1):会话正常结束后,把客户端重建的 Episode 换上 `deploy-…` 的 id 写进 `runs/<run_id>/episodes/`;测试证明落盘 episode 里 agent 轮次的轨迹全部通过 `validate_trace`、对手轮次的轨迹与内心独白都是 `None`,且服务端自己记录的 `ep-<session>` 版本逐轮相同。
- 重入(Ruling 2):已落盘、且 agent 轮次轨迹的 `library_version` 与当前实例一致的 episode 直接复用,不新建客户端;不一致就按同 id 重跑覆盖。
- 失败(Ruling 5):T1 整条重跑,有上限;其余不重跑;已成功的 episode 留在树里。实例的异常:`finally` 里先 `abort()`,原样上抛。

- [ ] **Step 0: 前置检查(计划 1、计划 6 已合并)**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev python -c "import inspect; from simple_actio.instance import instantiate; from simple_actio.skills import Skill, SKILL_ID_PREFIX; from simple_actio.memory import MemoryEntry, MEMORY_ID_PREFIX; from simple_actio.trace import validate_trace, normalize_library_version; from simple_actio.triggers import referenced_signals; from simple_actio.signals import default_registry; from inference_bridge import BridgeError, ScriptedBridge; from controlplane.paths import agent_round_file, proposal_file, skill_file, memory_file, library_state_file, failure_record_file; from controlplane.ledger import record_failure; from simulator.realtime.client import EnvironmentServiceClient; assert 'activation_trace' in inspect.signature(EnvironmentServiceClient.submit_turn).parameters; assert SKILL_ID_PREFIX == 'skill-' and MEMORY_ID_PREFIX == 'mem-'; print('prereqs ok')"
```

Expected: 最后一行 `prereqs ok`。任何 `ImportError`、`AttributeError` 或 `AssertionError` 都说明计划 1 或计划 6 没有按其文本合并:**停下**,把报错原样报告,不要往下做。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_ma_deployment.py`:

```python
import time
from types import SimpleNamespace

import grpc
import pytest

from simple_actio.instance import instantiate
from simple_actio.skills import Skill
from simple_actio.trace import validate_trace
from controlplane.gitstore import GitStore
from controlplane.paths import episode_file, episodes_dir
from mindagent.deployment import (
    PURPOSE, DeploymentFailedError, deployment_episode_id, run_deployment_episodes,
)
from simulator.backends import ScriptedBackend
from simulator.datamodel import Episode, Persona, RoleBinding, RoleSlot, Scenario, TurnRecord
from simulator.realtime.client import EnvironmentServiceClient, SessionRejectedError
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server
from simulator.store_adapter import read_episode, read_scenario, write_persona, write_scenario


class _DeadlineStub:
    """给每条 Interact 流加 gRPC deadline:服务端失联时客户端的 next() 以 DEADLINE_EXCEEDED
    结束,而不是挂住整个测试套件(套件没有单测超时)。"""

    def __init__(self, stub, timeout_s=15.0):
        self._stub = stub
        self._timeout_s = timeout_s

    def Interact(self, request_iterator):
        return self._stub.Interact(request_iterator, timeout=self._timeout_s)


RUN = "run-003"
MODEL = "zengen-v1"
AGENT = RoleBinding("a", "pa", "self_play")
OPP = RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")
BINDINGS = (OPP, AGENT)  # 对手先说,SimpleActio 实例后说


class Echo:
    def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
        return ("|".join(guidance) or "plain", f"saw {len(history)}")


CALM = Skill("skill-calm", 1, "micro", {"signal": "emotion_high"}, ("先回应情绪",), priority=1)


def _instance(skills=(), version=None):
    return instantiate(Echo(), skills=list(skills), memories=[],
                       library_version=version or {"skill": 0, "memory": 0}, profile=None)


def _personas():
    return {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    for sid in ("s1", "s2"):
        write_scenario(s, Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], PURPOSE))
    for pid, p in _personas().items():
        write_persona(s, p)
    return s


@pytest.fixture
def live(store):
    channels, servers = [], []

    def start(lines=("我真的很生气!!", "好吧", "还有呢")):
        closed = []

        def factory(scenario, assignment, personas, client_slot):
            return {s: ScriptedBackend({s: list(lines)}) for s in assignment.slot_ids() - {client_slot}}

        server = create_server(factory, port=0, turn_timeout_s=2.0, on_session_closed=closed.append,
                               scenario_resolver=lambda sid: read_scenario(store, sid))
        server.start()
        servers.append(server)

        def client_factory():
            channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
            channels.append(channel)
            return EnvironmentServiceClient(_DeadlineStub(pb2_grpc.EnvironmentServiceStub(channel)))

        return SimpleNamespace(server=server, closed=closed, client_factory=client_factory)

    yield start
    for c in channels:
        c.close()
    for s in servers:
        s.stop(0).wait()


def _run(store, instance, client_factory, **kw):
    args = dict(run_id=RUN, scenario_ids=["s1"], personas=_personas(), bindings=BINDINGS,
                model_id=MODEL, max_turns=4)
    args.update(kw)
    return run_deployment_episodes(store, instance, client_factory, **args)


# ---------------- 真实 loopback 服务端 ----------------

def test_episodes_are_persisted_under_the_bound_run_with_deploy_ids(store, live):
    env = live()
    episodes = _run(store, _instance([CALM]), env.client_factory, scenario_ids=["s1", "s2"],
                    episodes_per_scenario=2)
    ids = [e.id for e in episodes]
    assert ids == ["deploy-zengen-v1-s1-1", "deploy-zengen-v1-s1-2",
                   "deploy-zengen-v1-s2-1", "deploy-zengen-v1-s2-2"]
    assert store.list_products(episodes_dir(RUN)) == [episode_file(RUN, i) for i in sorted(ids)]
    for e in episodes:
        persisted = read_episode(store, RUN, e.id)
        assert persisted.to_dict() == e.to_dict()
        assert [t.speaker_slot for t in persisted.turns] == ["b", "a", "b", "a"]
        assert persisted.terminated_reason == "max_turns"


def test_agent_turns_carry_traces_and_opponent_turns_do_not(store, live):
    env = live()
    (episode,) = _run(store, _instance([CALM], {"skill": 2, "memory": 0}), env.client_factory)
    persisted = read_episode(store, RUN, episode.id)
    agent = [t for t in persisted.turns if t.speaker_slot == "a"]
    opp = [t for t in persisted.turns if t.speaker_slot == "b"]
    assert all(t.activation_trace is None and t.inner_thought is None for t in opp)
    for t in agent:
        validate_trace(t.activation_trace)
        assert t.activation_trace["library_version"] == {"skill": "2", "memory": "0"}
    assert agent[0].activation_trace["selected"] == "skill-calm"  # 对方开场"我真的很生气!!"
    assert agent[0].utterance == "[skill:skill-calm] 先回应情绪"
    assert agent[1].activation_trace["selected"] is None          # "好吧"


def test_the_agent_sees_the_opponents_opening_turn(store, live):
    env = live()
    seen = []

    class Recorder(Echo):
        def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
            seen.append([(t.speaker_slot, t.utterance) for t in history])
            return super().respond_guided(persona, scenario, history, speaker_slot, guidance)

    inst = instantiate(Recorder(), skills=[], memories=[], library_version={"skill": 0, "memory": 0}, profile=None)
    _run(store, inst, env.client_factory)
    assert seen[0] == [("b", "我真的很生气!!")]
    assert seen[1][-1] == ("b", "好吧")


def test_the_server_side_ep_copy_is_left_to_the_caller_and_the_deploy_copy_matches_it(store, live):
    env = live()
    (episode,) = _run(store, _instance([CALM]), env.client_factory)
    deadline = time.monotonic() + 3
    while not env.closed and time.monotonic() < deadline:
        time.sleep(0.02)
    (server_side,) = env.closed
    assert server_side.id.startswith("ep-sess-")
    assert [t.to_dict() for t in server_side.turns] == [t.to_dict() for t in episode.turns]
    assert not store.exists(episode_file(RUN, server_side.id))


def test_reentry_reuses_persisted_episodes_and_runs_only_the_missing_ones(store, live):
    env = live()
    _run(store, _instance(), env.client_factory)
    created = []

    def counting():
        created.append(1)
        return env.client_factory()

    episodes = _run(store, _instance(), counting, episodes_per_scenario=2)
    assert [e.id for e in episodes] == ["deploy-zengen-v1-s1-1", "deploy-zengen-v1-s1-2"]
    assert len(created) == 1


def test_a_persisted_episode_from_another_library_version_is_regenerated(store, live):
    env = live()
    _run(store, _instance(), env.client_factory)
    created = []

    def counting():
        created.append(1)
        return env.client_factory()

    (episode,) = _run(store, _instance([CALM], {"skill": 1, "memory": 0}), counting)
    assert len(created) == 1
    assert read_episode(store, RUN, episode.id).turns[1].activation_trace["library_version"]["skill"] == "1"


def test_a_stopped_server_is_transient_and_retried_then_reported(store, live):
    env = live()
    env.server.stop(0).wait()
    with pytest.raises(DeploymentFailedError) as exc:
        _run(store, _instance(), env.client_factory, max_retries_per_episode=1)
    assert exc.value.reason == "transport_unavailable"
    assert exc.value.transient is True and exc.value.attempts == 2
    assert store.list_products(episodes_dir(RUN)) == []


def test_an_instance_exception_aborts_the_session_and_nothing_is_persisted(store, live):
    env = live()

    class Boom(Exception):
        pass

    class Exploding(Echo):
        calls = 0

        def respond_guided(self, *a):
            Exploding.calls += 1
            if Exploding.calls == 2:
                raise Boom("model bug")
            return super().respond_guided(*a)

    inst = instantiate(Exploding(), skills=[], memories=[], library_version={"skill": 0, "memory": 0}, profile=None)
    with pytest.raises(Boom):
        _run(store, inst, env.client_factory)
    env.server.stop(2).wait()
    assert env.closed == []
    assert store.list_products(episodes_dir(RUN)) == []


# ---------------- 替身客户端 ----------------

class _Unavailable(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

    def details(self):
        return "reset"


class _FakeClient:
    """客户端后说:create 时收到对方开场,每次提交后对方回一句,max_turns=4。"""

    def __init__(self, *, create_error=None, end_reason="max_turns"):
        self._create_error = create_error
        self._end_reason = end_reason
        self._turns = []
        self.closed = False
        self.aborted = False
        self.created = None
        self.traces = []

    @property
    def turns(self):
        return list(self._turns)

    def create_session(self, *, scenario_id, role_assignment, personas, purpose, client_driven_slot):
        self.created = dict(scenario_id=scenario_id, role_assignment=role_assignment,
                            purpose=purpose, slot=client_driven_slot)
        if self._create_error is not None:
            self.closed = True
            raise self._create_error
        self._turns.append(TurnRecord(0, "b", "我很生气!!"))
        return "sess"

    def submit_turn(self, utterance, inner_thought=None, *, activation_trace=None):
        self.traces.append(activation_trace)
        self._turns.append(TurnRecord(len(self._turns), "a", utterance, inner_thought, activation_trace))
        if len(self._turns) < 4:
            self._turns.append(TurnRecord(len(self._turns), "b", "嗯"))
        if len(self._turns) >= 4:
            self.closed = True
        return []

    def drain_until_end(self):
        return Episode("ep-sess", self.created["scenario_id"], self.created["role_assignment"],
                       self.turns, self._end_reason)

    def abort(self):
        self.aborted = True
        self.closed = True


class _Factory:
    def __init__(self, *plans):
        self._plans = list(plans)
        self.clients = []

    def __call__(self):
        c = _FakeClient(**(self._plans.pop(0) if self._plans else {}))
        self.clients.append(c)
        return c


def test_create_session_gets_the_deployment_purpose_the_agent_slot_and_a_fresh_assignment(store):
    f = _Factory()
    _run(store, _instance(), f, episodes_per_scenario=2)
    first, second = f.clients[0].created, f.clients[1].created
    assert (first["purpose"], first["slot"]) == ("deployment_simulation", "a")
    assert [b.to_dict() for b in first["role_assignment"].bindings] == [OPP.to_dict(), AGENT.to_dict()]
    assert first["role_assignment"].max_turns == 4
    assert first["role_assignment"] is not second["role_assignment"]
    assert all(t is not None for t in f.clients[0].traces)


@pytest.mark.parametrize("plan,reason", [
    ({"end_reason": "timeout"}, "timeout"),
    ({"end_reason": "role_backend_timeout"}, "role_backend_timeout"),
    ({"create_error": _Unavailable()}, "transport_unavailable"),
])
def test_a_transient_failure_is_rerun_whole(store, plan, reason):
    f = _Factory(plan)
    (episode,) = _run(store, _instance(), f)
    assert episode.id == "deploy-zengen-v1-s1-1" and episode.terminated_reason == "max_turns"
    assert len(f.clients) == 2


def test_transient_retries_are_bounded(store):
    f = _Factory({"end_reason": "timeout"}, {"end_reason": "timeout"}, {"end_reason": "timeout"})
    with pytest.raises(DeploymentFailedError) as exc:
        _run(store, _instance(), f, max_retries_per_episode=2)
    assert (exc.value.attempts, exc.value.transient, exc.value.reason) == (3, True, "timeout")
    assert [a["attempt"] for a in exc.value.failed_attempts] == [1, 2, 3]
    assert len(f.clients) == 3


@pytest.mark.parametrize("plan,reason", [
    ({"create_error": SessionRejectedError("invalid_session", "bad")}, "invalid_session"),
    ({"end_reason": "role_backend_failure"}, "role_backend_failure"),
    ({"end_reason": "client_disconnected"}, "client_disconnected"),
])
def test_non_transient_failures_are_not_rerun(store, plan, reason):
    f = _Factory(plan)
    with pytest.raises(DeploymentFailedError) as exc:
        _run(store, _instance(), f)
    assert (exc.value.reason, exc.value.attempts, exc.value.transient) == (reason, 1, False)
    assert len(f.clients) == 1
    assert store.list_products(episodes_dir(RUN)) == []


def test_successful_episodes_before_a_failure_stay_in_the_tree(store):
    f = _Factory({}, {"create_error": SessionRejectedError("invalid_session", "x")})
    with pytest.raises(DeploymentFailedError) as exc:
        _run(store, _instance(), f, episodes_per_scenario=2)
    assert exc.value.episode_id == "deploy-zengen-v1-s1-2"
    assert store.list_products(episodes_dir(RUN)) == [episode_file(RUN, "deploy-zengen-v1-s1-1")]


def test_an_instance_exception_aborts_the_fake_session_unwrapped(store):
    class Broken(Echo):
        def respond_guided(self, *a):
            raise RuntimeError("model bug")

    inst = instantiate(Broken(), skills=[], memories=[], library_version={"skill": 0, "memory": 0}, profile=None)
    f = _Factory()
    with pytest.raises(RuntimeError, match="model bug") as exc:
        _run(store, inst, f)
    assert not isinstance(exc.value, DeploymentFailedError)
    assert f.clients[0].aborted


def test_episode_ids_are_deterministic():
    assert deployment_episode_id("zengen-v3", "scen-a", 2) == "deploy-zengen-v3-scen-a-2"
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ma_deployment.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindagent.deployment'`,末行 `1 error in ...`。

- [ ] **Step 3: 写实现**

新建 `src/mindagent/deployment.py`:

```python
"""部署驱动器:SimpleActio 实例占客户端角色位,经实时环境服务跑部署 episode 并落盘(闭环集成设计 §8.3)。

落盘归驱动器,不归服务端钩子:客户端在会话结束时重建的 Episode(自己轮次是服务端回显,
带激活轨迹;对手轮次的内心独白与轨迹按协议已被抹掉)以 deploy-<model>-<scenario>-<k> 为 id
写进绑定模型所在 run 的 episodes 目录。失败会话整条重跑(补充 E.4)。
"""
from __future__ import annotations

from collections.abc import Callable

import grpc

from controlplane.gitstore import GitStore
from controlplane.paths import episode_file
from mindagent.config import agent_slot_of
from simulator.datamodel import Episode, Persona, RoleAssignment, RoleBinding
from simulator.realtime.client import EnvironmentServiceClient, SessionRejectedError
from simulator.store_adapter import read_episode, read_scenario, write_episode

PURPOSE = "deployment_simulation"
# 与 mindtrainer.trajectory_source 同一套判定(本包不导入 mindtrainer):
ACCEPTED_REASONS = frozenset({"max_turns", "goal_reached"})
TRANSIENT_REASONS = frozenset({"timeout", "role_backend_timeout"})


class DeploymentFailedError(Exception):
    """某条部署 episode 失败且不再重跑(不可重跑的原因,或瞬态重跑用尽)。
    transient 为真表示最后一次失败是 T1(重跑用尽);failed_attempts 是本次调用的全部失败尝试。"""

    def __init__(
        self, *, episode_id: str, reason: str, detail: str, attempts: int,
        transient: bool, failed_attempts: list[dict],
    ) -> None:
        tail = f": {detail}" if detail else ""
        super().__init__(f"deployment episode {episode_id} failed after {attempts} attempt(s): {reason}{tail}")
        self.episode_id = episode_id
        self.reason = reason
        self.detail = detail
        self.attempts = attempts
        self.transient = transient
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


def deployment_episode_id(model_id: str, scenario_id: str, k: int) -> str:
    return f"deploy-{model_id}-{scenario_id}-{k}"


def _reusable(episode: Episode, agent_slot: str, library_version: dict) -> bool:
    """已落盘的部署 episode 只有在它的每个 agent 轮次都带着与当前实例相同库版本的轨迹时才复用;
    库在轮次中途被收录改过,就按同一个 id 重新生成并覆盖。"""
    agent_turns = [t for t in episode.turns if t.speaker_slot == agent_slot]
    return bool(agent_turns) and all(
        isinstance(t.activation_trace, dict)
        and t.activation_trace.get("library_version") == library_version
        for t in agent_turns
    )


def _attempt(instance, client_factory, scenario, assignment, personas, agent_slot) -> Episode:
    persona = personas[next(b.persona_id for b in assignment.bindings if b.slot_id == agent_slot)]
    client = client_factory()
    try:
        try:
            client.create_session(
                scenario_id=scenario.id,
                role_assignment=assignment,
                personas=personas,
                purpose=PURPOSE,
                client_driven_slot=agent_slot,
            )
        except SessionRejectedError as exc:
            raise _AttemptFailed(exc.reason, exc.detail, False) from exc
        except (grpc.RpcError, RuntimeError) as exc:
            raise _classify_client_error(exc) from exc
        while not client.closed:
            # 策略(SimpleActio 实例)的异常不在任何 except 里:穿过下面的 finally(先 abort)原样上抛
            utterance, thought, trace = instance.respond(persona, scenario, client.turns, agent_slot)
            try:
                client.submit_turn(utterance, thought, activation_trace=trace)
            except (grpc.RpcError, RuntimeError) as exc:
                raise _classify_client_error(exc) from exc
        try:
            episode = client.drain_until_end()
        except (grpc.RpcError, RuntimeError) as exc:
            raise _classify_client_error(exc) from exc
    finally:
        if not client.closed:
            # 不发 EndSession:服务端按断连判 FAILED,不落盘半截 episode
            client.abort()
    if episode.terminated_reason in ACCEPTED_REASONS:
        return episode
    raise _AttemptFailed(
        episode.terminated_reason, "", episode.terminated_reason in TRANSIENT_REASONS
    )


def run_deployment_episodes(
    store: GitStore,
    instance,
    client_factory: Callable[[], EnvironmentServiceClient],
    *,
    run_id: str,
    scenario_ids: list[str] | tuple[str, ...],
    personas: dict[str, Persona],
    bindings: list[RoleBinding] | tuple[RoleBinding, ...],
    model_id: str,
    max_turns: int,
    episodes_per_scenario: int = 1,
    max_retries_per_episode: int = 1,
) -> list[Episode]:
    """按 scenario_ids 的顺序,每个情境跑 episodes_per_scenario 条部署 episode,返回全部 episode
    (含重入时复用的)。每条 episode 落在 runs/<run_id>/episodes/deploy-<model>-<scenario>-<k>.yaml。

    - 已落盘且可复用(见 _reusable)的 episode 不重跑。
    - T1(timeout、role_backend_timeout、传输层 UNAVAILABLE)整条重跑,上限 max_retries_per_episode;
      其余失败不重跑。两者都以 DeploymentFailedError 结束,已成功的 episode 留在树里供重入复用。
    - 实例自己的异常:会话先 abort(服务端不落盘),原异常原样上抛,不重试、不包装。
    """
    agent_slot = agent_slot_of(bindings)
    out: list[Episode] = []
    failed_attempts: list[dict] = []
    for scenario_id in scenario_ids:
        scenario = None
        for k in range(1, episodes_per_scenario + 1):
            episode_id = deployment_episode_id(model_id, scenario_id, k)
            if store.exists(episode_file(run_id, episode_id)):
                existing = read_episode(store, run_id, episode_id)
                if _reusable(existing, agent_slot, instance.library_version):
                    out.append(existing)
                    continue
            if scenario is None:
                scenario = read_scenario(store, scenario_id)
            budget = 1 + max_retries_per_episode
            for attempt in range(1, budget + 1):
                assignment = RoleAssignment(
                    scenario_id=scenario_id,
                    bindings=[RoleBinding.from_dict(b.to_dict()) for b in bindings],
                    max_turns=max_turns,
                )
                try:
                    raw = _attempt(instance, client_factory, scenario, assignment, personas, agent_slot)
                except _AttemptFailed as failure:
                    failed_attempts.append({
                        "episode_id": episode_id, "attempt": attempt,
                        "reason": failure.reason, "detail": failure.detail,
                    })
                    if not failure.transient or attempt == budget:
                        raise DeploymentFailedError(
                            episode_id=episode_id, reason=failure.reason, detail=failure.detail,
                            attempts=attempt, transient=failure.transient,
                            failed_attempts=failed_attempts,
                        ) from failure
                    continue
                episode = Episode(
                    id=episode_id, scenario_id=raw.scenario_id, role_assignment=raw.role_assignment,
                    turns=raw.turns, terminated_reason=raw.terminated_reason,
                )
                write_episode(store, run_id, episode)
                out.append(episode)
                break
    return out
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ma_deployment.py`
Expected: `19 passed`,无警告。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+46 passed`(B 为开工时的基线),无警告。再跑 `git status --short`:除开工前就在的 ` M cli/package-lock.json`、` M cli/package.json` 外,只应有 `src/mindagent/deployment.py` 与 `tests/test_ma_deployment.py`。

- [ ] **Step 6: 提交**

```bash
git add src/mindagent/deployment.py
git add tests/test_ma_deployment.py
git commit -m "feat: mindagent deployment driver (SimpleActio instance as the client slot, traces submitted, deploy-<model> episodes persisted under the bound run, whole-session reruns)"
```

---

### Task 82: 经验分析器 `analyze`

**Files:**
- Create: `src/mindagent/analysis.py`(139 行)
- Test: `tests/test_ma_analysis.py`

**Interfaces:**
- Consumes: `simple_actio.signals.default_registry`、`SignalRegistry.get`;`simple_actio.triggers.referenced_signals`;`simple_actio.skills.Skill`;`simulator.datamodel.Episode`、`TurnRecord`。
- Produces(任务 83、84、87 依赖):
  - `analyze(episodes, *, min_support, min_ratio, skills=(), registry=None, signals=None) -> list[BehaviorFinding]`(纯函数,按信号名排序)。
  - `BehaviorFinding(id, signal, pattern, support, ratio, episode_refs)`(冻结,`to_dict()`);`finding_id(signal, episode_refs)`。
  - `visible_history(turns, index, slot)`、`agent_slot_of_episode(episode)`、`signal_turns(episode, signal, registry)`、`OPPONENT_SIGNALS = ("emotion_high", "no_new_information")`。

**设计要点:**
- 判定见 Ruling 6:信号按 agent 发言前能看到的历史重新求值;受影响 = 信号为真、`selected` 非空、选中技能的触发条件不引用该信号。一个 episode 只计一次。
- 分母是"有 agent 轮次的 episode 数";两个阈值都含等号;阈值不合法抛 `ValueError`。
- 空输入、裸模型(轨迹 `None`)、空库(`selected` 恒 `None`)都返回空列表;`not` 包住的信号不算"针对";轨迹选中库外技能抛 `MindAgentDataError`;非 `None` 的轨迹先过 `validate_trace`,不合模式也抛 `MindAgentDataError`。
- `id` 由信号与排序后的 episode 集合推出(Ruling 7),输入顺序不影响结果;不修改入参。

- [ ] **Step 0: 前置检查(计划 1、计划 6 已合并)**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev python -c "import inspect; from simple_actio.instance import instantiate; from simple_actio.skills import Skill, SKILL_ID_PREFIX; from simple_actio.memory import MemoryEntry, MEMORY_ID_PREFIX; from simple_actio.trace import validate_trace, normalize_library_version; from simple_actio.triggers import referenced_signals; from simple_actio.signals import default_registry; from inference_bridge import BridgeError, ScriptedBridge; from controlplane.paths import agent_round_file, proposal_file, skill_file, memory_file, library_state_file, failure_record_file; from controlplane.ledger import record_failure; from simulator.realtime.client import EnvironmentServiceClient; assert 'activation_trace' in inspect.signature(EnvironmentServiceClient.submit_turn).parameters; assert SKILL_ID_PREFIX == 'skill-' and MEMORY_ID_PREFIX == 'mem-'; print('prereqs ok')"
```

Expected: 最后一行 `prereqs ok`。任何 `ImportError`、`AttributeError` 或 `AssertionError` 都说明计划 1 或计划 6 没有按其文本合并:**停下**,把报错原样报告,不要往下做。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_ma_analysis.py`:

```python
import pytest

from simple_actio.skills import Skill
from simple_actio.trace import build_trace
from mindagent.analysis import BehaviorFinding, analyze, finding_id, signal_turns
from mindagent.errors import MindAgentDataError
from simple_actio.signals import default_registry
from simulator.datamodel import Episode, RoleAssignment, RoleBinding, TurnRecord

LV = {"skill": "1", "memory": "0"}
GOAL = Skill("skill-goal", 1, "macro", {"turn_at_least": 0}, ("推进目标",))
CALM = Skill("skill-calm", 1, "micro", {"any": [{"signal": "emotion_high"}, {"turn_at_least": 99}]}, ("先回应情绪",))
NOT_CALM = Skill("skill-notcalm", 1, "micro", {"not": {"signal": "emotion_high"}}, ("x",))


def _trace(selected):
    return build_trace(signals=[], candidates=[selected] if selected else [], selected=selected,
                       memory_hits=[], experience=None, library_version=LV)


def _episode(eid, opp_lines, selected="skill-goal", trace=True):
    """对手 b 先说,agent a 后说。每句对手话后面跟一轮 agent。"""
    turns = []
    for line in opp_lines:
        turns.append(TurnRecord(len(turns), "b", line))
        turns.append(TurnRecord(len(turns), "a", "reply", None, _trace(selected) if trace else None))
    ra = RoleAssignment("s1", [RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1"),
                               RoleBinding("a", "pa", "self_play")], len(turns))
    return Episode(eid, "s1", ra, turns, "max_turns")


ANGRY = "我真的很生气!!"


def test_empty_input_has_no_findings():
    assert analyze([], min_support=1, min_ratio=0.0) == []


def test_a_signal_handled_by_a_non_targeting_skill_is_a_finding():
    eps = [_episode(f"e{i}", [ANGRY, "好吧"]) for i in range(3)] + [_episode("e3", ["好吧", "嗯"])]
    (f,) = analyze(eps, min_support=3, min_ratio=0.5, skills=[GOAL])
    assert f.signal == "emotion_high"
    assert (f.support, f.ratio, f.episode_refs) == (3, 0.75, ("e0", "e1", "e2"))
    assert f.id == finding_id("emotion_high", ["e2", "e0", "e1"])
    assert f.id.startswith("finding-emotion_high-") and len(f.id) == len("finding-emotion_high-") + 12
    assert f.to_dict()["episode_refs"] == ["e0", "e1", "e2"]


def test_a_targeting_skill_is_not_a_gap_and_negation_does_not_count_as_targeting():
    eps = [_episode(f"e{i}", [ANGRY], selected="skill-calm") for i in range(3)]
    assert analyze(eps, min_support=1, min_ratio=0.0, skills=[CALM]) == []
    eps = [_episode(f"e{i}", [ANGRY], selected="skill-notcalm") for i in range(3)]
    assert [f.signal for f in analyze(eps, min_support=1, min_ratio=0.0, skills=[NOT_CALM])] == ["emotion_high"]


def test_support_below_the_threshold_is_not_a_finding():
    eps = [_episode("e0", [ANGRY]), _episode("e1", [ANGRY])]
    assert analyze(eps, min_support=3, min_ratio=0.0, skills=[GOAL]) == []
    assert len(analyze(eps, min_support=2, min_ratio=0.0, skills=[GOAL])) == 1


def test_ratio_at_the_threshold_counts_and_just_below_does_not():
    eps = [_episode("e0", [ANGRY]), _episode("e1", [ANGRY]), _episode("e2", ["好"]), _episode("e3", ["嗯"])]
    assert len(analyze(eps, min_support=1, min_ratio=0.5, skills=[GOAL])) == 1
    assert analyze(eps, min_support=1, min_ratio=0.51, skills=[GOAL]) == []


def test_bare_model_episodes_without_traces_never_produce_findings():
    eps = [_episode(f"e{i}", [ANGRY], trace=False) for i in range(5)]
    assert analyze(eps, min_support=1, min_ratio=0.0) == []


def test_an_empty_library_selects_nothing_so_there_is_nothing_to_find():
    eps = [_episode(f"e{i}", [ANGRY], selected=None) for i in range(5)]
    assert analyze(eps, min_support=1, min_ratio=0.0, skills=[]) == []


def test_two_signals_each_get_their_own_finding_sorted_by_name():
    eps = [_episode(f"e{i}", ["我们谈谈预算", ANGRY, "我们谈谈预算"]) for i in range(3)]
    found = analyze(eps, min_support=3, min_ratio=0.5, skills=[GOAL])
    assert [f.signal for f in found] == ["emotion_high", "no_new_information"]
    assert found[0].id != found[1].id


def test_an_episode_counts_once_even_if_the_signal_fires_on_many_turns():
    # 同一句重复三次也会触发 no_new_information,所以只看 emotion_high
    (f,) = analyze([_episode("e0", [ANGRY, ANGRY, ANGRY])], min_support=1, min_ratio=0.0, skills=[GOAL],
                   signals=("emotion_high",))
    assert f.support == 1 and f.ratio == 1.0


def test_episodes_without_agent_turns_are_not_in_the_denominator():
    only_opp = Episode("e9", "s1", RoleAssignment("s1", [RoleBinding("b", "pb", "frozen_reference", model_ref="m"),
                                                          RoleBinding("a", "pa", "self_play")], 1),
                       [TurnRecord(0, "b", ANGRY)], "max_turns")
    (f,) = analyze([_episode("e0", [ANGRY]), only_opp], min_support=1, min_ratio=1.0, skills=[GOAL])
    assert f.ratio == 1.0


def test_signals_can_be_restricted_and_unknown_names_are_ignored():
    eps = [_episode(f"e{i}", ["我们谈谈预算", ANGRY, "我们谈谈预算"]) for i in range(3)]
    found = analyze(eps, min_support=1, min_ratio=0.0, skills=[GOAL], signals=("no_new_information", "ghost"))
    assert [f.signal for f in found] == ["no_new_information"]


def test_an_off_schema_trace_is_a_data_error():
    ep = _episode("e0", [ANGRY])
    ep.turns[1].activation_trace = {"selected": "skill-goal"}
    with pytest.raises(MindAgentDataError, match="episode e0 turn 1: trace"):
        analyze([ep], min_support=1, min_ratio=0.0, skills=[GOAL])


def test_a_trace_selecting_a_skill_outside_the_library_is_a_data_error():
    with pytest.raises(MindAgentDataError, match="not in the library"):
        analyze([_episode("e0", [ANGRY])], min_support=1, min_ratio=0.0, skills=[])


def test_an_episode_without_exactly_one_self_play_binding_is_a_data_error():
    bad = _episode("e0", [ANGRY])
    bad.role_assignment.bindings[1].model_backend = "frozen_reference"
    with pytest.raises(MindAgentDataError, match="self_play"):
        analyze([bad], min_support=1, min_ratio=0.0, skills=[GOAL])


@pytest.mark.parametrize("kw", [{"min_support": 0, "min_ratio": 0.5}, {"min_support": 1, "min_ratio": 1.2},
                                {"min_support": True, "min_ratio": 0.5}])
def test_thresholds_are_validated(kw):
    with pytest.raises(ValueError):
        analyze([], **kw)


def test_signal_turns_uses_the_agents_view_of_the_history():
    ep = _episode("e0", ["好", ANGRY])
    assert signal_turns(ep, "emotion_high", default_registry()) == [3]


def test_analyze_is_pure_and_deterministic():
    eps = [_episode(f"e{i}", [ANGRY]) for i in range(3)]
    before = [e.to_dict() for e in eps]
    a = analyze(eps, min_support=1, min_ratio=0.0, skills=[GOAL])
    b = analyze(list(reversed(eps)), min_support=1, min_ratio=0.0, skills=[GOAL])
    assert a == b and isinstance(a[0], BehaviorFinding)
    assert [e.to_dict() for e in eps] == before
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ma_analysis.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindagent.analysis'`,末行 `1 error in ...`。

- [ ] **Step 3: 写实现**

新建 `src/mindagent/analysis.py`:

```python
"""经验分析器:跨 episode 聚合"信号触发了,被选中的技能却不针对它"的模式(闭环集成设计 §8.3)。

纯函数。核心原则"不基于单一个案":受影响的 episode 数不足 min_support,或占比低于 min_ratio,
都不产生发现。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from simple_actio.errors import SimpleActioDataError
from simple_actio.signals import SignalRegistry, default_registry
from simple_actio.skills import Skill
from simple_actio.trace import validate_trace
from simple_actio.triggers import referenced_signals
from mindagent.errors import MindAgentDataError
from simulator.datamodel import Episode, TurnRecord

# 读"对方最后一句"的内置信号。first_turn 看的是说话方自己有没有说过话,不在此列。
OPPONENT_SIGNALS: tuple[str, ...] = ("emotion_high", "no_new_information")


@dataclass(frozen=True)
class BehaviorFinding:
    id: str
    signal: str
    pattern: str
    support: int
    ratio: float
    episode_refs: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "signal": self.signal,
            "pattern": self.pattern,
            "support": self.support,
            "ratio": self.ratio,
            "episode_refs": list(self.episode_refs),
        }


def finding_id(signal: str, episode_refs: list[str] | tuple[str, ...]) -> str:
    """由信号名与(排序后的)episode 集合推出,同输入同 id。"""
    digest = hashlib.sha256("\n".join([signal, *sorted(episode_refs)]).encode("utf-8")).hexdigest()
    return f"finding-{signal}-{digest[:12]}"


def visible_history(turns: list[TurnRecord], index: int, slot: str) -> list[TurnRecord]:
    """第 index 轮发言前,slot 能看到的历史:与 runner 给后端的那份同一条规则(对方的内心独白与轨迹抹掉)。"""
    return [
        t if t.speaker_slot == slot else replace(t, inner_thought=None, activation_trace=None)
        for t in turns[:index]
    ]


def agent_slot_of_episode(episode: Episode) -> str:
    slots = [b.slot_id for b in episode.role_assignment.bindings if b.model_backend == "self_play"]
    if len(slots) != 1:
        raise MindAgentDataError(
            f"episode {episode.id}: expected exactly one self_play binding, got {slots}"
        )
    return slots[0]


def signal_turns(episode: Episode, signal: str, registry: SignalRegistry) -> list[int]:
    """该 episode 里 signal 在 agent 发言前为真的那些 agent 轮次的 turn_index。"""
    slot = agent_slot_of_episode(episode)
    fn = registry.get(signal)
    return [
        t.turn_index
        for i, t in enumerate(episode.turns)
        if t.speaker_slot == slot and fn(visible_history(episode.turns, i, slot), slot)
    ]


def analyze(
    episodes: list[Episode] | tuple[Episode, ...],
    *,
    min_support: int,
    min_ratio: float,
    skills: list[Skill] | tuple[Skill, ...] = (),
    registry: SignalRegistry | None = None,
    signals: tuple[str, ...] | None = None,
) -> list[BehaviorFinding]:
    """对每个信号,统计受影响的 episode:至少有一个 agent 轮次,信号在它发言前为真,而轨迹里
    被选中的技能不为空且其触发条件不引用该信号。ratio = 受影响数 / 有 agent 轮次的 episode 数。

    - 轨迹为 None(裸模型)或 selected 为 None 的轮次不算受影响:没有路由,谈不上"路由错了"。
    - skills 是这批 episode 所用的技能库;轨迹选中了库里没有的技能抛 MindAgentDataError。
    - 结果按信号名排序。
    """
    if isinstance(min_support, bool) or not isinstance(min_support, int) or min_support < 1:
        raise ValueError(f"min_support must be an int >= 1, got {min_support!r}")
    if isinstance(min_ratio, bool) or not isinstance(min_ratio, (int, float)) or not 0.0 <= min_ratio <= 1.0:
        raise ValueError(f"min_ratio must be a number in [0, 1], got {min_ratio!r}")
    reg = registry if registry is not None else default_registry()
    names = [s for s in (signals if signals is not None else OPPONENT_SIGNALS) if s in reg]
    targets = {s.id: referenced_signals(s.trigger) for s in skills}

    counted = [e for e in episodes if any(t.speaker_slot == agent_slot_of_episode(e) for t in e.turns)]
    findings: list[BehaviorFinding] = []
    for signal in sorted(names):
        affected: list[str] = []
        for episode in counted:
            turns_by_index = {t.turn_index: t for t in episode.turns}
            for idx in signal_turns(episode, signal, reg):
                trace = turns_by_index[idx].activation_trace
                if trace is not None:
                    try:
                        validate_trace(trace)  # 计划 6:消费方读落盘 episode 时校验轨迹
                    except SimpleActioDataError as exc:
                        raise MindAgentDataError(f"episode {episode.id} turn {idx}: {exc}") from None
                selected = trace["selected"] if trace is not None else None
                if selected is None:
                    continue
                if selected not in targets:
                    raise MindAgentDataError(
                        f"episode {episode.id} turn {idx}: selected skill {selected!r} is not in the library"
                    )
                if signal not in targets[selected]:
                    affected.append(episode.id)
                    break
        if not counted:
            continue
        support = len(affected)
        ratio = support / len(counted)
        if support < min_support or ratio < min_ratio:
            continue
        refs = tuple(sorted(affected))
        findings.append(BehaviorFinding(
            id=finding_id(signal, refs),
            signal=signal,
            pattern=f"signal {signal} fired but the selected skill did not target it",
            support=support,
            ratio=ratio,
            episode_refs=refs,
        ))
    return findings
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ma_analysis.py`
Expected: `19 passed`,无警告。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+65 passed`(B 为开工时的基线),无警告。再跑 `git status --short`:除开工前就在的 ` M cli/package-lock.json`、` M cli/package.json` 外,只应有 `src/mindagent/analysis.py` 与 `tests/test_ma_analysis.py`。

- [ ] **Step 6: 提交**

```bash
git add src/mindagent/analysis.py
git add tests/test_ma_analysis.py
git commit -m "feat: mindagent experience analyzer (cross-episode signal-vs-selected-skill gaps, support and ratio thresholds, deterministic finding ids)"
```

---

### Task 83: 技能提案生成器 `propose`

**Files:**
- Create: `src/mindagent/proposals.py`(146 行)
- Test: `tests/test_ma_proposals.py`

**Interfaces:**
- Consumes: `mindagent.analysis.BehaviorFinding`;`simple_actio.skills.Skill.from_dict`;注入的桥接(`complete(prompt, *, run_id, task_id) -> BridgeResult`)。
- Produces(任务 84、85、87 依赖):
  - `propose(findings, *, bridge=None, run_id="", task_id="mindagent-propose", max_parse_retries=2) -> list[ProposalDraft]`(偏差见 Ruling 19)。
  - `attempt_task_id(task_id, attempt) -> "<task_id>-attempt<n>"`。
  - `ProposalDraft(finding_id, increments: tuple[dict, ...], generator)`(`skill_definitions()`);`parse_strategy(text) -> (instructions, description)`。
  - 常量 `RULE_GENERATOR = "rule_template.v1"`、`BRIDGE_GENERATOR = "bridge.v1"`、`PROMPT_TEMPLATE_VERSION = "mindagent-skill-v1"`、`CANDIDATE_PRIORITY = 100`、`PROMPT_TEMPLATE`。

**设计要点:**
- 一条发现一份草案,只含一个 `kind: skill` 增量(Ruling 10);技能字典的形状见 Ruling 8,生成后立即 `Skill.from_dict` 读回一次,读不回就是 `ProposalParseError`。
- 桥接路径:提示要求"只输出一个 JSON 对象";`parse_strategy` 严格(未知键、空列表、非字符串都拒),失败把原因拼进下一次提示,总次数 `1 + max_parse_retries`,耗尽抛 `ProposalParseError(attempts=...)`。
- `BridgeError` 原样上抛(同一个对象),由轮次处理器按 R18 升级(Ruling 9)。每次尝试的 task_id 是 `<task_id>-<i>-attempt<n>`(`attempt_task_id`);`ScriptedBridge` 带 `store` 时,测试断言每次尝试各记一条 `external_inference`(两次解析失败加一次成功是三条)。
- 传了桥接而 `run_id` 为空:在调用桥接之前抛 `MindAgentDataError`(计划 1 的账本拒绝空 `run_id`);不传桥接时 `run_id` 可缺省。

- [ ] **Step 0: 前置检查(计划 1、计划 6 已合并)**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev python -c "import inspect; from simple_actio.instance import instantiate; from simple_actio.skills import Skill, SKILL_ID_PREFIX; from simple_actio.memory import MemoryEntry, MEMORY_ID_PREFIX; from simple_actio.trace import validate_trace, normalize_library_version; from simple_actio.triggers import referenced_signals; from simple_actio.signals import default_registry; from inference_bridge import BridgeError, ScriptedBridge; from controlplane.paths import agent_round_file, proposal_file, skill_file, memory_file, library_state_file, failure_record_file; from controlplane.ledger import record_failure; from simulator.realtime.client import EnvironmentServiceClient; assert 'activation_trace' in inspect.signature(EnvironmentServiceClient.submit_turn).parameters; assert SKILL_ID_PREFIX == 'skill-' and MEMORY_ID_PREFIX == 'mem-'; print('prereqs ok')"
```

Expected: 最后一行 `prereqs ok`。任何 `ImportError`、`AttributeError` 或 `AssertionError` 都说明计划 1 或计划 6 没有按其文本合并:**停下**,把报错原样报告,不要往下做。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_ma_proposals.py`:

```python
import json

import pytest

from simple_actio.skills import Skill
from controlplane.gitstore import GitStore
from controlplane.paths import run_trace_file
from inference_bridge import BridgeError, ScriptedBridge
from mindagent.analysis import BehaviorFinding
from mindagent.errors import MindAgentDataError, ProposalParseError
from mindagent.proposals import (
    BRIDGE_GENERATOR, CANDIDATE_PRIORITY, RULE_GENERATOR, ProposalDraft, attempt_task_id, parse_strategy, propose,
)


def _finding(signal="emotion_high", fid=None):
    return BehaviorFinding(id=fid or f"finding-{signal}-abc", signal=signal, pattern=f"p-{signal}",
                           support=3, ratio=0.75, episode_refs=("e0", "e1", "e2"))


GOOD = json.dumps({"instructions": ["先安抚对方", "再谈问题"], "description": "情绪优先"}, ensure_ascii=False)


def test_no_findings_no_drafts():
    assert propose([]) == []


def test_the_rule_template_makes_one_skill_that_targets_the_signal_and_reads_back():
    (d,) = propose([_finding()])
    assert isinstance(d, ProposalDraft) and d.finding_id == "finding-emotion_high-abc"
    assert d.generator == RULE_GENERATOR
    (inc,) = d.increments
    assert inc["kind"] == "skill"
    skill = Skill.from_dict(inc["definition"])
    assert skill.trigger == {"signal": "emotion_high"}
    assert skill.priority == CANDIDATE_PRIORITY and skill.level == "micro" and skill.version == 1
    assert skill.policy_instructions == ("先回应对方的情绪,确认对方的感受,再谈具体问题",)
    assert d.skill_definitions() == [inc["definition"]]


def test_each_finding_gets_its_own_draft_in_order_and_unknown_signals_get_a_generic_text():
    drafts = propose([_finding("no_new_information"), _finding("custom_signal")])
    assert [d.finding_id for d in drafts] == ["finding-no_new_information-abc", "finding-custom_signal-abc"]
    assert Skill.from_dict(drafts[1].increments[0]["definition"]).policy_instructions == (
        "信号 custom_signal 为真时,先处理它指向的状况",)


def test_with_a_bridge_the_model_writes_the_policy_text(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    bridge = ScriptedBridge([GOOD], store=store)
    (d,) = propose([_finding()], bridge=bridge, run_id="run-003", task_id="agent-zengen-v1-propose")
    assert d.generator == BRIDGE_GENERATOR
    definition = d.increments[0]["definition"]
    assert definition["policy"] == {"instructions": ["先安抚对方", "再谈问题"]}
    assert definition["description"] == "情绪优先"
    assert definition["trigger"] == {"signal": "emotion_high"}  # 触发条件不由外部模型决定
    ((prompt, run_id, task_id),) = bridge.calls
    assert (run_id, task_id) == ("run-003", "agent-zengen-v1-propose-1-attempt1")
    assert "emotion_high" in prompt and "只输出一个 JSON 对象" in prompt
    records = store.read_product(run_trace_file("run-003")).payload["cost_records"]
    assert [(r["task_id"], r["cost_type"], r["calls"]) for r in records] == [
        ("agent-zengen-v1-propose-1-attempt1", "external_inference", 1)]


def test_every_attempt_gets_its_own_task_id_and_its_own_cost_record(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    bridge = ScriptedBridge(["x", "y", GOOD, GOOD], store=store)
    propose([_finding("emotion_high", "f1"), _finding("no_new_information", "f2")],
            bridge=bridge, run_id="run-003", task_id="t")
    assert [c[2] for c in bridge.calls] == ["t-1-attempt1", "t-1-attempt2", "t-1-attempt3", "t-2-attempt1"]
    records = store.read_product(run_trace_file("run-003")).payload["cost_records"]
    assert [r["task_id"] for r in records] == ["t-1-attempt1", "t-1-attempt2", "t-1-attempt3", "t-2-attempt1"]
    assert attempt_task_id("t-1", 2) == "t-1-attempt2"


def test_a_bridge_without_a_run_id_is_rejected_before_any_call():
    bridge = ScriptedBridge([GOOD])
    with pytest.raises(MindAgentDataError, match="non-empty run_id"):
        propose([_finding()], bridge=bridge)
    assert bridge.calls == []
    assert len(propose([_finding()])) == 1  # 不用桥接时 run_id 可以缺省


def test_a_parse_failure_is_retried_with_the_error_in_the_prompt():
    bridge = ScriptedBridge(["not json", GOOD])
    (d,) = propose([_finding()], bridge=bridge, run_id="run-003")
    assert len(bridge.calls) == 2
    assert "上一次的输出不合格" in bridge.calls[1][0] and "not a single JSON object" in bridge.calls[1][0]
    assert d.increments[0]["definition"]["policy"]["instructions"] == ["先安抚对方", "再谈问题"]


def test_parse_retries_are_bounded_and_end_in_a_deterministic_error():
    bridge = ScriptedBridge(["x", "[]", '{"instructions": []}'])
    with pytest.raises(ProposalParseError) as exc:
        propose([_finding()], bridge=bridge, run_id="run-003", max_parse_retries=2)
    assert exc.value.attempts == 3 and "non-empty list" in exc.value.reason
    assert bridge.remaining == 0


def test_zero_parse_retries_means_one_call():
    bridge = ScriptedBridge(["x", GOOD])
    with pytest.raises(ProposalParseError):
        propose([_finding()], bridge=bridge, run_id="run-003", max_parse_retries=0)
    assert len(bridge.calls) == 1


@pytest.mark.parametrize("category", ["T1", "T2"])
def test_a_bridge_error_propagates_unchanged(category):
    err = BridgeError(category, "boom", attempts=3)
    with pytest.raises(BridgeError) as exc:
        propose([_finding()], bridge=ScriptedBridge([err]), run_id="run-003")
    assert exc.value is err


@pytest.mark.parametrize("text,msg", [
    ("", "not a single JSON object"),
    ('{"instructions": ["a"]} trailing', "not a single JSON object"),
    ('["a"]', "must be a JSON object"),
    ('{"instructions": ["a"], "trigger": {}}', "unknown keys"),
    ('{"instructions": "a"}', "non-empty list"),
    ('{"instructions": ["  "]}', "non-empty list"),
    ('{"instructions": ["a"], "description": 3}', "description must be a string"),
])
def test_parse_strategy_is_strict(text, msg):
    with pytest.raises(ValueError, match=msg):
        parse_strategy(text)


def test_parse_strategy_accepts_surrounding_whitespace_and_a_missing_description():
    assert parse_strategy('  {"instructions": [" a "]}\n') == (["a"], "")


def test_max_parse_retries_is_validated():
    with pytest.raises(ValueError):
        propose([], max_parse_retries=-1)
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ma_proposals.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindagent.proposals'`,末行 `1 error in ...`。

- [ ] **Step 3: 写实现**

新建 `src/mindagent/proposals.py`:

```python
"""技能提案生成器(闭环集成设计 §8.3):每条行为发现产出一份草案。

默认走规则模板(信号 -> 一条"先处理该信号"的技能);传入桥接时,让外部模型只写策略文本,
触发条件仍由发现的信号决定。外部模型必须只输出一个 JSON 对象;解析失败按 T2 带着错误信息
重试,上限 max_parse_retries,耗尽抛 ProposalParseError。桥接自己的 BridgeError 原样上抛。
记忆增量生成器 V1 不做:草案里只有 kind 为 skill 的增量。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from simple_actio.errors import SimpleActioDataError
from simple_actio.skills import Skill
from mindagent.analysis import BehaviorFinding
from mindagent.errors import MindAgentDataError, ProposalParseError

RULE_GENERATOR = "rule_template.v1"
BRIDGE_GENERATOR = "bridge.v1"
PROMPT_TEMPLATE_VERSION = "mindagent-skill-v1"
CANDIDATE_PRIORITY = 100

_RULE_TEXT = {
    "emotion_high": ("先回应对方的情绪,确认对方的感受,再谈具体问题",),
    "no_new_information": ("对方没有给出新信息时,先澄清追问,不要重复自己的立场",),
}

PROMPT_TEMPLATE = """你在为一个对话智能体写一条技能的响应策略。
跨多个对话观察到的行为模式:{pattern}
信号:{signal};受影响的对话 {support} 个,占比 {ratio:.2f}。
这条技能会在信号 {signal} 为真时被选中。请只输出一个 JSON 对象,不要输出任何其他文字:
{{"instructions": ["给模型的指令", ...], "description": "一句话说明"}}
instructions 是非空的字符串列表;description 是字符串。不允许其他键。"""


@dataclass(frozen=True)
class ProposalDraft:
    finding_id: str
    increments: tuple[dict, ...]
    generator: str

    def skill_definitions(self) -> list[dict]:
        return [inc["definition"] for inc in self.increments if inc["kind"] == "skill"]


def _skill_definition(finding: BehaviorFinding, instructions: list[str], description: str) -> dict:
    definition = {
        "id": f"skill-candidate-{finding.signal}",
        "version": 1,
        "level": "micro",
        "priority": CANDIDATE_PRIORITY,
        "trigger": {"signal": finding.signal},
        "policy": {"instructions": list(instructions)},
        "description": description,
    }
    Skill.from_dict(definition)  # 必须能被 SimpleActio 读回,否则是生成器的 bug
    return definition


def rule_template(finding: BehaviorFinding) -> list[str]:
    return list(_RULE_TEXT.get(finding.signal, (f"信号 {finding.signal} 为真时,先处理它指向的状况",)))


def parse_strategy(text: str) -> tuple[list[str], str]:
    """严格解析外部模型的输出。失败抛 ValueError(原因可读)。"""
    try:
        obj = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"output is not a single JSON object: {exc.msg}") from None
    if not isinstance(obj, dict):
        raise ValueError(f"output must be a JSON object, got {type(obj).__name__}")
    extra = sorted(set(obj) - {"instructions", "description"})
    if extra:
        raise ValueError(f"unknown keys {extra}")
    instructions = obj.get("instructions")
    if (
        not isinstance(instructions, list) or not instructions
        or any(not isinstance(s, str) or not s.strip() for s in instructions)
    ):
        raise ValueError("instructions must be a non-empty list of non-empty strings")
    description = obj.get("description", "")
    if not isinstance(description, str):
        raise ValueError("description must be a string")
    return [s.strip() for s in instructions], description


def attempt_task_id(task_id: str, attempt: int) -> str:
    """第 attempt 次(从 1 起)桥接调用的 task_id:<task_id>-attempt<n>。"""
    return f"{task_id}-attempt{attempt}"


def _from_bridge(finding: BehaviorFinding, bridge, *, run_id: str, task_id: str, max_parse_retries: int) -> tuple[list[str], str]:
    prompt = PROMPT_TEMPLATE.format(
        pattern=finding.pattern, signal=finding.signal, support=finding.support, ratio=finding.ratio,
    )
    attempts = 0
    last = ""
    while attempts <= max_parse_retries:
        attempts += 1
        asked = prompt if not last else f"{prompt}\n\n上一次的输出不合格:{last}。请重新输出。"
        # 每次尝试各用一个 task_id:账本对同一 (task_id, cost_type) 只记一次,复用会丢掉重试的成本
        result = bridge.complete(asked, run_id=run_id, task_id=attempt_task_id(task_id, attempts))
        try:
            return parse_strategy(result.text)
        except ValueError as exc:
            last = str(exc)
    raise ProposalParseError(f"finding {finding.id}: {last}", attempts=attempts)


def propose(
    findings: list[BehaviorFinding] | tuple[BehaviorFinding, ...],
    *,
    bridge=None,
    run_id: str = "",
    task_id: str = "mindagent-propose",
    max_parse_retries: int = 2,
) -> list[ProposalDraft]:
    """每条发现一份草案,顺序与 findings 相同。传入桥接时,第 i 条发现的第 n 次尝试用
    task_id-<i>-attempt<n> 调用,这样记账每次尝试各记一条。传入桥接时 run_id 必须非空
    (桥接会拿它记账、拼日志路径),否则在调用桥接之前抛 MindAgentDataError。"""
    if isinstance(max_parse_retries, bool) or not isinstance(max_parse_retries, int) or max_parse_retries < 0:
        raise ValueError(f"max_parse_retries must be an int >= 0, got {max_parse_retries!r}")
    if bridge is not None and not run_id:
        raise MindAgentDataError("propose with a bridge needs a non-empty run_id")
    drafts: list[ProposalDraft] = []
    for i, finding in enumerate(findings, start=1):
        if bridge is None:
            instructions = rule_template(finding)
            description = f"rule template for {finding.signal}"
            generator = RULE_GENERATOR
        else:
            instructions, description = _from_bridge(
                finding, bridge, run_id=run_id, task_id=f"{task_id}-{i}",
                max_parse_retries=max_parse_retries,
            )
            generator = BRIDGE_GENERATOR
        try:
            definition = _skill_definition(finding, instructions, description)
        except SimpleActioDataError as exc:
            raise ProposalParseError(f"finding {finding.id}: {exc}", attempts=1) from None
        drafts.append(ProposalDraft(
            finding_id=finding.id,
            increments=({"kind": "skill", "definition": definition},),
            generator=generator,
        ))
    return drafts
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ma_proposals.py`
Expected: `20 passed`,无警告。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+85 passed`(B 为开工时的基线),无警告。再跑 `git status --short`:除开工前就在的 ` M cli/package-lock.json`、` M cli/package.json` 外,只应有 `src/mindagent/proposals.py` 与 `tests/test_ma_proposals.py`。

- [ ] **Step 6: 提交**

```bash
git add src/mindagent/proposals.py
git add tests/test_ma_proposals.py
git commit -m "feat: mindagent skill proposal generator (rule template by default, optional bridge with strict JSON parsing and bounded T2 retries)"
```

---

### Task 84: 提案验证器 `verify`(R13)

**Files:**
- Create: `src/mindagent/verify.py`(134 行)
- Test: `tests/test_ma_verify.py`

**Interfaces:**
- Consumes: `mindagent.analysis`(`BehaviorFinding`、`agent_slot_of_episode`、`visible_history`);`mindagent.proposals.ProposalDraft`;`simple_actio.skills.Skill`;实例的 `respond` 与 `registry`。
- Produces(任务 85、87 依赖):
  - `verify(draft, findings, episodes, *, base_instance_factory, personas, scenarios, judge=None, run_id="", task_id="mindagent-judge") -> Verification`;`base_instance_factory(extra_skills: tuple[Skill, ...])` 每次返回新实例。
  - `Verification(method, episodes, fired_on_motivating, fired_elsewhere, notes, quality_notes=None)`(`to_dict()` 与设计 §4.2 的 `verification` 载荷同键;`quality_notes` 仅在有值时出现)。
  - `replay(instance, episode, persona, scenario) -> list[TurnRecord]`;`METHOD = "replay_behavior"`;`OVERFIT_NOTE`。

**设计要点:**
- 口径见 Ruling 11:离线重放、不起服务端;对方原话照抄且内心独白不给 agent 看;基线与候选两个实例各跑一遍。
- 测试覆盖三种行为:候选只在动机轮次上命中(1.0 / 0.0)、触发条件过宽(1.0 / 1.0)、优先级压不过基线技能(0.0)。
- 可选 judge 的回答只进 `quality_notes`,不影响两个比例;发现里引用的动机 episode 缺失或草案指向未知发现抛 `MindAgentDataError`;传了 judge 而 `run_id` 为空,在调用 judge 之前抛 `MindAgentDataError`。签名偏差见 Ruling 19。

- [ ] **Step 0: 前置检查(计划 1、计划 6 已合并)**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev python -c "import inspect; from simple_actio.instance import instantiate; from simple_actio.skills import Skill, SKILL_ID_PREFIX; from simple_actio.memory import MemoryEntry, MEMORY_ID_PREFIX; from simple_actio.trace import validate_trace, normalize_library_version; from simple_actio.triggers import referenced_signals; from simple_actio.signals import default_registry; from inference_bridge import BridgeError, ScriptedBridge; from controlplane.paths import agent_round_file, proposal_file, skill_file, memory_file, library_state_file, failure_record_file; from controlplane.ledger import record_failure; from simulator.realtime.client import EnvironmentServiceClient; assert 'activation_trace' in inspect.signature(EnvironmentServiceClient.submit_turn).parameters; assert SKILL_ID_PREFIX == 'skill-' and MEMORY_ID_PREFIX == 'mem-'; print('prereqs ok')"
```

Expected: 最后一行 `prereqs ok`。任何 `ImportError`、`AttributeError` 或 `AssertionError` 都说明计划 1 或计划 6 没有按其文本合并:**停下**,把报错原样报告,不要往下做。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_ma_verify.py`:

```python
import pytest

from simple_actio.instance import instantiate
from simple_actio.skills import Skill
from simple_actio.trace import build_trace
from inference_bridge import ScriptedBridge
from mindagent.analysis import BehaviorFinding, analyze
from mindagent.errors import MindAgentDataError
from mindagent.proposals import ProposalDraft, propose
from mindagent.verify import METHOD, OVERFIT_NOTE, Verification, replay, verify
from simulator.datamodel import Episode, Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario, TurnRecord

LV = {"skill": "1", "memory": "0"}
GOAL = Skill("skill-goal", 1, "macro", {"turn_at_least": 0}, ("推进目标",))
ANGRY = "我真的很生气!!"
PERSONAS = {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}
SCENARIOS = {"s1": Scenario("s1", "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "deployment_simulation")}


class Echo:
    def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
        return ("|".join(guidance) or "plain", None)


def _factory(base_skills=(GOAL,)):
    def make(extra=()):
        return instantiate(Echo(), skills=list(base_skills) + list(extra), memories=[],
                           library_version=LV, profile=None)
    return make


def _episode(eid, opp_lines):
    turns = []
    for line in opp_lines:
        turns.append(TurnRecord(len(turns), "b", line, "opp-thought"))
        trace = build_trace(signals=[], candidates=["skill-goal"], selected="skill-goal",
                            memory_hits=[], experience=None, library_version=LV)
        turns.append(TurnRecord(len(turns), "a", "old reply", None, trace))
    ra = RoleAssignment("s1", [RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1"),
                               RoleBinding("a", "pa", "self_play")], len(turns))
    return Episode(eid, "s1", ra, turns, "max_turns")


def _setup(lines=(ANGRY, "好吧")):
    eps = [_episode(f"e{i}", list(lines)) for i in range(3)]
    findings = analyze(eps, min_support=3, min_ratio=0.5, skills=[GOAL], signals=("emotion_high",))
    return eps, findings, propose(findings)


def test_the_candidate_fires_on_every_motivating_turn_and_nowhere_else():
    eps, findings, (draft,) = _setup()
    v = verify(draft, findings, eps, base_instance_factory=_factory(), personas=PERSONAS, scenarios=SCENARIOS)
    assert isinstance(v, Verification)
    assert v.method == METHOD == "replay_behavior"
    assert v.episodes == ("e0", "e1", "e2")
    assert (v.fired_on_motivating, v.fired_elsewhere) == (1.0, 0.0)
    assert "motivating turns 3, other turns 3" in v.notes
    assert "baseline selected the candidate on 0 motivating turn(s)" in v.notes
    assert OVERFIT_NOTE in v.notes
    assert v.quality_notes is None
    assert set(v.to_dict()) == {"method", "episodes", "fired_on_motivating", "fired_elsewhere", "notes"}


def test_a_candidate_that_also_fires_elsewhere_is_measured_as_such():
    eps, findings, (draft,) = _setup()
    broad = dict(draft.increments[0]["definition"], trigger={"turn_at_least": 0})
    wide = ProposalDraft(draft.finding_id, ({"kind": "skill", "definition": broad},), draft.generator)
    v = verify(wide, findings, eps, base_instance_factory=_factory(), personas=PERSONAS, scenarios=SCENARIOS)
    assert (v.fired_on_motivating, v.fired_elsewhere) == (1.0, 1.0)


def test_a_candidate_that_loses_routing_does_not_fire():
    eps, findings, (draft,) = _setup()
    weak = dict(draft.increments[0]["definition"], priority=-1)
    low = ProposalDraft(draft.finding_id, ({"kind": "skill", "definition": weak},), draft.generator)
    v = verify(low, findings, eps, base_instance_factory=_factory(), personas=PERSONAS, scenarios=SCENARIOS)
    assert v.fired_on_motivating == 0.0


def test_replay_keeps_opponent_lines_and_regenerates_agent_turns_without_leaking_thoughts():
    seen = []

    class Recorder(Echo):
        def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
            seen.append([(t.speaker_slot, t.utterance, t.inner_thought) for t in history])
            return super().respond_guided(persona, scenario, history, speaker_slot, guidance)

    inst = instantiate(Recorder(), skills=[GOAL], memories=[], library_version=LV, profile=None)
    turns = replay(inst, _episode("e0", [ANGRY, "好吧"]), PERSONAS["pa"], SCENARIOS["s1"])
    assert [(t.speaker_slot, t.utterance) for t in turns] == [
        ("b", ANGRY), ("a", "[skill:skill-goal] 推进目标"), ("b", "好吧"), ("a", "[skill:skill-goal] 推进目标")]
    assert seen[0] == [("b", ANGRY, None)]
    assert all(t.activation_trace["selected"] == "skill-goal" for t in turns if t.speaker_slot == "a")


def test_a_judge_adds_quality_notes_in_their_own_field():
    eps, findings, (draft,) = _setup()
    judge = ScriptedBridge(["候选回复更体贴"])
    v = verify(draft, findings, eps, base_instance_factory=_factory(), personas=PERSONAS,
               scenarios=SCENARIOS, judge=judge, run_id="run-003", task_id="agent-zengen-v1-judge")
    assert v.quality_notes == "候选回复更体贴"
    assert v.to_dict()["quality_notes"] == "候选回复更体贴"
    ((prompt, run_id, task_id),) = judge.calls
    assert (run_id, task_id) == ("run-003", "agent-zengen-v1-judge")
    assert prompt.count("[e") == 3 and "候选:[skill:skill-candidate-emotion_high]" in prompt


def test_a_judge_without_a_run_id_is_rejected_before_any_call():
    eps, findings, (draft,) = _setup()
    judge = ScriptedBridge(["x"])
    with pytest.raises(MindAgentDataError, match="non-empty run_id"):
        verify(draft, findings, eps, base_instance_factory=_factory(), personas=PERSONAS,
               scenarios=SCENARIOS, judge=judge)
    assert judge.calls == []


def test_missing_motivating_episodes_and_unknown_findings_are_data_errors():
    eps, findings, (draft,) = _setup()
    with pytest.raises(MindAgentDataError, match="motivating episodes not given"):
        verify(draft, findings, eps[:1], base_instance_factory=_factory(), personas=PERSONAS, scenarios=SCENARIOS)
    orphan = ProposalDraft("finding-ghost", draft.increments, draft.generator)
    with pytest.raises(MindAgentDataError, match="unknown finding"):
        verify(orphan, findings, eps, base_instance_factory=_factory(), personas=PERSONAS, scenarios=SCENARIOS)


def test_verify_does_not_touch_its_inputs():
    eps, findings, (draft,) = _setup()
    before = [e.to_dict() for e in eps]
    verify(draft, findings, eps, base_instance_factory=_factory(), personas=PERSONAS, scenarios=SCENARIOS)
    assert [e.to_dict() for e in eps] == before
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ma_verify.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindagent.verify'`,末行 `1 error in ...`。

- [ ] **Step 3: 写实现**

新建 `src/mindagent/verify.py`:

```python
"""提案验证器(闭环集成设计 §8.3、R13):离线重放动机 episode,度量"行为是否按预期改变"。

用保存下来的对方原话重放:对方轮次照抄,agent 轮次由实例重新生成(不起服务端)。分别用基线实例
与加载候选技能后的实例驱动,度量候选技能在动机轮次(动机 episode 里该信号为真的 agent 轮次)上被
选中的比例 fired_on_motivating,以及在动机 episode 其余 agent 轮次上被选中的比例 fired_elsewhere。
它不度量对话质量;可选传入 judge 桥接做质量评分,结果单列在 quality_notes。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from simple_actio.skills import Skill
from mindagent.analysis import BehaviorFinding, agent_slot_of_episode, visible_history
from mindagent.errors import MindAgentDataError
from mindagent.proposals import ProposalDraft
from simulator.datamodel import Episode, Persona, Scenario, TurnRecord

METHOD = "replay_behavior"
OVERFIT_NOTE = (
    "verified on the same episodes that motivated the proposal (no held-out set); "
    "overfitting risk, keep monitoring in later rounds"
)


@dataclass(frozen=True)
class Verification:
    method: str
    episodes: tuple[str, ...]
    fired_on_motivating: float
    fired_elsewhere: float
    notes: str
    quality_notes: str | None = None

    def to_dict(self) -> dict:
        d = {
            "method": self.method,
            "episodes": list(self.episodes),
            "fired_on_motivating": self.fired_on_motivating,
            "fired_elsewhere": self.fired_elsewhere,
            "notes": self.notes,
        }
        if self.quality_notes is not None:
            d["quality_notes"] = self.quality_notes
        return d


def replay(instance, episode: Episode, persona: Persona, scenario: Scenario) -> list[TurnRecord]:
    """按保存下来的对方原话重放一条 episode,返回重放出的完整轮次(agent 轮次带新轨迹)。"""
    slot = agent_slot_of_episode(episode)
    turns: list[TurnRecord] = []
    for saved in episode.turns:
        if saved.speaker_slot != slot:
            turns.append(TurnRecord(saved.turn_index, saved.speaker_slot, saved.utterance))
            continue
        history = visible_history(turns, len(turns), slot)
        utterance, thought, trace = instance.respond(persona, scenario, history, slot)
        turns.append(TurnRecord(saved.turn_index, slot, utterance, thought, trace))
    return turns


def _ratio(hits: int, total: int) -> float:
    return hits / total if total else 0.0


def verify(
    draft: ProposalDraft,
    findings: list[BehaviorFinding] | tuple[BehaviorFinding, ...],
    episodes: list[Episode] | tuple[Episode, ...],
    *,
    base_instance_factory: Callable[[tuple[Skill, ...]], object],
    personas: dict[str, Persona],
    scenarios: dict[str, Scenario],
    judge=None,
    run_id: str = "",
    task_id: str = "mindagent-judge",
) -> Verification:
    """base_instance_factory(extra_skills) 返回一个新实例:extra_skills 为空是基线,
    为草案的技能是候选。episodes 里必须包含发现引用的全部动机 episode。
    传入 judge 时 run_id 必须非空(judge 是桥接,会记账),否则抛 MindAgentDataError。"""
    if judge is not None and not run_id:
        raise MindAgentDataError("verify with a judge needs a non-empty run_id")
    finding = next((f for f in findings if f.id == draft.finding_id), None)
    if finding is None:
        raise MindAgentDataError(f"draft refers to unknown finding {draft.finding_id!r}")
    by_id = {e.id: e for e in episodes}
    missing = [ref for ref in finding.episode_refs if ref not in by_id]
    if missing:
        raise MindAgentDataError(f"finding {finding.id}: motivating episodes not given: {missing}")
    candidate = tuple(Skill.from_dict(d) for d in draft.skill_definitions())
    candidate_ids = {s.id for s in candidate}
    baseline = base_instance_factory(())
    loaded = base_instance_factory(candidate)

    on_hits = on_total = else_hits = else_total = base_on_hits = 0
    pairs: list[tuple[str, str, str]] = []
    for ref in finding.episode_refs:
        episode = by_id[ref]
        slot = agent_slot_of_episode(episode)
        persona_id = next(b.persona_id for b in episode.role_assignment.bindings if b.slot_id == slot)
        persona, scenario = personas[persona_id], scenarios[episode.scenario_id]
        base_turns = replay(baseline, episode, persona, scenario)
        cand_turns = replay(loaded, episode, persona, scenario)
        signal_fn = loaded.registry.get(finding.signal)
        for i, (b, c) in enumerate(zip(base_turns, cand_turns)):
            if c.speaker_slot != slot:
                continue
            fired = c.activation_trace["selected"] in candidate_ids
            if signal_fn(visible_history(cand_turns, i, slot), slot):
                on_total += 1
                on_hits += fired
                base_on_hits += b.activation_trace["selected"] in candidate_ids
                pairs.append((ref, b.utterance, c.utterance))
            else:
                else_total += 1
                else_hits += fired
    notes = (
        f"motivating turns {on_total}, other turns {else_total}; "
        f"baseline selected the candidate on {base_on_hits} motivating turn(s); {OVERFIT_NOTE}"
    )
    quality = None
    if judge is not None:
        prompt = "对比每组里基线回复与候选回复的对话质量,给出简短评价:\n" + "\n".join(
            f"[{ref}] 基线:{b} | 候选:{c}" for ref, b, c in pairs
        )
        quality = judge.complete(prompt, run_id=run_id, task_id=task_id).text
    return Verification(
        method=METHOD,
        episodes=tuple(finding.episode_refs),
        fired_on_motivating=_ratio(on_hits, on_total),
        fired_elsewhere=_ratio(else_hits, else_total),
        notes=notes,
        quality_notes=quality,
    )
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ma_verify.py`
Expected: `8 passed`,无警告。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+93 passed`(B 为开工时的基线),无警告。再跑 `git status --short`:除开工前就在的 ` M cli/package-lock.json`、` M cli/package.json` 外,只应有 `src/mindagent/verify.py` 与 `tests/test_ma_verify.py`。

- [ ] **Step 6: 提交**

```bash
git add src/mindagent/verify.py
git add tests/test_ma_verify.py
git commit -m "feat: mindagent proposal verifier (offline replay of motivating episodes, fired_on_motivating and fired_elsewhere, optional judge notes kept separate)"
```

---

### Task 85: 提案打包器 `package`

**Files:**
- Create: `src/mindagent/packaging.py`(141 行)
- Test: `tests/test_ma_packaging.py`

**Interfaces:**
- Consumes: `controlplane.paths.proposal_file`;`Envelope`、`OwnerRef`;`BehaviorFinding`、`ProposalDraft`、`Verification`。
- Produces(任务 87 依赖;载荷键是计划 8 命令行的契约,见"跨语言约定"):
  - `package(store, round_id, findings, drafts, verifications, *, thresholds, run_id, bound_model_id) -> list[Envelope]`。
  - `screen(drafts, verifications, thresholds) -> (kept_indices, dropped)`;`stale_proposals(store, round_id, kept) -> list[{proposal_id, reason}]`(Ruling 12);`Thresholds(min_fired_on_motivating, max_fired_elsewhere)`;`proposal_payload(...)`;`proposal_id_for(round_id, n)`;`PROPOSAL_TYPE = "skill_memory_proposal"`。

**设计要点:**
- 产物:`skill_memory_proposals/proposal-<round_id>-<n>/proposal.yaml`,`type: skill_memory_proposal`,状态 `pending_review`,载荷按设计 §4.2 加三个辅助键(Ruling 12)。
- 筛选阈值两端含等号;被筛掉的草案不占编号,原因由 `screen` 返回,轮次处理器写进 `dropped`。
- 重入:载荷相同不提交;人已审过的提案(非 `pending_review`)原样保留,不被覆盖。

- [ ] **Step 0: 前置检查(计划 1、计划 6 已合并)**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev python -c "import inspect; from simple_actio.instance import instantiate; from simple_actio.skills import Skill, SKILL_ID_PREFIX; from simple_actio.memory import MemoryEntry, MEMORY_ID_PREFIX; from simple_actio.trace import validate_trace, normalize_library_version; from simple_actio.triggers import referenced_signals; from simple_actio.signals import default_registry; from inference_bridge import BridgeError, ScriptedBridge; from controlplane.paths import agent_round_file, proposal_file, skill_file, memory_file, library_state_file, failure_record_file; from controlplane.ledger import record_failure; from simulator.realtime.client import EnvironmentServiceClient; assert 'activation_trace' in inspect.signature(EnvironmentServiceClient.submit_turn).parameters; assert SKILL_ID_PREFIX == 'skill-' and MEMORY_ID_PREFIX == 'mem-'; print('prereqs ok')"
```

Expected: 最后一行 `prereqs ok`。任何 `ImportError`、`AttributeError` 或 `AssertionError` 都说明计划 1 或计划 6 没有按其文本合并:**停下**,把报错原样报告,不要往下做。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_ma_packaging.py`:

```python
import pytest

from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT, proposal_file
from mindagent.analysis import BehaviorFinding
from mindagent.packaging import PROPOSAL_TYPE, Thresholds, package, proposal_id_for, screen
from mindagent.proposals import propose
from mindagent.verify import Verification

ROUND = "agent-round-zengen-v3"
TH = Thresholds(min_fired_on_motivating=0.8, max_fired_elsewhere=0.2)


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _finding(signal):
    return BehaviorFinding(f"finding-{signal}-x", signal, f"p-{signal}", 3, 0.75, ("e0", "e1", "e2"))


def _v(on, elsewhere):
    return Verification("replay_behavior", ("e0", "e1", "e2"), on, elsewhere, "notes")


def _pack(store, findings, verifications):
    drafts = propose(findings)
    return package(store, ROUND, findings, drafts, verifications, thresholds=TH,
                   run_id="run-003", bound_model_id="zengen-v3")


def test_a_passing_draft_becomes_a_pending_review_proposal_with_the_spec_payload(store):
    f = _finding("emotion_high")
    (env,) = _pack(store, [f], [_v(1.0, 0.0)])
    assert env.id == "proposal-agent-round-zengen-v3-1" == proposal_id_for(ROUND, 1)
    assert (env.type, env.status) == (PROPOSAL_TYPE, "pending_review")
    assert env.links == [ROUND, f.id]
    on_disk = store.read_product(proposal_file(env.id))
    assert on_disk.to_dict() == env.to_dict()
    p = on_disk.payload
    assert p["round_id"] == ROUND and p["run_id"] == "run-003" and p["bound_model_id"] == "zengen-v3"
    assert p["finding"] == {"id": f.id, "pattern": "p-emotion_high", "support": 3, "ratio": 0.75,
                            "episode_refs": ["e0", "e1", "e2"]}
    (inc,) = p["increments"]
    assert inc["kind"] == "skill" and inc["definition"]["trigger"] == {"signal": "emotion_high"}
    assert p["verification"] == {"method": "replay_behavior", "episodes": ["e0", "e1", "e2"],
                                 "fired_on_motivating": 1.0, "fired_elsewhere": 0.0, "notes": "notes"}


def test_failing_drafts_are_not_packaged_and_numbering_stays_dense(store):
    fs = [_finding("a_sig"), _finding("b_sig"), _finding("c_sig")]
    vs = [_v(0.5, 0.0), _v(1.0, 0.0), _v(1.0, 0.5)]
    envs = _pack(store, fs, vs)
    assert [e.id for e in envs] == ["proposal-agent-round-zengen-v3-1"]
    assert envs[0].payload["finding"]["id"] == "finding-b_sig-x"
    kept, dropped = screen(propose(fs), vs, TH)
    assert kept == [1]
    assert [(d["finding_id"], d["reason"]) for d in dropped] == [
        ("finding-a_sig-x", "fired_on_motivating 0.500 < 0.8"),
        ("finding-c_sig-x", "fired_elsewhere 0.500 > 0.2"),
    ]


def test_the_thresholds_are_inclusive(store):
    kept, dropped = screen(propose([_finding("a_sig")]), [_v(0.8, 0.2)], TH)
    assert kept == [0] and dropped == []


def test_repackaging_the_same_drafts_does_not_rewrite_or_duplicate(store):
    f = _finding("emotion_high")
    first = _pack(store, [f], [_v(1.0, 0.0)])
    head = store._git("rev-parse", "HEAD")
    again = _pack(store, [f], [_v(1.0, 0.0)])
    assert [e.to_dict() for e in again] == [e.to_dict() for e in first]
    assert store._git("rev-parse", "HEAD") == head
    assert len(list((store.repo_root / f"{CONTROL_PLANE_ROOT}/skill_memory_proposals").iterdir())) == 1


def test_a_reviewed_proposal_is_never_overwritten(store):
    f = _finding("emotion_high")
    (env,) = _pack(store, [f], [_v(1.0, 0.0)])
    env.status = "approved"
    store.write_product(proposal_file(env.id), env, "human: approve")
    (again,) = _pack(store, [f], [_v(0.9, 0.0)])
    assert again.status == "approved"
    assert store.read_product(proposal_file(env.id)).payload["verification"]["fired_on_motivating"] == 1.0


def test_a_pending_proposal_with_a_changed_payload_is_rewritten_in_place(store):
    f = _finding("emotion_high")
    _pack(store, [f], [_v(1.0, 0.0)])
    (again,) = _pack(store, [f], [_v(0.9, 0.0)])
    assert store.read_product(proposal_file(again.id)).payload["verification"]["fired_on_motivating"] == 0.9


def test_mismatched_lengths_are_rejected(store):
    with pytest.raises(ValueError, match="1 drafts but 0 verifications"):
        screen(propose([_finding("a_sig")]), [], TH)


def test_nothing_to_package_writes_nothing(store):
    assert package(store, ROUND, [], [], [], thresholds=TH, run_id="run-003", bound_model_id="zengen-v3") == []
    assert list((store.repo_root / f"{CONTROL_PLANE_ROOT}/skill_memory_proposals").iterdir()) == []
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ma_packaging.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindagent.packaging'`,末行 `1 error in ...`。

- [ ] **Step 3: 写实现**

新建 `src/mindagent/packaging.py`:

```python
"""提案打包器(闭环集成设计 §4.2、§8.3):验证通过的草案写成 skill_memory_proposal 产物。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT, proposal_file
from mindagent.analysis import BehaviorFinding
from mindagent.proposals import ProposalDraft
from mindagent.verify import Verification

PROPOSAL_TYPE = "skill_memory_proposal"


@dataclass(frozen=True)
class Thresholds:
    min_fired_on_motivating: float
    max_fired_elsewhere: float


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def proposal_id_for(round_id: str, n: int) -> str:
    return f"proposal-{round_id}-{n}"


def screen(
    drafts: list[ProposalDraft] | tuple[ProposalDraft, ...],
    verifications: list[Verification] | tuple[Verification, ...],
    thresholds: Thresholds,
) -> tuple[list[int], list[dict]]:
    """返回 (通过的草案下标, dropped 列表)。dropped 每项 {finding_id, reason, fired_on_motivating, fired_elsewhere}。"""
    if len(drafts) != len(verifications):
        raise ValueError(f"{len(drafts)} drafts but {len(verifications)} verifications")
    kept: list[int] = []
    dropped: list[dict] = []
    for i, (draft, v) in enumerate(zip(drafts, verifications)):
        reasons = []
        if v.fired_on_motivating < thresholds.min_fired_on_motivating:
            reasons.append(
                f"fired_on_motivating {v.fired_on_motivating:.3f} < {thresholds.min_fired_on_motivating}"
            )
        if v.fired_elsewhere > thresholds.max_fired_elsewhere:
            reasons.append(f"fired_elsewhere {v.fired_elsewhere:.3f} > {thresholds.max_fired_elsewhere}")
        if reasons:
            dropped.append({
                "finding_id": draft.finding_id, "reason": "; ".join(reasons),
                "fired_on_motivating": v.fired_on_motivating, "fired_elsewhere": v.fired_elsewhere,
            })
        else:
            kept.append(i)
    return kept, dropped


def proposal_payload(
    round_id: str, finding: BehaviorFinding, draft: ProposalDraft, verification: Verification,
    *, run_id: str, bound_model_id: str,
) -> dict:
    return {
        "round_id": round_id,
        "bound_model_id": bound_model_id,
        "run_id": run_id,
        "generator": draft.generator,
        "finding": {
            "id": finding.id,
            "pattern": finding.pattern,
            "support": finding.support,
            "ratio": finding.ratio,
            "episode_refs": list(finding.episode_refs),
        },
        "increments": [{"kind": inc["kind"], "definition": dict(inc["definition"])} for inc in draft.increments],
        "verification": verification.to_dict(),
    }


def package(
    store: GitStore,
    round_id: str,
    findings: list[BehaviorFinding] | tuple[BehaviorFinding, ...],
    drafts: list[ProposalDraft] | tuple[ProposalDraft, ...],
    verifications: list[Verification] | tuple[Verification, ...],
    *,
    thresholds: Thresholds,
    run_id: str,
    bound_model_id: str,
) -> list[Envelope]:
    """通过筛选的草案按顺序编号 proposal-<round_id>-<n>(n 从 1 起),写成 pending_review,返回写下
    (或已在树里)的信封。重入:同 id 的提案已不是 pending_review(人已审过)就原样保留;仍是
    pending_review 且载荷相同就不重写。"""
    by_id = {f.id: f for f in findings}
    kept, _ = screen(drafts, verifications, thresholds)
    out: list[Envelope] = []
    for n, i in enumerate(kept, start=1):
        draft, verification = drafts[i], verifications[i]
        pid = proposal_id_for(round_id, n)
        rel = proposal_file(pid)
        payload = proposal_payload(
            round_id, by_id[draft.finding_id], draft, verification,
            run_id=run_id, bound_model_id=bound_model_id,
        )
        if store.exists(rel):
            existing = store.read_product(rel)
            if existing.status != "pending_review" or existing.payload == payload:
                out.append(existing)
                continue
        env = Envelope(
            id=pid, type=PROPOSAL_TYPE, version=1, created_at=_now(), created_by=OwnerRef(),
            status="pending_review", schema_version=1, links=[round_id, draft.finding_id],
            payload=payload,
        )
        store.write_product(rel, env, f"mindagent: proposal {pid}")
        out.append(env)
    return out


def stale_proposals(store: GitStore, round_id: str, kept: int) -> list[dict]:
    """本轮重入后编号超出 kept 的旧提案(上一次运行留下、这次筛选没再产出的)。只报告仍为
    pending_review 的那些,原样留在树里不删不改,由轮次处理器写进 dropped 提醒审核人;
    已审过的不报告(人已经看过)。按编号排序。"""
    prefix = f"proposal-{round_id}-"
    root = store.repo_root / CONTROL_PLANE_ROOT / "skill_memory_proposals"
    if not root.is_dir():
        return []
    found: list[tuple[int, str]] = []
    for sub in root.iterdir():
        tail = sub.name[len(prefix):] if sub.name.startswith(prefix) else ""
        if tail.isdecimal() and int(tail) > kept and store.exists(proposal_file(sub.name)):
            found.append((int(tail), sub.name))
    out = []
    for _, pid in sorted(found):
        env = store.read_product(proposal_file(pid))
        if env.status == "pending_review":
            out.append({
                "proposal_id": pid,
                "reason": "stale: left by an earlier run of this round, not produced again; do not approve",
            })
    return out
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ma_packaging.py`
Expected: `8 passed`,无警告。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+101 passed`(B 为开工时的基线),无警告。再跑 `git status --short`:除开工前就在的 ` M cli/package-lock.json`、` M cli/package.json` 外,只应有 `src/mindagent/packaging.py` 与 `tests/test_ma_packaging.py`。

- [ ] **Step 6: 提交**

```bash
git add src/mindagent/packaging.py
git add tests/test_ma_packaging.py
git commit -m "feat: mindagent proposal packager (pending_review skill_memory_proposal per verified draft, failing drafts screened out, reviewed proposals never overwritten)"
```

---

### Task 86: 收录处理器 `handle_admit_proposal`

**Files:**
- Create: `src/mindagent/failures.py`(48 行)、`src/mindagent/admit.py`(107 行)
- Test: `tests/test_ma_admit.py`

**Interfaces:**
- Consumes: `controlplane.paths`(`proposal_file`、`skill_file`、`memory_file`、`library_state_file`、`failure_record_file`、`run_dir`);`controlplane.ledger.record_failure`(只经 `fail_once` 调);`controlplane.validate.EnvelopeValidationError`、`controlplane.migration.UnknownSchemaVersionError`;`controlplane.states.assert_transition`;`controlplane.reconcile.Action`;`simple_actio.skills.Skill`、`simple_actio.memory.MemoryEntry`;`mindagent.library`(`LIBRARY_STATE_ID`、`read_library_versions`、`skill_id_for`、`memory_id_for`)。
- Produces(计划 8 / 9 把它注册为 `admit_proposal` 的处理器):`handle_admit_proposal(store, action) -> None`,形状 `Callable[[GitStore, Action], None]`;`action.target_id` 是提案 id。
- Produces(任务 87 依赖):`mindagent.failures` 的 `UNREADABLE`、`read_or_none(store, rel) -> Envelope | None`、`_single_segment(run_id) -> bool`、`fail_once(store, run_id, *, task_id, category, point, retries, reason) -> bool`(Ruling 20、21)。

**设计要点:**
- 顺序与重入见 Ruling 16:先校验全部增量 → 写条目(已存在跳过)→ `library_state`(`admitted` 已记该提案就跳过)→ 提案 `effective`。
- 故障注入:分别让写技能条目、写 `library_state`、写提案三步抛异常,重跑两次后版本只加一、`admitted` 只有一条、条目只有一份;两个增量之间崩溃也收敛。
- 增量不合法,或 `library_state` 的版本不是十进制数字串(Ruling 15):经 `fail_once` 写一条 `admit-<提案 id>` 的失败记录(**T2**,`point: mindagent.admit`,Ruling 16),提案留在 `approved`,库不变,处理器不抛;再调一次不产生提交。
- 坏 run id(Ruling 21):提案载荷的 `run_id` 为 `"a/b"`、`".."`、空串、`None`、或指向不存在的 run 时,不抛、不新建 run 目录、不写失败记录,提案留在 `approved`(5 个参数化用例)。测试夹具先建好 `runs/run-003/`(真实树里提案所属的 run 必然存在)。
- 记忆增量用手写的提案测试(Ruling 10)。

- [ ] **Step 0: 前置检查(计划 1、计划 6 已合并)**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev python -c "import inspect; from simple_actio.instance import instantiate; from simple_actio.skills import Skill, SKILL_ID_PREFIX; from simple_actio.memory import MemoryEntry, MEMORY_ID_PREFIX; from simple_actio.trace import validate_trace, normalize_library_version; from simple_actio.triggers import referenced_signals; from simple_actio.signals import default_registry; from inference_bridge import BridgeError, ScriptedBridge; from controlplane.paths import agent_round_file, proposal_file, skill_file, memory_file, library_state_file, failure_record_file; from controlplane.ledger import record_failure; from simulator.realtime.client import EnvironmentServiceClient; assert 'activation_trace' in inspect.signature(EnvironmentServiceClient.submit_turn).parameters; assert SKILL_ID_PREFIX == 'skill-' and MEMORY_ID_PREFIX == 'mem-'; print('prereqs ok')"
```

Expected: 最后一行 `prereqs ok`。任何 `ImportError`、`AttributeError` 或 `AssertionError` 都说明计划 1 或计划 6 没有按其文本合并:**停下**,把报错原样报告,不要往下做。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_ma_admit.py`:

```python
import pytest

from simple_actio.memory import MemoryEntry
from simple_actio.skills import Skill
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import (
    CONTROL_PLANE_ROOT, failure_record_file, library_state_file, memory_file, proposal_file, run_dir, skill_file,
)
from controlplane.reconcile import Action
from mindagent.admit import handle_admit_proposal
from mindagent.library import load_library

PID = "proposal-agent-round-zengen-v3-1"
SKILL = {"id": "skill-candidate-emotion_high", "version": 1, "level": "micro", "priority": 100,
         "trigger": {"signal": "emotion_high"}, "policy": {"instructions": ["先回应情绪"]}, "description": "d"}
MEMORY = {"id": "mem-candidate", "version": 1, "subject": "pb", "content": "对方住在北京",
          "timestamp": "2026-10-01T00:00:00Z", "confidence": 0.8, "source_episode": "deploy-zengen-v3-s1-1"}


class Crash(Exception):
    pass


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    (s.repo_root / run_dir("run-003")).mkdir(parents=True)  # 提案所属的 run(产生绑定模型的那个)
    return s


def _put(store, pid=PID, status="approved", increments=None, run_id="run-003"):
    env = Envelope(
        id=pid, type="skill_memory_proposal", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status=status, schema_version=1, links=[],
        payload={"round_id": "agent-round-zengen-v3", "run_id": run_id,
                 "increments": increments if increments is not None else [{"kind": "skill", "definition": SKILL}],
                 "verification": {"method": "replay_behavior"}},
    )
    store.write_product(proposal_file(pid), env, "test: proposal")


def _admit(store, pid=PID):
    handle_admit_proposal(store, Action(kind="admit_proposal", target_id=pid, detail={}))


def _crash_on(monkeypatch, store, path_fragment):
    real = store.write_product

    def flaky(rel, env, message):
        if path_fragment in rel:
            raise Crash(rel)
        real(rel, env, message)

    monkeypatch.setattr(store, "write_product", flaky)
    return lambda: monkeypatch.setattr(store, "write_product", real)


def _state(store):
    return store.read_product(library_state_file()).payload


def test_an_approved_skill_proposal_is_admitted(store):
    _put(store)
    _admit(store)
    sid = "skill-agent-round-zengen-v3-1-1"
    env = store.read_product(skill_file(sid))
    assert (env.id, env.type, env.status, env.links) == (sid, "skill", "effective", [PID])
    assert Skill.from_dict(env.payload).id == sid
    assert env.payload["trigger"] == {"signal": "emotion_high"}
    assert _state(store) == {"skill_version": "1", "memory_version": "0", "admitted": [
        {"proposal_id": PID, "skill_version": "1", "memory_version": "0", "entries": [sid]}]}
    lib = store.read_product(library_state_file())
    assert (lib.id, lib.type, lib.status) == ("library-state", "library_state", "executed")
    assert store.read_product(proposal_file(PID)).status == "effective"
    assert load_library(store).version == {"skill": "1", "memory": "0"}


def test_a_handwritten_memory_proposal_is_admitted_to_the_memory_library(store):
    _put(store, increments=[{"kind": "memory", "definition": MEMORY}])
    _admit(store)
    mid = "mem-agent-round-zengen-v3-1-1"
    env = store.read_product(memory_file(mid))
    assert (env.type, env.status) == ("memory", "effective")
    assert MemoryEntry.from_dict(env.payload).content == "对方住在北京"
    assert _state(store)["skill_version"] == "0" and _state(store)["memory_version"] == "1"


def test_a_mixed_proposal_bumps_both_versions_once_and_numbers_entries_by_position(store):
    _put(store, increments=[{"kind": "skill", "definition": SKILL}, {"kind": "memory", "definition": MEMORY}])
    _admit(store)
    assert store.exists(skill_file("skill-agent-round-zengen-v3-1-1"))
    assert store.exists(memory_file("mem-agent-round-zengen-v3-1-2"))
    assert (_state(store)["skill_version"], _state(store)["memory_version"]) == ("1", "1")


def test_a_second_proposal_increments_from_the_current_state(store):
    _put(store)
    _admit(store)
    _put(store, pid="proposal-agent-round-zengen-v4-1")
    _admit(store, "proposal-agent-round-zengen-v4-1")
    assert [a["proposal_id"] for a in _state(store)["admitted"]] == [PID, "proposal-agent-round-zengen-v4-1"]
    assert _state(store)["skill_version"] == "2"
    assert store.read_product(library_state_file()).version == 2


def test_readmitting_is_a_noop(store):
    _put(store)
    _admit(store)
    head = store._git("rev-parse", "HEAD")
    _admit(store)
    assert store._git("rev-parse", "HEAD") == head


@pytest.mark.parametrize("status", ["pending_review", "rejected", "effective"])
def test_a_proposal_that_is_not_approved_is_left_alone(store, status):
    _put(store, status=status)
    head = store._git("rev-parse", "HEAD")
    _admit(store)
    assert store._git("rev-parse", "HEAD") == head
    assert not store.exists(library_state_file())


@pytest.mark.parametrize("fragment", ["skill_library/", "library_state.yaml", "proposal.yaml"])
def test_a_crash_at_any_step_converges_on_rerun_without_double_counting(store, monkeypatch, fragment):
    _put(store)
    restore = _crash_on(monkeypatch, store, fragment)
    with pytest.raises(Crash):
        _admit(store)
    assert store.read_product(proposal_file(PID)).status == "approved"
    restore()
    _admit(store)
    _admit(store)
    assert _state(store)["skill_version"] == "1"
    assert len(_state(store)["admitted"]) == 1
    assert store.read_product(proposal_file(PID)).status == "effective"
    assert len(list((store.repo_root / f"{CONTROL_PLANE_ROOT}/skill_library").glob("*.yaml"))) == 1


def test_a_crash_between_two_entries_converges(store, monkeypatch):
    _put(store, increments=[{"kind": "skill", "definition": SKILL}, {"kind": "memory", "definition": MEMORY}])
    restore = _crash_on(monkeypatch, store, "memory_library/")
    with pytest.raises(Crash):
        _admit(store)
    assert store.exists(skill_file("skill-agent-round-zengen-v3-1-1"))
    assert not store.exists(library_state_file())
    restore()
    _admit(store)
    assert (_state(store)["skill_version"], _state(store)["memory_version"]) == ("1", "1")


def test_a_non_decimal_library_version_fails_admission_without_crashing(store):
    store.write_product(library_state_file(), Envelope(
        id="library-state", type="library_state", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="executed", schema_version=1,
        payload={"skill_version": "lib-3", "memory_version": "0", "admitted": []}), "human: library")
    _put(store)
    _admit(store)  # 不抛:守护进程不会每个 tick 都崩
    fr = store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    assert "skill_version must be a decimal string" in fr.payload["reason"]
    assert store.read_product(proposal_file(PID)).status == "approved"
    assert not store.exists(skill_file("skill-agent-round-zengen-v3-1-1"))
    head = store._git("rev-parse", "HEAD")
    _admit(store)
    assert store._git("rev-parse", "HEAD") == head


@pytest.mark.parametrize("increments,msg", [
    ([], "non-empty list"),
    ([{"kind": "recipe", "definition": {}}], "unknown kind"),
    ([{"kind": "skill"}], "malformed"),
    ([{"kind": "skill", "definition": dict(SKILL, level="huge")}], "level"),
])
def test_malformed_increments_write_one_failure_record_and_nothing_else(store, increments, msg):
    _put(store, increments=increments)
    _admit(store)
    fr = store.read_product(failure_record_file("run-003", f"admit-{PID}"))
    # 收录层面的确定性数据错误记 T2(设计 §11):处理器空操作、只记一次;T2/T3 只是留痕分类
    assert fr.payload["category"] == "T2" and fr.payload["point"] == "mindagent.admit"
    assert msg in fr.payload["reason"]
    assert store.read_product(proposal_file(PID)).status == "approved"
    assert not store.exists(library_state_file())
    head = store._git("rev-parse", "HEAD")
    _admit(store)
    assert store._git("rev-parse", "HEAD") == head


@pytest.mark.parametrize("run_id", ["a/b", "..", "", None, "run-ghost"])
def test_a_bad_or_missing_run_id_writes_no_failure_record_and_no_run_dir(store, run_id):
    _put(store, increments=[], run_id=run_id)
    _admit(store)  # 不抛
    runs = sorted(p.name for p in (store.repo_root / CONTROL_PLANE_ROOT / "runs").iterdir())
    assert runs == ["run-003"]  # 没有凭空新建 run 目录
    assert not store.exists(failure_record_file("run-003", f"admit-{PID}"))
    assert store.read_product(proposal_file(PID)).status == "approved"  # 原因留在原地,等人处理
    assert not store.exists(library_state_file())
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ma_admit.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindagent.admit'`,末行 `1 error in ...`。

- [ ] **Step 3: 写实现**

新建 `src/mindagent/failures.py`:

```python
"""失败记录的守卫与轮次产物的容错读取。两个处理器都经这里写失败记录,不直接调 record_failure。"""
from __future__ import annotations

import yaml

from controlplane.envelope import Envelope
from controlplane.gitstore import GitStore
from controlplane.ledger import record_failure
from controlplane.migration import UnknownSchemaVersionError
from controlplane.paths import failure_record_file, run_dir
from controlplane.validate import EnvelopeValidationError

# 产物"文件在、内容读不出"时的异常,与计划 8 的 _agent_round_status 同一组:YAML 解析失败
# (含合并冲突标记)、缺键、类型不对、信封校验失败、schema_version 无法迁移。
# OSError 不在其中,原样上抛(瞬态)。
UNREADABLE = (KeyError, TypeError, AttributeError, yaml.YAMLError,
              EnvelopeValidationError, UnknownSchemaVersionError)


def read_or_none(store: GitStore, rel: str) -> Envelope | None:
    """读一份产物;文件不存在或读不出(UNREADABLE)时返回 None。调用方用 store.exists 区分两者。"""
    if not store.exists(rel):
        return None
    try:
        return store.read_product(rel)
    except UNREADABLE:
        return None


def _single_segment(run_id: str) -> bool:
    return "/" not in run_id and "\\" not in run_id and run_id not in (".", "..")


def fail_once(
    store: GitStore, run_id: object, *, task_id: str, category: str, point: str, retries: int, reason: str,
) -> bool:
    """写一次失败记录,返回是否写了。与计划 3 的 _fail_once 同一守卫:run_id 不是非空的单个路径段、
    runs/<run>/ 目录不在(不凭空新建 run 目录)、或该 (run, task) 已记过,都什么都不写,原因只留在
    调用方自己的产物上。"""
    if not isinstance(run_id, str) or not run_id or not _single_segment(run_id):
        return False
    if not (store.repo_root / run_dir(run_id)).is_dir():
        return False
    if store.exists(failure_record_file(run_id, task_id)):
        return False
    record_failure(store, run_id, task_id=task_id, category=category, point=point,
                   retries=retries, reason=reason)
    return True
```

新建 `src/mindagent/admit.py`:

```python
"""收录处理器 admit_proposal(闭环集成设计 §3.2、§8.3):approved 的提案写进正式库。

顺序:库条目(skill_file / memory_file,effective)→ library_state(版本 +1、记来源提案)→ 提案 effective。
每一步重入安全:条目已在就不重写;library_state 已记过该提案就不再递增;所以崩在任何两步之间重跑都收敛。
"""
from __future__ import annotations

from datetime import datetime, timezone

from simple_actio.errors import SimpleActioDataError
from simple_actio.memory import MemoryEntry
from simple_actio.skills import Skill
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import library_state_file, memory_file, proposal_file, skill_file
from controlplane.reconcile import Action
from controlplane.states import assert_transition
from mindagent.errors import MindAgentDataError
from mindagent.failures import fail_once
from mindagent.library import LIBRARY_STATE_ID, memory_id_for, read_library_versions, skill_id_for


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entries(proposal_id: str, increments: object) -> list[tuple[str, str, dict]]:
    """[(kind, 条目 id, 条目载荷)]。任何一条不合法抛 ValueError(在写任何东西之前)。"""
    if not isinstance(increments, list) or not increments:
        raise ValueError("increments must be a non-empty list")
    out = []
    for k, inc in enumerate(increments, start=1):
        if not isinstance(inc, dict) or not isinstance(inc.get("definition"), dict):
            raise ValueError(f"increment {k} is malformed: {inc!r}")
        kind = inc.get("kind")
        definition = dict(inc["definition"])
        try:
            if kind == "skill":
                definition["id"] = skill_id_for(proposal_id, k)
                out.append(("skill", definition["id"], Skill.from_dict(definition).to_dict()))
            elif kind == "memory":
                definition["id"] = memory_id_for(proposal_id, k)
                out.append(("memory", definition["id"], MemoryEntry.from_dict(definition).to_dict()))
            else:
                raise ValueError(f"increment {k}: unknown kind {kind!r}")
        except SimpleActioDataError as exc:
            raise ValueError(f"increment {k}: {exc}") from None
    return out


def _entry_envelope(kind: str, entry_id: str, payload: dict, proposal_id: str) -> Envelope:
    return Envelope(
        id=entry_id, type=kind, version=1, created_at=_now(), created_by=OwnerRef(),
        status="effective", schema_version=1, links=[proposal_id], payload=payload,
    )


def handle_admit_proposal(store: GitStore, action: Action) -> None:
    proposal_id = action.target_id
    rel = proposal_file(proposal_id)
    proposal = store.read_product(rel)
    if proposal.status != "approved":
        return  # effective 已收录;其余状态不归本处理器
    task_id = f"admit-{proposal_id}"
    run_id = proposal.payload.get("run_id")
    try:
        entries = _entries(proposal_id, proposal.payload.get("increments"))
        # 库版本在写任何条目之前就校验:不是十进制数字串时无法递增,按确定性失败处理
        state = read_library_versions(store)
    except (ValueError, MindAgentDataError) as exc:
        # 审核类状态机里 approved 只能去 effective,无处可放"失败":记一次 T2(确定性数据错误,处理器
        # 空操作,设计 §11),提案原样留在 approved 等人处理。run_id 不是单个路径段或 run 目录不在时
        # 不写失败记录(fail_once 的守卫)。之后每个 tick 重入都只是读一遍、什么都不写。
        fail_once(store, run_id, task_id=task_id, category="T2",
                  point="mindagent.admit", retries=0, reason=str(exc))
        return

    for kind, entry_id, payload in entries:
        path = skill_file(entry_id) if kind == "skill" else memory_file(entry_id)
        if not store.exists(path):
            store.write_product(path, _entry_envelope(kind, entry_id, payload, proposal_id),
                                f"mindagent: admit {entry_id} from {proposal_id}")

    admitted = list(state["admitted"])
    if all(a.get("proposal_id") != proposal_id for a in admitted):
        kinds = {kind for kind, _, _ in entries}
        # 版本存成十进制字符串(设计 §8.3);递增时按整数解析
        skill_v = str(int(state["skill_version"]) + (1 if "skill" in kinds else 0))
        memory_v = str(int(state["memory_version"]) + (1 if "memory" in kinds else 0))
        admitted.append({
            "proposal_id": proposal_id,
            "skill_version": skill_v,
            "memory_version": memory_v,
            "entries": [entry_id for _, entry_id, _ in entries],
        })
        lib_rel = library_state_file()
        version = store.read_product(lib_rel).version + 1 if store.exists(lib_rel) else 1
        env = Envelope(
            id=LIBRARY_STATE_ID, type="library_state", version=version, created_at=_now(),
            created_by=OwnerRef(), status="executed", schema_version=1, links=[proposal_id],
            payload={"skill_version": skill_v, "memory_version": memory_v, "admitted": admitted},
        )
        store.write_product(lib_rel, env, f"mindagent: library -> skill v{skill_v}, memory v{memory_v}")

    assert_transition("review", proposal.status, "effective")
    proposal.status = "effective"
    store.write_product(rel, proposal, f"mindagent: {proposal_id} effective")
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ma_admit.py`
Expected: `22 passed`,无警告。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+123 passed`(B 为开工时的基线),无警告。再跑 `git status --short`:除开工前就在的 ` M cli/package-lock.json`、` M cli/package.json` 外,只应有 `src/mindagent/failures.py`、`src/mindagent/admit.py` 与 `tests/test_ma_admit.py`。

- [ ] **Step 6: 提交**

```bash
git add src/mindagent/failures.py
git add src/mindagent/admit.py
git add tests/test_ma_admit.py
git commit -m "feat: mindagent admit_proposal handler (approved increments into skill_library and memory_library, library versions bumped once, crash-safe at every step)"
```

---

### Task 87: 轮次处理器 `make_start_round_handler`

**Files:**
- Create: `src/mindagent/rounds.py`(294 行)
- Test: `tests/test_ma_rounds.py`

**Interfaces:**
- Consumes: 任务 80 至 85 的全部产出;`simple_actio.instance.instantiate`;`controlplane.paths`(`agent_round_file`、`model_file`、`scenario_file`、`persona_file`、`CONTROL_PLANE_ROOT`);`controlplane.ledger.record_failure`;`controlplane.states.assert_transition`;`inference_bridge.BridgeError`;`simulator.store_adapter.read_scenario`。
- Produces(计划 8 / 9 把返回值注册为 `start_agent_round` 的处理器):
  - `make_start_round_handler(*, config: AgentConfig, client_factory, backend_factory, personas_loader, bridge=None) -> Callable[[GitStore, Action], None]`;`action.target_id` 是模型 id(`zengen-vN`)。工厂形式见 Ruling 19;名字与位置是计划 9 的契约,不改。
  - `backend_factory(model_id: str, checkpoint_ref: BigRef | None) -> 模型后端`;`personas_loader(store, persona_ids: list[str]) -> dict[str, Persona]`。
  - `supersede_stale_rounds(store, latest_version) -> list[str]`;`round_id_for(model_id)`;`task_id_for(model_id) -> "agent-<model>"`;`ROUND_TYPE = "agent_round"`。

**设计要点:**
- 流程、终态与失败分类见 Ruling 13;轮次产物的载荷见 Ruling 14。
- 第一轮空库:轮次 `executed`、`findings`、`proposals`、`dropped` 全空、`failure` 为 `None`、没有失败记录、没有提案目录(`test_the_first_round_with_an_empty_library_is_silent_by_design`)。
- 重入:`executed` 空操作(不新建客户端、不提交);`pending` 时已落盘的部署 episode 不再新建会话;崩在打包之后、轮次置 `executed` 之前,重跑仍是同一个提案 id、只有一份。
- 读不出的轮次产物(Ruling 20):合并冲突标记与信封校验失败各一条——处理器正常返回,轮次整份重写为 `failed`(`unreadable round product`,T3),失败记录恰好一份,不建任何会话;再跑一次没有提交。`supersede_stale_rounds` 跳过两种坏文件(各一条),别的模型的轮次照常执行。读轮次产物时的 `OSError` 原样上抛。
- 坏 run id(Ruling 21):模型的 `links[1]` 为 `"a/b"` 或不存在的 run 时,轮次 `failed` 且原因留在产物上,不抛、不新建 run 目录。测试夹具先建好 `runs/run-001/`(真实树里晋级模型的 run 必然存在)。
- 故障注入覆盖:会话 T1 重跑用尽(上抛、`pending`,随后再调一次处理器即重入成功,这正是 R19 下调和循环下一个 tick 会做的事)、人格加载器抛普通异常(上抛、`pending`)、桥接 T1 与 T2(R18:都 `failed` + T3 记录,`retries = attempts - 1`)、解析耗尽(`failed`,`retries` 为已重试次数)、会话被拒(`failed`)、情境缺失(`failed`,不建任何会话)、库条目不合法(`failed`)、未知模型(`failed`,无失败记录)。
- 取代:新模型晋级后,旧模型 `pending` 的轮次被置 `failed`(superseded);旧模型的过期动作不跑。两种情形都断言没有失败记录。
- 过期提案:重入时编号超出本次数目、仍为 `pending_review` 的旧提案写进 `dropped`,原样留在树里(Ruling 12)。

- [ ] **Step 0: 前置检查(计划 1、计划 6 已合并)**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev python -c "import inspect; from simple_actio.instance import instantiate; from simple_actio.skills import Skill, SKILL_ID_PREFIX; from simple_actio.memory import MemoryEntry, MEMORY_ID_PREFIX; from simple_actio.trace import validate_trace, normalize_library_version; from simple_actio.triggers import referenced_signals; from simple_actio.signals import default_registry; from inference_bridge import BridgeError, ScriptedBridge; from controlplane.paths import agent_round_file, proposal_file, skill_file, memory_file, library_state_file, failure_record_file; from controlplane.ledger import record_failure; from simulator.realtime.client import EnvironmentServiceClient; assert 'activation_trace' in inspect.signature(EnvironmentServiceClient.submit_turn).parameters; assert SKILL_ID_PREFIX == 'skill-' and MEMORY_ID_PREFIX == 'mem-'; print('prereqs ok')"
```

Expected: 最后一行 `prereqs ok`。任何 `ImportError`、`AttributeError` 或 `AssertionError` 都说明计划 1 或计划 6 没有按其文本合并:**停下**,把报错原样报告,不要往下做。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_ma_rounds.py`:

```python
import json

import grpc
import pytest

from simple_actio.skills import Skill
from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import (
    CONTROL_PLANE_ROOT, run_dir, agent_round_file, episode_file, episodes_dir, failure_record_file, model_file, proposal_file, skill_file,
)
from controlplane.reconcile import Action
from inference_bridge import BridgeError, ScriptedBridge
from mindagent.config import AgentConfig
from mindagent.rounds import make_start_round_handler, round_id_for, supersede_stale_rounds, task_id_for
from simulator.datamodel import Episode, Persona, RoleBinding, RoleSlot, Scenario, TurnRecord
from simulator.realtime.client import SessionRejectedError
from simulator.store_adapter import read_persona, write_persona, write_scenario

AGENT = RoleBinding("a", "pa", "self_play")
OPP = RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")
ANGRY = "我真的很生气!!"
GOAL = {"id": "skill-goal", "version": 1, "level": "macro", "priority": 0, "trigger": {"turn_at_least": 0},
        "policy": {"instructions": ["推进目标"]}, "description": ""}
CKPT = BigRef("file:///ckpt/v1", "c" * 64, 1)


class Crash(Exception):
    pass


class Echo:
    def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
        return ("|".join(guidance) or "plain", None)


class _Unavailable(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

    def details(self):
        return "reset"


class _FakeClient:
    """对方先说(每句来自 lines),agent 后说;max_turns=4。"""

    def __init__(self, lines, *, create_error=None, end_reason="max_turns"):
        self._lines = list(lines)
        self._create_error = create_error
        self._end_reason = end_reason
        self._turns = []
        self.closed = False
        self.created = None

    @property
    def turns(self):
        return list(self._turns)

    def create_session(self, *, scenario_id, role_assignment, personas, purpose, client_driven_slot):
        self.created = dict(scenario_id=scenario_id, role_assignment=role_assignment)
        if self._create_error is not None:
            self.closed = True
            raise self._create_error
        self._turns.append(TurnRecord(0, "b", self._lines[0]))
        return "sess"

    def submit_turn(self, utterance, inner_thought=None, *, activation_trace=None):
        self._turns.append(TurnRecord(len(self._turns), "a", utterance, inner_thought, activation_trace))
        if len(self._turns) < 4:
            self._turns.append(TurnRecord(len(self._turns), "b", self._lines[len(self._turns) // 2]))
        if len(self._turns) >= 4:
            self.closed = True
        return []

    def drain_until_end(self):
        return Episode("ep-sess", self.created["scenario_id"], self.created["role_assignment"],
                       self.turns, self._end_reason)

    def abort(self):
        self.closed = True


class _Factory:
    def __init__(self, lines=(ANGRY, "好吧"), plans=()):
        self.lines = lines
        self.plans = list(plans)
        self.count = 0

    def __call__(self):
        self.count += 1
        return _FakeClient(self.lines, **(self.plans.pop(0) if self.plans else {}))


def _env(id_, type_, status, payload, links=()):
    return Envelope(id=id_, type=type_, version=1, created_at="2026-10-09T00:00:00Z",
                    created_by=OwnerRef(), status=status, schema_version=1, links=list(links), payload=payload)


def _model(store, n, run_id, status="effective"):
    store.write_product(model_file(n), _env(f"zengen-v{n}", "zengen_model", status,
                        {"promoted_from": f"cand-{run_id}", "checkpoint_ref": CKPT.to_dict()},
                        [f"cand-{run_id}", run_id]), "test: model")


def _skill(store, d):
    store.write_product(skill_file(d["id"]), _env(d["id"], "skill", "effective", d), "test: skill")


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    for sid in ("s1", "s2", "s3"):
        write_scenario(s, Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"],
                                   "deployment_simulation"))
    for pid in ("pa", "pb"):
        write_persona(s, Persona(pid, ["t"], "m", "s", ["v"]))
    _model(s, 1, "run-001")
    (s.repo_root / run_dir("run-001")).mkdir(parents=True)  # 产生绑定模型的 run(晋级时必然存在)
    return s


def _cfg(**kw):
    base = dict(scenario_ids=("s1", "s2", "s3"), bindings=(OPP, AGENT), max_turns=4,
                min_support=3, min_ratio=0.5)
    base.update(kw)
    return AgentConfig(**base)


def _loader(store, ids):
    return {pid: read_persona(store, pid) for pid in ids}


def _handler(factory=None, bridge=None, backends=None, **cfg):
    seen = backends if backends is not None else []

    def backend_factory(model_id, ckpt):
        seen.append((model_id, ckpt))
        return Echo()

    return make_start_round_handler(config=_cfg(**cfg), client_factory=factory or _Factory(),
                                    backend_factory=backend_factory, personas_loader=_loader, bridge=bridge)


def _start(store, handler, model="zengen-v1"):
    handler(store, Action(kind="start_agent_round", target_id=model, detail={}))
    return store.read_product(agent_round_file(model))


def test_the_first_round_with_an_empty_library_is_silent_by_design(store):
    backends = []
    env = _start(store, _handler(backends=backends))
    assert (env.id, env.type, env.status) == ("agent-round-zengen-v1", "agent_round", "executed")
    p = env.payload
    assert p["model_id"] == "zengen-v1" and p["run_id"] == "run-001" and p["model_version"] == 1
    assert p["checkpoint_ref"] == CKPT.to_dict()
    assert p["library_version"] == {"skill": "0", "memory": "0"}
    assert p["episode_ids"] == ["deploy-zengen-v1-s1-1", "deploy-zengen-v1-s2-1", "deploy-zengen-v1-s3-1"]
    assert (p["findings"], p["proposals"], p["dropped"], p["failure"]) == ([], [], [], None)
    assert p["generator_version"] == "rule_template.v1" and "prompt_template_version" not in p
    assert p["uses_bridge"] is False
    assert p["scenario_ids"] == ["s1", "s2", "s3"] and p["persona_ids"] == ["pb", "pa"]
    assert p["analyzer_config"] == {"min_support": 3, "min_ratio": 0.5}
    assert env.links == ["zengen-v1", "run-001"]
    assert not store.exists(failure_record_file("run-001", task_id_for("zengen-v1")))
    assert list((store.repo_root / f"{CONTROL_PLANE_ROOT}/skill_memory_proposals").iterdir()) == []
    assert backends and all(b == ("zengen-v1", CKPT) for b in backends)
    for eid in p["episode_ids"]:
        assert store.exists(episode_file("run-001", eid))


def test_a_library_that_ignores_emotion_produces_a_pending_review_proposal(store):
    _skill(store, GOAL)
    env = _start(store, _handler())
    p = env.payload
    assert env.status == "executed"
    (finding,) = p["findings"]
    assert finding["signal"] == "emotion_high" and finding["support"] == 3 and finding["ratio"] == 1.0
    assert p["proposals"] == ["proposal-agent-round-zengen-v1-1"]
    prop = store.read_product(proposal_file("proposal-agent-round-zengen-v1-1"))
    assert prop.status == "pending_review"
    assert prop.payload["verification"]["fired_on_motivating"] == 1.0
    assert prop.payload["verification"]["fired_elsewhere"] == 0.0
    assert prop.payload["finding"]["episode_refs"] == p["episode_ids"]


def test_dropped_drafts_are_recorded_in_the_round(store):
    strong = dict(GOAL, id="skill-top", priority=1000)
    _skill(store, strong)
    env = _start(store, _handler())
    assert env.status == "executed"
    assert env.payload["proposals"] == []
    (dropped,) = env.payload["dropped"]
    assert dropped["fired_on_motivating"] == 0.0
    assert dropped["reason"] == "fired_on_motivating 0.000 < 0.8"


def test_with_a_bridge_the_round_records_the_prompt_version_and_the_cost(store):
    _skill(store, GOAL)
    good = json.dumps({"instructions": ["先安抚"], "description": "d"})
    bridge = ScriptedBridge([good], store=store)
    env = _start(store, _handler(bridge=bridge))
    assert env.payload["generator_version"] == "bridge.v1"
    assert env.payload["prompt_template_version"] == "mindagent-skill-v1"
    assert env.payload["uses_bridge"] is True
    assert bridge.calls[0][1:] == ("run-001", "agent-zengen-v1-propose-1-attempt1")


def test_an_executed_round_is_a_noop(store):
    h = _handler()
    _start(store, h)
    head = store._git("rev-parse", "HEAD")
    factory = _Factory()
    _start(store, _handler(factory=factory))
    assert store._git("rev-parse", "HEAD") == head and factory.count == 0


def test_a_pending_round_resumes_without_regenerating_persisted_episodes(store, monkeypatch):
    _skill(store, GOAL)
    real = store.write_product

    def crash_on_proposal(rel, env, message):
        if "skill_memory_proposals" in rel:
            raise Crash(rel)
        real(rel, env, message)

    monkeypatch.setattr(store, "write_product", crash_on_proposal)
    with pytest.raises(Crash):
        _start(store, _handler())
    assert store.read_product(agent_round_file("zengen-v1")).status == "pending"
    assert len(store.list_products(episodes_dir("run-001"))) == 3
    monkeypatch.setattr(store, "write_product", real)
    factory = _Factory()
    env = _start(store, _handler(factory=factory))
    assert factory.count == 0
    assert env.status == "executed" and env.payload["proposals"] == ["proposal-agent-round-zengen-v1-1"]


def test_a_crash_after_packaging_reruns_into_the_same_proposal_id(store, monkeypatch):
    _skill(store, GOAL)
    real = store.write_product
    calls = {"n": 0}

    def crash_on_executed(rel, env, message):
        if rel == agent_round_file("zengen-v1") and env.status == "executed":
            raise Crash(rel)
        real(rel, env, message)

    monkeypatch.setattr(store, "write_product", crash_on_executed)
    with pytest.raises(Crash):
        _start(store, _handler())
    monkeypatch.setattr(store, "write_product", real)
    env = _start(store, _handler())
    assert env.payload["proposals"] == ["proposal-agent-round-zengen-v1-1"]
    assert len(list((store.repo_root / f"{CONTROL_PLANE_ROOT}/skill_memory_proposals").iterdir())) == 1


def test_a_transient_failure_propagates_and_leaves_the_round_pending(store):
    factory = _Factory(plans=[{"create_error": _Unavailable()}] * 2)
    with pytest.raises(Exception) as exc:
        _start(store, _handler(factory=factory, max_retries_per_episode=1))
    assert type(exc.value).__name__ == "DeploymentFailedError" and exc.value.transient
    assert store.read_product(agent_round_file("zengen-v1")).status == "pending"
    assert not store.exists(failure_record_file("run-001", "agent-zengen-v1"))
    env = _start(store, _handler())
    assert env.status == "executed"


def test_an_unexpected_exception_propagates_and_leaves_the_round_pending(store):
    def loader(store, ids):
        raise Crash("disk")

    h = make_start_round_handler(config=_cfg(), client_factory=_Factory(),
                                 backend_factory=lambda m, c: Echo(), personas_loader=loader)
    with pytest.raises(Crash):
        _start(store, h)
    assert store.read_product(agent_round_file("zengen-v1")).status == "pending"


def test_an_exhausted_bridge_t1_fails_the_round_r18(store):
    _skill(store, GOAL)
    _start(store, _handler(bridge=ScriptedBridge([BridgeError("T1", "timeout", attempts=3)])))
    fr = _assert_failed(store, "mindagent.proposals", "bridge T1: timeout")
    assert fr.payload["retries"] == 2  # 已重试次数 = attempts - 1


def _assert_failed(store, point, fragment):
    env = store.read_product(agent_round_file("zengen-v1"))
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T3" and env.payload["failure"]["point"] == point
    assert fragment in env.payload["failure"]["reason"]
    fr = store.read_product(failure_record_file("run-001", "agent-zengen-v1"))
    assert (fr.payload["category"], fr.payload["point"]) == ("T3", point)
    assert fragment in fr.payload["reason"]
    return fr


def test_a_bridge_t2_fails_the_round(store):
    _skill(store, GOAL)
    _start(store, _handler(bridge=ScriptedBridge([BridgeError("T2", "no such tool")])))
    _assert_failed(store, "mindagent.proposals", "bridge T2: no such tool")


def test_unparseable_bridge_output_fails_the_round_after_bounded_retries(store):
    _skill(store, GOAL)
    _start(store, _handler(bridge=ScriptedBridge(["x", "y"]), max_parse_retries=1))
    fr = _assert_failed(store, "mindagent.proposals", "not a single JSON object")
    assert fr.payload["retries"] == 1


def test_a_rejected_session_fails_the_round(store):
    factory = _Factory(plans=[{"create_error": SessionRejectedError("invalid_session", "bad persona")}])
    _start(store, _handler(factory=factory))
    _assert_failed(store, "mindagent.deployment", "invalid_session")


def test_a_missing_scenario_fails_the_round_before_any_session(store):
    factory = _Factory()
    _start(store, _handler(factory=factory, scenario_ids=("s1", "ghost")))
    _assert_failed(store, "mindagent.rounds.inputs", "scenario 'ghost'")
    assert factory.count == 0


def test_a_malformed_library_entry_fails_the_round(store):
    _skill(store, dict(GOAL, level="huge"))
    _start(store, _handler())
    _assert_failed(store, "mindagent.rounds", "SimpleActioDataError")


def test_a_failed_round_is_terminal(store):
    _start(store, _handler(scenario_ids=("ghost",)))
    head = store._git("rev-parse", "HEAD")
    _start(store, _handler())
    assert store._git("rev-parse", "HEAD") == head


def test_an_unknown_model_fails_the_round_without_a_failure_record(store):
    _start(store, _handler(), model="zengen-v9")
    env = store.read_product(agent_round_file("zengen-v9"))
    assert env.status == "failed" and "not found" in env.payload["failure"]["reason"]


def test_a_newer_model_supersedes_a_pending_round_and_an_old_action_is_refused(store, monkeypatch):
    real = store.write_product

    def crash(rel, env, message):
        if "episodes/" in rel:
            raise Crash(rel)
        real(rel, env, message)

    monkeypatch.setattr(store, "write_product", crash)
    with pytest.raises(Crash):
        _start(store, _handler())
    monkeypatch.setattr(store, "write_product", real)
    _model(store, 2, "run-002")
    env2 = _start(store, _handler(), model="zengen-v2")
    assert env2.status == "executed" and env2.payload["run_id"] == "run-002"
    old = store.read_product(agent_round_file("zengen-v1"))
    assert old.status == "failed"
    assert old.payload["failure"] == {"category": "superseded", "reason": "superseded by zengen-v2",
                                      "point": "mindagent.rounds.supersede"}
    assert env2.payload["episode_ids"][0] == "deploy-zengen-v2-s1-1"
    assert store.exists(episode_file("run-002", "deploy-zengen-v2-s1-1"))
    assert not store.exists(failure_record_file("run-001", "agent-zengen-v1"))


def test_a_stale_action_for_an_old_model_is_superseded_not_run(store):
    _model(store, 2, "run-002")
    factory = _Factory()
    _start(store, _handler(factory=factory))
    env = store.read_product(agent_round_file("zengen-v1"))
    assert env.status == "failed" and env.payload["failure"]["category"] == "superseded"
    assert factory.count == 0
    assert not store.exists(failure_record_file("run-001", "agent-zengen-v1"))


def test_a_stale_pending_proposal_from_an_earlier_run_is_reported_in_dropped(store, monkeypatch):
    _skill(store, GOAL)
    real = store.write_product

    def crash_on_executed(rel, env, message):
        if rel == agent_round_file("zengen-v1") and env.status == "executed":
            raise Crash(rel)
        real(rel, env, message)

    monkeypatch.setattr(store, "write_product", crash_on_executed)
    with pytest.raises(Crash):
        _start(store, _handler())
    monkeypatch.setattr(store, "write_product", real)
    # 模拟"上一次运行多产出了一条":手工复制出 -2
    first = store.read_product(proposal_file("proposal-agent-round-zengen-v1-1"))
    first.id = "proposal-agent-round-zengen-v1-2"
    store.write_product(proposal_file(first.id), first, "test: leftover")
    env = _start(store, _handler())
    assert env.payload["proposals"] == ["proposal-agent-round-zengen-v1-1"]
    assert env.payload["dropped"] == [{
        "proposal_id": "proposal-agent-round-zengen-v1-2",
        "reason": "stale: left by an earlier run of this round, not produced again; do not approve",
    }]
    assert store.read_product(proposal_file(first.id)).status == "pending_review"  # 不删不改


def test_supersede_only_touches_pending_rounds_of_older_models(store):
    for n, status in ((1, "pending"), (2, "executed"), (3, "pending")):
        store.write_product(agent_round_file(f"zengen-v{n}"), _env(round_id_for(f"zengen-v{n}"), "agent_round",
                            status, {"model_id": f"zengen-v{n}", "run_id": "", "failure": None}), "t")
    assert supersede_stale_rounds(store, 3) == ["agent-round-zengen-v1"]
    assert [store.read_product(agent_round_file(f"zengen-v{n}")).status for n in (1, 2, 3)] == [
        "failed", "executed", "pending"]


CONFLICTED = "<<<<<<< HEAD\nid: agent-round-zengen-v1\n=======\nid: other\n>>>>>>> branch\n"


def _corrupt_round(store, text=CONFLICTED, model="zengen-v1"):
    path = store.repo_root / agent_round_file(model)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("text", [
    CONFLICTED,                                              # 合并冲突标记:YAML 解析失败
    "id: agent-round-zengen-v1\ntype: agent_round\n",       # 缺必填键:EnvelopeValidationError
], ids=["merge_conflict", "envelope_invalid"])
def test_an_unreadable_round_product_is_rewritten_failed_once_without_raising(store, text):
    _corrupt_round(store, text)
    factory = _Factory()
    _start(store, _handler(factory=factory))  # 不抛:守护进程不会崩溃循环
    env = store.read_product(agent_round_file("zengen-v1"))
    assert env.status == "failed" and env.id == "agent-round-zengen-v1"
    assert env.payload["failure"] == {"category": "T3", "reason": "unreadable round product",
                                      "point": "mindagent.rounds.read"}
    fr = store.read_product(failure_record_file("run-001", "agent-zengen-v1"))
    assert (fr.payload["category"], fr.payload["reason"]) == ("T3", "unreadable round product")
    assert factory.count == 0
    head = store._git("rev-parse", "HEAD")
    _start(store, _handler(factory=factory))  # 再来一次 tick:终态,没有动作
    assert store._git("rev-parse", "HEAD") == head
    assert len([p for p in (store.repo_root / run_dir("run-001")).iterdir() if p.name.startswith("failure-")]) == 1


@pytest.mark.parametrize("text", [CONFLICTED, "id: agent-round-zengen-v1\ntype: agent_round\n"],
                         ids=["merge_conflict", "envelope_invalid"])
def test_supersede_skips_an_unreadable_round(store, text):
    _corrupt_round(store, text)
    assert supersede_stale_rounds(store, 5) == []
    assert (store.repo_root / agent_round_file("zengen-v1")).read_text(encoding="utf-8") == text
    _model(store, 2, "run-002")
    (store.repo_root / run_dir("run-002")).mkdir(parents=True)
    env = _start(store, _handler(), model="zengen-v2")  # 别的模型的轮次不受这份坏文件影响
    assert env.status == "executed"


def test_an_os_error_reading_the_round_still_propagates(store, monkeypatch):
    _start(store, _handler(scenario_ids=("ghost",)))  # 先有一份轮次产物
    real = store.read_product

    def flaky(rel):
        if rel == agent_round_file("zengen-v1"):
            raise OSError("disk")
        return real(rel)

    monkeypatch.setattr(store, "read_product", flaky)
    with pytest.raises(OSError):
        _start(store, _handler())


@pytest.mark.parametrize("bad_run", ["a/b", "run-ghost"])
def test_a_bad_or_missing_run_id_fails_the_round_without_a_failure_record_or_run_dir(store, bad_run):
    store.write_product(model_file(1), _env("zengen-v1", "zengen_model", "effective",
                        {"promoted_from": "cand", "checkpoint_ref": CKPT.to_dict()}, ["cand", bad_run]), "t")
    _start(store, _handler(scenario_ids=("ghost",)))  # 不抛
    env = store.read_product(agent_round_file("zengen-v1"))
    assert env.status == "failed" and "scenario 'ghost'" in env.payload["failure"]["reason"]
    runs = sorted(p.name for p in (store.repo_root / CONTROL_PLANE_ROOT / "runs").iterdir())
    assert runs == ["run-001"]
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ma_rounds.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindagent.rounds'`,末行 `1 error in ...`。

- [ ] **Step 3: 写实现**

新建 `src/mindagent/rounds.py`:

```python
"""轮次处理器 start_agent_round(闭环集成设计 §3.2、§8.3)。

流程:读绑定模型 → 把被取代的 pending 旧轮次置 failed → 读当前正式库 → 写 agent_round(pending)
→ 部署 episode → 分析 → 提案 → 验证 → 打包 → 轮次置 executed。

失败分类(与派发器同一约定):
- 确定性错误(配置、树里的数据、解析耗尽、不可重跑的会话失败)与任何 BridgeError(R18:T1 是
  桥接已用尽自己的重试,T2 是确定性故障)→ 轮次 failed,并经 ledger.record_failure 写一条 T3
  失败记录(task_id 为 agent-<model>)。
- 非桥接的瞬态(会话 T1 重跑用尽)与其余异常原样上抛,轮次保持 pending。按 R19,调和循环对
  pending 的轮次在下一个 tick 重新触发 start_agent_round,所以本处理器的重入必须幂等:已落盘的
  部署 episode 不重跑,提案按确定性 id 重写。
"""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone

from simple_actio.errors import SimpleActioDataError
from simple_actio.instance import instantiate
from simple_actio.skills import Skill
from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT, agent_round_file, model_file, persona_file, scenario_file
from controlplane.reconcile import Action
from controlplane.states import assert_transition
from inference_bridge import BridgeError
from mindagent.analysis import analyze
from mindagent.config import AgentConfig
from mindagent.deployment import DeploymentFailedError, run_deployment_episodes
from mindagent.errors import AgentRoundFailedError, MindAgentDataError, ProposalParseError
from mindagent.failures import UNREADABLE, fail_once, read_or_none
from mindagent.library import LibraryState, load_library
from mindagent.packaging import Thresholds, package, screen, stale_proposals
from mindagent.proposals import BRIDGE_GENERATOR, PROMPT_TEMPLATE_VERSION, RULE_GENERATOR, propose
from mindagent.verify import verify
from simulator.datamodel import Persona
from simulator.store_adapter import read_scenario

ROUND_TYPE = "agent_round"
_MODEL_ID = re.compile(r"^zengen-v(\d+)$")
_ROUND_FILE = re.compile(r"agent-round-(zengen-v(\d+))\.yaml$")

BackendFactory = Callable[[str, "BigRef | None"], object]
PersonasLoader = Callable[[GitStore, list[str]], dict[str, Persona]]
Handler = Callable[[GitStore, Action], None]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def round_id_for(model_id: str) -> str:
    return f"agent-round-{model_id}"


def task_id_for(model_id: str) -> str:
    return f"agent-{model_id}"


def _model_version(model_id: str) -> int:
    match = _MODEL_ID.match(model_id)
    if match is None:
        raise AgentRoundFailedError(f"not a zengen model id: {model_id!r}", point="mindagent.rounds.model")
    return int(match.group(1))


def _read_model(store: GitStore, model_id: str) -> tuple[int, str, BigRef | None]:
    """(版本, 产生它的 run, checkpoint_ref)。promote_candidate 写的 links 是 [候选 id, run id]。"""
    version = _model_version(model_id)
    rel = model_file(version)
    if not store.exists(rel):
        raise AgentRoundFailedError(f"model {model_id} not found at {rel}", point="mindagent.rounds.model")
    env = store.read_product(rel)
    if env.status != "effective":
        raise AgentRoundFailedError(f"model {model_id} is {env.status}, not effective", point="mindagent.rounds.model")
    if len(env.links) < 2 or not env.links[1]:
        raise AgentRoundFailedError(f"model {model_id} has no run in its links", point="mindagent.rounds.model")
    raw = env.payload.get("checkpoint_ref")
    return version, env.links[1], BigRef.from_dict(raw) if raw is not None else None


def _latest_effective_version(store: GitStore) -> int:
    best = 0
    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/models"):
        match = re.search(r"zengen-v(\d+)\.yaml$", rel)
        if match and store.read_product(rel).status == "effective":
            best = max(best, int(match.group(1)))
    return best


def _fail_round(store: GitStore, env: Envelope, reason: str, *, category: str, point: str) -> None:
    assert_transition("nonreview", env.status, "failed")
    env.status = "failed"
    env.payload["failure"] = {"category": category, "reason": reason, "point": point}
    store.write_product(agent_round_file(env.payload["model_id"]), env, f"mindagent: {env.id} failed ({category})")


def supersede_stale_rounds(store: GitStore, latest_version: int) -> list[str]:
    """把绑定模型版本低于 latest_version、仍为 pending 的轮次置 failed(superseded)。返回被置的轮次 id。"""
    out: list[str] = []
    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/agent_rounds"):
        match = _ROUND_FILE.search(rel)
        if match is None or int(match.group(2)) >= latest_version:
            continue
        env = read_or_none(store, rel)
        if env is None:
            continue  # 读不出:留给它自己那个模型的 start_agent_round 判损坏(计划 8 视它为可触发)
        if env.status == "pending":
            _fail_round(store, env, f"superseded by zengen-v{latest_version}",
                        category="superseded", point="mindagent.rounds.supersede")
            out.append(env.id)
    return out


def _existing_or_new(store: GitStore, model_id: str, run_id: str) -> Envelope:
    """已有的轮次产物;不存在或读不出时新建一份最小的 pending 产物(随后由调用方置 failed)。"""
    env = read_or_none(store, agent_round_file(model_id))
    return env if env is not None else _round_envelope(
        model_id, {"model_id": model_id, "run_id": run_id, "failure": None})


def _round_envelope(model_id: str, payload: dict) -> Envelope:
    return Envelope(
        id=round_id_for(model_id), type=ROUND_TYPE, version=1, created_at=_now(),
        created_by=OwnerRef(), status="pending", schema_version=1,
        links=[model_id, payload["run_id"]], payload=payload,
    )


def _binding_payload(model_id: str, version: int, run_id: str, ckpt: BigRef | None,
                     lib: LibraryState, config: AgentConfig, generator: str) -> dict:
    payload = {
        "model_id": model_id,
        "model_version": version,
        "run_id": run_id,
        "library_version": dict(lib.version),
        "scenario_ids": list(config.scenario_ids),
        "persona_ids": [b.persona_id for b in config.bindings],
        "bindings": [b.to_dict() for b in config.bindings],
        "analyzer_config": {"min_support": config.min_support, "min_ratio": config.min_ratio},
        "verifier_config": {
            "method": "replay_behavior",
            "min_fired_on_motivating": config.min_fired_on_motivating,
            "max_fired_elsewhere": config.max_fired_elsewhere,
        },
        "generator_version": generator,
        "uses_bridge": generator == BRIDGE_GENERATOR,
        "episode_ids": [],
        "findings": [],
        "proposals": [],
        "dropped": [],
        "failure": None,
    }
    if ckpt is not None:
        payload["checkpoint_ref"] = ckpt.to_dict()
    if generator == BRIDGE_GENERATOR:
        payload["prompt_template_version"] = PROMPT_TEMPLATE_VERSION
    return payload


def _check_inputs(store: GitStore, config: AgentConfig) -> None:
    for sid in config.scenario_ids:
        if not store.exists(scenario_file(sid)):
            raise AgentRoundFailedError(f"scenario {sid!r} not in the tree", point="mindagent.rounds.inputs")
    for b in config.bindings:
        if not store.exists(persona_file(b.persona_id)):
            raise AgentRoundFailedError(f"persona {b.persona_id!r} not in the tree", point="mindagent.rounds.inputs")


def make_start_round_handler(
    *,
    config: AgentConfig,
    client_factory: Callable[[], object],
    backend_factory: BackendFactory,
    personas_loader: PersonasLoader,
    bridge=None,
) -> Handler:
    """backend_factory(model_id, checkpoint_ref) 每次调用返回一个新的模型后端(普通或带指导语的);
    personas_loader(store, persona_ids) 返回 {persona_id: Persona}。bridge 缺省走规则模板。"""
    generator = RULE_GENERATOR if bridge is None else BRIDGE_GENERATOR

    def handle(store: GitStore, action: Action) -> None:
        model_id = action.target_id
        rel = agent_round_file(model_id)
        existing = read_or_none(store, rel)
        if existing is None and store.exists(rel):
            _fail_unreadable(store, model_id)
            return
        if existing is not None and existing.status != "pending":
            return  # executed 或 failed:终态,空操作
        latest = _latest_effective_version(store)
        supersede_stale_rounds(store, latest)

        try:
            version, run_id, ckpt = _read_model(store, model_id)
        except AgentRoundFailedError as exc:
            _fail_round(store, _existing_or_new(store, model_id, ""), exc.reason, category="T3", point=exc.point)
            return  # 没有 run 可挂失败记录;原因写在轮次产物里
        if version < latest:
            _fail_round(store, _existing_or_new(store, model_id, run_id), f"superseded by zengen-v{latest}",
                        category="superseded", point="mindagent.rounds.supersede")
            return

        lib: LibraryState | None = None
        try:
            lib = load_library(store)
            payload = _binding_payload(model_id, version, run_id, ckpt, lib, config, generator)
            env = _round_envelope(model_id, payload)
            if existing is not None:
                env.created_at = existing.created_at
            store.write_product(rel, env, f"mindagent: {env.id} pending")
            _check_inputs(store, config)
            _run(store, env, lib, run_id, model_id, ckpt)
        except DeploymentFailedError as exc:
            if exc.transient:
                raise
            _record(store, rel, run_id, model_id, f"deployment: {exc}", "mindagent.deployment", exc.attempts - 1)
        except ProposalParseError as exc:
            _record(store, rel, run_id, model_id, str(exc), "mindagent.proposals", exc.attempts - 1)
        except BridgeError as exc:
            # R18:桥接已经自己退避重试过,T1 也升级为 T3,不留 pending 让守护循环无限空转外部推理
            # retries 是已重试次数(计划 1):attempts 是总尝试次数
            _record(store, rel, run_id, model_id, f"bridge {exc.category}: {exc.reason}",
                    "mindagent.proposals", max(exc.attempts - 1, 0))
        except AgentRoundFailedError as exc:
            _record(store, rel, run_id, model_id, exc.reason, exc.point, 0)
        except (MindAgentDataError, SimpleActioDataError) as exc:
            _record(store, rel, run_id, model_id, f"{type(exc).__name__}: {exc}", "mindagent.rounds", 0)

    def _run(store: GitStore, env: Envelope, lib: LibraryState, run_id: str, model_id: str,
             ckpt: BigRef | None) -> None:
        def instance_with(extra: tuple[Skill, ...] = ()):
            return instantiate(
                backend_factory(model_id, ckpt), skills=lib.skills + tuple(extra), memories=lib.memories,
                library_version=lib.version, profile={"model_id": model_id, "round_id": env.id},
            )

        persona_ids = sorted({b.persona_id for b in config.bindings})
        personas = personas_loader(store, persona_ids)
        episodes = run_deployment_episodes(
            store, instance_with(), client_factory, run_id=run_id,
            scenario_ids=config.scenario_ids, personas=personas, bindings=config.bindings,
            model_id=model_id, max_turns=config.max_turns,
            episodes_per_scenario=config.episodes_per_scenario,
            max_retries_per_episode=config.max_retries_per_episode,
        )
        findings = analyze(episodes, min_support=config.min_support, min_ratio=config.min_ratio, skills=lib.skills)
        drafts = propose(findings, bridge=bridge, run_id=run_id, task_id=f"{task_id_for(model_id)}-propose",
                         max_parse_retries=config.max_parse_retries)
        scenarios = {sid: read_scenario(store, sid) for sid in config.scenario_ids}
        verifications = [
            verify(d, findings, episodes, base_instance_factory=instance_with,
                   personas=personas, scenarios=scenarios)
            for d in drafts
        ]
        thresholds = Thresholds(config.min_fired_on_motivating, config.max_fired_elsewhere)
        written = package(store, env.id, findings, drafts, verifications, thresholds=thresholds,
                          run_id=run_id, bound_model_id=model_id)
        _, dropped = screen(drafts, verifications, thresholds)
        dropped += stale_proposals(store, env.id, len(written))
        env.payload.update({
            "episode_ids": [e.id for e in episodes],
            "findings": [f.to_dict() for f in findings],
            "proposals": [p.id for p in written],
            "dropped": dropped,
        })
        assert_transition("nonreview", env.status, "executed")
        env.status = "executed"
        store.write_product(agent_round_file(model_id), env, f"mindagent: {env.id} executed")

    def _record(store: GitStore, rel: str, run_id: str, model_id: str, reason: str, point: str, retries: int) -> None:
        # 读库失败时轮次产物还没写:先补一份最小的 pending 产物再置 failed
        _fail_round(store, _existing_or_new(store, model_id, run_id), reason, category="T3", point=point)
        fail_once(store, run_id, task_id=task_id_for(model_id), category="T3",
                  point=point, retries=max(retries, 0), reason=reason)

    def _fail_unreadable(store: GitStore, model_id: str) -> None:
        """轮次产物在但读不出(计划 8 Ruling 20:视图按"没有轮次"再次触发):用新信封整份重写为
        failed(T3),按 fail_once 的守卫记一次失败记录。run id 取绑定模型产物里的那个;模型本身也读
        不出或不合法时 run id 为空,只在轮次产物上留原因。"""
        try:
            _, run_id, _ = _read_model(store, model_id)
        except (AgentRoundFailedError, *UNREADABLE):
            run_id = ""
        reason = "unreadable round product"
        point = "mindagent.rounds.read"
        env = _round_envelope(model_id, {"model_id": model_id, "run_id": run_id, "failure": None})
        _fail_round(store, env, reason, category="T3", point=point)
        fail_once(store, run_id, task_id=task_id_for(model_id), category="T3",
                  point=point, retries=0, reason=reason)

    return handle
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ma_rounds.py`
Expected: `28 passed`,无警告。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+151 passed`(B 为开工时的基线),无警告。再跑 `git status --short`:除开工前就在的 ` M cli/package-lock.json`、` M cli/package.json` 外,只应有 `src/mindagent/rounds.py` 与 `tests/test_ma_rounds.py`。

- [ ] **Step 6: 提交**

```bash
git add src/mindagent/rounds.py
git add tests/test_ma_rounds.py
git commit -m "feat: mindagent start_agent_round handler (bind latest model and library, deploy, analyze, propose, verify, package; superseded rounds, T3 failure records, reentrant)"
```

---

### Task 88: 端到端——真实 loopback 服务端上的完整 AgentRSI 环路

**Files:**
- Test: `tests/test_ma_e2e.py`

**Interfaces:**
- Consumes: `make_start_round_handler`、`handle_admit_proposal`、`load_library`、`AgentConfig`;`simulator.realtime.server.create_server`(带 `scenario_resolver`,不配落盘钩子);计划 6 的协议改动。
- Produces: 无新名字。

**设计要点:**
- 客户端 stub 外包一层 `_DeadlineStub`(每条流 15 秒 deadline),夹具收尾 `server.stop(0).wait()`(两个 gRPC 测试文件同此)。
- 服务端搭法沿用 `tests/test_mt_trajectory_interact.py` 的 live 夹具:`create_server(factory, port=0, turn_timeout_s=2.0, scenario_resolver=...)`,对手是每个会话新建的 `ScriptedBackend`,说"我真的很生气!!""好吧,你说说看""那就这样吧"。
- 四段:①`zengen-v1`、空库 → 轮次 `executed`、零发现零提案,所有 agent 轨迹 `selected is None`;②手工放基线技能 `skill-goal`(`turn_at_least: 0`,不处理情绪)与 `library_state`(版本 `"1"`)、晋级 `zengen-v2` → 一条 `emotion_high` 发现(3 个 episode、占比 1.0),提案 `pending_review`,验证 1.0 / 0.0;③手工把提案置 `approved` → 收录,库版本变 `{"skill": "2", "memory": "0"}`;④晋级 `zengen-v3` → 第三轮绑定新库,对方怒语之后的那一轮轨迹 `selected` 是新技能、`candidates` 是 `[新技能, skill-goal]`、`conflict` 非空,话语以 `[skill:<新技能>]` 开头;其余轮次仍选 `skill-goal`;没有新发现。
- 本任务只加测试:前面的任务已经实现了全部行为,所以 Step 2 预期直接通过。若失败,说明任务 81 至 87 之间有接口没对上,回到对应任务排查,不要改本测试去迁就。

- [ ] **Step 0: 前置检查(计划 1、计划 6 已合并)**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev python -c "import inspect; from simple_actio.instance import instantiate; from simple_actio.skills import Skill, SKILL_ID_PREFIX; from simple_actio.memory import MemoryEntry, MEMORY_ID_PREFIX; from simple_actio.trace import validate_trace, normalize_library_version; from simple_actio.triggers import referenced_signals; from simple_actio.signals import default_registry; from inference_bridge import BridgeError, ScriptedBridge; from controlplane.paths import agent_round_file, proposal_file, skill_file, memory_file, library_state_file, failure_record_file; from controlplane.ledger import record_failure; from simulator.realtime.client import EnvironmentServiceClient; assert 'activation_trace' in inspect.signature(EnvironmentServiceClient.submit_turn).parameters; assert SKILL_ID_PREFIX == 'skill-' and MEMORY_ID_PREFIX == 'mem-'; print('prereqs ok')"
```

Expected: 最后一行 `prereqs ok`。任何 `ImportError`、`AttributeError` 或 `AssertionError` 都说明计划 1 或计划 6 没有按其文本合并:**停下**,把报错原样报告,不要往下做。

- [ ] **Step 1: 写测试**

新建 `tests/test_ma_e2e.py`:

```python
"""MindAgent 端到端:真实 loopback gRPC 服务端 + 计划 6 的激活轨迹协议。

空库第一轮零提案 → 手工放一个不处理情绪的基线技能 → 第二轮发现 emotion_high 缺口并打包提案
→ 人工批准 → 收录 → 第三轮绑定新库版本,新技能在轨迹里被选中。"""
import grpc
import pytest

from simple_actio.trace import validate_trace
from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import agent_round_file, library_state_file, model_file, proposal_file, skill_file
from controlplane.reconcile import Action
from mindagent.admit import handle_admit_proposal
from mindagent.config import AgentConfig
from mindagent.library import load_library
from mindagent.rounds import make_start_round_handler
from simulator.backends import ScriptedBackend
from simulator.datamodel import Persona, RoleBinding, RoleSlot, Scenario
from simulator.realtime.client import EnvironmentServiceClient
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server
from simulator.store_adapter import read_episode, read_persona, read_scenario, write_persona, write_scenario


class _DeadlineStub:
    """给每条 Interact 流加 gRPC deadline:服务端失联时客户端的 next() 以 DEADLINE_EXCEEDED
    结束,而不是挂住整个测试套件(套件没有单测超时)。"""

    def __init__(self, stub, timeout_s=15.0):
        self._stub = stub
        self._timeout_s = timeout_s

    def Interact(self, request_iterator):
        return self._stub.Interact(request_iterator, timeout=self._timeout_s)


AGENT = RoleBinding("a", "pa", "self_play")
OPP = RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")
OPP_LINES = ["我真的很生气!!", "好吧,你说说看", "那就这样吧"]
GOAL = {"id": "skill-goal", "version": 1, "level": "macro", "priority": 0, "trigger": {"turn_at_least": 0},
        "policy": {"instructions": ["推进目标"]}, "description": "baseline: ignores emotion"}


class Echo:
    """罐头带指导语后端:把指导语拼进话语,技能是否生效直接看得见。"""

    def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
        return ("|".join(guidance) or "plain", f"saw {len(history)}")


def _env(id_, type_, status, payload, links=()):
    return Envelope(id=id_, type=type_, version=1, created_at="2026-10-09T00:00:00Z",
                    created_by=OwnerRef(), status=status, schema_version=1, links=list(links), payload=payload)


def _promote(store, n, run_id):
    ckpt = BigRef(f"file:///ckpt/v{n}", f"{n}" * 64, 1)
    store.write_product(model_file(n), _env(f"zengen-v{n}", "zengen_model", "effective",
                        {"promoted_from": f"cand-{run_id}", "checkpoint_ref": ckpt.to_dict()},
                        [f"cand-{run_id}", run_id]), f"test: promote zengen-v{n}")


@pytest.fixture
def world(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    for sid in ("s1", "s2", "s3"):
        write_scenario(store, Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"],
                                       "deployment_simulation"))
    for pid in ("pa", "pb"):
        write_persona(store, Persona(pid, ["t"], "m", "s", ["v"]))

    def factory(scenario, assignment, personas, client_slot):
        return {s: ScriptedBackend({s: list(OPP_LINES)}) for s in assignment.slot_ids() - {client_slot}}

    server = create_server(factory, port=0, turn_timeout_s=2.0,
                           scenario_resolver=lambda sid: read_scenario(store, sid))
    server.start()
    channels = []

    def client_factory():
        channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
        channels.append(channel)
        return EnvironmentServiceClient(_DeadlineStub(pb2_grpc.EnvironmentServiceStub(channel)))

    handler = make_start_round_handler(
        config=AgentConfig(scenario_ids=("s1", "s2", "s3"), bindings=(OPP, AGENT), max_turns=4,
                           min_support=3, min_ratio=0.5),
        client_factory=client_factory,
        backend_factory=lambda model_id, ckpt: Echo(),
        personas_loader=lambda s, ids: {pid: read_persona(s, pid) for pid in ids},
    )
    yield store, handler
    for c in channels:
        c.close()
    server.stop(0).wait()


def _round(store, handler, model):
    handler(store, Action(kind="start_agent_round", target_id=model, detail={}))
    return store.read_product(agent_round_file(model))


def _agent_traces(store, run_id, episode_ids):
    out = []
    for eid in episode_ids:
        ep = read_episode(store, run_id, eid)
        assert all(t.activation_trace is None for t in ep.turns if t.speaker_slot == "b")
        for t in ep.turns:
            if t.speaker_slot == "a":
                validate_trace(t.activation_trace)
                out.append((t.utterance, t.activation_trace))
    return out


def test_the_full_agent_rsi_loop_over_a_real_server(world):
    store, handler = world

    # 第一轮:空库,裸路径,零提案
    _promote(store, 1, "run-001")
    r1 = _round(store, handler, "zengen-v1")
    assert r1.status == "executed" and r1.payload["proposals"] == [] and r1.payload["findings"] == []
    traces = _agent_traces(store, "run-001", r1.payload["episode_ids"])
    assert len(traces) == 6 and all(t["selected"] is None for _, t in traces)
    assert traces[0][1]["signals"] == ["emotion_high", "first_turn"]

    # 手工放一个不处理情绪的基线技能(库版本 1)
    store.write_product(skill_file("skill-goal"), _env("skill-goal", "skill", "effective", GOAL), "human: skill")
    store.write_product(library_state_file(), _env("library-state", "library_state", "executed",
                        {"skill_version": "1", "memory_version": "0", "admitted": []}), "human: library")

    # 第二轮:发现缺口,提案待审
    _promote(store, 2, "run-002")
    r2 = _round(store, handler, "zengen-v2")
    assert r2.status == "executed"
    assert r2.payload["library_version"] == {"skill": "1", "memory": "0"}
    (finding,) = r2.payload["findings"]
    assert (finding["signal"], finding["support"], finding["ratio"]) == ("emotion_high", 3, 1.0)
    (pid,) = r2.payload["proposals"]
    assert pid == "proposal-agent-round-zengen-v2-1"
    proposal = store.read_product(proposal_file(pid))
    assert proposal.status == "pending_review"
    v = proposal.payload["verification"]
    assert (v["method"], v["fired_on_motivating"], v["fired_elsewhere"]) == ("replay_behavior", 1.0, 0.0)
    assert v["episodes"] == finding["episode_refs"] == r2.payload["episode_ids"]
    assert {u for u, t in _agent_traces(store, "run-002", r2.payload["episode_ids"])} == {"[skill:skill-goal] 推进目标"}

    # 门②:人工批准,收录
    proposal.status = "approved"
    store.write_product(proposal_file(pid), proposal, "human: approve")
    handle_admit_proposal(store, Action(kind="admit_proposal", target_id=pid, detail={}))
    assert store.read_product(proposal_file(pid)).status == "effective"
    lib = load_library(store)
    assert lib.version == {"skill": "2", "memory": "0"}
    assert [s.id for s in lib.skills] == ["skill-agent-round-zengen-v2-1-1", "skill-goal"]

    # 第三轮:绑定新库,新技能在动机轮次上被选中,其余轮次仍是基线技能
    _promote(store, 3, "run-003")
    r3 = _round(store, handler, "zengen-v3")
    assert r3.status == "executed" and r3.payload["library_version"] == {"skill": "2", "memory": "0"}
    assert r3.payload["findings"] == [] and r3.payload["proposals"] == []
    traces = _agent_traces(store, "run-003", r3.payload["episode_ids"])
    new_id = "skill-agent-round-zengen-v2-1-1"
    first_turns = traces[0::2]
    assert all(t["selected"] == new_id and t["library_version"] == {"skill": "2", "memory": "0"}
               for _, t in first_turns)
    assert all(t["candidates"] == [new_id, "skill-goal"] and t["conflict"] is not None for _, t in first_turns)
    assert first_turns[0][0] == f"[skill:{new_id}] 先回应对方的情绪,确认对方的感受,再谈具体问题"
    assert all(t["selected"] == "skill-goal" for _, t in traces[1::2])
```

- [ ] **Step 2: 运行**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ma_e2e.py`
Expected: `1 passed`(实测约 2 至 7 秒)。连续跑三次都应通过:

```bash
for i in 1 2 3; do PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ma_e2e.py tests/test_ma_deployment.py tests/test_ma_rounds.py | tail -1; done
```

Expected: 三行都是 `48 passed`。

- [ ] **Step 3: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+152 passed`(B 为开工时的基线),无警告。再跑 `git status --short`:除开工前就在的 ` M cli/package-lock.json`、` M cli/package.json` 外,只应有 `tests/test_ma_e2e.py`。

- [ ] **Step 4: 提交**

```bash
git add tests/test_ma_e2e.py
git commit -m "test: mindagent end to end over a real loopback server (silent first round, emotion gap proposal, admission, new skill selected in the next round)"
```

---

### Task 89: 全量回归与改动范围检查

**Files:** 无改动。

- [ ] **Step 1: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+152 passed`,无警告。

- [ ] **Step 2: 改动范围只在文件表内**

Run(`BASE` 是任务 80 Step 0b 记进执行记录的提交;当前 shell 里没有这个变量就先 `BASE=<记录里的 sha>`。BASE 丢失时的退路:按本计划逐任务提交、中间没有别的提交时,它就是 `HEAD~9`):

```bash
git diff --stat "$BASE"..HEAD -- . ':(exclude)docs/superpowers'
```

Expected(顺序为 git 的字母序):

```
 pyproject.toml                  |   2 +-
 src/mindagent/__init__.py       |   4 +
 src/mindagent/admit.py          | 107 +++++++++
 src/mindagent/analysis.py       | 139 ++++++++++++
 src/mindagent/config.py         |  56 +++++
 src/mindagent/deployment.py     | 181 +++++++++++++++
 src/mindagent/errors.py         |  24 ++
 src/mindagent/failures.py       |  48 ++++
 src/mindagent/library.py        |  84 +++++++
 src/mindagent/packaging.py      | 141 ++++++++++++
 src/mindagent/proposals.py      | 146 ++++++++++++
 src/mindagent/rounds.py         | 294 +++++++++++++++++++++++++
 src/mindagent/verify.py         | 134 +++++++++++
 tests/test_ma_admit.py          | 197 +++++++++++++++++
 tests/test_ma_analysis.py       | 143 ++++++++++++
 tests/test_ma_config_library.py | 137 ++++++++++++
 tests/test_ma_deployment.py     | 358 ++++++++++++++++++++++++++++++
 tests/test_ma_e2e.py            | 169 ++++++++++++++
 tests/test_ma_packaging.py      | 106 +++++++++
 tests/test_ma_proposals.py      | 137 ++++++++++++
 tests/test_ma_rounds.py         | 477 ++++++++++++++++++++++++++++++++++++++++
 tests/test_ma_verify.py         | 130 +++++++++++
 22 files changed, 3213 insertions(+), 1 deletion(-)
```

再确认三件事:

```bash
git diff --name-only "$BASE"..HEAD -- cli schemas src/controlplane src/simulator src/simple_actio src/inference_bridge src/mindtrainer src/diagnostic
grep -rn "mindagent" src/controlplane || echo "controlplane clean"
git status --short cli
```

Expected:第一条无输出;第二条输出 `controlplane clean`;第三条只列出开工前就在的 ` M cli/package-lock.json`、` M cli/package.json`(本计划没碰它们,也没提交它们)。

- [ ] **Step 3: 新源文件都以 `from __future__ import annotations` 开头**

```bash
python3 - <<'EOF'
import ast
import pathlib

bad = []
for f in sorted(pathlib.Path("src/mindagent").glob("*.py")):
    body = ast.parse(f.read_text(encoding="utf-8")).body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    first = body[0] if body else None
    if not (isinstance(first, ast.ImportFrom) and first.module == "__future__"):
        bad.append(str(f))
print("missing __future__:", bad)
EOF
```

Expected: `missing __future__: []`。

本任务不提交(没有改动)。

---
## 完成标准

十个任务做完(九次提交)后:

- 全量命令为 **B+152 通过**、无警告(本计划新增 152 个用例:`test_ma_config_library` 27、`test_ma_deployment` 19、`test_ma_analysis` 19、`test_ma_proposals` 20、`test_ma_verify` 8、`test_ma_packaging` 8、`test_ma_admit` 22、`test_ma_rounds` 28、`test_ma_e2e` 1)。
- `make_start_round_handler(...)` 的返回值与 `handle_admit_proposal` 都是 `Callable[[GitStore, Action], None]`,可直接调用,也可由计划 8 的 `handlers` 注入。
- 空库第一轮 `executed`、零提案、无失败记录;库里有不处理情绪的技能时,会触发 `emotion_high` 的对手台词产出一条 `pending_review` 的提案,载荷含设计 §4.2 的全部键;批准后收录进 `skill_library/`,`library_state` 版本加一;下一轮绑定新库版本,新技能在轨迹里被选中(真实 loopback gRPC 服务端上验证)。
- 两个处理器在每个崩溃点重跑都收敛:不重复生成部署 episode、不重复写提案、不重复递增库版本。本计划的验证范围是直接调用处理器(崩溃后、瞬态失败后再调一次);再加上调和循环对 `pending` 轮次的重新触发(R19),后者由计划 8、计划 9 经调和循环验证。
- 确定性失败:轮次 `failed` + `failure-agent-<model>.yaml`(T3);非桥接的瞬态:上抛、轮次 `pending`;任何 `BridgeError`:按 R18 判 T3。
- `controlplane` 不导入 `mindagent`;`mindagent` 不导入 `mindtrainer`、`diagnostic`;`pyproject.toml` 的 wheel 列表含 `src/mindagent`;没有新增第三方依赖;`cli/` 未改。

## 已知限制

1. **树内持久化的 agent 配置不在本期。** `AgentConfig` 由组合根注入(Ruling 3);改阈值、情境清单、episode 数要改组合根并重启守护进程。用过的值写在每个轮次产物里。
2. **记忆增量生成器没有实现。** 从发现里抽取"对方反复声明的事实"需要语义理解;V1 的记忆库只能靠人手写 `kind: memory` 的提案增长(收录路径可用,有测试)。
3. **空库永远不会自己长出第一条技能。** "缺口"的定义是"选中的技能不针对信号";库里一条技能都没有时没有路由,分析器按设计沉默。第一条技能要人放。
4. **候选技能固定 `priority: 100`。** 库里已有更高优先级且同样命中的技能时,候选在验证中不会被选中,会被筛掉并记进 `dropped`。
5. **验证用的是触发提案的同一批数据**,`fired_elsewhere` 只统计动机 episode 内的非动机轮次;没有留出集,过拟合风险写在每份提案的 `notes` 里(设计 §15 明确不做)。验证不度量对话质量;judge 的评价只是附注,不参与筛选。
6. **部署 episode 里没有对手的内心独白与轨迹**(Ruling 1)。服务端若另配了落盘钩子,树里会同时有一份 `ep-<session>` 版本,两者不互相引用。
7. **轮次是同步的。** 一轮要跑完全部部署会话、分析、(可选的)外部推理与重放,期间调和循环不转;正在跑的轮次不会因为新模型晋级而被中途打断,只在下一次触发时被置为 superseded。
8. **瞬态异常会让守护进程退出**(与派发器同一已知限制):部署会话 T1 重跑用尽时 `DeploymentFailedError` 上抛,`run_daemon` 不捕获。轮次保持 `pending`;守护进程重启(或下一个 tick)后,按 R19 调和循环会对这个 `pending` 轮次重新触发 `start_agent_round`,处理器复用已落盘的 episode 续跑。外部服务一直不可用时它会每次启动都在同一处退出,需要外部进程管理。
9. **一条增量不合法的已批准提案会让 `admit_proposal` 每个 tick 都被产生一次**(处理器只读不写),直到人改好提案。审核类状态机里 `approved` 没有通往失败的出口。
10. **模型产物坏了(不存在、非 `effective`、`links` 不足两项)、或 run id 不是单个路径段、或 run 目录不存在时没有失败记录**(Ruling 21),原因只写在轮次产物(或原样留着的提案)上:失败记录需要一个真实存在的 run 目录。
11. **分析器与部署驱动器各有一份失败分类与信号视图规则**,分别与 `mindtrainer.trajectory_source`、`simulator.runner` 重复(依赖方向不允许导入前者;后者没有公开函数)。两处若改规则需要同步。
12. **默认只分析 `emotion_high` 与 `no_new_information`**。计划 6 以后注册的新信号要经 `analyze(..., signals=...)` 显式传入;轮次处理器 V1 不传,只用默认两个。
13. **agent_rsi 类型配方在 V1 不自动提名**(设计 §15)。`nominate_recipe` 只由训练 run 的候选触发,MindAgent 的轮次结果没有对应的提名触发;需要时人工写配方候选,锚定字段从轮次产物里取(Ruling 14),载荷须含 `uses_bridge`(计划 3 的 Ruling 9)。
14. **重入之间筛选结果变少会留下过期提案**(Ruling 12):多出来的 `proposal-<round>-<n>` 仍是 `pending_review`,只在轮次产物的 `dropped` 里标为 stale。审批前请核对 `dropped` 与提案所属的 `round_id`。
15. **重入时外部推理成本可能少记。** R19 下轮次重入后会重跑 `propose`,外部推理沿用同一组 `(task_id, cost_type)`(`<task_id>-<i>-attempt<n>` 在每次重入里都从 1 编起),计划 1 的账本先到者为准,会把重跑的成本静默去重,成本被低估。这是有意的取舍;要修需要给 task_id 加重入序号,V1 不做。
16. **树里已提交产物损坏时的行为沿用现状。** 库条目、模型产物与提案读不出(YAML 解析失败、合并冲突标记、信封缺键)时,本计划的处理器不兜底:`load_library`、`read_library_versions`、`_latest_effective_version` 等直接读取会抛出,守护进程退出,重启后同一处再抛。这与现有 `controlplane` 对指令、候选、模型的读取一致(见设计 §14 第 12 条),修法在 `controlplane` 的读取层,不在本计划;只有轮次产物(Ruling 20)有专门兜底。

## 未验证项

1. **累计测试数是在一份临时拷贝里跑出来的。** 拷贝 = 基线(`main` 在 `0385004`,820 通过)+ 计划 1 的最终文本 + 计划 6 的最终文本(从两份计划文档原样抽出全部代码块、原地修改脚本与差异块,按顺序应用,再运行生成脚本)。该拷贝的基线 B 实测为 **1197**(两份前置计划在审计后都加了用例,所以比早先的 1124 大;本计划的增量不受影响),按本计划逐任务放入文件后每个任务结束时的全量结果依次为:任务 80 后 1224、81 后 1243、82 后 1262、83 后 1282、84 后 1290、85 后 1298、86 后 1320、87 后 1348、88 后 1349、89 后仍 1349(每次全量约 100 至 145 秒),全程无警告。真实仓库里 B 取决于开工时已合并的计划,增量不变。拷贝不含 `.git` 历史,所以任务 89 的 `git diff --stat` 是在拷贝里以"未加 `src/mindagent` 的 `pyproject.toml`"为基点、提交本计划全部文件后模拟出来的;行数与文件清单来自那次运行。
2. **每个任务的"确认失败"预期**(`ModuleNotFoundError: No module named 'mindagent'` 或 `'mindagent.<模块>'`)是在拷贝里把对应模块移开后真实运行得到的;未逐任务真实执行"写测试 → 跑失败 → 写实现"的完整顺序。
3. **gRPC 用例的稳定性只在 macOS 上验证过**:`tests/test_ma_deployment.py`、`tests/test_ma_rounds.py`、`tests/test_ma_e2e.py` 连续跑三次都是 `48 passed`;Linux 与 CI 上没有跑过。
4. **套件没有单测超时。** 服务端失联时客户端在 `next()` 上阻塞;本计划的两个 gRPC 测试文件用 `_DeadlineStub` 给每条 `Interact` 流加了 15 秒 deadline,超时以 `DEADLINE_EXCEEDED` 结束,不会挂住整个套件;夹具收尾用 `server.stop(0).wait()`。生产代码里的 `EnvironmentServiceClient` 没有 deadline(改它属于 simulator,不在本计划范围),组合根若要防挂住,应在构造 stub 时同样包一层。
5. **真实外部推理没有调用过。** 桥接路径只用 `ScriptedBridge` 测;外部模型是否真能稳定地"只输出一个 JSON 对象"未知(设计 §14 第 5 条)。
6. **真实模型后端没有接过。** `backend_factory` 在测试里返回回显指导语的罐头后端;真实 checkpoint 怎么按 `checkpoint_ref` 加载、是否支持 `respond_guided`,不在本期。
7. **信号词典与阈值没有数据支持**(设计 §14 第 3 条)。`min_support=3`、`min_ratio=0.5`、`0.8 / 0.2` 的验证阈值、候选优先级 100 都是拟的。
8. **与计划 8 的处理器注入只做了文本核对**:计划 8 的 `start_agent_round` 的 `target_id` 是模型 id、`admit_proposal` 的是提案 id、`detail` 分别为 `{"version", "run_id"}` 与 `{}`,本计划的处理器不读 `detail`;没有在同一份拷贝里把计划 8 的 `reconcile_once(handlers=...)` 与本计划的处理器接起来跑。计划 9 的端到端负责这一步。
