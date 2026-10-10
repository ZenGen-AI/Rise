# RismForge 设计文档集

本目录是 RismForge 的当前系统设计。读者是人类架构师和 AI。每篇文档自包含:只读一篇,就有足够的背景理解它讲的子系统,不需要跳转。

## 从哪里开始

- 想快速了解系统:读 `00-overview.md` 的第 1 章「一页读懂」。
- 要理解整体架构、核心机制和端到端流程:读 `00-overview.md` 全文。
- 要动手改某个子系统:读 `00-overview.md` 的第 3 到 5 章,再读该子系统的文档。
- AI 读者:把 `00-overview.md` 加上相关子系统文档放进上下文。每篇子系统文档都内嵌「系统速览」,单读一篇也不缺背景。

## 按问题找文档

| 问题 | 文档 |
|---|---|
| 系统是什么,为什么这样设计,现状到哪一步 | `00-overview.md` |
| 产物、状态机、调和循环、审核门、命令行审核 | `10-controlplane.md` |
| 情境、人格、仿真运行器、数据集、实时环境协议 | `20-socialsimulator.md` |
| 诊断评测怎么打分、怎么判退化、怎么防污染 | `21-diagnostic-evaluation.md` |
| 训练怎么跑、奖励怎么算、怎么续训 | `30-mindtrainer.md` |
| 技能、记忆、激活轨迹 | `40-actio.md` |
| 部署态智能体怎么自进化 | `50-mindagent.md` |
| 配方怎么校验、检索、记战绩 | `60-recipestore.md` |
| 下一轮怎么装配上下文、校验决策、构造指令 | `70-trainscheduler.md` |
| 决策怎么生成、怎么严格解析 | `71-trainplanner.md` |
| 怎么调外部强模型、怎么记成本 | `80-inference-bridge.md` |
| 怎么把全部组件装起来、有哪些命令行入口 | `90-factory-and-cli.md` |

## 状态标记

| 标记 | 含义 |
|---|---|
| **[已落地]** | 代码在主干上且有测试 |
| **[计划中]** | 设计与施工计划已定,代码还没写 |
| **[推迟]** | V1 明确不做 |

每篇文档头部写明基线提交。已落地部分以代码为准,计划中部分的接口签名来自施工计划,施工时可能微调。

图只画目标系统,不标进展:图里的每个组件与连线都是系统要实现成的样子。哪些已落地、哪些计划中,只看正文的状态标记与各文档的现状章。

## 与历史文档的关系

`docs/superpowers/specs/` 下有四份历史设计,`docs/superpowers/plans/` 下有施工计划。本目录取代了这四份设计。它们保留作历史记录,与本目录冲突时以本目录为准。施工计划是逐任务的施工单,含完整测试与代码,不属于本文集。

下表给出闭环集成的九个工作包与施工计划文件的对应,文档里的「工作包 N」指这一列。

| 工作包 | 任务号 | 施工计划文件 |
|---|---|---|
| 1 地基 | 19 至 27 | `plans/2026-10-09-bridge-and-foundations.md` |
| 2 数据合成接线 | 30 至 34 | `plans/2026-10-09-data-synthesis-wiring.md` |
| 3 RecipeStore | 40 至 48 | `plans/2026-10-09-recipestore.md` |
| 4 TrainPlanner | 50 至 56 | `plans/2026-10-09-trainplanner.md` |
| 5 TrainScheduler | 60 至 68 | `plans/2026-10-09-trainscheduler.md` |
| 6 Actio | 70 至 78 | `plans/2026-10-09-actio.md` |
| 7 MindAgent | 80 至 89 | `plans/2026-10-09-mindagent.md` |
| 8 调和循环扩展 | 90 至 98 | `plans/2026-10-09-reconcile-extension.md` |
| 9 组合根与端到端 | 100 至 109 | `plans/2026-10-09-factory-and-e2e.md` |

已落地部分的施工记录在同目录下更早的计划里:控制面与工厂模拟器、诊断评测、实时环境服务、MindTrainer 两个部分与它的接线。

## 维护

- 文档结构与写法见 `STYLE.md`,子系统文档骨架见 `TEMPLATE.md`。
- 图由 `diagrams/fig_*.py` 生成,共享的「系统速览」区块源在 `_shared/`。不要手改文档里被 `<!-- figure:... -->` 与 `<!-- shared:... -->` 包住的区块。
- 提交前运行 `python3 docs/design/tools/sync_docs.py` 与 `python3 docs/design/tools/check_docs.py`。
