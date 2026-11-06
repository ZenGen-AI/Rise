# SoMBench 评测接入与训练隔离 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让诊断评测的静态轨跑真实 SoMBench:从 SoMEval 检出目录导入 3481 道题(Q1 到 Q4 全类型)成树内分类表与题库,在远端起 vLLM 服务并用 SoMEval 子进程评分,把结果转成诊断报告;同时用 20 字滑窗哈希准入检查保证这些题不进训练(设计 21 的 G7、G8,DX-16 到 DX-22;设计 90 的 FC-16;设计 30 的 MT-15)。

**Architecture:** 判定与索引在 `diagnostic`(`contamination.py` 新建,`someval_adapter.py` 加 `evaluate_scored` 与 `SampleOutcome`);导入命令、真实运行器、污染准入检查的装配、`--evaluator someval` 开关都在 `factory`(组合根,唯一可以同时认识 `compute` 与 `diagnostic` 的地方);`mindtrainer.RollExecutor` 只增加一个可选的注入点 `row_gate`。依赖方向不变:`diagnostic` 不导入 `compute`、`mindtrainer`、`factory`;`mindtrainer` 不导入 `diagnostic`。

**Tech Stack:** Python 3.11+ / uv / pytest / pyyaml(已有);**新增一个可选依赖组** `sombench = ["pyarrow>=25.0.1"]`,只有 `import-sombench` 用得到,运行时路径与闸门只用标准库。

**Spec:** `docs/design/21-diagnostic-evaluation.md` 的 DX-16 到 DX-22、G7、G8 与第 10 章「SoMBench 评测」「污染索引与准入检查」;`docs/design/90-factory-and-cli.md` 的 FC-16、不变量 8 到 10、装配步骤 4 与 4b;`docs/design/30-mindtrainer.md` 的 MT-15 与「训练数据准入检查」;`docs/design/00-overview.md` 的 SYS-24。

## 已核实的事实(实现者不要再猜)

撰写时在 `19e7b78` 上逐条核对,行号会漂,以符号名为准:

| 事实 | 依据 |
|---|---|
| SoMBench 本地文件 `~/develop/SoMEval/datasets/SoMBench/test-00000-of-00001.parquet`:3481 行、`meta.id` 全部唯一、2969502 字节、sha256 前 12 位 `5ace4bd4b872` | pyarrow 实读 + 数据集卡片 `num_examples: 3481`、`download_size: 2969502` |
| 题型 Q1 713、Q2 626、Q3 1661、Q4 481;`variant` base 1704、hardest 1209、varA 284、varB 284;3 个 L1、17 个 L2、71 个 L3,每个 L3 42 到 53 道 | 同上 |
| id 形如 `1.1.1_0_first_person\|base\|Q1`;Q3 多一段 `\|item1` 到 `\|item3`,按 `\|` 切是 4 段(1661 条),其余是 3 段(1820 条) | 同上 |
| `varA`/`varB` 的 id 在变体段前多 `_varA`/`_varB`(如 `1.1.1_0_first_person_varA\|varA\|Q1`);把头部去掉后缀、换回 `base`、保留其余段,284 + 284 条**全部**唯一对上一个 base 行;`hardest` 同理 1209 条全部唯一 | 实测脚本 |
| 有 `varA`/`varB` 变体的基题共 529 个,其中 490 个有一道变体、39 个有两道 | 同上 |
| `hardest` 与基题的**故事逐字相同**(1209/1209),题干相同的是 0;`varA` 故事相同 0/284、题干相同 204;`varB` 故事相同 0/284、题干相同 163 | 同上 |
| 文本段(故事、问题、正确选项、错误选项、`q4_reference`)共 12957 段;20 字滑窗去重后 763027 个,8 字节哈希约 5.8 MiB;出现在 10 段以上不同文本里的窗口 37 个,50 段以上 0 个 | 实测脚本 |
| `meta` 键:`dim`、`dim1`、`dim2`、`domain`、`id`、`lang`、`length_mode`、`pair_id`、`perspective`、`q4_reference`、`qtype`、`review_fail_reason`、`review_pass`、`variant`;`answer` 键只有 `correct_answers`、`wrong_answers` | 同上 |
| SoMEval 的 `detailed_metrics.jsonl` 每行:`is_correct`、`error_reason`、`extracted`、`sample_id`、`repeat`、`prompt_type`;开放题另有 `judge_score`、`judge1_score`、`judge_reason`。一次历史运行里 `error_reason` 只出现 `wrong_answer`、`below_threshold`、`None` | 读 `results/SoMBench/Qwen3.8-Flash-Next/exp_20260830_123410` |
| SoMEval 入口 `run_eval.py --experiment-config <路径> [--exp-dir <后缀>]`;stage 与 datasets 从实验配置读;`tasks/SoMBench/run.py` 只用 `Path(__file__)` 定位它自己的配置,所以**当前目录可以是我们自己的工作目录**;但 `pipeline.py` 的 `log_dir = Path("logs")` 与 `Path(f"tasks/{dataset}")` 是**相对当前目录**的,而 `tasks/` 在检出目录里 | 读 `run_eval.py`、`tasks/SoMBench/run.py`、`src/evaluation/pipeline.py` |
| SoMEval 的结果目录是 `results/<数据集>/<模型名>/exp_<RUN_TIMESTAMP>`,时间戳由 `run_eval.py` 设为环境变量 `RUN_TIMESTAMP`,所有子进程共用 | `src/evaluation/paths.py` |
| SoMEval `save_config` 会剥掉 `llm_config`、`judge1`、`judge2` 的 `api_key` 与 `api_url`;历史 `config.json` 里确实没有这两项 | 读 `src/evaluation/paths.py` 与历史结果 |
| SoMEval 协议表:`cot` = temperature 0.6、max_tokens 32768、n_samples 1、`enable_thinking` 真 | `src/evaluation/protocols.py` |
| SoMEval 的模型客户端 `ContentClient.generate` 重试 5 次后返回 None,不抛异常;样本因此落成无内容、计为错 | `src/llm/content_client.py` |
| 历史一次 SoMBench 完整运行(预测 + 判分 + 出图)2 小时 06 分;只判分 22 分钟 | `logs/实验_SoMBench_*` 的首末时间戳 |
| `compute.vllm_service.VllmServeConfig`、`compute.service.serve`、`compute.tunnel.SshTunnel` 已落地并有测试 | `19e7b78` |
| `RollExecutor.run_sft_stage` 在 `self.client.put(train_path, data)` **之前**组装好 `data`(`bytes`),这就是准入检查要拦的位置 | `src/mindtrainer/roll_executor.py` |
| `RollExecutor.__init__` 现在收 `client`、`endpoint`、`run_id`、`checkpoint_root`、`heldout` 等;项目里没有 `dataclasses.replace` 的用法,测试都直接构造 | `src/mindtrainer/roll_executor.py`、`tests/test_mt_roll_executor.py` |
| `diagnostic.datamodel.DiagnosticItem(id, l3_task, item_type, scoring_mode, track, reference_answer, rubric, family_id)`;`ItemPool(version, items, variant_families)`;`DiagnosticTaxonomy(version, l1_dimensions, l2_dimensions, l3_tasks, g_probes)` | 读源码 |
| 编排器对 `scoring_mode == "rule"` 的静态题用 `RuleScorer` 重打分,其余原样保留 `run_static_items` 的结果 | `src/diagnostic/orchestrator.py` |
| `factory.evaluation` 的 `evaluator.yaml` 只用于 mock 评估器;`build_mock_evaluator` 在 `store` 里重新读资产 | 读源码 |
| 本项目 `pyproject.toml` 只有 `pyyaml` 一个运行时依赖;SoMEval 的 venv 用 Python 3.14.3、pyarrow 25.0.1、datasets 5.0.1 | 读两边 |
| 全量测试基线 **2868 passed**(`19e7b78`) | 实测 |

## Rulings

设计没规定或与现状冲突的细节,在这里裁决。每条给出判错的代价。

1. **SoMEval 子进程用检出目录作为当前工作目录,结果与日志用绝对路径或环境变量指定。** 原因:`pipeline.py` 的 `log_dir`、`task_dir` 是相对当前目录的,换到别的工作目录会让它读不到 `tasks/SoMBench/config.yaml`。代价**:`logs/` 与 `figures/` 会写进检出目录**,而检出目录里有他人未提交的改动。缓解:只用 `stage` 的 `predict` 与 `metric`(这两条路径都不建 `figures/`),并在运行前后各记录一次检出目录里我们可能触碰的三类路径(`logs/`、`results/`、`figures/`)的文件清单,运行后若有新增或改动,报 T3 并让人看一眼。这与设计 21 里「不把输出写进检出目录」的措辞冲突,**实现前必须按这条裁定改设计的措辞**(Task 8 的第 1 步)。
2. **评测的工作目录(实验配置、结果根、指纹)放树外,用绝对路径传给 SoMEval。** `results_path` 设为 `<工作根>/runs/<candidate>/<时间戳>/results`,`normalized_datasets_path` 设为 `<检出目录>/datasets`。这样 `prediction.jsonl`(含完整题面)不在检出目录,也不在产物树。
3. **结果目录名由指纹而不是时间戳定位。** `run_eval.py` 会在环境里设 `RUN_TIMESTAMP`,所有子进程共用,所以先算 `结果根/SoMBench/forge/exp_<时间戳>` 的预期路径,读完 `detailed_metrics.jsonl` 后核对 `config.json` 里 `experiment_config` 的指纹与本次一致;一致才采信。不一致报 T2。
4. **模型名固定 `forge`。** 基座与适配器在同一个 vLLM 端点上都以 `forge` 这一路 LoRA 模型出现(基座另名为 `base`,不评)。代价:报告里看不出评的是哪个候选,靠 `candidate_ref` 区分。
5. **`SampleOutcome` 三字段,`raw_outputs` 存抽出的字母。** `ItemResult.raw_outputs` 是既有字段,会进 `ItemResult.to_dict()`,但报告只带聚合,`ItemResult` 不落盘;为了保守,存 `extracted`(题面内容不在内)而不是模型完整输出。
6. **`error_reason` 的处理。** `judge_error` → 整次评测作废,抛延后异常。`content_none` → 计数,占比超过 `content_none_ratio_max`(默认 0.01)即抛延后异常;不超过则计为错,数量写进报告 `someval` 块。`extraction_failed`、`wrong_answer`、`below_threshold` → 计为错。未知原因码 → 计为错并写进 `someval` 块的 `unknown_error_reasons` 计数。理由:延后通道会重试,而确定性内容问题重试无益,要同时避免两类误判。
7. **`someval` 块写在报告载荷,** 由 `diagnostic.report.build_report` 增加一个可选参数 `someval: dict | None = None`,不进 `DiagnosticReport` 数据类(它只承载判级相关字段),由 `write_report` 原样放进载荷。`diagnostic_grade` 不看它。
8. **版本号。** 分类表与题库版本都是 `sombench-<parquet 的 sha256 前 12 位>`,例如 `sombench-5ace4bd4b872`。运行器与准入检查装配时重新算 parquet 哈希,与版本里的前缀比对,不一致拒绝。`benchmark_version` 仍是 `evaluator.yaml` 里人写的那个字符串,建议写 `benchmark-sombench-v1`,换版本号由人做。
9. **索引文件格式。** 头部 48 字节:`b"SBIDX1\0\0"`(8)+ 窗口数(8,小端无符号)+ parquet 完整 sha256 的 32 字节原始值;其后是窗口数个小端无符号 64 位整数,升序。读取时校验魔数、长度与文件大小完全一致,并把头部哈希与 parquet 现算哈希比对。
10. **索引与配置都不进树。** 索引路径、SoMEval 检出目录、工作根、远端服务端口、裁判凭证全在树外本机配置 `RISE_EVAL_CONFIG` 指向的文件里,权限必须 0600,且路径不得落在仓库根之下。
11. **准入检查先解析 `train.jsonl` 再查。** 每一行是 `{"instruction": ..., "output": ...}`,对两个值做规范化与滑窗;解析失败同样按确定性失败处理(那说明数据本身坏了)。返回 `(bad_lines, hits)`,零命中才放行。
12. **`RollExecutor` 的注入点是可选关键字参数 `row_gate: Callable[[bytes], tuple[int, int]] | None = None`,** 为 `None` 时不检查;`factory` 在 `--executor roll` 下**恒构造并注入**;直接构造 `RollExecutor` 的既有测试因此不受影响。判错的代价:绕过组合根手写脚本仍能跳过检查,设计上以「真训练只经组合根」为前提。
13. **`--evaluator someval` 的服务上下文上限用常量 `SOMBENCH_MAX_MODEL_LEN = 40960`,** 不进配置;端口用本机配置里的 `serve_port`,缺省 18081。原因:上限由协议固定(max_tokens 32768 加最长提示约 1700 字),没有调的余地。
14. **SoMEval 分两次子进程:先 `predict` 再 `metric`。** 只判分时用 `--exp-dir` 指向 `exp_<时间戳>` 后缀。`metric` 结束才读结果,`visualize` 不跑。
15. **凭证扫描的范围是工作目录下所有文件,逐字节查找本机配置里出现的每个凭证字符串。** 命中就删工作目录并抛不继承 `DeferredError` 的 `EvalSecretLeakError`,让守护进程退出。扫描不打印任何取值,只写相对路径。
16. **`factory import-sombench` 需要可选的 `pyarrow`,** 缺依赖时报 `FactoryConfigError`,提示 `uv run --extra sombench`。运行器与准入检查不需要它。
17. **真机验证(GPU 现在被别的任务占着)拆到 Task 9,** 明确标为「待 GPU 空闲后补」,不阻塞前八个任务。
18. **本轮不改 `diagnostic.isolation`。** 它对中文无效但已有测试与调用方为零,留着并保持现状;新的判定全部在 `contamination.py`。

## Global Constraints

每个任务的要求都隐含包含本节。

- 新文件都以 `from __future__ import annotations` 开头(模块 docstring 之后的第一条语句也算)。
- **依赖方向:** `diagnostic` 只导入标准库、`diagnostic.*`、`controlplane.*`、`simulator.*`;**不导入** `mindtrainer`、`compute`、`factory`、`trainscheduler`、`recipestore`。`mindtrainer` 不导入 `diagnostic`。`factory` 可以导入任何包。既有边界测试(`tests/test_factory_cross_plan.py`、`tests/test_diag_evaluator.py` 的导入扫描)必须继续通过。
- **不引入新的运行时依赖;** `pyproject.toml` 只加一个可选依赖组 `[project.optional-dependencies] sombench = ["pyarrow>=25.0.1"]` 与 `[dependency-groups]` 里对应的 `dev` 条目(Task 1)。
- **凭证绝不进 git 树、命令行或日志。** 本机配置里读出的值只进子进程环境与 `Authorization` 头;配置错误只写键名与路径。
- **不修改 `~/develop/SoMEval` 的任何文件。** 只读它的 `datasets/`、`tasks/SoMBench/`、`src/` 与入口脚本;`logs/`、`results/`、`figures/` 按 Ruling 1 处理并前后比对。
- **不碰** `cli/`、`schemas/`、`compute/`(已落地,不改)。
- `git add` 逐个文件,**不要用 `git add .`**;**提交信息不加 `Co-Authored-By` 行**。
- **测试命令**一律是 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`(下文简称"全量命令",跑单个文件时在末尾加路径)。不得出现警告。
- **测试数:** 基线 **B = 2868**(`19e7b78`)。以各任务的增量为准。

## 文件表

| 文件 | 动作 | 职责 |
|---|---|---|
| `src/diagnostic/contamination.py` | 新建 | 规范化、滑窗哈希、索引构建与读写、`parse_train_jsonl`、准入检查工厂 |
| `src/diagnostic/datamodel.py` | 改 | 加 `SampleOutcome`(带 `to_dict`/`from_dict`) |
| `src/diagnostic/someval_adapter.py` | 改 | 加 `ScoredSoMEvalRunner` 协议与 `run_static_items_scored` |
| `src/diagnostic/orchestrator.py` | 改 | `someval` 模式走已判好的结果;`someval_stats` 透传 |
| `src/diagnostic/report.py`、`store_adapter.py` | 改 | 报告载荷加可选的 `someval` 块 |
| `src/mindtrainer/roll_executor.py` | 改 | 可选 `row_gate`,上传前调用 |
| `src/mindtrainer/errors.py`(或 `executor.py`) | 改 | `TrainingDataRejectedError`(确定性) |
| `src/factory/sombench.py` | 新建 | `import-sombench`:读 parquet、写分类表/题库/索引、版本与哈希比对 |
| `src/factory/sombench_runner.py` | 新建 | `SomevalRunner`:起服务、跑 SoMEval 子进程、解析、凭证扫描、`SampleOutcome` |
| `src/factory/eval_config.py` | 新建 | 读树外本机评测配置;权限、路径、键的校验 |
| `src/factory/evaluation.py` | 改 | `build_someval_evaluator` |
| `src/factory/wire.py`、`cli.py` | 改 | `--evaluator someval`、`import-sombench` 子命令、装配与校验 |
| `pyproject.toml` | 改 | 可选依赖组 `sombench` |
| `tests/test_diag_contamination.py` | 新建 | Task 2 |
| `tests/test_diag_someval_scored.py` | 新建 | Task 3 |
| `tests/test_mt_roll_gate.py` | 新建 | Task 4 |
| `tests/test_factory_sombench_import.py` | 新建 | Task 5 |
| `tests/test_factory_eval_config.py` | 新建 | Task 6 |
| `tests/test_factory_sombench_runner.py` | 新建 | Task 7 |
| `tests/test_factory_wire_someval.py`、`tests/test_factory_cli.py` | 新建/改 | Task 8 |
| `docs/design/21-diagnostic-evaluation.md` | 改 | Task 8:按 Ruling 1 改工作目录措辞,回填实测数 |
| `docs/superpowers/plans/2026-10-19-sombench-eval.md` | 本文件 | — |

## Task 1: 可选依赖组、`SampleOutcome` 与导入命令的骨架

**Files:** 改 `pyproject.toml`;新建 `src/factory/sombench.py`(先只有常量与 `import_sombench` 的签名与校验骨架)、`tests/test_factory_sombench_import.py`。

- [ ] **Step 1: 写失败的测试**

覆盖:版本号由 parquet 的 sha256 前 12 位生成;`meta.id` 缺失或重复时拒绝;不依赖 `pyarrow` 就能导入本模块(把 `import pyarrow` 放在函数内部)。

- [ ] **Step 2: 跑,确认失败**(全量命令 + `tests/test_factory_sombench_import.py`,期望 ImportError 或 AttributeError)

- [ ] **Step 3: 实现**

`pyproject.toml`:

```toml
[project.optional-dependencies]
sombench = ["pyarrow>=25.0.1"]
```

`src/factory/sombench.py` 顶部只写常量与纯函数:

```python
VERSION_PREFIX = "sombench-"

def sombench_version(parquet_sha256: str) -> str:
    """版本号取数据集哈希前 12 位:数据集一变,版本必须跟着变。"""
    return f"{VERSION_PREFIX}{parquet_sha256[:12]}"

def check_sombench_version(version: str, parquet_sha256: str) -> None:
    expected = sombench_version(parquet_sha256)
    if version != expected:
        raise FactoryConfigError(
            f"the diagnostic assets are versioned {version!r} but the dataset hashes to {expected!r}"
        )
```

- [ ] **Step 4: 跑通**,期望新增 4 个用例通过
- [ ] **Step 5: Commit**(逐个 `git add`)

```bash
git commit -m "feat(factory): SoMBench 版本号与数据集哈希绑定"
```

## Task 2: 污染索引与准入检查(`diagnostic.contamination`)

**Files:** 新建 `src/diagnostic/contamination.py`、`tests/test_diag_contamination.py`。

覆盖的判定:

- 规范化:NFKC、小写、只留字母数字(含汉字),全角标点与空白被丢弃。
- 窗口长度 20;短于 20 的文本没有窗口。
- 索引:魔数、窗口数、数据集 sha256 头部;长度不符、魔数不符、哈希不符都拒绝。
- 逐字拷贝命中;删掉最后一句仍命中(这正是词元相似度做不到的);两句无关中文不命中。
- 阈值:命中数大于 0 即拒绝。
- `parse_train_jsonl`:每行两个键;解析失败、缺键、非字符串都抛确定性异常且不吞原始异常类型。
- 原因字符串只含行数与窗口数,不含文本。

- [ ] **Step 1: 写失败的测试**(用合成文本,不读真实 parquet)
- [ ] **Step 2: 跑,确认失败**
- [ ] **Step 3: 实现**

```python
WINDOW = 20
MAGIC = b"SBIDX1\0\0"

def normalize_text(text: str) -> str: ...
def shingles(text: str) -> set[int]: ...            # 8 字节 blake2b,升序返回
def build_index(texts: Iterable[str], dataset_sha256: str) -> bytes: ...
def load_index(data: bytes, dataset_sha256: str) -> array("Q"): ...
def count_hits(index, text: str) -> int: ...
class TrainingDataRejectedError(Exception): ...    # 只含计数
def make_train_gate(index, dataset_sha256) -> Callable[[bytes], tuple[int, int]]: ...
```

`make_train_gate` 返回的可调用对象收整份 `train.jsonl` 字节,返回 `(bad_lines, hits)`;`hits > 0` 时由 `RollExecutor` 抛 `TrainingDataRejectedError`(Task 4)。

- [ ] **Step 4: 跑通**,期望新增约 12 个用例
- [ ] **Step 5: Commit**

## Task 3: `SampleOutcome`、`evaluate_scored` 与编排器

**Files:** 改 `src/diagnostic/datamodel.py`、`someval_adapter.py`、`orchestrator.py`、`report.py`、`store_adapter.py`;新建 `tests/test_diag_someval_scored.py`;改既有 `tests/test_diag_orchestrator.py`(只加用例)。

覆盖的判定:

- `SampleOutcome` 往返;`score` 只接受 0 或 1。
- `run_static_items_scored` 把 `SampleOutcome` 转成 `ItemResult`,`raw_outputs` 是 `extracted`,`per_sample_scores` 是 `score`。
- 题库里有 `someval` 题而运行器没有 `evaluate_scored` → 跑任何一题之前抛确定性异常。
- 编排器:`someval` 题不被 `RuleScorer` 重打分;`rule` 题的行为不变(既有用例)。
- 报告:传了 `someval` 块就原样落盘;不传时载荷里没有这个键(既有用例)。
- `diagnostic_grade` 对带 `someval` 块但不带 `backends` 的报告仍判 `stub`.

- [ ] **Step 1: 写失败的测试**
- [ ] **Step 2: 跑,确认失败**
- [ ] **Step 3: 实现**(按 DX-16、Ruling 5、Ruling 7)

`src/diagnostic/datamodel.py` 追加:

```python
@dataclass
class SampleOutcome:
    """一道题的一次判定。真实运行器返回它;`extracted` 是抽出的答案字母,不是模型的完整输出。"""

    score: int              # 0 或 1
    extracted: str          # 选择题为字母;开放题与抽取失败为空串
    error_reason: str | None  # SoMEval 的原因码;正确时为 None

    def __post_init__(self) -> None:
        if self.score not in (0, 1):
            raise ValueError("score must be 0 or 1")
```

`src/diagnostic/someval_adapter.py` 追加:

```python
class ScoredSoMEvalRunner(SoMEvalRunner, Protocol):
    """能直接给出每样本判定的运行器。选项被打乱、答案以文本存放,规则打分器对这类题不成立。"""

    def evaluate_scored(self, items, candidate_ref: str, k: int) -> dict[str, list[SampleOutcome]]: ...


def run_static_items_scored(items, runner: ScoredSoMEvalRunner, candidate_ref: str, k: int) -> list[ItemResult]:
    """把已判好的结果转成 `ItemResult`;`raw_outputs` 存抽出的字母,不存模型全文。"""
    raw = runner.evaluate_scored(items, candidate_ref, k)
    results = []
    for item in items:
        outcomes = raw[item.id]
        scores = [o.score for o in outcomes]
        results.append(ItemResult(
            item_id=item.id,
            raw_outputs=[o.extracted for o in outcomes],
            per_sample_scores=scores,
            pass_rate=pass_rate(scores),
            variance=variance(scores),
            track=item.track,
        ))
    return results
```

- [ ] **Step 4: 跑通**,期望新增约 10 个用例
- [ ] **Step 5: Commit**

## Task 4: 训练数据的准入检查注入点

**Files:** 改 `src/mindtrainer/roll_executor.py`、`src/mindtrainer/executor.py`(或新建 `src/mindtrainer/errors.py` 放 `TrainingDataRejectedError`);新建 `tests/test_mt_roll_gate.py`。

覆盖的判定:

- 不传 `row_gate` 时,上传的内容与今天逐字节相同(用既有 `FakeClient` 的 `put` 断言)。
- 传了 `row_gate`,返回 `(0, 0)` 时照常上传。
- 返回 `(2, 7)` 时抛 `TrainingDataRejectedError`,**远端什么都没写**(`put` 没被调用),异常信息只有 `2` 与 `7`,不含任何文本。
- 检查的字节与将要上传的字节完全相同(含续训前缀行)。
- 异常是确定性错误,不在任何 `DeferredError` 子类里。

- [ ] **Step 1: 写失败的测试**
- [ ] **Step 2: 跑,确认失败**
- [ ] **Step 3: 实现**(按 Ruling 12)
- [ ] **Step 4: 跑通**,期望新增约 5 个用例
- [ ] **Step 5: Commit**

## Task 5: `import-sombench`(分类表、题库、污染索引)

**Files:** 补全 `src/factory/sombench.py`;补全 `tests/test_factory_sombench_import.py`。

覆盖的判定:

- 用一个小的合成 parquet(临时文件,含 `story`、`question`、`answer`、`meta` 结构与长文本)跑通,断言:
  - 分类表 3 个 L1、2 个 L2、3 个 L3(按合成数据),`g_probes` 为空;
  - 题库条目数等于行数,id 与 `meta.id` 一一对应,`scoring_mode` 全为 `someval`,`reference_answer` 与 `rubric` 全为 `None`;
  - 变体族:只有 `varA`/`varB` 行入族,`hardest` 不入族;
  - 树内资产序列化后**不含**合成故事里的任一 20 字片段(扫描 `yaml.safe_dump` 的结果);
  - 索引头部记录的哈希与 parquet 现算一致;索引里的窗口数大于 0;
  - 同一份输入跑两次,`git` 不产生第二次提交(沿用 `write_product` 的幂等)。
- 真实 parquet 的一次性冒烟放在 Task 9(需要 GPU 无关,但会读用户目录,单独跑)。

- [ ] **Step 1: 写失败的测试**
- [ ] **Step 2: 跑,确认失败**
- [ ] **Step 3: 实现**;`import pyarrow.parquet` 放在函数内,缺依赖时提示 `uv run --extra sombench`
- [ ] **Step 4: 跑通**,期望新增约 8 个用例
- [ ] **Step 5: Commit**

## Task 6: 树外本机评测配置

**Files:** 新建 `src/factory/eval_config.py`、`tests/test_factory_eval_config.py`。

配置键(缺一即 `FactoryConfigError`,多余键同样拒绝):

| 键 | 含义 |
|---|---|
| `someval_root` | SoMEval 检出目录的绝对路径 |
| `someval_python` | 跑 SoMEval 的解释器绝对路径(通常是它 venv 里的 python) |
| `work_root` | 评测工作根(树外),每次运行在其下新建子目录 |
| `index_path` | 污染索引文件路径 |
| `serve_port` | 远端 vLLM 端口,缺省 18081 |
| `judge_base_url` | 裁判端点地址 |
| `judge_api_key` | 裁判密钥 |
| `content_none_ratio_max` | 无答案占比阈值,缺省 0.01 |

覆盖的判定:

- 文件不存在、权限不是 0600(用 `os.chmod` 造一个 0644)、路径落在仓库根之下、键缺失或多余,都拒绝,且错误信息不含任何取值。
- `RISE_EVAL_CONFIG` 未设时,`--evaluator someval` 的装配报用法错误。

- [ ] **Step 1: 写失败的测试**
- [ ] **Step 2: 跑,确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑通**,期望新增约 8 个用例
- [ ] **Step 5: Commit**

## Task 7: 真实运行器(`factory.sombench_runner`)

**Files:** 新建 `src/factory/sombench_runner.py`、`tests/test_factory_sombench_runner.py`。

构造参数(全部注入,测试不起服务):`store`、`candidate_ref` 由调用时给、`client`、`endpoint`、`eval_config`、`build_job`/`serve`/`tunnel_factory`/`probe`(缺省取 `compute` 的真实实现)、`now`(时间戳,测试给定)、`run`(子进程执行器,缺省 `subprocess.run`)。

流程(按设计 21 第 10 章的 6 步):

1. 读 `candidate_checkpoint.yaml`,取 `checkpoint_ref`;`fetch_hint.type` 必须是 `roll_ssh`;`uri` 的端点必须等于 `endpoint.endpoint_id`。
2. `serve(...)`,基座 `base_model_path`、适配器 `forge` 指向导出目录、`max_model_len=40960`、`NCCL_NVLS_ENABLE=0` 由 `build_serve_job` 负责。
3. 写实验配置到工作目录;环境:`VLLM_API_KEY`=服务密钥、`OPENAI_API_KEY`=`judge_api_key`、`OPENAI_BASE_URL`=`judge_base_url`、`RUN_TIMESTAMP`。
4. 两次子进程(`predict`、`metric`),超时分别可配。
5. 解析 `detailed_metrics.jsonl`(按 Ruling 6);核对 `config.json` 指纹。
6. 凭证扫描;返回 `(outcomes: dict[str, list[SampleOutcome]], someval_stats: dict)`。

覆盖的判定:

- 候选是 mock 导出、端点不一致、`candidate_checkpoint.yaml` 缺失 → 确定性异常。
- `judge_error` 出现一次 → 延后异常,报告不写。
- `content_none` 占比 = 阈值时放行(边界),超过则延后。
- 结果里缺题库里的某道题 → 确定性异常且原因里只有数量。
- `config.json` 指纹不符 → 确定性异常。
- 子进程非零退出 → 延后异常。
- 工作目录里某个文件含裁判密钥 → `EvalSecretLeakError`,工作目录被删,异常信息不含密钥。
- 子进程命令:两次都带 `--experiment-config <绝对路径>`,`metric` 那次带 `--exp-dir exp_<时间戳>`;当前目录是 `someval_root`(Ruling 1)。
- `someval_stats` 的键:协议、样本数、`extraction_failed`、`content_none`、`unknown_error_reasons`、`dataset_sha256`、`someval_commit`、(工作树不干净时)`dirty`。

- [ ] **Step 1: 写失败的测试**(用假的 SoMEval 入口脚本写固定 `detailed_metrics.jsonl`;`serve` 换成上下文管理器替身)
- [ ] **Step 2: 跑,确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑通**,期望新增约 14 个用例
- [ ] **Step 5: Commit**

## Task 8: 装配(`--evaluator someval`、准入检查注入、命令行)与设计回填

**Files:** 改 `src/factory/evaluation.py`、`wire.py`、`cli.py`;新建 `tests/test_factory_wire_someval.py`;改 `tests/test_factory_cli.py`;改 `docs/design/21-diagnostic-evaluation.md`。

覆盖的判定:

- `--evaluator someval` 不给 `--executor roll` → 退出码 2;给了 `--enable` → 退出码 2。
- `--executor roll` 缺索引或哈希不符 → 退出码 2;索引正常 → 执行器拿到非空的 `row_gate`。
- `--evaluator someval` 且配置齐全 → 评估器是真实运行器包装,`backends="real"`,`component_backends={executor: real, converter: real, someval: real, judge: unused}`。
- `import-sombench` 子命令的参数解析与退出码。
- 默认路径(不传任何新开关)与今天逐字节相同。

设计回填:

- 设计 21 的「SoMBench 评测」一节:按 Ruling 1 改工作目录的措辞(SoMEval 以检出目录为当前目录,`logs/` 会写进去,前后比对);把「结果目录名由时间戳定位」改成指纹;写清 `someval_stats` 的键。
- 设计 21 的测试策略表:把新增测试文件名与用例数按实测填。

- [ ] **Step 1: 写失败的测试**
- [ ] **Step 2: 跑,确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑通**
- [ ] **Step 5: 跑文档工具**

```bash
cd docs/design
python3 tools/sync_docs.py && python3 tools/check_docs.py && python3 tools/check_consistency.py
```

期望 `check_docs` 0 个错误、告警回落到 3 条(符号告警因施工计划已出现而消失);`check_consistency` 0 个错误、3 条告警。

- [ ] **Step 6: Commit**

## Task 9: 真机验证(待 GPU 空闲后补)

**Files:** 不改代码;可能改设计文档的实测数字。

- [ ] **Step 1:** 在空闲主机上 `factory import-sombench`,核对 3481 行、71 个 L3、529 个变体族、索引约 5.8 MiB。
- [ ] **Step 2:** 用已有的适配器导出起一次真实评测,记录:服务启动耗时、预测耗时、判分耗时、峰值显存、`content_none` 占比、`extraction_failed` 数,以及 Q4 的 `judge_score` 分布。
- [ ] **Step 3:** 带与不带 `--reasoning-parser` 各跑一批 Q4,比较裁判分数(设计 21 的未核实项)。
- [ ] **Step 4:** 用已有的 episode 跑一遍准入检查,统计误命中率(设计 21 的未核实项)。
- [ ] **Step 5:** 把实测数字回填设计 21 的「未核实项」与「已知局限」两表,把新增的发现写进去。

## 实施记录(施工时补)

- Task 7 的 `SomevalRunner` 增加 `last_stats()`:评估器把最近一次评测的统计写进报告载荷的 `someval` 块。
- Task 8 的装配:真实 SoMEval 评测只允许 `tier: full`、`sampling_k: 1`;其它取值在装配期拒绝。
