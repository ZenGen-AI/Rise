# 数据合成接线:data_spec、DataProducer 与 MindTrainer 派发器的合成分支 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让一条带 `data_spec` 的训练指令在它点名的数据集清单还不在树里时,由派发器调用注入的数据生产器先把数据合成出来,再照常训练成待审候选。默认生产器用模板生成情境与人格、用确定性 rollout 后端产出组内有奖励差异的 episode,产出的数据能一路通过 `resume_or_run`。不注入生产器、或指令不带 `data_spec` 时,派发器的行为与现在完全相同。

**Architecture:** 新模块 `mindtrainer.data_spec` 负责三件纯逻辑的事:`data_spec` 的严格解析(每个字段的类型与范围,错一律抛 `InvalidDataSpecError`)、"要合成哪些阶段"的判定(与读清单共用同一个 `required_stages`)、`data_spec` 与训练配置的交叉校验;并定义 `DataProducer` 协议。新模块 `mindtrainer.data_producer` 提供默认实现 `TemplateDataProducer` 与可注入的 rollout 后端工厂 `keyword_alternating_rollouts`。`mindtrainer.dispatch.make_mindtrainer_dispatcher` 加一个 keyword-only 的 `data_producer`,在读清单之前调用 `_synthesize_missing`。`controlplane` 一行不改。

**Tech Stack:** Python 3.11+ / uv / pytest;**无新增第三方依赖**。

**Spec:**
- `docs/superpowers/specs/2026-10-09-closed-loop-integration-design.md` §9(数据合成接线,R14、G9)。§11 的依赖方向与名字照用。
- 前置:`main` 在 `0385004` 时全量基线为 820 通过、无警告。按设计 §12,计划 1 先落地,实施时的基线会更高(计划 1 自报 978)。**本计划只保证增量**,基线 B 由任务 30 第 1 步实测。
- 本计划只依赖现有代码,**不依赖计划 1 的任何新名字**(设计 §12 把计划 1 列为前置,但本计划实际用不到计划 1 的产出,见 Ruling 1)。

**依赖已有产出的名字**

| 来源 | 名字 |
|---|---|
| `controlplane.daemon` | `DirectiveFailedError`、`Dispatcher`、`reconcile_once` |
| `controlplane.envelope` / `controlplane.gitstore` | `Envelope`、`OwnerRef`、`BigRef` / `GitStore`(`exists`、`read_product`、`write_product`、`list_products`) |
| `controlplane.paths` | `dataset_manifest_file`、`episodes_dir`、`run_dir`、`candidate_checkpoint_file` |
| `mindtrainer.training_config` | `TrainingConfig`、`RewardConfig`、`STAGES`、`load_config_from_directive`、`validate_training_config` |
| `mindtrainer.dispatch` | `make_mindtrainer_dispatcher`、`_DETERMINISTIC`、`_batch_manifests`、`_interact_sources`、`_required_stages`、`_data_run_id` |
| `mindtrainer.orchestrator` / `mindtrainer.grouping` / `mindtrainer.reward_pipeline` | `resume_or_run`、`NotEnoughGroupsError` / `group_by_id`、`count_degenerate_groups` / `build_attribution_backend`、`run_reward_pipeline` |
| `mindtrainer.executor` / `mindtrainer.checkpoint` / `mindtrainer.trajectory_source` | `MockExecutor` / `MockConverter` / `BatchTrajectorySource` |
| `simulator.backends` | `GenerationBackend`、`ModelBackend`、`ScriptedBackend`、`TemplateGenerationBackend` |
| `simulator.generators` | `generate_scenario`、`generate_persona` |
| `simulator.datamodel` | `Persona`、`Scenario`、`RoleSlot`、`RoleBinding`、`RoleAssignment` |
| `simulator.dataset` | `produce_grouped_dataset`、`read_manifest`、`verify_manifest`、`write_manifest`、`DatasetIntegrityError` |

**设计 §9 的各条,本计划怎么处理**

| §9 的条目 | 处理 |
|---|---|
| `data_spec` 载荷格式 | **做**:`parse_data_spec` 严格解析,四个键缺一不可、多一个也拒绝(任务 30) |
| 调度器构造 `new` 指令时写 `stage_manifest_refs` 与 `data_spec` | 不做:那是计划 5(TrainScheduler)的事。本计划钉死它要写出的格式 |
| 派发器"缺清单 + 带 `data_spec` + 数据所在 run 是本 run"才调生产器,其余照旧 | **做**(任务 31 的 `stages_to_synthesize`,任务 33 的 `_synthesize_missing`) |
| 没注入生产器而指令需要它,判 `failed` 并写明缺什么 | **做**(任务 33) |
| 清单已在不重新合成;episode 写完而清单未写时重跑覆盖 | **做**,并有崩溃重入测试(任务 33) |
| 默认生产器 `TemplateGenerationBackend` + `produce_grouped_dataset`,rollout 后端由调用方注入 | **做**(任务 32)。rollout 后端工厂是 `TemplateDataProducer(rollout_factory=...)` 的参数,默认 `keyword_alternating_rollouts` |
| V1 与目标维度无关 | 照做,写进已知限制 |
| `mindtrainer.wire` 注入生产器 | 不做:设计 §10.2 规定 `mindtrainer.wire` 保持原样;组合根 `factory.wire`(计划 9)负责注入 |

**已核实的事实(实现者不要再猜)**

- 清单 id 唯一的构造点是 `simulator.dataset.build_manifest`:`id=f"dsm-{run_id}-{name}"`,文件在 `dataset_manifest_file(run_id, name)` = `control-plane/runs/<run>/dataset_manifest_<name>.yaml`。
- `produce_grouped_dataset` 对每个情境连续 rollout `group_size` 条,episode id 为 `{run}-{阶段}-{情境 id}-r{k}`,**全部 episode 写完之后才写清单**。episode 是 `run_episode` 的纯函数,所以同输入重跑写出逐字节相同的文件,`GitStore.write_product` 对相同内容是空操作。
- 组内退化有两种,判定不同:`validate_groups` 只拦"成员数不足 `min_group_size`"(抛 `DegenerateGroupError`);"成员够、奖励全相同"不报错,只计入 `n_degenerate_groups` 与 `curve_summary["degenerate_groups_total"]`,但这类组的优势恒为 0,训练什么都学不到。所以默认生产器必须让组内奖励真有差异,测试用 `degenerate_groups_total == 0` 钉住。
- `OutcomeSplitAttribution`:训练角色位**任一**发言含某分量**任一**关键词(纯子串、区分大小写),该分量结果分 1.0,否则 0.0;非训练角色位的发言不算。`combined_reward` 是训练轮的均值。所以训练位每句都含关键词时 sum 组合得 `len(components)`,product 组合得 1.0;一句都不含时两者都是 0.0。
- `TemplateGenerationBackend.make_scenario` 的角色位写死为 `role_a`、`role_b`,而训练配置的 `trained_slot` 可以是任意名字(既有测试用 `"a"`)。`run_episode` 要求 `RoleAssignment` 的每个 slot 都在 `Scenario.role_slots` 里,否则抛 `RoleSlotMismatchError`。所以生产器要按配置改写情境的角色位。
- interact 模式只从清单读 `sft`:`_required_stages` 对 interact 只返回 `sft`(配了 `sft_steps` 时),`_interact_sources` 的 `grpo_opd` 来自实时会话。所以对 interact 指令,`data_spec` 只会合成 `sft` 清单。
- 派发器里 `client_factory`/`policy` 缺失的判定在构造数据源之前;读清单那段 `try` 只接确定性的读取错误,`OSError` 原样上抛(`tests/test_mt_dispatch.py` 有用例钉住)。
- `pyproject.toml` 的 wheel 列表登记的是整个 `src/mindtrainer`,新增模块不需要改打包配置;`test_mt_packaging` 只比对顶层包目录。

## Ruling(本计划的裁决)

1. Ruling: 不依赖计划 1,`data_spec` 留在指令载荷里,不做成配置信封 —— `data_spec` 是指令的一部分(设计 §4.2"指令新增键"),不是 `config/` 下的配置文件,补充契约里"配置文件一律是信封"管不到它;本计划用到的名字全部已在 `main` 上 —— 判错的代价:若以后决定把 `data_spec` 挪成独立产物,解析函数不变,只改 `data_spec_from_directive` 的取值来源。
2. Ruling: `data_spec` 严格,恰好四个键 `scenario_count`、`seed`、`purpose`、`stages`,每个阶段恰好一个键 `group_size`;缺键、多键都拒绝 —— 宽松解析会把拼错的键(例如 `scenario_cout`)静默忽略,数据量就悄悄回落成别的值 —— 判错的代价:调度器(计划 5)将来要加字段时,必须同步改 `_KEYS` 与解析函数。
3. Ruling: 范围上限 `scenario_count ∈ [1, 64]`、`seed ∈ [0, 2^31-1]`、`group_size ∈ [1, 16]`,`grpo_opd` 的 `group_size ≥ 2`;`purpose ∈ {training, deployment_simulation}`(取自 `Scenario.purpose` 的注释) —— 上限防止一条写错的指令在一个 tick 里同步写出成千上万个文件、每个都是一次 git 提交;`grpo_opd` 组小于 2 必然零方差 —— 判错的代价:上限是拍的,真需要更大的数据集时改两个常量。
4. Ruling: 配置校验、归因构造与 interact 客户端检查都通过之后,指令只要带 `data_spec` 就先严格解析,哪怕所有清单都已在树里(在那几项检查之前判 `failed` 的指令,坏的 `data_spec` 不会被报出,与 Ruling 8 一致) —— 写错的 `data_spec` 是指令本身的错,等到"需要合成"时才暴露,同一条指令就会时而成功时而失败(取决于清单是否先到) —— 判错的代价:`clone`/`replay` 指令若被调度器误带一个坏的 `data_spec`,会判 `failed` 而不是照常训练;设计 §9 本就规定它们不带 `data_spec`。
5. Ruling: 合成前先用 `check_data_spec_against_config` 交叉校验(要合成的阶段在 `data_spec.stages` 里都有条目;合成 `grpo_opd` 时 `group_size ≥ min_group_size`、`scenario_count ≥ groups_per_step`) —— 这些问题训练时也会以 `DegenerateGroupError`、`NotEnoughGroupsError` 暴露,但那时 episode 已经写进树了,原因也不直接指向 `data_spec` —— 判错的代价:与编排器的校验有一处重复;两边不一致时以编排器为准(它仍会判 `failed`)。
6. Ruling: 新增 `DataSynthesisError` 作为生产器的确定性错误;生产器抛出的 `DataSynthesisError` 与 `_DETERMINISTIC` 里的错误判 `failed`(T2,原因前缀 `data synthesis failed (<类型>: ...)`),其余异常原样上抛、指令保持 `pending` —— 与读清单那段 `try`、与 `resume_or_run` 的既有约定一致 —— 判错的代价:生产器若把确定性错误抛成别的类型,会被当成瞬态,守护循环每轮重派并退出;`NeedsHumanError` 若从生产器抛出会被记成 T2 而非 T3(生产器不会抛它)。
7. Ruling: `_required_stages` 与清单 id 的解析搬进 `mindtrainer.data_spec`(`required_stages`、`manifest_data_run`),`dispatch.py` 保留 `_required_stages` 这个别名、`_data_run_id` 改为调用 `manifest_data_run` —— "要合成哪些阶段"与"要读哪些清单"必须同一份实现,否则会合成用不到的阶段或漏合成;别名让既有代码与测试不动 —— 判错的代价:无行为变化,只是多一层间接。
8. Ruling: interact 指令的合成放在 `client_factory`/`policy` 检查之后 —— 缺实时客户端的指令反正要判 `failed`,先合成只会白写数据 —— 判错的代价:无。
9. Ruling: 默认 rollout 后端 `keyword_alternating_rollouts`:训练位在偶数序号 rollout 里每句都说"每个分量的第一个关键词用空格连起来",奇数序号说一句不含任何关键词的中性话;对手位恒说中性话。中性话从固定候选里取第一句不含任何关键词的,取不到抛 `DataSynthesisError`;非 `outcome_split` 归因也抛 `DataSynthesisError` —— 这样 `group_size ≥ 2` 的每组奖励都是 {高, 0} 混合,sum 与 product 都不退化 —— 判错的代价:数据只证明流程跑通,不代表任何社会能力;judge 归因的指令用默认生产器做不成(而且现有入口本就不提供 judge,派发器在合成前已判 `failed`)。
10. Ruling: 角色位布局由训练配置决定:配置带 `bindings`(interact)就照它(训练位排第一,对手照抄 `model_backend` 与 `model_ref`);不带(batch)就配一个 `frozen_reference` 的对手位 `opponent`(训练位恰好叫 `opponent` 时改叫 `opponent_b`),`model_ref` 默认 `template-opponent-v1`。情境的 `role_slots` 按这个布局改写,背景、冲突、规范仍取模板 —— 模板写死的 `role_a`/`role_b` 与任意 `trained_slot` 对不上(见已核实的事实) —— 判错的代价:batch 数据的对手不是真实的冻结模型,`model_ref` 只是一个标签;它进 `episode_group_id`,所以换了标签就是另一份数据。
11. Ruling: 生产器只写 episode 与清单,不把生成的情境与人格写进 `scenarios/`、`personas/` —— batch 训练与 interact 的 sft 只读清单点名的 episode;模板生成的 id(`scen-auto-<seed>`、`pers-auto-<seed>`)写进共享目录会覆盖同 id 的既有产物 —— 判错的代价:想按 id 查生成的情境时只能看 episode 里的 `scenario_id` 与 `role_assignment`,情境全文不在树里。
12. Ruling: 种子派生:第 i 个情境用 `seed + i`;该情境第 j 个角色位的人格用 `(seed + i) * 角色位数 + j` —— 确定性,且同一情境的各角色位人格不同 —— 判错的代价:不同 `data_spec.seed` 生成的情境集合会大幅重叠(模板池只有 3 种背景)。
13. Ruling: 默认 `max_turns=4`,构造时校验为 `[1, 200]` 的整数(200 与实时服务端的 `max_turns_cap` 一致);`max_turns` 不放进 `data_spec` —— 设计 §9 的载荷格式没有它,加键要改契约 —— 判错的代价:想改轮数只能在组合根里构造生产器时传参,不能按指令调。

14. Ruling: 合成之前,先对不合成的其余阶段(`required_stages` 里不在待合成集合中、且点了清单的阶段)做 id 形状与文件在场的检查,有一份缺了或 id 不对就直接判 `failed`,不调生产器 —— 指令反正要判 `failed`,先合成只会在树里留下一批不属于任何训练的 episode(与 Ruling 8 同理);内容能否读出、与 episode 是否一致仍由 `_batch_manifests` 在合成之后完整校验,所以"文件在场但内容坏了"的情形仍会先合成再判 `failed` —— 判错的代价:与 `_batch_manifests` 的在场检查重复一次,报错文字与它逐字相同。
15. Ruling: `data_spec` 的错误信息里展示值一律经 `_show`(照 `training_config._short`:`repr` 失败退化成 `<类型名>`,过长截断),键也经它转成展示串再排序 —— 超过 4300 位的整数(YAML 的 `0xfff...` 写得出)做 `repr` 或 f-string 会抛 `ValueError`,穿过派发器的 `except InvalidDataSpecError` 逃出守护循环;已在临时拷贝里用这样一条指令复现过 —— 判错的代价:错误信息里看不到那个超长值本身。

## Global Constraints

每个任务的要求都隐含包含本节。

- **`src/` 下每个源文件以 `from __future__ import annotations` 开头**(模块 docstring 之后的第一条语句也算)。
- **不引入新第三方依赖**;`pyproject.toml` 不改。
- **凭证绝不进 git 树。** 本计划不涉及任何凭证字段。
- **读写产物走 `GitStore` + `Envelope`。** episode 与清单经 `simulator.store_adapter.write_episode` 与 `simulator.dataset.write_manifest` 写,不直接碰文件系统写产物。
- **`controlplane` 一行不改**;`mindtrainer` 不导入 `diagnostic`;`simulator` 不改。
- **既有测试一行不改**:`tests/test_mt_dispatch.py`、`tests/test_mt_wire.py` 必须原样全部通过。
- **默认行为不变**:`make_mindtrainer_dispatcher()` 不传 `data_producer`、或指令不带 `data_spec` 时,派发器的行为与改动前完全相同(缺清单判 `failed`)。
- **新参数 keyword-only**:`make_mindtrainer_dispatcher` 本就全是 keyword-only,`data_producer` 加在末尾,默认 `None`。
- **确定性失败判 `failed`(T2),瞬态异常原样上抛、指令保持 `pending`**,与读清单那段 `try` 的既有约定一致。
- **`git add` 逐个文件**,不要用 `git add .`;**不要碰** `cli/package.json`、`cli/package-lock.json`。
- **提交信息不加 `Co-Authored-By` 行**(用户明确要求)。
- **测试命令**一律用 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`(下文记作"测试命令")。**本计划只保证增量,不保证绝对数**:增量为任务 30 +70、任务 31 +17、任务 32 +25、任务 33 +33(共 **+145**),任务 34 +0。基线 B 由任务 30 第 1 步实测;第 N 个任务后的全量通过数为 B 加上该任务及之前各任务的增量之和,即 **B+70 → B+87 → B+112 → B+145 → B+145**。全程不得出现警告。(在 `main@0385004` 上验证时 B=820,对应 890 → 907 → 932 → 965。)

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `src/mindtrainer/data_spec.py` | 新建(任务 30 写 141 行,任务 31 扩到 215 行) | `DataSpec`、`StageSpec`、`parse_data_spec`、`data_spec_from_directive`、`InvalidDataSpecError`、`DataSynthesisError`;`required_stages`、`manifest_data_run`、`stages_to_synthesize`、`check_data_spec_against_config`;`DataProducer` 协议 |
| `src/mindtrainer/data_producer.py` | 新建(148 行) | `TemplateDataProducer`、`keyword_alternating_rollouts`、`RolloutFactory` |
| `src/mindtrainer/dispatch.py` | 修改(195 → 244 行;`git diff --numstat` 为 65 行新增、16 行删除) | `make_mindtrainer_dispatcher(..., data_producer=None)`、`_synthesize_missing` |
| `tests/test_mt_data_spec.py` | 新建 | 解析与逐字段类型、范围,含超长整数(70 个用例) |
| `tests/test_mt_data_spec_stages.py` | 新建 | 阶段判定与交叉校验(17 个用例) |
| `tests/test_mt_data_producer.py` | 新建 | 默认生产器,含一路通过 `resume_or_run`(25 个用例) |
| `tests/test_mt_dispatch_synthesis.py` | 新建 | 派发器的合成分支(33 个用例) |

**打包说明:** 无需改动(见"已核实的事实"最后一条)。

---

### Task 30: data_spec 的严格解析

**Files:**
- Create: `src/mindtrainer/data_spec.py`(141 行)
- Test: `tests/test_mt_data_spec.py`(70 个用例)

**Interfaces:**
- Consumes: `controlplane.envelope.Envelope`;`mindtrainer.training_config.STAGES`(`("sft", "grpo_opd")`)。
- Produces(任务 31–33 依赖这些名字):
  - `InvalidDataSpecError(ValueError)`:`data_spec` 缺字段、类型不对或越界。
  - `DataSynthesisError(ValueError)`:生产器确定性做不成(任务 32 抛,任务 33 接)。
  - `StageSpec(group_size: int)`、`DataSpec(scenario_count: int, seed: int, purpose: str, stages: dict[str, StageSpec])`,均 frozen;`DataSpec.to_dict()` 还原成载荷格式。
  - `parse_data_spec(raw) -> DataSpec`:只抛 `InvalidDataSpecError`。
  - `data_spec_from_directive(directive: Envelope) -> DataSpec | None`:没有 `data_spec` 键或值为 `null` 返回 `None`。
  - 常量 `PURPOSES`、`MAX_SCENARIO_COUNT = 64`、`MAX_GROUP_SIZE = 16`、`MAX_SEED = 2**31 - 1`。

**设计要点:**
- **类型先于范围,且绝不让别的异常类型漏出去。** 上一轮接线的教训:类型不对的配置若不在早期校验,`TypeError`/`AttributeError` 会逃出守护循环。整数字段排除 `bool`(`True` 是 `int` 的子类),也排除 `float`(包括 `4.0`、`nan`、`inf`)。
- **错误信息点名字段**,形如 `data_spec.scenario_count must be an int, got str`、`data_spec.stages['grpo_opd'].group_size must be in [2, 16], got 1`。任务 33 的测试按这些片段断言 `failure.reason`。
- **键可能是任意 YAML 标量**(整数、`null`;在 Python 里构造时还可能是元组)。报错时先把键经 `_show` 转成展示串再排序,免得 `sorted` 在混合类型上自己抛 `TypeError`。
- **超长整数**(超过 4300 位十进制,YAML 写 `0xfff...` 就能得到)做 `repr` 或 f-string 会抛 `ValueError`。所有展示值都经 `_show`,失败时退化成 `<int>`(Ruling 15)。测试把 `int("f" * 4000, 16)` 分别放在 `scenario_count`、`seed`、`group_size`、顶层键、阶段键五个位置。
- **阶段顺序规范化**为 `STAGES` 的顺序(`sft` 在前),与输入顺序无关。
- 严格程度见 Ruling 2、3。

- [ ] **Step 1: 记下基线,再写失败测试**

先在**动任何文件之前**记下两个值,后面的任务都要用:

```bash
BASE=$(git rev-parse HEAD); echo "BASE=$BASE"
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error | tail -1
```

Expected: 第一行打印 `BASE=<提交号>`;第二行形如 `B passed in ...s`,无警告。把 `BASE` 与通过数 `B` 记下来(写进执行记录,别只留在 shell 变量里,换终端会丢)。在 `main@0385004` 上 B=820;计划 1 已落地时 B 更大。

然后新建 `tests/test_mt_data_spec.py`:

```python
"""data_spec 的严格解析:每个字段都有类型与范围用例。"""
import pytest

from controlplane.envelope import Envelope, OwnerRef
from mindtrainer.data_spec import (
    MAX_GROUP_SIZE, MAX_SCENARIO_COUNT, MAX_SEED, DataSpec, InvalidDataSpecError, StageSpec,
    data_spec_from_directive, parse_data_spec,
)


def _raw(**kw):
    base = {
        "scenario_count": 4, "seed": 0, "purpose": "training",
        "stages": {"sft": {"group_size": 1}, "grpo_opd": {"group_size": 2}},
    }
    base.update(kw)
    return base


def _directive(payload):
    return Envelope(
        id="run-1", type="directive", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1, payload=payload,
    )


def test_the_design_example_parses():
    spec = parse_data_spec(_raw())
    assert spec == DataSpec(4, 0, "training", {"sft": StageSpec(1), "grpo_opd": StageSpec(2)})
    assert spec.to_dict() == _raw()


def test_stages_come_out_in_canonical_order_whatever_the_input_order():
    spec = parse_data_spec(_raw(stages={"grpo_opd": {"group_size": 2}, "sft": {"group_size": 1}}))
    assert list(spec.stages) == ["sft", "grpo_opd"]


def test_a_single_stage_is_enough():
    assert list(parse_data_spec(_raw(stages={"sft": {"group_size": 1}})).stages) == ["sft"]


def test_boundary_values_are_accepted():
    spec = parse_data_spec(_raw(
        scenario_count=MAX_SCENARIO_COUNT, seed=MAX_SEED, purpose="deployment_simulation",
        stages={"sft": {"group_size": MAX_GROUP_SIZE}, "grpo_opd": {"group_size": MAX_GROUP_SIZE}},
    ))
    assert (spec.scenario_count, spec.seed, spec.purpose) == (
        MAX_SCENARIO_COUNT, MAX_SEED, "deployment_simulation")


@pytest.mark.parametrize("raw", [None, [], "spec", 3, True], ids=lambda v: type(v).__name__)
def test_a_data_spec_that_is_not_a_dict_is_rejected(raw):
    with pytest.raises(InvalidDataSpecError, match="data_spec must be a dict"):
        parse_data_spec(raw)


@pytest.mark.parametrize("key", ["scenario_count", "seed", "purpose", "stages"])
def test_a_missing_key_is_rejected(key):
    raw = _raw()
    del raw[key]
    with pytest.raises(InvalidDataSpecError, match=f"missing key.*{key}"):
        parse_data_spec(raw)


def test_an_unknown_key_is_rejected():
    with pytest.raises(InvalidDataSpecError, match="unknown key.*dimension"):
        parse_data_spec(_raw(dimension="tom"))


def test_a_non_string_key_does_not_break_the_error_message():
    raw = _raw()
    raw[1] = "x"
    with pytest.raises(InvalidDataSpecError, match="unknown key"):
        parse_data_spec(raw)


@pytest.mark.parametrize("field", ["scenario_count", "seed"])
@pytest.mark.parametrize("value", ["4", 4.0, None, True, [4], {"n": 4}],
                         ids=["str", "float", "none", "bool", "list", "dict"])
def test_integer_fields_reject_every_other_type(field, value):
    with pytest.raises(InvalidDataSpecError, match=f"data_spec.{field} must be an int"):
        parse_data_spec(_raw(**{field: value}))


@pytest.mark.parametrize("value", [0, -1, MAX_SCENARIO_COUNT + 1, 10**30])
def test_scenario_count_out_of_range_is_rejected(value):
    with pytest.raises(InvalidDataSpecError, match=r"data_spec.scenario_count must be in \[1, "):
        parse_data_spec(_raw(scenario_count=value))


@pytest.mark.parametrize("value", [-1, MAX_SEED + 1])
def test_seed_out_of_range_is_rejected(value):
    with pytest.raises(InvalidDataSpecError, match=r"data_spec.seed must be in \[0, "):
        parse_data_spec(_raw(seed=value))


@pytest.mark.parametrize("value", [1, None, ["training"], True], ids=["int", "none", "list", "bool"])
def test_purpose_must_be_a_string(value):
    with pytest.raises(InvalidDataSpecError, match="data_spec.purpose must be a str"):
        parse_data_spec(_raw(purpose=value))


@pytest.mark.parametrize("value", ["", "Training", "eval"])
def test_purpose_must_be_a_known_value(value):
    with pytest.raises(InvalidDataSpecError, match="data_spec.purpose must be one of"):
        parse_data_spec(_raw(purpose=value))


@pytest.mark.parametrize("value", [[], "sft", None, 1], ids=["list", "str", "none", "int"])
def test_stages_must_be_a_dict(value):
    with pytest.raises(InvalidDataSpecError, match="data_spec.stages must be a dict"):
        parse_data_spec(_raw(stages=value))


def test_stages_must_not_be_empty():
    with pytest.raises(InvalidDataSpecError, match="at least one stage"):
        parse_data_spec(_raw(stages={}))


def test_an_unknown_stage_is_rejected():
    with pytest.raises(InvalidDataSpecError, match="unknown stage.*'rl'"):
        parse_data_spec(_raw(stages={"sft": {"group_size": 1}, "rl": {"group_size": 2}}))


@pytest.mark.parametrize("value", [2, None, [2], "2"], ids=["int", "none", "list", "str"])
def test_a_stage_entry_must_be_a_dict(value):
    with pytest.raises(InvalidDataSpecError, match=r"data_spec.stages\['sft'\] must be a dict"):
        parse_data_spec(_raw(stages={"sft": value}))


def test_a_stage_entry_needs_exactly_group_size():
    with pytest.raises(InvalidDataSpecError, match="missing key.*group_size"):
        parse_data_spec(_raw(stages={"sft": {}}))
    with pytest.raises(InvalidDataSpecError, match="unknown key.*count"):
        parse_data_spec(_raw(stages={"sft": {"group_size": 1, "count": 3}}))


@pytest.mark.parametrize("value", ["1", 1.0, None, True, [1], {"n": 1}],
                         ids=["str", "float", "none", "bool", "list", "dict"])
def test_group_size_must_be_an_int(value):
    with pytest.raises(InvalidDataSpecError, match=r"group_size must be an int"):
        parse_data_spec(_raw(stages={"sft": {"group_size": value}}))


@pytest.mark.parametrize(
    "stage, value",
    [("sft", 0), ("sft", MAX_GROUP_SIZE + 1), ("grpo_opd", 1), ("grpo_opd", 0),
     ("grpo_opd", MAX_GROUP_SIZE + 1)],
)
def test_group_size_out_of_range_is_rejected(stage, value):
    with pytest.raises(InvalidDataSpecError, match=rf"data_spec.stages\['{stage}'\].group_size must be in"):
        parse_data_spec(_raw(stages={stage: {"group_size": value}}))


def test_a_directive_without_data_spec_has_none():
    assert data_spec_from_directive(_directive({"target_dimension": "tom"})) is None
    assert data_spec_from_directive(_directive({"data_spec": None})) is None


def test_a_directive_with_data_spec_is_parsed_strictly():
    assert data_spec_from_directive(_directive({"data_spec": _raw()})).scenario_count == 4
    with pytest.raises(InvalidDataSpecError):
        data_spec_from_directive(_directive({"data_spec": _raw(seed="0")}))


HUGE = int("f" * 4000, 16)  # 4817 位十进制,超过 int 转字符串的 4300 位上限;YAML 写 0xfff... 就能得到


@pytest.mark.parametrize(
    "raw, field",
    [
        (_raw(scenario_count=HUGE), "data_spec.scenario_count must be in"),
        (_raw(seed=HUGE), "data_spec.seed must be in"),
        (_raw(stages={"sft": {"group_size": HUGE}}), "group_size must be in"),
        ({**_raw(), HUGE: 1}, "unknown key"),
        (_raw(stages={"sft": {"group_size": 1}, HUGE: {"group_size": 2}}), "unknown stage"),
    ],
    ids=["scenario_count", "seed", "group_size", "top_level_key", "stage_key"],
)
def test_an_integer_too_long_to_print_is_still_an_invalid_data_spec_error(raw, field):
    with pytest.raises(InvalidDataSpecError, match=field) as info:
        parse_data_spec(raw)
    assert "<int>" in str(info.value)  # 值只写类型,不尝试转字符串


def test_parsing_never_raises_anything_but_invalid_data_spec_error():
    """守护循环的教训:错类型的输入若在这里漏成 TypeError/AttributeError,就会逃出派发器。"""
    weird = [
        _raw(stages={"sft": {"group_size": float("nan")}}),
        _raw(scenario_count=float("inf")),
        _raw(stages={("sft",): {"group_size": 1}}),
        _raw(stages={None: {"group_size": 1}}),
        {1: 2, "a": 3},
    ]
    for raw in weird:
        with pytest.raises(InvalidDataSpecError):
            parse_data_spec(raw)
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_mt_data_spec.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindtrainer.data_spec'`,末行 `1 error`。

- [ ] **Step 3: 写实现**

新建 `src/mindtrainer/data_spec.py`:

```python
"""指令里的 data_spec:严格解析、与训练配置的交叉校验、数据生产器协议(设计 §9,R14)。

data_spec 由调度器写进 directive 的 payload,形如:

    {scenario_count: 4, seed: 0, purpose: training,
     stages: {sft: {group_size: 1}, grpo_opd: {group_size: 2}}}

解析在派发器里、碰树之前就跑:字符串当整数、布尔当整数、列表当字典之类的输入在这里
一律变成点名字段的 InvalidDataSpecError,不让 TypeError 或 AttributeError 一路滑进生产器、
再逃出守护循环。
"""
from __future__ import annotations

from dataclasses import dataclass

from controlplane.envelope import Envelope
from mindtrainer.training_config import STAGES

PURPOSES = ("training", "deployment_simulation")
MAX_SCENARIO_COUNT = 64
MAX_GROUP_SIZE = 16
MAX_SEED = 2**31 - 1
_KEYS = frozenset({"scenario_count", "seed", "purpose", "stages"})
_STAGE_KEYS = frozenset({"group_size"})


class InvalidDataSpecError(ValueError):
    """data_spec 缺字段、类型不对或越界。确定性失败:同一份 data_spec 重试还会失败。"""


class DataSynthesisError(ValueError):
    """数据生产器确定性地做不成(例如训练角色位与对手角色位同名)。派发器把它判成 T2。"""


@dataclass(frozen=True)
class StageSpec:
    group_size: int


@dataclass(frozen=True)
class DataSpec:
    scenario_count: int
    seed: int
    purpose: str
    stages: dict[str, StageSpec]

    def to_dict(self) -> dict:
        return {
            "scenario_count": self.scenario_count,
            "seed": self.seed,
            "purpose": self.purpose,
            "stages": {name: {"group_size": s.group_size} for name, s in self.stages.items()},
        }


def _show(value) -> str:
    """错误信息里展示的值(照 training_config._short 的做法):太长就截断;转字符串本身会抛时只写类型。
    超过 4300 位的整数(YAML 的十六进制能写出来)做 repr 或 f-string 都会抛 ValueError,
    若让它从拼错误信息的地方漏出去,就绕过了 InvalidDataSpecError,一路逃出守护循环。"""
    try:
        text = repr(value)
    except Exception:  # 超长整数的 ValueError,或自定义对象的 __repr__ 抛的任何异常
        return f"<{type(value).__name__}>"
    return text if len(text) <= 48 else text[:45] + "..."


def _keys_text(keys) -> str:
    # 键可能是互不可比较的类型(int 和 str 混在一起),也可能是超长整数:排序前先转成安全的展示串
    return str(sorted(_show(k) for k in keys))


def _int_in_range(value, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidDataSpecError(f"{label} must be an int, got {type(value).__name__}")
    if not (low <= value <= high):
        raise InvalidDataSpecError(f"{label} must be in [{low}, {high}], got {_show(value)}")
    return value


def _exact_keys(raw: dict, allowed: frozenset, label: str) -> None:
    missing = allowed - set(raw)
    if missing:
        raise InvalidDataSpecError(f"{label} is missing key(s) {_keys_text(missing)}")
    unknown = set(raw) - allowed
    if unknown:
        raise InvalidDataSpecError(
            f"{label} has unknown key(s) {_keys_text(unknown)}; allowed {sorted(allowed)}"
        )


def _parse_stage(name: str, raw) -> StageSpec:
    label = f"data_spec.stages[{name!r}]"
    if not isinstance(raw, dict):
        raise InvalidDataSpecError(f"{label} must be a dict, got {type(raw).__name__}")
    _exact_keys(raw, _STAGE_KEYS, label)
    # grpo_opd 的组少于 2 条时组内零方差、优势恒为 0;sft 每组一条是正常的
    low = 2 if name == "grpo_opd" else 1
    return StageSpec(_int_in_range(raw["group_size"], f"{label}.group_size", low, MAX_GROUP_SIZE))


def parse_data_spec(raw) -> DataSpec:
    """严格解析。任何类型错、范围错、缺键、多键都抛 InvalidDataSpecError(信息里点名字段),
    绝不抛别的异常类型。"""
    if not isinstance(raw, dict):
        raise InvalidDataSpecError(f"data_spec must be a dict, got {type(raw).__name__}")
    _exact_keys(raw, _KEYS, "data_spec")
    scenario_count = _int_in_range(
        raw["scenario_count"], "data_spec.scenario_count", 1, MAX_SCENARIO_COUNT
    )
    seed = _int_in_range(raw["seed"], "data_spec.seed", 0, MAX_SEED)
    purpose = raw["purpose"]
    if not isinstance(purpose, str):
        raise InvalidDataSpecError(
            f"data_spec.purpose must be a str, got {type(purpose).__name__}"
        )
    if purpose not in PURPOSES:
        raise InvalidDataSpecError(
            f"data_spec.purpose must be one of {list(PURPOSES)}, got {purpose!r}"
        )
    stages_raw = raw["stages"]
    if not isinstance(stages_raw, dict):
        raise InvalidDataSpecError(
            f"data_spec.stages must be a dict, got {type(stages_raw).__name__}"
        )
    if not stages_raw:
        raise InvalidDataSpecError("data_spec.stages must name at least one stage")
    unknown = set(stages_raw) - set(STAGES)
    if unknown:
        raise InvalidDataSpecError(
            f"data_spec.stages has unknown stage(s) {_keys_text(unknown)}; allowed {list(STAGES)}"
        )
    stages = {name: _parse_stage(name, stages_raw[name]) for name in STAGES if name in stages_raw}
    return DataSpec(scenario_count=scenario_count, seed=seed, purpose=purpose, stages=stages)


def data_spec_from_directive(directive: Envelope) -> DataSpec | None:
    """payload 里没有 data_spec 键,或值为 null,都表示"这条指令不合成数据"。"""
    raw = directive.payload.get("data_spec")
    if raw is None:
        return None
    return parse_data_spec(raw)
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_mt_data_spec.py`
Expected: `70 passed`。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+70 passed`(B 是任务 30 第 1 步记下的基线),无警告。再跑 `git status --short`:除原本就有的 `cli/package*.json` 与未跟踪的设计文档外,只应多出 `src/mindtrainer/data_spec.py` 和 `tests/test_mt_data_spec.py`。

- [ ] **Step 6: 提交**

```bash
git add src/mindtrainer/data_spec.py
git add tests/test_mt_data_spec.py
git commit -m "feat: strict data_spec parsing for directives (every field type- and range-checked, one error type)"
```

---

### Task 31: 要合成哪些阶段、data_spec 与训练配置的交叉校验、DataProducer 协议

**Files:**
- Modify: `src/mindtrainer/data_spec.py`(141 → 215 行)
- Test: `tests/test_mt_data_spec_stages.py`(17 个用例)

**Interfaces:**
- Consumes: `GitStore.exists`;`controlplane.paths.dataset_manifest_file`;`mindtrainer.training_config.TrainingConfig`;任务 30 的 `DataSpec`。
- Produces(任务 32、33 依赖):
  - `required_stages(cfg) -> list[str]`:与现在 `dispatch._required_stages` 逐字相同的逻辑(任务 33 把派发器改成用它)。
  - `manifest_data_run(ref: str, stage: str) -> str | None`:从 `dsm-<run>-<阶段>` 解出数据所在的 run,格式不对返回 `None`。
  - `stages_to_synthesize(store, run_id, cfg) -> list[str]`:会用到、点了清单、数据所在 run 就是 `run_id`、清单文件不在树里,四者同时成立的阶段,按 `required_stages` 的顺序。
  - `check_data_spec_against_config(spec, cfg, stages) -> str | None`:见 Ruling 5。
  - `DataProducer` 协议:`produce(store, run_id, cfg, data_spec) -> None`。

**设计要点:**
- **清单 id 格式不对的阶段不在这里判。** `stages_to_synthesize` 直接跳过它,让派发器的 `_data_run_id` 照旧判 `failed`(原因里点名 `stage_manifest_refs[...]`),行为与现在一致。
- **数据所在 run 不是本 run 的清单永远不合成。** 那是复用前一轮的数据集(`clone`/`replay`),缺了就是缺了,照旧判 `failed`;替别的 run 合成会改写另一轮已锚定的数据。
- **`DataProducer` 的契约写在协议的 docstring 里**:写出 `stages_to_synthesize` 列出的阶段;episode 先写、清单最后写;确定性错误抛 `DataSynthesisError`;瞬态错误原样上抛。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_mt_data_spec_stages.py`:

```python
"""data_spec 与训练配置的交叉校验、要合成哪些阶段。"""
import pytest

from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import dataset_manifest_file
from mindtrainer.data_spec import (
    check_data_spec_against_config, manifest_data_run, parse_data_spec, required_stages,
    stages_to_synthesize,
)
from mindtrainer.training_config import RewardConfig, TrainingConfig
from simulator.datamodel import RoleBinding

RUN = "run-1"
BASE = BigRef("file:///ckpt/base", "a" * 64, 1)
TEACHER = BigRef("file:///ckpt/teacher", "b" * 64, 1)
REWARD = RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]})


def _cfg(**kw):
    base = dict(
        mode="batch", trained_slot="a", base_checkpoint_ref=BASE, teacher_model_ref=TEACHER,
        reward=REWARD, sft_steps=1, grpo_steps=1, sft_batch_size=1, groups_per_step=1,
        group_size=2, grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=1,
        allow_resume=True,
        stage_manifest_refs={"sft": f"dsm-{RUN}-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"},
    )
    base.update(kw)
    return TrainingConfig(**base)


def _interact(**kw):
    base = dict(mode="interact", scenario_ids=["s1"], max_turns_per_episode=4,
                bindings=[RoleBinding("a", "pa", "self_play"),
                          RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")],
                stage_manifest_refs={"sft": f"dsm-{RUN}-sft"})
    base.update(kw)
    return _cfg(**base)


def _spec(**kw):
    raw = {"scenario_count": 2, "seed": 0, "purpose": "training",
           "stages": {"sft": {"group_size": 1}, "grpo_opd": {"group_size": 2}}}
    raw.update(kw)
    return parse_data_spec(raw)


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _put_manifest_file(store, run_id, stage):
    env = Envelope(id=f"dsm-{run_id}-{stage}", type="dataset_manifest", version=1,
                   created_at="2026-10-09T00:00:00Z", created_by=OwnerRef(), status="executed",
                   schema_version=1, payload={})
    store.write_product(dataset_manifest_file(run_id, stage), env, "seed manifest")


def test_required_stages_follow_the_steps_and_the_mode():
    assert required_stages(_cfg()) == ["sft", "grpo_opd"]
    assert required_stages(_cfg(sft_steps=0)) == ["grpo_opd"]
    assert required_stages(_cfg(grpo_steps=0)) == ["sft"]
    assert required_stages(_interact(grpo_steps=2)) == ["sft"]  # interact 的 grpo_opd 来自实时会话


@pytest.mark.parametrize(
    "ref, stage, expected",
    [("dsm-run-1-sft", "sft", "run-1"), ("dsm-a-b-c-grpo_opd", "grpo_opd", "a-b-c"),
     ("dsm--sft", "sft", None), ("dsm-run-1-sft", "grpo_opd", None), ("nonsense", "sft", None)],
)
def test_manifest_data_run_parses_the_build_manifest_id_format(ref, stage, expected):
    assert manifest_data_run(ref, stage) == expected


def test_every_missing_stage_of_this_run_is_synthesized(store):
    assert stages_to_synthesize(store, RUN, _cfg()) == ["sft", "grpo_opd"]


def test_a_stage_whose_manifest_is_already_in_the_tree_is_not_synthesized_again(store):
    _put_manifest_file(store, RUN, "sft")
    assert stages_to_synthesize(store, RUN, _cfg()) == ["grpo_opd"]
    _put_manifest_file(store, RUN, "grpo_opd")
    assert stages_to_synthesize(store, RUN, _cfg()) == []


def test_a_manifest_whose_data_lives_under_another_run_is_never_synthesized(store):
    cfg = _cfg(stage_manifest_refs={"sft": "dsm-run-0-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"})
    assert stages_to_synthesize(store, RUN, cfg) == ["grpo_opd"]


def test_a_malformed_manifest_id_is_left_to_the_dispatcher(store):
    cfg = _cfg(stage_manifest_refs={"sft": "nonsense", "grpo_opd": f"dsm-{RUN}-grpo_opd"})
    assert stages_to_synthesize(store, RUN, cfg) == ["grpo_opd"]


def test_a_stage_without_steps_is_not_synthesized_even_if_it_is_named(store):
    assert stages_to_synthesize(store, RUN, _cfg(grpo_steps=0)) == ["sft"]


def test_interact_only_ever_synthesizes_sft(store):
    cfg = _interact(grpo_steps=2, stage_manifest_refs={"sft": f"dsm-{RUN}-sft",
                                                       "grpo_opd": f"dsm-{RUN}-grpo_opd"})
    assert stages_to_synthesize(store, RUN, cfg) == ["sft"]


def test_a_spec_that_fits_the_config_passes():
    assert check_data_spec_against_config(_spec(), _cfg(), ["sft", "grpo_opd"]) is None


def test_a_spec_must_cover_every_stage_to_synthesize():
    spec = _spec(stages={"sft": {"group_size": 1}})
    reason = check_data_spec_against_config(spec, _cfg(), ["sft", "grpo_opd"])
    assert "no entry for stage 'grpo_opd'" in reason
    assert check_data_spec_against_config(spec, _cfg(), ["sft"]) is None


def test_extra_stages_in_the_spec_are_ignored():
    assert check_data_spec_against_config(_spec(), _interact(), ["sft"]) is None


def test_grpo_groups_must_reach_min_group_size():
    cfg = _cfg(min_group_size=3, group_size=3)
    reason = check_data_spec_against_config(_spec(), cfg, ["grpo_opd"])
    assert "group_size 2 is below min_group_size 3" in reason


def test_there_must_be_a_scenario_per_group_in_a_step():
    reason = check_data_spec_against_config(_spec(scenario_count=1), _cfg(groups_per_step=2), ["grpo_opd"])
    assert "scenario_count 1 is below groups_per_step 2" in reason
    assert check_data_spec_against_config(_spec(scenario_count=1), _cfg(groups_per_step=2), ["sft"]) is None
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_mt_data_spec_stages.py`
Expected: 收集阶段报错 `ImportError: cannot import name 'check_data_spec_against_config' from 'mindtrainer.data_spec'`,末行 `1 error`。

- [ ] **Step 3: 写实现**

用下面的完整内容覆盖 `src/mindtrainer/data_spec.py`。与任务 30 相比只有两处:import 段多了 `typing.Protocol`、`GitStore`、`dataset_manifest_file`、`TrainingConfig`;文件末尾从 `def required_stages` 起是新增内容。

```python
"""指令里的 data_spec:严格解析、与训练配置的交叉校验、数据生产器协议(设计 §9,R14)。

data_spec 由调度器写进 directive 的 payload,形如:

    {scenario_count: 4, seed: 0, purpose: training,
     stages: {sft: {group_size: 1}, grpo_opd: {group_size: 2}}}

解析在派发器里、碰树之前就跑:字符串当整数、布尔当整数、列表当字典之类的输入在这里
一律变成点名字段的 InvalidDataSpecError,不让 TypeError 或 AttributeError 一路滑进生产器、
再逃出守护循环。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from controlplane.envelope import Envelope
from controlplane.gitstore import GitStore
from controlplane.paths import dataset_manifest_file
from mindtrainer.training_config import STAGES, TrainingConfig

PURPOSES = ("training", "deployment_simulation")
MAX_SCENARIO_COUNT = 64
MAX_GROUP_SIZE = 16
MAX_SEED = 2**31 - 1
_KEYS = frozenset({"scenario_count", "seed", "purpose", "stages"})
_STAGE_KEYS = frozenset({"group_size"})


class InvalidDataSpecError(ValueError):
    """data_spec 缺字段、类型不对或越界。确定性失败:同一份 data_spec 重试还会失败。"""


class DataSynthesisError(ValueError):
    """数据生产器确定性地做不成(例如训练角色位与对手角色位同名)。派发器把它判成 T2。"""


@dataclass(frozen=True)
class StageSpec:
    group_size: int


@dataclass(frozen=True)
class DataSpec:
    scenario_count: int
    seed: int
    purpose: str
    stages: dict[str, StageSpec]

    def to_dict(self) -> dict:
        return {
            "scenario_count": self.scenario_count,
            "seed": self.seed,
            "purpose": self.purpose,
            "stages": {name: {"group_size": s.group_size} for name, s in self.stages.items()},
        }


def _show(value) -> str:
    """错误信息里展示的值(照 training_config._short 的做法):太长就截断;转字符串本身会抛时只写类型。
    超过 4300 位的整数(YAML 的十六进制能写出来)做 repr 或 f-string 都会抛 ValueError,
    若让它从拼错误信息的地方漏出去,就绕过了 InvalidDataSpecError,一路逃出守护循环。"""
    try:
        text = repr(value)
    except Exception:  # 超长整数的 ValueError,或自定义对象的 __repr__ 抛的任何异常
        return f"<{type(value).__name__}>"
    return text if len(text) <= 48 else text[:45] + "..."


def _keys_text(keys) -> str:
    # 键可能是互不可比较的类型(int 和 str 混在一起),也可能是超长整数:排序前先转成安全的展示串
    return str(sorted(_show(k) for k in keys))


def _int_in_range(value, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidDataSpecError(f"{label} must be an int, got {type(value).__name__}")
    if not (low <= value <= high):
        raise InvalidDataSpecError(f"{label} must be in [{low}, {high}], got {_show(value)}")
    return value


def _exact_keys(raw: dict, allowed: frozenset, label: str) -> None:
    missing = allowed - set(raw)
    if missing:
        raise InvalidDataSpecError(f"{label} is missing key(s) {_keys_text(missing)}")
    unknown = set(raw) - allowed
    if unknown:
        raise InvalidDataSpecError(
            f"{label} has unknown key(s) {_keys_text(unknown)}; allowed {sorted(allowed)}"
        )


def _parse_stage(name: str, raw) -> StageSpec:
    label = f"data_spec.stages[{name!r}]"
    if not isinstance(raw, dict):
        raise InvalidDataSpecError(f"{label} must be a dict, got {type(raw).__name__}")
    _exact_keys(raw, _STAGE_KEYS, label)
    # grpo_opd 的组少于 2 条时组内零方差、优势恒为 0;sft 每组一条是正常的
    low = 2 if name == "grpo_opd" else 1
    return StageSpec(_int_in_range(raw["group_size"], f"{label}.group_size", low, MAX_GROUP_SIZE))


def parse_data_spec(raw) -> DataSpec:
    """严格解析。任何类型错、范围错、缺键、多键都抛 InvalidDataSpecError(信息里点名字段),
    绝不抛别的异常类型。"""
    if not isinstance(raw, dict):
        raise InvalidDataSpecError(f"data_spec must be a dict, got {type(raw).__name__}")
    _exact_keys(raw, _KEYS, "data_spec")
    scenario_count = _int_in_range(
        raw["scenario_count"], "data_spec.scenario_count", 1, MAX_SCENARIO_COUNT
    )
    seed = _int_in_range(raw["seed"], "data_spec.seed", 0, MAX_SEED)
    purpose = raw["purpose"]
    if not isinstance(purpose, str):
        raise InvalidDataSpecError(
            f"data_spec.purpose must be a str, got {type(purpose).__name__}"
        )
    if purpose not in PURPOSES:
        raise InvalidDataSpecError(
            f"data_spec.purpose must be one of {list(PURPOSES)}, got {purpose!r}"
        )
    stages_raw = raw["stages"]
    if not isinstance(stages_raw, dict):
        raise InvalidDataSpecError(
            f"data_spec.stages must be a dict, got {type(stages_raw).__name__}"
        )
    if not stages_raw:
        raise InvalidDataSpecError("data_spec.stages must name at least one stage")
    unknown = set(stages_raw) - set(STAGES)
    if unknown:
        raise InvalidDataSpecError(
            f"data_spec.stages has unknown stage(s) {_keys_text(unknown)}; allowed {list(STAGES)}"
        )
    stages = {name: _parse_stage(name, stages_raw[name]) for name in STAGES if name in stages_raw}
    return DataSpec(scenario_count=scenario_count, seed=seed, purpose=purpose, stages=stages)


def data_spec_from_directive(directive: Envelope) -> DataSpec | None:
    """payload 里没有 data_spec 键,或值为 null,都表示"这条指令不合成数据"。"""
    raw = directive.payload.get("data_spec")
    if raw is None:
        return None
    return parse_data_spec(raw)


def required_stages(cfg: TrainingConfig) -> list[str]:
    """真正会用到的阶段:配了步数的才算,且与模式有关。stage_manifest_refs 里多写的阶段不读。

    sft 两种模式都从清单读,配了 sft_steps 就要。grpo_opd 只有 batch 模式从清单读;
    interact 模式的 grpo_opd 来自实时会话(_interact_sources 事后整段覆盖),不读清单,
    所以即便指令在 stage_manifest_refs 里为 interact 顺手点了一个 grpo_opd 的清单 id
    (validate_training_config 并不禁止),这里也不该把它当成需要校验的阶段。"""
    wanted = []
    if cfg.sft_steps > 0:
        wanted.append("sft")
    if cfg.mode == "batch" and cfg.grpo_steps > 0:
        wanted.append("grpo_opd")
    return wanted


def manifest_data_run(ref: str, stage: str) -> str | None:
    """清单 id 的格式是 dsm-{数据所在的 run}-{阶段}(build_manifest 是唯一的构造点)。
    解出数据所在的 run;格式不对返回 None。"""
    head, tail = "dsm-", f"-{stage}"
    if not (ref.startswith(head) and ref.endswith(tail) and len(ref) > len(head) + len(tail)):
        return None
    return ref[len(head):-len(tail)]


def stages_to_synthesize(store: GitStore, run_id: str, cfg: TrainingConfig) -> list[str]:
    """需要合成的阶段:会用到、点了清单、清单的数据所在 run 就是本 run、且清单文件还不在树里。
    清单已在的阶段不重新合成;id 格式不对的阶段不在这里判(派发器会判 failed)。"""
    out = []
    for name in required_stages(cfg):
        ref = cfg.stage_manifest_refs.get(name)
        if not ref or manifest_data_run(ref, name) != run_id:
            continue
        if not store.exists(dataset_manifest_file(run_id, name)):
            out.append(name)
    return out


def check_data_spec_against_config(
    spec: DataSpec, cfg: TrainingConfig, stages: list[str]
) -> str | None:
    """data_spec 能否为这些阶段产出训练用得上的数据。返回第一条原因,合格返回 None。
    这些问题训练时也会以 DegenerateGroupError、NotEnoughGroupsError 暴露,在这里先判,
    是为了一条 episode 都不写就失败,并且原因直接指向 data_spec。"""
    for name in stages:
        if name not in spec.stages:
            return f"data_spec.stages has no entry for stage {name!r}, which must be synthesized"
    if "grpo_opd" in stages:
        size = spec.stages["grpo_opd"].group_size
        if size < cfg.min_group_size:
            return (
                f"data_spec.stages['grpo_opd'].group_size {size} is below "
                f"min_group_size {cfg.min_group_size}"
            )
        if spec.scenario_count < cfg.groups_per_step:
            return (
                f"data_spec.scenario_count {spec.scenario_count} is below "
                f"groups_per_step {cfg.groups_per_step} (one group per scenario)"
            )
    return None


class DataProducer(Protocol):
    def produce(
        self, store: GitStore, run_id: str, cfg: TrainingConfig, data_spec: DataSpec
    ) -> None:
        """写出 stages_to_synthesize(store, run_id, cfg) 列出的各阶段数据集:episode 先写、
        清单最后写,清单 id 为 dsm-{run_id}-{阶段}。确定性做不成抛 DataSynthesisError
        (或 DatasetIntegrityError 等派发器认得的确定性错误);瞬态错误原样上抛。"""
        ...
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_mt_data_spec_stages.py tests/test_mt_data_spec.py`
Expected: `87 passed`。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+87 passed`,无警告。

- [ ] **Step 6: 提交**

```bash
git add src/mindtrainer/data_spec.py
git add tests/test_mt_data_spec_stages.py
git commit -m "feat: decide which stages to synthesize and cross-check data_spec against the training config"
```

---

### Task 32: 默认数据生产器与确定性 rollout 后端

**Files:**
- Create: `src/mindtrainer/data_producer.py`(148 行)
- Test: `tests/test_mt_data_producer.py`(25 个用例)

**Interfaces:**
- Consumes: `simulator.generators.generate_scenario` / `generate_persona`;`simulator.backends.TemplateGenerationBackend` / `ScriptedBackend`;`simulator.dataset.produce_grouped_dataset`;任务 31 的 `stages_to_synthesize`、`DataSpec`、`DataSynthesisError`。
- Produces(任务 33 与计划 9 的组合根依赖):
  - `RolloutFactory = Callable[[TrainingConfig, list[str], int], Callable[[Scenario, int], dict[str, ModelBackend]]]`:`(cfg, 角色位顺序, max_turns)` → `produce_grouped_dataset` 的 `backends_for_rollout`。以后换真实 rollout 后端,就是换这个工厂。
  - `keyword_alternating_rollouts`:默认工厂(Ruling 9)。
  - `TemplateDataProducer(*, rollout_factory=keyword_alternating_rollouts, generation_backend=None, max_turns=4, opponent_model_ref="template-opponent-v1")`:满足 `DataProducer` 协议。

**设计要点:**
- **组内必须真有差异。** 偶数序号 rollout 的训练位每句含每个分量的第一个关键词,奇数序号说中性话,所以每组奖励是"高、0"交替。测试同时覆盖 sum 与 product、多分量、`group_size=3`;并用 `resume_or_run` 跑完整训练,断言 `degenerate_groups_total == 0`、`final_mean_reward == 0.5`。
- **中性话要逐句核对。** 关键词是纯子串,"嗯"这类关键词会让中性候选"嗯。"命中;候选全被命中时抛 `DataSynthesisError`,不静默产出零方差数据。
- **确定性。** 同一份 `(run_id, cfg, data_spec)` 两次产出逐字节相同,这是任务 33 崩溃自愈的前提(重跑覆盖成同样内容)。
- **只合成缺的阶段。** `produce` 自己再调一次 `stages_to_synthesize`,所以直接调用(不经派发器)也不会覆盖已有清单。
- **绑定不合法是确定性错误。** interact 的 `bindings` 没绑训练位、没有对手、或同一角色位绑了两次,抛 `DataSynthesisError`,一条 episode 都不写。
- **interact 的人格被换成生成的人格。** 生产器照抄 `bindings` 的角色位、`model_backend` 与 `model_ref`,但 `persona_id` 换成模板生成的 `pers-auto-<n>`,不用 `bindings` 里点名的人格;合成的 sft episode 因此与实时会话里的人格不同(见已知限制 13)。
- 角色位、种子、轮数、不写情境与人格目录的取舍见 Ruling 10–13。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_mt_data_producer.py`:

```python
"""默认数据生产器:模板情境与人格 + 按序号交替关键词的 rollout,产出的数据要一路训得通。"""
import pytest

from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from controlplane.paths import dataset_manifest_file, episodes_dir
from mindtrainer.checkpoint import MockConverter
from mindtrainer.data_producer import TemplateDataProducer, keyword_alternating_rollouts
from mindtrainer.data_spec import DataSynthesisError, parse_data_spec
from mindtrainer.executor import MockExecutor
from mindtrainer.grouping import count_degenerate_groups, group_by_id
from mindtrainer.orchestrator import resume_or_run
from mindtrainer.reward_pipeline import build_attribution_backend, run_reward_pipeline
from mindtrainer.training_config import RewardConfig, TrainingConfig
from mindtrainer.trajectory_source import BatchTrajectorySource
from simulator.backends import ScriptedBackend
from simulator.datamodel import RoleBinding
from simulator.dataset import read_manifest, verify_manifest

RUN = "run-1"
BASE = BigRef("file:///ckpt/base", "a" * 64, 1)
TEACHER = BigRef("file:///ckpt/teacher", "b" * 64, 1)
REWARD = RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]})


def _cfg(**kw):
    base = dict(
        mode="batch", trained_slot="a", base_checkpoint_ref=BASE, teacher_model_ref=TEACHER,
        reward=REWARD, sft_steps=2, grpo_steps=3, sft_batch_size=1, groups_per_step=2,
        group_size=2, grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=2,
        allow_resume=True,
        stage_manifest_refs={"sft": f"dsm-{RUN}-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"},
    )
    base.update(kw)
    return TrainingConfig(**base)


def _spec(**kw):
    raw = {"scenario_count": 3, "seed": 0, "purpose": "training",
           "stages": {"sft": {"group_size": 1}, "grpo_opd": {"group_size": 2}}}
    raw.update(kw)
    return parse_data_spec(raw)


def _store(tmp_path, name="repo"):
    s = GitStore(str(tmp_path / name))
    s.init()
    return s


@pytest.fixture
def store(tmp_path):
    return _store(tmp_path)


def _snapshot(store):
    files = store.list_products(episodes_dir(RUN)) + [
        dataset_manifest_file(RUN, n) for n in ("sft", "grpo_opd")
        if store.exists(dataset_manifest_file(RUN, n))
    ]
    return {rel: store.read_product(rel).to_dict() for rel in files}


def _rewarded(store, cfg):
    batch = BatchTrajectorySource(store, read_manifest(store, RUN, "grpo_opd")).collect()
    return run_reward_pipeline(batch, cfg.reward, build_attribution_backend(cfg.reward),
                               trained_slot=cfg.trained_slot)


def test_it_writes_one_manifest_per_stage_under_this_run(store):
    TemplateDataProducer().produce(store, RUN, _cfg(), _spec())
    sft, grpo = read_manifest(store, RUN, "sft"), read_manifest(store, RUN, "grpo_opd")
    assert (sft.id, grpo.id) == (f"dsm-{RUN}-sft", f"dsm-{RUN}-grpo_opd")
    assert (len(sft.episode_refs), len(grpo.episode_refs)) == (3, 6)  # 3 个情境 x 组大小
    assert len(verify_manifest(store, grpo)) == 6


def test_every_grpo_group_has_reward_variance(store):
    cfg = _cfg()
    TemplateDataProducer().produce(store, RUN, cfg, _spec())
    groups = group_by_id(_rewarded(store, cfg))
    assert len(groups) == 3
    by_group = {gid: sorted(r.combined_reward for r in members) for gid, members in groups.items()}
    assert all(rewards == [0.0, 1.0] for rewards in by_group.values())
    assert count_degenerate_groups(by_group) == 0


@pytest.mark.parametrize("combination", ["sum", "product"])
def test_variance_holds_with_several_components_and_either_combination(store, combination):
    reward = RewardConfig(["goal", "care"], "outcome_split", combination,
                          {"goal": ["agreed", "deal"], "care": ["sorry"]})
    cfg = _cfg(reward=reward)
    TemplateDataProducer().produce(store, RUN, cfg, _spec(
        stages={"sft": {"group_size": 1}, "grpo_opd": {"group_size": 3}}))
    groups = group_by_id(_rewarded(store, cfg))
    high = 2.0 if combination == "sum" else 1.0
    for members in groups.values():
        assert [r.combined_reward for r in members] == [high, 0.0, high]  # 序号 0、1、2 交替


def test_the_data_trains_end_to_end_through_resume_or_run(store):
    cfg = _cfg()
    TemplateDataProducer().produce(store, RUN, cfg, _spec())

    def make_sources(_start):
        return {n: BatchTrajectorySource(store, read_manifest(store, RUN, n)) for n in ("sft", "grpo_opd")}

    run = resume_or_run(cfg, store=store, run_id=RUN, make_sources=make_sources,
                        attribution=build_attribution_backend(cfg.reward),
                        executor=MockExecutor(), converter=MockConverter())
    assert run.status == "executed"
    assert run.curve_summary["grpo_steps_done"] == 3
    assert run.curve_summary["degenerate_groups_total"] == 0
    assert run.curve_summary["final_mean_reward"] == 0.5


def test_it_is_deterministic(tmp_path):
    a, b = _store(tmp_path, "a"), _store(tmp_path, "b")
    for s in (a, b):
        TemplateDataProducer().produce(s, RUN, _cfg(), _spec())
    assert _snapshot(a) == _snapshot(b)


def test_another_seed_gives_other_scenarios(tmp_path):
    a, b = _store(tmp_path, "a"), _store(tmp_path, "b")
    TemplateDataProducer().produce(a, RUN, _cfg(), _spec(seed=0))
    TemplateDataProducer().produce(b, RUN, _cfg(), _spec(seed=5))
    assert read_manifest(a, RUN, "sft").manifest_hash != read_manifest(b, RUN, "sft").manifest_hash


def test_a_stage_whose_manifest_exists_is_left_alone(store):
    producer = TemplateDataProducer()
    producer.produce(store, RUN, _cfg(grpo_steps=0), _spec())  # 只合成 sft
    assert not store.exists(dataset_manifest_file(RUN, "grpo_opd"))
    before = store.read_product(dataset_manifest_file(RUN, "sft")).to_dict()
    producer.produce(store, RUN, _cfg(), _spec(seed=9))  # sft 已在,只补 grpo_opd
    assert store.read_product(dataset_manifest_file(RUN, "sft")).to_dict() == before
    assert store.exists(dataset_manifest_file(RUN, "grpo_opd"))


def test_nothing_to_do_writes_nothing(store):
    cfg = _cfg(stage_manifest_refs={"sft": "dsm-run-0-sft", "grpo_opd": "dsm-run-0-grpo_opd"})
    TemplateDataProducer().produce(store, RUN, cfg, _spec())
    assert store.list_products(episodes_dir(RUN)) == []


def test_episodes_bind_the_trained_slot_as_self_play_and_an_opponent_with_a_model_ref(store):
    TemplateDataProducer(opponent_model_ref="ref-x").produce(store, RUN, _cfg(), _spec())
    episode = verify_manifest(store, read_manifest(store, RUN, "sft"))[0]
    bindings = [(b.slot_id, b.model_backend, b.model_ref) for b in episode.role_assignment.bindings]
    assert bindings == [("a", "self_play", None), ("opponent", "frozen_reference", "ref-x")]
    assert episode.turns[0].speaker_slot == "a"


def test_a_trained_slot_called_opponent_gets_a_differently_named_opponent(store):
    cfg = _cfg(trained_slot="opponent")
    TemplateDataProducer().produce(store, RUN, cfg, _spec())
    episode = verify_manifest(store, read_manifest(store, RUN, "sft"))[0]
    assert [b.slot_id for b in episode.role_assignment.bindings] == ["opponent", "opponent_b"]


def test_interact_bindings_are_followed(store):
    cfg = _cfg(mode="interact", grpo_steps=1, groups_per_step=1, scenario_ids=["s1"],
               max_turns_per_episode=4, stage_manifest_refs={"sft": f"dsm-{RUN}-sft"},
               bindings=[RoleBinding("b", "pb", "external_api", model_ref="api-1"),
                         RoleBinding("a", "pa", "self_play")])
    TemplateDataProducer().produce(store, RUN, cfg, _spec())
    assert not store.exists(dataset_manifest_file(RUN, "grpo_opd"))  # interact 只合成 sft
    episode = verify_manifest(store, read_manifest(store, RUN, "sft"))[0]
    bindings = [(b.slot_id, b.model_backend, b.model_ref) for b in episode.role_assignment.bindings]
    assert bindings == [("a", "self_play", None), ("b", "external_api", "api-1")]


@pytest.mark.parametrize(
    "bindings",
    [[RoleBinding("b", "pb", "frozen_reference", model_ref="r")],
     [RoleBinding("a", "pa", "self_play")],
     [RoleBinding("a", "pa", "self_play"), RoleBinding("b", "pb", "frozen_reference", model_ref="r"),
      RoleBinding("b", "pc", "frozen_reference", model_ref="r")]],
    ids=["no_trained_slot", "no_opponent", "duplicate_slot"],
)
def test_bindings_that_cannot_make_an_episode_are_a_synthesis_error(store, bindings):
    with pytest.raises(DataSynthesisError, match="bind"):
        TemplateDataProducer().produce(store, RUN, _cfg(bindings=bindings), _spec())
    assert store.list_products(episodes_dir(RUN)) == []


def test_the_rollout_factory_is_injectable(store):
    seen = []

    def factory(cfg, slots, max_turns):
        seen.append((slots, max_turns))
        return lambda scenario, k: {s: ScriptedBackend({s: [f"{s}-{k}-agreed"] * max_turns}) for s in slots}

    TemplateDataProducer(rollout_factory=factory, max_turns=2).produce(store, RUN, _cfg(), _spec())
    assert seen == [(["a", "opponent"], 2)]
    episode = verify_manifest(store, read_manifest(store, RUN, "grpo_opd"))[1]
    assert [t.utterance for t in episode.turns] == ["a-1-agreed", "opponent-1-agreed"]


def test_keyword_rollouts_refuse_judge_attribution():
    reward = RewardConfig(["goal"], "judge", "sum", judge_model_id="j", rubric_version="r1")
    with pytest.raises(DataSynthesisError, match="outcome_split"):
        keyword_alternating_rollouts(_cfg(reward=reward), ["a", "opponent"], 4)


def test_keyword_rollouts_refuse_keywords_that_swallow_every_neutral_line():
    reward = RewardConfig(["goal"], "outcome_split", "sum",
                          {"goal": ["嗯", "(", ".", "-", "~", "|"]})
    with pytest.raises(DataSynthesisError, match="neutral"):
        keyword_alternating_rollouts(_cfg(reward=reward), ["a", "opponent"], 4)


def test_the_neutral_line_skips_a_keyword_that_it_would_contain(store):
    reward = RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["嗯"]})
    cfg = _cfg(reward=reward)
    TemplateDataProducer().produce(store, RUN, cfg, _spec())
    groups = group_by_id(_rewarded(store, cfg))
    assert all(sorted(r.combined_reward for r in m) == [0.0, 1.0] for m in groups.values())


@pytest.mark.parametrize("max_turns, exc", [(0, ValueError), (201, ValueError), ("4", TypeError), (True, TypeError)])
def test_max_turns_is_checked_at_construction(max_turns, exc):
    with pytest.raises(exc):
        TemplateDataProducer(max_turns=max_turns)


@pytest.mark.parametrize("ref", ["", None])
def test_the_opponent_model_ref_must_be_non_empty(ref):
    with pytest.raises(ValueError):
        TemplateDataProducer(opponent_model_ref=ref)
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_mt_data_producer.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'mindtrainer.data_producer'`,末行 `1 error`。

- [ ] **Step 3: 写实现**

新建 `src/mindtrainer/data_producer.py`:

```python
"""默认的数据生产器:模板生成情境与人格,确定性 rollout 后端,再交给既有的 produce_grouped_dataset。

**与目标维度无关(R14)。** 情境与人格来自 TemplateGenerationBackend 的固定模板池,只受
data_spec.seed 影响;rollout 是罐头话语。它让闭环能跑通,但训练数据并不针对规划器选的维度。

grpo_opd 的组内必须真有差异,否则优势恒为 0。默认 rollout 后端按 rollout 序号交替:
偶数序号的训练角色位每句都含 reward.outcome_rules 里每个分量的第一个关键词(结果分 1),
奇数序号说一句不含任何关键词的中性话(结果分 0)。所以 group_size >= 2 的每组奖励都是
{1, 0} 混合,sum 与 product 两种组合下都不退化。
"""
from __future__ import annotations

from collections.abc import Callable

from controlplane.gitstore import GitStore
from mindtrainer.data_spec import DataSpec, DataSynthesisError, stages_to_synthesize
from mindtrainer.training_config import TrainingConfig
from simulator.backends import GenerationBackend, ModelBackend, ScriptedBackend, TemplateGenerationBackend
from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
from simulator.dataset import produce_grouped_dataset
from simulator.generators import generate_persona, generate_scenario

# (cfg, 角色位顺序, max_turns) -> produce_grouped_dataset 的 backends_for_rollout
RolloutFactory = Callable[
    [TrainingConfig, list[str], int], Callable[[Scenario, int], dict[str, ModelBackend]]
]

# 中性话语的候选:取第一句不含任何关键词的。关键词是纯子串匹配,所以要逐句核对。
_NEUTRAL_CANDIDATES = ("嗯。", "(沉默)", "...", "-", "~", "|")
_MAX_TURNS_CAP = 200


def _neutral_utterance(keywords: list[str]) -> str:
    for text in _NEUTRAL_CANDIDATES:
        if not any(kw in text for kw in keywords):
            return text
    raise DataSynthesisError(
        f"every neutral utterance {list(_NEUTRAL_CANDIDATES)} contains an outcome keyword; "
        f"the keyword rollouts cannot produce a zero-reward rollout"
    )


def keyword_alternating_rollouts(
    cfg: TrainingConfig, slots: list[str], max_turns: int
) -> Callable[[Scenario, int], dict[str, ModelBackend]]:
    """默认的 rollout 后端工厂。只对 outcome_split 归因有意义:judge 归因的分数不由关键词决定。"""
    if cfg.reward.attribution != "outcome_split":
        raise DataSynthesisError(
            f"keyword rollouts need reward.attribution 'outcome_split', got {cfg.reward.attribution!r}"
        )
    hit_words = [cfg.reward.outcome_rules[name][0] for name in cfg.reward.components]
    all_words = [w for name in cfg.reward.components for w in cfg.reward.outcome_rules[name]]
    hit = " ".join(hit_words)
    miss = _neutral_utterance(all_words)

    def backends_for_rollout(scenario: Scenario, k: int) -> dict[str, ModelBackend]:
        trained_line = hit if k % 2 == 0 else miss
        return {
            slot: ScriptedBackend(
                {slot: [trained_line if slot == cfg.trained_slot else miss] * max_turns}
            )
            for slot in slots
        }

    return backends_for_rollout


def _slot_layout(cfg: TrainingConfig, opponent_model_ref: str) -> list[tuple[str, str, str | None]]:
    """[(slot_id, model_backend, model_ref)],训练角色位在前。interact 指令自带 bindings,
    照它的角色位与对手后端;batch 指令没有 bindings,配一个 frozen_reference 的对手。"""
    if cfg.bindings:
        slots = [b.slot_id for b in cfg.bindings]
        if len(set(slots)) != len(slots):
            raise DataSynthesisError(f"bindings bind a slot more than once: {slots}")
        others = [b for b in cfg.bindings if b.slot_id != cfg.trained_slot]
        if cfg.trained_slot not in slots or not others:
            raise DataSynthesisError(
                f"bindings must bind trained_slot {cfg.trained_slot!r} "
                f"and at least one other slot, got {slots}"
            )
        return [(cfg.trained_slot, "self_play", None)] + [
            (b.slot_id, b.model_backend, b.model_ref) for b in others
        ]
    opponent = "opponent" if cfg.trained_slot != "opponent" else "opponent_b"
    return [(cfg.trained_slot, "self_play", None), (opponent, "frozen_reference", opponent_model_ref)]


class TemplateDataProducer:
    """DataProducer 的默认实现。确定性:同一份 (run_id, cfg, data_spec) 写出逐字节相同的
    episode 与清单,所以崩溃后重跑会覆盖成同样的内容(GitStore 对相同内容是空操作)。"""

    def __init__(
        self,
        *,
        rollout_factory: RolloutFactory = keyword_alternating_rollouts,
        generation_backend: GenerationBackend | None = None,
        max_turns: int = 4,
        opponent_model_ref: str = "template-opponent-v1",
    ) -> None:
        if isinstance(max_turns, bool) or not isinstance(max_turns, int):
            raise TypeError(f"max_turns must be an int, got {type(max_turns).__name__}")
        if not (1 <= max_turns <= _MAX_TURNS_CAP):
            raise ValueError(f"max_turns must be in [1, {_MAX_TURNS_CAP}], got {max_turns}")
        if not isinstance(opponent_model_ref, str) or not opponent_model_ref:
            raise ValueError("opponent_model_ref must be a non-empty str")
        self._rollout_factory = rollout_factory
        self._generation = generation_backend or TemplateGenerationBackend()
        self._max_turns = max_turns
        self._opponent_model_ref = opponent_model_ref

    def _world(
        self, cfg: TrainingConfig, spec: DataSpec
    ) -> tuple[list[tuple[Scenario, RoleAssignment]], dict[str, Persona], list[str]]:
        layout = _slot_layout(cfg, self._opponent_model_ref)
        slots = [slot for slot, _, _ in layout]
        personas: dict[str, Persona] = {}
        scenarios: list[tuple[Scenario, RoleAssignment]] = []
        for i in range(spec.scenario_count):
            base = generate_scenario(self._generation, spec.seed + i, spec.purpose)
            scenario = Scenario(
                id=base.id, background=base.background,
                role_slots=[RoleSlot(slot, f"角色位 {slot}") for slot in slots],
                conflict=base.conflict, norms=list(base.norms),
                purpose=base.purpose, source=base.source,
            )
            bindings = []
            for j, (slot, backend, model_ref) in enumerate(layout):
                persona = generate_persona(self._generation, (spec.seed + i) * len(layout) + j)
                personas[persona.id] = persona
                bindings.append(RoleBinding(slot, persona.id, backend, model_ref=model_ref))
            scenarios.append(
                (scenario, RoleAssignment(scenario.id, bindings, max_turns=self._max_turns))
            )
        return scenarios, personas, slots

    def produce(self, store: GitStore, run_id: str, cfg: TrainingConfig, data_spec: DataSpec) -> None:
        stages = stages_to_synthesize(store, run_id, cfg)
        if not stages:
            return
        scenarios, personas, slots = self._world(cfg, data_spec)
        backends_for_rollout = self._rollout_factory(cfg, slots, self._max_turns)
        for name in stages:  # required_stages 的顺序:sft 在前,grpo_opd 在后
            produce_grouped_dataset(
                store, run_id, name,
                scenarios=scenarios, personas=personas,
                backends_for_rollout=backends_for_rollout,
                group_size=data_spec.stages[name].group_size,
            )
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_mt_data_producer.py`
Expected: `25 passed`。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+112 passed`,无警告。

- [ ] **Step 6: 提交**

```bash
git add src/mindtrainer/data_producer.py
git add tests/test_mt_data_producer.py
git commit -m "feat: template data producer with keyword-alternating rollouts (grouped data with real in-group reward variance)"
```

---

### Task 33: 派发器的合成分支

**Files:**
- Modify: `src/mindtrainer/dispatch.py`(195 → 244 行;`git diff --numstat` 为 65 行新增、16 行删除)
- Test: `tests/test_mt_dispatch_synthesis.py`(33 个用例)

**Interfaces:**
- Consumes: 任务 30–32 的全部名字;既有的 `_DETERMINISTIC`、`_batch_manifests`、`_interact_sources`。
- Produces:
  - `make_mindtrainer_dispatcher(*, executor_factory=MockExecutor, converter=None, client_factory=None, policy=None, data_producer: DataProducer | None = None) -> Dispatcher`。
  - 私有 `_synthesize_missing(store, run_id, cfg, directive, data_producer) -> None`。

**设计要点:**
- **调用点在读清单之前**:batch 在 `_batch_sources` 之前;interact 在 `client_factory`/`policy` 检查之后、`_interact_sources` 之前(Ruling 8)。
- **`_synthesize_missing` 的顺序**:严格解析 `data_spec`(坏的判 `failed`,Ruling 4)→ 没有 `data_spec` 就返回 → 算 `stages_to_synthesize`,为空就返回 → 没注入生产器判 `failed`,原因写明缺哪些阶段、缺的是 `data_producer` → 其余阶段的清单先查 id 形状与文件在场(Ruling 14)→ 交叉校验(Ruling 5)→ 调生产器。
- **不留孤儿 episode**:其余阶段缺清单或 id 不对时,生产器一次都不调,`episodes_dir(RUN)` 为空(`test_a_missing_manifest_of_another_run_is_never_synthesized_and_nothing_is_written`、`test_a_malformed_manifest_id_for_another_stage_fails_before_synthesizing`);其余阶段的清单在别的 run 下且在场时,只为本 run 缺的阶段合成(`test_a_present_manifest_of_another_run_lets_this_runs_stage_be_synthesized`)。
- **超长整数不经调和循环测**:`test_an_integer_too_long_to_print_in_data_spec_is_a_directive_failure_not_a_value_error` 直接调派发器,断言抛的是 `DirectiveFailedError`。经 `reconcile_once` 测不了,原因见已知限制 12。
- **异常分流**:生产器抛 `DataSynthesisError` 或 `_DETERMINISTIC` 里的错误 → `DirectiveFailedError`(T2);其余原样上抛,指令保持 `pending`(Ruling 6)。测试分别钉住 `OSError`、`RuntimeError` 上抛,并验证之后注入正常生产器能自愈。
- **崩溃自愈**:清单最后写。测试让 `write_manifest` 在 `grpo_opd` 上抛 `OSError`,断言 sft 清单与全部 episode 已落盘、`grpo_opd` 清单不在、指令仍 `pending`;下一轮只合成 `grpo_opd`、episode 文件集合不变(覆盖而非新增)、sft 清单逐字节不变、指令 `executed`。
- **其余情况与现状完全相同**:不带 `data_spec`、或缺的是别的 run 的清单,照旧由 `_batch_manifests` 判 `failed`。`tests/test_mt_dispatch.py` 与 `tests/test_mt_wire.py` 一行不改、全部通过就是证明。
- **重构不改行为**:`_required_stages` 变成 `data_spec.required_stages` 的别名,`_data_run_id` 改为调用 `manifest_data_run`,报错文字逐字不变(Ruling 7)。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_mt_dispatch_synthesis.py`:

```python
"""派发器的数据合成分支:带 data_spec 的指令缺本 run 的清单时,先用注入的生产器合成。"""
import pytest

from controlplane.daemon import DirectiveFailedError, reconcile_once
from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, dataset_manifest_file, episodes_dir, run_dir
from mindtrainer.data_producer import TemplateDataProducer
from mindtrainer.data_spec import DataSynthesisError, parse_data_spec
from mindtrainer.dispatch import make_mindtrainer_dispatcher
from mindtrainer.orchestrator import NotEnoughGroupsError
from mindtrainer.training_config import RewardConfig, TrainingConfig
from simulator.backends import ScriptedBackend
from simulator.datamodel import RoleBinding
from simulator.dataset import DatasetIntegrityError, read_manifest, verify_manifest

RUN = "run-1"
BASE = BigRef("file:///ckpt/base", "a" * 64, 1)
TEACHER = BigRef("file:///ckpt/teacher", "b" * 64, 1)
REWARD = RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]})
SPEC = {"scenario_count": 2, "seed": 0, "purpose": "training",
        "stages": {"sft": {"group_size": 1}, "grpo_opd": {"group_size": 2}}}


def _cfg_dict(run_id=RUN, **kw):
    base = dict(
        mode="batch", trained_slot="a", base_checkpoint_ref=BASE.to_dict(),
        teacher_model_ref=TEACHER.to_dict(), reward=REWARD.to_dict(),
        sft_steps=1, grpo_steps=2, sft_batch_size=1, groups_per_step=1, group_size=2,
        grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=1, allow_resume=True,
        stage_manifest_refs={"sft": f"dsm-{run_id}-sft", "grpo_opd": f"dsm-{run_id}-grpo_opd"},
    )
    base.update(kw)
    return base


def _directive(store, run_id=RUN, *, cfg=None, data_spec=SPEC):
    payload = {"target_dimension": "tom", "recipe_ref": "",
               "training_config": cfg if cfg is not None else _cfg_dict(run_id)}
    if data_spec is not None:
        payload["data_spec"] = data_spec
    env = Envelope(
        id=run_id, type="directive", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1, payload=payload,
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "scheduler: write directive")


def _status(store, run_id=RUN):
    return store.read_product(f"{run_dir(run_id)}/directive.yaml")


class Spy:
    """记下调用,再交给真的默认生产器(或抛出给定的异常)。"""

    def __init__(self, raises=None):
        self.calls = []
        self._raises = raises
        self._inner = TemplateDataProducer()

    def produce(self, store, run_id, cfg, data_spec):
        self.calls.append((run_id, data_spec.to_dict()))
        if self._raises is not None:
            raise self._raises
        self._inner.produce(store, run_id, cfg, data_spec)


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def test_a_directive_with_data_spec_synthesizes_its_data_and_trains(store):
    _directive(store)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    assert spy.calls == [(RUN, SPEC)]
    assert _status(store).status == "executed"
    run = store.read_product(f"{run_dir(RUN)}/training_run.yaml")
    assert run.status == "executed"
    assert run.payload["curve_summary"]["degenerate_groups_total"] == 0
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"
    for stage in ("sft", "grpo_opd"):
        manifest = read_manifest(store, RUN, stage)
        assert manifest.id == f"dsm-{RUN}-{stage}"
        verify_manifest(store, manifest)


def test_without_a_producer_the_directive_fails_and_says_what_is_missing(store):
    _directive(store)
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = _status(store)
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T2"
    reason = env.payload["failure"]["reason"]
    assert "data_producer" in reason and "['sft', 'grpo_opd']" in reason
    assert store.list_products(episodes_dir(RUN)) == []


def test_without_data_spec_a_missing_manifest_still_fails_and_the_producer_is_not_called(store):
    _directive(store, data_spec=None)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    assert spy.calls == []
    env = _status(store)
    assert env.status == "failed"
    assert "is not in the tree" in env.payload["failure"]["reason"]


def test_a_missing_manifest_of_another_run_is_never_synthesized_and_nothing_is_written(store):
    """sft 点名 run-0 的清单且它不在树里:指令反正要判 failed,所以本 run 的 grpo_opd 也不先合成,
    树里不留孤儿 episode。"""
    cfg = _cfg_dict(stage_manifest_refs={"sft": "dsm-run-0-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"})
    _directive(store, cfg=cfg)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    assert spy.calls == []
    assert store.list_products(episodes_dir(RUN)) == []
    assert not store.exists(dataset_manifest_file(RUN, "grpo_opd"))
    assert not store.exists(dataset_manifest_file("run-0", "sft"))
    env = _status(store)
    assert env.status == "failed"
    assert "'dsm-run-0-sft'" in env.payload["failure"]["reason"]


def test_a_malformed_manifest_id_for_another_stage_fails_before_synthesizing(store):
    cfg = _cfg_dict(stage_manifest_refs={"sft": "nonsense", "grpo_opd": f"dsm-{RUN}-grpo_opd"})
    _directive(store, cfg=cfg)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    assert spy.calls == []
    assert store.list_products(episodes_dir(RUN)) == []
    env = _status(store)
    assert env.status == "failed"
    assert "stage_manifest_refs['sft']" in env.payload["failure"]["reason"]


def test_a_present_manifest_of_another_run_lets_this_runs_stage_be_synthesized(store):
    """另一阶段的清单在别的 run 下且确实在场:只为本 run 缺的阶段合成,然后照常训练。"""
    TemplateDataProducer().produce(store, "run-0", TrainingConfig.from_dict(_cfg_dict("run-0")), parse_data_spec(SPEC))
    cfg = _cfg_dict(stage_manifest_refs={"sft": "dsm-run-0-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"})
    _directive(store, cfg=cfg)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    assert spy.calls == [(RUN, SPEC)]
    assert store.exists(dataset_manifest_file(RUN, "grpo_opd"))
    assert not store.exists(dataset_manifest_file(RUN, "sft"))  # 不替别的 run 的清单在本 run 下造一份
    assert _status(store).status == "executed"


def test_without_a_producer_a_valid_spec_with_every_manifest_present_trains_normally(store):
    TemplateDataProducer().produce(store, RUN, TrainingConfig.from_dict(_cfg_dict()), parse_data_spec(SPEC))
    _directive(store)
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    assert _status(store).status == "executed"
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"


def test_manifests_already_in_the_tree_are_not_synthesized_again(store):
    TemplateDataProducer().produce(store, RUN, TrainingConfig.from_dict(_cfg_dict()), parse_data_spec(SPEC))
    _directive(store)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    assert spy.calls == []
    assert _status(store).status == "executed"


def test_a_second_tick_does_not_synthesize_again(store):
    _directive(store)
    spy = Spy()
    disp = make_mindtrainer_dispatcher(data_producer=spy)
    reconcile_once(store, dispatcher=disp)
    reconcile_once(store, dispatcher=disp)
    assert len(spy.calls) == 1


@pytest.mark.parametrize(
    "data_spec, field",
    [
        ("lots", "data_spec must be a dict"),
        (dict(SPEC, scenario_count="2"), "data_spec.scenario_count must be an int"),
        (dict(SPEC, scenario_count=0), "data_spec.scenario_count must be in"),
        (dict(SPEC, seed=1.5), "data_spec.seed must be an int"),
        (dict(SPEC, seed=-1), "data_spec.seed must be in"),
        (dict(SPEC, purpose=None), "data_spec.purpose must be a str"),
        (dict(SPEC, purpose="eval"), "data_spec.purpose must be one of"),
        (dict(SPEC, stages=["sft"]), "data_spec.stages must be a dict"),
        (dict(SPEC, stages={"sft": {"group_size": "1"}}), "group_size must be an int"),
        (dict(SPEC, stages={"grpo_opd": {"group_size": 1}}), "group_size must be in"),
        (dict(SPEC, extra=1), "unknown key"),
    ],
    ids=["not_dict", "count_str", "count_zero", "seed_float", "seed_negative", "purpose_none",
         "purpose_unknown", "stages_list", "group_size_str", "grpo_group_size_one", "extra_key"],
)
def test_a_malformed_data_spec_fails_the_directive_before_touching_the_tree(store, data_spec, field):
    _directive(store, data_spec=data_spec)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))  # 不抛
    env = _status(store)
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T2"
    assert field in env.payload["failure"]["reason"]
    assert spy.calls == []
    assert store.list_products(episodes_dir(RUN)) == []


def test_an_integer_too_long_to_print_in_data_spec_is_a_directive_failure_not_a_value_error(store):
    """YAML 的 0xfff... 能写出超过 4300 位的整数;拼错误信息时若对它做 f-string,裸 ValueError 会逃出派发器。
    这里直接调派发器而不经 reconcile_once:调和循环把指令置 failed 时要把整份 payload 写回 YAML,
    而 yaml.safe_dump 写这个整数本身就抛 ValueError(controlplane 的既有限制,见已知限制)。"""
    payload = {"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict(),
               "data_spec": dict(SPEC, scenario_count=int("f" * 4000, 16))}
    directive = Envelope(
        id=RUN, type="directive", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1, payload=payload,
    )
    spy = Spy()
    with pytest.raises(DirectiveFailedError, match="data_spec.scenario_count must be in") as info:
        make_mindtrainer_dispatcher(data_producer=spy)(store, directive)
    assert "<int>" in info.value.reason
    assert spy.calls == []


def test_a_malformed_data_spec_fails_even_when_every_manifest_is_present(store):
    TemplateDataProducer().produce(store, RUN, TrainingConfig.from_dict(_cfg_dict()), parse_data_spec(SPEC))
    _directive(store, data_spec=dict(SPEC, seed="0"))
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher())
    env = _status(store)
    assert env.status == "failed"
    assert "data_spec.seed" in env.payload["failure"]["reason"]


def test_a_data_spec_that_cannot_feed_the_config_fails_before_writing_episodes(store):
    _directive(store, cfg=_cfg_dict(groups_per_step=3), data_spec=dict(SPEC, scenario_count=2))
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    env = _status(store)
    assert env.status == "failed"
    assert "scenario_count 2 is below groups_per_step 3" in env.payload["failure"]["reason"]
    assert spy.calls == []


def test_a_data_spec_missing_a_needed_stage_fails(store):
    _directive(store, data_spec=dict(SPEC, stages={"sft": {"group_size": 1}}))
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=Spy()))
    env = _status(store)
    assert env.status == "failed"
    assert "no entry for stage 'grpo_opd'" in env.payload["failure"]["reason"]


@pytest.mark.parametrize(
    "exc",
    [DatasetIntegrityError("x"), DataSynthesisError("x"), NotEnoughGroupsError("x")],
    ids=lambda e: type(e).__name__,
)
def test_a_deterministic_producer_failure_fails_the_directive(store, exc):
    _directive(store)
    disp = make_mindtrainer_dispatcher(data_producer=Spy(raises=exc))
    reconcile_once(store, dispatcher=disp)  # 不抛
    env = _status(store)
    assert env.status == "failed"
    assert env.payload["failure"]["category"] == "T2"
    assert f"data synthesis failed ({type(exc).__name__}" in env.payload["failure"]["reason"]
    assert reconcile_once(store, dispatcher=disp) == []


@pytest.mark.parametrize("exc", [OSError("disk hiccup"), RuntimeError("flaky")], ids=lambda e: type(e).__name__)
def test_a_transient_producer_failure_propagates_and_the_directive_stays_pending(store, exc):
    _directive(store)
    with pytest.raises(type(exc)):
        reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=Spy(raises=exc)))
    assert _status(store).status == "pending"
    assert not store.exists(f"{run_dir(RUN)}/training_run.yaml")
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=Spy()))
    assert _status(store).status == "executed"


def test_a_crash_after_the_episodes_but_before_the_manifest_heals_on_the_next_tick(store, monkeypatch):
    """清单最后写:episode 写完、grpo_opd 清单没写就崩,下一轮只重跑缺清单的阶段并覆盖成同样的内容。"""
    import simulator.dataset as dataset_module

    real_write = dataset_module.write_manifest

    def crash_on_grpo(s, manifest):
        if manifest.name == "grpo_opd":
            raise OSError("crash before the grpo_opd manifest")
        return real_write(s, manifest)

    _directive(store)
    monkeypatch.setattr(dataset_module, "write_manifest", crash_on_grpo)
    with pytest.raises(OSError, match="crash before"):
        reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=TemplateDataProducer()))
    assert _status(store).status == "pending"
    assert store.exists(dataset_manifest_file(RUN, "sft"))
    assert not store.exists(dataset_manifest_file(RUN, "grpo_opd"))
    leftover = store.list_products(episodes_dir(RUN))
    assert len(leftover) == 2 + 4  # sft 2 条 + grpo_opd 2 组 x 2 条,都已落盘
    sft_before = store.read_product(dataset_manifest_file(RUN, "sft")).to_dict()

    monkeypatch.setattr(dataset_module, "write_manifest", real_write)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    assert len(spy.calls) == 1
    assert _status(store).status == "executed"
    assert store.read_product(dataset_manifest_file(RUN, "sft")).to_dict() == sft_before
    assert store.list_products(episodes_dir(RUN)) == leftover  # 覆盖,不多出 episode
    verify_manifest(store, read_manifest(store, RUN, "grpo_opd"))


def test_an_interact_directive_synthesizes_only_its_sft_manifest(store):
    """interact 只从清单读 sft;grpo_steps=0 时不需要实时服务端,client_factory 不会被调用。"""
    cfg = _cfg_dict(
        mode="interact", grpo_steps=0, scenario_ids=["s1"], max_turns_per_episode=4,
        stage_manifest_refs={"sft": f"dsm-{RUN}-sft", "grpo_opd": f"dsm-{RUN}-grpo_opd"},
        bindings=[RoleBinding("a", "pa", "self_play").to_dict(),
                  RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1").to_dict()],
    )
    _directive(store, cfg=cfg)

    def no_client():
        raise AssertionError("grpo_steps=0: the live server must not be contacted")

    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        client_factory=no_client, policy=ScriptedBackend({}), data_producer=TemplateDataProducer()))
    assert _status(store).status == "executed"
    assert store.exists(dataset_manifest_file(RUN, "sft"))
    assert not store.exists(dataset_manifest_file(RUN, "grpo_opd"))
    episode = verify_manifest(store, read_manifest(store, RUN, "sft"))[0]
    assert [b.slot_id for b in episode.role_assignment.bindings] == ["a", "b"]


def test_an_interact_directive_without_a_client_fails_before_synthesizing(store):
    cfg = _cfg_dict(
        mode="interact", scenario_ids=["s1"], max_turns_per_episode=4,
        stage_manifest_refs={"sft": f"dsm-{RUN}-sft"},
        bindings=[RoleBinding("a", "pa", "self_play").to_dict(),
                  RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1").to_dict()],
    )
    _directive(store, cfg=cfg)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    env = _status(store)
    assert env.status == "failed"
    assert "client_factory" in env.payload["failure"]["reason"]
    assert spy.calls == []


def test_a_later_round_reuses_an_earlier_runs_data_without_synthesizing(store):
    """clone/replay 的指令点名前一轮的清单,不带 data_spec,也不会触发合成。"""
    _directive(store)
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=TemplateDataProducer()))
    _directive(store, "run-2", cfg=_cfg_dict(RUN), data_spec=None)
    spy = Spy()
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=spy))
    assert spy.calls == []
    assert _status(store, "run-2").status == "executed"
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_mt_dispatch_synthesis.py`
Expected: 末行 `32 failed, 1 passed`(通过的那条是不注入生产器、清单全在的 `test_without_a_producer_a_valid_spec_with_every_manifest_present_trains_normally`,行为与现状相同)。传 `data_producer` 的用例报 `TypeError: make_mindtrainer_dispatcher() got an unexpected keyword argument 'data_producer'`;不传的用例断言失败,例如 `test_without_a_producer_the_directive_fails_and_says_what_is_missing` 报 `assert ('data_producer' in "directive run-1: dataset manifest 'dsm-run-1-sft' for stage 'sft' is not in the tree")`。

- [ ] **Step 3: 写实现**

把下面的差异存成仓库根目录下的 `dispatch.patch`,运行 `git apply dispatch.patch && rm dispatch.patch`(或按块手改)。改完 `wc -l src/mindtrainer/dispatch.py` 应为 `244`。

```diff
--- a/src/mindtrainer/dispatch.py
+++ b/src/mindtrainer/dispatch.py
@@ -10,6 +10,10 @@
 from controlplane.migration import UnknownSchemaVersionError
 from controlplane.paths import dataset_manifest_file
 from mindtrainer.checkpoint import CheckpointConverter, MockConverter
+from mindtrainer.data_spec import (
+    DataProducer, DataSynthesisError, InvalidDataSpecError, check_data_spec_against_config,
+    data_spec_from_directive, manifest_data_run, required_stages, stages_to_synthesize,
+)
 from mindtrainer.executor import MockExecutor, TrainingExecutor
 from mindtrainer.grouping import DegenerateGroupError
 from mindtrainer.orchestrator import NotEnoughGroupsError, resume_or_run
@@ -35,33 +39,73 @@
 )
 
 
-def _required_stages(cfg: TrainingConfig) -> list[str]:
-    """真正会用到的阶段:配了步数的才算,且与模式有关。stage_manifest_refs 里多写的阶段不读。
+# 真正会用到的阶段只在 data_spec.required_stages 一处实现:合成与读清单必须对"要哪几个阶段"
+# 意见一致,否则会合成用不到的阶段,或漏合成要用的阶段。
+_required_stages = required_stages
 
-    sft 两种模式都从清单读,配了 sft_steps 就要。grpo_opd 只有 batch 模式从清单读;
-    interact 模式的 grpo_opd 来自实时会话(_interact_sources 事后整段覆盖),不读清单,
-    所以即便指令在 stage_manifest_refs 里为 interact 顺手点了一个 grpo_opd 的清单 id
-    (validate_training_config 并不禁止),这里也不该把它当成需要校验的阶段。"""
-    wanted = []
-    if cfg.sft_steps > 0:
-        wanted.append("sft")
-    if cfg.mode == "batch" and cfg.grpo_steps > 0:
-        wanted.append("grpo_opd")
-    return wanted
 
-
 def _data_run_id(run_id: str, ref: str, stage: str) -> str:
     """清单 id 的格式是 dsm-{数据所在的 run}-{阶段}(build_manifest 是唯一的构造点)。后一轮训练
     常常复用前一轮的数据集,所以数据所在的 run 从 id 里解出来,不假设它等于指令自己的 run。"""
-    head, tail = "dsm-", f"-{stage}"
-    if not (ref.startswith(head) and ref.endswith(tail) and len(ref) > len(head) + len(tail)):
+    data_run = manifest_data_run(ref, stage)
+    if data_run is None:
         raise DirectiveFailedError(
             f"directive {run_id}: stage_manifest_refs[{stage!r}]={ref!r} is not a manifest id "
             f"of the form 'dsm-<run>-{stage}'"
         )
-    return ref[len(head):-len(tail)]
+    return data_run
 
 
+def _synthesize_missing(
+    store: GitStore,
+    run_id: str,
+    cfg: TrainingConfig,
+    directive: Envelope,
+    data_producer: DataProducer | None,
+) -> None:
+    """缺清单、指令带 data_spec、清单的数据所在 run 就是本 run:三者同时成立才合成。
+    其余情况什么都不做,交给 _batch_manifests 按原样判(缺清单仍是 failed)。
+
+    走到这里时配置校验、归因与 interact 客户端检查都已通过;此后 data_spec 只要在 payload 里就先严格解析:
+    写错的 data_spec 是指令本身的错,不等用到才暴露。不合成的其余阶段先查清单在场,再调生产器。
+    生产器抛的确定性错误判 failed(T2);瞬态错误(OSError 等)原样上抛,指令保持 pending,
+    下一轮重入时已在树里的清单不再合成,episode 写完而清单未写的阶段整段重跑覆盖。"""
+    try:
+        spec = data_spec_from_directive(directive)
+    except InvalidDataSpecError as exc:
+        raise DirectiveFailedError(f"directive {run_id}: {exc}") from exc
+    if spec is None:
+        return
+    stages = stages_to_synthesize(store, run_id, cfg)
+    if not stages:
+        return
+    if data_producer is None:
+        raise DirectiveFailedError(
+            f"directive {run_id}: dataset manifest(s) for stage(s) {stages} are not in the tree and "
+            f"must be synthesized from data_spec, but no data_producer was supplied"
+        )
+    # 其余阶段的清单(不合成的那些)先查 id 形状与文件是否在场:有一份缺了,指令反正要判 failed,
+    # 先合成只会在树里留下一批不属于任何训练的 episode。内容是否读得出、与 episode 是否一致,
+    # 仍由 _batch_manifests 在合成之后完整校验。
+    for name in _required_stages(cfg):
+        ref = cfg.stage_manifest_refs.get(name)
+        if name in stages or not ref:
+            continue
+        if not store.exists(dataset_manifest_file(_data_run_id(run_id, ref, name), name)):
+            raise DirectiveFailedError(
+                f"directive {run_id}: dataset manifest {ref!r} for stage {name!r} is not in the tree"
+            )
+    reason = check_data_spec_against_config(spec, cfg, stages)
+    if reason is not None:
+        raise DirectiveFailedError(f"directive {run_id}: {reason}")
+    try:
+        data_producer.produce(store, run_id, cfg, spec)
+    except (DataSynthesisError, *_DETERMINISTIC) as exc:
+        raise DirectiveFailedError(
+            f"directive {run_id}: data synthesis failed ({type(exc).__name__}: {exc})"
+        ) from exc
+
+
 def _batch_manifests(store: GitStore, run_id: str, cfg: TrainingConfig) -> dict[str, DatasetManifest]:
     """指令点名的清单必须在场,且就是点名的那一份。缺文件是配置问题(确定性),而 read_product
     缺文件抛的是 FileNotFoundError、read_manifest 又只会因内容对不上抛 DatasetIntegrityError,
@@ -136,6 +180,7 @@
     converter: CheckpointConverter | None = None,
     client_factory: Callable[[], object] | None = None,
     policy: ModelBackend | None = None,
+    data_producer: DataProducer | None = None,
 ) -> Dispatcher:
     """构造 MindTrainer 派发器。它只做一件事:把一条 pending 的 directive 跑成候选。
 
@@ -144,6 +189,8 @@
 
     - batch:按 stage_manifest_refs 读数据集清单。
     - interact:另需 client_factory 与 policy,注入后 grpo_opd 走实时会话。
+    - data_producer:指令带 data_spec 且本 run 名下的清单缺失时,用它先合成数据(两种模式都可,
+      interact 只合成 sft)。不注入时行为与改动前相同,需要合成的指令判 failed 并写明缺什么。
     - 确定性失败(数据不对、配置不合法、该人工介入)抛 DirectiveFailedError,调和循环把
       directive 置 failed,不再重试;其余异常原样上抛,保持 pending 等下一轮。
     """
@@ -168,8 +215,10 @@
                 raise DirectiveFailedError(
                     f"directive {run_id}: interact mode needs a client_factory and a policy"
                 )
+            _synthesize_missing(store, run_id, cfg, directive, data_producer)
             make_sources = _interact_sources(store, run_id, cfg, client_factory, policy)
         else:
+            _synthesize_missing(store, run_id, cfg, directive, data_producer)
             make_sources = _batch_sources(store, run_id, cfg)
         try:
             resume_or_run(
```

- [ ] **Step 4: 运行,确认通过,且既有派发器测试原样通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_mt_dispatch_synthesis.py tests/test_mt_dispatch.py tests/test_mt_wire.py`
Expected: `76 passed`(33 + 39 + 4)。

再跑 `git diff --stat -- tests/test_mt_dispatch.py tests/test_mt_wire.py`:输出为空(既有测试一行未改)。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+145 passed`,无警告。

- [ ] **Step 6: 提交**

```bash
git add src/mindtrainer/dispatch.py
git add tests/test_mt_dispatch_synthesis.py
git commit -m "feat: the mindtrainer dispatcher synthesizes missing manifests from data_spec through an injected data producer"
```

---

### Task 34: 全量回归与改动范围检查

**Files:** 无改动。

- [ ] **Step 1: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: 通过数为起点 B 加上 +145(B 是任务 30 第 1 步记下的基线;在 `main@0385004` 上即 965),无警告。若 B 没有记下,不要在当前工作区回退,而是在另一个工作树里补测:`git worktree add ../b-check "$BASE"`,进 `../b-check` 跑一次测试命令并记下通过数,再 `git worktree remove ../b-check`。

- [ ] **Step 2: 每个新源文件的第一条语句**

Run: `grep -n "^from __future__ import annotations" src/mindtrainer/data_spec.py src/mindtrainer/data_producer.py`
Expected(两个文件前面都只有模块 docstring):

```
src/mindtrainer/data_spec.py:12:from __future__ import annotations
src/mindtrainer/data_producer.py:11:from __future__ import annotations
```

- [ ] **Step 3: 依赖方向**

Run:

```bash
if grep -rnE "^(from|import) diagnostic" src/mindtrainer/; then echo "diagnostic: FOUND"; else echo "diagnostic: none"; fi
if grep -rnE "^(from|import) mindtrainer" src/controlplane/ src/simulator/; then echo "reverse: FOUND"; else echo "reverse: none"; fi
```

Expected: 恰好两行 `diagnostic: none` 与 `reverse: none`。两条分开判,任一条打印 `FOUND`(连同匹配的行)都算失败。

- [ ] **Step 4: 改动范围**

Run: `git diff --stat "$BASE"..HEAD`(`BASE` 是任务 30 第 1 步记下的提交号)。真实仓库里本计划的四个提交之后可能紧跟别的计划的提交,那时 `HEAD~4` 不再指向任务 30 之前,所以以 `BASE` 为准。只有 `BASE` 没记下时,才退回 `git diff --stat HEAD~4..HEAD`,并先用 `git log --oneline -6` 核对最上面四条恰好是本计划的四个提交(提交信息见各任务 Step 6),且 HEAD 之后没有别的提交;对不上就按提交信息找出任务 30 的提交,用它的父提交代替 `HEAD~4`。
Expected:

```
 src/mindtrainer/data_producer.py    | 148 +++++++++++++++
 src/mindtrainer/data_spec.py        | 215 ++++++++++++++++++++++
 src/mindtrainer/dispatch.py         |  81 ++++++--
 tests/test_mt_data_producer.py      | 231 +++++++++++++++++++++++
 tests/test_mt_data_spec.py          | 197 ++++++++++++++++++++
 tests/test_mt_data_spec_stages.py   | 133 ++++++++++++++
 tests/test_mt_dispatch_synthesis.py | 358 ++++++++++++++++++++++++++++++++++++
 7 files changed, 1347 insertions(+), 16 deletions(-)
```

恰好 7 个文件,都在 File Structure 表里;不含 `cli/package*.json`、`pyproject.toml`、`src/controlplane/`、`src/simulator/`、`tests/test_mt_dispatch.py`、`tests/test_mt_wire.py`。`git status --short` 里 `cli/package*.json` 仍是改动前的 ` M`,不要碰。

本任务不提交。

---

## 完成标准

- 测试命令的通过数为 **B+145**、无警告。B 是任务 30 第 1 步实测的基线;增量为任务 30 +70、任务 31 +17、任务 32 +25、任务 33 +33。在 `main@0385004` 上 B=820,即 965。
- 改动只涉及 File Structure 表里的 7 个文件;`tests/test_mt_dispatch.py`、`tests/test_mt_wire.py` 一行未改且全部通过。
- 一条带 `data_spec` 的 batch 指令,在只有指令、没有任何数据的仓库里,经 `reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(data_producer=TemplateDataProducer()))` 一轮后:两份清单 `dsm-<run>-sft`、`dsm-<run>-grpo_opd` 在树里且通过 `verify_manifest`,`training_run` 为 `executed` 且 `degenerate_groups_total == 0`,候选 `pending_review`,指令 `executed`(`test_a_directive_with_data_spec_synthesizes_its_data_and_trains`)。
- `data_spec` 每个字段的类型错与范围错都判指令 `failed`(T2),不写任何 episode,也不让异常逃出 `reconcile_once`。

## 已知限制

1. **默认生产器与目标维度无关(R14)。** 情境与人格来自 `TemplateGenerationBackend` 的固定模板池,只受 `data_spec.seed` 影响;rollout 是罐头话语,奖励差异是人为交替出来的。所以闭环能跑通,但训练数据并不针对规划器选的维度,训练出的候选也不代表任何能力提升。按维度生成需要桥接后端的生成器,不在本期。
2. **默认 rollout 后端只适用于 `outcome_split` 归因。** judge 归因的指令用它会抛 `DataSynthesisError`。现有派发器在合成之前就因"没有 judge"判了 `failed`,所以目前走不到这里;以后注入 judge 时要同时换 rollout 工厂。
3. **合成是同步的。** 生产器在派发器里、也就在调和循环的 tick 里跑,每写一条 episode 是一次 git 提交,而每次 `write_product` 要起三个 git 子进程(`add`、`diff --cached`、`commit`)。`scenario_count=64` 且两个阶段的 `group_size` 都取上限 16 时,两份数据集共 2048 条 episode,即 2048 次提交、约 6000 个子进程,期间审批与晋级动作被饿住。与派发器既有的同步阻塞限制相同。
4. **瞬态异常会让守护进程退出。** 生产器抛的 `OSError` 原样上抛,`run_daemon` 不捕获,需要外部监督者重启;重启后指令仍 `pending`,已在树里的清单不重做,缺清单的阶段整段重跑。
5. **崩溃自愈依赖生产器的确定性。** 默认生产器是确定性的,重跑覆盖成同样内容。以后注入的非确定性 rollout 后端重跑时会写出 id 相同、内容不同的 episode 并覆盖旧文件;清单只锚定最后一次写出的内容,所以最终仍一致,但 git 历史里会留下被覆盖的版本。若某阶段这一轮的 episode 少于上一轮(例如改了 `data_spec`),多出的旧 episode 会留在目录里但不在清单里。
6. **生成的情境与人格不写进 `scenarios/`、`personas/` 目录**(Ruling 11)。interact 的 `grpo_opd` 实时会话读的是指令 `scenario_ids` 点名的情境,与合成的 sft 数据无关;为 interact 指令准备这些情境是调度器(计划 5)的事。
7. **`data_spec` 的字段集由本计划钉死**(Ruling 2)。调度器要加字段(例如 `max_turns`、按维度的生成提示)时,必须同步改 `parse_data_spec`。
8. **`mindtrainer.wire` 不注入生产器**(设计 §10.2 要求它保持原样)。用 `scripts/run_controlplane.py` 跑时,带 `data_spec` 而缺清单的指令会判 `failed` 并写明缺 `data_producer`。注入由计划 9 的 `factory.wire` 负责。
9. **注入的生产器必须自己把确定性错误包成 `DataSynthesisError`。** 派发器只把 `DataSynthesisError` 与 `_DETERMINISTIC` 里的错误判 `failed`。替换后的生产器若让 `ValueError`(例如 `produce_grouped_dataset` 的参数检查)、`simulator.runner.RoleSlotMismatchError`(它是 `ValueError` 的子类,但不在接的元组里)、`KeyError`(人格缺失)原样抛出,会被当成瞬态:指令保持 `pending`、每轮重派,守护进程每次都在这里退出。默认生产器在调用前已排除这些情形,走不到。
10. **半截清单无法自愈。** "清单是否已在"用 `GitStore.exists` 判,它只看文件是否在场。崩在清单文件写到一半、或写完文件但 `git commit` 之前时,文件已在,下一轮不会重新合成,随后 `_batch_manifests` 读不出(YAML 截断)或读出与 episode 对不上,指令判 `failed`。要人工删掉那份清单文件、写一条新指令重来。
11. **默认 rollout 下 sft 的 `group_size` 建议取 1。** 同一个 rollout 工厂也用于 sft:`group_size>1` 时奇数序号的 sft 样本是中性话,一半 SFT 数据只是"嗯。"之类的无内容回复。调度器(计划 5)给 sft 写 `group_size: 1`(设计 §9 的示例就是 1)即可避开;本计划不为 sft 单独换成恒命中的话语。
12. **超长整数在调和循环的写回处仍会逃出。** 本计划保证 `data_spec` 里的超长整数变成 `DirectiveFailedError`,但调和循环随后把指令置 `failed` 时要把整份 payload 用 `yaml.safe_dump` 写回,而 PyYAML 写超过 4300 位的整数同样抛 `ValueError`,守护进程仍会退出。这是 `controlplane` 的既有问题,与 `data_spec` 无关:在 `main` 上把 `training_config.sft_steps` 写成同样的整数,同样复现。修它要改 `controlplane.daemon` 或 `GitStore`,不在本计划范围。
13. **interact 合成的 sft 数据不用 `bindings` 里的人格**(见任务 32 设计要点)。训练位的角色位名与对手的后端照抄,人格换成模板生成的;要让 sft 与实时会话用同一组人格,需要生产器读 `personas/` 目录,本期不做。

## 未验证项

1. **已验证**:计划里的全部代码与测试,在 `main@0385004` 的两份临时拷贝里按任务 30 → 33 的顺序真实跑过。各任务的失败输出、通过数与累计数(890 / 907 / 932 / 965,按审计意见修订后重跑)都来自那次运行,`-W error` 下无警告。第二份拷贝里建了一个临时 git 仓库,按计划的 `git add` 与 `git commit` 命令逐个任务提交,任务 34 Step 3、Step 4 的输出就是在那里实测的。任务 33 的补丁用 `git apply` 在原始 `dispatch.py` 上应用,结果与验证时的文件逐字节相同。
2. **没有在真实仓库里提交过**:临时仓库的基点是 rsync 出来的单个提交,不是 `main` 的真实历史。所以任务 34 Step 4 改用任务 30 第 1 步记下的 `BASE`,`HEAD~4` 只作退路。绝对通过数依赖执行顺序(计划 1 等是否先落地),所以本计划改成只保证增量(+145),不保证绝对数;890 / 907 / 932 / 965 只是 B=820 时的实测值。
3. **变异检查只做了五处**,都被测试抓住:去掉 Ruling 14 的预检(2 个用例失败)、生产器异常一律接成 `failed`(3 个用例失败)、`stages_to_synthesize` 不判数据所在 run(派发器与阶段判定都有用例失败)、rollout 不交替(6 个用例失败)、interact 路径不调合成(1 个用例失败)。没有做系统性的变异测试。
4. **没有测过耗时**:真实的 `run_daemon` 长循环,以及 `scenario_count`、`group_size` 取上限时的耗时与 git 提交量都没有测。
5. **与计划 5 的格式对接没有验证**:调度器是否按本计划钉死的 `data_spec` 字段集与 `dsm-<run>-<阶段>` 规则写指令,计划 5 已落盘:它的 `construct_directive` 与 `check_directive` 测试用本计划的 `parse_data_spec` 与 `check_data_spec_against_config` 校验调度器写出的指令,覆盖了这一对接;以最终完整重放为准。
