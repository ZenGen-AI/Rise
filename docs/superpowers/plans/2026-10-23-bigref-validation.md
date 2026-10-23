# BigRef 构造即校验 Implementation Plan

**Goal:** 让任何 `BigRef` 实例都满足 I8(`sha256` 是 64 位小写十六进制,可以直接拼进路径与作业参数),并保证树里的坏引用在每个读取点都变成调用方已有的确定性失败,不打崩守护进程。

**Architecture:** 校验放在 `BigRef.__post_init__`,类冻结;规则常量 `SHA256_HEX` 只有一份,四个使用方改为引用它。六个读取点各自把 `InvalidBigRefError` 转成调用方已经按确定性失败处理的异常。

**Tech Stack:** Python 3.11+、pytest。无新增依赖。

**Spec:** `docs/design/10-controlplane.md` 的实体表 `BigRef` 行、I8、CP-16、第 8 章「产物里的 `BigRef` 不合规」与读取点映射表;`docs/design/30-mindtrainer.md` 配置校验要点的「引用」行。

## Global Constraints

- 每个源文件首条语句是 `from __future__ import annotations`。
- `InvalidBigRefError` 继承 `TypeError`,定义在 `controlplane/envelope.py`。
- 读取点的转换只换异常类型、补引用名,不改调用方原有的失败落点。
- 每个任务先写失败的测试,确认它失败,再实现。
- 测试命令一律 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest <path> -q`。

---

### Task 1: `BigRef` 构造即校验并冻结

**Files:**
- Modify: `src/controlplane/envelope.py`
- Test: `tests/test_envelope.py`

```python
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class InvalidBigRefError(TypeError):
    """BigRef 的字段不合规。继承 TypeError:各读树点已把 TypeError 当成确定性读不出。"""


@dataclass(frozen=True)
class BigRef:
    ...

    def __post_init__(self) -> None:
        # uri 非空字符串;sha256 匹配 SHA256_HEX;size_bytes 是 >= 0 的 int 且不是 bool;
        # fetch_hint 为 None 或 dict。违反任一条抛 InvalidBigRefError,信息点名字段、不回显超长值。
        ...
```

- [ ] 测试:合规引用往返不变;每个字段各一组违规值都抛 `InvalidBigRefError`,且它是 `TypeError`;`dataclasses.replace` 换成坏值同样抛;赋值字段抛 `FrozenInstanceError`;`from_dict` 读到坏值同样抛。
- [ ] `tests/test_envelope.py` 里 `sha256="abc"` 的夹具换成合规值。

### Task 2: 规则常量收成一份,训练配置点名引用

**Files:**
- Modify: `src/mindtrainer/training_config.py`、`src/mindtrainer/roll_converter.py`、`src/mindtrainer/executor.py`、`src/recipestore/validate.py`
- Test: `tests/test_mt_config.py`、`tests/test_mt_roll_converter.py`

- [ ] 四处 `_SHA256 = re.compile(...)` 改为引用 `SHA256_HEX`。
- [ ] `_ref_from_dict(name, value)` 接住 `InvalidBigRefError`,以 `InvalidBigRefError(f"{name}: {exc}")` 重抛,`load_config_from_directive` 照旧把它包成 `InvalidTrainingConfigError`。
- [ ] 删掉 `_check_ref` 与校验函数里已不可达的 sha256、size_bytes、fetch_hint 检查,保留 `isinstance(ref, BigRef)`。
- [ ] `test_mt_config.py` 里构造坏 `BigRef` 的四条用例改为走指令字典:坏 `sha256` 经 `load_config_from_directive` 得 `InvalidTrainingConfigError`,信息含引用名。
- [ ] 转换器的两条用例(空 `sha256`、`../../..` 穿越)改为断言坏值在构造 `BigRef` 时就被拒;转换器自己的检查保留,作纵深防御。

### Task 3: 六个读取点的转换

**Files:**
- Modify: `src/controlplane/models.py`、`src/trainscheduler/assemble.py`、`src/mindagent/rounds.py`、`src/mindtrainer/store_adapter.py`、`src/mindtrainer/dispatch.py`、`src/factory/sombench_runner.py`
- Test: 各自的测试文件,每个读取点一条

- [ ] `latest_promoted_checkpoint`:`InvalidBigRefError` 转 `MissingCheckpointRefError`,含义扩为「缺失或不可用」。
- [ ] `assemble._strategy_experience`:`MissingCheckpointRefError` 转 `ContextAssemblyError`(开关打开时,基座缺 `checkpoint_ref` 原先会打崩规划处理器)。
- [ ] `mindagent.rounds._read_model`:转 `AgentRoundFailedError`,`point="mindagent.rounds.model"`。
- [ ] `read_training_run`:转 `NeedsHumanError`;派发器 `except Exception` 分支里重读记录处,`NeedsHumanError` 转 `DirectiveFailedError`。
- [ ] `sombench_runner`:转 `CandidateUnusableError`。

### Task 4: 文档标记、检查与全量

- [ ] 把 `10-controlplane.md`、`30-mindtrainer.md` 里本计划对应的 [计划中] 改为 [已落地]。
- [ ] `check_docs`、`check_consistency` 都是 0 错误 0 告警;全量测试通过。
