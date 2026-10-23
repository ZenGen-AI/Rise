# 读不出的已提交产物不让守护进程退出 Implementation Plan

**Goal:** 指令、候选、规划决策、模型与门产物读不出时,守护进程不退出、不做错事、告诉人;依赖它的工作暂停,直到人修好文件。

**Architecture:** 视图读取把 `_UNREADABLE` 那一族异常(YAML 坏、缺键、类型不对、信封校验失败、schema 版本无法迁移;`InvalidBigRefError` 是 `TypeError` 子类,也在内)转成状态 `UNREADABLE_STATUS`,id 从路径取,路径收进 `TreeState.unreadable`。`reconcile` 加两条暂停规则。`reconcile_once` 对每个读不出的路径记警告,守护进程持有已报告集合去重。

**Tech Stack:** Python 3.11+、pytest。

**Spec:** `docs/design/10-controlplane.md` 的 `TreeState.unreadable` 行、第 6 章「读不出的产物不确定就停」、第 8 章读不出那一行、CP-17;`docs/design/00-overview.md` 风险表第 5 项。

## Global Constraints

- `OSError` 不在 `_UNREADABLE` 里,原样上抛(瞬态)。
- 轮次产物(R19)与诊断报告(记 `stub`)的既有兜底不改。
- 树里没有坏文件时,`read_tree_state` 与 `reconcile` 的结果和改动前相同;`enabled` 为空时也读模型文件。
- 每个任务先写失败的测试,确认它失败,再实现。
- 测试命令一律 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest <path> -q`。

---

### Task 1: 视图投影 `unreadable`

**Files:** Modify `src/controlplane/reconcile.py`(`UNREADABLE_STATUS`、`TreeState.unreadable`)、`src/controlplane/daemon.py`;Test `tests/test_daemon_closed_loop_views.py`

```python
UNREADABLE_STATUS = "unreadable"


@dataclass
class TreeState:
    ...
    unreadable: tuple[str, ...] = ()
```

- [ ] `_UNREADABLE_ROUND` 改名 `_UNREADABLE`,轮次与报告两处照用。
- [ ] 指令(既有视图与 `RunView`)、候选(同上)、规划决策、模型、配方候选与提案:读不出时状态记 `UNREADABLE_STATUS`,id 依次取 run id、`cand-<run>`、文件名去后缀、`zengen-v<版本>`、文件名去后缀、提案目录名;路径进 `unreadable`(排序去重)。
- [ ] `enabled` 为空时也逐个读模型文件,只为填 `unreadable`。
- [ ] 测试:每类一份坏文件,视图状态是 `unreadable` 且路径在 `unreadable` 里;`OSError` 仍上抛;好树的 `unreadable` 为空。

### Task 2: 两条暂停规则

**Files:** Modify `src/controlplane/reconcile.py`;Test `tests/test_reconcile_closed_loop.py`、`tests/test_reconcile.py`

- [ ] `unreadable` 非空时 `_closed_loop_actions` 返回空列表。
- [ ] `unreadable` 里有 `models/` 下的路径时,不产生 `request_diagnostic` 与 `promote_candidate`;`dispatch_directive` 与 `await_review` 照常。
- [ ] 测试:纯函数,每条规则的触发与不触发。

### Task 3: 警告日志与守护进程不退出

**Files:** Modify `src/controlplane/daemon.py`;Test `tests/test_daemon_closed_loop_views.py`

- [ ] `reconcile_once(..., reported: set[str] | None = None)`:对 `unreadable` 里每个路径记一行警告(路径、异常类名);传入 `reported` 时只记新出现的,从树里消失的路径移出集合,再坏会再记。`run_daemon` 持有这个集合。
- [ ] 测试:坏文件在树里时 `run_daemon` 跑多个 tick 不退出、同一路径只记一次、其余指令照常派发;修好之后闭环恢复。

### Task 4: 文档标记、检查与全量

- [ ] 本计划对应的 [计划中] 改为 [已落地];`check_docs`、`check_consistency` 0 错误 0 告警;全量测试通过。
