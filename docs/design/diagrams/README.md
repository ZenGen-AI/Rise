# 绘图速查

图用 `svgkit.py` 生成,纯矢量 SVG。每张图一个脚本 `fig_<名字>.py`,暴露 `build() -> Canvas`。`build_all.py` 逐个加载,产出 `<名字>.svg` 与 `<名字>.meta.json`。文档里用同名的 figure 标记引用,同步脚本把图、图号、说明和文字版读图写进文档。

现成的示例:`fig_00-architecture.py`(分层与受限直连)、`fig_00-closed-loop.py`(带编号的流程)、`fig_00-deployment.py`(部署与存储)、`fig_00-states.py`(状态机)。

## 命令

```
python3 docs/design/diagrams/build_all.py 名字 ...     生成指定的图,做卫生检查
python3 docs/design/tools/render_figs.py 名字 ...      渲染成 PNG,打印路径,用 Read 打开目检
python3 docs/design/tools/sync_docs.py 文档名.md       把图写进文档
```

## 接口

| 调用 | 作用 |
|---|---|
| `Canvas(w, h, title=, desc=)` | 画布。宽度建议 1080,高度不超过 800。`title` 是图名,`desc` 是一句话说明这张图回答什么问题 |
| `lane(x, y, w, h, label, kind=, dashed=False, label_align="start")` | 泳道,用来表示层或分组,默认实线。只画底框,里面的方框自己摆 |
| `box(x, y, w, title, sub=None, h=None, kind=, dashed=False, name=None)` | 圆角方框。`sub` 用换行分多行。返回节点 |
| `gate(x, y, w, title, sub=None, h=None, kind="human")` | 六边形,表示人工审核门或判断点 |
| `store(x, y, w, title, sub=None, h=None, kind="store")` | 圆柱,表示存储 |
| `edge(a, b, a_side="right", b_side="left", a_f=0.5, b_f=0.5, via=None, label=None, dashed=False, tail=False, color=, width=)` | 两个节点之间的连线,自动折线。`a_side`、`b_side` 取 left、right、top、bottom。`a_f`、`b_f` 是沿该边的位置比例。`tail=True` 画双向箭头 |
| `arrow(points, label=None, label_at=0.5, ...)` | 按坐标点画折线箭头 |
| `badge(x, y, n, kind=)` | 编号圆点,中心在 (x, y)。流程图用它标步骤号 |
| `note(x, y, w, text, kind=)` | 便签,多行用换行 |
| `label(x, y, s)`、`text(x, y, s, ...)` | 带白底的标注、普通文字 |
| `legend(x, y, [(kind, 文字, 是否虚线), ...])` | 图例,放在图底部 |

节点对象有 `.left(f)`、`.right(f)`、`.top(f)`、`.bottom(f)` 返回边上的点,还有 `.x .y .w .h .cx .cy`。

颜色语义固定:`control` 控制层,`compute` 计算层,`infra` 共享基础设施,`human` 人工门与人工动作,`store` 存储,`neutral` 中性,`danger` 失败与风险。图只画目标系统,不标进展。虚线只表达设计事实(人工直接改文件、只存引用、下一 tick 重入、别的写方、禁止的导入),用了就要在图例写明含义。制图库出图时扫描可见文字,出现「计划中」「已落地」「推迟」「V1」「今天」等字样直接报错。

## 自动检查

构建时下面三种问题直接抛 `ValueError`,按提示改:文字溢出方框(报告需要多少像素、只有多少),方框互相重叠,图元超出画布。汉字按一个字符一个字号宽估算,15 号加粗标题每个汉字约 16 像素,12 号说明每个汉字约 12 像素。

## 常见坑

- 连线标签放在折线中点。长竖线的标签可能压住别的线,用 `label_at` 挪位置,或用 `via` 改走向。
- 多条线进同一个方框的同一边,用 `b_f` 错开。
- 连线穿过无关方框时,用 `via=[(x, y), ...]` 绕行。库不检查线与方框相交,只能靠目检。
- 两个节点标题相同时给其中一个传 `name=`,否则文字版里分不清。
- 一张图不超过 12 个节点。总览的架构图和流程图可到 16。
- 文字版由节点名、副标题和连线标签自动生成,所以标签要写成人能读懂的短语。
