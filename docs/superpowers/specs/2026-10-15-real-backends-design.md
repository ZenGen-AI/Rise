# 真实后端接入设计:LoRA SFT 经 ROLL 训练,经 vLLM + SoMEval 评测

状态:草案,待审阅。日期:2026-10-15。
前置设计:`2026-10-03-mindtrainer-design.md`、`2026-10-09-closed-loop-integration-design.md`。

## 0. 这份文档做什么

闭环现在全部跑在 mock 上:`MockExecutor`、`MockConverter`、`MockSoMEvalRunner`、`MockJudgeBackend`,`factory/evaluation.py` 里 `backends` 恒为 `"mock"`。这份设计把其中一条链路换成真的:

基座权重 → ROLL 在远端 8×H800 上做 LoRA SFT → 存点 → 转成 vLLM 原生 LoRA 适配器 → vLLM 服务 + SoMEval 做诊断评测 → `backends="real"` 的诊断报告

本稿采用的三个取舍(请确认,我不能保证它们都来自你明确的指示):

1. 训练只接 LoRA SFT。GRPO/OPD 这一轮保持 mock,并且拒绝与真实执行器混用。
2. 远端主机用 ROLL4ZenGen 文档记录的验证主机(文档里记作 `.181`,8×H800),本机经 SSH 调度。
3. 推理用该主机容器里的 vLLM 开发版,不用 PyPI 发布版。

不做:GRPO/OPD 真实训练、自定义 reward worker、把适配器合并回完整 HF 权重、Q4 开放题的真实 judge(RealJudge)、升级 vLLM、训练与评测并发抢 GPU。

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

没读到:`factory` 的 CLI 与装配入口、子系统设计里的 SSH 约定、`_rule_hit` 的判分细节、SoMBench 样本的具体字段、ROLL 的 `summary.json` 与逐步日志格式。这些放进第 10 节作为未核实项。

## 2. 裁决总表

| 编号 | 决定 | 理由 |
| --- | --- | --- |
| D1 | 本机编排,远端以"后台作业 + 轮询"执行,不做常驻服务 | ROLL4ZenGen 只提供整段作业入口;作业可脱离 SSH 连接存活,守护进程重启后能重新接上 |
| D2 | 编排器增加可选的 `run_sft_stage`;没有该方法的执行器走原来的逐步路径 | 让 ROLL 整段作业与逐步协议对上,又不动 Mock 与现有测试 |
| D3 | 真实执行器只声明 `supported_phases = {"sft"}`;`grpo_steps > 0` 在配置校验期失败 | 一半真一半 mock 的存点没有意义,也会谎报 |
| D4 | 真实存点是 `BigRef`:`uri=ssh://<别名>/<路径>`,`sha256`=目录清单哈希,`fetch_hint.type="roll_ssh"` | 复用现有信封;跨模式续训在派发层挡住(见 §6.1) |
| D5 | 转换器调用 `LoRAHFConverter`(CPU),产物是适配器导出,`fetch_hint` 写明所需基座 | 这是文档里唯一验证过的导出路径 |
| D6 | SoMEval 以子进程运行,不在进程内导入 | 隔离重依赖与顶层 `src` 包名 |
| D7 | `backends="real"` 只在所有参与评测的组件都是真实现时才声明 | 沿用 `diagnostic_grade` 的失败关闭语义 |
| D8 | GPU 独占:主机租约 + 预检,绝不终止非本任务启动的进程 | 验证主机上有别人的服务 |
| D9 | 连接配置放本机 `compute.yaml`,不进 git 树;密钥只记环境变量名 | 地址与路径因环境而异,密钥不落盘 |
| D10 | 远端只写 `work_root`;所有远端操作经一个只依赖标准库的助手脚本 | 缩小 shell 引号面,远端行为可被哈希固定 |

## 3. 远端计算层

新增顶层包 `compute`,只管"在远端主机上安全地跑一个作业",不认识训练和评测。

### 3.1 配置

本机文件 `compute.yaml`,路径由环境变量 `RISMFORGE_COMPUTE_CONFIG` 指定,不进 git 树。装配期读取,键缺失、多余或类型不对按 factory 现有约定退出码 2,报错只写键名,不回显值。

| 键 | 含义 |
| --- | --- |
| `ssh_alias` | `~/.ssh/config` 里的主机别名。密钥、跳板、端口都在那里,这份配置不存凭据 |
| `work_root` | 远端唯一可写根目录。作业、租约、助手脚本、产物都在其下 |
| `roll_root` | ROLL4ZenGen 检出目录(只读使用) |
| `launch_prefix` | 启动命令前缀(argv 列表),例如进入训练容器的命令。裸机时为空列表 |
| `base_model_path` | 远端基座 HF 目录 |
| `heldout_jsonl` | 远端已就位的留出集。不由训练数据派生 |
| `sft_template` | `roll_root` 下的 LoRA SFT 配置模板路径 |
| `min_free_host_mem_gib`、`min_free_disk_gib` | 预检阈值 |
| `poll_seconds`、`job_timeout_seconds` | 轮询间隔与整段作业超时 |

SSH 一律带 `-o BatchMode=yes -o ConnectTimeout=...`。远端命令用 `shlex.join` 拼成一个字符串,文件内容走 stdin,不拼进命令行。

### 3.2 远端助手 `job_tool.py`

只依赖标准库的单文件脚本,sha256 写死在 `compute` 包里。首次使用时上传到 `<work_root>/bin/job_tool-<sha8>.py`,已存在则校验哈希。所有远端动作都经它,输入输出都是 JSON。

- `start`:按 `job_id` 建 `<work_root>/jobs/<job_id>/`,写 `spec.json`。目录已存在且 spec 哈希相同则直接返回当前状态(幂等);spec 不同则报错。用 `setsid` 起进程,输出重定向到 `stdout.log`,包装层在进程退出时写 `exit_code`。
- `status`:返回 `running`、`exited`(带退出码)或 `missing`。
- `tail`:取日志末尾 N 行。
- `stop`:只向本作业 `spec.json` 记录的进程组发信号,并核对进程启动时间戳,防止 pid 复用。不碰任何其他进程。
- `put`、`hash-tree`:带哈希的原子写入(临时文件加重命名)和目录清单哈希。

### 3.3 GPU 租约与预检

- 租约是 `<work_root>/gpu.lease`,用 `O_CREAT|O_EXCL` 创建,内容是 `job_id`。持有者的作业已有 `exit_code` 或进程已不在,租约视为陈旧,下一个申请者可回收。
- 启动前预检:`nvidia-smi` 报告没有计算进程(本作业除外),空闲主机内存和磁盘达到阈值。
- 预检不过抛 `ComputeBusyError`。它不在 `_DETERMINISTIC` 里,所以 run 保持 pending,下个 tick 重试。我们从不结束别人的进程,哪怕它占着 GPU。

## 4. 编排器改动:可选的阶段接口

ROLL 只提供整段作业,逐步协议要求每步返回指标。对接方式是给 `TrainingExecutor` 增加一个可选方法:

```python
class StagedExecutor(TrainingExecutor, Protocol):
    def run_sft_stage(
        self, batches: list[list[Episode]], *, first_step: int, trained_slot: str
    ) -> list[StepResult]: ...
```

`batches[i]` 是全局第 `first_step + i` 步的数据,与逐步路径里 `batch_for_step` 取到的相同。返回的 `StepResult` 按步排列,`step` 字段必须连续。

`run_training` 的 SFT 阶段改成:

1. 执行器有 `run_sft_stage` 时,把 SFT 阶段切成段。段边界先取下一个存点步、SFT 结束步和 `total` 里最近的一个,再让执行器有机会把段切得更细。切细用的接口现在就定下来,不留到实现:

   ```python
   def stage_end(self, batches: list[list[Episode]], *, first_step: int, limit: int) -> int: ...
   ```

   `limit` 是编排器算出的段终点,开区间,即不含该步;返回值是执行器能一次吃下的终点,同样是开区间。返回值必须满足 `first_step < 返回值 <= limit`,**保证至少前进一步**,否则编排器抛 `RuntimeError`:不前进就是死循环,要响亮地失败。没有这个方法的执行器按 `limit` 处理。
   真实执行器用它实现段内各步行数一致(第 5.2 节第 2 步);`MockExecutor` 不实现它,所以逐步路径不受影响。
2. 段返回后,对其中每个结果执行与逐步路径完全相同的检查和记账:`result.step != step` 抛 `RuntimeError`,`_update_summary`,`on_step`。
3. 段尾再走原来的存点判断(`done < total and done % checkpoint_every_steps == 0`),调 `save_checkpoint`。
4. 没有 `run_sft_stage` 的执行器(含 `MockExecutor`)走原路径,字节级不变。

把循环体里"收到结果之后"的那部分抽成一个内部函数,两条路径共用,避免复制出两份记账逻辑。

行为差异,都要写进代码注释和测试:

- `on_step` 在一段结束后成批到达,不再实时。实时进度看远端日志。
- 段中途崩溃,回退到上一个存点。这与逐步路径一致,因为存点本来就是回退边界。
- 作业非零退出时,执行器抛 `RemoteJobFailedError`。它不在 `_DETERMINISTIC` 里,run 保持 pending,`resume_attempts` 递增,受 `max_resume_attempts` 约束,超限由 `decide_resume` 升级给人。

## 5. RollExecutor

放在 `mindtrainer/roll_executor.py`,依赖 `compute`。`executor_factory` 是闭包,装配期绑定好 `compute.yaml`。

### 5.1 协议实现

- `supported_phases = frozenset({"sft"})`。`run_training` 开头检查:执行器声明了 `supported_phases` 且 `cfg.grpo_steps > 0` 而 `"grpo_opd"` 不在其中时,抛 `InvalidTrainingConfigError`(确定性失败,run 记为 failed)。真实模式需要配套的 `training_defaults.yaml`,其中 `grpo_steps: 0`。
- `load(ref)`:接受两类引用。(a) 起点基座,必须指向 `base_model_path`,manifest 哈希与引用一致;(b) 本设计产出的训练存点,先过 `checkpoint_intact`,不通过抛 `CheckpointCorruptError`。`mock://` 引用一律拒绝。`load` 只做校验和记录,不占 GPU。
  **多轮闭环的缺口(本轮不做)。** `executor.py:88` 写明晋级后下一轮训练的基座就是转换后的那份,而真实转换产出的是 `qwen4_exp_vllm_2d` 适配器导出,不是 HF 训练起点。ROLL 能否以它为训练起点,`mindtrainer` 设计 §10 本来就列为未核实。所以真实后端本轮**只支持单轮**:第二轮的基座仍要人工指定为 `base_model_path`,`load` 收到适配器导出引用时抛 `InvalidTrainingConfigError` 并说明原因,不静默接受。这条写进不做清单。
- `sft_step`、`grpo_opd_step`:抛明确的异常。真实执行器只能走阶段路径,逐步调用说明编排器被改坏了,要响亮地失败。
- `save_checkpoint(tag)`:ROLL 的存点在阶段内部写出,这里把该阶段产出的存点目录封成 `BigRef`,不另外触发保存。有两种情况没有本次阶段产出,都要处理:
  - **循环为空。** 续训时若 `start == total`(崩在最终存点之后、置 `executed` 之前),一步都不会跑,但 `orchestrator.py:226` 仍无条件调 `save_checkpoint("final")`。此时要从 run 记录的 `last_checkpoint_ref` 回认那份已有存点,并校验它仍然完整;认不出就抛 `CheckpointCorruptError`,绝不返回一个没有对应目录的引用。
  - **段被切细。** 子段末尾的 ROLL 存点不是编排器的存点步(`orchestrator.py:209` 只在存点步回写),但它是下一子段的续训输入。这类中间存点由执行器自己记在作业目录下,不进 git 树;整个阶段成功后才可以清理,清理范围严格限定在本 `run_id` 的作业目录内。
- `checkpoint_intact(ref)`:经 `job_tool hash-tree` 核对 manifest,并检查 ROLL 的结构文件齐全(tracker、迭代目录、每个 worker 的 RNG、scheduler、适配器配置)。只能证明结构完整,不证明张量没有坏。

### 5.2 `run_sft_stage`

1. 渲染数据,**多轮监督**(已定)。被训练角色位在一个剧集里说了 T 句,就渲染出 T 行 `{"instruction", "output"}`:第 t 行的 `output` 是该角色位的第 t 句,`instruction` 是这句之前的全部发言按固定模板拼成的文本。每一句都被监督,它之前的上下文只作为输入,不参与 loss。
   用行展开而不是单次前向加 loss mask,是因为 `sft_lora.yaml` 只有 `prompt_key/response_key` 这一对键,表达不了逐轮掩码。代价要写明:同一段上下文会被重复前向,token 量随 T 平方增长;这在语义上等价于逐轮掩码,在计算上不等价。真正的单次前向掩码需要在 ROLL 侧自定义数据管线与模板,本轮不做。
2. 批次与累积步数。编排器的步数由配置定死:`total_steps(cfg) = sft_steps + grpo_steps`,`phase_at` 也按 `sft_steps` 切阶段(`training_config.py:448`、`452`)。所以步数**不能**由数据行数反推,一步仍然恰好是 `batch_for_step(episodes, step, sft_batch_size)` 取到的那几个剧集,逐步路径与阶段路径取到的数据完全相同。
   行展开发生在这之后:该步取到的剧集逐个展开求和,得到这一步的行数。各剧集长短不一,所以每步行数可以不同。ROLL 的一个优化步吃 DP8 × 每卡批 1 × 累积 N 行,于是要求每步行数是 8 的倍数,并按该步实际行数渲染 `gradient_accumulation_steps = 行数 / 8`。
   **整除性要在训练前一次性预检,不能等渲染到某一段才发现。** 数据是 step 的纯函数,所以 `[0, sft_steps)` 上每一步的行数在动执行器之前就能全部算出来。预检放在 `run_training` 调 `executor.load` 之前,对照 GRPO 的分组预检也在循环之前(`orchestrator.py:54`、`171`),理由一样:数据有问题时一步都不浪费。不预检的话,一步的行数恰好是 8 的倍数大约只有 1/8 的概率,100 步几乎必然中途失败,而且可能发生在已经跑了几小时之后。
   预检不过有两条出路,都不补齐也不丢行,因为补齐会凭空造监督样本,丢行会静默少训:
   - **改 `sft_batch_size`。** 预检报出当前每步行数与它不是 8 的倍数这个事实,让人改配置。确定性失败,抛 `InvalidTrainingConfigError`。
   - **约束每集行数。** 默认数据生产器的轮数固定为 `max_turns`,所以 T 恒定,`sft_batch_size × T` 是否为 8 的倍数在配置期就能算准。interact 轨迹长短不一,本轮不支持真实后端下的 interact 模式,写进不做清单。
   段内各步行数不同就意味着累积步数不同,而一次 ROLL 作业只有一份配置,**所以一个作业段内的各步行数必须一致**;不一致就把段再切细。T 恒定时段不会被切细,这是预期的常态路径。
   段被切细的代价不只是启动开销:每个子作业都会按 `--save-steps` 写一次完整存点,而存点含几十 GB 的优化器状态(第 6.1 节)。所以预检要顺带报出本阶段会产生多少次存点,并把磁盘阈值按这个数算,而不是只按一次存点算。段被切得过细时,宁可在预检阶段就确定性失败,让人改 `sft_batch_size` 或 `checkpoint_every_steps`。
   覆盖 `gradient_accumulation_steps` 是否安全、ROLL 是否确实按这个算式消费整批,都没有核实,见 V11。
3. 数据按步序排列成 `train.jsonl`,连同哈希上传。
4. 作业身份 `job_id = sha256(run_id, 起点存点 sha256, first_step, last_step, 数据哈希, 模板哈希, launcher 版本)[:16]`。约定 `last_step` 与 `stage_end` 的返回值一致,是**开区间**终点,所以这一段覆盖的是 `[first_step, last_step)`,共 `last_step - first_step` 步。`run_id` 与起点存点必须进哈希,否则起点不同而数据相同的两次续训会撞同一目录。
   **幂等与重试要分开。** `start` 对「目录已存在且 spec 哈希相同」直接返回当前状态,这让守护进程崩溃后能接上**正在跑**的作业;但同一条规则会让**已失败**的作业永远重跑不了:重试时 `start` 命中那个已退出的目录,`status` 仍报非零退出,于是再抛一次同样的异常,直到 `resume_attempts` 触顶升级给人。OOM 或被抢占这类瞬态失败就恢复不了。
   所以作业目录名带尝试序号 `<job_id>-a<N>`,而 `job_id` 仍只由确定输入算出:
   - `start` 先找该 `job_id` 下最大的尝试序号。该尝试仍在跑就接上(幂等,崩溃恢复靠这条);已退出且退出码为 0 就直接返回成功。
   - 已退出且非零,或曾被 `stop`,就新建下一个尝试序号。旧目录保留不删,留作证据。
   - 尝试序号的上限与 `max_resume_attempts` 分开记账,见第 7.3 节。
5. 启动命令是 `run_sft_validation.py`,参数为 `--config <sft_lora 模板> --model-path --output-dir --train-data --heldout-data --max-steps <last_step> --save-steps <last_step> --eval-steps <last_step>`,`first_step > 0` 时续训。`--resume` 这个参数存在,但它是开关还是带路径的参数没有读到,存点怎么指定也没读到,见 V9。目标是让 ROLL 恰好在段尾写存点。
   步数口径要小心:`sft_lora.yaml` 里 100 步是由 "400 条 / DP8 × 2 个 epoch" 推出来的,配置按 epoch 走。脚本另有 `--train-records`、`--heldout-records`、`--sequence-length`、`--checkpoint-output-dir`、`--checkpoint-hard-links`,含义都没读。所以 `--max-steps` 是否真能截断到段尾、与 epoch 数怎么相互作用,要先验证(V2、V10)。我印象里该脚本有"工程验证入口,不是模型支持声明"一类的声明,没读到原文,先按未核对处理;确认有的话,要在日志和报告里保留。
6. 轮询 `status`,直到 `exited`。退出码非零抛 `RemoteJobFailedError`,附日志末尾。超过 `job_timeout_seconds` 先 `stop` 再抛 `RemoteJobTimeoutError`。
7. 收集逐步指标,转成 `StepResult(step, "sft", {"sft_loss": ...})`。
   **step 不能按位置合成。** 若直接写 `first_step + i`,`orchestrator.py:200` 的 `result.step != step` 检查就恒为真,而它本来是用来发现执行器与 run 记录不同步的,等于把这道校验废掉。真实后端恰恰最需要它,因为 ROLL 实际从哪一步起训要靠 `--resume` 的语义(V2、V9)才能确定。
   所以 step 要从 ROLL 自己的记录里读:取存点 tracker 或日志里的 global step,与 `first_step` 对账,不一致抛 `MetricsMissingError`。读不到 ROLL 的 global step 时同样失败,不退化成按位置编号。
   指标条数与 `len(batches)` 不符也抛 `MetricsMissingError`。另外 `_update_summary`(`orchestrator.py:83`)直接读 `metrics["sft_loss"]`,缺键会是 `KeyError`,所以这个异常要同时覆盖条数不对和缺 `sft_loss` 键两种情况。

**数据顺序约束。** 编排器的续训等价依赖"第 i 步的数据是 i 的纯函数"。这只在 ROLL 按文件顺序消费数据时成立。我没有核实 ROLL 的 dataloader 是否打乱(配置里有 `seed: 42`,注释按 epoch 算步数)。第一个验证任务必须回答这个问题,见第 10 节。

## 6. 检查点与转换器

### 6.1 存点引用

`BigRef(uri="ssh://<别名>/<work_root>/jobs/<job_id>/<目录>", sha256=<manifest 哈希>, size_bytes=<总字节>, fetch_hint={"type": "roll_ssh", "format": "roll_lora_legacy", "job_id": ...})`。

manifest 哈希 = 排序后的 `(相对路径, 字节数)` 列表的哈希,再并入所有小文件(json、txt、小于 64 MiB 的 pt)的内容哈希。大张量分片只记大小,不逐字节哈希:几十 GB 的优化器状态每次存点都完整读一遍代价太高。它能发现结构变化和截断,发现不了位腐烂。

**跨模式续训要挡住,但不能靠改 `MockExecutor`。** 现状已核实:`MockExecutor.load`(`executor.py:109`)对非 `mock://` 的引用走 else 分支,当作外部起点(`state = ref.sha256`,步数 0),并不拒绝。要让它拒绝 `ssh://` 就得改 Mock,这与 D2(Mock 字节级不变)直接冲突。

所以挡在派发层:装配时知道当前是哪种后端,续训前比对 `last_checkpoint_ref` 的 `fetch_hint["type"]` 与当前执行器的类型,不一致就抛 `InvalidTrainingConfigError`(确定性,run 记 failed)。不挡的后果是真实 run 被 Mock 续训时不会在 `load` 处失败,而是到第一步才因 `result.step != step` 抛 `RuntimeError`(`orchestrator.py:200`),而该异常是非确定性的,会一直 pending。反方向同理:`RollExecutor.load` 收到 `mock://` 引用时抛 `CheckpointCorruptError`。

### 6.2 RollLoraConverter

实现 `CheckpointConverter.convert(ref) -> BigRef`:

1. 在远端 CPU 上跑 `LoRAHFConverter`,`CUDA_VISIBLE_DEVICES=` 置空,不占 GPU 租约,但要检查主机空闲内存(文档记录峰值约 8 GiB)。
2. 目标目录必须是全新的。导出到 `exports/<存点 sha8>.tmp/`,成功后写完成标记,再原子重命名为 `exports/<存点 sha8>/`。已存在带标记的目录则直接返回对应引用(幂等);只有未完成的 `.tmp` 才会被清理,清理范围严格限制在自己创建的 `.tmp` 目录。
3. 返回的 `BigRef.fetch_hint` 写明 `{"type": "roll_ssh", "format": "qwen4_exp_vllm_2d", "base_model_path": ..., "requires": "native-vllm-lora"}`。通用 HF/PEFT 不能加载这个布局,评测侧必须读到这个声明。
4. 引用 uri 不带 `.hf` 后缀。已核实:只有 `MockConverter.convert` 看 uri 后缀(`checkpoint.py:32`),`_is_inference_format` 看的是 `fetch_hint["format"] == "hf"`(`executor.py:64`)。真实转换器写 `format: "qwen4_exp_vllm_2d"`,所以不会被 `_is_inference_format` 判成推理格式,这正是上面 I9 那条缺口的来源。

## 7. 评测:vLLM 服务与 SoMEval

### 7.1 服务生命周期

`RealSoMEvalRunner.evaluate(items, candidate_ref, k)` 内部按顺序执行:

1. 取 GPU 租约并预检(同 3.3)。训练作业退出后才会走到这里,租约保证两者不并发。
2. 经 `job_tool start` 在容器里启动 `vllm serve`:加载 `base_model_path`,注册导出的适配器,名称取存点 sha8。TP/EP 相关参数照搬 ROLL4ZenGen 原生 vLLM LoRA 探针的设置。探针不是 `vllm serve`,这一步能否跑通没有核实,见第 10 节 V3。
3. 服务只绑定远端回环地址,并带 `--api-key`(密钥取自环境变量,配置里只记变量名)。本机经 `ssh -L 127.0.0.1:<随机端口>:127.0.0.1:<远端端口>` 访问。不要监听外网卡:vLLM 的 HTTP 接口没有别的访问控制。
4. 轮询 `/v1/models`,直到适配器出现。超时则 `stop` 并抛 `ServeStartupError`。
5. 评测结束,无论成败,`stop` 服务、关隧道、释放租约。

### 7.2 SoMEval 调用

- 以子进程运行 `run_prediction_stage`,工作目录是 SoMEval 检出目录。配置是我们生成的临时 `experiment_config.yaml`:`llm.model_name` 取适配器名,`llm.api_url` 指向隧道地址,`llm.api_key` 取环境变量,`repeats` 取 k。
- SoMEval 按数据集选样,配置里没有按 id 选题的字段;`DiagnosticItem` 又不带题干。所以需要一份 `item_map.json`(`item_id` 到 SoMBench 样本)。我们据此生成子集数据集,让 `normalized_datasets_path` 指向它。
- **`candidate_ref` 是 `str`,不是 `BigRef`。** 协议把它声明为字符串(`someval_adapter.py:12`),评估器传进来的是 `candidate_id`(`evaluator.py:99`)。所以运行器拿不到 uri 与 `fetch_hint`,必须自己按 `candidate_id` 去读 `candidate_checkpoint.yaml` 产物,从 `payload["checkpoint_ref"]` 取回 `BigRef`,再从它的 `fetch_hint` 读 `base_model_path` 与 `requires`。这要求运行器持有 `GitStore`,构造时注入。
- **映射缺失的 item 没有表达通道,只能失败。** `run_static_items` 对每个 item 直接 `raw[item.id]`(`someval_adapter.py:77`),缺键就 `KeyError`;协议的返回类型只有 `dict[str, list[str]]`,没有地方放那几道没评上的题。所以缺失 item 一律抛 `UnmappedItemsError`(确定性),附缺失清单,不静默丢弃也不假装评过。先前写的记入 `unmapped` 并写进报告做不到,已撤回。
- **`_rule_hit` 是规则判分的唯一入口**(`someval_adapter.py:92`),它拿原始输出与 `reference_answer` 比较。真实模型的自由文本能否命中,取决于它的比较方式(精确相等还是包含),我没读到函数体,见 V14。无论哪种,SoMEval 抽取出的答案都要先归一化成与 `reference_answer` 同一形状再回填,否则真实评测的分数没有意义。
- 读 `prediction.jsonl`,按样本 id 回填成 `{item_id: [k 条原始输出]}`。某个 item 的输出条数不等于 k,该 item 记为失败。
- `item_map.json` 的来源是本设计的一个假设:题库构建步骤提供它。如果默认题库和 SoMBench 样本之间没有 id 对应关系,这一节要重做,见第 10 节 V6。

### 7.3 失败分类

先说清现状,因为下表的多数条目都需要改代码才成立。`_DETERMINISTIC` 有两处,**都只管训练派发路径**:

- `orchestrator.py:36`,五项:`DatasetIntegrityError`、`DegenerateGroupError`、`AttributionError`、`NotEnoughGroupsError`、`InvalidTrainingConfigError`。命中则把 training_run 写成 failed。
- `dispatch.py:36`,六项:上面五项再加 `NeedsHumanError`。命中则映射成 `DirectiveFailedError`,并按 `NeedsHumanError` 判 T3、其余判 T2。

**两处要一起改。** 只加 orchestrator,directive 的归类会走通用分支;只加 dispatch,directive 判了 failed 而 training_run 还是 pending,两份记录不一致。所以新增的训练侧确定性异常,两个元组都要列入,不是按需。

| 异常 | 现状 | 本设计要的语义 | 要动的代码 |
| --- | --- | --- | --- |
| `ComputeBusyError` | 新增 | 等资源,**不耗续训预算** | 见下方记账问题 |
| `RemoteJobFailedError`、`RemoteJobTimeoutError` | 新增 | pending,可重试,计入 `resume_attempts` | 不入两个元组即可 |
| `InvalidTrainingConfigError` | 已是确定性 | failed | 无 |
| `CheckpointCorruptError` | **不在任何元组里**,且 `executor.load` 在 `try` 之外(`orchestrator.py:163`),所以现在是原样上抛、run 保持 pending | failed | 两个元组都要加,且 `load` 要移进 `try` 或另行捕获 |
| `MetricsMissingError` | 新增 | failed | 两个元组都要加 |

**阶段路径的调用必须放进现有的 `try`**(`orchestrator.py:170` 到 `218`)。放在外面,新增的确定性异常就拿不到 failed 记录。

**失败点字符串。** 逐步路径把 `point` 设成 `f"step {step} ({phase})"`(`orchestrator.py:176`),它会写进 failed 记录的 `failure.point`。阶段路径一次跨多步,所以改成 `f"sft stage [{first_step}, {last_step}) "` 再按情况接上 `rendering`、`remote job` 或 `metrics`,让人从记录里看出失败发生在渲染、远端作业还是回收指标。段内逐个结果记账时,仍按逐步路径的格式设 `point`,这样段中途的对账失败能定位到具体哪一步。

**`ComputeBusyError` 的记账问题,没有现成答案。** 每次带 `resume_from` 进入 `run_training`,都先把 `resume_attempts + 1` 落盘(`orchestrator.py:147`),之后才碰执行器。而 D8 说验证主机上常有别人的服务,所以忙是常态。于是几次忙就能耗尽 `max_resume_attempts`,升级成 `NeedsHumanError`,而这并不是真的失败。两条出路,实现时选一条并测:(a) 把资源预检提到 `run_training` 之前,在派发层就判定忙并直接返回,run 记录完全不动;(b) 给 `TrainingRun` 增加一个与 `resume_attempts` 分开的 `busy_attempts` 计数。倾向 (a),它不改产物 schema。

**评测侧异常不在这张表里。** `ServeStartupError`、`UnmappedItemsError` 发生在诊断评估器里,两个 `_DETERMINISTIC` 对它们没有作用,run 保持 pending 这套语义也不适用于评测。评测的失败如何落到报告与 directive 上,取决于 `diagnostic/orchestrator.py` 与守护循环的既有写法,我没读到,见 V13。

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

升到 real 要另开一轮:实现 RealJudge(独立于候选模型的 OpenAI 兼容端点,沿用 `scorer.py` 现有的自评偏差防护),再把 `backends` 改成 `"real"`。届时 `resolve_baseline` 要求基线报告的等级与当前等级相同,所以切换后的头几轮不会有可比增益,这一点闭环设计第 10 条已有记载。

## 9. 测试策略

默认测试套件不连远端,不需要 GPU。

- **阶段路径等价。** 给 `MockExecutor` 包一层只暴露 `run_sft_stage` 的适配器,断言它与逐步路径产出相同的摘要、存点集合和 `StepResult` 序列。这是对第 4 节改动的主要回归保护。
- **段边界。** 覆盖 `total` 恰为存点步整数倍、不是整数倍、小于存点间隔三种情况;覆盖续训从中间存点开始。再加行数维度:等长剧集、长短不一导致段被切细、行数不是 8 的倍数时确定性失败。
- **`compute` 包。** 用假的 SSH 传输(记录命令,回放预设输出)测试:`start` 幂等、`stop` 只处理本作业的进程组、租约的创建/陈旧回收/竞争、预检不过。命令拼接测引号:作业 id 和路径里带空格、引号、`$()` 时不会被 shell 解释。
- **`RollExecutor`。** 假传输加固定的日志与指标文件:数据渲染(多轮监督按句展开、上下文只作输入、哈希稳定)、行数整除性预检在动执行器之前、`job_id` 含 `run_id` 与起点存点且对相同输入稳定、失败后换尝试序号重跑、非零退出、超时、step 从 ROLL 记录读出并与 `first_step` 对账、指标缺键。
- **转换器。** 幂等(已有完成标记直接返回)、`.tmp` 清理范围、引用格式。
- **评测。** 假子进程与假隧道:`candidate_id` 经产物解析回 `BigRef`、`prediction.jsonl` 回填、答案归一化后才比对 `reference_answer`、k 条不齐、缺失 item 抛 `UnmappedItemsError`、服务启动超时仍会释放租约。
- **证据等级。** 混合组件时 `diagnostic_grade` 不返回 real 的回归测试。
- **真实冒烟(`@pytest.mark.remote`,默认跳过,需要 `compute.yaml`)。** 一条 8 步的最小 SFT 加一次转换加 2 个 item 的评测,验证整条链路能走通。它证明接线正确,不证明模型变好。

## 10. 未核实项与待你确认

下面每一项都会影响设计。标"阻塞"的在实现开始前要有答案。

**待你确认的设计假设**

- **A1,已定:多轮监督。** 按行展开实现(第 5.2 节第 1 步),代价是上下文重复前向,且作业段要按"段内各步行数一致"再切(第 5.2 节第 2 步)。
- **A2,已定:方案 B。** 混合后端,等级不升,另加 `component_backends` 记录字段(第 8 节)。

**需要在主机上验证**

- **V1(阻塞)。** ROLL 的 dataloader 是否按文件顺序消费 `train.jsonl`、是否打乱。决定续训等价(第 5.2 节"数据顺序约束")。
- **V2(阻塞)。** `--max-steps` 配合 `--resume` 是绝对步数还是追加步数。第 5.2 节假设是绝对步数。
- **V3(阻塞)。** 容器里的 vLLM 能否用 `vllm serve` 加载这种导出的适配器。文档记录的是探针通过。
- **V4。** `run_sft_validation.py` 的指标输出位置和格式(逐步 loss)。第 5.2 节第 7 步依赖它。
- **V5。** `.181` 上当前的 GPU 占用与可用内存。预检阈值要据此定。
- **V6(阻塞)。** SoMBench 样本和 `DiagnosticItem` 之间是否有 id 对应关系,以及 `item_map.json` 由谁生成。
- ~~V7~~,**已核实。** 后缀只被 `MockConverter` 用,`_is_inference_format` 看 `fetch_hint`(第 6.2 节第 4 步)。
- **V8。** SoMEval 的子进程入口怎么调(命令行还是需要小包装脚本),依赖能否装进现有环境。
- **V9(阻塞)。** `run_sft_validation.py --resume` 的参数形态,以及续训时存点目录如何指定。
- **V10。** 该脚本里 `--max-steps`、`--train-records` 与配置里 `num_train_epochs` 的关系:数据行数变化时步数怎么算,`--max-steps` 能否在 epoch 中途停下并写存点。
- **V11(阻塞)。** 渲染配置时覆盖 `gradient_accumulation_steps` 是否安全,以及 ROLL 是否按 DP8 × 每卡批 × 累积 消费整批(第 5.2 节第 2 步的算式依赖它)。顺带确认 `--sequence-length` 对长上下文行的截断行为:行展开会让后面的行变长,被静默截断就等于丢监督。
- **V13。** 诊断评测失败时,报告与 directive 各自落到什么状态。`diagnostic/orchestrator.py` 与守护循环对评估器抛异常的处理我没读到,第 7.3 节末尾那段依赖它。
- **V14(阻塞)。** `_rule_hit` 的比较方式(`someval_adapter.py:92`),以及题库里 `reference_answer` 的实际写法。它直接决定真实模型输出要怎么归一化才可能得分,不确认就等于不知道真实评测的分数意味着什么。
- ~~V12~~,**已核实。** 载荷在 `diagnostic/store_adapter.py:16` 的 `write_report` 里组装,`component_backends` 加在那里(第 8 节)。

**本机还没读的部分**

子系统设计里的 SSH 约定、`_rule_hit` 的判分细节还没读,实现计划阶段先读。`factory` 的装配入口已核实,见第 12 节。

## 11. 实施顺序

1. 编排器的 `run_sft_stage`、`stage_end` 与等价性测试。完全在本机,不依赖远端,先落地。同一步里把 `CheckpointCorruptError` 补进两处 `_DETERMINISTIC`、把 `executor.load` 纳入 `try`,并补跨模式续训的拦截(第 7.3 节、第 6.1 节)。
2. `compute` 包(配置、传输、`job_tool`、租约、预检)和假传输测试。
3. 在主机上做 V1 到 V5 的验证,结果回填本文档。
4. `RollExecutor` 与 `RollLoraConverter`。
5. 评测:先 V3、V6、V8,再做 `RealSoMEvalRunner`。
6. 装配(接线点见第 12 节):`assemble` 的真实后端分支、`component_backends` 记录、`training_defaults.yaml` 的真实模式配置(`grpo_steps: 0`)、远端冒烟。

1 和 2 不依赖任何待验证项,可以并行开始。3 之后每一步都要等对应验证有答案。

## 12. 装配(已核实的接线点)

`factory/wire.py:106` 的 `assemble(store, options, *, env=None, log_dir=None, builders=None)` 是唯一入口。核实到的两条路径:

- 不传 `--enable` 时直接 `return Assembly(make_mindtrainer_dispatcher(), {}, None, False)`,评估器为 `None`。
- 开了闭环开关时注入 `make_mindtrainer_dispatcher(data_producer=TemplateDataProducer())` 与 `build_mock_evaluator(store)`。

真实后端因此只需在 `assemble` 里多一条分支:给 `make_mindtrainer_dispatcher` 传 `executor_factory` 与 `converter`,并把评估器从 `build_mock_evaluator` 换成真实组装。无开关路径一行不动,保持与 `mindtrainer.wire` 字节等价这条既有约定。

真实后端用什么开关表达(复用 `--enable`,还是单独的 `--backends real`),以及 `compute.yaml` 缺失时的退出码,取决于 `factory/switches.py` 与 `FactoryOptions` 的现有形状,实现计划阶段定。
