# 设计三：记忆按情境世界组织

状态：设计稿，待审。日期：2026-11-04。基线：rise `84188e5`。
权威文档集是 `docs/design/`。本稿审定后并入 00、20、30、40、50、90 六篇，并按施工计划落地；并入后本稿只作历史来源。

## 0. 这份文档做什么

把记忆从"一个人格一份库"改成"一个模拟世界一份库，人格是其中的主体"。

- **部署**：同一情境的所有人格共处一个部署世界，智能体位和对手位都在里面跨会话积累记忆。
- **训练**：每条 rollout 从这个情境的种子世界复制出一个平行世界，训练位和对手位都是它的主体。
- 改动先在替身后端（`FileTupleStore`、`TupleMemory`）上做完。Starling 记忆服务（需求文档 `2026-11-02-starling-service-requirements.md`）落地后，只换后端实现，协议和调用方不再动。

为什么改：社会情境模拟里，同一情境中的几个人在同一个世界里。一个人格一份库，等于把同一场景的人拆进平行世界，"B 在场听到 A 说话"、共识、二阶 ToM 都没有地方表达。用户要在 Starling 看板里按世界查看一个情境的全貌，包括训练时的每条 rollout。

## 1. 已定事项

| 事项 | 决定 | 来源 |
|---|---|---|
| 世界的粒度 | 一个情境一个部署世界；每条 rollout 一个平行世界 | 用户 2026-11-03 |
| 部署对手位的记忆 | 也进部署世界，跨会话积累 | 用户 2026-11-04 |
| 同一人格在一个情境里占两个角色位 | 拒绝 | 用户 2026-11-04 |
| 快照与恢复的粒度 | 整个世界，不做按主体的局部恢复 | 用户 2026-11-03 |
| 感知语义 | 不改：每个实例只写他人的发言，写在自己的主体名下 | 本稿 |
| 训练对手位记忆 | 照旧从种子起、每条 rollout 一份，与训练位同一个世界 | 本稿 |

## 2. 目标与非目标

目标：

1. 记忆的单位是世界。部署世界按情境，训练世界按 rollout，种子按情境。
2. 一个世界里有多个主体，每个主体只召回自己名下的记忆。
3. 世界级的快照、恢复、摘要、待处理事件；部署轮次与门② 按世界取锁。
4. 同一组 rollout 从同一个种子世界出发，互相看不见；起点摘要按情境进 `config_hash`。
5. 部署对手位跨会话积累记忆，部署轮次失败时一起回滚。

非目标：

- 接 Starling 记忆服务。本稿只定协议与替身实现；Starling 后端在服务落地后另做。
- 共享感知（在场的人都记住同一句话）。以后接 Starling 的感知授权。
- 跨情境共享一个人格的记忆。同一人格在两个情境里是两个世界里的两个主体。
- 已有部署库与训练记录的迁移。旧数据只来自验证（见 4.8）。

## 3. 世界与主体

### 3.1 三类世界

| 世界 | 键 | 主体 | 谁写 | 生命周期 |
|---|---|---|---|---|
| 部署世界 | `scenario_id` | 这个情境绑定的全部人格 | 部署会话的每个位；门② 收录 | 长期 |
| 种子世界 | `(seed_id, scenario_id)` | 种子里的人格 | 人放进去，rise 只读 | 长期 |
| rollout 世界 | `(run_id, logical_episode_id)` | 这条 rollout 的全部位的人格 | 训练位（客户端）与对手位（服务端） | 一条 rollout |

验证重放用的临时世界，是从部署世界的快照恢复出来的只读复本，见 4.7。

### 3.2 主体与端口

- 一个端口绑定 `(世界, 主体)`。主体就是人格 id。
- `recall` 只返回本主体名下的记忆。`remember` 写在本主体名下。
- 一个世界里同一主体只开一个可写端口（第 4.2 节"一位一主体"保证）。
- 世界级操作（快照、恢复、摘要、待处理事件、放弃事件）作用于整个世界，不分主体。

替身怎么做：世界是一份账本文件，账本里每条记忆和每个事件行都带 `holder`。召回按 `holder` 过滤，摘要覆盖全部主体。Starling 怎么做：世界是一个 Starling 世界，端口以 `agent=<主体>` 打开，与今天 `Memory.open(agent=persona_id)` 一致。

### 3.3 事件 key

一个世界里所有主体共用一张事件表，主键是 `key`。今天的 key 是 `<逻辑 episode id>#<turn_index>`。实例只写别人的发言（`simple_actio.instance` 的 `_remember_new`），所以同一轮会被除说话人以外的每个主体各写一次。

- 两个角色位时：第 3 轮是 A 说的，只有 B 写它，key 不会撞。
- 三个及以上角色位时：A 说的第 3 轮，B 和 C 都会写，key 就撞了。替身的 `_Ledger.begin` 把同 key、同文本的事件当成幂等重写，第二个主体的那条记忆根本写不进去。Starling 的事件主键是 `(tenant_id, key)`，同样会撞。

情境的角色位数没有上限。`Scenario.role_slots` 是一个列表，`simulator.runner.run_episode` 按 `turn_index % n_roles` 轮流发言。今天的默认情境和测试夹具都只有两个位，但设计不能依赖这一点。

**新 key 的格式是 `<前缀>#<turn_index>@<主体>`**，主体就是写入方的人格 id。前缀按场合取：

| 场合 | 前缀 | 例子 |
|---|---|---|
| 训练（训练位与对手位） | 逻辑 episode id | `run-7-grpo3-s1-r0#5@pa` |
| 部署（智能体位与对手位） | `<round_id>/<部署 episode id>` | `agent-round-zengen-v2/deploy-zengen-v2-s1-1#5@pb` |
| 门② | 不变，仍是 `<proposal_id>#<k>`，不带主体 | `proposal-agent-round-zengen-v2-1#1` |

门② 的 key 不带主体也撞不上：它以提案 id 开头，和对话 key 的前缀不同。

**部署对手位要能拿到轮次 id。** `CreateSession.episode_id` 只允许 `[A-Za-z0-9._-]`（`simulator.realtime.validation.validate_episode_id`），装不下 `<round_id>/`。做法和训练会话一样，靠登记：

- 部署驱动器开会话之前，向 `AgentRuntime` 登记 `(episode_id → round_id)`，会话终态后注销。今天训练会话按情境 id 登记 run（`register_session`），这里多一张按 episode id 的部署登记表。
- 服务端造会话资源时，按 `info.episode_id` 查这张表，拿到 `round_id`，拼成 `<round_id>/<episode_id>`，作为对手位实例的 `episode_id`。
- 查不到就按"没有部署记忆"处理：对手位照旧用一份空的 `TupleMemory`，并记一条 warning。只有绕过部署驱动器直接开会话的调用方会落到这里。

这样，提交时按前缀 `<round_id>/<eid>#` 放弃 pending 行，就能把对手位留下的行一起放弃，提交逻辑不用改。

### 3.4 一位一主体

同一人格在一个情境里占两个角色位时拒绝。两个位共用一个主体，就共用一份记忆和同一组事件 key，今天"两个位各开一份、互不可见"的行为没有对应物。

拒绝点：

| 场合 | 位置 | 判法 |
|---|---|---|
| 训练配置校验 | `validate_training_config` | `bindings` 里 `persona_id` 重复 → 配置错误，指令 T2 |
| 部署配置 | `read_agent_settings` | 同上，抛 `FactoryConfigError`。它有三个调用方：`factory.handlers`（轮次处理器装配）和 `factory.wire`（运行时装配）在装配期调用，出错退出码 2；`factory.interact_policy` 在每次派发时调用，把错误转成 `DirectiveFailedError`，指令判 T2 |
| 合成的绑定 | 派发器在合成情境与人格之后、解析记忆之前 | 同上 → T2 |

现有配置与测试里没有这种绑定（实测：`tests/` 下所有 `interlocutors` 字典的值都不重复）。

## 4. 部署

### 4.1 布局

| 路径（替身，`<根>` 是记忆根） | 内容 |
|---|---|
| `<根>/deploy/<scenario_id>.json` | 部署世界 |
| `<根>/deploy/<scenario_id>.lock` | 世界锁 |
| `<根>/snapshots/<round_id>/deploy-<episode_id>.json` | 起点快照，一个 episode 一份，整个世界 |
| `<根>/snapshots/<round_id>/verify-<scenario_id>.json` | 验证快照，一个情境一份 |
| `<根>/snapshots/<round_id>/verify/` | 验证临时世界 |

快照文件名里没有人格，也不需要从文件名里解析人格。起点快照属于哪个世界由 episode id 决定：`deploy-<model>-<scenario>-<k>`，轮次里每个 episode 只属于一个情境。为了不靠解析 episode id，快照旁写一个同名 `.world` 文件，内容是 `scenario_id`。回滚读它找到世界。

Starling 后端的路径相同，后缀是 `.db`。

### 4.2 锁

| 锁 | 键 | 谁拿 |
|---|---|---|
| 世界锁 | `scenario_id` | 部署轮次：本轮的全部情境；门②：收录涉及的全部情境 |

取代今天的人格锁。轮次的情境集合来自 `AgentConfig.scenario_ids`，不随人格变。按 `scenario_id` 排序后依次取，取不到就 `DeferredError`。

### 4.3 未提交快照的守卫

一个世界的时间是线性的。回滚一个世界会退掉快照之后的所有写入，所以在快照与回滚之间不能有别的动作写这个世界。

| 动作 | 写入前要求 |
|---|---|
| 部署轮次开一个新的部署 episode | 这个情境世界没有**别的轮次**留下的未提交起点快照 |
| 门② 收录 | 涉及的情境世界没有任何未提交起点快照（今天已有全局版本：只含技能的提案也要等，见 `mindagent/admit.py`，保留） |

部署轮次一条：今天没有。守护进程一次只处理一个动作，轮次处理器开头还会先跑 `supersede_stale_rounds`，把更旧的 `pending` 轮次回滚、删掉它们的快照。所以正常流程里不会出现"R1 留着快照、R2 写同一个世界"。只有两种情形会出现：

| 情形 | 为什么快照还在 | 守卫的作用 |
|---|---|---|
| R1 的回滚失败（`RestoreFailedError`） | 设计上就把库和快照留给人处理 | R2 不往这个世界写，免得人恢复 R1 的快照时把 R2 的写入一起退掉 |
| R1 的轮次产物读不出 | `supersede_stale_rounds` 跳过读不出的产物 | 同上 |

两种情形都需要人来处理，所以守卫抛 `DeferredError`，原因里写出快照所在的轮次 id 和"需要人工恢复或删除快照"。守护进程连续延后 20 次后会带着这条原因退出（`controlplane.daemon.DeferredExhaustedError`），这正是要的结果：让人看到。

本轮自己的快照不算在内：重入时"快照在就先恢复"是今天的恢复路径，不变。

测试构造：直接在快照目录里放一份属于别的轮次 id 的起点快照，再跑本轮。

### 4.4 一个部署 episode

| 步 | 今天 | 改后 |
|---|---|---|
| 起点快照 | `backup_to(agent_persona, snap)` | `backup_to(scenario_id, snap)`，整个世界。文件名 `deploy-<episode_id>`，episode id 本身也以 `deploy-` 开头，所以是 `deploy-deploy-zengen-v2-s1-1.json`。双前缀让新文件名与今天的 `<episode_id>-<persona_id>` 分得开，旧布局检查靠它 |
| 记起点摘要 | `on_episode_start(eid, {persona: digest})` | `on_episode_start(eid, digest)`，一个世界一个摘要 |
| 每次尝试开端口 | `open(agent_persona)` | `open(scenario_id, agent_persona)` |
| 部署对手位 | 每会话一份空 `TupleMemory` | `open(scenario_id, opponent_persona)`，服务端开，见 4.5 |
| 尝试失败 | 关端口，`restore_from(agent_persona, snap)` | 关**全部**端口（客户端与服务端），`restore_from(scenario_id, snap)` |
| 成功 | 放弃本 episode 前缀的 pending 行，删快照 | 同，pending 行按世界列出 |
| 复用判据 | 首个智能体轮次 `digest_before == episode_digests[eid][persona]` | `== episode_digests[eid]`（世界摘要） |

复用判据要改，原因见 4.5 末尾的"首轮摘要的顺序"。

`episode_digests` 的形状从 `{episode_id: {persona_id: digest}}` 改成 `{episode_id: digest}`。

### 4.5 部署会话里的对手位

部署对手位今天每会话一份空记忆，不进部署库。改后它进部署世界：

- 服务端造会话资源时，对没登记给训练 run 的会话（部署会话），按 `(scenario_id, persona_id)` 开部署世界的端口。
- 开端口需要部署记忆管理面。`AgentRuntime` 构造时多收一个可选的 `deployment_store`，只在启用了部署轮次时由组合根传入。
- 会话终态时服务端关掉这些端口（随 `SessionResources.close`）。

**顺序约束：恢复和下一次尝试之前，服务端的端口必须已关。** 部署驱动器每次尝试的收尾顺序是：客户端 `abort` 或会话正常结束，然后服务端在 `Interact` 的 finally 里收尾，最后驱动器 `restore_from`。服务端收尾有三步：先 quiesce，再 join 在途后端线程（`resource_join_timeout_s`，默认 60 秒），最后 close 端口（`simulator.realtime.servicer`），而且是在另一个线程里做的。

做法：管理面记录每个世界当前打开的端口数，并提供 `wait_closed(world_id, timeout_s)`。驱动器在 `restore_from` 之前、在每次尝试开端口之前、在 episode 提交之前都先等这个世界的端口全部关掉，上限是 `SESSION_CLOSE_WAIT_S`，取 70 秒（服务端的 join 上限再加 10 秒余量）。超时有两种处理：
- 要恢复的时候，按今天的 `RestoreFailedError` 处理，库和快照都保留，等人来看；
- 只是开下一次尝试的时候，按瞬态失败处理，轮次留在 `pending`，守护进程退避后重入，重入时"快照在就先恢复"。

join 等不到时，服务端把资源移进滞留表，由回收线程过后再关。这种情况下 70 秒也等不到，就落进上面两条。

**首轮摘要的顺序**：今天的复用判据靠"首个智能体轮次的 `digest_before` 等于起点摘要"。

- 两个角色位时这条判据仍然成立。先开口的一方 history 为空，什么都不写；智能体位首轮时世界里还没有本 episode 的写入。
- 三个及以上角色位时不成立。比如 C 先开口、B 第二个说话、智能体位 A 第三个说话：B 第二个说话时已经把 C 的话写进了世界，A 首轮的 `digest_before` 就不再等于起点摘要。

所以复用判据改成：episode 已落盘，起点快照已删除，`episode_digests[eid]` 有值，并且每个智能体轮次的技能版本都等于当前版本。首轮的 `digest_before` 仍写进轨迹，但不再参与判据。

理由：复用要防的是"同一个 episode id 下世界的起点不一样"。起点摘要是在拍快照时记进轮次产物的，提交点是删除快照，两者在同一个轮次里一一对应。`digest_before` 原本只是这份对应关系的旁证，在多人写入的世界里已经不能可靠地读出起点。

### 4.6 门②

- 提案的记忆增量 `definition` 从 `{persona_id, text, interlocutor}` 改成 `{scenario_id, persona_id, text, interlocutor}`。`scenario_id` 指世界，`persona_id` 指主体。
- 校验：`scenario_id` 过路径段白名单；`(scenario_id, persona_id)` 必须是部署配置里这个情境确实绑定的一对，否则 T2。
- 今天没有代码生成记忆增量（`mindagent/proposals.py` 的生成器只出技能），只有手写的提案与测试会带它。所以没有生产者要改，只改校验与写入。
- 门② 取涉及情境的世界锁，并守 4.3 的规则。

### 4.7 验证重放

今天：所有 episode 跑完后，给智能体位人格的部署库拍一份验证快照。`verify()` 只用一个基线实例和一个候选实例，回放 `finding.episode_refs` 里的全部 episode。`analysis.analyze` 按信号聚合全部 episode，不分情境，所以一个发现引用的 episode 会跨情境。

改后，一个端口只绑一个 `(情境世界, 主体)`，实例要按情境来造：

| 项 | 改后 |
|---|---|
| 验证快照 | 所有 episode 跑完后，本轮每个情境世界各拍一份 `verify-<scenario_id>` |
| 实例工厂 | `base_instance_factory(extra_skills, scenario_id)`：从这个情境的验证快照恢复出一个临时世界，开智能体位主体的只读端口 |
| `verify()` | 回放每个 episode 时，按 `episode.scenario_id` 取这个情境的基线实例和候选实例，同一情境的实例在同一个 `verify()` 调用里复用；指标照旧对全部 episode 累计 |
| 清理 | 和今天一样，在 finally 里关掉所有实例，删掉所有验证快照和临时世界 |

候选技能对所有情境都一样，只有记忆按情境不同。重放实例是只读的（`replay_read_only=True`），回放时不往临时世界里写。

### 4.8 旧数据

不迁移。现有部署库只有验证留下的 `rise-memory/deploy/` 一份。装配期发现记忆根下有旧布局（`deploy/` 下有不是已知情境名的库文件，或 `snapshots/` 下有旧格式文件名）时，**拒绝启动**（`FactoryConfigError`，退出码 2），文字说明要清空哪个目录。不静默忽略：旧库里的记忆不会被任何世界读到，悄悄留着会让人以为它还在用。

## 5. 训练

### 5.1 rollout 世界

一条 rollout 一个世界，训练位与对手位都在里面。

| 项 | 今天 | 改后 |
|---|---|---|
| 训练位副本 | `runs/<run>/client/<episode>/<slot>.db`，客户端开 | `runs/<run>/<episode>.db`，客户端开 |
| 对手位副本 | `runs/<run>/<session>/<slot>.db`，服务端开 | 同一个 `runs/<run>/<episode>.db`，服务端开 |
| 起点 | 每个位从 `seeds/<seed>/<persona>.db` 复制 | 整个世界从 `seeds/<seed>/<scenario>.db` 复制 |
| 谁建世界 | 各开各的 | 客户端的 `policy_for_rollout` 建（复制种子），服务端只开 |
| 收尾 | 各删各的目录 | 客户端删：服务端端口全关之后 |

逻辑 episode id 是 `{run_id}-grpo{step}-{scenario_id}-r{k}`，只用 `[A-Za-z0-9._-]`（`mindtrainer.trajectory_source.logical_episode_id`），是单段名，直接拼进路径，过今天 `_segment` 的同一份白名单。

替身今天没有训练副本（每个位一份新的 `TupleMemory`）。改后替身也走 rollout 世界：每条 rollout 一份内存里的世界对象，训练位与对手位共用它，不落盘。起点是空世界（替身不支持种子，与今天一致）。

### 5.2 客户端与服务端怎么找到同一个世界

`TrainingCopies` 在进程内持一张表 `episode_id → 世界`，一把锁护着：

1. 客户端 `policy_for_rollout(episode_id)` 进入时：复制种子建世界，登记进表，开训练位端口。
2. 客户端 `create_session(episode_id=...)`。服务端造会话资源时按 `info.episode_id` 查表，开对手位端口。查不到 → 造后端失败（今天"run 没 bind"同一路径），会话被拒。
3. 会话终态：服务端关对手位端口。
4. 客户端退出 `policy_for_rollout` 时：先关训练位端口，再等服务端端口关完（上限与 4.5 相同，70 秒），然后从表里摘掉这个世界，删掉世界文件。等不到时，世界登记进保留表，目录留给启动清扫处理，和今天 `release` 关不上端口时的做法一样。这条 rollout 照常结束，因为它的轨迹已经拿到了。

两端在同一个进程里，所以一张进程内的表够用。模拟器搬到 GPU 主机之后，这张表要换成 Starling 服务里的世界名，那时由服务按名字找世界。

重试：同一个 episode id 的重试按今天客户端副本的做法，先删后建。这样做没有风险，因为上一次尝试的上下文已经退出。如果表里还登记着同名世界，说明服务端端口在 70 秒内没关上，这时拒绝重建，抛**瞬态**记忆失败（`FactoryMemoryUnavailableError`）：

- 今天这条路径走不到，因为客户端副本和服务端副本在两个不同的目录。改为同一个世界之后，服务端最长要收尾 60 秒，滞留得还会更久。重试往往紧跟在一次瞬态失败之后，正是服务端收尾最慢的时候。
- 判成确定性失败的话，一次慢收尾就会让整个训练 run 判 T2，代价不对等。
- 判成瞬态之后，轨迹源把它记为 `role_memory_unavailable` 再重跑；重试次数用完就抛 `RolloutFailedError`，交给 `factory.interact_policy` 翻译成 `RolloutDeferredError` 走延后通道，指令保持 `pending`。守护进程退避之后，滞留的资源大多已被回收线程关掉。
- **轨迹源要配套改一处。** 今天 `InteractTrajectorySource._attempt` 只接 `respond` 里抛出的 `RoleMemoryUnavailableError`；进入 `policy_for_rollout` 时抛的异常原样上抛，既不记失败尝试，也不重跑。改后，进入策略上下文时抛的 `RoleMemoryUnavailableError` 也记成一次瞬态失败尝试 `role_memory_unavailable`，并重跑。别的进入期异常照旧原样上抛。

今天 `_held` 有一条"拒绝往被保留的目录里重建"的防线，它的语义不变：仍然拒绝，只是把类别从确定性改成瞬态。

### 5.3 起点摘要

| 项 | 今天 | 改后 |
|---|---|---|
| 解析器签名 | `resolve(seed, persona_ids) -> ({persona: digest}, runtime)` | `resolve(seed, scenario_ids) -> ({scenario: digest}, runtime)` |
| `memory.digests` | `{persona_id: digest}`，必须恰好是绑定的人格 | `{scenario_id: digest}`，必须恰好是配置的情境 |
| 种子文件 | `seeds/<seed>/<persona>.db` | `seeds/<seed>/<scenario>.db` |
| 没有这个文件 | 这个人格从空库起 | 这个情境从空世界起 |

`memory.digests` 进 `config_hash`。键的含义变了，所以已有的 interact 训练记录续训时哈希不同，判 T3（"配置变了"），不会静默续训。这与本稿"旧数据不迁移"一致。

合成情境（`scenario_ids` 由派发器合成）时，解析在合成之后，情境集合已经确定，与今天"解析在合成之后"的顺序相同。

## 6. 协议

### 6.1 端口

`MemoryBackend` 五个方法不变。端口在打开时绑定世界与主体，方法签名里没有主体。

### 6.2 部署管理面

```python
class DeploymentMemoryStore(Protocol):
    def open(self, world_id: str, holder: str) -> MemoryBackend: ...
    def remember_event(self, world_id: str, holder: str, key: str, text: str, *, interlocutor: str) -> RememberOutcome: ...
    def abandon_event(self, world_id: str, key: str) -> None: ...
    def pending_events(self, world_id: str) -> list[str]: ...
    def backup_to(self, world_id: str, path: Path) -> str: ...
    def restore_from(self, world_id: str, path: Path) -> str: ...
    def digest(self, world_id: str) -> str: ...
    def wait_closed(self, world_id: str, timeout_s: float) -> bool: ...
```

- `world_id` 和 `holder` 都要通过 `PERSONA_ID_PATTERN` 校验。两者用同一份白名单，因为都要拼进文件名。
- `wait_closed` 等这个世界打开着的端口数降到 0 才返回 `True`，超过 `timeout_s` 返回 `False`。端口数由管理面在实例内计数，包括别的线程开的端口。所以组合根只能造一个管理面，并把它交给部署驱动器、门② 和服务端三方共用。今天的做法已经要求轮次处理器和门② 共用同一个管理面，这里只是再加上服务端一方。
- `restore_from` 在这个世界还有任何打开的端口时抛 `MemoryFailureError(transient=False)`。别的世界开着端口不影响。
- `abandon_event`、`pending_events` 按世界，不分主体：事件 key 已经带了主体。

### 6.3 训练副本

```python
class TrainingWorlds:
    def bind(self, run_id: str, seed: str | None) -> None: ...
    def unbind(self, run_id: str) -> None: ...
    def open_rollout(self, run_id: str, scenario_id: str, episode_id: str, holder: str) -> MemoryBackend: ...
    def join_rollout(self, episode_id: str, holder: str) -> MemoryBackend: ...
    def close_rollout(self, episode_id: str, ports: Sequence[MemoryBackend]) -> bool: ...
    def sweep(self) -> int: ...
```

`open_rollout` 建世界并开第一个端口（客户端）；`join_rollout` 在已登记的世界里开端口（服务端）；`close_rollout` 关端口，最后一个关上且建世界的一方调用时删世界。替身与 Starling 各一个实现，过同一套契约测试。

## 7. 关键决策

| 编号 | 决策 | 备选 | 理由 | 代价 |
|---|---|---|---|---|
| D1 | 一个情境一个部署世界 | 一个人格一个世界；所有情境共处一个世界 | 情境是模拟世界的单位，人格之间的在场、共识发生在情境里。所有情境一个世界则不同情境的会话在同一时间线上，回滚一个情境的 episode 会退掉别的情境 | 同一人格在两个情境里记忆不相通 |
| D2 | 快照与恢复按整个世界 | 按主体局部恢复 | 局部恢复要在多主体共享的表里逐行剔除，正是 Starling 世界设计避开的"逐表删干净" | 回滚一个 episode 会退掉同世界里别人在快照之后的写入，靠 4.3 的守卫挡住 |
| D3 | 部署对手位进部署世界 | 每会话一份空记忆 | 用户定：对手位人格也是这个世界里的人，应当记得以前的相遇 | 对手行为随部署次数漂移；验证重放与轮次之间比较多一个变量，见第 9 节 |
| D4 | 事件 key 加 `@<主体>` | key 不变，按主体分事件表；只在三个及以上角色位时加 | 一个世界只有一张事件表，三个及以上角色位时，同一轮会被两个主体各写一次；替身会把第二次当成幂等重写，Starling 的 `(tenant_id, key)` 主键会冲突。两个角色位时也统一加上，这样 key 格式不随角色位数变化 | 历史轨迹和旧库里的 key 格式不一样；旧数据不迁移 |
| D5 | 复用判据改为只看轮次产物里记下的起点摘要和快照是否已删，不再比较首轮 `digest_before` | 保留首轮比较，只在两个角色位时用 | 有三个及以上角色位时，第二个对手可能先于智能体位写入，首轮摘要就不再等于起点 | 少了一个旁证；提交点仍是删除快照 |
| D9 | 部署对手位靠登记表拿到轮次 id | 放宽 `CreateSession.episode_id` 的白名单，允许 `/`；提交时另外匹配 `<eid>#` 前缀 | 白名单拼进落盘路径，不能放宽；只匹配 `<eid>#` 会误伤下一轮同名的 episode | `AgentRuntime` 要多维护一张登记表 |
| D6 | 同一人格占两个位拒绝 | 允许，共用主体 | 共用主体就共用记忆与 key，与今天"互不可见"的行为不同，也没有使用场景 | 想让一个人格同时扮两个位的配置写不出来 |
| D7 | 客户端建、删 rollout 世界，服务端只开 | 服务端建；各自建各自的 | 客户端先于服务端知道 episode id，且 `policy_for_rollout` 包住整条 rollout；服务端建则客户端要等会话建成才能开训练位端口 | 服务端开端口依赖一张进程内的表，模拟器搬走后要换成按名字找 |
| D8 | 旧布局拒绝启动，不迁移 | 自动迁移；静默忽略 | 旧数据只来自验证；静默忽略会让人以为旧记忆还在用 | 升级时要手动清空记忆根 |

## 8. 失败处理

| 情形 | 判定 | 去向 |
|---|---|---|
| 世界锁被占 | 瞬态 | `DeferredError`，守护进程退避 |
| 部署 episode 开始时有别的轮次的未提交快照 | 瞬态 | `DeferredError` |
| 恢复时服务端端口 70 秒内没关 | 确定性 | `RestoreFailedError`，库与快照保留等人 |
| 开下一次尝试或提交时服务端端口 70 秒内没关 | 瞬态 | `DeferredError`，轮次留 pending；重入时先恢复快照 |
| 服务端按 episode id 找不到 rollout 世界 | 照现有的 `role_backend_failure` 处理 | 造后端失败，会话以 `role_backend_failure` 被拒，轨迹源不重跑。`factory.interact_policy` 把它译成 `RolloutDeferredError` 走延后通道，续训上限用完后升 T3。只有绕过 `policy_for_rollout` 直接开会话的调用方会落到这里 |
| 训练重试时同名世界还被服务端端口占着 | 瞬态 | `open_rollout` 抛 `FactoryMemoryUnavailableError`。轨迹源把它记成一次瞬态的失败尝试 `role_memory_unavailable`，原样重跑；重试用完后走延后通道 |
| 门② 的 `(scenario_id, persona_id)` 不在部署配置里 | 确定性 | T2，提案保持原状 |
| 绑定里人格重复 | 确定性 | 训练 T2；部署配置退出码 2 |
| 记忆根是旧布局 | 确定性 | 装配期退出码 2 |

## 9. 对实验的影响

- **部署对手位会积累记忆**，一个情境部署得越多，对手越"认识"智能体。验证重放不受影响：重放只跑智能体位，对手的话按存档原样回放（`mindagent.verify.replay`），对手不读记忆。轮次之间比较行为时，对手的记忆状态是一个新变量。
- **训练对手位行为不变**：仍从种子起、每条 rollout 一份。区别只是和训练位在同一个世界里。今天主体之间互相看不见对方名下的记忆，所以召回结果与今天相同。
- **起点摘要的身份变了**：从"每个人格的库"变成"每个情境的世界"。旧 interact run 续训判 T3。

## 10. 测试

| 层 | 内容 |
|---|---|
| 契约 | 两个部署管理面、两个 `TrainingWorlds` 各过同一套契约测试：同世界两主体互不召回；世界摘要覆盖两主体；恢复在任一主体端口开着时被拒；别的世界开着端口不影响恢复；事件 key 带主体不撞 |
| 部署 | 对手位跨两个部署 episode 能记得上一次；某次尝试失败后，两个位的写入都回滚；快照目录里预先放一份别的轮次的起点快照，本轮被延后，原因里写明那个轮次；三个角色位、智能体位第三个发言时，已提交的 episode 仍能复用；部署对手位的 pending 行在提交时被放弃 |
| 三位 | 三个角色位的同一轮被两个主体各写一次，两条记忆都在，key 不撞（替身与 Starling 各一条） |
| 验证 | 一个发现跨两个情境时，每个情境的 episode 都在本情境的验证世界里回放 |
| 门② | 增量带 `scenario_id`；不在配置里的 `(scenario, persona)` 判 T2；守卫按世界 |
| 训练 | 同一 rollout 的训练位与对手位在同一世界、互不召回；同一组两条 rollout 起点摘要相同、互不可见；服务端找不到世界时会话被拒；客户端在服务端端口关完之后才删世界 |
| 配置 | 人格重复在三处拒绝；`memory.digests` 键必须恰好是情境集合；旧布局拒绝启动 |

## 11. 未核实项

| 项 | 怎么核实 |
|---|---|
| 服务端关端口的耗时分布：正常收尾是否远低于 70 秒 | 跑 100 条部署 episode，记录每条从客户端结束到服务端端口关完的耗时 |
| 部署对手位积累记忆后，智能体位的行为是否随部署次数漂移 | 同一模型连续部署 5 轮，比较每轮的发现集合 |

## 12. 对 Starling 需求文档的影响

需求文档 `2026-11-02-starling-service-requirements.md` 的 5.2 已按"一个模拟世界一个 Starling 世界"写好。本稿补两点，需求文档随之修订：

- 事件 key 在世界内唯一，rise 自己在 key 里带主体（3.3），Starling 不需要按主体分事件表。
- rollout 世界由客户端建、服务端按名字打开（5.2）。服务化以后，"按名字打开已存在的世界"是必须的。
