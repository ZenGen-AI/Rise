# 真实后端接入设计:LoRA SFT 经 ROLL 训练,经 vLLM + SoMEval 评测

状态:草案,已按架构评审修订(失败通道、阻塞代价、评测失败落点、哈希口径),待审阅。日期:2026-10-15。
前置设计:`2026-10-03-mindtrainer-design.md`、`2026-10-09-closed-loop-integration-design.md`。

## 0. 这份文档做什么

闭环现在全部跑在 mock 上:`MockExecutor`、`MockConverter`、`MockSoMEvalRunner`、`MockJudgeBackend`,`factory/evaluation.py` 里 `backends` 恒为 `"mock"`。这份设计把其中一条链路换成真的:

基座权重 → ROLL 在远端 8×H800 上做 LoRA SFT → 存点 → 转成 vLLM 原生 LoRA 适配器 → vLLM 服务 + SoMEval 做诊断评测 → `backends="real"` 的诊断报告

本稿采用的三个取舍(请确认,我不能保证它们都来自你明确的指示):

1. 训练只接 LoRA SFT。GRPO/OPD 这一轮保持 mock,并且拒绝与真实执行器混用。
2. 远端主机用 ROLL4ZenGen 文档记录的验证主机(文档里记作 `.181`,8×H800),本机经 SSH 调度。
3. 推理用该主机容器里的 vLLM 开发版,不用 PyPI 发布版。

不做:GRPO/OPD 真实训练、自定义 reward worker、把适配器合并回完整 HF 权重、Q4 开放题的真实 judge(RealJudge)、升级 vLLM、训练与评测并发抢 GPU、异步派发(`submitted` 契约,见 D12)、把 GPU 小时写入成本账本(见 §8.2)。

## 1. 事实依据

先说清可信度。这次会话里大输出的读取命令返回的内容是残缺的,所以我没能完整读到多数源码和文档。后来用只回传几行的小探针,核对到下面这些,它们是实际看到的输出:

- `sft_lora.yaml`:`seed: 42`、`max_steps: 100`、`save_steps: 50`、`prompt_key: instruction`、`response_key: output`、`lora_rank: 64`、`num_train_epochs: 2`、每卡批大小 1、梯度累积 1。文件里有注释 "400 examples / DP8 * 2 epochs = 100 optimizer steps"。搜 `shuffle` 没有命中。
- `run_sft_validation.py` 的参数:`--checkpoint-hard-links --checkpoint-output-dir --config --eval-steps --heldout-data --heldout-records --max-steps --model-path --output-dir --resume --save-steps --sequence-length --train-data --train-records`。
- README:vLLM 版本串 `0.1.dev20073+g8e685d198`;第 26 行说版本串本身不能标识全部 wheel/构建输入;第 187 行说适配器带 `roll_lora_layout=qwen4_exp_vllm_2d`,给原生 vLLM 文件加载器用。
- 本机:`TrainingExecutor` 协议在 `mindtrainer/executor.py:31`,五个方法名与下文一致;`_DETERMINISTIC` 有两处,`mindtrainer/dispatch.py:36` 和 `mindtrainer/orchestrator.py:36`。

除此之外的内容,包括 "29 秒、约 8 GiB、148,808 个张量"这类数字,以及 SoMEval 和本机其余代码的描述,都来自会话摘要和我的整理,**不要当作已核实的事实**,实现前必须重读原文,尤其是第 10 节标"阻塞"的项。ROLL4ZenGen 文档里的验证结果是它自己的记录,我没有在主机上复现。

本机(未核对,实现前重读 `mindtrainer/executor.py`、`orchestrator.py`、`dispatch.py`、`diagnostic/` 与 `controlplane/evidence.py`):

- `TrainingExecutor` 协议有五个方法:`load`、`sft_step`、`grpo_opd_step`、`save_checkpoint`、`checkpoint_intact`。`MockExecutor.sft_step` 的指标名是 `sft_loss`。
- `run_training` 逐步循环:每步取 `batch_for_step(episodes, step, sft_batch_size)`,校验 `result.step == step`,更新摘要,调 `on_step`;每 `checkpoint_every_steps` 步存点并回写 run;训练完存 `final`,调 `converter.convert`,再 `register_candidate`。
- `make_mindtrainer_dispatcher` 有 `executor_factory`(无参,默认 `MockExecutor`)和 `converter`(默认 `MockConverter`)两个注入点。
- `SoMEvalRunner.evaluate(items, candidate_ref, k)` 返回 `{item_id: [k 条原始输出]}`。`DiagnosticItem` 不带题干,只有 id、l3_task、item_type、scoring_mode、reference_answer、rubric。
- `diagnostic_grade`(`evidence.py:8`)按顺序判:`payload["stub"]` 为真返回 `stub`;`backends == "mock"` 返回 `mock`;`backends == "real"` 且 `dimension_scores` 为非空列表返回 `real`;其余一律 `stub`(失败关闭)。

ROLL4ZenGen(除上面列出的几项外,以下均未核对):

- `sft_lora.yaml`:8 卡,TP1/EP8,rank 64,`max_steps: 100`,`save_steps: 50`,每步全局 8 条(DP8 × 每卡 1 × 累积 1),数据键 `instruction` / `output`。文件头是否有"工程候选,真实容量与完整验收未完成"这类声明,我没读到,按未核对处理。
- 文档记录 LoRA 100 步与从 checkpoint 50 冷恢复已在 `.181` 跑过,并有检查点结构校验脚本。这是验证记录,不是验收结论。
- 导出:`LoRAHFConverter` 纯 CPU,文档记录约 29 秒、约 8 GiB 主机内存、3.5 GB、148,808 个张量,布局 `qwen4_exp_vllm_2d`。通用 HF/PEFT 无法重新加载这种布局,推理还需要原基座和冻结的 N-gram 资产。
- vLLM 是开发版 `0.1.dev20073+g8e685d198`。README 明说换成 PyPI 最新版没有验证过。文档记录原生 vLLM TP8/EP LoRA 探针通过,但那是探针,不是 `vllm serve`。

SoMEval(未核对):

- 预测阶段入口是 `run_prediction_stage`,按 `experiment_config.yaml` 的 `llm`(model_name、api_key、api_url)、`protocol`、`repeats`、`max_samples` 运行,写 `prediction.jsonl`。样本由 `normalized_datasets_path` 下的数据集决定,配置里没有按样本 id 选题的字段。
- SoMBench 有 3 个一级、17 个二级、59 个三级维度。Q4 开放题要 LLM judge,密钥与地址取自环境变量。
- 依赖 pandas、datasets、matplotlib 等,代码用 `from src.llm import ...` 这类顶层 `src` 导入。

**评审核实的本机事实。** 初稿之后,三份只读评审逐条对照代码,下面各条以代码为准,初稿与之冲突处已在对应章节改写:

- 守护进程只接住 `DirectiveFailedError`,其他异常让进程退出;仓库里没有监督脚本。
- 派发器正常返回,directive 即被置为 `executed`。
- 诊断评估器抛任何异常,守护进程都退出,重启后重发同一评测。
- `MockExecutor.checkpoint_intact` 对 `ssh://` 引用返回 False。
- 规则题判分走 `RuleScorer`(`diagnostic/scorer.py`),比较前对两侧去空白、转小写,再整串相等;`_rule_hit` 不是唯一入口。
- 两处 `_DETERMINISTIC` 差一项(`NeedsHumanError`)是有意的,但没有测试钉住。
- 晋级处理只看对应报告在场,不看等级;命令行 `review` 对 mock 报告标出 `[MOCK BACKENDS]`。
- 训练与评测都不写成本账本的 GPU 小时,规划器只看得到 token 数。
- 总览与 ControlPlane 设计都写明:接真实训练之前必须先落地异步派发契约(`submitted`)。它没有落地。

仍没读到:子系统设计里的 SSH 约定、SoMBench 样本的具体字段、ROLL 的 `summary.json` 与逐步日志格式。这些放进第 10 节作为未核实项。

## 2. 裁决总表

| 编号 | 决定 | 理由 |
| --- | --- | --- |
| D1 | 本机编排,远端以"后台作业 + 轮询"执行,不做常驻服务 | ROLL4ZenGen 只提供整段作业入口;作业可脱离 SSH 连接存活,守护进程重启后能重新接上 |
| D2 | 编排器增加可选的 `run_sft_stage`;没有该方法的执行器走原来的逐步路径 | 让 ROLL 整段作业与逐步协议对上,又不动 Mock 与现有测试 |
| D3 | 真实执行器只声明 `supported_phases = {"sft"}`;`grpo_steps > 0` 在配置校验期失败 | 一半真一半 mock 的存点没有意义,也会谎报 |
| D4 | 真实存点是 `BigRef`:`uri=ssh://<endpoint_id>/<路径>`,`sha256`=目录清单哈希,`fetch_hint.type="roll_ssh"` | 复用现有信封;跨模式续训在派发层挡住(见 §6.1) |
| D5 | 转换器调用 `LoRAHFConverter`(CPU),产物是适配器导出,`fetch_hint` 写明所需基座 | 这是文档里唯一验证过的导出路径 |
| D6 | SoMEval 以子进程运行,不在进程内导入 | 隔离重依赖与顶层 `src` 包名 |
| D7 | `backends="real"` 只在所有参与评测的组件都是真实现时才声明 | 沿用 `diagnostic_grade` 的失败关闭语义 |
| D8 | GPU 独占:主机租约 + 预检,绝不终止非本任务启动的进程 | 验证主机上有别人的服务 |
| D9 | 连接配置拆两处:路径与阈值进 git 树 `control-plane/compute/endpoints.yaml`,主机定位(ssh 别名)留本机文件;密钥只记环境变量名 | 远端路径与阈值要可追溯;主机地址因环境而异,不进仓库历史 |
| D10 | 远端只写 `work_root`;所有远端操作经一个只依赖标准库的助手脚本 | 缩小 shell 引号面,远端行为可被哈希固定 |
| D11 | 新增"延后"通道:`DeferredError` 基类,守护进程接住后不写树、状态不动、带退避;资源预检是执行器的可选方法,在 `resume_or_run` 里 `decide_resume` 之后、`run_training` 之前调用 | 现有代码里瞬态失败只能让守护进程退出,且没有监督脚本;预检放在 `run_training` 之后会先耗掉续训预算 |
| D12 | 本轮接受训练阻塞调和循环;异步派发(`submitted`)不做,列为下一轮 | 异步派发要改指令状态机与守护循环,与真实后端叠在一起风险过大;代价写明在 §4.1 |
| D13 | 评测失败分两类:确定性错误写一份带原因的 `stub` 报告,瞬态错误走 D11 的延后通道 | 评估器抛任何异常现在都会让守护进程退出并重发,确定性错误会无限崩溃循环 |
| D14 | 哈希口径分两种:转换器导出目录全量哈希;训练存点对大张量按固定分块抽样 | 导出目录的张量大小恒定,只比大小会让不同训练的产物撞同一哈希,而候选归属靠它判断;训练存点体量大,全量读代价过高 |

## 3. 远端计算层

新增顶层包 `compute`,只管"在远端主机上安全地跑一个作业",不认识训练和评测。

### 3.1 配置

配置拆成两处,按"是否需要可追溯"切。

**树内 `control-plane/compute/endpoints.yaml`。** 一份 config 信封,用 `read_tree_config` 读,与 `evaluator.yaml` 走同一条路径,凭证字段名检查随之生效。在 `controlplane/paths.py` 加一个 `compute_endpoints_file()`,和 `backends_config_file()` 并列。装配期读取,键缺失、多余或类型不对按 factory 现有约定退出码 2,报错只写键名,不回显值。读取与校验写在 `factory`(新模块 `factory/compute_config.py`),产出 `compute.ComputeEndpoint` 冻结数据类再交给 `compute`:边界测试禁止任何包导入 `factory`,而 `read_tree_config` 在 `factory.configio` 里,所以 `compute` 自己不读 git 树。`compute_endpoints_file()` 加在 `controlplane/paths.py`,由 `factory` 调用。`install_defaults` 不安装它:远端路径没有合理默认值,缺了就拒绝装配真实后端。

| 键 | 含义 |
| --- | --- |
| `endpoint_id` | 逻辑端点名,写进存点的 `uri`。它不是 ssh 别名,换主机时保持不变 |
| `work_root` | 远端唯一可写根目录。作业、租约、助手脚本、产物都在其下 |
| `roll_root` | ROLL4ZenGen 检出目录(只读使用) |
| `launch_prefix` | 启动命令前缀(argv 列表),例如进入训练容器的命令。裸机时为空列表 |
| `base_model_path` | 远端基座 HF 目录 |
| `heldout_jsonl` | 远端已就位的留出集。不由训练数据派生 |
| `sft_template` | `roll_root` 下的 LoRA SFT 配置模板路径 |
| `min_free_host_mem_gib`、`min_free_disk_gib` | 预检阈值 |
| `poll_seconds`、`job_timeout_seconds` | 轮询间隔与整段作业超时 |

**本机主机映射。** 一个 YAML 文件,路径由环境变量 `RISE_COMPUTE_HOSTS` 指定,放在仓库之外,内容是 `{<endpoint_id>: <ssh 别名>}`。别名指向 `~/.ssh/config` 里的主机条目,密钥、跳板、端口都在那里,所以两处配置都不存凭据。映射里查不到树内的 `endpoint_id` 时退出码 2,报错写环境变量名和 `endpoint_id`。换机器只改本机映射,树内记录与已有存点引用都不用动。

**与子系统设计 G.2 的关系。** G.2 的 L-B 一行把主机、端口、用户名都放进 `endpoints.yaml`。这里收窄为只放路径与阈值,主机定位退到本机,理由与 G.3 相同:地址一旦进了提交就清不掉。G.2 已按此同步修订。

SSH 一律带 `-o BatchMode=yes -o ConnectTimeout=...`。远端命令用 `shlex.join` 拼成一个字符串,文件内容走 stdin,不拼进命令行。

### 3.2 远端助手 `job_tool.py`

只依赖标准库的单文件脚本,sha256 写死在 `compute` 包里。首次使用时上传到 `<work_root>/bin/job_tool-<sha8>.py`,已存在则校验哈希。所有远端动作都经它,输入输出都是 JSON。

- `start`:按 `job_id` 建 `<work_root>/jobs/<job_id>/`,写 `spec.json`。目录已存在且 spec 哈希相同则直接返回当前状态(幂等);spec 不同则报错。用 `setsid` 起进程,输出重定向到 `stdout.log`,包装层在进程退出时写 `exit_code`。
- `status`:返回 `running`、`exited`(带退出码)或 `missing`。
- `tail`:取日志末尾 N 行。
- `stop`:只向本作业 `spec.json` 记录的进程组发信号,并核对进程启动时间戳,防止 pid 复用。不碰任何其他进程。
- `put`、`hash-tree`:带哈希的原子写入(临时文件加重命名)和目录清单哈希。`hash-tree` 带 `mode` 参数,取 `sampled` 或 `full`,口径见 §6.1。

**密钥不进命令行与 `spec.json`。** 评测服务的 `--api-key` 若写进 argv,同一主机上的其他用户用进程列表就能读到(D8:验证主机是共享的);若写进 `spec.json`,它还会进 spec 哈希,密钥一轮换就让幂等失效。做法:密钥经 stdin 传给 `start`,助手把它写成 `jobs/<job_id>/secret`(权限 0600),启动包装脚本从该文件读取并以环境变量交给服务进程,`spec.json` 只记变量名,spec 哈希不含密钥。作业结束或 `stop` 时删除该文件。

**`launch_prefix` 的信任。** 它来自 git 树里的 `endpoints.yaml`,会被原样拼在远端命令前,等于"谁能改这个文件,谁就能在远端执行任意命令"。所以装配期校验 `argv[0]`:只允许 `docker`、`podman` 或空列表(裸机),其余一律 `FactoryConfigError`;各参数逐个 `shlex.quote`,不经 shell 展开。放宽白名单要改代码并经评审。

**容器里的进程。** 所有 `job_tool` 调用都经 `launch_prefix` 在容器内执行,所以 `setsid`、进程组、信号与启动时间戳核对都在同一个 PID 命名空间里,不会出现宿主机对容器内 pid 发信号。预检的 `nvidia-smi` 在宿主机执行,不经 `launch_prefix`,因为容器内通常看不到其他容器的计算进程。这两个前提(容器内外 `work_root` 是同一路径、宿主机 `nvidia-smi` 能看到别人的进程)待主机验证,见 V15。

### 3.3 GPU 租约与预检

- 租约是 `<work_root>/gpu.lease`,用 `O_CREAT|O_EXCL` 创建,内容是持有者的作业标识:训练作业是 `job_id`,评测服务是 `serve-<sha16>`,由候选 id、存点哈希、rank 与启动包装版本算出,同一输入恒得同一标识。持有者的作业已有 `exit_code` 或进程已不在,租约视为陈旧。
- 回收陈旧租约不能"先读再删":两个申请者可能同时判定它陈旧,后到的会把先到者刚创建的新租约删掉。做法:先 `rename` 到 `gpu.lease.stale-<随机串>`,`rename` 失败说明别人已回收,按忙处理;成功后重读内容,若不是刚才判定陈旧的那个持有者,把文件原样 `rename` 回去并按忙处理;确认无误才删除,再走一次 `O_EXCL` 创建。
- 守护进程崩溃后重启:评测服务的作业标识是确定的,`start` 对同标识的运行中服务直接接上,对已退出的先清理再重启,所以不会出现重启后永久被自己上次的服务占着租约。
- 启动前预检:`nvidia-smi` 报告没有计算进程(本作业除外),空闲主机内存和磁盘达到阈值。
- 预检不过抛 `ComputeBusyError`,它是 `DeferredError` 的子类(定义见 §7.3)。它不是训练失败,也不进两个 `_DETERMINISTIC`:它沿派发器原样上抛到守护进程的延后通道(§7.4),指令保持 `pending`,run 记录一个字节都不动,守护进程按退避间隔重试。我们从不结束别人的进程,哪怕它占着 GPU。
- 预检由执行器的可选方法 `preflight()` 承担,`compute` 包只提供检查本身。同一次预检在 `resume_or_run` 里调用一次,租约在作业真正启动时才申请,两者之间别人抢占 GPU 的窗口由租约的 `O_EXCL` 兜住:申请失败同样抛 `ComputeBusyError`。

## 4. 编排器改动:可选的阶段接口

ROLL 只提供整段作业,逐步协议要求每步返回指标。对接方式是给 `TrainingExecutor` 增加一个可选方法:

```python
class StagedExecutor(TrainingExecutor, Protocol):
    def run_sft_stage(
        self, batches: list[list[Episode]], *, first_step: int, trained_slot: str,
        prior_batches: list[list[Episode]],
    ) -> list[StepResult]: ...
```

`batches[i]` 是全局第 `first_step + i` 步的数据,与逐步路径里 `batch_for_step` 取到的相同。返回的 `StepResult` 按步排列,`step` 字段必须连续。

`prior_batches[j]` 是全局第 `j` 步的数据,`j` 取 `[0, first_step)`,`first_step == 0` 时为空列表。执行器不在这些步上训练,但必须把它们写进 `train.jsonl` 的前缀:ROLL 续训时按位置跳过已消费的批次(`sft_pipeline.py` 里 `divmod(global_step, steps_per_epoch)` 之后对迭代器 `next` 若干次),前缀缺失会让续训从错误的数据位置接着训,而且不报错。数据是 step 的纯函数,编排器用同一个 `batch_for_step` 现算前缀,没有额外读盘。

`run_training` 的 SFT 阶段改成:

1. 执行器有 `run_sft_stage` 时,把 SFT 阶段切成段。段边界先取下一个存点步、SFT 结束步和 `total` 里最近的一个,再让执行器有机会把段切得更细。这个接口保留:读 ROLL 源码后,"行数一致"已不再是切段理由(见第 5.2 节第 2 步),但执行器仍可能因别的约束(如存点磁盘)要求更短的段。接口现在就定下来:

   ```python
   def stage_end(self, batches: list[list[Episode]], *, first_step: int, limit: int) -> int: ...
   ```

   `limit` 是编排器算出的段终点,开区间,即不含该步;返回值是执行器能一次吃下的终点,同样是开区间。返回值必须满足 `first_step < 返回值 <= limit`,**保证至少前进一步**,否则编排器抛 `RuntimeError`:不前进就是死循环,要响亮地失败。没有这个方法的执行器按 `limit` 处理。
   `MockExecutor` 不实现它,所以逐步路径不受影响。真实执行器暂时总是返回 `limit`。
2. 段返回后,对其中每个结果执行与逐步路径完全相同的检查和记账:`result.step != step` 抛 `RuntimeError`,`_update_summary`,`on_step`。
3. 段尾再走原来的存点判断(`done < total and done % checkpoint_every_steps == 0`),调 `save_checkpoint`。
4. 没有 `run_sft_stage` 的执行器(含 `MockExecutor`)走原路径,字节级不变。

把循环体里"收到结果之后"的那部分抽成一个内部函数,两条路径共用,避免复制出两份记账逻辑。

行为差异,都要写进代码注释和测试:

- `on_step` 在一段结束后成批到达,不再实时。实时进度看远端日志。
- 段中途崩溃,回退到上一个存点。这与逐步路径一致,因为存点本来就是回退边界。
- 作业非零退出时,执行器抛 `RemoteJobFailedError`,它是 `DeferredError` 的子类,不入两个 `_DETERMINISTIC`。此时 `resume_attempts` 已在进入 `run_training` 时落盘加一,run 保持 pending。派发器的通用异常分支见记录仍是 pending,原样重抛(§7.4),守护进程按 D11 退避后重试,指令保持 pending;重试预算仍是 `max_resume_attempts`,超限由 `decide_resume` 升级给人。与 `ComputeBusyError` 的区别只在这里:忙是预检阶段抛出、不动 run、不耗预算;作业失败发生在训练中、已耗一次预算。

### 4.1 训练阻塞调和循环的代价(D12)

本轮不做异步派发。派发器同步跑完整段训练,调和循环在这期间不处理任何其他指令。代价逐项写明,实现与运维都按此预期:

| 影响 | 表现 | 本轮的处理 |
| --- | --- | --- |
| 其他指令等待 | 一段训练数小时,期间评测、审核后的晋级、规划都不推进 | 接受。单端点、单租约,同一时刻本来只有一个 GPU 作业 |
| 进程被杀 | 守护进程在轮询中被杀,远端作业继续跑;重启后以同一 `job_id` 的 `start` 接上正在跑的作业 | 靠 D1 的幂等 `start`,不丢进度 |
| 无实时进度 | `on_step` 成批到达 | 看远端日志与 `job_tool tail` |
| 超时 | `job_timeout_seconds` 到期先 `stop` 再抛 `RemoteJobTimeoutError` | 计入 `resume_attempts` |

异步派发的前置条件已在 ControlPlane 设计里写明:指令状态机新增 `submitted`,守护循环按轮询推进。它要改指令信封与守护循环,与本设计的改动面不重叠,所以放下一轮。判错的代价:若真实训练一段超过一个工作日,阻塞会让人工审核后的晋级长时间不生效,此时必须先做异步派发。

## 5. RollExecutor

放在 `mindtrainer/roll_executor.py`,依赖 `compute`。`executor_factory` 是闭包,装配期绑定好树内 `endpoints.yaml` 与本机主机映射。

### 5.1 协议实现

- `supported_phases = frozenset({"sft"})`。`run_training` 开头检查:执行器声明了 `supported_phases` 且 `cfg.grpo_steps > 0` 而 `"grpo_opd"` 不在其中时,抛 `InvalidTrainingConfigError`(确定性失败,run 记为 failed)。真实模式需要配套的 `training_defaults.yaml`,其中 `grpo_steps: 0`。
- `preflight()`:可选方法,返回 `None`,不通过抛 `ComputeBusyError`。内容是 §3.3 的 GPU、内存、磁盘检查,不取租约、不写远端。`resume_or_run` 在判定续训之后、调 `run_training` 之前调用(D11)。`MockExecutor` 没有这个方法,用 `getattr` 探测,缺省视为通过,逐步路径与现有测试不变。
- `load(ref)`:接受两类引用。(a) 起点基座,必须指向 `base_model_path`,manifest 哈希与引用一致;(b) 本设计产出的训练存点,先过 `checkpoint_intact`,不通过抛 `CheckpointCorruptError`。`mock://` 引用一律拒绝。`load` 只做校验和记录,不占 GPU。
  **多轮闭环的缺口(本轮不做)。** `executor.py:88` 写明晋级后下一轮训练的基座就是转换后的那份,而真实转换产出的是 `qwen4_exp_vllm_2d` 适配器导出,不是 HF 训练起点。ROLL 能否以它为训练起点,`mindtrainer` 设计 §10 本来就列为未核实。所以真实后端本轮**只支持单轮**:第二轮的基座仍要人工指定为 `base_model_path`,`load` 收到适配器导出引用时抛 `InvalidTrainingConfigError` 并说明原因,不静默接受。这条写进不做清单。
- `sft_step`、`grpo_opd_step`:抛明确的异常。真实执行器只能走阶段路径,逐步调用说明编排器被改坏了,要响亮地失败。
- `save_checkpoint(tag)`:ROLL 的存点在阶段内部写出,这里把该阶段产出的存点目录封成 `BigRef`,不另外触发保存。有两种情况没有本次阶段产出,都要处理:
  - **循环为空。** 续训时若 `start == total`(崩在最终存点之后、置 `executed` 之前),一步都不会跑,但 `orchestrator.py:226` 仍无条件调 `save_checkpoint("final")`。此时要从 run 记录的 `last_checkpoint_ref` 回认那份已有存点,并校验它仍然完整;认不出就抛 `CheckpointCorruptError`,绝不返回一个没有对应目录的引用。
  - **段被切细(目前真实执行器不会触发,保留以备 `stage_end` 返回更短的段)。** 子段末尾的 ROLL 存点不是编排器的存点步(`orchestrator.py:209` 只在存点步回写),但它是下一子段的续训输入。这类中间存点由执行器自己记在作业目录下,不进 git 树;整个阶段成功后才可以清理,清理范围严格限定在本 `run_id` 的作业目录内。
- `checkpoint_intact(ref)`:经 `job_tool hash-tree` 核对 manifest,并检查 ROLL 的结构文件齐全(tracker、迭代目录、每个 worker 的 RNG、scheduler、适配器配置)。只能证明结构完整,不证明张量没有坏。manifest 用 `sampled` 口径(§6.1)。SSH 或远端助手的传输错误一律抛 `TransportError`(`DeferredError` 子类),**不返回 False**:`False` 会让 `decide_resume` 升级成 `NeedsHumanError`(T3,永久),一次网络抖动不该把 run 判死。只有核对完成且结构确实不符才返回 False。

### 5.2 `run_sft_stage`

1. 渲染数据,**多轮监督**(已定)。被训练角色位在一个剧集里说了 T 句,就渲染出 T 行 `{"instruction", "output"}`:第 t 行的 `output` 是该角色位的第 t 句,`instruction` 是这句之前的全部发言按固定模板拼成的文本。每一句都被监督,它之前的上下文只作为输入,不参与 loss。
   用行展开而不是单次前向加 loss mask,是因为 `sft_lora.yaml` 只有 `prompt_key/response_key` 这一对键,表达不了逐轮掩码。代价要写明:同一段上下文会被重复前向,token 量随 T 平方增长;这在语义上等价于逐轮掩码,在计算上不等价。真正的单次前向掩码需要在 ROLL 侧自定义数据管线与模板,本轮不做。
2. 批次口径(**已按 ROLL 源码核实后重写**)。编排器的步数由配置定死:`total_steps(cfg) = sft_steps + grpo_steps`,`phase_at` 也按 `sft_steps` 切阶段(`training_config.py:448`、`452`),所以步数不能由数据行数反推。一步仍然是 `batch_for_step(episodes, step, sft_batch_size)` 取到的那几个剧集。
   ROLL 侧的事实(`sft_pipeline.py:180`、`step_planning.py`):全局批次 `G = dp_size × ga_steps × per_device_bs` 是配置里的**常数**,DataLoader 每步固定取 `G` 行,`shuffle=False, drop_last=True`;`gradient_accumulation_steps` 整个作业只有一个值。所以逐步变化的行数、按步改 ga,都做不到。
   因此约束是:**每个编排步展开出的行数必须恒等于 `G`。** 行展开是把该步的剧集逐个展开求和:行数 = Σ(各剧集被训练角色位的句数)。默认数据生产器里每集的轮数固定为 `max_turns`(`data_producer.py` 的 `[trained_line … ] * max_turns`),所以 T 恒定,行数 = `sft_batch_size × T`。于是配置期就能算准:`sft_batch_size × T == G`。其中 `G` 取模板里 `dp_size × per_device_bs`(DP8、每卡 1,即 8)乘以渲染时选定的 `ga_steps`。
   **这条在训练前一次性预检,不能等渲染到某一段才发现。** 数据是 step 的纯函数,`[0, sft_steps)` 上每一步的行数在动执行器之前就能全部算出来。预检放在 `run_training` 调 `executor.load` 之前,对照 GRPO 的分组预检也在循环之前(`orchestrator.py:54`、`171`),理由一样:数据有问题时一步都不浪费。预检不过一律确定性失败(`InvalidTrainingConfigError`),报出实际行数与 `G`,让人改 `sft_batch_size` 或 `max_turns`。**不补齐也不丢行**:补齐会凭空造监督样本,而丢行恰是 `drop_last=True` 会静默做的事,所以预检要替 ROLL 先挡住。
   interact 轨迹长短不一,每步行数无法恒定,本轮不支持真实后端下的 interact 模式,写进不做清单。
   原先的"段按行数再切细"整个删掉:一个作业只有一份 ga 配置,切细不能让不同行数的步共存。第 4 节里 `stage_end` 接口保留(它仍可用于别的约束,如磁盘),真实执行器暂时总是返回 `limit`。
   **截断必须预检。** `DataCollatorForSFT` 在 `max_length` 处把 `input_ids` 与 `labels` 一起截断,不报错不警告(`collator.py:900` 起)。行展开越往后的行上下文越长,最容易被静默截断。预检要用 tokenizer 实测每一行的token 长度,超过 `--sequence-length` 就确定性失败。tokenizer 与聊天模板必须和 ROLL 用的是同一份(`get_chat_template(global_template, tokenizer)`),否则预检的长度和实际不一致。
   模板拼接也影响长度与掩码:`get_encode_function` 把 `prompt + ("\n" + query)` 放进 user 消息,response 放进 assistant 消息,prompt 部分的 labels 设为 `IGNORE_INDEX`,只监督 response。我们的 `instruction` 因此会被包成单轮 user 消息,而不是还原成多轮对话——上下文里的"对手发言"和"被训练角色位的前几句"都被压成一段 user 文本。这与"多轮监督"的语义是否等价,取决于你要的是"每句都被监督"还是"保留多轮对话结构":本设计满足前者,不满足后者。
3. 数据按步序排列成 `train.jsonl`:先是 `prior_batches` 渲染出的行(步 `[0, first_step)`),再是 `batches` 渲染出的行(步 `[first_step, last_step)`)。哈希覆盖整份文件,所以 `job_id` 随前缀变化。行数与截断预检对前缀行同样适用。
4. 作业身份 `job_id = sha256(run_id, 起点存点 sha256, first_step, last_step, 数据哈希, 模板哈希, launcher 版本)[:16]`。约定 `last_step` 与 `stage_end` 的返回值一致,是**开区间**终点,所以这一段覆盖的是 `[first_step, last_step)`,共 `last_step - first_step` 步。`run_id` 与起点存点必须进哈希,否则起点不同而数据相同的两次续训会撞同一目录。
   **幂等与重试要分开。** `start` 对「目录已存在且 spec 哈希相同」直接返回当前状态,这让守护进程崩溃后能接上**正在跑**的作业;但同一条规则会让**已失败**的作业永远重跑不了:重试时 `start` 命中那个已退出的目录,`status` 仍报非零退出,于是再抛一次同样的异常,直到 `resume_attempts` 触顶升级给人。OOM 或被抢占这类瞬态失败就恢复不了。
   所以作业目录名带尝试序号 `<job_id>-a<N>`,而 `job_id` 仍只由确定输入算出:
   - `start` 先找该 `job_id` 下最大的尝试序号。该尝试仍在跑就接上(幂等,崩溃恢复靠这条);已退出且退出码为 0 就直接返回成功。
   - 已退出且非零,或曾被 `stop`,就新建下一个尝试序号。旧目录保留不删,留作证据。
   - 尝试序号的上限与 `max_resume_attempts` 分开记账,见第 7.3 节。
5. 启动命令是 `run_sft_validation.py`,参数为 `--config <sft_lora 模板> --model-path --output-dir --train-data --heldout-data --max-steps <last_step> --save-steps <last_step> --eval-steps <last_step>`,`first_step > 0` 时加 `--resume <pipeline/checkpoint-{first_step-1} 的绝对路径>`(V9 已核实:带路径参数,指向含 `pipeline/` 子目录的那一级)。`--max-steps` 取 `last_step`(绝对步数,V2 已核实),`--save-steps` 也取 `last_step`,使 ROLL 恰好在段尾写存点。注意两点:(a) 存点编号是 `checkpoint-{global_step}`,`global_step` 是该步的零基索引,所以段尾存点叫 `checkpoint-{last_step - 1}`;(b) `do_checkpoint` 里存点条件是 `global_step > 0 且 (global_step % save_steps == 0 或 global_step == max_steps - 1)`,最后一步总会存。
   步数口径要小心:`sft_lora.yaml` 里 100 步是由 "400 条 / DP8 × 2 个 epoch" 推出来的,配置按 epoch 走。脚本另有 `--train-records`、`--heldout-records`、`--sequence-length`、`--checkpoint-output-dir`、`--checkpoint-hard-links`,含义都没读。所以 `--max-steps` 是否真能截断到段尾、与 epoch 数怎么相互作用,要先验证(V2、V10)。我印象里该脚本有"工程验证入口,不是模型支持声明"一类的声明,没读到原文,先按未核对处理;确认有的话,要在日志和报告里保留。
6. 轮询 `status`,直到 `exited`。退出码非零抛 `RemoteJobFailedError`,附日志末尾。超过 `job_timeout_seconds` 先 `stop` 再抛 `RemoteJobTimeoutError`。
7. 收集逐步指标,转成 `StepResult(step, "sft", {"sft_loss": ...})`。
   **step 不能按位置合成。** 若直接写 `first_step + i`,`orchestrator.py:200` 的 `result.step != step` 检查就恒为真,而它本来是用来发现执行器与 run 记录不同步的,等于把这道校验废掉。真实后端恰恰最需要它,因为 ROLL 实际从哪一步起训要靠 `--resume` 的语义(V2、V9)才能确定。
   所以 step 要从 ROLL 自己的记录里读:取存点 tracker 或日志里的 global step,与 `first_step` 对账,不一致抛 `MetricsMissingError`。读不到 ROLL 的 global step 时同样失败,不退化成按位置编号。
   指标条数与 `len(batches)` 不符也抛 `MetricsMissingError`。另外 `_update_summary`(`orchestrator.py:83`)直接读 `metrics["sft_loss"]`,缺键会是 `KeyError`,所以这个异常要同时覆盖条数不对和缺 `sft_loss` 键两种情况。

**数据顺序约束(已核实)。** 编排器的续训等价依赖"第 i 步的数据是 i 的纯函数"。ROLL 的 DataLoader 是 `shuffle=False`,按文件顺序取整批,所以成立,前提是 `train.jsonl` 里的行序就是步序、且总行数是 `G` 的整数倍。续训时 ROLL 用 `divmod(global_step, steps_per_epoch)` 跳过已消费的批次,并从存点里的 RNG 文件恢复随机状态(`sft_pipeline.py:244`–`265`),对确定性数据没有额外影响。

## 6. 检查点与转换器

### 6.1 存点引用

`BigRef(uri="ssh://<endpoint_id>/<work_root>/jobs/<job_id>/<目录>", sha256=<manifest 哈希>, size_bytes=<总字节>, fetch_hint={"type": "roll_ssh", "format": "roll_lora_legacy", "job_id": ...})`。

manifest 哈希按用途分两种口径(D14),`hash-tree` 带 `mode` 参数:

| 口径 | 用在 | 内容 | 能发现 | 发现不了 |
| --- | --- | --- | --- | --- |
| `sampled` | 训练存点 | 排序后的 `(相对路径, 字节数)` 列表,加所有小文件(json、txt、小于 64 MiB 的 pt)的内容哈希,加每个大张量分片的固定三块(头、中、尾,各 1 MiB)内容哈希 | 结构变化、截断、整块替换 | 三块之外的位腐烂 |
| `full` | 转换器导出目录 | 排序后的 `(相对路径, 字节数, 全文件 sha256)` 列表 | 任何字节变化 | 无 |

分两种的原因:LoRA 适配器的张量形状只由 rank 和层数决定,两次不同训练的导出目录文件名与字节数完全相同。若只记大小,它们的哈希会相同,而候选归属靠 `checkpoint_ref.sha256` 判断,会把两个候选认成同一个。导出目录约 3.5 GB,转换时本来就要整体写一遍,全量哈希多读一遍可以接受。训练存点含几十 GB 的优化器状态,每次存点都全量读代价过高,抽样足以挡住截断与误拷贝,位腐烂另由 `checkpoint_intact` 的结构检查之外的离线校验承担,本轮不做。判错的代价:训练存点的抽样漏掉三块之外的损坏,续训在 ROLL 加载处才暴露,表现为 `RemoteJobFailedError` 并耗续训预算。

**跨模式续训要挡住,但不能靠改 `MockExecutor`。** 现状已核实两点。第一,`MockExecutor.load` 对非 `mock://` 的引用走 else 分支,当作外部起点。第二,续训路径上 `orchestrator.py` 先用 `checkpoint_intact` 校验 `last_checkpoint_ref`,而 `MockExecutor.checkpoint_intact` 对 `ssh://` 引用返回 False,所以真实 run 被 Mock 续训时,`decide_resume` 看到存点不完整,直接升级成 `NeedsHumanError`(T3),不会拖到第一步才报错。结果是被拦住了,但归类是"需人工",理由字段是"存点损坏",与真实原因"后端模式不一致"不符。要让 Mock 给出准确原因就得改 Mock,这与 D2(Mock 字节级不变)冲突。

所以在 `resume_or_run` 开头、`checkpoint_intact` 之前补一道前置检查,写法同 `run_training` 里配置校验失败的那一段(先把 run 写成 failed 再抛,让 run 与 directive 一致):续训前比对 `last_checkpoint_ref` 的 `fetch_hint["type"]` 与当前执行器的类型,不一致就抛 `InvalidTrainingConfigError`,message 写明两侧类型(确定性,run 记 failed,directive 判 T2)。这道检查在 `checkpoint_intact` 之前,所以原因准确。反方向同理:`RollExecutor.load` 收到 `mock://` 引用时抛 `CheckpointCorruptError`,并由 §7.3 把它归入确定性失败。判错的代价:缺这道检查,运维看到的是"存点损坏"的 T3,会去查远端磁盘而不是查装配开关。

### 6.2 RollLoraConverter

实现 `CheckpointConverter.convert(ref) -> BigRef`:

1. 在远端 CPU 上跑 `LoRAHFConverter`,`CUDA_VISIBLE_DEVICES=` 置空,不占 GPU 租约,但要检查主机空闲内存(文档记录峰值约 8 GiB)。
2. 目标目录必须是全新的。导出到 `exports/<存点 sha8>.tmp/`,成功后用 `full` 口径(§6.1)算导出目录哈希,连同完成标记一起写入,再原子重命名为 `exports/<存点 sha8>/`。已存在带标记的目录则重新核对哈希,一致就返回对应引用(幂等),不一致抛 `CheckpointCorruptError`;只有未完成的 `.tmp` 才会被清理,清理范围严格限制在自己创建的 `.tmp` 目录。返回的 `BigRef.sha256` 就是这份 `full` 哈希。
3. 返回的 `BigRef.fetch_hint` 写明 `{"type": "roll_ssh", "format": "qwen4_exp_vllm_2d", "base_model_path": ..., "requires": "native-vllm-lora"}`。通用 HF/PEFT 不能加载这个布局,评测侧必须读到这个声明。
4. 引用 uri 不带 `.hf` 后缀。已核实:只有 `MockConverter.convert` 看 uri 后缀(`checkpoint.py:32`),`_is_inference_format` 看的是 `fetch_hint["format"] == "hf"`(`executor.py:64`)。真实转换器写 `format: "qwen4_exp_vllm_2d"`,所以不会被 `_is_inference_format` 判成推理格式,这正是上面 I9 那条缺口的来源。

## 7. 评测:vLLM 服务与 SoMEval

### 7.1 服务生命周期

`RealSoMEvalRunner.evaluate(items, candidate_ref, k)` 内部按顺序执行:

1. 取 GPU 租约并预检(同 3.3)。训练作业退出后才会走到这里,租约保证两者不并发。
2. 经 `job_tool start` 在容器里启动推理服务。**不能直接用裸 `vllm serve`**(读源码得出):ROLL 让 vLLM 能加载这套 LoRA 导出,靠的是两个进程内补丁,而不是 vLLM 包本身——`patch_vllm_lora_manager()` 替换 `LRUCacheWorkerLoRAManager._load_adapter`(`roll/third_party/vllm/vllm_utils.py`、`worker.py:220`),`patch_qwen38_lora_shrink` 把 Triton 的 `split_k` 强制成 1 以保证 LoRA 推理可重复(`worker.py:63`)。裸 `vllm serve` 不会打这两个补丁。
   两条路,倾向第一条:(a) 一个启动包装脚本,在容器里先 `import` 并调用这两个补丁,再把控制权交给 vLLM 的 OpenAI 兼容服务入口,引擎参数照搬 ROLL 的 `enable_lora=True, max_loras=1, max_lora_rank=<rank>`(`vllm_strategy.py:350`);(b) 评测改走 ROLL 的 worker。(b) 要把 ROLL 的 Ray 集群生命周期拉进来,代价大得多。无论哪条,TP/EP 等并行参数照搬原生 vLLM LoRA 探针。**这一步能否跑通仍要主机实测**:补丁打上之后,HTTP 层的适配器加载是否真的走到被替换的 `_load_adapter`,读源码判断不了,见 V3。
3. 服务只绑定远端回环地址,并带 `--api-key`(密钥取自环境变量,配置里只记变量名)。本机经 `ssh -L 127.0.0.1:<随机端口>:127.0.0.1:<远端端口>` 访问。不要监听外网卡:vLLM 的 HTTP 接口没有别的访问控制。
4. 轮询 `/v1/models`,直到适配器出现。超时则 `stop` 并抛 `ServeStartupError`。
5. 评测结束,无论成败,`stop` 服务、关隧道、释放租约。

### 7.2 SoMEval 调用

- 以子进程运行 `run_prediction_stage`,工作目录是 SoMEval 检出目录。配置是我们生成的临时 `experiment_config.yaml`:`llm.model_name` 取适配器名,`llm.api_url` 指向隧道地址,`llm.api_key` 取环境变量,`repeats` 取 k。
- SoMEval 按数据集选样,配置里没有按 id 选题的字段;`DiagnosticItem` 又不带题干。所以需要一份 `item_map.json`(`item_id` 到 SoMBench 样本)。我们据此生成子集数据集,让 `normalized_datasets_path` 指向它。
- **`candidate_ref` 是 `str`,不是 `BigRef`。** 协议把它声明为字符串(`someval_adapter.py:12`),评估器传进来的是 `candidate_id`(`evaluator.py:99`)。所以运行器拿不到 uri 与 `fetch_hint`,必须自己按 `candidate_id` 去读 `candidate_checkpoint.yaml` 产物,从 `payload["checkpoint_ref"]` 取回 `BigRef`,再从它的 `fetch_hint` 读 `base_model_path` 与 `requires`。这要求运行器持有 `GitStore`,构造时注入。
- **映射缺失的 item 没有表达通道,只能失败。** `run_static_items` 对每个 item 直接 `raw[item.id]`(`someval_adapter.py:77`),缺键就 `KeyError`;协议的返回类型只有 `dict[str, list[str]]`,没有地方放那几道没评上的题。所以缺失 item 一律抛 `UnmappedItemsError`,附缺失清单,不静默丢弃也不假装评过。它是确定性失败,评估器按 §7.3 捕获并写 `stub` 报告。
- **规则题的判分走 `RuleScorer`**(`diagnostic/scorer.py`),`someval_adapter.py` 里的 `_rule_hit` 只是其中一个调用点。比较方式已核实:两侧都先去空白、转小写,再做整串相等,不是包含匹配。所以真实模型的自由文本只有在抽取后恰好等于 `reference_answer` 时才得分。SoMEval 抽取出的答案必须先归一化成与 `reference_answer` 同一形状再回填(单个选项字母、或与参考答案同写法的短串),否则真实评测的规则题分数恒为零,没有意义。这一步由 `item_map.json` 的每条记录带 `answer_shape` 字段驱动,V14 据此收窄为核实默认题库的 `reference_answer` 形状。
- 读 `prediction.jsonl`,按样本 id 回填成 `{item_id: [k 条原始输出]}`。某个 item 的输出条数不等于 k,抛 `ItemOutputCountError`(确定性,同样落成 `stub` 报告)。
- `item_map.json` 的来源是本设计的一个假设:题库构建步骤提供它。如果默认题库和 SoMBench 样本之间没有 id 对应关系,这一节要重做,见第 10 节 V6。

### 7.3 失败分类

先说清现状,因为下表的多数条目都需要改代码才成立。`_DETERMINISTIC` 有两处,**都只管训练派发路径**:

- `orchestrator.py:36`,五项:`DatasetIntegrityError`、`DegenerateGroupError`、`AttributionError`、`NotEnoughGroupsError`、`InvalidTrainingConfigError`。命中则把 training_run 写成 failed。
- `dispatch.py:36`,六项:上面五项再加 `NeedsHumanError`。命中则映射成 `DirectiveFailedError`,并按 `NeedsHumanError` 判 T3、其余判 T2。

**两处要一起改。** 只加 orchestrator,directive 的归类会走通用分支;只加 dispatch,directive 判了 failed 而 training_run 还是 pending,两份记录不一致。所以新增的训练侧确定性异常,两个元组都要列入,不是按需。

| 异常 | 现状 | 本设计要的语义 | 要动的代码 |
| --- | --- | --- | --- |
| `ComputeBusyError` | 新增,`DeferredError` 子类 | 等资源,**不耗续训预算**,不写树 | `resume_or_run` 预检与延后通道,见下方 |
| `RemoteJobFailedError`、`RemoteJobTimeoutError` | 新增,`DeferredError` 子类 | pending,可重试,计入 `resume_attempts` | 不入两个元组;派发层映射成延后 |
| `ServeStartupError` | 新增,`DeferredError` 子类 | 评测瞬态失败:不写报告,directive 保持 pending,退避重试 | 守护进程的延后通道 |
| `UnmappedItemsError`、`CandidateUnresolvableError`、`ItemOutputCountError` | 新增 | 评测确定性失败:写带原因的 `stub` 报告 | 评估器内部捕获,见下方 |
| `InvalidTrainingConfigError` | 已是确定性 | failed | 无 |
| `CheckpointCorruptError` | **不在任何元组里**,且 `executor.load` 在 `try` 之外(`orchestrator.py:163`),所以现在是原样上抛、run 保持 pending | failed | 两个元组都要加,且 `load` 要移进 `try` 或另行捕获 |
| `MetricsMissingError` | 新增 | pending:远端作业已成功,重试只重新解析它的产物,不重训;计入 `resume_attempts` | 不入两个元组,见 §7.4 |

**阶段路径的调用必须放进现有的 `try`**(`orchestrator.py:170` 到 `218`)。放在外面,新增的确定性异常就拿不到 failed 记录。

**失败点字符串。** 逐步路径把 `point` 设成 `f"step {step} ({phase})"`(`orchestrator.py:176`),它会写进 failed 记录的 `failure.point`。阶段路径一次跨多步,所以改成 `f"sft stage [{first_step}, {last_step}) "` 再按情况接上 `rendering`、`remote job` 或 `metrics`,让人从记录里看出失败发生在渲染、远端作业还是回收指标。段内逐个结果记账时,仍按逐步路径的格式设 `point`,这样段中途的对账失败能定位到具体哪一步。

**延后通道(D11)。** 现状是:守护进程只接住 `DirectiveFailedError`(指令判失败),其他异常让进程退出,靠外部重启,而仓库里没有任何监督脚本。瞬态失败要"保持 pending 并重试",现有代码做不到。新增契约:

| 部分 | 内容 |
| --- | --- |
| 异常基类 | `DeferredError(Exception)`,带 `reason: str` 与可选 `retry_after_seconds: int`。放在 `controlplane/deferred.py`(无其他依赖的小模块),`compute`、`mindtrainer`、`diagnostic` 都从这里导入;`ComputeBusyError`、`TransportError`、`RemoteJob*Error` 定义在 `compute` 里并继承它 |
| 守护进程 | `reconcile_once` 对每个动作调用 `apply_action` 时接住 `DeferredError`(`dispatch_directive` 与 `request_diagnostic` 两类动作都可能抛):不写树、不改状态;退避表以 `(动作种类, 目标 id)` 为键,记下次尝试时间(默认退避 30 秒,取 `retry_after_seconds` 与默认值的较大者,上限 15 分钟),退避期内跳过该动作;同一 tick 的其他动作照常处理。`reconcile_once` 返回的仍是 `reconcile` 给出的完整动作列表,契约不变;退避表由 `run_daemon` 持有并传入,单次调用不传则只接住不退避 |
| 持久性 | 退避表只在内存。守护进程重启后所有指令立即可重试,代价是重启后多一次预检或多一次轮询,没有数据后果 |
| 可观测 | 每次延后写一行结构化日志:指令 id、异常类名、`reason`、下次尝试时间。不进树 |

**预检放在 `resume_or_run`。** 每次带 `resume_from` 进入 `run_training`,都先把 `resume_attempts + 1` 落盘,之后才碰执行器;验证主机上常有别人的服务,忙是常态,几次忙就会耗尽 `max_resume_attempts` 并升级成 `NeedsHumanError`,而这不是真的失败。方案 (b) 加 `busy_attempts` 计数要改产物 schema,弃用。方案 (a) 的字面做法"预检失败直接返回"不可行:派发器正常返回,directive 就被置为 `executed`,指令会丢。

可行形态:在 `resume_or_run` 里,`decide_resume` 判定 resume 或 restart 之后、调 `run_training` 之前,若执行器有 `preflight` 方法就调用它;新运行(没有既有记录)同样在 `run_training` 之前调用;`noop` 与 `escalate` 不触发预检。预检抛 `ComputeBusyError`,经延后通道传到守护进程;此时 run 记录没有被写过,幂等与 pending 语义都不受影响。预检通过后,租约申请失败(别人在两步之间占了 GPU)同样抛 `ComputeBusyError`,但这时 `resume_attempts` 已加一,所以窗口要小:租约在 `start` 之前的最后一步申请,不在渲染数据之前。

**评测失败的落点(D13)。** 现状是:`diagnostic/orchestrator.py` 与评估器对异常没有任何捕获,守护进程在 `evaluate` 抛出异常时退出;重启后该候选仍没有报告,`reconcile` 再次给出同一个 `request_diagnostic`,评测被原样重发。确定性错误(数据映射缺失、候选无法解析)会因此无限崩溃循环,每轮还占着 GPU 租约。新的归类:

| 失败 | 类 | 处理 | 留痕 |
| --- | --- | --- | --- |
| `UnmappedItemsError`、`CandidateUnresolvableError`、`ItemOutputCountError` | T2 | 评估器内部捕获,写一份 `stub: true` 的报告,载荷带 `failure_reason` 与缺失清单;`request_diagnostic` 动作正常结束 | 报告产物本身,等级为 `stub`;闭环里记录、提名、规划下一轮都要求证据可接受,所以不推进;晋级本身仍只看报告在场,由人工审核门拦(§8.1) |
| `ServeStartupError`、隧道建立失败、`ComputeBusyError` | T1 | 抛 `DeferredError` 子类,守护进程退避重试;不写报告 | 日志行 |
| 评估器里其他未预期异常 | 未分类 | 不捕获,沿用现状(守护进程退出) | 进程日志 |

写 `stub` 报告而不是让动作一直失败,是因为报告是评测的结论载体,且 `diagnostic_grade` 对 `stub: true` 已经失败关闭。报告由 `store_adapter` 里新增的 `write_failure_report(store, run_id, candidate_id, *, reason, detail)` 写入,路径同 `diagnostic_report_file(run_id)`,形状沿用 `simulator/stubs.py` 的占位信封(`stub: True`、`candidate_ref`、`benchmark_version`、`dimension_count: 0`),另加 `failure_reason` 与 `failure_detail`。判错的代价:若把确定性错误也当瞬态,守护进程无限重试同一个必然失败的评测,GPU 租约被反复占用。

### 7.4 前置检查的归宿与连带修正

**前置检查写在编排器里,不写在派发器里。** `supported_phases`、行数与截断预检、跨模式续训三类检查,失败时都要让 training_run 与 directive 同时变成失败。只在派发器里抛,directive 判 failed 而 training_run 仍是 pending,两份记录不一致,与上文对两处元组的警告同类。所以放进编排器,写法同 `run_training` 里配置校验失败的那一段:先 `write_training_run` 写 failed,再抛 `InvalidTrainingConfigError`。

| 检查 | 位置 | 理由 |
| --- | --- | --- |
| 跨模式续训 | `resume_or_run` 开头,`checkpoint_intact` 之前 | `checkpoint_intact` 对另一种后端的引用返回 False,会抢先升级成 T3 并给出不符的原因 |
| `supported_phases` | `run_training`,紧跟配置校验 | 与配置校验同属"配置不合法" |
| 行数与截断预检 | `run_training`,`executor.load` 之前,经执行器的可选方法 `validate_sft_data(batches, *, trained_slot)` | 数据有问题时一步都不浪费;tokenizer 在执行器手里。`batches` 是 `[0, sft_steps)` 全部步的数据,由编排器按纯函数算出 |

`validate_sft_data` 与 `preflight`、`run_sft_stage` 一样是可选方法,`MockExecutor` 没有,编排器用 `getattr` 探测。

**`DeferredError` 的穿透。** 派发器的通用异常分支先读 training_run:记录已 failed 就包成 `DirectiveFailedError`,否则原样重抛。`DeferredError` 走后一条:记录是 pending,原样重抛到守护进程的延后通道,派发器不用改。

**`executor.load` 移进 `try`。** `CheckpointCorruptError` 要拿到 failed 记录,`load` 必须在 `try` 里(同 §7.3 表)。

**尝试序号不另设上限。** 新的 `<job_id>-a<N>` 只在上一尝试失败后产生,而每次重新进入 `run_training` 都先把 `resume_attempts` 加一,所以 N 至多是 `max_resume_attempts + 1`,由 `decide_resume` 的上限兜住。`ComputeBusyError` 不产生新尝试。

**`MetricsMissingError` 判 pending,不判 failed。** 抛它时远端作业已经成功,判 failed 会作废一段数小时的训练,人只能重写指令重训。判 pending 后,重试时 `start` 命中已成功退出的尝试直接返回(§5.2 第 4 步),只重新解析产物,没有 GPU 开销;若解析逻辑确定性地错,它会耗尽 `resume_attempts` 升级给人,代价是几次廉价的重试。

**两个元组的关系用测试钉住。** `dispatch.py` 的元组等于 `orchestrator.py` 的元组加 `NeedsHumanError`,任何一边新增而另一边漏掉,测试失败(§9)。当前没有任何测试引用这两个元组。

## 8. 证据等级

`diagnostic_grade` 失败关闭:`backends` 不是 `"real"` 或没有分数,一律不算 real。这个设计里的真实路径覆盖执行器、转换器和 SoMEval 推理,但 Q4 开放题需要 LLM judge,而 RealJudge 不在范围内。

**已定:方案 B,保持混合,等级不升。** 全部 item 都评,judge 部分仍走 `MockJudgeBackend`,`backends` 传 `"mock"`,`diagnostic_grade` 与下游门控一行不改。这一轮因此拿不到 real 等级的报告,闭环的提名动作仍然不会被真分数驱动。这是有意的:宁可报告的等级偏保守,也不让一份含 mock judge 的报告占用 real 的含义。

但"等级是 mock"不等于"什么都没变",报告必须能说清哪几个环节已经是真的。所以诊断报告载荷里增加一个只作记录、不参与判级的字段:

```
component_backends: {executor: "real", converter: "real", someval: "real", judge: "mock"}
```

`diagnostic_grade` 不读这个字段,所以等级判定不受影响;人和后续的 real 升级则能据此知道缺口只剩 judge。

**值从哪里来。** 诊断侧并不知道训练用的是什么执行器,所以 `executor` 与 `converter` 两项要从`candidate_checkpoint.yaml` 的 `checkpoint_ref.fetch_hint` 推:`type == "roll_ssh"` 即训练与转换都是真实的,`type == "mock"` 即都是 mock。运行器本来就要读这份产物取 `BigRef`(第 7.2 节),顺带把这个判断一起做掉。`someval` 与 `judge` 两项由评估器装配时直接声明。

加在哪里已核实:`diagnostic/store_adapter.py:16` 的 `write_report(store, run_id, report, *, backends)` 做 `payload = report.to_dict()` 再 `payload[BACKENDS_KEY] = backends`,报告与标记同一次写入落盘。`component_backends` 按同样方式加在这里,多一个关键字参数,不进 `DiagnosticReport` 数据类,也不动 `run_diagnostic`(它在 `diagnostic/orchestrator.py:202` 调用 `write_report`)。

### 8.1 mock 与 stub 报告的晋级

晋级处理只看对应报告是否在场,不看等级,所以一份 `mock` 或 `stub` 报告在技术上可以被晋级。本轮不改这条:它是 ControlPlane 的既有行为,改它会动审核门的语义。现有的缓解是人工审核门:命令行 `review` 对 mock 报告标 `[MOCK BACKENDS]`,对 stub 报告标 `[STUB]`(`cli/src/commands.ts` 的 `summarizeDiagnostic`),人批准之前能看到。现状的缺口:stub 分支只打印"placeholder"和 `candidate_ref`,不打印失败原因。本设计新增的失败报告(§7.3)带 `failure_reason`,所以 `summarizeDiagnostic` 的 stub 分支要加一行打印它,批准人才能区分"占位"和"评测没跑成"。这是本设计在 TypeScript 侧需要的改动。失败报告出现后,该候选的 `has_diagnostic` 为真,评测不会自动重跑;原因是确定性的,重跑也同样失败。修复原因后如何重新触发,本设计不新增命令,实现时在运维说明里写明。判错的代价:若有人不看标记直接批准,晋级的是没有真实分数支撑的候选;这属于审核门的责任边界,本轮在文档里写明,不在代码里加拦截。

### 8.2 GPU 小时不入账

成本账本现在只记 token(`ledger.py` 的记账函数没有 GPU 维度),训练与评测都不写它,规划器因此只看得到 token 数。本轮不加 GPU 小时记账,理由是单端点、单租约、同一时刻只有一个 GPU 作业,资源争用靠租约解决而不靠预算。作业的起止时间和租约持有时间已写在远端 `jobs/<job_id>/` 与结构化日志里,下一轮要做账本维度时可据此回填。判错的代价:规划器在真实后端下仍按 token 估算成本,会低估真实训练的开销;本轮接受。

升到 real 要另开一轮:实现 RealJudge(独立于候选模型的 OpenAI 兼容端点,沿用 `scorer.py` 现有的自评偏差防护),再把 `backends` 改成 `"real"`。届时 `resolve_baseline` 要求基线报告的等级与当前等级相同,所以切换后的头几轮不会有可比增益,这一点闭环设计第 10 条已有记载。

## 9. 测试策略

默认测试套件不连远端,不需要 GPU。

- **阶段路径等价。** 给 `MockExecutor` 包一层只暴露 `run_sft_stage` 的适配器,断言它与逐步路径产出相同的摘要、存点集合和 `StepResult` 序列。这是对第 4 节改动的主要回归保护。
- **段边界。** 覆盖 `total` 恰为存点步整数倍、不是整数倍、小于存点间隔三种情况;覆盖续训从中间存点开始。再加行数维度:每步行数恰等于全局批次时通过;不等于时在动执行器之前确定性失败;`stage_end` 返回更短的段时按子段正确衔接。
- **`compute` 包。** 用假的 SSH 传输(记录命令,回放预设输出)测试:`start` 幂等、`stop` 只处理本作业的进程组、租约的创建/陈旧回收/竞争、预检不过。命令拼接测引号:作业 id 和路径里带空格、引号、`$()` 时不会被 shell 解释。
- **`RollExecutor`。** 假传输加固定的日志与指标文件:数据渲染(多轮监督按句展开、上下文只作输入、哈希稳定)、行数恒等于全局批次的预检(含超长截断的 token 实测)在动执行器之前、`job_id` 含 `run_id` 与起点存点且对相同输入稳定、失败后换尝试序号重跑、非零退出、超时、step 从 ROLL 记录读出并与 `first_step` 对账、指标缺键。
- **转换器。** 幂等(已有完成标记直接返回)、`.tmp` 清理范围、引用格式。
- **评测。** 假子进程与假隧道:`candidate_id` 经产物解析回 `BigRef`、`prediction.jsonl` 回填、答案归一化后才比对 `reference_answer`、k 条不齐、缺失 item 抛 `UnmappedItemsError`、服务启动超时仍会释放租约。
- **证据等级。** 混合组件时 `diagnostic_grade` 不返回 real 的回归测试。
- **延后通道。** 守护进程接住 `DeferredError` 后指令保持 `pending`、树里没有新提交、退避期内跳过该指令而其他指令照常处理;`ComputeBusyError` 不增加 `resume_attempts`(断言 run 记录逐字节不变);`RemoteJobFailedError` 增加一次。
- **前置检查。** 跨模式续训、`supported_phases`、行数与截断预检失败时,training_run 是 `failed` 且 directive 是 T2,两份记录一致。
- **两个元组。** 断言 `dispatch` 的元组等于 `orchestrator` 的元组加 `NeedsHumanError`。
- **数据前缀。** `first_step > 0` 时 `train.jsonl` 前缀等于步 `[0, first_step)` 的渲染结果,且 `job_id` 随前缀变化。
- **哈希口径。** 两份张量大小相同、内容不同的导出目录,`full` 口径哈希不同;训练存点三块抽样能发现截断与整块替换。
- **评测失败落点。** `UnmappedItemsError` 写出 `stub: true` 且带 `failure_reason` 的报告、该候选此后不再触发评测;`ServeStartupError` 不写报告并走延后通道。
- **CLI 展示。** `summarizeDiagnostic` 对带 `failure_reason` 的 stub 载荷打印原因,对不带的 stub 载荷输出与现状逐字相同。
- **租约回收竞态。** 两个申请者同时回收同一份陈旧租约,恰有一个成功,另一个按忙处理,且不会删掉刚创建的新租约。
- **密钥。** `start` 之后 argv 与 `spec.json` 里都找不到密钥值,spec 哈希在密钥轮换前后不变。
- **真实冒烟(`@pytest.mark.remote`,默认跳过,需要树内 `endpoints.yaml` 与本机主机映射)。** 一条 8 步的最小 SFT 加一次转换加 2 个 item 的评测,验证整条链路能走通。它证明接线正确,不证明模型变好。

## 10. 未核实项与待你确认

下面每一项都会影响设计。标"阻塞"的在实现开始前要有答案。

**待你确认的设计假设**

- **A1,已定:多轮监督。** 按行展开实现(第 5.2 节第 1 步),代价是上下文重复前向,且每步行数必须恒等于 ROLL 的全局批次,由训练前预检保证(第 5.2 节第 2 步)。
- **A2,已定:方案 B。** 混合后端,等级不升,另加 `component_backends` 记录字段(第 8 节)。
- **A3,已定:** 延后通道、本轮接受训练阻塞调和循环、评测确定性失败落 `stub` 报告、哈希分两种口径(D11 到 D14)。

**需要在主机上验证**

- ~~V1~~,**已核实(读源码)。** `sft_pipeline.py:187` 的 `DataLoader(..., shuffle=False, drop_last=True)`,按文件顺序、整批消费,每个优化步取 `global_train_batch_size = dp_size × ga_steps × per_device_bs` 行,尾部不足一批的行被丢掉(`drop_last`)。所以第 i 步的数据确实是文件顺序的纯函数,续训等价的前提成立。两条后果见 5.2 节第 3 步:渲染出的行数必须正好是全局批次的整数倍,否则尾部会被静默丢弃。
- ~~V2~~,**已核实(读源码)。** 是绝对步数。`sft_pipeline.py:243` 起 `global_step = self.state.step + 1`,`state` 从存点里的 `worker_state_pipeline.json` 恢复;循环里 `if global_step >= pipeline_max_steps: break` (第 270 行),注释写明 `N` 个上限对应索引 `[0, N)`、恰好 N 次更新。所以段 `[first_step, last_step)` 对应 `--max-steps last_step`,且 `--resume` 指向的存点必须是 `checkpoint-{first_step - 1}`。
- **V3(阻塞,源码只能证明一半)。** 读到的事实:ROLL 通过 `roll/third_party/vllm/vllm_utils.py` 的 `patch_vllm_lora_manager()` **替换** `LRUCacheWorkerLoRAManager._load_adapter`(`worker.py:220` 调用),并在 `worker.py:63` 对 Flash-Next 应用 `patch_qwen38_lora_shrink`(把 Triton 的 `split_k` 强制成 1,为让 LoRA 推理可重复)。ROLL 自己的引擎以 `enable_lora=True, max_loras=1` 启动(`vllm_strategy.py:350`)。**这意味着:裸的 `vllm serve` 不会带这两个补丁**。补丁是 ROLL worker 初始化时进程内打上的,不在 vLLM 包里。所以"容器里的 vLLM 能加载"只在经过 ROLL worker 的路径上被验证过;独立的 `vllm serve` 要么需要自己复刻这两个补丁(写成一个启动包装脚本,在 import vLLM 后先调用它们),要么评测改走 ROLL 的 worker。两条都是设计变更,见 7.1 节修订。无法靠读源码判定的是:加了补丁之后 `vllm serve` 的 HTTP 层是否仍能走到被替换的 `_load_adapter`,这必须在主机上实测。
- ~~V4~~,**部分核实(读源码)。** 逐步指标有三处可取:(a) 存点里 `pipeline/worker_state_pipeline.json` 的 `log_history`(每步一个 dict,含 `system/step`),但只在存点步写出,且 `--save-steps` 之间的步没有;(b) `track_with: stdout` 时日志里每步一行 `metrics_tag: {"step": N, "metrics": {...}}`;(c) `completion.json` 只含 first/last step 与存点结构检查,**没有逐步 loss**。loss 的键是 `sft_train/loss@sum`(`sft_pipeline.py:302`),`StdoutTracker` 经 `strip_at_tag_in_log` 去掉 `@sum` 后缀,所以 stdout 里实际键名是 `sft_train/loss`。结论:逐步 loss 要从 stdout 日志解析,存点的 `log_history` 只能用来对账 step,不能当作完整曲线。**未核实:** stdout 日志落在哪个文件(各 rank 的 `log_rank_*` 文件在 `logging_dir` 下,driver 的 stdout 取决于启动方式)。
- **V5。** `.181` 上当前的 GPU 占用与可用内存。预检阈值要据此定。
- **V6(阻塞)。** SoMBench 样本和 `DiagnosticItem` 之间是否有 id 对应关系,以及 `item_map.json` 由谁生成。
- ~~V7~~,**已核实。** 后缀只被 `MockConverter` 用,`_is_inference_format` 看 `fetch_hint`(第 6.2 节第 4 步)。
- **V8。** SoMEval 的子进程入口怎么调(命令行还是需要小包装脚本),依赖能否装进现有环境。
- ~~V9~~,**已核实(读源码)。** `--resume` 带一个路径参数(`argparse` 里 `type=Path`)。脚本把它转成 `cfg.resume_from_checkpoint = str(args.resume.resolve())`,流水线再取该目录下的 `pipeline/` 子目录读 `worker_state_pipeline.json` 和 `rng_state_pipeline.pth`(`base_pipeline.py:82`、`sft_pipeline.py:246`)。所以 `--resume` 指向的是 `pipeline/checkpoint-N/`,即含 `pipeline/` 子目录的那一级;缺少 RNG 文件会直接抛 `FileNotFoundError`。
- ~~V10~~,**已核实(读源码)。** `step_planning.py` 的 `resolve_sft_step_plan`:`configured_max_steps > 0` 时`pipeline_steps = max_steps`,`epochs = ceil(max_steps / steps_per_epoch)`,所以 `--max-steps` 优先于 `num_train_epochs`,可以在 epoch 中途停下。另有两条硬约束:数据集不足一个全局批次直接抛 `ValueError`;脚本还会校验 `min(epoch_steps, max_steps) == max_steps`,即数据量足够跑满 `max_steps` 步,否则报错。`--max-steps` 下限是 2(脚本 `parser.error`)。
- ~~V11~~,**大部分已核实(读源码),有一处否定了设计假设。**
  - 算式成立:`sft_pipeline.py:180` 起 `global_train_batch_size = dp_size * ga_steps * per_device_bs`,DataLoader 的 `batch_size` 就是它。
  - **否定的假设:一个作业内每步行数可以不同。** 全局批次是配置里的常数,DataLoader 每步固定取这么多行;`gradient_accumulation_steps` 是**整个作业一个值**。所以"段内各步行数必须一致"不是一个可以靠切段绕开的软约束,而是 ROLL 的硬事实:每个编排步渲染出的行数必须**恒等于**全局批次(8 × ga),不能逐步变化。原先"按该步实际行数渲染 ga、段按行数切细"的设计里,切细只能让段内行数相等,却不能让它等于全局批次的整数倍之外的任何值。见 5.2 节第 2 步的修订。
  - 超长截断:`DataCollatorForSFT`(`collator.py`)在 `max_length` 处把 `input_ids` 和 `labels` 一起截断,**不报错也不警告**。行展开的后几行上下文最长,最容易被静默截断。预检必须拿 tokenizer 实测每行长度,超过 `--sequence-length` 就确定性失败,不能指望 ROLL 报错。
- ~~V13~~,**已核实(读源码)。** 评估器与诊断编排器不捕获异常,守护进程的 `apply_action` 只接住 `DirectiveFailedError`;评估器抛任何其他异常,进程退出,重启后 `reconcile` 再次给出同一个 `request_diagnostic`。第 7.3 节据此分类。
- **V14(已收窄到真实题库)。** 比较方式已核实(§7.2:规则题走 `RuleScorer`,去空白、转小写后整串相等)。默认题库已核实:`factory/defaults.py` 的 `DEFAULT_ITEM_POOL` 只有两道 `mcq` 规则题,`reference_answer` 是单个选项字母("B"、"C"),并且是最小夹具,注释写明真实题库替换它。所以默认夹具下 `answer_shape` 取"单个选项字母"即可;真实题库的答案写法仍未知,它决定 `item_map.json` 的 `answer_shape` 怎么填,接入真实题库前必须确认,否则真实评测的规则题分数没有意义。
- **V15。** `launch_prefix` 内外的 `work_root` 是否是同一路径(绑定挂载),宿主机上的 `nvidia-smi` 能否看到其他容器的计算进程。§3.2 与 §3.3 都依赖这两点。核实办法:在验证主机上从容器内外各创建一个文件、各跑一次 `nvidia-smi`。
- ~~V12~~,**已核实。** 载荷在 `diagnostic/store_adapter.py:16` 的 `write_report` 里组装,`component_backends` 加在那里(第 8 节)。

**本机还没读的部分**

子系统设计里的 SSH 约定、默认题库资产的位置(V14)还没读,实现计划阶段先读。`factory` 的装配入口已核实,见第 12 节。

## 11. 实施顺序

1. 编排器的阶段路径:`run_sft_stage`(含 `prior_batches`)、`stage_end` 与等价性测试;`CheckpointCorruptError` 补进两处 `_DETERMINISTIC` 并加同步测试,`executor.load` 移进 `try`;跨模式、`supported_phases`、行数与截断三类前置检查(第 7.4 节)。完全在本机,不依赖远端。
2. 延后通道:`controlplane/deferred.py` 的 `DeferredError`,守护进程接住并退避,`resume_or_run` 里的 `preflight` 钩子,评估器对确定性失败写 `stub` 报告(第 7.3 节)。完全在本机,不依赖远端;改动面与第 1 步不重叠(前者在 `mindtrainer/orchestrator.py`,后者在 `controlplane/daemon.py`、`diagnostic/evaluator.py`、`diagnostic/store_adapter.py`(新增写失败报告的函数)和 `cli/src/commands.ts`)。
3. `compute` 包(配置、传输、`job_tool`、租约、预检)和假传输测试。
4. 在主机上做 V1 到 V5 与 V15 的验证,结果回填本文档。
5. `RollExecutor` 与 `RollLoraConverter`。
6. 评测:先 V3、V6、V8,再做 `RealSoMEvalRunner`。
7. 装配(接线点见第 12 节):`assemble` 的真实后端分支、`component_backends` 记录、`training_defaults.yaml` 的真实模式配置(`grpo_steps: 0`)、远端冒烟。

1、2、3 不依赖任何待验证项,可以并行开始。4 之后每一步都要等对应验证有答案。

## 12. 装配(已核实的接线点)

`factory/wire.py:106` 的 `assemble(store, options, *, env=None, log_dir=None, builders=None)` 是唯一入口。核实到的两条路径:

- 不传 `--enable` 时直接 `return Assembly(make_mindtrainer_dispatcher(), {}, None, False)`,评估器为 `None`。
- 开了闭环开关时注入 `make_mindtrainer_dispatcher(data_producer=TemplateDataProducer())` 与 `build_mock_evaluator(store)`。

真实后端因此只需在 `assemble` 里多一条分支:给 `make_mindtrainer_dispatcher` 传 `executor_factory` 与 `converter`,并把评估器从 `build_mock_evaluator` 换成真实组装。无开关路径一行不动,保持与 `mindtrainer.wire` 字节等价这条既有约定。

真实后端用什么开关表达(复用 `--enable`,还是单独的 `--backends real`),取决于 `factory/switches.py` 与 `FactoryOptions` 的现有形状,实现计划阶段定。树内 `endpoints.yaml` 或本机主机映射缺失、不合法,一律是 `FactoryConfigError`,退出码 2,与 `evaluator.yaml` 缺失同一约定。
