# 远端计算层 `compute` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增顶层包 `compute`:在一台共享的远端 GPU 主机上"安全地跑一个作业"。包含不依赖训练与评测的客户端、只依赖标准库的远端助手 `job_tool.py`(启动、状态、日志、停止、原子写文件、目录哈希、GPU 租约、资源探测)、SSH 传输、GPU 预检,以及 `factory/compute_config.py`(读树内 `endpoints.yaml` 与本机主机映射,产出 `ComputeEndpoint` 和 `ComputeClient`)。新增 `controlplane/deferred.py` 的 `DeferredError` 基类和 `controlplane.paths.compute_endpoints_file()`。全部测试脱离真实主机:助手脚本在本机真跑(经 `/bin/sh -c`,与 ssh 同样的一次 shell 解析),SSH 与客户端用假传输。

**Architecture:** `compute` 只导入标准库与 `controlplane.deferred`,不认识训练、评测、git 树;树内配置的读取与校验在 `factory`(边界测试禁止任何包导入 `factory`,而 `read_tree_config` 在 `factory.configio`)。模块:`errors`(异常)、`endpoint`(`ComputeEndpoint`、`launch_prefix` 白名单)、`transport`(`SshTransport`、`RunResult`、`Runner`)、`job_tool`(远端助手,单文件)、`helper`(助手字节、写死的 sha256、引导脚本)、`client`(`ComputeClient`:调助手、翻译应答、`run_job`、`preflight`)。依赖方向:`factory → compute → controlplane.deferred`;`mindtrainer -> compute` 这条边由 Task 7、8 建立(设计 §11 第 5 步,执行器与转换器按设计 §5 住在 `mindtrainer`);`diagnostic` 留给评测那一步(第 6 步),本计划不动它。

**Tech Stack:** Python 3.11+ / uv / pytest / pyyaml(已有);**无新增第三方依赖**。远端助手只用标准库,语法写到 Python 3.8。

**Spec:** `docs/superpowers/specs/2026-10-15-real-backends-design.md` §3(本计划的范围)、§2 的 D1/D8/D9/D10/D11、§6.1 的哈希口径(`hash-tree` 的两种模式)、§7.3 的异常表。设计 §11 第 3 步;第 1 步(编排器阶段路径)与本计划的 Task 1-4 互不依赖;第 2 步(延后通道)的守护进程一侧并入本计划的 Task 6,编排器一侧的 `preflight` 钩子在 Task 7。

## 已核实的事实(实现者不要再猜)

撰写时在 `5fa42b7` 上逐条核对:

| 事实 | 依据 |
|---|---|
| `src/compute/`、`src/controlplane/deferred.py` 都不存在;全量收集 **2642** 个测试 | `ls`、`pytest --collect-only` |
| `write_config` 只接受 `control-plane/config/` 下的 `.yaml`,所以 `control-plane/compute/endpoints.yaml` 不能用它写;`read_tree_config` 读 `compute/` 下的 config 信封没问题,文件不在抛 `FileNotFoundError` | 实测,`config.py:199-215` |
| `SUBDIRS` 已含 `compute`;`GitStore.write_product` 自己建父目录 | `paths.py:5`、`paths.py:147` 的注释 |
| 两个 `_DETERMINISTIC` 都在 `mindtrainer`(`orchestrator.py:36`、`dispatch.py:36`),不含任何 `DeferredError` 子类 | 读源码 |
| `ssh` 别名以 `-` 开头且不加 `--` 时被当成选项(`-oProxyCommand=evil` 被解析成 `proxycommand evil`);加 `--` 后同样的串被当作主机名而拒绝 | `ssh -G` 实测 |
| `shlex.join` 拼出的命令经一次 `/bin/sh -c` 解析后,含空格、引号、`$()`、反引号、分号、换行、空串的参数逐字还原,不产生副作用 | 实测 |
| 包装进程用"空操作"处理器接 SIGTERM(不是 `SIG_IGN`,后者会被 exec 继承),`killpg` 只杀子进程,包装进程幸存并能写 `exit_code`(子进程退出码为 -15) | 实测 |
| `ps -o lstart=` 的输出随区域设置变化(实测输出了中文"四 10月/15 ..."),所以必须带 `LC_ALL=C`;macOS 没有 `/proc` | 实测 |
| `factory.errors.FactoryConfigError`、`factory.configio.key_names`、`read_tree_config` 的签名与行为 | 读源码 |
| `tests/` 没有 `conftest.py`;共用夹具放在 `*_fixtures.py`,测试里 `from xxx_fixtures import ...`(`pythonpath = ["src"]`,pytest prepend 模式) | `tests/factory_fixtures.py` |

## Rulings

设计没规定或与现状冲突的细节,在这里裁决。每条给出判错的代价。

1. **助手的命令行上只有命令名。** 路径、作业 id、密钥、哈希都走 stdin 的一个 JSON 对象,所以远端 shell 没有任何可被解释的数据,`launch_prefix` 之后的 argv 恒为 `[python, <助手路径>, <命令>]`。判错的代价:无。
2. **助手比设计 §3.2 多三个命令:`lease-acquire`、`lease-release`、`lease-peek`,以及 `probe`。** 租约的 `O_EXCL` 创建与"rename 回收"必须在远端原子执行,预检要读内存与磁盘;设计说"所有远端动作都经它",所以放进助手。判错的代价:助手的 sha256 固定了这些命令,日后增减都要改哈希并过评审。
3. **作业状态多一个 `lost`**:包装进程与被包装进程都不在、也没有 `exit_code`(主机重启、被外部 SIGKILL)。设计只列了 `running`、`exited`、`missing`;不加的话这种作业会永远显示 `running`,租约永不陈旧。客户端把 `lost` 当失败。
4. **密钥文件在被包装进程拉起后立即删除**,不等作业结束。比设计 §3.2 的"作业结束或 stop 时删除"更紧,可观察的终态相同(结束后文件不在)。续跑是新的 `start`,调用方会再传一次密钥。判错的代价:无。
5. **包装进程之外再记 `child.json`**(被包装进程的 pid 与启动标记)。包装进程被外部杀掉时孤儿子进程会继续占着 GPU;只看包装进程会把它报成 `lost`、租约变陈旧、别人拿到 GPU 与孤儿相撞。有 `child.json` 时两者任一存活都算 `running`,`stop` 对进程组发信号。判错的代价:孤儿仍可能存在于 `child.json` 写入之前的极短窗口。
6. **`sampled` 口径按文件大小切分**:小于 64 MiB 的文件整体哈希,其余取头、中、尾各 1 MiB。设计 §6.1 写的是"json、txt、小于 64 MiB 的 pt"整体哈希、"大张量分片"抽样,是按类型描述的;训练存点里没有 64 MiB 以上的 json 或 txt,两种写法在真实存点上结果相同。**这是对设计措辞的一处收窄,请在实现前确认。** 判错的代价:若出现 64 MiB 以上的非张量文件,它只被抽样。
7. **`preflight()` 第一次调用会把助手上传到 `<work_root>/bin/`**(内容寻址、幂等、只写 `work_root`)。设计 §3.3 说预检"不写远端",这是唯一例外:没有助手就读不了租约和内存。判错的代价:无,但字面上与设计不一致,请知悉。
8. **预检里"本作业的进程除外"靠租约判定,不比对 pid。** 容器内外 pid 不同,宿主机的 `nvidia-smi` 看到的是宿主机 pid。租约当前持有者就是调用方给的 `own_holder` 时跳过"无计算进程"检查,仍检查内存与磁盘。判错的代价:自己的作业已结束、租约尚未释放时,别人的进程占 GPU 也会被放行;`run_job` 在作业结束时释放租约,窗口很小。
9. **错误分类。** `ComputeBusyError`、`TransportError`、`RemoteJobFailedError`、`RemoteJobTimeoutError` 继承 `DeferredError`(延后通道)。请求本身不合法(`bad_path`、`bad_request` 等)、规格冲突、助手哈希不符是确定性的,继承普通 `ComputeError`,不走延后通道。助手崩溃或输出无法解析按 `TransportError`:可能是瞬态的,代价是助手有 bug 时守护进程按退避无限重试同一个调用,日志里的 `reason` 会写明。
10. **`TransportError` 与 `RemoteJobFailedError` 的 `reason` 不含作业日志。** 日志尾巴放在 `RemoteJobFailedError.log_tail` 属性里,由调用方决定是否落盘;守护进程的结构化日志只打 `reason`。
11. **`endpoints.yaml` 没有写入函数。** `write_config` 限定在 `config/` 下(见上表),设计也不让 `install_defaults` 安装它;它由人写、随提交进树。测试用 `Envelope` + `write_product` 造。
12. **`launch_prefix` 必须让 stdin 穿过去**(例如 `["docker", "exec", "-i", "roll"]`)。助手的请求和密钥都走 stdin,`docker exec` 缺 `-i` 时请求会被吞掉,表现为助手收到空请求。**未在真实主机上核实**,归入设计的 V15 一起验证。
13. **租约的"陈旧"判定:** 持有者的作业 `exited` 或 `lost` 即陈旧;持有者的作业目录不存在,或租约内容为空,只有租约文件的修改时间超过 `grace_s`(默认 120 秒)才算陈旧——"申请租约"与"`start` 写出作业目录"之间有一小段合法的空档,不能让别人在这段空档里抢走租约。
14. **(Task 2)远端 Python 版本未知,助手写到 3.8 语法**(`ast.parse(feature_version=(3, 8))` 有测试)。远端实际版本归入 V15 一起验证。
15. **真实存点与导出的 `BigRef.size_bytes` 填 0。** 助手的 `hash-tree` 应答里有 `bytes`,但客户端的 `hash_tree` 只回 sha256;`MockExecutor` 同样填 0(`executor.py:195`),`BigRef` 契约(`10-controlplane.md` 的 `BigRef` 行)不要求非零,下游也没有读它的代码。判错的代价:人从产物里看不出存点大小,要看远端目录。
16. **(Task 6)延后的退避与上限写死在守护进程里,不进配置。** 键是 `(动作种类, 目标 id)`;等待秒数取 `retry_after_seconds` 与默认 30 秒的较大者、封顶 900 秒;**连续**延后同一动作超过 20 次抛 `DeferredExhaustedError`,守护进程带原因退出。设计 §7.3 只说"默认退避 30 秒、上限 15 分钟",没有给连续次数的上限;不设上限的话,一台被别人长期占着的 GPU 会让守护进程永远空转,没人知道。退避表只在内存(设计 §7.3 的"持久性"一行),重启后立即可重试。判错的代价:上限偏小会在长队列前面过早退出,偏大则发现得晚;两者都只是重启一次守护进程,树里没有任何写入。

## Global Constraints

每个任务的要求都隐含包含本节。

- `src/compute/` 与 `src/factory/compute_config.py` 的每个源文件以 `from __future__ import annotations` 开头(模块 docstring 之后的第一条语句也算);`job_tool.py` 同样,Task 1 的 `tests/test_compute_scaffold.py` 与既有的 `tests/test_factory_boundaries.py` 强制检查。
- **依赖方向:** `src/compute/*.py` 只导入标准库、`compute.*`、`controlplane.deferred`;`job_tool.py` 只导入标准库。`controlplane`、`simulator`、`inference_bridge`、`recipestore`、`trainplanner`、`trainscheduler`、`simple_actio`、`mindagent` 不导入 `compute`。`compute` 不导入 `factory`、`mindtrainer`、`diagnostic`。
- **不引入新第三方依赖**;`pyproject.toml` 只改两处:wheel 包列表追加 `src/compute`(Task 1,追加式脚本)、`[tool.pytest.ini_options]` 新增 `markers`(Task 7 Step 5)。
- **凭证绝不进 git 树、命令行或日志。** 密钥只走 stdin;`TransportError` 的 `reason` 只含 stderr 尾巴与退出码,不含请求内容;配置错误只写键名与路径,不回显取值。
- **绝不终止非本任务启动的进程。** `stop` 只对 `proc.json` 记录且启动标记仍匹配的进程组发信号。
- **只改本计划文件表与各任务 Files 小节列出的文件**:`src/mindtrainer` 只动文件表列出的那几个(`roll_executor.py`、`roll_converter.py` 新建,`executor.py`、`orchestrator.py`、`dispatch.py`、`checkpoint.py` 按 Task 5、7、8 的步骤改);不碰 `src/diagnostic`、`schemas/`、`cli/`;`src/controlplane` 只动三处:`deferred.py` 新建(Task 1)、`paths.py` 追加一个函数(Task 1)、`daemon.py` 加延后通道(Task 6)。
- `git add` 逐个文件,**不要用 `git add .`**;**不要碰** `cli/package.json`、`cli/package-lock.json`;**提交信息不加 `Co-Authored-By` 行**。
- **测试命令**一律是 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`(下文简称"全量命令",跑单个文件时在末尾加路径),不得出现警告。pytest 不必带 `PYTHONPATH`,`pyproject.toml` 已设 `pythonpath = ["src"]`;只有 Task 5、Task 8 那两行 `PYTHONPATH=src python3 -` 的即席脚本要自己带。
- **测试数:** 基线 **B = 2642**(`5fa42b7`)。各任务增量见任务末尾的"期望",累计数在 Task 8 末尾给出;以增量为准,若 B 因其他计划先落地而不同,绝对数会变。

## 文件表

| 文件 | 动作 | 职责 |
|---|---|---|
| `src/controlplane/deferred.py` | 新建 | `DeferredError` 基类(若延后通道那份计划已落地,只核对签名) |
| `src/controlplane/paths.py` | 改 | 追加 `compute_endpoints_file()` |
| `pyproject.toml` | 改 | Task 1:wheel 包列表追加 `src/compute`;Task 7:`[tool.pytest.ini_options]` 新增 `markers` |
| `src/compute/{__init__,errors,endpoint,transport,helper,client}.py` | 新建 | 见 Architecture |
| `src/compute/job_tool.py` | 新建 | 远端助手,只用标准库,3.8 语法 |
| `src/factory/compute_config.py` | 新建 | Task 3 读树内 `endpoints.yaml` 出 `ComputeEndpoint` 列表;Task 4 读本机主机映射出 `ComputeClient` |
| `tests/compute_fixtures.py` | 新建 | Task 2 的 `LocalSsh`(ssh 调用在本机用 `/bin/sh -c` 真跑);Task 7、8 的 `FakeClient` 与 `fake_endpoint`(内存替身,签名对齐 `ComputeClient`) |
| `src/mindtrainer/roll_executor.py` | 新建 | Task 7:`RollExecutor`,ROLL 整段 SFT 作业(设计 §5) |
| `src/mindtrainer/roll_converter.py` | 新建 | Task 8:`RollLoraConverter`,适配器导出(设计 §6.2) |
| `src/mindtrainer/{executor,orchestrator,dispatch,checkpoint}.py` | 改 | Task 5、7、8:阶段协议、预检与跨模式检查、协议 docstring;`dispatch.py` 另在 Task 6 加 `except DeferredError`;两处 `_DETERMINISTIC` 不改 |
| `tests/test_compute_*.py`、`tests/test_factory_compute_config.py` | 新建 | Task 1-4 的测试 |
| `tests/test_mt_roll_executor.py`、`tests/test_mt_roll_converter.py` | 新建 | Task 7、8 的单测 |
| `tests/test_mt_orchestrator.py` | 改 | Task 5、7、8 的集成测试 |
| `tests/test_factory_cross_plan.py` | 改 | Task 1:`NEW_PACKAGES` 加 `compute` |
| `tests/test_daemon_deferred.py` | 新建 | Task 6:守护进程延后通道的测试 |
| `tests/test_mt_dispatch.py` | 改 | Task 6:追加两条延后异常用例 |
| `src/controlplane/daemon.py` | 改 | Task 6:`DeferralBook`、`DeferredExhaustedError`、`reconcile_once` 接住 `DeferredError`、`run_daemon` 持有退避表 |

**代码块约定:** 围栏信息串里的 `path=` 是整文件创建;`insert-before=<path>` 表示把代码块插到该文件的 `# --- entry ---` 行之前。

## Task 1: 脚手架(延后基类、异常、路径、包注册)

**Files:** 新建 `src/controlplane/deferred.py`、`src/compute/__init__.py`、`src/compute/errors.py`、`tests/test_compute_scaffold.py`;改 `src/controlplane/paths.py`、`pyproject.toml`、`tests/test_factory_cross_plan.py`(`NEW_PACKAGES` 加 `compute`)。

- [ ] **Step 1: 写失败的测试**

**Create `tests/test_compute_scaffold.py`:**

```python path=tests/test_compute_scaffold.py
"""compute 脚手架:延后通道基类、异常层次、端点文件路径。"""
from __future__ import annotations

import pytest

from compute.errors import (ComputeBusyError, ComputeError, RemoteJobFailedError,
                            RemoteJobTimeoutError, TransportError)
from controlplane.deferred import DeferredError
from controlplane.paths import compute_endpoints_file

DEFERRED = [ComputeBusyError, TransportError, RemoteJobFailedError, RemoteJobTimeoutError]


def test_deferred_error_carries_reason_and_retry_hint():
    exc = DeferredError("gpu busy", retry_after_seconds=90)
    assert exc.reason == "gpu busy" and exc.retry_after_seconds == 90 and str(exc) == "gpu busy"
    assert DeferredError("x").retry_after_seconds is None


@pytest.mark.parametrize("cls", DEFERRED)
def test_retryable_errors_ride_the_deferred_channel(cls):
    exc = cls("why")
    assert isinstance(exc, DeferredError) and not isinstance(exc, ComputeError)


def test_deterministic_error_is_not_deferred():
    assert not issubclass(ComputeError, DeferredError)


def test_remote_job_failed_error_keeps_the_log_tail_out_of_the_reason():
    exc = RemoteJobFailedError("job j1 exited with code 3", exit_code=3, log_tail="boom")
    assert exc.exit_code == 3 and exc.log_tail == "boom"
    assert "boom" not in exc.reason and "boom" not in str(exc)


@pytest.mark.parametrize("module", ["mindtrainer.dispatch", "mindtrainer.orchestrator"])
def test_deferred_errors_are_not_in_the_deterministic_sets(module):
    import importlib
    det = tuple(importlib.import_module(module)._DETERMINISTIC)
    assert not any(issubclass(cls, det) for cls in DEFERRED)


def test_endpoints_file_sits_beside_the_other_control_plane_products():
    assert compute_endpoints_file() == "control-plane/compute/endpoints.yaml"
```

- [ ] **Step 2: 跑,确认失败**

全量命令 + `tests/test_compute_scaffold.py`。期望:收集阶段 `ModuleNotFoundError: No module named 'compute'`。

- [ ] **Step 3: 实现**

**Create `src/controlplane/deferred.py`:**

```python path=src/controlplane/deferred.py
"""延后通道的异常基类(真实后端设计 §7.3)。

没有任何依赖,所以 compute、mindtrainer、diagnostic 都能从这里导入而不互相牵连。
守护进程接住它之后不写树、状态不动、按退避重试。
"""
from __future__ import annotations


class DeferredError(Exception):
    def __init__(self, reason: str, retry_after_seconds: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
```

**Create `src/compute/__init__.py`:**

```python path=src/compute/__init__.py
"""远端计算层:在共享的远端 GPU 主机上安全地跑一个作业。不认识训练、评测与 git 树。"""
```

**Create `src/compute/errors.py`:**

```python path=src/compute/errors.py
"""计算层异常。确定性错误继承 ComputeError;可重试的继承 DeferredError(Ruling 9)。"""
from __future__ import annotations

from controlplane.deferred import DeferredError


class ComputeError(Exception):
    """确定性错误:请求不合法、规格冲突、助手哈希不符。重试没有用,不走延后通道。"""


class HelperError(ComputeError):
    """远端助手返回了 ok=false 的确定性错误。code 是助手的错误码,调用方按它分支。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class ComputeBusyError(DeferredError):
    """GPU 被别人占着,或租约申请失败。不是训练失败,不耗续训预算。"""


class TransportError(DeferredError):
    """SSH 或远端助手出错。reason 只含退出码与 stderr 尾巴,不含请求内容。"""


class RemoteJobFailedError(DeferredError):
    """作业非零退出或丢失。日志尾巴放在 log_tail,不进 reason(Ruling 10)。"""

    def __init__(self, reason: str, *, exit_code: int | None = None, log_tail: str = "",
                 retry_after_seconds: int | None = None) -> None:
        super().__init__(reason, retry_after_seconds=retry_after_seconds)
        self.exit_code = exit_code
        self.log_tail = log_tail


class RemoteJobTimeoutError(DeferredError):
    """作业超过 job_timeout_seconds,已先 stop。"""
```

**Modify `src/controlplane/paths.py`:** 在 `backends_config_file()` 之后追加:

```python
def compute_endpoints_file() -> str:
    return f"{CONTROL_PLANE_ROOT}/compute/endpoints.yaml"
```

**Modify `pyproject.toml`**(追加式,幂等,不覆盖别的计划加进列表的包):

```bash
python3 - <<'PY'
import pathlib, re
p = pathlib.Path("pyproject.toml"); s = p.read_text()
if '"src/compute"' not in s:
    s, n = re.subn(r'(packages = \[[^\]]*?)\s*\]', r'\1, "src/compute"]', s, count=1)
    assert n == 1, "packages 列表没找到"
    p.write_text(s)
PY
```

- [ ] **Step 4: `NEW_PACKAGES` 加上 `compute`**

`tests/test_factory_cross_plan.py:116` 的 `NEW_PACKAGES` 是唯一同时看得见所有新包的地方,
`test_controlplane_imports_none_of_the_new_packages` 用它守住"`controlplane` 不导入新包"。
`compute` 不在里面,所以 Global Constraints 的依赖方向这一条现在没有测试守着:

```python
NEW_PACKAGES = ("inference_bridge", "recipestore", "trainplanner", "trainscheduler", "simple_actio", "mindagent", "factory",
                "compute")
```

注意方向:禁的是 `controlplane` 导入 `compute`。反向的 `compute → controlplane.deferred`
是本计划的设计(`deferred.py` 无依赖),这条检查看不到它,也不该看到。

**已在隔离拷贝里验证:** 加上 `compute` 后该用例通过;在 `controlplane/paths.py` 里插一行
`import compute` 后它变红。用例数不变(这是改一个常量,不是加用例)。

- [ ] **Step 5: 跑通**

全量命令 + `tests/test_compute_scaffold.py`。期望 **10 passed**。再跑
`tests/test_factory_cross_plan.py`,期望既有用例全部通过(用例数不变)。

- [ ] **Step 6: Commit**

```bash
git add src/controlplane/deferred.py src/controlplane/paths.py src/compute/__init__.py src/compute/errors.py pyproject.toml tests/test_compute_scaffold.py tests/test_factory_cross_plan.py
git commit -m "feat(compute): DeferredError 基类、计算层异常与端点文件路径"
```

## Task 2: 远端助手与客户端(设计 §3.2、§3.3;Ruling 2-14)

**Files:** 新建 `src/compute/job_tool.py`、`src/compute/{endpoint,transport,helper,client}.py`、`tests/test_compute_assistant.py`;改 `src/compute/errors.py` 加 `HelperError`;选填 `src/compute/__init__.py`。

全都是纯本地数据类、协议与逻辑,不读配置、不碰 git 树,所以测试不用真 ssh,只在本机冒烟:
- `LocalSsh`:实现 `Runner` 协议;把"ssh <别名> <cmd>"形态的命令行转为 `/bin/sh -c '<cmd>'` 真跑。
- 远端助手测试:本机冒烟,所有命令都调一遍(start/status/tail/stop、put/hash-tree、租约三件套、probe)。
- 传输与引导:把助手送上副本 work_root、故意损坏文件再 `ensure_helper`,触发重送。
- 客户端:与远端助手配合,覆盖 run_job 成功、非零退出、超时 stop、租约忙、preflight 四种资源阻塞场景、幂等重启。

Ruling 数字顺承设计文档:
- **Ruling 2**: 端点描述不含主机定位:SSH 别名单独给,所以端点可以当内容寻址的冻结值放进 git 树。
- **Ruling 3**: 作业状态四态(missing/running/exited/lost)。
- **Ruling 4**: 助手包装进程读密钥给子进程启动,立即删除 secret 文件,不泄露给日志与外部探测。
- **Ruling 5**: spec 规格哈希不含 secret 本身,所以幂等重启时同规格的请求不会因密钥刷新而被拒作冲突。
- **Ruling 6**: hash-tree 小文件全文,≥64 MiB 文件采样(头、中、尾各 1 MiB);参数模型二十几 GB,散列仍秒出。
- **Ruling 7**: `preflight()` 不写远端,除了首次调用把助手送上去;不取租约、不终止任何进程。
- **Ruling 8**: `preflight()` 的租约持有者就是 `own_holder` 时,跳过"无计算进程"检查,因为进程就是本作业起的。
- **Ruling 9**: 确定性错误(请求不合法、租约规格冲突、助手哈希不符)继承 `ComputeError`,不走延后通道。可重试错误(GPU 忙、传输失败、作业失败、作业超时)继承 `DeferredError`,走延后通道且不耗续训预算。
- **Ruling 10**: 作业日志尾巴单独放在 `RemoteJobFailedError.log_tail`,不进 `.reason`,因为训练脚本的正常输出会包含请求数据(样本)与权重数字,不能让它们污染 step_metadata.error_reason。
- **Ruling 11**: `launch_prefix` 可注入任意命令,但白名单只放 `docker exec` 与 `podman exec`:改动要改代码并过评审。不允许 `run`,因为它每次起新容器,破坏"所有调用在同一个 PID 命名空间"的前提。
- **Ruling 12**: 进程启动标记(Linux 用 `/proc/<pid>/stat` 的启动时刻,其余平台用 `ps` 输出)防 PID 复用;kill 信号只发给标记仍匹配的进程。
- **Ruling 13**: 租约持有者的作业已 `exited` 或 `lost` 算陈旧;作业目录不存在或租约内容为空时,只在超过宽限期后才算陈旧,防抢占正在启动的作业。

- [ ] **Step 1: 写远端助手 `src/compute/job_tool.py`**

语法必须能在 Python 3.8 解析(不用 match、不用 `X | Y` 签名、不用 `dict[...]` 泛型)。完整内容按节给出,依次拼成一个文件;`compute/helper.py` 里的 `HELPER_SHA256` 必须等于这个文件的 sha256,Step 5 的测试会拦住不符。

```python path=src/compute/job_tool.py
"""远端助手(真实后端设计 §3.2、D10):在共享 GPU 主机上安全地跑一个作业。

只依赖标准库,语法写到 Python 3.8;sha256 写死在 compute/helper.py,改这个文件必须同步改那里。

调用:python3 job_tool.py <命令>。命令行上只有命令名;请求是 stdin 里的一个 JSON 对象,应答是 stdout
里的一行 JSON:{"ok": true, ...} 或 {"ok": false, "error": <码>, "detail": <说明>}。错误说明只写字段名,
不回显请求里的取值(密钥会经过这里)。

远端只写 work_root。作业目录 <work_root>/jobs/<job_id>/ 里:
  spec.json   启动规格(不含密钥)         secret      密钥,0600,包装进程读入后立即删除
  proc.json   包装进程的 pid 与启动标记     child.json  被包装进程的 pid 与启动标记
  stdout.log  合并后的输出                exit_code   被包装进程的退出码(被信号杀死时为负数)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

# ===== core =====

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_PUT_BYTES = 16 * 1024 * 1024
SAMPLE_THRESHOLD = 64 * 1024 * 1024
SAMPLE_CHUNK = 1024 * 1024
START_WAIT_S = 5.0
STARTING_GRACE_S = 15.0
DEFAULT_LEASE_GRACE_S = 120.0


COMMANDS = {}


def command(name):
    def register(fn):
        COMMANDS[name] = fn
        return fn
    return register


class Failure(Exception):
    def __init__(self, code, detail=""):
        Exception.__init__(self, code)
        self.code = code
        self.detail = detail


# ---------- 请求字段 ----------

def _str(req, key):
    value = req.get(key)
    if not isinstance(value, str) or not value:
        raise Failure("bad_request", "%s must be a non-empty string" % key)
    return value


def _number(req, key, default, low, high):
    value = req.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not (low <= value <= high):
        raise Failure("bad_request", "%s must be a number in [%s, %s]" % (key, low, high))
    return value


def _work_root(req):
    root = _str(req, "work_root")
    if not os.path.isabs(root) or ".." in root.split("/") or os.path.normpath(root) == "/":
        raise Failure("bad_request", "work_root must be an absolute path below /")
    return os.path.normpath(root)


def _job_id(req, key="job_id"):
    value = _str(req, key)
    if not JOB_ID_RE.match(value):
        raise Failure("bad_request", "%s has an invalid form" % key)
    return value


def _job_dir(root, job_id):
    return os.path.join(root, "jobs", job_id)


def _abs_path_inside(root, req, key="path"):
    path = _str(req, key)
    if not os.path.isabs(path) or ".." in path.split("/"):
        raise Failure("bad_path", "%s must be an absolute path without '..'" % key)
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    if real != real_root and not real.startswith(real_root + os.sep):
        raise Failure("bad_path", "%s is outside work_root" % key)
    return path


# ---------- 文件 ----------

def _atomic_write(path, data, mode=0o644, sync=False):
    if isinstance(data, str):
        data = data.encode("utf-8")
    tmp = "%s.tmp-%d-%s" % (path, os.getpid(), uuid.uuid4().hex[:8])
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if sync:
                os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        _unlink(tmp)
        raise


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except (OSError, ValueError):
        return None


def _read_json(path):
    text = _read_text(path)
    if text is None:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _age(path):
    try:
        return time.time() - os.stat(path).st_mtime
    except OSError:
        return 0.0


def _wait_for(predicate, timeout, step=0.05):
    deadline = time.time() + timeout
    while True:
        value = predicate()
        if value or time.time() >= deadline:
            return value
        time.sleep(step)


# ---------- 进程身份(防 pid 复用) ----------

def _parse_proc_stat(text):
    """/proc/<pid>/stat → (状态, 启动时刻)。comm 字段可含空格与括号,所以从最后一个 ')' 之后切。"""
    rest = text[text.rindex(")") + 1:].split()
    return rest[0], rest[19]


def _proc_marker(pid):
    """进程的启动标记;进程不存在或已是僵尸时返回 None。Linux 读 /proc,其余平台(开发机)用 ps。"""
    if os.path.exists("/proc/self/stat"):
        try:
            with open("/proc/%d/stat" % pid) as handle:
                state, start = _parse_proc_stat(handle.read())
        except (OSError, ValueError, IndexError):
            return None
        return None if state in ("Z", "X") else "p" + start
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    try:
        out = subprocess.run(["ps", "-o", "stat=,lstart=", "-p", str(pid)], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=env, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    parts = out.stdout.decode("ascii", "replace").split(None, 1)
    if out.returncode != 0 or len(parts) < 2 or parts[0].startswith("Z"):
        return None
    return "s" + " ".join(parts[1].split())


def _record_alive(record):
    if not record or not isinstance(record.get("pid"), int) or not record.get("start"):
        return False
    return _proc_marker(record["pid"]) == record["start"]


def _exit_code(jobdir):
    text = _read_text(os.path.join(jobdir, "exit_code"))
    try:
        return int(text.strip())
    except (AttributeError, ValueError):
        return None


def _job_state(jobdir):
    """→ (state, exit_code);state 是 missing、running、exited、lost 之一(Ruling 3)。"""
    if not os.path.isdir(jobdir):
        return "missing", None
    code = _exit_code(jobdir)
    if code is not None:
        return "exited", code
    wrapper = _read_json(os.path.join(jobdir, "proc.json"))
    child = _read_json(os.path.join(jobdir, "child.json"))
    if _record_alive(wrapper) or _record_alive(child):
        return "running", None
    code = _exit_code(jobdir)  # 两个进程刚好在两次检查之间收尾
    if code is not None:
        return "exited", code
    if wrapper is None and _age(jobdir) < STARTING_GRACE_S:
        return "running", None
    return "lost", None


def _spec_digest(spec):
    blob = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _status_result(jobdir):
    state, code = _job_state(jobdir)
    spec = _read_json(os.path.join(jobdir, "spec.json"))
    return {"state": state, "exit_code": code, "spec_sha256": _spec_digest(spec) if spec else None}

# ===== jobs =====

SELF = os.path.abspath(__file__)
WRAPPER_ENV = "RISE_JOBDIR"


def _ignore_signal(signum, frame):
    # 不用 SIG_IGN:被忽略的信号会被 exec 出的子进程继承,子进程就杀不死了。函数处理器在 exec 时复位为默认。
    pass


def _parse_spec(spec):
    if not isinstance(spec, dict):
        raise Failure("bad_request", "spec must be an object")
    extra = sorted(set(spec) - {"argv", "cwd", "env", "secret_env"})
    if extra:
        raise Failure("bad_request", "spec has unknown keys: " + ",".join(str(k) for k in extra))
    argv = spec.get("argv")
    if (not isinstance(argv, list) or not argv or not all(isinstance(a, str) and "\0" not in a for a in argv)
            or not argv[0]):
        raise Failure("bad_request", "spec.argv must be a non-empty list of strings")
    cwd = spec.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not os.path.isabs(cwd) or "\0" in cwd):
        raise Failure("bad_request", "spec.cwd must be an absolute path")
    env = spec.get("env", {})
    if not isinstance(env, dict) or not all(
            isinstance(k, str) and ENV_NAME_RE.match(k) and isinstance(v, str) and "\0" not in v
            for k, v in env.items()):
        raise Failure("bad_request", "spec.env must map variable names to strings")
    secret_env = spec.get("secret_env")
    if secret_env is not None and (not isinstance(secret_env, str) or not ENV_NAME_RE.match(secret_env)
                                   or secret_env in env):
        raise Failure("bad_request", "spec.secret_env must be a variable name not present in spec.env")
    return {"argv": argv, "cwd": cwd, "env": env, "secret_env": secret_env}


def _write_exit(jobdir, code):
    _atomic_write(os.path.join(jobdir, "exit_code"), str(code), sync=True)


def _note(jobdir, text):
    with open(os.path.join(jobdir, "stdout.log"), "ab") as log:
        log.write((text + "\n").encode("utf-8"))


def _run_wrapped(jobdir):
    spec = _read_json(os.path.join(jobdir, "spec.json"))
    me = os.getpid()
    _atomic_write(os.path.join(jobdir, "proc.json"), json.dumps({"pid": me, "start": _proc_marker(me)}))
    stop_flag = os.path.join(jobdir, "stop_requested")
    secret_path = os.path.join(jobdir, "secret")
    if spec is None:
        _note(jobdir, "spec.json is unreadable")
        return _write_exit(jobdir, 126)
    if os.path.exists(stop_flag):
        _unlink(secret_path)
        return _write_exit(jobdir, -signal.SIGTERM)
    env = dict(os.environ)
    env.update(spec["env"])
    if spec["secret_env"]:
        secret = _read_text(secret_path)
        if secret is None:
            _note(jobdir, "the secret file is missing")
            return _write_exit(jobdir, 125)
        env[spec["secret_env"]] = secret
    with open(os.path.join(jobdir, "stdout.log"), "ab") as log:
        try:
            child = subprocess.Popen(spec["argv"], cwd=spec["cwd"], env=env, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, close_fds=True)
        except OSError as exc:
            log.write(("cannot launch the job: %s\n" % type(exc).__name__).encode("utf-8"))
            _unlink(secret_path)
            return _write_exit(jobdir, 127)
    _unlink(secret_path)  # 密钥只活到子进程拉起(Ruling 4)
    marker = _proc_marker(child.pid)
    if marker:
        _atomic_write(os.path.join(jobdir, "child.json"), json.dumps({"pid": child.pid, "start": marker}))
    if os.path.exists(stop_flag):
        child.terminate()
    _write_exit(jobdir, child.wait())


def _wrapper_main():
    signal.signal(signal.SIGTERM, _ignore_signal)
    signal.signal(signal.SIGHUP, _ignore_signal)
    jobdir = os.environ.pop(WRAPPER_ENV, "")
    if not jobdir:
        return 2
    try:
        _run_wrapped(jobdir)
    except BaseException as exc:  # 包装进程自己出错:留一行类名,写一个退出码,别让作业永远显示 running
        try:
            _note(jobdir, "wrapper error: %s" % type(exc).__name__)
            if _exit_code(jobdir) is None:
                _write_exit(jobdir, 126)
        except OSError:
            pass
    return 0


@command("start")
def cmd_start(req):
    root = _work_root(req)
    job_id = _job_id(req)
    spec = _parse_spec(req.get("spec"))
    secret = req.get("secret")
    if secret is not None and (not isinstance(secret, str) or not secret or "\0" in secret):
        raise Failure("bad_request", "secret must be a non-empty string")
    if secret is not None and not spec["secret_env"]:
        raise Failure("bad_request", "secret requires spec.secret_env")
    jobdir = _job_dir(root, job_id)
    digest = _spec_digest(spec)
    os.makedirs(os.path.join(root, "jobs"), exist_ok=True)
    for _ in range(2):
        try:
            os.mkdir(jobdir, 0o700)
        except FileExistsError:
            existing = _wait_for(lambda: _read_json(os.path.join(jobdir, "spec.json")), START_WAIT_S)
            if existing is not None:
                if _spec_digest(existing) != digest:
                    raise Failure("spec_conflict", "the job exists with a different spec")
                return _status_result(jobdir)  # 幂等:同一规格的重复 start 不再拉起第二份
            if _age(jobdir) < STARTING_GRACE_S:
                raise Failure("job_starting", "another start of this job is still in progress")
            shutil.rmtree(jobdir, ignore_errors=True)  # 上一次 start 在写 spec 之前崩了
            continue
        break
    else:
        raise Failure("job_starting", "another start of this job is still in progress")
    if spec["secret_env"] and secret is None:
        shutil.rmtree(jobdir, ignore_errors=True)
        raise Failure("bad_request", "spec.secret_env is set but no secret was given")
    _atomic_write(os.path.join(jobdir, "spec.json"), json.dumps(spec, sort_keys=True))
    if secret is not None:
        _atomic_write(os.path.join(jobdir, "secret"), secret, mode=0o600)
    env = dict(os.environ)
    env[WRAPPER_ENV] = jobdir
    try:
        subprocess.Popen([sys.executable, SELF, "_wrap"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=env, close_fds=True, start_new_session=True)
    except OSError as exc:
        _unlink(os.path.join(jobdir, "secret"))
        raise Failure("launch_failed", type(exc).__name__)
    _wait_for(lambda: os.path.exists(os.path.join(jobdir, "child.json"))
              or os.path.exists(os.path.join(jobdir, "exit_code")), START_WAIT_S)
    return _status_result(jobdir)


@command("status")
def cmd_status(req):
    return _status_result(_job_dir(_work_root(req), _job_id(req)))


@command("tail")
def cmd_tail(req):
    jobdir = _job_dir(_work_root(req), _job_id(req))
    if not os.path.isdir(jobdir):
        raise Failure("not_found", "no such job")
    lines = int(_number(req, "lines", 50, 1, 5000))
    try:
        with open(os.path.join(jobdir, "stdout.log"), "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 1024 * 1024))
            data = handle.read()
    except OSError:
        data = b""
    return {"text": "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:])}


def _signal_job(wrapper, child, sig):
    """只向启动标记仍匹配的进程发信号(防 pid 复用);包装进程活着就发整个进程组。"""
    try:
        if _record_alive(wrapper):
            os.killpg(wrapper["pid"], sig)
            return True
        if _record_alive(child):
            os.kill(child["pid"], sig)
            return True
    except OSError:
        pass
    return False


@command("stop")
def cmd_stop(req):
    root = _work_root(req)
    jobdir = _job_dir(root, _job_id(req))
    grace = _number(req, "grace_s", 10, 0, 600)
    if not os.path.isdir(jobdir):
        return {"state": "missing", "exit_code": None, "spec_sha256": None, "signaled": False}
    signaled = False
    if _job_state(jobdir)[0] == "running":
        _atomic_write(os.path.join(jobdir, "stop_requested"), "1")  # 包装进程还没拉起子进程时,靠它拦住
        wrapper = _read_json(os.path.join(jobdir, "proc.json"))
        child = _read_json(os.path.join(jobdir, "child.json"))
        signaled = _signal_job(wrapper, child, signal.SIGTERM)
        if not _wait_for(lambda: _job_state(jobdir)[0] != "running", grace):
            if _record_alive(child):
                _signal_job(None, child, signal.SIGKILL)
            if not _wait_for(lambda: _job_state(jobdir)[0] != "running", 3.0) and _record_alive(wrapper):
                _signal_job(wrapper, None, signal.SIGKILL)
                _wait_for(lambda: _job_state(jobdir)[0] != "running", 3.0)
    _unlink(os.path.join(jobdir, "secret"))
    result = _status_result(jobdir)
    result["signaled"] = signaled
    return result

# ===== files =====

@command("put")
def cmd_put(req):
    root = _work_root(req)
    path = _abs_path_inside(root, req)
    expected = _str(req, "sha256")
    try:
        data = base64.b64decode(_str(req, "content_b64").encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        raise Failure("bad_request", "content_b64 is not valid base64")
    if len(data) > MAX_PUT_BYTES:
        raise Failure("too_large", "content exceeds %d bytes" % MAX_PUT_BYTES)
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected:
        raise Failure("hash_mismatch", "the content does not match the given sha256")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_write(path, data, mode=0o755 if req.get("executable") is True else 0o644, sync=True)
    return {"size": len(data), "sha256": digest}


def _digest_file(path, size, mode):
    """full:整个文件;sampled:小于 SAMPLE_THRESHOLD 的整体哈希,更大的取头、中、尾各 SAMPLE_CHUNK(Ruling 6)。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        if mode == "full" or size < SAMPLE_THRESHOLD:
            for block in iter(lambda: handle.read(SAMPLE_CHUNK), b""):
                digest.update(block)
        else:
            for offset in (0, (size - SAMPLE_CHUNK) // 2, size - SAMPLE_CHUNK):
                handle.seek(offset)
                digest.update(handle.read(SAMPLE_CHUNK))
    return digest.hexdigest()


@command("hash-tree")
def cmd_hash_tree(req):
    top = _str(req, "path")
    if not os.path.isabs(top) or ".." in top.split("/"):
        raise Failure("bad_path", "path must be an absolute path without '..'")
    mode = req.get("mode")
    if mode not in ("sampled", "full"):
        raise Failure("bad_request", "mode must be 'sampled' or 'full'")
    if not os.path.isdir(top):
        raise Failure("not_found", "no such directory")
    entries = []
    for base, dirs, names in os.walk(top, followlinks=False):
        dirs.sort()
        for name in sorted(names):
            full = os.path.join(base, name)
            rel = os.path.relpath(full, top).replace(os.sep, "/")
            info = os.lstat(full)
            if os.path.islink(full):
                entries.append((rel, "link", hashlib.sha256(os.readlink(full).encode("utf-8", "replace")).hexdigest()))
            elif os.path.isfile(full):
                entries.append((rel, str(info.st_size), _digest_file(full, info.st_size, mode)))
    entries.sort()
    manifest = hashlib.sha256()
    for rel, size, digest in entries:
        manifest.update(("%s\0%s\0%s\n" % (rel, size, digest)).encode("utf-8"))
    total = sum(int(size) for _, size, _ in entries if size != "link")
    return {"sha256": manifest.hexdigest(), "files": len(entries), "bytes": total}

# ===== lease =====

LEASE_NAME = "gpu.lease"


def _lease_path(root):
    return os.path.join(root, LEASE_NAME)


def _holder_stale(root, holder, lease_age, grace):
    """Ruling 13:持有者的作业已 exited 或 lost 即陈旧;作业目录不存在或租约内容为空时,只在超过宽限期后才算陈旧。"""
    if not holder or not JOB_ID_RE.match(holder):
        return lease_age > grace
    jobdir = _job_dir(root, holder)
    if not os.path.isdir(jobdir):
        return lease_age > grace
    return _job_state(jobdir)[0] in ("exited", "lost")


def _take_lease(root):
    """把租约文件 rename 到私有名字再读:别的申请者同时回收时只有一个 rename 成功。返回 (私有路径, 内容) 或 None。"""
    taken = os.path.join(root, "%s.stale-%s" % (LEASE_NAME, uuid.uuid4().hex[:12]))
    try:
        os.rename(_lease_path(root), taken)
    except OSError:
        return None
    return taken, (_read_text(taken) or "").strip()


def _put_back(taken, root):
    """原样放回被误取的租约。目标已被别人重新创建时(极窄的竞态)不覆盖,私有文件留作证据,调用方按忙处理。"""
    try:
        os.link(taken, _lease_path(root))
    except OSError:
        return False
    _unlink(taken)
    return True


def _create_lease(root, holder):
    try:
        fd = os.open(_lease_path(root), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as handle:
        handle.write(holder)
    return True


@command("lease-acquire")
def cmd_lease_acquire(req):
    root = _work_root(req)
    holder = _job_id(req, "holder")
    grace = _number(req, "grace_s", DEFAULT_LEASE_GRACE_S, 0, 86400)
    os.makedirs(root, exist_ok=True)
    if _create_lease(root, holder):
        return {"acquired": True, "holder": holder, "reclaimed": False}
    current = (_read_text(_lease_path(root)) or "").strip()
    if current == holder:
        return {"acquired": True, "holder": holder, "reclaimed": False}
    if not _holder_stale(root, current, _age(_lease_path(root)), grace):
        return {"acquired": False, "holder": current or None, "reclaimed": False}
    taken = _take_lease(root)
    if taken is None:  # 别人先一步回收了
        return {"acquired": False, "holder": None, "reclaimed": False}
    path, seen = taken
    if seen != current:  # 取到的不是判定陈旧的那份:放回,按忙处理
        _put_back(path, root)
        return {"acquired": False, "holder": seen or None, "reclaimed": False}
    _unlink(path)
    if _create_lease(root, holder):
        return {"acquired": True, "holder": holder, "reclaimed": True}
    return {"acquired": False, "holder": None, "reclaimed": True}


@command("lease-release")
def cmd_lease_release(req):
    root = _work_root(req)
    holder = _job_id(req, "holder")
    taken = _take_lease(root)
    if taken is None:
        return {"released": False, "holder": None}
    path, seen = taken
    if seen == holder:
        _unlink(path)
        return {"released": True, "holder": holder}
    _put_back(path, root)
    return {"released": False, "holder": seen or None}


@command("lease-peek")
def cmd_lease_peek(req):
    root = _work_root(req)
    grace = _number(req, "grace_s", DEFAULT_LEASE_GRACE_S, 0, 86400)
    path = _lease_path(root)
    text = _read_text(path)
    if text is None:
        return {"holder": None, "stale": False}
    holder = text.strip()
    return {"holder": holder or None, "stale": _holder_stale(root, holder, _age(path), grace)}


# ===== probe =====

@command("probe")
def cmd_probe(req):
    root = _work_root(req)
    mem = None
    try:
        with open("/proc/meminfo") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    mem = round(int(line.split()[1]) / (1024.0 * 1024.0), 3)  # kB → GiB
    except (OSError, ValueError, IndexError):
        mem = None
    probe_path = root
    while not os.path.exists(probe_path) and probe_path != "/":
        probe_path = os.path.dirname(probe_path)
    return {"mem_available_gib": mem,
            "disk_free_gib": round(shutil.disk_usage(probe_path).free / float(1 << 30), 3)}


# ===== entry =====

MAX_REQUEST_BYTES = 32 * 1024 * 1024


def _emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main(argv):
    if len(argv) == 2 and argv[1] == "_wrap":
        return _wrapper_main()
    if len(argv) != 2 or argv[1] not in COMMANDS:
        _emit({"ok": False, "error": "unknown_command", "detail": "expected one of: " + ", ".join(sorted(COMMANDS))})
        return 0
    try:
        req = json.loads(sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1).decode("utf-8") or "null")
        if not isinstance(req, dict):
            raise Failure("bad_request", "the request must be a JSON object")
        _emit(dict(COMMANDS[argv[1]](req), ok=True))
    except Failure as failure:
        _emit({"ok": False, "error": failure.code, "detail": failure.detail})
    except ValueError:
        _emit({"ok": False, "error": "bad_request", "detail": "the request is not valid JSON"})
    except Exception as exc:  # 请求里可能有密钥:异常信息不回显,只给类名
        _emit({"ok": False, "error": "internal", "detail": type(exc).__name__})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

- [ ] **Step 2: 写失败的测试**

```python path=tests/test_compute_assistant.py
"""远端助手、传输、引导、客户端,本机冒烟。

总耗时约 10s(助手在本机真起子进程、真等其退出)。不加自定义标记:仓库的 pytest 配置没有注册 markers,
全量命令带 -W error,未注册的标记会让收集阶段直接失败。
"""
from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from compute.client import NVIDIA_SMI, ComputeClient, JobSpec
from compute.endpoint import ENDPOINT_ID_PATTERN, LAUNCH_PREFIX_PROGRAMS, ComputeEndpoint, check_launch_prefix
from compute.errors import ComputeBusyError, ComputeError, HelperError, RemoteJobFailedError, RemoteJobTimeoutError
from compute.helper import HELPER_SHA256, helper_bytes
from compute.transport import RunResult, SshTransport

TOOL = Path(__file__).parent.parent / "src" / "compute" / "job_tool.py"
PYTHON = "python3"


# ===== 本机替身 =====

def local_runner(argv: list[str], *, input: bytes, timeout: float) -> RunResult:
    """把 ssh <别名> <cmd> 形态的命令行转为 /bin/sh -c '<cmd>' 真跑,绕过 ssh。"""
    assert argv[0] == "ssh" and len(argv) >= 3
    sep = argv.index("--")
    cmd = argv[sep + 2]  # SshTransport 已用 shlex.join 把远端命令拼成这一个字符串,不能再 join 一次
    proc = subprocess.run(["/bin/sh", "-c", cmd], input=input, capture_output=True,
                          timeout=timeout, check=False)
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


# ===== endpoint =====

def test_endpoint_id_pattern_accepts_common_forms():
    for good in ("a", "gpu1", "dev.local", "prod-us-1", "h_2", "3090-test"):
        assert ENDPOINT_ID_PATTERN.match(good)
    for bad in ("-a", "x y", "ö", "a" * 65, ""):
        assert not ENDPOINT_ID_PATTERN.match(bad)


def test_launch_prefix_empty_or_docker_exec():
    assert check_launch_prefix([]) == ()
    assert check_launch_prefix(["docker", "exec", "cont"]) == ("docker", "exec", "cont")
    assert check_launch_prefix(["podman", "exec", "-it", "x"]) == ("podman", "exec", "-it", "x")
    with pytest.raises(ComputeError, match="empty or start with"):
        check_launch_prefix(["bash", "-c", "ls"])
    with pytest.raises(ComputeError, match=r"exec \.\.\. <container>"):
        check_launch_prefix(["docker", "run", "x"])
    with pytest.raises(ComputeError, match="non-empty strings"):
        check_launch_prefix(["", "exec"])  # type: ignore


def test_launch_prefix_program_whitelist_guards_against_arbitrary_injection():
    assert tuple(LAUNCH_PREFIX_PROGRAMS) == ("docker", "podman")


def test_endpoint_validates_work_root():
    base = {"endpoint_id": "a", "work_root": "/w", "roll_root": "/r", "launch_prefix": (),
            "base_model_path": "/m", "heldout_jsonl": "/h", "sft_template": "t",
            "min_free_host_mem_gib": 1.0, "min_free_disk_gib": 1.0, "poll_seconds": 1.0, "job_timeout_seconds": 60.0}
    ComputeEndpoint(**base)
    with pytest.raises(ComputeError, match="work_root"):
        ComputeEndpoint(**{**base, "work_root": "rel"})  # type: ignore
    with pytest.raises(ComputeError, match="work_root"):
        ComputeEndpoint(**{**base, "work_root": "/a/../etc"})  # type: ignore


# ===== transport =====

def test_ssh_transport_composes_batch_mode_command():
    tr = SshTransport("dev", connect_timeout_s=5.0)
    assert tr.ssh_argv(["ls", "-l"]) == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                         "--", "dev", "ls -l"]


def test_ssh_alias_cannot_start_with_dash_or_contain_quotes():
    SshTransport("valid_host.local")
    with pytest.raises(ComputeError, match="invalid form"):
        SshTransport("-evil")
    with pytest.raises(ComputeError, match="invalid form"):
        SshTransport("a b")


# ===== helper =====

def test_helper_sha256_matches_the_assistant_file():
    assert hashlib.sha256(TOOL.read_bytes()).hexdigest() == HELPER_SHA256


def test_helper_bytes_refuses_to_serve_a_tampered_file(tmp_path: Path):
    import compute.helper as mod
    real = mod._HELPER_FILE
    fake = tmp_path / "job_tool.py"
    fake.write_text("# evil")
    mod._HELPER_FILE = fake
    try:
        with pytest.raises(ComputeError, match="does not match the pinned sha256"):
            helper_bytes()
    finally:
        mod._HELPER_FILE = real


# ===== assistant 本机冒烟 =====

def test_assistant_start_runs_and_injects_secret_then_removes_it(tmp_path: Path):
    root = tmp_path / "work"
    spec = JobSpec(argv=(PYTHON, "-c", "import os;print('K=',os.environ['K']);print('done',flush=True)"),
                   env={"E": "v"}, secret_env="K")
    call = _make_caller(root)
    status = call("start", job_id="j1", spec=spec.to_wire(), secret="s3cr3t")
    assert status["state"] in ("running", "exited")  # 作业很短,start 返回时可能已经结束
    _wait(lambda: call("status", job_id="j1")["state"] != "running", 5.0)
    tail = call("tail", job_id="j1", lines=50)["text"]
    assert "K= s3cr3t" in tail and "done" in tail
    assert not (root / "jobs" / "j1" / "secret").exists()


def test_assistant_start_is_idempotent_with_the_same_spec(tmp_path: Path):
    root = tmp_path / "work"
    spec = {"argv": ["sleep", "5"], "secret_env": "K"}  # 带密钥必须声明 secret_env;sleep 保证第二次调用时还在跑
    call = _make_caller(root)
    call("start", job_id="j2", spec=spec, secret="first")
    status2 = call("start", job_id="j2", spec=spec, secret="rotated")  # 密钥不进规格哈希,不算冲突
    assert status2["state"] == "running"
    call("stop", job_id="j2", grace_s=5.0)


def test_assistant_start_refuses_a_different_spec_for_the_same_job_id(tmp_path: Path):
    root = tmp_path / "work"
    call = _make_caller(root)
    call("start", job_id="j3", spec={"argv": ["sleep", "1"]})
    err = call.raw("start", job_id="j3", spec={"argv": ["true"]})
    assert not err["ok"] and err["error"] == "spec_conflict"


def test_assistant_stop_terminates_then_reports_state(tmp_path: Path):
    root = tmp_path / "work"
    call = _make_caller(root)
    call("start", job_id="j4", spec={"argv": ["sleep", "600"]})
    assert call("status", job_id="j4")["state"] == "running"
    t = time.monotonic()
    result = call("stop", job_id="j4", grace_s=5.0)
    assert time.monotonic() - t < 6.0 and result["state"] == "exited" and result["signaled"]


def test_assistant_put_writes_executable_and_verifies_hash(tmp_path: Path):
    root = tmp_path / "work"
    call = _make_caller(root)
    data = b"#!/bin/sh\necho hi\n"
    call("put", path=str(root / "bin" / "x.sh"), content_b64=base64.b64encode(data).decode(),
         sha256=hashlib.sha256(data).hexdigest(), executable=True)
    written = root / "bin" / "x.sh"
    assert written.read_bytes() == data and (written.stat().st_mode & 0o111)
    err = call.raw("put", path=str(root / "bad"), content_b64=base64.b64encode(data).decode(), sha256="0" * 64)
    assert not err["ok"] and err["error"] == "hash_mismatch"


def test_assistant_put_forbids_path_outside_work_root(tmp_path: Path):
    root = tmp_path / "work"
    call = _make_caller(root)
    err = call.raw("put", path="/tmp/evil", content_b64="", sha256="e" * 64)
    assert not err["ok"] and err["error"] == "bad_path"


def test_assistant_hash_tree_full_and_sampled_are_identical_for_small_trees(tmp_path: Path):
    root = tmp_path / "work"
    d = root / "ckpt"
    d.mkdir(parents=True)
    (d / "a.json").write_text("{}")
    (d / "sub").mkdir()
    (d / "sub" / "w.pt").write_bytes(os.urandom(1000))
    call = _make_caller(root)
    full = call("hash-tree", path=str(d), mode="full")
    sampled = call("hash-tree", path=str(d), mode="sampled")
    assert full["sha256"] == sampled["sha256"] and full["files"] == 2


def test_assistant_hash_tree_changes_when_content_changes(tmp_path: Path):
    root = tmp_path / "work"
    d = root / "ckpt"
    d.mkdir(parents=True)
    (d / "a.json").write_text("{}")
    call = _make_caller(root)
    h1 = call("hash-tree", path=str(d), mode="full")["sha256"]
    (d / "a.json").write_text("{ }")
    h2 = call("hash-tree", path=str(d), mode="full")["sha256"]
    assert h1 != h2


def test_assistant_lease_acquire_release_peek(tmp_path: Path):
    root = tmp_path / "work"
    call = _make_caller(root)
    assert call("lease-peek")["holder"] is None
    assert call("lease-acquire", holder="A")["acquired"]
    assert call("lease-acquire", holder="A")["acquired"]  # 持有者再申请幂等
    assert not call("lease-acquire", holder="B")["acquired"]  # 新鲜,被占
    assert not call("lease-acquire", holder="B", grace_s=3600.0)["acquired"]  # 作业目录不存在,但仍在宽限期内
    time.sleep(0.1)
    acq = call("lease-acquire", holder="B", grace_s=0.05)  # 超宽限期,陈旧回收
    assert acq["acquired"] and acq["reclaimed"]
    assert call("lease-peek")["holder"] == "B"
    assert call("lease-release", holder="A")["holder"] == "B"  # 非持有者释放失败
    assert call("lease-release", holder="B")["released"]
    assert not list((root / "jobs").glob("*.stale-*"))  # 没有遗留的私有文件


def test_assistant_lease_holder_with_running_job_blocks_others(tmp_path: Path):
    root = tmp_path / "work"
    call = _make_caller(root)
    call("start", job_id="run1", spec={"argv": ["sleep", "30"]})
    assert call("lease-acquire", holder="run1")["acquired"]
    assert not call("lease-acquire", holder="other", grace_s=0.0)["acquired"]
    call("stop", job_id="run1", grace_s=5.0)
    _wait(lambda: call("status", job_id="run1")["state"] != "running", 7.0)
    assert call("lease-acquire", holder="other")["acquired"]


def test_assistant_probe_reports_memory_and_disk(tmp_path: Path):
    root = tmp_path / "work"
    call = _make_caller(root)
    p = call("probe")
    assert isinstance(p.get("mem_available_gib"), (int, float, type(None)))
    assert isinstance(p["disk_free_gib"], (int, float)) and p["disk_free_gib"] > 0


def test_assistant_unknown_command_returns_error(tmp_path: Path):
    err = _make_caller(tmp_path).raw("rm", {})
    assert not err["ok"] and err["error"] == "unknown_command"


def _make_caller(root: Path):
    root.mkdir(parents=True, exist_ok=True)

    class _Caller:
        def raw(self, cmd: str, req: dict | None = None, **fields: object) -> dict:
            """不断言 ok:预期失败的用例用它读错误应答。"""
            req = {**(req or {}), **fields}
            import json, subprocess
            req["work_root"] = str(root)
            out = subprocess.run(["/bin/sh", "-c", f"{PYTHON} {TOOL} {cmd}"], input=json.dumps(req),
                                 capture_output=True, text=True, timeout=60, check=False)
            assert out.returncode == 0, out.stderr
            return json.loads(out.stdout)

        def __call__(self, cmd: str, **fields: object) -> dict:
            reply = self.raw(cmd, **fields)
            assert reply["ok"], reply
            return reply

    return _Caller()


def _wait(pred, timeout: float, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if pred() or time.monotonic() >= deadline:
            return pred()
        time.sleep(step)


# ===== client =====

def _endpoint(root: Path, **extra: object) -> ComputeEndpoint:
    base = {"endpoint_id": "test", "work_root": str(root / "w"), "roll_root": str(root / "r"),
            "launch_prefix": (), "base_model_path": str(root / "m"), "heldout_jsonl": str(root / "h.jsonl"),
            "sft_template": "t", "min_free_host_mem_gib": 0.5, "min_free_disk_gib": 1.0,
            "poll_seconds": 0.3, "job_timeout_seconds": 10.0}
    return ComputeEndpoint(**{**base, **extra})


def test_client_uploads_the_helper_once(tmp_path: Path):
    ep = _endpoint(tmp_path)
    tr = SshTransport("local", runner=local_runner)
    cl = ComputeClient(ep, tr)
    assert not cl._ready
    cl.ensure_helper()
    assert cl._ready
    helper_path = Path(ep.work_root) / "bin" / f"job_tool-{HELPER_SHA256[:8]}.py"
    assert helper_path.read_bytes() == helper_bytes()
    helper_path.write_text("# corrupted")
    cl.ensure_helper()  # 已标记 ready,不再上传
    assert helper_path.read_text() == "# corrupted"


def test_client_retries_helper_upload_when_remote_file_is_missing(tmp_path: Path):
    ep = _endpoint(tmp_path)
    tr = SshTransport("local", runner=local_runner)
    cl = ComputeClient(ep, tr)
    cl.ensure_helper()
    helper_path = Path(ep.work_root) / "bin" / f"job_tool-{HELPER_SHA256[:8]}.py"
    helper_path.unlink()
    cl._call("probe")  # 助手不在:_call 重新上传
    assert helper_path.exists()


def test_client_run_job_success_acquires_lease_and_releases_after(tmp_path: Path):
    ep = _endpoint(tmp_path)
    cl = ComputeClient(ep, SshTransport("local", runner=local_runner))
    spec = JobSpec(argv=(PYTHON, "-c", "print('ok',flush=True)"))
    result = cl.run_job("job1", spec)
    assert result.exit_code == 0 and "ok" in result.log_tail
    assert cl.lease_holder()[0] is None


def test_client_run_job_nonzero_exit_releases_lease_and_raises(tmp_path: Path):
    ep = _endpoint(tmp_path)
    cl = ComputeClient(ep, SshTransport("local", runner=local_runner))
    with pytest.raises(RemoteJobFailedError, match="exited with code 2") as exc:
        cl.run_job("job2", JobSpec(argv=(PYTHON, "-c", "import sys;print('boom');sys.exit(2)")))
    assert exc.value.exit_code == 2 and "boom" in exc.value.log_tail
    assert cl.lease_holder()[0] is None


def test_client_run_job_timeout_stops_the_job_and_releases_lease(tmp_path: Path):
    ep = _endpoint(tmp_path, job_timeout_seconds=2.0, poll_seconds=0.3)
    cl = ComputeClient(ep, SshTransport("local", runner=local_runner))
    t = time.monotonic()
    with pytest.raises(RemoteJobTimeoutError, match="exceeded 2") as exc:
        cl.run_job("job3", JobSpec(argv=("sleep", "600")))
    assert 2.0 < time.monotonic() - t < 4.0
    assert cl.lease_holder()[0] is None


def test_client_run_job_lease_held_raises_busy(tmp_path: Path):
    ep = _endpoint(tmp_path)
    cl = ComputeClient(ep, SshTransport("local", runner=local_runner))
    assert cl.lease_acquire("other")
    with pytest.raises(ComputeBusyError, match="held by job other"):
        cl.run_job("job4", JobSpec(argv=("true",)))
    cl.lease_release("other")


def test_client_run_job_is_idempotent_after_restart(tmp_path: Path):
    ep = _endpoint(tmp_path, job_timeout_seconds=30.0)
    cl = ComputeClient(ep, SshTransport("local", runner=local_runner))
    spec = JobSpec(argv=("sleep", "2"))
    cl.start("job5", spec)  # 第一次启动
    assert cl.status("job5").state == "running"
    cl2 = ComputeClient(ep, SshTransport("local", runner=local_runner))  # 守护进程重启(新客户端)
    result = cl2.run_job("job5", spec)  # 同规格再调:接上已有作业
    assert result.exit_code == 0


def test_client_preflight_gpu_count_own_holder_skips_process_check(tmp_path: Path):
    ep = _endpoint(tmp_path)
    cl = ComputeClient(ep, SshTransport("local", runner=local_runner))
    cl.lease_acquire("me")
    cl.start("me", JobSpec(argv=("sleep", "2")))
    import unittest.mock as mock
    real_call = cl._call

    def fake_call(command: str, *args: object, **fields: object) -> dict:
        if command == "probe":  # 开发机不一定是 Linux,没有 /proc/meminfo;资源数字在这里固定
            return {"ok": True, "mem_available_gib": 64.0, "disk_free_gib": 500.0}
        return real_call(command, *args, **fields)

    with mock.patch.object(cl, "_call", side_effect=fake_call), \
            mock.patch.object(cl, "gpu_process_count", return_value=1):
        cl.preflight(own_holder="me")  # 不抛异常:租约是自己的,GPU 上的进程就是本作业
        with pytest.raises(ComputeBusyError, match="held by job me"):
            cl.preflight()  # 没声明自己是持有者:先被租约拦下
        cl.stop("me")
        cl.lease_release("me")
        with pytest.raises(ComputeBusyError, match="1 compute process"):
            cl.preflight()  # 租约空了但 GPU 上还有进程:被进程数拦下(Ruling 8 只豁免自己的作业)


def test_client_preflight_lease_held_by_another_raises_busy(tmp_path: Path):
    ep = _endpoint(tmp_path)
    cl = ComputeClient(ep, SshTransport("local", runner=local_runner))
    cl.lease_acquire("other")
    with pytest.raises(ComputeBusyError, match="held by job other"):
        cl.preflight()
    cl.lease_release("other")


def test_nvidia_smi_command_has_no_injection_vector():
    """nvidia-smi 命令是硬编码元组(没有任何占位符),所以输入数据无法注入它。"""
    assert NVIDIA_SMI == ("nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader")


```

- [ ] **Step 3: 跑,确认失败**

全量命令 + `tests/test_compute_assistant.py`。期望:收集阶段 `ModuleNotFoundError: No module named 'compute.endpoint'`(`job_tool.py` 已在 Step 1 落盘,但其余模块还没有)。

- [ ] **Step 4: 实现端点、传输、助手引导与客户端**

**Create `src/compute/endpoint.py`:**

```python path=src/compute/endpoint.py
"""端点描述(冻结数据类)与 launch_prefix 白名单(真实后端设计 §3.1、§3.2)。

主机定位(ssh 别名)不在这里:它留在本机文件里,由 SshTransport 持有。
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from compute.errors import ComputeError

ENDPOINT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
LAUNCH_PREFIX_PROGRAMS = ("docker", "podman")


def check_launch_prefix(prefix: Sequence[str]) -> tuple[str, ...]:
    """空列表(裸机),或 `<docker|podman> exec ... <容器>`;其余一律拒绝。

    它来自 git 树,会被原样拼在远端命令前,所以放宽白名单要改代码并经评审(设计 §3.2)。
    必须是 exec:`run` 每次起新容器,会破坏"所有调用在同一个 PID 命名空间"的前提。
    报错只写约束,不回显取值。"""
    if not isinstance(prefix, (list, tuple)) or not all(isinstance(a, str) and a and "\0" not in a for a in prefix):
        raise ComputeError("launch_prefix must be a list of non-empty strings")
    if not prefix:
        return ()
    if prefix[0] not in LAUNCH_PREFIX_PROGRAMS:
        raise ComputeError("launch_prefix must be empty or start with docker or podman")
    if len(prefix) < 3 or prefix[1] != "exec":
        raise ComputeError("launch_prefix must have the form '<docker|podman> exec ... <container>'")
    return tuple(prefix)


def is_clean_abs_path(path: object) -> bool:
    return (isinstance(path, str) and path.startswith("/") and "\0" not in path
            and ".." not in path.split("/") and path.rstrip("/") != "")


@dataclass(frozen=True)
class ComputeEndpoint:
    endpoint_id: str
    work_root: str
    roll_root: str
    launch_prefix: tuple[str, ...]
    base_model_path: str
    heldout_jsonl: str
    sft_template: str
    min_free_host_mem_gib: float
    min_free_disk_gib: float
    poll_seconds: float
    job_timeout_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "launch_prefix", check_launch_prefix(self.launch_prefix))
        if not isinstance(self.endpoint_id, str) or not ENDPOINT_ID_PATTERN.match(self.endpoint_id):
            raise ComputeError("endpoint_id has an invalid form")
        for name in ("work_root", "roll_root", "base_model_path", "heldout_jsonl"):
            if not is_clean_abs_path(getattr(self, name)):
                raise ComputeError(f"{name} must be an absolute path below / without '..'")
        if not isinstance(self.sft_template, str) or not self.sft_template:
            raise ComputeError("sft_template must be a non-empty string")
        for name in ("min_free_host_mem_gib", "min_free_disk_gib"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ComputeError(f"{name} must be a non-negative number")
        for name in ("poll_seconds", "job_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ComputeError(f"{name} must be a positive number")
```

**Create `src/compute/transport.py`:**

```python path=src/compute/transport.py
"""SSH 传输(真实后端设计 §3.1):BatchMode、连接超时、命令用 shlex.join 拼成一个字符串,数据走 stdin。"""
from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from compute.errors import ComputeError, TransportError

# 不能以 '-' 开头(会被 ssh 当成选项),不含空白与引号。再加 `--` 双保险。
_ALIAS = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@:-]*$")


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class Runner(Protocol):
    """把一条完整的 ssh 命令行跑完。测试里换成本机替身。"""

    def __call__(self, argv: Sequence[str], *, input: bytes, timeout: float) -> RunResult: ...


class Transport(Protocol):
    def run(self, remote_argv: Sequence[str], *, stdin: bytes = b"", timeout: float = 60.0) -> RunResult: ...


def subprocess_runner(argv: Sequence[str], *, input: bytes, timeout: float) -> RunResult:
    try:
        out = subprocess.run(list(argv), input=input, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise TransportError(f"ssh did not finish within {timeout:g}s") from None
    except OSError as exc:
        raise TransportError(f"cannot run ssh ({type(exc).__name__})") from None
    return RunResult(out.returncode, out.stdout, out.stderr)


class SshTransport:
    def __init__(self, alias: str, *, connect_timeout_s: float = 10.0, runner: Runner = subprocess_runner) -> None:
        if not isinstance(alias, str) or not _ALIAS.match(alias):
            raise ComputeError("the ssh alias has an invalid form")
        self._alias = alias
        self._connect_timeout = max(1, int(connect_timeout_s))
        self._runner = runner

    def ssh_argv(self, remote_argv: Sequence[str]) -> list[str]:
        return ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={self._connect_timeout}",
                "--", self._alias, shlex.join(remote_argv)]

    def run(self, remote_argv: Sequence[str], *, stdin: bytes = b"", timeout: float = 60.0) -> RunResult:
        return self._runner(self.ssh_argv(remote_argv), input=stdin, timeout=timeout)
```

**Create `src/compute/helper.py`:**(`HELPER_SHA256` 是 Step 1 落盘的 `job_tool.py` 的 sha256;在仓库根跑 `python3 -c "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('src/compute/job_tool.py').read_bytes()).hexdigest())"` 核对,若与下面不符,以命令输出为准并改这一行。)

```python path=src/compute/helper.py
"""远端助手的字节、写死的 sha256、以及把它送上远端的引导脚本。

改 job_tool.py 必须同步改 HELPER_SHA256(测试会拦住),等于每次改动都过一遍评审(设计 §3.2)。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from compute.errors import ComputeError

HELPER_SHA256 = "f222b70d0770335b93828968541cf5421977588fdf60926eaf2f06ff6f20c75c"
_HELPER_FILE = Path(__file__).with_name("job_tool.py")

# 引导脚本只做一件事:把 stdin 里的助手原子地写到指定路径,哈希不符就不写。已有同哈希文件则什么都不写。
# 路径与哈希走位置参数,脚本里没有任何可被 shell 解释的数据。
BOOTSTRAP = (
    "import hashlib, os, sys, uuid\n"
    "d, p, h = sys.argv[1:4]\n"
    "b = sys.stdin.buffer.read()\n"
    "if hashlib.sha256(b).hexdigest() != h: sys.exit(3)\n"
    "if os.path.exists(p) and hashlib.sha256(open(p, 'rb').read()).hexdigest() == h: sys.exit(0)\n"
    "os.makedirs(d, exist_ok=True)\n"
    "t = p + '.tmp-' + uuid.uuid4().hex[:8]\n"
    "f = open(t, 'wb'); f.write(b); f.flush(); os.fsync(f.fileno()); f.close()\n"
    "os.chmod(t, 0o755); os.replace(t, p)\n"
)


def helper_bytes() -> bytes:
    data = _HELPER_FILE.read_bytes()
    if hashlib.sha256(data).hexdigest() != HELPER_SHA256:
        raise ComputeError("job_tool.py does not match the pinned sha256 in compute/helper.py")
    return data


def remote_helper_path(work_root: str) -> str:
    return f"{work_root.rstrip('/')}/bin/job_tool-{HELPER_SHA256[:8]}.py"
```

**Create `src/compute/client.py`:**

```python path=src/compute/client.py
"""ComputeClient:经传输调用远端助手,翻译应答,提供 run_job 与 preflight(真实后端设计 §3.2、§3.3)。

不认识训练与评测。run_job 的重入语义:租约持有者就是 job_id,start 对同规格幂等,所以守护进程重启后
再调同一个 run_job 会接上正在跑的作业。
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from compute.endpoint import ComputeEndpoint
from compute.errors import (ComputeBusyError, HelperError, RemoteJobFailedError,
                            RemoteJobTimeoutError, TransportError)
from compute.helper import BOOTSTRAP, helper_bytes, remote_helper_path
from compute.transport import RunResult, Transport

_STDERR_TAIL = 300
_TRANSIENT_CODES = ("job_starting", "internal")  # 助手返回这些时重试有意义,按 TransportError 走延后通道
NVIDIA_SMI = ("nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader")


@dataclass(frozen=True)
class JobSpec:
    argv: tuple[str, ...]
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    secret_env: str | None = None  # 密钥经 stdin 传给 start,作业里以这个变量名读到;不进 spec 哈希

    def to_wire(self) -> dict:
        return {"argv": list(self.argv), "cwd": self.cwd, "env": dict(self.env), "secret_env": self.secret_env}


@dataclass(frozen=True)
class JobStatus:
    state: str  # missing | running | exited | lost
    exit_code: int | None
    spec_sha256: str | None


@dataclass(frozen=True)
class JobResult:
    exit_code: int
    log_tail: str


def _tail_of(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").strip()[-_STDERR_TAIL:]


class ComputeClient:
    def __init__(self, endpoint: ComputeEndpoint, transport: Transport, *, python: str = "python3",
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.endpoint = endpoint
        self._transport = transport
        self._python = python
        self._sleep = sleep
        self._clock = clock
        self._ready = False

    # ---------- 助手调用 ----------

    def _remote(self, argv: Sequence[str]) -> list[str]:
        return [*self.endpoint.launch_prefix, *argv]

    def ensure_helper(self) -> None:
        """把助手送上远端(内容寻址、幂等、只写 work_root);同一个客户端只做一次。"""
        if self._ready:
            return
        data = helper_bytes()
        path = remote_helper_path(self.endpoint.work_root)
        result = self._transport.run(
            self._remote([self._python, "-c", BOOTSTRAP, path.rsplit("/", 1)[0], path,
                          hashlib.sha256(data).hexdigest()]),
            stdin=data, timeout=120.0)
        if result.returncode != 0:
            raise TransportError(f"uploading the remote helper failed (exit {result.returncode}): "
                                 f"{_tail_of(result.stderr)}")
        self._ready = True

    def _invoke(self, command: str, request: dict, timeout: float) -> RunResult:
        argv = self._remote([self._python, remote_helper_path(self.endpoint.work_root), command])
        return self._transport.run(argv, stdin=json.dumps(request).encode("utf-8"), timeout=timeout)

    def _call(self, command: str, timeout: float = 60.0, **fields: object) -> dict:
        request = {"work_root": self.endpoint.work_root, **fields}
        for attempt in (1, 2):
            self.ensure_helper()
            result = self._invoke(command, request, timeout)
            lines = [ln for ln in result.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
            if result.returncode == 0 and lines:
                break
            if attempt == 1:
                self._ready = False  # 远端助手文件可能被清掉了:重新上传再试一次
                continue
            raise TransportError(f"remote helper call '{command}' failed (exit {result.returncode}): "
                                 f"{_tail_of(result.stderr)}")
        try:
            reply = json.loads(lines[-1])
        except ValueError:
            reply = None
        if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
            raise TransportError(f"remote helper call '{command}' returned unparseable output")
        if not reply["ok"]:
            code, detail = str(reply.get("error", "unknown")), str(reply.get("detail", ""))
            if code in _TRANSIENT_CODES:
                raise TransportError(f"remote helper call '{command}' failed transiently ({code}): {detail}")
            raise HelperError(code, detail)
        return reply

    # ---------- 作业 ----------

    def start(self, job_id: str, spec: JobSpec, *, secret: str | None = None) -> JobStatus:
        fields: dict = {"job_id": job_id, "spec": spec.to_wire()}
        if secret is not None:
            fields["secret"] = secret
        return self._status_of(self._call("start", **fields))

    def status(self, job_id: str) -> JobStatus:
        return self._status_of(self._call("status", job_id=job_id))

    def tail(self, job_id: str, lines: int = 50) -> str:
        return str(self._call("tail", job_id=job_id, lines=lines)["text"])

    def stop(self, job_id: str, *, grace_s: float = 10.0) -> JobStatus:
        return self._status_of(self._call("stop", timeout=grace_s + 30.0, job_id=job_id, grace_s=grace_s))

    @staticmethod
    def _status_of(reply: dict) -> JobStatus:
        return JobStatus(reply["state"], reply.get("exit_code"), reply.get("spec_sha256"))

    # ---------- 文件 ----------

    def put(self, path: str, data: bytes, *, executable: bool = False) -> None:
        self._call("put", path=path, content_b64=base64.b64encode(data).decode("ascii"),
                   sha256=hashlib.sha256(data).hexdigest(), executable=executable)

    def hash_tree(self, path: str, mode: str) -> str:
        return str(self._call("hash-tree", timeout=3600.0, path=path, mode=mode)["sha256"])

    # ---------- 租约与资源 ----------

    def lease_acquire(self, holder: str, *, grace_s: float | None = None) -> bool:
        extra = {} if grace_s is None else {"grace_s": grace_s}
        return bool(self._call("lease-acquire", holder=holder, **extra)["acquired"])

    def lease_release(self, holder: str) -> bool:
        return bool(self._call("lease-release", holder=holder)["released"])

    def lease_holder(self) -> tuple[str | None, bool]:
        reply = self._call("lease-peek")
        return reply["holder"], bool(reply["stale"])

    def gpu_process_count(self) -> int:
        """宿主机上的计算进程数(不经 launch_prefix:容器内通常看不到别的容器的进程)。只数,不记名字。"""
        result = self._transport.run(list(NVIDIA_SMI), timeout=60.0)
        if result.returncode != 0:
            raise TransportError(f"nvidia-smi failed (exit {result.returncode}): {_tail_of(result.stderr)}")
        return sum(1 for ln in result.stdout.decode("utf-8", "replace").splitlines() if ln.strip())

    def preflight(self, *, own_holder: str | None = None) -> None:
        """GPU、内存、磁盘检查;不过抛 ComputeBusyError。不取租约、不终止任何进程(设计 §3.3、D8)。

        唯一的写是首次调用时把助手送上远端(Ruling 7)。租约持有者就是 own_holder 时视为本作业,
        跳过"无计算进程"检查(Ruling 8)。"""
        holder, stale = self.lease_holder()
        if holder and not stale and holder != own_holder:
            raise ComputeBusyError(f"the GPU lease is held by job {holder}")
        probe = self._call("probe")
        mem, disk = probe.get("mem_available_gib"), probe.get("disk_free_gib")
        if mem is None or disk is None:
            raise TransportError("the host did not report memory or disk figures")
        if mem < self.endpoint.min_free_host_mem_gib:
            raise ComputeBusyError(f"free host memory {mem:g} GiB is below {self.endpoint.min_free_host_mem_gib:g}")
        if disk < self.endpoint.min_free_disk_gib:
            raise ComputeBusyError(f"free disk {disk:g} GiB is below {self.endpoint.min_free_disk_gib:g}")
        if not (holder and holder == own_holder):
            count = self.gpu_process_count()
            if count:
                raise ComputeBusyError(f"{count} compute process(es) are running on the GPU host")

    # ---------- 整段作业 ----------

    def run_job(self, job_id: str, spec: JobSpec, *, secret: str | None = None,
                tail_lines: int = 40) -> JobResult:
        """取租约、启动(幂等)、轮询到结束、释放租约。非零退出抛 RemoteJobFailedError,超时先 stop 再抛
        RemoteJobTimeoutError,租约被占抛 ComputeBusyError。传输错误原样抛出且不释放租约:作业还在远端跑,
        下一次调用会接上它。超时从本次调用算起,守护进程重启后重新计时。"""
        if not self.lease_acquire(job_id):
            holder, _ = self.lease_holder()
            raise ComputeBusyError(f"the GPU lease is held by job {holder or 'unknown'}")
        started = self._clock()
        status = self.start(job_id, spec, secret=secret)
        while status.state == "running":
            remaining = self.endpoint.job_timeout_seconds - (self._clock() - started)
            if remaining <= 0:
                self.stop(job_id)
                self.lease_release(job_id)
                raise RemoteJobTimeoutError(f"job {job_id} exceeded {self.endpoint.job_timeout_seconds:g}s")
            self._sleep(min(self.endpoint.poll_seconds, remaining))
            status = self.status(job_id)
        tail = self.tail(job_id, tail_lines)
        self.lease_release(job_id)
        if status.state == "exited" and status.exit_code == 0:
            return JobResult(0, tail)
        what = f"exited with code {status.exit_code}" if status.state == "exited" else f"is {status.state}"
        raise RemoteJobFailedError(f"job {job_id} {what}", exit_code=status.exit_code, log_tail=tail)
```

- [ ] **Step 5: 跑通**

全量命令 + `tests/test_compute_assistant.py`。期望 **30 passed**,耗时约 10 秒(助手在本机真起子进程、真等其退出,所以不是毫秒级)。再跑一次不带路径的全量命令,期望 **B + 10 + 30** 个通过(`B = 2642`,若别的计划先落地,以增量为准)。

若 `test_helper_pin_matches_file` 失败:`HELPER_SHA256` 与 `job_tool.py` 对不上。按 Step 4 里 `helper.py` 那段的命令重算,只改那一行常量,不要改 `job_tool.py` 去凑哈希。

- [ ] **Step 6: Commit**

```bash
git add src/compute/job_tool.py src/compute/endpoint.py src/compute/transport.py src/compute/helper.py src/compute/client.py src/compute/errors.py tests/test_compute_assistant.py
git commit -m "feat(compute): 远端助手、SSH 传输、端点描述与客户端(run_job、preflight)"
```

---

## Task 3: 端点配置读取(设计 §3.1)

**Files:**
- Create: `src/factory/compute_config.py`
- Create: `tests/test_factory_compute_config.py`

**Interfaces:**
- Consumes: `factory.configio.read_tree_config`(返回 **payload 的深拷贝**,不是 Envelope)、`factory.configio.key_names`、`factory.errors.FactoryConfigError`、`compute.endpoint.ComputeEndpoint`(Task 2 的冻结数据类)
- Produces: `read_endpoints_config(store) -> list[ComputeEndpoint]`、`COMPUTE_ENDPOINTS_FILE = compute_endpoints_file()`(模块级常量)

**不做什么:** 不加 `compute_endpoints_file()`(Task 1 已加,定义在 `paths.py`);不动 `install_defaults`(设计 §3.1 结尾明说端点配置**不**由默认值安装)。

**已核实的事实(实现者不要再猜):**

| 事实 | 依据 |
|---|---|
| `read_tree_config(store, rel)` 返回 payload 的深拷贝,不是 Envelope | `configio.py:22` → `config.py:184-191` |
| 信封必须是 `type="config"`;别的 type 被 `read_config` 拒绝 | `config.py:157,188-189` |
| `write_config` 只接受 `control-plane/config/` 下的路径,端点配置不能用它写 | `config.py:199-215` |
| `read_tree_config` 任何读不了的情形都抛 `FactoryConfigError`:信封 type 不是 `config` 时文案是 `<路径> is not a configuration product (its type field is wrong)`;其余(YAML、schema、版本)是 `<路径> cannot be read as a configuration product (<异常类名>)` | `configio.py:34-40` |
| 信封 schema 要求 `payload` 是对象(`schemas/envelope.schema.json:22`):payload 写成列表时 `write_product` 照写,`read_config` 读时 `validate_envelope` 拒绝(`[...] is not of type 'object'`)。所以端点列表放在 `payload["endpoints"]` 下,与 `evaluator.yaml` 同样是映射 | 实测 |
| `key_names({"work_root"})` 返回 `['work_root']` | 实测 |
| `ComputeEndpoint(**item)` 坏值抛 `ComputeError`,文案形如 `endpoint_id has an invalid form`、`poll_seconds must be a positive number`;`launch_prefix=[]` 归一成 `()` | 实测 |
| `ComputeEndpoint` **没有** `from_dict`,只接受关键字构造 | 实测 |
| 边界测试禁止别的包导入 `factory`,**不禁止** `factory` 导入别的包,所以 `factory → compute` 合法 | `test_factory_boundaries.py:22-27` |

- [ ] **Step 1: 写失败测试**

```python path=tests/test_factory_compute_config.py
"""计算端点配置读取:缺文件、信封不合、载荷畸形、键缺失或多余、正常多端点。

信封必须是 type="config"(read_config 只认 CONFIG_TYPE),端点列表放在 payload 里。
"""
from __future__ import annotations

import pytest
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from factory.compute_config import COMPUTE_ENDPOINTS_FILE, read_endpoints_config
from factory.errors import FactoryConfigError

_ITEM = {
    "endpoint_id": "gpu-1",
    "work_root": "/srv/rise/work",
    "roll_root": "/srv/roll",
    "launch_prefix": [],
    "base_model_path": "/srv/models/qwen-4b",
    "heldout_jsonl": "/srv/roll/data/heldout.jsonl",
    "sft_template": "/srv/roll/configs/sft_lora.yaml",
    "min_free_host_mem_gib": 12.0,
    "min_free_disk_gib": 50.0,
    "poll_seconds": 10.0,
    "job_timeout_seconds": 3600.0,
}


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _write(store: GitStore, payload, *, type_: str = "config") -> None:
    env = Envelope(
        id="cfg-compute-endpoints", type=type_, version=1, created_at="2026-10-15T00:00:00Z",
        created_by=OwnerRef(), status="effective", schema_version=1, payload=payload,
    )
    store.write_product(COMPUTE_ENDPOINTS_FILE, env, "test: compute endpoints")


def test_missing_file_is_a_factory_config_error(store):
    with pytest.raises(FactoryConfigError, match=r"control-plane/compute/endpoints\.yaml is missing"):
        read_endpoints_config(store)


def test_wrong_envelope_type_is_rejected(store):
    _write(store, {"endpoints": []}, type_="compute_endpoints")
    with pytest.raises(FactoryConfigError, match=r"is not a configuration product"):
        read_endpoints_config(store)


def test_endpoints_must_be_a_list(store):
    _write(store, {"endpoints": {"gpu-1": _ITEM}})
    with pytest.raises(FactoryConfigError, match=r"payload\.endpoints must be a list"):
        read_endpoints_config(store)


def test_item_must_be_a_mapping(store):
    _write(store, {"endpoints": ["nope"]})
    with pytest.raises(FactoryConfigError, match=r"item 0 must be a mapping"):
        read_endpoints_config(store)


def test_missing_key_is_named(store):
    _write(store, {"endpoints": [{k: v for k, v in _ITEM.items() if k != "work_root"}]})
    with pytest.raises(FactoryConfigError, match=r"missing \['work_root'\]"):
        read_endpoints_config(store)


def test_unknown_key_is_named(store):
    _write(store, {"endpoints": [{**_ITEM, "tail_lines": 50}]})
    with pytest.raises(FactoryConfigError, match=r"unknown \['tail_lines'\]"):
        read_endpoints_config(store)


def test_malformed_value_is_named(store):
    _write(store, {"endpoints": [{**_ITEM, "endpoint_id": "-bad"}]})
    with pytest.raises(FactoryConfigError, match=r"item 0: endpoint_id has an invalid form"):
        read_endpoints_config(store)


def test_success_reads_two_endpoints(store):
    _write(store, {"endpoints": [_ITEM, {**_ITEM, "endpoint_id": "gpu-2", "poll_seconds": 5.0}]})
    eps = read_endpoints_config(store)
    assert [e.endpoint_id for e in eps] == ["gpu-1", "gpu-2"]
    assert eps[0].work_root == "/srv/rise/work"
    assert eps[1].poll_seconds == 5.0
    assert eps[0].launch_prefix == ()
```

- [ ] **Step 2: 跑,确认失败**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_compute_config.py -q -p no:cacheprovider --color=no -W error
```

期望:`ModuleNotFoundError: No module named 'factory.compute_config'`。

- [ ] **Step 3: 写实现**

```python path=src/factory/compute_config.py
"""计算端点的配置读取与装配注入(真实后端设计 §3.1)。

两处配置各读各的:
- 树内 `control-plane/compute/endpoints.yaml`(config 信封,与 `evaluator.yaml` 同一条路径),
  读出 `compute.ComputeEndpoint` 列表。缺文件、信封不合、键缺失或多余、取值不合数据类约束,
  一律 `FactoryConfigError`,装配期就报,不等首次 reconcile。
- 本机主机映射:路径由环境变量 `RISE_COMPUTE_HOSTS` 指定,内容是 `{endpoint_id: ssh 别名}`,
  放在仓库之外,凭据都在 `~/.ssh/config` 里。

`read_tree_config` 返回 payload 的深拷贝,不是 Envelope;信封 type 与 schema_version 已由
`read_config` 与迁移链把关,这里不重复判。
"""
from __future__ import annotations

import os
from collections.abc import Mapping

import yaml

from compute.client import ComputeClient
from compute.endpoint import ComputeEndpoint
from compute.errors import ComputeError
from compute.transport import SshTransport
from controlplane.gitstore import GitStore
from controlplane.paths import compute_endpoints_file
from factory.configio import key_names, read_tree_config
from factory.errors import FactoryConfigError

COMPUTE_ENDPOINTS_FILE = compute_endpoints_file()
HOSTS_ENV = "RISE_COMPUTE_HOSTS"

_ENDPOINT_KEYS = (
    "endpoint_id", "work_root", "roll_root", "launch_prefix", "base_model_path",
    "heldout_jsonl", "sft_template", "min_free_host_mem_gib", "min_free_disk_gib",
    "poll_seconds", "job_timeout_seconds",
)


def read_endpoints_config(store: GitStore) -> list[ComputeEndpoint]:
    """树内端点列表。远端路径没有合理默认值,缺了就拒绝装配真实后端。"""
    try:
        payload = read_tree_config(store, COMPUTE_ENDPOINTS_FILE)
    except FileNotFoundError:
        raise FactoryConfigError(
            f"{COMPUTE_ENDPOINTS_FILE} is missing; real backends need it to build compute endpoints"
        ) from None
    # 信封 schema 要求 payload 是映射,端点列表放在 endpoints 键下;缺键与非列表同一句报错。
    items = payload.get("endpoints")
    if not isinstance(items, list):
        raise FactoryConfigError(f"{COMPUTE_ENDPOINTS_FILE} payload.endpoints must be a list")
    endpoints: list[ComputeEndpoint] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise FactoryConfigError(f"{COMPUTE_ENDPOINTS_FILE} item {index} must be a mapping")
        keys = set(item)
        missing = key_names(set(_ENDPOINT_KEYS) - keys)
        unknown = key_names(keys - set(_ENDPOINT_KEYS))
        if missing or unknown:
            raise FactoryConfigError(
                f"{COMPUTE_ENDPOINTS_FILE} item {index}: missing {missing}, unknown {unknown}"
            )
        try:
            endpoints.append(ComputeEndpoint(**item))
        except ComputeError as exc:
            raise FactoryConfigError(f"{COMPUTE_ENDPOINTS_FILE} item {index}: {exc}") from None
    return endpoints
```

- [ ] **Step 4: 跑通**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_compute_config.py -q -p no:cacheprovider --color=no -W error
```

期望:**8 passed**。

- [ ] **Step 5: Commit**

```bash
git add src/factory/compute_config.py tests/test_factory_compute_config.py
git commit -m "feat(factory): 读树内 compute/endpoints.yaml 产出 ComputeEndpoint 列表"
```

---

## Task 4: 本机主机映射与 ComputeClient 装配(设计 §3.1)

**Files:**
- Modify: `src/factory/compute_config.py`(追加 `read_host_map` 与 `build_compute_client`)
- Modify: `tests/test_factory_compute_config.py`(追加测试)

**Interfaces:**
- Consumes: `read_endpoints_config`(本任务前一步)、`compute.client.ComputeClient`、`compute.transport.SshTransport`、`os.environ`
- Produces: `read_host_map(*, env: Mapping[str, str] | None = None) -> dict[str, str]`、`build_compute_client(endpoint: ComputeEndpoint, hosts: Mapping[str, str], *, env=..., python: str = "python3") -> ComputeClient`

**为什么单独一个任务:** 树内不存主机地址(设计 D9)。主机定位只在仓库之外的本机映射里,
换机器只改这一份映射,树内记录与已有存点引用都不动。

**已核实的事实:**

| 事实 | 依据 |
|---|---|
| `ComputeClient(endpoint, transport, *, python="python3", sleep, clock)` | `compute/client.py:55` |
| `SshTransport(alias, *, connect_timeout_s=10.0, runner=subprocess_runner)`;别名不合法抛 `ComputeError` | `compute/transport.py:44-50` |
| `read_tree_config` 返回 payload 深拷贝,已在上一步用 | 实测 |
| `install_defaults` **不**安装端点配置,它返回的 `paths` 里不含 `COMPUTE_ENDPOINTS_FILE` | 设计 §3.1;`defaults.py:73-90` |
| 端点缺失或不合法一律 `FactoryConfigError`(退出码 2 由 CLI 层负责) | 设计 §3.1、§12 |

- [ ] **Step 1: 写失败测试(追加到 `tests/test_factory_compute_config.py` 末尾)**

```python
# ---------------- 本机主机映射与装配注入 ----------------

from factory.compute_config import HOSTS_ENV, build_compute_client, read_host_map


def _hosts_file(tmp_path, text):
    path = tmp_path / "hosts.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_host_map_missing_env_names_the_variable(tmp_path):
    with pytest.raises(FactoryConfigError, match=rf"{HOSTS_ENV} is not set"):
        read_host_map(env={})


def test_host_map_missing_file_names_the_path(tmp_path):
    missing = str(tmp_path / "nope.yaml")
    with pytest.raises(FactoryConfigError, match=r"cannot be read as a compute host map"):
        read_host_map(env={HOSTS_ENV: missing})


def test_host_map_must_be_a_mapping_of_strings(tmp_path):
    path = _hosts_file(tmp_path, "- just\n- a list\n")
    with pytest.raises(FactoryConfigError, match=r"must be a mapping of endpoint_id to ssh alias"):
        read_host_map(env={HOSTS_ENV: path})


def test_host_map_reads_the_aliases(tmp_path):
    path = _hosts_file(tmp_path, "gpu-1: .181\nmock-gpu: localhost\n")
    assert read_host_map(env={HOSTS_ENV: path}) == {"gpu-1": ".181", "mock-gpu": "localhost"}


def test_unknown_endpoint_in_the_map_is_a_config_error(tmp_path):
    path = _hosts_file(tmp_path, "other: somewhere\n")
    with pytest.raises(FactoryConfigError, match=rf"{HOSTS_ENV} has no entry for endpoint 'gpu-1'"):
        build_compute_client(ComputeEndpoint(**_ITEM), read_host_map(env={HOSTS_ENV: path}))


def test_build_compute_client_wires_the_ssh_transport(tmp_path):
    path = _hosts_file(tmp_path, "gpu-1: .181\n")
    client = build_compute_client(ComputeEndpoint(**_ITEM), read_host_map(env={HOSTS_ENV: path}))
    assert isinstance(client, ComputeClient)
    assert client.endpoint.endpoint_id == "gpu-1"
    assert client._transport.ssh_argv(["python3", "-c", "1"])[:6] == [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--",
    ]
```

同时把导入段补上:

```python
from compute.client import ComputeClient
from compute.endpoint import ComputeEndpoint
```

(放在文件顶部已有的导入之后。)

- [ ] **Step 2: 跑,确认失败**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_compute_config.py -q -p no:cacheprovider --color=no -W error
```

期望:`ImportError: cannot import name 'HOSTS_ENV'`。

- [ ] **Step 3: 写实现(追加到 `src/factory/compute_config.py` 末尾)**

```python
def read_host_map(*, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """本机主机映射 `{endpoint_id: ssh 别名}`。路径由 `RISE_COMPUTE_HOSTS` 指定。

    映射放在仓库之外,别名指向 `~/.ssh/config` 的主机条目,所以两处配置都不存凭据。
    报错只写环境变量名与路径,不回显内容。
    """
    environ = os.environ if env is None else env
    path = environ.get(HOSTS_ENV)
    if not path:
        raise FactoryConfigError(
            f"{HOSTS_ENV} is not set; real backends need it to locate the endpoint hosts"
        )
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except OSError:
        raise FactoryConfigError(f"{path} cannot be read as a compute host map (OSError)") from None
    except yaml.YAMLError:
        raise FactoryConfigError(f"{path} cannot be read as a compute host map (YAMLError)") from None
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v for k, v in data.items()
    ):
        raise FactoryConfigError(f"{path} must be a mapping of endpoint_id to ssh alias")
    return dict(data)


def build_compute_client(
    endpoint: ComputeEndpoint,
    hosts: Mapping[str, str],
    *,
    python: str = "python3",
) -> ComputeClient:
    """按端点与主机映射装配一个 `ComputeClient`。别名只在映射里,不进 git 树。"""
    alias = hosts.get(endpoint.endpoint_id)
    if alias is None:
        raise FactoryConfigError(
            f"{HOSTS_ENV} has no entry for endpoint {endpoint.endpoint_id!r}"
        )
    try:
        transport = SshTransport(alias)
    except ComputeError:
        raise FactoryConfigError(
            f"the ssh alias for endpoint {endpoint.endpoint_id!r} has an invalid form"
        ) from None
    return ComputeClient(endpoint, transport, python=python)
```

- [ ] **Step 4: 跑通**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_compute_config.py -q -p no:cacheprovider --color=no -W error
```

期望:**14 passed**。

- [ ] **Step 5: 断言默认值不安装端点配置**

追加一个测试,把设计 §3.1 结尾那句钉住:

```python
def test_install_defaults_does_not_install_endpoints(tmp_path):
    """远端路径没有合理默认值:默认值安装器不得写端点配置,缺了就拒绝装配。"""
    from factory.defaults import install_defaults

    store = GitStore(str(tmp_path / "cp"))
    store.init()
    paths = install_defaults(store)
    assert COMPUTE_ENDPOINTS_FILE not in paths
    assert not store.exists(COMPUTE_ENDPOINTS_FILE)
    with pytest.raises(FactoryConfigError, match=r"is missing; real backends need it"):
        read_endpoints_config(store)
```

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_factory_compute_config.py -q -p no:cacheprovider --color=no -W error
```

期望:**15 passed**。

- [ ] **Step 6: Commit**

```bash
git add src/factory/compute_config.py tests/test_factory_compute_config.py
git commit -m "feat(factory): 本机主机映射与 ComputeClient 装配;默认值不安装端点"
```

---

## Task 5: 分阶段执行器协议(设计 §4)

**Files:**
- Modify: `src/mindtrainer/executor.py`(`TrainingExecutor` 之后新增 `StagedExecutor`)
- Modify: `src/mindtrainer/orchestrator.py`(循环体改 `while`、新增切段与记账辅助)
- Modify: `tests/test_mt_orchestrator.py`(末尾追加 `TestStagedExecutor`)

**Interfaces:**
- Consumes: `TrainingExecutor`、`StepResult`、`Episode`、`TrainingConfig`、`batch_for_step`、`total_steps`、`phase_at`
- Produces: `StagedExecutor` 协议(`run_sft_stage`、`stage_end`)

**本任务不改**:`MockExecutor` 保持单步路径(它没有 `run_sft_stage`);GRPO 分支与批量预检原样;远端执行器由 Task 7 实现。

**已核实的事实(实现者不要再猜):**

| 事实 | 依据 |
|---|---|
| `StepResult` 是 `@dataclass(frozen=True)` | `datamodel.py:75` |
| `_update_summary(summary, result, reward_mean, failed)` 四个参数 | `orchestrator.py:79` |
| `on_step: Callable[[StepResult], None]`,现调用点是 `on_step(result)` | `orchestrator.py:106,206-207` |
| 主循环是 `for step in range(start, total)` | `orchestrator.py:174` |
| `Episode(id, scenario_id, role_assignment, turns, terminated_reason)`;`TurnRecord(turn_index, speaker_slot, utterance)` | `simulator/datamodel.py:151,181` |
| 测试辅助 `_batch_cfg` / `_batch_sources` / `_go` / `_populate` / `RUN` 都在 `tests/test_mt_orchestrator.py` | 该文件 35,74,97,109,120 行 |
| `build_attribution_backend(cfg.reward)` 构造归因后端 | `reward_pipeline.py:123` |
| `batch_for_step(items, step, size)` 按 `(step*size+i) % n` 回绕,单步数据是 step 的纯函数 | `trajectory_source.py:23` |

**为什么循环必须从 `for range` 改成 `while`:** 阶段路径一次跨多步,要跳过若干步。`range` 每次迭代重新赋值 `step`,所以循环体里写 `step = end - 1` 再 `continue` 不生效,会立刻退回下一格重跑已训过的步。必须用 `while`,并且每条分支自己把 `step` 推进到该分支实际训到的位置(单步路径 `step = done`,阶段路径 `step = end`)。

- [ ] **Step 1: executor.py 新增 `StagedExecutor` 协议**

在 `class TrainingExecutor(Protocol):` 的 `checkpoint_intact` 之后插入:

```python
class StagedExecutor(TrainingExecutor, Protocol):
    """支持阶段批量执行的扩展协议(真实后端设计 §4)。

    run_sft_stage(batches, *, first_step, trained_slot, prior_batches) -> list[StepResult]:
    按阶段返回整段步级结果。batches[i] 是全局第 first_step + i 步的数据,与逐步路径里
    batch_for_step 取到的相同。返回的 StepResult 按步排列,step 字段必须连续且从 first_step 起。

    prior_batches[j] 是全局第 j 步的数据,j 取 [0, first_step)。执行器不在这些步上训练,
    但必须把它们写进 train.jsonl 的前缀:ROLL 续训按位置跳过已消费的批次,前缀缺失会让它
    从错误的数据位置接着跑,而且不报错。first_step == 0 时为空列表。

    stage_end(batches, *, first_step, limit) -> int:可选方法,让执行器把段切得更细。
    limit 是编排器算出的段终点(开区间,不含该步);返回值是执行器能一次吃下的终点,
    同样开区间,必须满足 first_step < 返回值 <= limit,否则编排器抛 RuntimeError
    (不前进就是死循环,要响亮地失败)。没有这个方法的执行器按 limit 处理。
    """

    def run_sft_stage(
        self,
        batches: list[list[Episode]],
        *,
        first_step: int,
        trained_slot: str,
        prior_batches: list[list[Episode]],
    ) -> list[StepResult]: ...

    def stage_end(
        self, batches: list[list[Episode]], *, first_step: int, limit: int
    ) -> int: ...
```

- [ ] **Step 2: orchestrator.py 新增两个辅助函数**

放在 `run_training` 之前(模块级,`_failed` 之后再靠后一点即可):

```python
def _compute_stage_end(
    executor: TrainingExecutor, batches: list[list[Episode]], first_step: int, limit: int
) -> int:
    """执行器能一次吃下的段终点(开区间)。limit 是编排器算的段边界,开区间。

    有 stage_end 的执行器可以让段更短(例如磁盘容量约束);返回值必须满足
    first_step < end <= limit。不前进就是死循环,所以违反时响亮失败。
    """
    end_fn = getattr(executor, "stage_end", None)
    if end_fn is None:
        return limit
    end = end_fn(batches, first_step=first_step, limit=limit)
    if not (first_step < end <= limit):
        raise RuntimeError(
            f"executor.stage_end returned {end} which is not in ({first_step}, {limit}], "
            "violating the guarantee of forward progress"
        )
    return end


def _process_step_results(
    results: list[StepResult],
    *,
    summary: dict,
    start_step: int,
    reward_mean: float | None,
    failed: int,
    on_step: Callable[[StepResult], None] | None,
) -> None:
    """对一批结果逐个做与逐步路径完全相同的检查和记账:step 字段必须连续且从 start_step 起,
    然后写摘要、回调。抽出来让单步路径与阶段路径共用,避免两份记账逻辑漂移。"""
    for i, result in enumerate(results):
        expected_step = start_step + i
        if result.step != expected_step:
            raise RuntimeError(
                f"executor returned step {result.step} at index {i}, expected {expected_step}"
            )
        _update_summary(summary, result, reward_mean, failed)
        if on_step is not None:
            on_step(result)
```

- [ ] **Step 3: 循环改成 `while`,SFT 分支加阶段路径**

把 `run_training` 里的 `for step in range(start, total):` 改成 `step = start` + `while step < total:`,并按下文改 SFT 分支与循环末尾。改动后的循环骨架(其余分支原样保留):

```python
        step = start
        while step < total:
            phase = phase_at(step, cfg)
            point = f"step {step} ({phase})"
            reward_mean, failed = None, 0
            if phase == "sft":
                if sft_episodes is None:
                    sft_episodes = sources["sft"].collect().episodes
                run_sft_stage = getattr(executor, "run_sft_stage", None)
                if run_sft_stage is not None:
                    # 阶段路径:一段跑完再记账,段尾补一次存点判断
                    sft_end = min(cfg.sft_steps, total)
                    next_checkpoint = (step // cfg.checkpoint_every_steps + 1) * cfg.checkpoint_every_steps
                    limit = min(next_checkpoint, sft_end)
                    prior_batches = [
                        batch_for_step(sft_episodes, i, cfg.sft_batch_size) for i in range(step)
                    ]
                    end = _compute_stage_end(executor, prior_batches, step, limit)
                    batches = [
                        batch_for_step(sft_episodes, i, cfg.sft_batch_size) for i in range(step, end)
                    ]
                    point = f"sft stage [{step}, {end})"
                    results = run_sft_stage(
                        batches, first_step=step, trained_slot=cfg.trained_slot,
                        prior_batches=prior_batches,
                    )
                    _process_step_results(
                        results, summary=summary, start_step=step,
                        reward_mean=None, failed=0, on_step=on_step,
                    )
                    done = end
                    step = end
                    if done < total and done % cfg.checkpoint_every_steps == 0:
                        ref = executor.save_checkpoint(f"step-{done}")
                        run = dataclasses.replace(
                            run, checkpoint_step=done, last_checkpoint_ref=ref,
                            curve_summary=copy.deepcopy(summary),
                        )
                        write_training_run(store, run)
                    continue
                chosen = batch_for_step(sft_episodes, step, cfg.sft_batch_size)
                result = executor.sft_step(chosen, trained_slot=cfg.trained_slot)
            else:
                # GRPO 分支原文不动
                ...
            if result.step != step:
                raise RuntimeError(
                    f"executor reports step {result.step} but the orchestrator is at step {step}: "
                    f"executor and run record are out of sync"
                )
            _process_step_results(
                [result], summary=summary, start_step=step,
                reward_mean=reward_mean, failed=failed, on_step=on_step,
            )
            done = step + 1
            step = done
            if done < total and done % cfg.checkpoint_every_steps == 0:
                ref = executor.save_checkpoint(f"step-{done}")
                run = dataclasses.replace(
                    run, checkpoint_step=done, last_checkpoint_ref=ref,
                    curve_summary=copy.deepcopy(summary),
                )
                write_training_run(store, run)
```

要点:
- 原来 `if result.step != step` 与 `_update_summary` / `on_step` 三行,合并进 `_process_step_results`。
- 循环末尾原来用 `done = step + 1` 之后靠 `range` 自增;现在必须自己写 `step = done`。
- 阶段路径自己处理存点与 `continue`,不再走到公共尾巴。

- [ ] **Step 4: 冒烟:协议形状**

```bash
PYTHONPATH=src python3 - <<'PY'
from mindtrainer.executor import MockExecutor, StagedExecutor, TrainingExecutor

assert issubclass(StagedExecutor, TrainingExecutor)
assert hasattr(StagedExecutor, "run_sft_stage") and hasattr(StagedExecutor, "stage_end")
assert getattr(MockExecutor, "run_sft_stage", None) is None, "MockExecutor 不该有 run_sft_stage"
print("✓ 协议定义正常")
PY
```

期望:`✓ 协议定义正常`。

- [ ] **Step 5: 写测试 `TestStagedExecutor`**

在 `tests/test_mt_orchestrator.py` 末尾追加(直接复用文件里已有的 `_populate` / `_batch_cfg` / `_batch_sources` / `_go` / `RUN`):

```python
class TestStagedExecutor:
    """阶段执行器协议:run_sft_stage 成批返回、stage_end 切段、prior_batches 是前缀。

    覆盖:
    - 无 run_sft_stage 时走单步路径,on_step 逐步到达(与改动前一致)
    - 有 run_sft_stage 时成批返回,段内每步都被记账
    - stage_end 返回更短的段,编排器按子段衔接,prior_batches 随段增长
    - stage_end 不前进(first_step <= end)抛 RuntimeError
    - 结果的 step 字段与预期不符抛 RuntimeError
    """

    def test_single_step_path_unchanged(self, store):
        """MockExecutor 无 run_sft_stage:单步路径逐步回调,run 正常收尾。"""
        steps = []
        cfg = _batch_cfg(sft_steps=3, grpo_steps=0, checkpoint_every_steps=100)
        run = _go(store, cfg, MockExecutor(), steps=steps)
        assert [r.step for r in steps] == [0, 1, 2]
        assert run.status == "executed"

    def test_staged_path_records_every_step(self, store):
        """有 run_sft_stage:一次成批返回,段内每步都进 on_step 与摘要。"""

        class StagedExecutor(MockExecutor):
            def __init__(self):
                super().__init__()
                self.calls = []

            def run_sft_stage(self, batches, *, first_step, trained_slot, prior_batches):
                self.calls.append((first_step, len(batches), len(prior_batches)))
                return [
                    self.sft_step(batch, trained_slot=trained_slot) for batch in batches
                ]

        steps = []
        cfg = _batch_cfg(sft_steps=5, grpo_steps=0, checkpoint_every_steps=100)
        executor = StagedExecutor()
        run = _go(store, cfg, executor, steps=steps)
        assert executor.calls == [(0, 5, 0)], executor.calls
        assert [r.step for r in steps] == [0, 1, 2, 3, 4]
        assert run.curve_summary["sft_steps_done"] == 5

    def test_stage_end_cuts_finer_and_prefix_grows(self, store):
        """stage_end 每段只吃 2 步:段依次衔接,prior_batches 随已训步数增长。"""

        class ChunkingExecutor(MockExecutor):
            def __init__(self):
                super().__init__()
                self.calls = []

            def run_sft_stage(self, batches, *, first_step, trained_slot, prior_batches):
                self.calls.append((first_step, len(batches), len(prior_batches)))
                return [
                    self.sft_step(batch, trained_slot=trained_slot) for batch in batches
                ]

            def stage_end(self, batches, *, first_step, limit):
                return min(first_step + 2, limit)

        cfg = _batch_cfg(sft_steps=5, grpo_steps=0, checkpoint_every_steps=100)
        executor = ChunkingExecutor()
        _go(store, cfg, executor)
        assert executor.calls == [(0, 2, 0), (2, 2, 2), (4, 1, 4)], executor.calls

    def test_stage_end_without_forward_progress_fails_loudly(self, store):
        class StuckExecutor(MockExecutor):
            def run_sft_stage(self, batches, *, first_step, trained_slot, prior_batches):
                return []

            def stage_end(self, batches, *, first_step, limit):
                return first_step

        cfg = _batch_cfg(sft_steps=3, grpo_steps=0)
        with pytest.raises(RuntimeError, match=r"violating the guarantee of forward progress"):
            _go(store, cfg, StuckExecutor())

    def test_stage_end_beyond_limit_is_rejected(self, store):
        class OverrunExecutor(MockExecutor):
            def run_sft_stage(self, batches, *, first_step, trained_slot, prior_batches):
                return []

            def stage_end(self, batches, *, first_step, limit):
                return limit + 1

        cfg = _batch_cfg(sft_steps=3, grpo_steps=0)
        with pytest.raises(RuntimeError, match=r"violating the guarantee of forward progress"):
            _go(store, cfg, OverrunExecutor())

    def test_results_step_mismatch_is_rejected(self, store):
        """结果的 step 字段错开时,记账处抛 RuntimeError(step 校验没有被废掉)。"""

        class MismatchExecutor(MockExecutor):
            def run_sft_stage(self, batches, *, first_step, trained_slot, prior_batches):
                return [
                    StepResult(first_step + i + 100, "sft", {"sft_loss": 0.1})
                    for i in range(len(batches))
                ]

        cfg = _batch_cfg(sft_steps=3, grpo_steps=0)
        with pytest.raises(RuntimeError, match=r"executor returned step \d+ at index \d+, expected \d+"):
            _go(store, cfg, MismatchExecutor())
```

- [ ] **Step 6: 跑通**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_mt_orchestrator.py::TestStagedExecutor -q -p no:cacheprovider --color=no -W error
```

期望:**6 passed**。

再跑整个文件,确认单步路径与 GRPO 路径的既有测试一字未改地全过:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_mt_orchestrator.py -q -p no:cacheprovider --color=no -W error
```

期望:该文件全部通过(既有用例 + 6)。

- [ ] **Step 7: Commit**

```bash
git add src/mindtrainer/executor.py src/mindtrainer/orchestrator.py tests/test_mt_orchestrator.py
git commit -m "feat(mindtrainer): 分阶段执行器协议(run_sft_stage、stage_end、prior_batches)"
```

---

## Task 6: 守护进程的延后通道(设计 §7.3、§7.4;Ruling 16)

**Files:**
- Modify: `src/controlplane/daemon.py`(新增 `DeferralBook`、`DeferredExhaustedError`;`reconcile_once` 接住 `DeferredError`;`run_daemon` 持有退避表)
- Modify: `src/mindtrainer/dispatch.py`(通用异常分支之前加 `except DeferredError: raise`)
- Create: `tests/test_daemon_deferred.py`
- Modify: `tests/test_mt_dispatch.py`(末尾追加两条)

**Interfaces:**
- Consumes: Task 1 的 `controlplane.deferred.DeferredError`
- Produces: `controlplane.daemon.DeferralBook`、`DeferredExhaustedError`;`reconcile_once(..., deferrals=None)`

**本任务是 Task 7 的前置。** `compute` 的四个异常继承 `DeferredError`,而现状是守护进程只接住 `DirectiveFailedError`,其余异常让进程退出,仓库里没有监督脚本。不先落地这条通道,Task 7 的预检一被占用就是守护进程崩溃。本任务动 `src/controlplane/daemon.py`,所以放宽了上面"只改文件表里的文件"一条(见 Global Constraints)。

**已核实的事实(实现者不要再猜):**

| 事实 | 依据 |
|---|---|
| `apply_action` 只在 `dispatch_directive` 分支接 `DirectiveFailedError`;闭环处理器与评估器的异常原样上抛 | `daemon.py:245` 起 |
| `reconcile_once` 对 `reconcile` 给出的每个动作调 `apply_action`,返回完整动作列表 | `daemon.py:341` 起 |
| `run_daemon` 在 `acquire_repo_lock` 之内循环 `reconcile_once` + `time.sleep` | `daemon.py:361` 起 |
| 派发器的通用异常分支见到"记录已 `failed`"会把任何异常包成 `DirectiveFailedError`(终态);记录是 `pending` 才原样重抛 | `dispatch.py:236-243` |
| 仓库里 `logging` 只有 `simulator/realtime/servicer.py` 在用;`controlplane` 没有先例 | `grep` |

**设计要点。** 动作键是 `(动作种类, 目标 id)`。`DeferredError` 被接住时:不写树、状态不动;进入退避期,期内同一动作被跳过,同一 tick 的其他动作照常处理。退避秒数取 `retry_after_seconds` 与默认 30 秒的较大者,封顶 900 秒。**连续**延后同一动作超过上限(默认 20 次)就抛 `DeferredExhaustedError`,守护进程带着原因退出:这是 Task 5/设计 G5 里"续训预算"的对应物,否则一台被别人长期占着的 GPU 会让守护进程永远空转。动作一旦成功就清零。`DeferredExhaustedError` 刻意不继承 `DeferredError`,否则会被自己接住。

- [ ] **Step 1: 写失败的测试**

```python path=tests/test_daemon_deferred.py
"""守护进程的延后通道(真实后端设计 §7.3):DeferredError 不写树、状态不动、带退避、有重试上限。"""
from __future__ import annotations

import pytest

from controlplane.daemon import (
    DeferralBook, DeferredExhaustedError, reconcile_once, run_daemon,
)
from controlplane.deferred import DeferredError
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import run_dir


def _put(store, run_id):
    env = Envelope(
        id=run_id, type="directive", version=1, created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1,
        payload={"target_dimension": "tom", "recipe_ref": ""},
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "human: write directive")


def _status(store, run_id):
    return store.read_product(f"{run_dir(run_id)}/directive.yaml").status


def _head(store):
    return store._git("rev-parse", "HEAD").strip()


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _busy(calls, *, retry_after=None):
    def dispatcher(s, d):
        calls.append(d.id)
        raise DeferredError("gpu busy", retry_after_seconds=retry_after)
    return dispatcher


def test_a_deferred_dispatch_writes_nothing_and_keeps_the_directive_pending(store):
    _put(store, "run-1")
    before, calls = _head(store), []
    reconcile_once(store, dispatcher=_busy(calls), deferrals=DeferralBook(clock=_Clock()))
    assert calls == ["run-1"]
    assert _status(store, "run-1") == "pending" and _head(store) == before


def test_without_a_book_the_error_is_caught_but_not_backed_off(store):
    _put(store, "run-1")
    calls = []
    reconcile_once(store, dispatcher=_busy(calls))
    reconcile_once(store, dispatcher=_busy(calls))
    assert calls == ["run-1", "run-1"] and _status(store, "run-1") == "pending"


def test_the_action_is_skipped_during_backoff_and_retried_after(store):
    _put(store, "run-1")
    clock, calls = _Clock(), []
    book = DeferralBook(clock=clock, default_backoff_s=30)
    reconcile_once(store, dispatcher=_busy(calls), deferrals=book)
    clock.now += 29
    reconcile_once(store, dispatcher=_busy(calls), deferrals=book)
    assert calls == ["run-1"]
    clock.now += 2
    reconcile_once(store, dispatcher=_busy(calls), deferrals=book)
    assert calls == ["run-1", "run-1"]


def test_retry_after_raises_the_wait_and_the_cap_lowers_it(store):
    _put(store, "run-1")
    clock = _Clock()
    book = DeferralBook(clock=clock, default_backoff_s=30, max_backoff_s=900)
    reconcile_once(store, dispatcher=_busy([], retry_after=120), deferrals=book)
    assert book.next_try(("dispatch_directive", "run-1")) == 1000.0 + 120
    clock.now += 121
    reconcile_once(store, dispatcher=_busy([], retry_after=99999), deferrals=book)
    assert book.next_try(("dispatch_directive", "run-1")) == clock.now + 900


def test_other_directives_are_handled_while_one_is_backed_off(store):
    _put(store, "run-1")
    _put(store, "run-2")
    seen = []

    def dispatcher(s, d):
        seen.append(d.id)
        if d.id == "run-1":
            raise DeferredError("busy")
        # run-2 正常返回,由调和循环置 executed

    reconcile_once(store, dispatcher=dispatcher, deferrals=DeferralBook(clock=_Clock()))
    assert sorted(seen) == ["run-1", "run-2"]
    assert _status(store, "run-1") == "pending" and _status(store, "run-2") == "executed"


def test_a_success_clears_the_streak(store):
    _put(store, "run-1")
    clock = _Clock()
    book = DeferralBook(clock=clock, default_backoff_s=1, max_consecutive=2)
    for _ in range(2):
        reconcile_once(store, dispatcher=_busy([]), deferrals=book)
        clock.now += 5
    book.clear(("dispatch_directive", "run-1"))
    reconcile_once(store, dispatcher=_busy([]), deferrals=book)  # 不抛:连续计数已清零


def test_the_streak_cap_turns_into_a_human_problem(store):
    _put(store, "run-1")
    clock = _Clock()
    book = DeferralBook(clock=clock, default_backoff_s=1, max_consecutive=3)
    for _ in range(3):
        reconcile_once(store, dispatcher=_busy([]), deferrals=book)
        clock.now += 5
    with pytest.raises(DeferredExhaustedError, match=r"dispatch_directive run-1.*3"):
        reconcile_once(store, dispatcher=_busy([]), deferrals=book)
    assert _status(store, "run-1") == "pending"


def test_the_exhausted_error_is_not_a_deferred_error():
    assert not issubclass(DeferredExhaustedError, DeferredError)


def test_run_daemon_owns_the_book_and_backs_off_across_ticks(store):
    _put(store, "run-1")
    calls = []
    run_daemon(store, 0.0, max_ticks=3, dispatcher=_busy(calls))
    assert calls == ["run-1"]  # 默认退避 30 秒:后两个 tick 都在退避期内
    assert _status(store, "run-1") == "pending"


def test_run_daemon_with_a_success_after_a_deferral_resets_the_streak(store):
    _put(store, "run-1")
    attempts = {"n": 0}

    def dispatcher(s, d):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise DeferredError("busy")

    book = DeferralBook(clock=_Clock(), default_backoff_s=0)
    reconcile_once(store, dispatcher=dispatcher, deferrals=book)
    reconcile_once(store, dispatcher=dispatcher, deferrals=book)
    assert _status(store, "run-1") == "executed"
    assert book.streak(("dispatch_directive", "run-1")) == 0
```

在 `tests/test_mt_dispatch.py` 末尾追加:

```python path=tests/test_mt_dispatch.py mode=append
def test_a_deferred_error_from_the_executor_leaves_the_directive_pending_and_is_absorbed(store):
    """延后异常(资源暂时不可用)不是指令的错:守护进程接住,指令保持 pending,下一次再派发。"""
    from controlplane.daemon import DeferralBook
    from controlplane.deferred import DeferredError

    class BusyExecutor(MockExecutor):
        def load(self, checkpoint_ref):
            raise DeferredError("gpu busy")

    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})
    disp = make_mindtrainer_dispatcher(executor_factory=BusyExecutor)
    reconcile_once(store, dispatcher=disp, deferrals=DeferralBook())  # 不抛
    assert store.read_product(f"{run_dir(RUN)}/directive.yaml").status == "pending"
    assert not store.exists(candidate_checkpoint_file(RUN))


def test_a_deferred_error_is_never_rewrapped_as_a_permanent_failure(store, monkeypatch):
    """兜底分支见到"记录已 failed"会把异常包成 DirectiveFailedError(终态)。DeferredError 不在此列:
    它说的是"稍后再试",不论记录当时是什么状态,都要原样上抛。"""
    from controlplane.deferred import DeferredError
    from mindtrainer import dispatch as dispatch_module
    from mindtrainer.store_adapter import read_training_run, write_training_run

    _directive(store, payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict()})

    def fake_resume_or_run(cfg, *, store, run_id, **kwargs):
        from mindtrainer.datamodel import TrainingRun
        write_training_run(store, TrainingRun(run_id=run_id, config_hash="h", status="failed",
                                              failure={"category": "T2", "point": "x", "detail": "y"}))
        raise DeferredError("busy")

    monkeypatch.setattr(dispatch_module, "resume_or_run", fake_resume_or_run)
    directive = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    with pytest.raises(DeferredError):
        make_mindtrainer_dispatcher()(store, directive)
    assert read_training_run(store, RUN).status == "failed"  # 记录没人动,指令也没被判死
```

- [ ] **Step 2: 跑,确认失败**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_daemon_deferred.py tests/test_mt_dispatch.py -q -p no:cacheprovider --color=no -W error -k "deferred"
```

期望:`test_daemon_deferred.py` 收集阶段 `ImportError: cannot import name 'DeferralBook' from 'controlplane.daemon'`;`test_mt_dispatch.py` 里的第二条因兜底分支把 `DeferredError` 包成 `DirectiveFailedError` 而失败。

- [ ] **Step 3: 实现**

`src/controlplane/daemon.py`:导入段加 `import logging` 与 `from controlplane.deferred import DeferredError`(按字母序放在 `controlplane.envelope` 之前);在 `_MODEL_FILE` 之后加:

```python
_log = logging.getLogger("controlplane.daemon")

ActionKey = tuple[str, str]  # (动作种类, 目标 id)


class DeferredExhaustedError(Exception):
    """同一个动作连续延后达到上限:瞬态失败拖成了常态,该人来看。

    刻意不继承 DeferredError:它要逃出调和循环、让守护进程带着原因退出,而不是再被接住退避。
    树里没有写任何东西,指令与候选仍是原状态;人处理完原因后重启守护进程即可。"""


class DeferralBook:
    """延后动作的退避表(真实后端设计 §7.3)。只在内存里,守护进程重启后所有动作立即可重试。

    退避秒数取 retry_after_seconds 与 default_backoff_s 的较大者,再封顶 max_backoff_s;
    连续延后次数达到 max_consecutive,下一次延后就抛 DeferredExhaustedError。动作成功即清零。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        default_backoff_s: float = 30.0,
        max_backoff_s: float = 900.0,
        max_consecutive: int = 20,
    ) -> None:
        self._clock = clock
        self._default = default_backoff_s
        self._max_backoff = max_backoff_s
        self._max_consecutive = max_consecutive
        self._next: dict[ActionKey, float] = {}
        self._streak: dict[ActionKey, int] = {}

    def due(self, key: ActionKey) -> bool:
        return self._clock() >= self._next.get(key, 0.0)

    def next_try(self, key: ActionKey) -> float | None:
        return self._next.get(key)

    def streak(self, key: ActionKey) -> int:
        return self._streak.get(key, 0)

    def record(self, key: ActionKey, exc: DeferredError) -> float:
        streak = self._streak.get(key, 0) + 1
        if streak > self._max_consecutive:
            raise DeferredExhaustedError(
                f"{key[0]} {key[1]} was deferred {self._max_consecutive} times in a row; "
                f"last reason: {exc.reason}"
            ) from exc
        self._streak[key] = streak
        wait = min(max(float(exc.retry_after_seconds or 0), self._default), self._max_backoff)
        self._next[key] = self._clock() + wait
        return self._next[key]

    def clear(self, key: ActionKey) -> None:
        self._next.pop(key, None)
        self._streak.pop(key, None)
```

把 `reconcile_once` 整个换成:

```python
def reconcile_once(
    store: GitStore,
    *,
    dispatcher: Dispatcher | None = None,
    handlers: Mapping[str, Handler] | None = None,
    evaluator: Evaluator | None = None,
    accept_mock_evidence: bool = False,
    deferrals: DeferralBook | None = None,
) -> list[Action]:
    """一次性调和：读状态 → reconcile → 执行非人工动作。返回本轮动作列表。
    dispatcher 缺省为桩;注入后只替换"下发 directive"这一步,其余动作不变。
    handlers 的键就是启用的闭环动作(闭环集成设计 §3.3);没有处理器的动作种类不会出现在
    返回列表里。evaluator 缺省为诊断桩;注入后只替换 request_diagnostic 这一步。

    任何动作抛 DeferredError(资源暂时不可用,真实后端设计 §7.3):不写树、状态不动,同一 tick 的
    其他动作照常处理。传入 deferrals 时该动作进退避期(期内被跳过)并累计连续次数;不传则只接住、
    不退避,行为与引入延后通道之前相比只多了"不再让进程退出"。返回的仍是 reconcile 给出的完整动作列表。"""
    enabled = _check_handlers(handlers)
    state = read_tree_state(store, enabled=enabled, accept_mock_evidence=accept_mock_evidence)
    actions = reconcile(state)
    for action in actions:
        key = (action.kind, action.target_id)
        if deferrals is not None and not deferrals.due(key):
            continue
        try:
            apply_action(store, action, dispatcher=dispatcher, handlers=handlers, evaluator=evaluator)
        except DeferredError as exc:
            retry_at = deferrals.record(key, exc) if deferrals is not None else None
            _log.warning(
                "deferred %s %s: %s (%s); next try at %s",
                action.kind, action.target_id, exc.reason, type(exc).__name__, retry_at,
            )
        else:
            if deferrals is not None:
                deferrals.clear(key)
    return actions
```

`run_daemon` 里取锁之后创建退避表并传入:

```python
    with acquire_repo_lock(store.repo_root):
        deferrals = DeferralBook()  # 退避表归守护进程持有,只在内存里
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            reconcile_once(
                store, dispatcher=dispatcher, handlers=handlers, evaluator=evaluator,
                accept_mock_evidence=accept_mock_evidence, deferrals=deferrals,
            )
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            time.sleep(interval_s)
```

`src/mindtrainer/dispatch.py`:导入 `from controlplane.deferred import DeferredError`(紧跟 `from controlplane.daemon import ...` 之后),在 `dispatch` 函数最后一个 `except Exception as exc:` 之前加:

```python
        except DeferredError:
            # 资源暂时不可用(真实后端设计 §7.3):不是指令的错,不论记录当时是什么状态都原样上抛,
            # 交给守护进程的延后通道;不能落进下面的兜底分支被包成终态的 DirectiveFailedError。
            raise
```

`src/controlplane/deferred.py` 的 docstring 补一句(Task 1 写的是"按退避重试"):

```text
守护进程接住它之后不写树、状态不动、按退避重试;连续延后达到上限就带原因退出(见 daemon.DeferredExhaustedError)。
```

- [ ] **Step 4: 跑通**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_daemon_deferred.py tests/test_mt_dispatch.py tests/test_mt_dispatch_synthesis.py tests/test_daemon.py tests/test_daemon_dispatcher.py tests/test_daemon_closed_loop_seams.py tests/test_repo_lock.py tests/test_e2e_loop.py -q -p no:cacheprovider --color=no -W error
```

期望:全部通过;新增 **12** 个(`test_daemon_deferred.py` 10 个,`test_mt_dispatch.py` 2 个),既有用例一行不改。

**变异检查(已在隔离拷贝里做过,实施者不必重做,但改动实现后应能被测试抓住):** 去掉 `except DeferredError`、去掉退避期跳过、去掉连续次数上限、忽略 `retry_after_seconds` 与封顶、成功后不清零、`run_daemon` 不传退避表、去掉派发器的 `except DeferredError: raise`,七处各自都有测试变红。

- [ ] **Step 5: Commit**

```bash
git add src/controlplane/daemon.py src/controlplane/deferred.py src/mindtrainer/dispatch.py tests/test_daemon_deferred.py tests/test_mt_dispatch.py
git commit -m "feat(controlplane): 守护进程的延后通道(接住 DeferredError,带退避与连续次数上限)"
```

---

## Task 7: RollExecutor(设计 §5:远端数据渲染、SSH 作业控制、指标回收)

**Files:**
- Create: `src/mindtrainer/roll_executor.py`(约 320 行)
- Create: `tests/compute_fixtures.py`(约 180 行:`FakeClient`、端点夹具)
- Create: `tests/test_mt_roll_executor.py`(约 320 行)
- Modify: `src/mindtrainer/executor.py`(`TrainingExecutor` 协议加可选 `preflight()`;`_is_inference_format` 不动,留给 Task 8)
- Modify: `src/mindtrainer/orchestrator.py`(`resume_or_run` 加预检与跨模式检查;`run_training` 加 `supported_phases` 检查;两处 `_DETERMINISTIC` 与 `executor.load` 的位置**不动**,见 Step 2)
- Modify: `tests/test_mt_orchestrator.py`(末尾追加 `TestRollExecutorIntegration`)
- Modify: `pyproject.toml`(`[tool.pytest.ini_options]` 新增 `markers`)

**Interfaces:**
- Consumes: `compute.client.ComputeClient`(`run_job`、`status`、`put`、`hash_tree`、`preflight`)、`compute.client.JobSpec`、`compute.errors.{HelperError,TransportError,RemoteJobFailedError}`、`compute.endpoint.ComputeEndpoint`、`simulator.datamodel.Episode`、`mindtrainer.datamodel.StepResult`
- Produces: `RollExecutor`(实现 `StagedExecutor`:`load`、`sft_step`、`grpo_opd_step`、`save_checkpoint`、`checkpoint_intact`、`run_sft_stage`、`stage_end`,加可选 `preflight`,加 `supported_phases`、`checkpoint_type` 两个类属性)、`MetricsMissingError`

**本任务不改**:GRPO 路径(真实后端不支持,由 `supported_phases` 挡住)、interact 模式、行数与截断预检(V2/V14 未核实,见下方不做清单)、转换器(Task 8)。

**已核实的事实(实现者不要再猜):**

| 事实 | 依据 |
|---|---|
| `ComputeClient` 的方法只有 `ensure_helper`、`start`、`status`、`tail`、`stop`、`put`、`hash_tree`、`lease_acquire`、`lease_release`、`lease_holder`、`gpu_process_count`、`preflight`、`run_job`。**没有** `acquire_lease`、`release_lease`、`fetch_file`、`get`、`rename` | `compute/client.py` |
| `run_job(job_id, spec, *, secret=None, tail_lines=40) -> JobResult`,它自己取租约、`start`、轮询、释放租约;非零退出抛 `RemoteJobFailedError`,超时先 `stop` 再抛 `RemoteJobTimeoutError`,租约被占抛 `ComputeBusyError` | `client.py:188` |
| `JobSpec(argv: tuple[str,...], cwd=None, env={}, secret_env=None)` | `client.py:27` |
| `JobResult(exit_code: int, log_tail: str)`;`JobStatus(state, exit_code, spec_sha256)`,`state ∈ {missing,running,exited,lost}` | `client.py:38,45` |
| `hash_tree(path, mode) -> str`,`mode ∈ {"sampled","full"}`;目录不存在时助手回 `not_found`,客户端翻成 `HelperError(code="not_found")` | `client.py:142`、`job_tool.py:480` |
| `put(path, data: bytes, *, executable=False)`,内容寻址、原子写、自己建父目录 | `client.py:138`、`job_tool.py:447` |
| `preflight(*, own_holder=None)` 做 GPU/内存/磁盘检查,不过抛 `ComputeBusyError`;首次调用会上传助手 | `client.py:165`(Ruling 7) |
| `Episode(id, scenario_id, role_assignment, turns, terminated_reason)`;`TurnRecord(turn_index, speaker_slot, utterance, inner_thought=None, activation_trace=None)`。**没有** `messages` / `role` / `content` | `simulator/datamodel.py:151,181` |
| `StepResult(step, phase, metrics)` 是 `frozen=True` | `datamodel.py:75` |
| `CheckpointCorruptError` 在 `mindtrainer/executor.py:23`,**不**继承 `DeferredError` | 读源码 |
| `_DETERMINISTIC` 两处:`orchestrator.py:36`(五项)、`dispatch.py:36`(六项,多 `NeedsHumanError`);本任务不改 | 读源码 |
| `executor.load(...)` 在 `try` **之外**(`orchestrator.py:163`);本任务不改。`NeedsHumanError` 经 `dispatch.py` 的元组映射成 `DirectiveFailedError(category="T3")`,记录不改 | 读源码 |
| `resume_or_run` 的 `decision.action ∈ resume.ACTIONS = {"noop","resume","restart","escalate"}`(`resume.py:9`);`checkpoint_intact` 在 `decide_resume` 之前被调 | `orchestrator.py:267-284` |
| `pyproject.toml` 的 `[tool.pytest.ini_options]` 只有 `pythonpath`、`testpaths`,**没有** `markers` 键 | 读文件 |
| `tests/` 无 `conftest.py` 也无 `__init__.py`;夹具用 `from compute_fixtures import ...` | `tests/factory_fixtures.py` 用法 |

**V11 占位的部分(本任务只搭骨架,主机验证后补齐):** 行数必须恒等于 `G` 的预检(V2、V14)、tokenizer 实测截断预检(V10)、ROLL 结构文件齐全性检查(V15)、`run_sft_validation.py` 的真实参数语义(V2、V9)。每处在代码里以 `# V__:` 注释标明,并在不做清单里列出。

- [ ] **Step 1: `executor.py` 的 `TrainingExecutor` 协议加可选 `preflight()`**

在 `checkpoint_intact` 之后插入:

```python
    def preflight(self) -> None:
        """可选预检(真实后端设计 §3.3、§7.3):资源不可用时抛 ComputeBusyError
        (DeferredError 子类),由守护进程延后重试,不耗 resume_attempts 预算。

        无参:端点与租约标识都由执行器自己持有。`MockExecutor` 不实现它,
        `resume_or_run` 用 getattr 探测,缺省视为通过。"""
        ...
```

- [ ] **Step 2: 存点损坏保持现状,两处 `_DETERMINISTIC` 一行不改**

原计划这里要把 `CheckpointCorruptError` 加进两处 `_DETERMINISTIC`,并把 `executor.load`
移进 `try`,好让它判 failed(T2)。**不做**,理由:

`30-mindtrainer.md` 的失败表写明"存点损坏或无法确认"是 **T3**:抛 `NeedsHumanError`、
指令判 failed、**记录不改**。改成 T2 会把记录写成 `failed`(永久终态),同一件事经
`decide_resume` 与经 `load` 两条路径就会得到两种分级;已落地的 mock 坏基座路径也会从
"保持 pending 等人看"变成终态。真实后端这一侧沿用现状:`RollExecutor.checkpoint_intact`
核对完成且确实不符时返回 False,`decide_resume` 据此升级成 `NeedsHumanError`(T3、记录不改),
与 `MockExecutor` 同一条路径;`RollExecutor.load` 抛的 `CheckpointCorruptError` 与
`MockExecutor.load` 抛的一样,不在任何元组里、`load` 在 `try` 之外(`orchestrator.py:163`),
原样上抛、run 停在 pending。两种后端行为一致,这一轮不动它。

所以本任务不动 `_DETERMINISTIC`,也不动 `executor.load` 的位置。`tests/test_compute_scaffold.py`
里"延后异常不在确定性集合里"那条断言照旧成立。

- [ ] **Step 3: `run_training` 加 `supported_phases` 检查**

真实执行器只支持 SFT。配置要求 GRPO 步而执行器不支持时,是确定性失败,要在动执行器之前就报(设计 §5.1)。在 `validate_training_config` 那段之后、算起点之前插入:

```python
    phases = getattr(executor, "supported_phases", None)
    if phases is not None:
        wanted = {stage for stage, steps in (("sft", cfg.sft_steps), ("grpo_opd", cfg.grpo_steps)) if steps > 0}
        unsupported = sorted(wanted - set(phases))
        if unsupported:
            reason = (
                f"the executor supports only {sorted(phases)} but the config asks for {unsupported}; "
                "real backends need a training_defaults with grpo_steps: 0"
            )
            write_training_run(store, _failed(base, "config", InvalidTrainingConfigError(reason)))
            raise InvalidTrainingConfigError(reason)
```

- [ ] **Step 4: `resume_or_run` 加预检与跨模式检查**

设计 §6.1 要求续训前先比对后端类型,且这道检查必须在 `checkpoint_intact` **之前**——否则 `MockExecutor.checkpoint_intact` 对 `ssh://` 引用返回 False,会抢先升级成 `NeedsHumanError`,报错理由写成"存点损坏",与真实原因"后端模式不一致"不符。

跨模式续训判 **T3**:抛 `NeedsHumanError`、记录不改。这是装配开关与已有记录对不上,要人来决定是换回原来的开关还是重写指令;写成 `failed`(终态)会让这条 run 再也不能被续上。理由字段写明真实原因。

设计 §7.3 要求预检在 `decide_resume` 判定之后、`run_training` 之前,且 `noop` 与 `escalate` 不触发。

把 `resume_or_run` 改成:

```python
    existing = read_training_run(store, run_id)
    kwargs = dict(
        store=store, run_id=run_id, make_sources=make_sources, attribution=attribution,
        executor=executor, converter=converter,
        metric_stream_refs=metric_stream_refs, on_step=on_step,
    )
    preflight = getattr(executor, "preflight", None)
    if existing is None:
        if preflight is not None:
            preflight()  # ComputeBusyError 走延后通道:此时 run 记录一个字节都没写
        return run_training(cfg, **kwargs)
    _check_backend_match(store, existing, cfg, executor)
    intact = (
        executor.checkpoint_intact(existing.last_checkpoint_ref)
        if existing.last_checkpoint_ref is not None else None
    )
    decision = decide_resume(existing, cfg, checkpoint_intact=intact)
    if decision.action == "noop":
        return existing
    if decision.action == "escalate":
        raise NeedsHumanError(decision.reason)
    if preflight is not None:
        preflight()
    return run_training(cfg, resume_from=existing, **kwargs)
```

并在 `resume_or_run` 之前加这个辅助(不写任何记录,和 `decide_resume` 的 `escalate` 分支一样直接抛 `NeedsHumanError`):

```python
def _check_backend_match(
    store: GitStore, existing: TrainingRun, cfg: TrainingConfig, executor: TrainingExecutor
) -> None:
    """续训前比对存点的后端类型与当前执行器(设计 §6.1)。

    不一致是装配开关的问题,不是存点损坏。这道检查必须在 checkpoint_intact 之前:
    另一种后端的 checkpoint_intact 会对陌生引用返回 False,同样升级成 T3,
    但理由字段会写"存点损坏",让运维去查远端磁盘而不是查装配。这里也是 T3,只是理由说对。
    """
    ref = existing.last_checkpoint_ref
    want = getattr(executor, "checkpoint_type", None)
    if ref is None or want is None:
        return
    got = (ref.fetch_hint or {}).get("type")
    if got is not None and got != want:
        reason = (
            f"run {existing.run_id} has a {got!r} checkpoint but the executor produces {want!r}; "
            "resuming across backend modes is not supported"
        )
        raise NeedsHumanError(reason)
```

- [ ] **Step 5: `pyproject.toml` 新增 `markers`**

`[tool.pytest.ini_options]` 现在只有两行,所以是**新增键**而不是追加列表项:

```toml
[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
markers = [
    "slow: 真起子进程或多段作业的测试,用 -m 'not slow' 跳过",
]
```

`-W error` 下未注册的标记会变成错误,所以这一步必须在写 `@pytest.mark.slow` 之前做。

- [ ] **Step 6: 新建 `src/mindtrainer/roll_executor.py`**

```python path=src/mindtrainer/roll_executor.py
"""RollExecutor:把 ROLL 的整段 SFT 作业接到编排器的阶段协议上(真实后端设计 §5)。

它住在 `mindtrainer` 而不是 `compute`:设计 §5 把它放这里,而且它要认识 Episode、
StepResult 与 CheckpointCorruptError,这些都是训练侧的概念。依赖方向是
`mindtrainer -> compute`,`compute` 自己仍然不认识训练(设计 §3 开头)。

只支持 SFT(`supported_phases`)。逐步方法一律响亮失败:真实执行器只能整段跑,
被逐步调用说明编排器被改坏了。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from compute.client import ComputeClient, JobSpec
from compute.endpoint import ComputeEndpoint
from compute.errors import HelperError, TransportError
from controlplane.envelope import BigRef
from mindtrainer.datamodel import StepResult
from mindtrainer.executor import CheckpointCorruptError
from mindtrainer.training_config import InvalidTrainingConfigError
from simulator.datamodel import Episode

LAUNCHER_VERSION = "v0"  # 进 job_id 的哈希;改了启动命令的拼法就要改它
METRIC_PREFIX = "metrics_tag:"  # V2 未核实:ROLL 的逐步指标行前缀,主机验证后改这里
MAX_ATTEMPTS = 8  # 同一 job_id 下的尝试序号上限,与 max_resume_attempts 分开记账(设计 §5.2)


class MetricsMissingError(RuntimeError):
    """远端作业成功了,但拿不到对账得上的逐步指标。

    瞬态:重试只重新解析同一批产物,不重训,所以**不**进两个 _DETERMINISTIC
    (设计 §7.3、§7.4)。计入 resume_attempts。
    """


def _render_rows(episode: Episode, trained_slot: str) -> list[dict]:
    """一个剧集 -> 多行 {"instruction", "output"}(设计 §5.2 第 1 步,多轮监督)。

    被训练角色位说了 T 句就出 T 行:第 t 行的 output 是它的第 t 句,instruction 是
    这句之前的全部发言按固定模板拼成的文本。每句都被监督,上下文只作输入。

    用行展开而不是逐轮 loss mask,是因为 sft_lora.yaml 只有 prompt_key/response_key
    这一对键,表达不了逐轮掩码。代价:同一段上下文会被重复前向,token 量随 T 平方增长。
    语义上等价于逐轮掩码,计算上不等价(设计 §5.2)。
    """
    rows: list[dict] = []
    history: list[str] = []
    for turn in episode.turns:
        line = f"{turn.speaker_slot}: {turn.utterance}"
        if turn.speaker_slot == trained_slot:
            rows.append({"instruction": "\n".join(history), "output": turn.utterance})
        history.append(line)
    return rows


@dataclass
class RollExecutor:
    """ROLL SSH 作业的分阶段执行器(设计 §5)。

    client:已装配好的 ComputeClient(由 factory.compute_config.build_compute_client 造)。
    endpoint:同一个端点的冻结配置,路径与阈值都从它取。
    run_id:进 job_id 的哈希,避免不同 run 的同样数据撞同一作业目录。
    """

    client: ComputeClient
    endpoint: ComputeEndpoint
    run_id: str

    supported_phases = frozenset({"sft"})  # 设计 §5.1:真实后端本轮只做 SFT
    checkpoint_type = "roll_ssh"  # 跨模式续训检查比对的就是它(设计 §6.1)

    _base_uri: str | None = field(default=None, init=False, repr=False)
    _stage_ref: BigRef | None = field(default=None, init=False, repr=False)

    # ---------- 协议:资源与存点 ----------

    def preflight(self) -> None:
        """GPU、内存、磁盘检查。不取租约、不终止任何进程(设计 §3.3、D8)。"""
        self.client.preflight()

    def load(self, checkpoint_ref: BigRef) -> None:
        """只做校验与记录,不占 GPU(设计 §5.1)。

        接受两类引用:指向 base_model_path 的起点基座,和本设计产出的训练存点。
        mock:// 引用抛 CheckpointCorruptError:重试不会好,但它不在任何 _DETERMINISTIC 里、`load` 在 `try` 之外,
        所以和 MockExecutor 的损坏存点一样原样上抛、记录保持 pending(本任务不改这条,见 Task 7 Step 2)。
        适配器导出引用抛 InvalidTrainingConfigError:真实转换产出的是
        qwen4_exp_vllm_2d 布局,不是 HF 训练起点,所以本轮只支持单轮(设计 §5.1)。
        """
        hint = checkpoint_ref.fetch_hint or {}
        if hint.get("format") == "qwen4_exp_vllm_2d":
            raise InvalidTrainingConfigError(
                "this checkpoint is an adapter export, not a training start point; "
                "real backends support one round only, so point base_checkpoint_ref at base_model_path"
            )
        if not checkpoint_ref.uri.startswith("ssh://"):
            raise CheckpointCorruptError(
                f"RollExecutor cannot load {checkpoint_ref.uri!r}: it expects an ssh:// reference"
            )
        path = self._remote_path(checkpoint_ref.uri)
        if path != self.endpoint.base_model_path and not self.checkpoint_intact(checkpoint_ref):
            raise CheckpointCorruptError(f"the checkpoint at {checkpoint_ref.uri!r} is not intact")
        self._base_uri = checkpoint_ref.uri
        self._stage_ref = None

    def checkpoint_intact(self, checkpoint_ref: BigRef) -> bool:
        """sampled 口径核对 manifest(设计 §6.1)。

        传输错误一律上抛 TransportError,**不**返回 False:False 会让 decide_resume
        升级成 NeedsHumanError(T3,永久),一次网络抖动不该把 run 判死(设计 §5.1)。
        只有核对完成且确实不符才返回 False。
        """
        if not checkpoint_ref.uri.startswith("ssh://"):
            return False
        try:
            digest = self.client.hash_tree(self._remote_path(checkpoint_ref.uri), "sampled")
        except HelperError as exc:
            if exc.code == "not_found":
                return False  # 目录真的不在:核对完成,结论是不完整
            raise
        # V15:还要检查 ROLL 的结构文件齐全(tracker、迭代目录、各 worker 的 RNG、
        # scheduler、适配器配置)。只能证明结构完整,不证明张量没坏。
        return digest == checkpoint_ref.sha256

    def save_checkpoint(self, tag: str) -> BigRef:
        """ROLL 的存点在阶段内部就写出了,这里只把它封成 BigRef,不另外触发保存。

        循环为空时(续训时 start == total,崩在最终存点之后、置 executed 之前)本次
        没有阶段产出,`orchestrator` 仍会无条件调用一次。此时回认 load 记下的那份,
        并校验它仍然完整;认不出就抛 CheckpointCorruptError,绝不返回一个没有对应
        目录的引用(设计 §5.1)。
        """
        if self._stage_ref is not None:
            return self._stage_ref
        if self._base_uri is None:
            raise CheckpointCorruptError("save_checkpoint was called before load")
        path = self._remote_path(self._base_uri)
        try:
            digest = self.client.hash_tree(path, "sampled")
        except HelperError as exc:
            if exc.code == "not_found":
                raise CheckpointCorruptError(
                    f"no checkpoint was produced in this stage and {self._base_uri!r} is gone"
                ) from None
            raise
        return BigRef(
            uri=self._base_uri,
            sha256=digest,
            size_bytes=0,
            fetch_hint={"type": self.checkpoint_type, "format": "roll_lora_legacy"},
        )

    # ---------- 协议:逐步方法一律拒绝 ----------

    def sft_step(self, episodes, *, trained_slot):  # noqa: ARG002
        raise RuntimeError("RollExecutor runs whole stages; call run_sft_stage instead of sft_step")

    def grpo_opd_step(self, rewarded, *, opd_coefficient, teacher_model_ref):  # noqa: ARG002
        raise RuntimeError("RollExecutor does not support GRPO; supported_phases is ('sft',)")

    # ---------- 协议:阶段 ----------

    def stage_end(self, batches, *, first_step, limit):  # noqa: ARG002
        """恒返回 limit。一个作业只有一份 gradient_accumulation 配置,切细不能让不同
        行数的步共存(设计 §5.2 第 2 步),所以本轮不切细。接口保留以备磁盘约束。"""
        return limit

    def run_sft_stage(
        self,
        batches: list[list[Episode]],
        *,
        first_step: int,
        trained_slot: str,
        prior_batches: list[list[Episode]],
    ) -> list[StepResult]:
        """渲染数据、推 job_id、跑整段作业、回收逐步指标(设计 §5.2)。"""
        if self._base_uri is None:
            raise RuntimeError("run_sft_stage was called before load")
        last_step = first_step + len(batches)

        # 1. 数据按步序排列:先前缀([0, first_step)),再本段。ROLL 的 DataLoader 是
        #    shuffle=False、drop_last=True,按文件顺序取整批,所以行序必须就是步序。
        #    V14:这里还要加"每步行数恒等于 G"与 tokenizer 实测截断两道预检,
        #    不过不补齐也不丢行——drop_last 会静默丢,所以预检要替 ROLL 先挡住。
        rows: list[dict] = []
        for batch in list(prior_batches) + list(batches):
            for episode in batch:
                rows.extend(_render_rows(episode, trained_slot))
        payload = "".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in rows)
        data = payload.encode("utf-8")

        # 2. 作业身份。run_id 与起点存点必须进哈希,否则起点不同而数据相同的两次续训
        #    会撞同一目录(设计 §5.2 第 4 步)。last_step 是开区间终点。
        job_id = hashlib.sha256(
            "\0".join([
                self.run_id, self._base_uri, str(first_step), str(last_step),
                hashlib.sha256(data).hexdigest(), self.endpoint.sft_template, LAUNCHER_VERSION,
            ]).encode("utf-8")
        ).hexdigest()[:16]

        # 3. 尝试序号。start 对「同 spec 的已存在目录」直接返回当前状态,这让守护进程
        #    崩溃后能接上正在跑的作业;但同一条规则会让已失败的作业永远重跑不了。
        #    所以目录名带尝试序号,job_id 仍只由确定输入算出(设计 §5.2 第 4 步)。
        attempt_id = self._pick_attempt(job_id)
        work = self.endpoint.work_root.rstrip("/")
        train_path = f"{work}/data/{attempt_id}/train.jsonl"
        out_dir = f"{work}/runs/{self.run_id}/{attempt_id}"
        self.client.put(train_path, data)

        # 4. 启动命令(V2、V9 未核实:--max-steps 是否真能截到段尾、--resume 的路径层级)
        argv = [
            "python", f"{self.endpoint.roll_root.rstrip('/')}/run_sft_validation.py",
            "--config", self.endpoint.sft_template,
            "--model-path", self.endpoint.base_model_path,
            "--output-dir", out_dir,
            "--train-data", train_path,
            "--heldout-data", self.endpoint.heldout_jsonl,
            "--max-steps", str(last_step),
            "--save-steps", str(last_step),
            "--eval-steps", str(last_step),
        ]
        if first_step > 0:
            argv += ["--resume", self._remote_path(self._base_uri)]
        spec = JobSpec(argv=tuple(argv), cwd=self.endpoint.roll_root)

        # 5. 整段跑。run_job 自己取租约、轮询、释放;非零退出抛 RemoteJobFailedError
        #    (日志尾巴在 log_tail 里,不进 reason),超时抛 RemoteJobTimeoutError,
        #    租约被占抛 ComputeBusyError——三者都是 DeferredError(Ruling 9、10)。
        result = self.client.run_job(attempt_id, spec, tail_lines=40 + 4 * len(batches))

        # 6. 回收指标。step 必须从 ROLL 自己的记录里读,不能按位置合成:否则
        #    orchestrator 的 result.step != step 检查会恒为真,那道校验就被废掉了,
        #    而真实后端最需要它(设计 §5.2 第 7 步)。
        results = self._parse_metrics(result.log_tail, first_step, last_step)

        # 7. 段尾存点。编号是 checkpoint-{global_step},global_step 是零基索引,
        #    所以段尾那份叫 checkpoint-{last_step - 1}(设计 §5.2 第 5 步)。
        ckpt_dir = f"{out_dir}/checkpoint-{last_step - 1}"
        self._stage_ref = BigRef(
            uri=f"ssh://{self.endpoint.endpoint_id}{ckpt_dir}",
            sha256=self.client.hash_tree(ckpt_dir, "sampled"),
            size_bytes=0,  # Ruling 15:与 MockExecutor 一致,不填
            fetch_hint={
                "type": self.checkpoint_type,
                "format": "roll_lora_legacy",
                "job_id": attempt_id,
                "step": last_step,
            },
        )
        return results

    # ---------- 内部 ----------

    def _pick_attempt(self, job_id: str) -> str:
        """挑这次要用的尝试目录:仍在跑或已成功的直接接上,失败过的另起一个。"""
        for n in range(1, MAX_ATTEMPTS + 1):
            attempt_id = f"{job_id}-a{n}"
            status = self.client.status(attempt_id)
            if status.state == "missing":
                return attempt_id
            if status.state == "running":
                return attempt_id  # 幂等:接上正在跑的那次
            if status.state == "exited" and status.exit_code == 0:
                return attempt_id  # 已成功:run_job 会直接返回它的结果
        raise MetricsMissingError(
            f"job {job_id} has already used {MAX_ATTEMPTS} attempts; a human should look at the logs"
        )

    def _parse_metrics(self, log_tail: str, first_step: int, last_step: int) -> list[StepResult]:
        """从日志里取 ROLL 的 global step 与 sft 损失,与 [first_step, last_step) 对账。

        读不到、条数不符、缺 loss 键都抛 MetricsMissingError:绝不退化成按位置编号。
        _update_summary 直接读 metrics["sft_loss"],缺键会是 KeyError,所以这里
        一并覆盖(设计 §5.2 第 7 步)。
        """
        seen: dict[int, float] = {}
        for line in log_tail.splitlines():
            head, sep, body = line.partition(METRIC_PREFIX)
            if not sep:
                continue
            try:
                record = json.loads(body)
                step = int(record["step"])
                metrics = record["metrics"]
                loss = float(metrics["sft_train/loss"])
            except (ValueError, TypeError, KeyError):
                continue
            seen[step] = loss
        wanted = list(range(first_step, last_step))
        missing = [s for s in wanted if s not in seen]
        if missing:
            raise MetricsMissingError(
                f"the remote job reported no sft loss for step(s) {missing} "
                f"in [{first_step}, {last_step}); refusing to synthesise step numbers"
            )
        return [StepResult(step, "sft", {"sft_loss": seen[step]}) for step in wanted]

    def _remote_path(self, uri: str) -> str:
        """ssh://<endpoint_id>/<绝对路径> -> /<绝对路径>。"""
        rest = uri[len("ssh://"):]
        _, slash, path = rest.partition("/")
        if not slash:
            raise CheckpointCorruptError(f"{uri!r} carries no remote path")
        return "/" + path
```

**`save_checkpoint` 的两条路径:** 本段有产出就返回段尾那份(`_stage_ref`);没有产出就回认
`load` 记下的起点,但要**重新**算一次 `hash_tree` 拿真实摘要,而不是拿一个空 sha256 去调
`checkpoint_intact`(那样恒为 False,回认永远失败)。目录已经不在时抛 `CheckpointCorruptError`,
绝不返回没有对应目录的引用。

- [ ] **Step 7: 新建 `tests/compute_fixtures.py`**

真实 SSH 一概不碰:`FakeClient` 实现 `RollExecutor` 用到的那几个方法,状态全在内存里。
注意它要和 `ComputeClient` 的**真实签名**一致,否则测试通过而线上报错。

```python path=tests/compute_fixtures.py
"""compute 相关测试的共用替身。

FakeClient 覆盖 RollExecutor 用到的 ComputeClient 方法,签名逐一对齐
`src/compute/client.py`:run_job、status、put、hash_tree、preflight。
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable

from compute.client import JobResult, JobSpec, JobStatus
from compute.endpoint import ComputeEndpoint
from compute.errors import ComputeBusyError, HelperError

ENDPOINT_FIELDS = {
    "endpoint_id": "gpu-1",
    "work_root": "/srv/rise/work",
    "roll_root": "/srv/roll",
    "launch_prefix": (),
    "base_model_path": "/srv/models/qwen-4b",
    "heldout_jsonl": "/srv/roll/data/heldout.jsonl",
    "sft_template": "/srv/roll/configs/sft_lora.yaml",
    "min_free_host_mem_gib": 12.0,
    "min_free_disk_gib": 50.0,
    "poll_seconds": 0.01,
    "job_timeout_seconds": 60.0,
}


def fake_endpoint(**overrides) -> ComputeEndpoint:
    return ComputeEndpoint(**{**ENDPOINT_FIELDS, **overrides})


def metrics_log(first_step: int, last_step: int, *, loss: float = 0.5) -> str:
    """造一段形如 ROLL 逐步指标的日志(前缀见 roll_executor.METRIC_PREFIX)。"""
    return "".join(
        'metrics_tag: {"step": %d, "metrics": {"sft_train/loss": %r}}\n' % (step, loss - 0.01 * step)
        for step in range(first_step, last_step)
    )


class FakeClient:
    """内存替身。files 是 {路径: 字节},jobs 是 {job_id: JobStatus}。

    busy=True 时 preflight 抛 ComputeBusyError(模拟别人占着 GPU)。
    job_hook(spec) 在 run_job 里调用,用来模拟作业的副作用(写出存点目录)。
    """

    def __init__(
        self,
        *,
        busy: bool = False,
        log: str = "",
        exit_code: int = 0,
        job_hook: Callable[[str, JobSpec], None] | None = None,
    ) -> None:
        self.busy = busy
        self.log = log
        self.exit_code = exit_code
        self.job_hook = job_hook
        self.files: dict[str, bytes] = {}
        self.jobs: dict[str, JobStatus] = {}
        self.run_calls: list[tuple[str, JobSpec]] = []
        self.put_calls: list[str] = []
        self.preflight_calls = 0

    # --- 与 ComputeClient 对齐的方法 ---

    def preflight(self, *, own_holder: str | None = None) -> None:
        self.preflight_calls += 1
        if self.busy:
            raise ComputeBusyError("the GPU lease is held by job someone-else")

    def put(self, path: str, data: bytes, *, executable: bool = False) -> None:
        self.files[path] = data
        self.put_calls.append(path)

    def status(self, job_id: str) -> JobStatus:
        return self.jobs.get(job_id, JobStatus("missing", None, None))

    def hash_tree(self, path: str, mode: str) -> str:
        assert mode in ("sampled", "full"), mode
        under = {k: v for k, v in self.files.items() if k == path or k.startswith(path.rstrip("/") + "/")}
        if not under:
            raise HelperError("not_found", "no such directory")
        digest = hashlib.sha256()
        for key in sorted(under):
            digest.update(key.encode("utf-8"))
            digest.update(b"\0")
            digest.update(under[key] if mode == "full" else str(len(under[key])).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    def run_job(self, job_id: str, spec: JobSpec, *, secret: str | None = None,
                tail_lines: int = 40) -> JobResult:
        self.run_calls.append((job_id, spec))
        if self.busy:
            raise ComputeBusyError(f"the GPU lease is held by job other-{job_id}")
        if self.job_hook is not None:
            self.job_hook(job_id, spec)
        self.jobs[job_id] = JobStatus("exited", self.exit_code, None)
        return JobResult(self.exit_code, self.log)

    # --- 测试辅助 ---

    def seed_dir(self, path: str, *, files: tuple[str, ...] = ("config.json",)) -> str:
        """造一个目录并返回它的 sampled 摘要。"""
        for name in files:
            self.files[f"{path.rstrip('/')}/{name}"] = b"x"
        return self.hash_tree(path, "sampled")
```

- [ ] **Step 8: 新建 `tests/test_mt_roll_executor.py`**

```python path=tests/test_mt_roll_executor.py
"""RollExecutor:load 的三种引用、存点回认、阶段作业、指标对账、逐步方法拒绝。"""
from __future__ import annotations

import pytest
from compute_fixtures import FakeClient, fake_endpoint, metrics_log
from compute.errors import ComputeBusyError, HelperError
from controlplane.envelope import BigRef
from mindtrainer.executor import CheckpointCorruptError
from mindtrainer.roll_executor import MetricsMissingError, RollExecutor, _render_rows
from mindtrainer.training_config import InvalidTrainingConfigError
from simulator.datamodel import Episode, RoleAssignment, RoleBinding, TurnRecord

RUN = "run-1"


def _episode(ep_id="e0", *, turns=(("a", "u1"), ("b", "v1"), ("a", "u2"))):
    assignment = RoleAssignment(
        "s1",
        [RoleBinding("a", "pa", "self_play"),
         RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")],
        max_turns=4,
    )
    records = [TurnRecord(i, slot, text) for i, (slot, text) in enumerate(turns)]
    return Episode(ep_id, "s1", assignment, records, "max_turns")


def _executor(client: FakeClient, **kw) -> RollExecutor:
    return RollExecutor(client=client, endpoint=fake_endpoint(**kw), run_id=RUN)


def _base_ref(client: FakeClient) -> BigRef:
    endpoint = fake_endpoint()
    digest = client.seed_dir(endpoint.base_model_path)
    return BigRef(
        uri=f"ssh://{endpoint.endpoint_id}{endpoint.base_model_path}",
        sha256=digest, size_bytes=0, fetch_hint={"type": "roll_ssh"},
    )


# ---------------- 数据渲染 ----------------

def test_render_rows_supervises_every_trained_turn():
    """被训练角色位说了 2 句就出 2 行,instruction 是该句之前的全部发言。"""
    rows = _render_rows(_episode(), "a")
    assert rows == [
        {"instruction": "", "output": "u1"},
        {"instruction": "a: u1\nb: v1", "output": "u2"},
    ]


def test_render_rows_ignores_other_slots():
    rows = _render_rows(_episode(turns=(("b", "v1"), ("b", "v2"))), "a")
    assert rows == []


# ---------------- load ----------------

def test_load_accepts_the_base_model():
    client = FakeClient()
    executor = _executor(client)
    executor.load(_base_ref(client))  # 不抛即通过:起点基座不过 checkpoint_intact


def test_load_rejects_mock_refs():
    """mock:// 引用:load 抛 CheckpointCorruptError(重试不会好;分级沿用现状,见 Task 7 Step 2)。"""
    executor = _executor(FakeClient())
    ref = BigRef(uri="mock://ckpt/" + "a" * 64 + "@3", sha256="a" * 64, size_bytes=0)
    with pytest.raises(CheckpointCorruptError, match=r"expects an ssh:// reference"):
        executor.load(ref)


def test_load_rejects_adapter_exports():
    """适配器导出不是训练起点:本轮只支持单轮(设计 §5.1)。"""
    executor = _executor(FakeClient())
    ref = BigRef(
        uri="ssh://gpu-1/srv/rise/work/exports/aabbccdd", sha256="b" * 64, size_bytes=0,
        fetch_hint={"type": "roll_ssh", "format": "qwen4_exp_vllm_2d"},
    )
    with pytest.raises(InvalidTrainingConfigError, match=r"adapter export"):
        executor.load(ref)


def test_load_rejects_a_checkpoint_that_is_not_intact():
    client = FakeClient()
    client.seed_dir("/srv/rise/work/runs/run-1/j-a1/checkpoint-1")
    ref = BigRef(
        uri="ssh://gpu-1/srv/rise/work/runs/run-1/j-a1/checkpoint-1",
        sha256="c" * 64, size_bytes=0, fetch_hint={"type": "roll_ssh"},
    )
    with pytest.raises(CheckpointCorruptError, match=r"is not intact"):
        _executor(client).load(ref)


# ---------------- checkpoint_intact ----------------

def test_checkpoint_intact_is_false_when_the_directory_is_gone():
    ref = BigRef(uri="ssh://gpu-1/srv/rise/work/nope", sha256="d" * 64, size_bytes=0)
    assert _executor(FakeClient()).checkpoint_intact(ref) is False


def test_checkpoint_intact_propagates_transport_errors():
    """传输错误不能变成 False:那会被 decide_resume 判成 T3 永久失败(设计 §5.1)。"""

    class Flaky(FakeClient):
        def hash_tree(self, path, mode):
            raise HelperError("internal", "boom")

    ref = BigRef(uri="ssh://gpu-1/srv/rise/work/x", sha256="e" * 64, size_bytes=0)
    with pytest.raises(HelperError):
        _executor(Flaky()).checkpoint_intact(ref)


# ---------------- preflight 与逐步方法 ----------------

def test_preflight_delegates_to_the_client():
    client = FakeClient()
    _executor(client).preflight()
    assert client.preflight_calls == 1


def test_preflight_raises_compute_busy():
    with pytest.raises(ComputeBusyError):
        _executor(FakeClient(busy=True)).preflight()


def test_step_methods_refuse_to_run():
    executor = _executor(FakeClient())
    with pytest.raises(RuntimeError, match=r"call run_sft_stage"):
        executor.sft_step([_episode()], trained_slot="a")
    with pytest.raises(RuntimeError, match=r"does not support GRPO"):
        executor.grpo_opd_step([], opd_coefficient=0.1, teacher_model_ref=None)


def test_supported_phases_is_sft_only():
    assert RollExecutor.supported_phases == frozenset({"sft"})
    assert RollExecutor.checkpoint_type == "roll_ssh"


# ---------------- run_sft_stage ----------------

def _staged(client: FakeClient, batches, *, first_step=0, prior=()):
    executor = _executor(client)
    executor.load(_base_ref(client))
    return executor, executor.run_sft_stage(
        batches, first_step=first_step, trained_slot="a", prior_batches=list(prior)
    )


def _ckpt_hook(last_step: int):
    """模拟作业写出段尾存点目录。"""
    def hook(job_id, spec):
        out = dict(zip(spec.argv, spec.argv[1:]))["--output-dir"]
        FakeClient.seed_dir.__get__(hook.client)(f"{out}/checkpoint-{last_step - 1}")
    return hook


def test_run_sft_stage_writes_rows_in_step_order_and_returns_roll_steps():
    client = FakeClient(log=metrics_log(0, 2))
    hook = _ckpt_hook(2)
    hook.client = client
    client.job_hook = hook
    executor, results = _staged(client, [[_episode("e0")], [_episode("e1")]])
    assert [r.step for r in results] == [0, 1]
    assert all(r.phase == "sft" and "sft_loss" in r.metrics for r in results)
    # 每集 2 行(被训练角色位 2 句)× 2 步 = 4 行
    train = next(p for p in client.put_calls if p.endswith("train.jsonl"))
    assert len(client.files[train].decode("utf-8").strip().splitlines()) == 4


def test_prior_batches_become_the_file_prefix_and_change_the_job_id():
    """前缀进 train.jsonl 且进 job_id:ROLL 按位置跳过已消费的批次(设计 §5.2)。"""
    first = FakeClient(log=metrics_log(0, 1))
    h1 = _ckpt_hook(1); h1.client = first; first.job_hook = h1
    _staged(first, [[_episode("e0")]])

    second = FakeClient(log=metrics_log(1, 2))
    h2 = _ckpt_hook(2); h2.client = second; second.job_hook = h2
    _staged(second, [[_episode("e1")]], first_step=1, prior=[[_episode("e0")]])

    assert first.run_calls[0][0] != second.run_calls[0][0]
    prefix_rows = second.files[next(p for p in second.put_calls if p.endswith("train.jsonl"))]
    assert prefix_rows.decode("utf-8").splitlines()[0].count("u1") == 1  # 前缀行在最前


def test_resume_adds_the_resume_flag():
    client = FakeClient(log=metrics_log(2, 3))
    hook = _ckpt_hook(3); hook.client = client; client.job_hook = hook
    _staged(client, [[_episode("e2")]], first_step=2, prior=[[_episode("e0")], [_episode("e1")]])
    argv = client.run_calls[0][1].argv
    assert "--resume" in argv and "--max-steps" in argv
    assert argv[argv.index("--max-steps") + 1] == "3"


def test_metrics_missing_when_a_step_has_no_loss():
    """条数不符一律失败,绝不按位置合成 step(设计 §5.2 第 7 步)。"""
    client = FakeClient(log=metrics_log(0, 1))  # 只报了 1 步,却跑了 2 步
    hook = _ckpt_hook(2); hook.client = client; client.job_hook = hook
    with pytest.raises(MetricsMissingError, match=r"no sft loss for step\(s\) \[1\]"):
        _staged(client, [[_episode("e0")], [_episode("e1")]])


def test_metrics_missing_when_the_loss_key_is_absent():
    client = FakeClient(log='metrics_tag: {"step": 0, "metrics": {"other": 1.0}}\n')
    hook = _ckpt_hook(1); hook.client = client; client.job_hook = hook
    with pytest.raises(MetricsMissingError):
        _staged(client, [[_episode("e0")]])


def test_stage_checkpoint_reference_carries_the_roll_hint():
    client = FakeClient(log=metrics_log(0, 1))
    hook = _ckpt_hook(1); hook.client = client; client.job_hook = hook
    executor, _ = _staged(client, [[_episode("e0")]])
    ref = executor.save_checkpoint("step-1")
    assert ref.uri.startswith("ssh://gpu-1/srv/rise/work/runs/run-1/")
    assert ref.uri.endswith("/checkpoint-0")
    assert ref.fetch_hint["type"] == "roll_ssh"
    assert ref.fetch_hint["format"] == "roll_lora_legacy"
    assert len(ref.sha256) == 64


def test_save_checkpoint_reclaims_the_loaded_one_when_no_stage_ran():
    """续训时 start == total:一步都没跑,但编排器仍会调 save_checkpoint(设计 §5.1)。"""
    client = FakeClient()
    executor = _executor(client)
    base = _base_ref(client)
    executor.load(base)
    ref = executor.save_checkpoint("final")
    assert ref.uri == base.uri and ref.sha256 == base.sha256


def test_save_checkpoint_refuses_when_the_directory_is_gone():
    client = FakeClient()
    executor = _executor(client)
    executor.load(_base_ref(client))
    client.files.clear()
    with pytest.raises(CheckpointCorruptError, match=r"is gone"):
        executor.save_checkpoint("final")


def test_stage_end_never_cuts_finer():
    assert _executor(FakeClient()).stage_end([], first_step=2, limit=7) == 7
```

- [ ] **Step 9: 跑通单测**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_mt_roll_executor.py -q -p no:cacheprovider --color=no -W error
```

期望:**21 passed**(上面列出的全部用例)。`FakeClient` 纯内存,应在 1 秒内跑完。

**写测试时会遇到的两件事:**
1. `_ckpt_hook` 那种 `hook.client = client` 的写法是为了让钩子拿到客户端。实现时若觉得绕,
   改成 `FakeClient` 的子类或在 `compute_fixtures` 里加一个 `writes_checkpoint(last_step)`
   工厂都可以,只要作业跑完时存点目录确实存在。
2. `test_load_accepts_the_base_model` 的签名里那个 walrus 形参是笔误,写成普通
   `def test_load_accepts_the_base_model():` 即可。

- [ ] **Step 10: `tests/test_mt_orchestrator.py` 末尾追加 `TestRollExecutorIntegration`**

```python
class TestRollExecutorIntegration:
    """RollExecutor 与编排器合在一起:预检时机、阶段路径、跨模式与 supported_phases。

    用 FakeClient,不碰真实 SSH,所以不标 slow。
    """

    def _executor(self, client):
        from compute_fixtures import fake_endpoint
        from mindtrainer.roll_executor import RollExecutor

        return RollExecutor(client=client, endpoint=fake_endpoint(), run_id=RUN)

    def test_preflight_runs_before_the_run_record_is_written(self, store):
        """预检在 run_training 之前:被占时 run 记录一个字节都不写(设计 §7.3)。"""
        from compute.errors import ComputeBusyError
        from compute_fixtures import FakeClient
        from controlplane.paths import training_run_file

        cfg = _batch_cfg(sft_steps=2, grpo_steps=0)
        with pytest.raises(ComputeBusyError):
            _go(store, cfg, self._executor(FakeClient(busy=True)))
        assert not store.exists(training_run_file(RUN))

    def test_grpo_steps_are_refused_as_a_config_error(self, store):
        """supported_phases 只有 sft,配置却要 GRPO:确定性失败,run 记 failed(设计 §5.1)。"""
        from compute_fixtures import FakeClient

        cfg = _batch_cfg(sft_steps=1, grpo_steps=2)
        with pytest.raises(InvalidTrainingConfigError, match=r"supports only \['sft'\]"):
            _go(store, cfg, self._executor(FakeClient()))
        run = read_training_run(store, RUN)
        assert run.status == "failed" and run.failure["category"] == "T2"

    def test_resuming_a_mock_run_with_the_real_executor_needs_a_human(self, store):
        """跨模式续训要在 checkpoint_intact 之前挡住:T3、理由是后端不一致、记录不改(设计 §6.1)。"""
        from compute_fixtures import FakeClient

        cfg = _batch_cfg(sft_steps=4, grpo_steps=0, checkpoint_every_steps=2)
        with pytest.raises(SimulatedCrashError):
            _go(store, cfg, MockExecutor(crash_at_step=3), converter=MockConverter())  # 第 2 步存点后崩在第 3 步
        pending = read_training_run(store, RUN)
        assert pending.status == "pending" and pending.last_checkpoint_ref is not None

        with pytest.raises(NeedsHumanError, match=r"resuming across backend modes"):
            _go(store, cfg, self._executor(FakeClient()))
        assert read_training_run(store, RUN) == pending  # 记录一个字节都没动
```

第三个用例用 `sft_steps=4`、`checkpoint_every_steps=2`、`crash_at_step=3`:第 2 步存点之后崩在第 3 步,留下一份 `mock://` 中间存点且记录仍是 `pending`(已在隔离拷贝里跑过)。`SimulatedCrashError` 与 `MockExecutor`、`MockConverter` 在该文件顶部已导入。

- [ ] **Step 11: 跑通集成测试与整文件**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_mt_orchestrator.py -q -p no:cacheprovider --color=no -W error
```

期望:新增 3 个用例通过(Task 7 共 21 + 3 = **24**),且该文件既有用例全部不受影响。

- [ ] **Step 12: 全量**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error
```

期望:无警告、无失败。本任务没有改两处 `_DETERMINISTIC`,所以 `tests/test_compute_scaffold.py` 里
"延后异常不在确定性集合里"那条断言照旧成立。

- [ ] **Step 13: Commit**

```bash
git add src/mindtrainer/roll_executor.py src/mindtrainer/executor.py src/mindtrainer/orchestrator.py src/mindtrainer/dispatch.py tests/compute_fixtures.py tests/test_mt_roll_executor.py tests/test_mt_orchestrator.py pyproject.toml
git commit -m "$(cat <<'EOF'
feat(mindtrainer): RollExecutor 接 ROLL 整段 SFT 作业

- 阶段协议实现:多轮监督行展开、job_id 与尝试序号、整段 run_job、指标按 ROLL 的 global step 对账
- supported_phases 挡住 GRPO;跨模式续训在 checkpoint_intact 之前抛 NeedsHumanError(T3,记录不改)
- 预检钩子在 run_training 之前,被占时不写 run 记录
EOF
)"
```

---
## Task 8: RollLoraConverter(设计 §6.2)

**Files:**
- Modify: `src/compute/job_tool.py`(新增 `get`、`rename`、`rm-tmp` 三个命令)
- Modify: `src/compute/helper.py`(重新钉 `HELPER_SHA256`)
- Modify: `src/compute/client.py`(新增 `get`、`rename`、`rm_tmp` 包装;`run_job` 加 `lease` 开关)
- Modify: `tests/test_compute_assistant.py`(三个新命令的本机冒烟)
- Modify: `src/mindtrainer/checkpoint.py`(`CheckpointConverter` 协议补 docstring)
- Create: `src/mindtrainer/roll_converter.py`(约 180 行)
- Create: `tests/test_mt_roll_converter.py`(约 220 行)
- Modify: `tests/compute_fixtures.py`(`FakeClient` 跟着加三个方法)
- Modify: `tests/test_mt_orchestrator.py`(末尾追加 `TestRollConverterIntegration`)

**Interfaces:**
- Consumes: `ComputeClient.{get,rename,rm_tmp,hash_tree,run_job}`、`JobSpec`、`ComputeEndpoint`、`BigRef`、`CheckpointCorruptError`
- Produces: `RollLoraConverter`(实现 `CheckpointConverter.convert(ref) -> BigRef`)

**为什么要动 Task 2 冻结的助手:** 转换器要做三件远端动作,现有助手一个都做不了——
读完成标记(`get`)、把 `.tmp` 原子改名成正式目录(`rename`)、清掉自己留下的未完成
`.tmp`(`rm-tmp`)。`put`/`hash-tree` 都替代不了。按 Ruling 2 的约定,增减命令要改
`HELPER_SHA256` 并过评审,所以这几条命令连同新哈希一起落在本任务。

**已核实的事实(实现者不要再猜):**

| 事实 | 依据 |
|---|---|
| `CheckpointConverter` 协议在 `mindtrainer/checkpoint.py:21`,签名 `convert(self, ref: BigRef) -> BigRef`,已存在 | 读源码 |
| `MockConverter.convert` 看 uri 后缀(`.hf`);`_is_inference_format` 看 `fetch_hint["format"] == "hf"` | `checkpoint.py:27`、`executor.py:64` |
| 编排器在 `save_checkpoint("final")` 之后调 `converter.convert(final_ref)`,再 `register_candidate` | `orchestrator.py:226-233` |
| 助手的命令用 `@command("名字")` 注册,失败抛 `Failure(code, detail)`;路径校验用 `_abs_path_inside(root, req)`(越界抛 `bad_path`) | `job_tool.py:94-98,447` |
| `hash-tree` 对不存在的目录抛 `Failure("not_found", ...)`,客户端翻成 `HelperError(code="not_found")` | `job_tool.py:484`、`client.py:107` |
| `_TRANSIENT_CODES` 里的错误码会被翻成 `TransportError`,其余翻成 `HelperError` | `client.py:105-108` |
| `HELPER_SHA256` 由 `helper.py` 钉死,`helper_bytes()` 不符就抛;有测试拦 | `helper.py:12,31` |
| `run_job` 现在**无条件**取租约,转换器是纯 CPU 作业、不该占 GPU 租约(设计 §6.2 第 1 步) | `client.py:188` |
| `JobSpec.env` 是 `Mapping[str,str]`,进 spec 哈希;`secret_env` 不进哈希 | `client.py:27-34` |

- [ ] **Step 1: 助手新增 `get`、`rename`、`rm-tmp`**

在 `src/compute/job_tool.py` 的 `cmd_put` 之后插入。三条都走 `_abs_path_inside`,所以路径
一律限制在 `work_root` 之内;`rm-tmp` 另加一道形状检查,只肯删自己造的 `.tmp-` 目录。

```python
MAX_GET_BYTES = 1 << 20  # 完成标记、tracker 这类小文件;大文件不走这条路


@command("get")
def cmd_get(req):
    root = _work_root(req)
    path = _abs_path_inside(root, req)
    if not os.path.isfile(path):
        raise Failure("not_found", "no such file")
    size = os.lstat(path).st_size
    if size > MAX_GET_BYTES:
        raise Failure("too_large", "file exceeds %d bytes" % MAX_GET_BYTES)
    with open(path, "rb") as handle:
        data = handle.read()
    return {"content_b64": base64.b64encode(data).decode("ascii"),
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


@command("rename")
def cmd_rename(req):
    """os.replace:同一文件系统内原子改名。目标已存在就拒绝,不覆盖别人的产物。"""
    root = _work_root(req)
    src = _abs_path_inside(root, req, key="src")
    dst = _abs_path_inside(root, req, key="dst")
    if not os.path.exists(src):
        raise Failure("not_found", "no such source path")
    if os.path.exists(dst):
        raise Failure("exists", "the destination already exists")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.replace(src, dst)
    return {"renamed": True}


@command("rm-tmp")
def cmd_rm_tmp(req):
    """只删形如 <...>.tmp-<十六进制> 的目录:调用方自己造的未完成产物。

    删除是不可逆的,所以不接受任意路径:名字不合这个形状一律 bad_path。
    """
    root = _work_root(req)
    path = _abs_path_inside(root, req)
    name = os.path.basename(path.rstrip("/"))
    if ".tmp-" not in name or not name.rsplit(".tmp-", 1)[1].isalnum():
        raise Failure("bad_path", "rm-tmp only removes directories named '<name>.tmp-<token>'")
    if not os.path.isdir(path):
        return {"removed": False}
    shutil.rmtree(path)
    return {"removed": True}
```

`job_tool.py` 顶部的导入要加 `shutil`。`_abs_path_inside` 现在按固定键名 `path` 取值,
`rename` 需要两个路径,所以给它加一个 `key` 形参(默认 `"path"`),改动只有一行。

- [ ] **Step 2: 重新钉 `HELPER_SHA256`**

```bash
PYTHONPATH=src python3 -c "
import hashlib, pathlib
print(hashlib.sha256(pathlib.Path('src/compute/job_tool.py').read_bytes()).hexdigest())
"
```

把输出填进 `src/compute/helper.py` 的 `HELPER_SHA256`。改完跑:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_compute_assistant.py -q -p no:cacheprovider --color=no -W error
```

期望:原有用例全过(钉死哈希那条会校验新值)。远端助手路径里带哈希前 8 位,所以
`remote_helper_path` 变了,已上传的旧助手不会被误用。

- [ ] **Step 3: 客户端包装三个新命令,并给 `run_job` 加 `lease` 开关**

在 `src/compute/client.py` 的 `put` / `hash_tree` 旁边加:

```python
    def get(self, path: str) -> bytes:
        """读一个小文件(完成标记、tracker)。文件不在抛 HelperError(code='not_found')。"""
        reply = self._call("get", path=path)
        data = base64.b64decode(str(reply["content_b64"]).encode("ascii"), validate=True)
        if hashlib.sha256(data).hexdigest() != reply["sha256"]:
            raise TransportError("the remote helper returned a file whose sha256 does not match")
        return data

    def rename(self, src: str, dst: str) -> None:
        """原子改名。目标已存在抛 HelperError(code='exists')。"""
        self._call("rename", src=src, dst=dst)

    def rm_tmp(self, path: str) -> bool:
        """删掉自己造的 '<name>.tmp-<token>' 目录。别的形状抛 HelperError(code='bad_path')。"""
        return bool(self._call("rm-tmp", path=path)["removed"])
```

`run_job` 的签名加一个关键字,默认保持现在的行为:

```python
    def run_job(self, job_id: str, spec: JobSpec, *, secret: str | None = None,
                tail_lines: int = 40, lease: bool = True) -> JobResult:
```

函数体里原来无条件取租约的那三行,改成只在 `lease` 为真时执行;结尾释放租约同样加条件。
纯 CPU 的转换作业传 `lease=False`:它不占 GPU,不该跟训练抢租约(设计 §6.2 第 1 步)。

- [ ] **Step 4: 助手新命令的本机冒烟**

在 `tests/test_compute_assistant.py` 末尾追加:

```python
def test_get_reads_a_small_file(local_client, work_root):
    local_client.put(f"{work_root}/marker", b"abc")
    assert local_client.get(f"{work_root}/marker") == b"abc"


def test_get_reports_a_missing_file(local_client, work_root):
    with pytest.raises(HelperError) as caught:
        local_client.get(f"{work_root}/nope")
    assert caught.value.code == "not_found"


def test_rename_is_atomic_and_refuses_to_overwrite(local_client, work_root):
    local_client.put(f"{work_root}/a.tmp-1234/f", b"x")
    local_client.rename(f"{work_root}/a.tmp-1234", f"{work_root}/a")
    assert local_client.get(f"{work_root}/a/f") == b"x"
    local_client.put(f"{work_root}/b.tmp-5678/f", b"y")
    with pytest.raises(HelperError) as caught:
        local_client.rename(f"{work_root}/b.tmp-5678", f"{work_root}/a")
    assert caught.value.code == "exists"


def test_rm_tmp_only_removes_tmp_directories(local_client, work_root):
    local_client.put(f"{work_root}/c.tmp-aaaa/f", b"z")
    assert local_client.rm_tmp(f"{work_root}/c.tmp-aaaa") is True
    assert local_client.rm_tmp(f"{work_root}/c.tmp-aaaa") is False  # 幂等
    local_client.put(f"{work_root}/keep/f", b"z")
    with pytest.raises(HelperError) as caught:
        local_client.rm_tmp(f"{work_root}/keep")
    assert caught.value.code == "bad_path"


def test_paths_outside_work_root_are_refused(local_client, work_root):
    for call in (lambda: local_client.get("/etc/passwd"),
                 lambda: local_client.rm_tmp("/tmp/x.tmp-1111")):
        with pytest.raises(HelperError) as caught:
            call()
        assert caught.value.code == "bad_path"
```

夹具名(`local_client`、`work_root`)按 Task 2 里实际写出的那套改;若 Task 2 用的是
`LocalSsh` 直接构造客户端,这里照它的写法。

- [ ] **Step 5: `CheckpointConverter` 协议补 docstring**

`src/mindtrainer/checkpoint.py:21` 的协议签名与设计相符,只是没写明产出引用的形状与异常。
只改注释,不动签名,保持与 `MockConverter` 的后向兼容:

```python
class CheckpointConverter(Protocol):
    """训练存点 -> 推理格式(设计 §6.2)。

    返回的 BigRef 要满足:
    - `uri` 指向转换产物自己的目录,不复用训练存点的路径;
    - `sha256` 是产物目录的 **full** 口径哈希(§6.1:任何字节变化都会变号);
    - `fetch_hint` 写明 `type`(后端种类)、`format`(布局)、以及推理侧加载所需的
      `base_model_path` 与 `requires`。评测侧靠 `type` 判断证据等级(§8),靠
      `format` 与 `requires` 决定能不能加载。

    幂等:同一个输入引用重复调用,返回同一个产物引用,不重做转换。

    抛:`CheckpointCorruptError`(产物已存在但哈希不符)、`ComputeBusyError` 与
    `TransportError`(都是 `DeferredError`,走延后通道,不耗续训预算)。
    """

    def convert(self, ref: BigRef) -> BigRef: ...
```

- [ ] **Step 6: 新建 `src/mindtrainer/roll_converter.py`**

```python path=src/mindtrainer/roll_converter.py
"""RollLoraConverter:ROLL 训练存点 -> vLLM LoRA 适配器导出(设计 §6.2)。

纯 CPU:`CUDA_VISIBLE_DEVICES` 置空、`run_job(lease=False)`,不占 GPU 租约,但转换峰值
约 8 GiB 主机内存,所以靠端点的 `min_free_host_mem_gib` 预检(V5 待主机核实)。

产物目录是内容寻址的:`exports/<训练存点 sha8>/`。写的时候先落到
`exports/<sha8>.tmp-<token>/`,算完 full 哈希、写完成标记,再原子改名。所以"目录存在
且带标记"就意味着"上一次确实写完了"。
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from compute.client import ComputeClient, JobSpec
from compute.endpoint import ComputeEndpoint
from compute.errors import ComputeBusyError, HelperError
from controlplane.envelope import BigRef
from mindtrainer.executor import CheckpointCorruptError

EXPORT_MARKER = ".exported"  # 内容是产物目录的 full 哈希
EXPORT_FORMAT = "qwen4_exp_vllm_2d"  # ROLL 的 LoRAHFConverter 布局(设计 §1 已核实)

# V15:真实导出命令待主机核实(V3、V5)。ROLL 文档记录的是 LoRAHFConverter,
# 入口脚本名与参数都还没在机器上跑过,所以这里只固定"纯 CPU、写进 tmp 目录"这两点。
EXPORT_SCRIPT = "scripts/export_lora_for_vllm.py"


@dataclass
class RollLoraConverter:
    """训练存点 -> 适配器导出(设计 §6.2)。

    client / endpoint:与执行器同一套装配(factory.compute_config.build_compute_client)。
    """

    client: ComputeClient
    endpoint: ComputeEndpoint

    def convert(self, ref: BigRef) -> BigRef:
        if not ref.uri.startswith("ssh://"):
            raise CheckpointCorruptError(
                f"RollLoraConverter cannot convert {ref.uri!r}: it expects an ssh:// reference"
            )
        if len(ref.sha256) != 64:
            raise CheckpointCorruptError(
                f"the checkpoint reference carries no usable sha256: {ref.sha256!r}"
            )
        src = self._remote_path(ref.uri)
        work = self.endpoint.work_root.rstrip("/")
        export_dir = f"{work}/exports/{ref.sha256[:8]}"

        # 1. 已经转过就直接返回(幂等)。标记在而哈希不符,说明产物被动过:
        #    重做只会再次覆盖同一份产物、结果同样不可信,所以抛 CheckpointCorruptError 而不是重做;
        #    它的分级沿用现状(不在 _DETERMINISTIC、原样上抛),不是 T2。
        recorded = self._read_marker(export_dir)
        if recorded is not None:
            actual = self.client.hash_tree(export_dir, "full")
            if actual != recorded:
                raise CheckpointCorruptError(
                    f"the export at {export_dir} records {recorded[:12]}... "
                    f"but its contents hash to {actual[:12]}..."
                )
            return self._build_ref(export_dir, actual, ref)

        # 2. 内存预检。转换不占 GPU,但峰值约 8 GiB,挤爆主机会把别人的训练一起拖垮。
        #    不够就按忙处理:ComputeBusyError 是 DeferredError,延后重试,不耗续训预算。
        self._require_host_memory()

        # 3. 写进自己的 tmp 目录。token 让并发的两次转换互不覆盖。
        tmp_dir = f"{export_dir}.tmp-{uuid.uuid4().hex[:8]}"
        job_id = "export-" + hashlib.sha256(
            f"{ref.sha256}\0{tmp_dir}\0{EXPORT_FORMAT}".encode("utf-8")
        ).hexdigest()[:12]
        spec = JobSpec(
            argv=(
                "python", f"{self.endpoint.roll_root.rstrip('/')}/{EXPORT_SCRIPT}",
                "--checkpoint", src,
                "--base-model", self.endpoint.base_model_path,
                "--output", tmp_dir,
            ),
            cwd=self.endpoint.roll_root,
            env={"CUDA_VISIBLE_DEVICES": ""},  # 设计 §6.2 第 1 步:纯 CPU
        )
        try:
            # lease=False:纯 CPU 作业不跟训练抢 GPU 租约(设计 §6.2 第 1 步)
            self.client.run_job(job_id, spec, tail_lines=40, lease=False)
            # 4. full 口径:LoRA 适配器的形状只由 rank 与层数决定,两次不同训练的导出
            #    目录文件名与字节数完全相同,只记大小会让它们撞同一个哈希,而候选归属
            #    靠 checkpoint_ref.sha256 判断(设计 §6.1、D14)。
            digest = self.client.hash_tree(tmp_dir, "full")
            # 5. 标记写在 tmp 里,随改名一起变成正式产物:不会出现"目录已就位但没标记"。
            self.client.put(f"{tmp_dir}/{EXPORT_MARKER}", digest.encode("ascii"))
            self.client.rename(tmp_dir, export_dir)
        except HelperError as exc:
            if exc.code == "exists":
                # 别人刚好把同一个 sha8 转完了:它的产物与我们要产的是同一份,
                # 认它,并清掉自己这份未完成的 tmp。
                self._discard(tmp_dir)
                return self.convert(ref)
            self._discard(tmp_dir)
            raise
        except Exception:
            # 清理范围严格限制在自己创建的 tmp 目录,绝不碰已完成的 exports/<sha8>/
            self._discard(tmp_dir)
            raise
        return self._build_ref(export_dir, digest, ref)

    # ---------- 内部 ----------

    def _read_marker(self, export_dir: str) -> str | None:
        """产物目录的完成标记;没有标记(含目录不存在)返回 None。"""
        try:
            data = self.client.get(f"{export_dir}/{EXPORT_MARKER}")
        except HelperError as exc:
            if exc.code == "not_found":
                return None
            raise
        recorded = data.decode("ascii", "replace").strip()
        if len(recorded) != 64:
            raise CheckpointCorruptError(
                f"the export marker at {export_dir} does not hold a sha256"
            )
        return recorded

    def _require_host_memory(self) -> None:
        """空闲主机内存不足按忙处理(设计 §6.2 第 1 步)。

        V15:`probe` 的应答字段名待主机核实;拿不到读数时放行而不是硬拦——
        预检本身不该成为新的失败源,真不够的话转换会在远端 OOM,那是 T1 可重试。
        """
        probe = getattr(self.client, "probe", None)
        if probe is None:
            return
        free = probe().get("mem_available_gib")
        if free is None:
            return
        if float(free) < self.endpoint.min_free_host_mem_gib:
            raise ComputeBusyError(
                f"only {float(free):.1f} GiB of host memory is free; "
                f"the export needs {self.endpoint.min_free_host_mem_gib:.1f} GiB"
            )

    def _discard(self, tmp_dir: str) -> None:
        """清掉自己造的 tmp。清理失败不掩盖原始异常:留个孤儿目录比丢掉错因好。"""
        try:
            self.client.rm_tmp(tmp_dir)
        except Exception:  # noqa: BLE001
            pass

    def _build_ref(self, export_dir: str, digest: str, source: BigRef) -> BigRef:
        """uri 不带 .hf 后缀:只有 MockConverter 看后缀(设计 §6.2 第 4 步)。"""
        return BigRef(
            uri=f"ssh://{self.endpoint.endpoint_id}{export_dir}",
            sha256=digest,
            size_bytes=0,  # Ruling 15:与 MockExecutor 一致,不填
            fetch_hint={
                "type": "roll_ssh",
                "format": EXPORT_FORMAT,
                "base_model_path": self.endpoint.base_model_path,
                "requires": "native-vllm-lora",  # 需要 ROLL 的 vLLM 补丁(设计 V3)
                "source_checkpoint_sha256": source.sha256,
            },
        )

    def _remote_path(self, uri: str) -> str:
        rest = uri[len("ssh://"):]
        _, slash, path = rest.partition("/")
        if not slash:
            raise CheckpointCorruptError(f"{uri!r} carries no remote path")
        return "/" + path
```

- [ ] **Step 7: `FakeClient` 跟着加三个方法**

在 `tests/compute_fixtures.py` 的 `FakeClient` 里补上(签名与客户端一致):

```python
    def get(self, path: str) -> bytes:
        if path not in self.files:
            raise HelperError("not_found", "no such file")
        return self.files[path]

    def rename(self, src: str, dst: str) -> None:
        under = {k: v for k, v in self.files.items()
                 if k == src or k.startswith(src.rstrip("/") + "/")}
        if not under:
            raise HelperError("not_found", "no such source path")
        if any(k == dst or k.startswith(dst.rstrip("/") + "/") for k in self.files):
            raise HelperError("exists", "the destination already exists")
        for key, value in under.items():
            self.files[dst + key[len(src):]] = value
            del self.files[key]
        self.rename_calls.append((src, dst))

    def rm_tmp(self, path: str) -> bool:
        name = path.rstrip("/").rsplit("/", 1)[-1]
        if ".tmp-" not in name:
            raise HelperError("bad_path", "rm-tmp only removes '<name>.tmp-<token>'")
        under = [k for k in self.files
                 if k == path or k.startswith(path.rstrip("/") + "/")]
        for key in under:
            del self.files[key]
        self.rm_tmp_calls.append(path)
        return bool(under)

    def probe(self) -> dict:
        return {"mem_available_gib": self.mem_available_gib}
```

`__init__` 里相应加 `self.rename_calls = []`、`self.rm_tmp_calls = []`、
`self.mem_available_gib = 32.0`,并把 `run_job` 的签名改成带 `lease: bool = True`
(记进 `self.run_calls`,好让测试断言转换作业确实没取租约)。

- [ ] **Step 8: 新建 `tests/test_mt_roll_converter.py`**

```python path=tests/test_mt_roll_converter.py
"""RollLoraConverter:幂等、产物形状、tmp 清理、内存预检、哈希不符。"""
from __future__ import annotations

import pytest
from compute_fixtures import FakeClient, fake_endpoint
from compute.errors import ComputeBusyError, HelperError
from controlplane.envelope import BigRef
from mindtrainer.executor import CheckpointCorruptError
from mindtrainer.roll_converter import EXPORT_FORMAT, EXPORT_MARKER, RollLoraConverter

CKPT = "/srv/rise/work/runs/run-1/j-a1/checkpoint-1"
SHA = "a1b2c3d4" + "0" * 56


def _ref(sha: str = SHA) -> BigRef:
    return BigRef(uri=f"ssh://gpu-1{CKPT}", sha256=sha, size_bytes=0,
                  fetch_hint={"type": "roll_ssh", "format": "roll_lora_legacy"})


def _exporting_client(**kw) -> FakeClient:
    """作业跑起来就往 --output 指的 tmp 目录里写几个文件。"""
    client = FakeClient(**kw)

    def hook(job_id, spec):
        out = spec.argv[spec.argv.index("--output") + 1]
        for name in ("adapter_model.safetensors", "adapter_config.json"):
            client.files[f"{out}/{name}"] = b"weights"

    client.job_hook = hook
    return client


def _converter(client: FakeClient) -> RollLoraConverter:
    return RollLoraConverter(client=client, endpoint=fake_endpoint())


# ---------------- 正常转换 ----------------

def test_convert_writes_the_export_and_returns_an_inference_ref():
    client = _exporting_client()
    ref = _converter(client).convert(_ref())
    assert ref.uri == f"ssh://gpu-1/srv/rise/work/exports/{SHA[:8]}"
    assert not ref.uri.endswith(".hf")  # 只有 MockConverter 看后缀(设计 §6.2 第 4 步)
    assert len(ref.sha256) == 64
    assert ref.fetch_hint == {
        "type": "roll_ssh",
        "format": EXPORT_FORMAT,
        "base_model_path": "/srv/models/qwen-4b",
        "requires": "native-vllm-lora",
        "source_checkpoint_sha256": SHA,
    }


def test_the_export_job_is_cpu_only_and_takes_no_lease():
    """设计 §6.2 第 1 步:CUDA_VISIBLE_DEVICES 置空,不占 GPU 租约。"""
    client = _exporting_client()
    _converter(client).convert(_ref())
    job_id, spec, lease = client.run_calls[0]
    assert spec.env["CUDA_VISIBLE_DEVICES"] == ""
    assert lease is False
    assert "--checkpoint" in spec.argv and CKPT in spec.argv


def test_the_marker_lands_inside_the_final_directory():
    client = _exporting_client()
    ref = _converter(client).convert(_ref())
    marker = client.files[f"/srv/rise/work/exports/{SHA[:8]}/{EXPORT_MARKER}"]
    assert marker.decode("ascii") == ref.sha256


def test_nothing_is_left_in_a_tmp_directory():
    client = _exporting_client()
    _converter(client).convert(_ref())
    assert not [p for p in client.files if ".tmp-" in p]


# ---------------- 幂等 ----------------

def test_convert_is_idempotent_and_does_not_rerun_the_job():
    client = _exporting_client()
    converter = _converter(client)
    first = converter.convert(_ref())
    second = converter.convert(_ref())
    assert first.uri == second.uri and first.sha256 == second.sha256
    assert len(client.run_calls) == 1, "已有带标记的产物时不该再跑一次作业"


def test_a_tampered_export_is_a_corrupt_error():
    """标记在而内容变了:抛 CheckpointCorruptError,不重做(设计 §6.2 第 2 步;分级沿用现状)。"""
    client = _exporting_client()
    converter = _converter(client)
    converter.convert(_ref())
    client.files[f"/srv/rise/work/exports/{SHA[:8]}/adapter_config.json"] = b"tampered"
    with pytest.raises(CheckpointCorruptError, match=r"but its contents hash to"):
        converter.convert(_ref())


def test_a_marker_that_is_not_a_sha256_is_rejected():
    client = _exporting_client()
    client.files[f"/srv/rise/work/exports/{SHA[:8]}/{EXPORT_MARKER}"] = b"nonsense"
    with pytest.raises(CheckpointCorruptError, match=r"does not hold a sha256"):
        _converter(client).convert(_ref())


# ---------------- 失败与清理 ----------------

def test_a_failed_job_leaves_no_tmp_behind():
    client = _exporting_client()

    def boom(job_id, spec):
        raise HelperError("internal", "the export crashed")

    client.job_hook = boom
    with pytest.raises(HelperError):
        _converter(client).convert(_ref())
    assert client.rm_tmp_calls, "失败后要清掉自己造的 tmp"
    assert not [p for p in client.files if ".tmp-" in p]


def test_cleanup_never_touches_a_finished_export():
    """清理范围严格限制在自己的 tmp(设计 §6.2 第 2 步)。"""
    client = _exporting_client()
    converter = _converter(client)
    converter.convert(_ref())
    done = f"/srv/rise/work/exports/{SHA[:8]}"
    with pytest.raises(HelperError):
        client.rm_tmp(done)  # 正式目录名字不含 .tmp-,助手会拒
    assert f"{done}/{EXPORT_MARKER}" in client.files


def test_low_host_memory_defers():
    client = _exporting_client()
    client.mem_available_gib = 4.0  # 端点阈值是 12.0
    with pytest.raises(ComputeBusyError, match=r"host memory is free"):
        _converter(client).convert(_ref())
    assert not client.run_calls, "内存不够时不该起作业"


# ---------------- 输入校验 ----------------

def test_mock_refs_are_refused():
    with pytest.raises(CheckpointCorruptError, match=r"expects an ssh:// reference"):
        _converter(FakeClient()).convert(
            BigRef(uri="mock://ckpt/" + "a" * 64 + "@3", sha256="a" * 64, size_bytes=0)
        )


def test_a_reference_without_a_sha256_is_refused():
    with pytest.raises(CheckpointCorruptError, match=r"no usable sha256"):
        _converter(FakeClient()).convert(
            BigRef(uri=f"ssh://gpu-1{CKPT}", sha256="", size_bytes=0)
        )
```

- [ ] **Step 9: 跑通转换器单测**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_mt_roll_converter.py -q -p no:cacheprovider --color=no -W error
```

期望:上面列出的用例全过。

- [ ] **Step 10: `tests/test_mt_orchestrator.py` 追加 `TestRollConverterIntegration`**

```python
class TestRollConverterIntegration:
    """编排器调真实转换器:候选产物里落的是适配器导出引用。"""

    def test_the_candidate_carries_the_adapter_export_reference(self, store):
        """run_training 收尾时调 converter.convert(final_ref),产出进 candidate_checkpoint.yaml。"""
        from compute_fixtures import FakeClient, fake_endpoint, metrics_log
        from mindtrainer.roll_converter import EXPORT_FORMAT, RollLoraConverter
        from mindtrainer.roll_executor import RollExecutor

        client = FakeClient(log=metrics_log(0, 2))
        endpoint = fake_endpoint()

        def hook(job_id, spec, lease=True):
            if "--output" in spec.argv:  # 转换作业
                out = spec.argv[spec.argv.index("--output") + 1]
                client.files[f"{out}/adapter_model.safetensors"] = b"w"
            else:  # 训练作业
                out = spec.argv[spec.argv.index("--output-dir") + 1]
                client.files[f"{out}/checkpoint-1/config.json"] = b"c"

        client.job_hook = hook
        client.seed_dir(endpoint.base_model_path)
        executor = RollExecutor(client=client, endpoint=endpoint, run_id=RUN)
        converter = RollLoraConverter(client=client, endpoint=endpoint)

        cfg = _batch_cfg(sft_steps=2, grpo_steps=0, checkpoint_every_steps=100,
                         base_checkpoint_ref=BigRef(
                             uri=f"ssh://{endpoint.endpoint_id}{endpoint.base_model_path}",
                             sha256=client.hash_tree(endpoint.base_model_path, "sampled"),
                             size_bytes=0, fetch_hint={"type": "roll_ssh"}))
        run = _go(store, cfg, executor, converter=converter)
        assert run.status == "executed"

        candidate = _candidate(store).payload
        ref = BigRef.from_dict(candidate["checkpoint_ref"])
        assert "/exports/" in ref.uri
        assert ref.fetch_hint["format"] == EXPORT_FORMAT
        assert ref.fetch_hint["type"] == "roll_ssh"
```

`_batch_cfg` 默认的 `base_checkpoint_ref` 是 `file://` 引用,`RollExecutor.load` 会拒,
所以这里显式传一个 `ssh://` 的。`hook` 的 `lease` 形参要与 `FakeClient.run_job` 传钩子的
方式一致;若钩子只收两个参数,把它改成 `def hook(job_id, spec)`。

- [ ] **Step 11: 关于 `_is_inference_format`——本任务不改**

原计划这里要把 `_is_inference_format` 改成也认 `qwen4_exp_vllm_2d`。**不做**,理由:

`_is_inference_format` 有两个调用点,都在 `MockExecutor`:`load`(`executor.py:110`)与
`checkpoint_intact`(`executor.py:118`),用来挡住"拿推理格式存点续训"。而真实后端这条路已经被两道更准的检查挡住了:
`RollExecutor.load` 对适配器导出抛 `InvalidTrainingConfigError`(Task 7 Step 6),
`_check_backend_match` 对跨模式续训抛 `NeedsHumanError`(Task 7 Step 4)。两处的报错都写明了
真实原因,而改 `_is_inference_format` 只会让 `MockExecutor` 对一个它永远拿不到的
格式多一条分支,还动了"Mock 字节级不变"(真实后端设计 D2)。设计 §10 把 I9 列为缺口,它的
归宿是"真实后端只支持单轮",已经由上面两道检查落实。

- [ ] **Step 12: 全量**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error
```

期望:无警告、无失败。助手改了,所以 `tests/test_compute_assistant.py` 里钉死哈希与
3.8 语法那两条断言都要仍然成立。

- [ ] **Step 13: Commit**

```bash
git add src/compute/job_tool.py src/compute/helper.py src/compute/client.py src/mindtrainer/checkpoint.py src/mindtrainer/roll_converter.py tests/compute_fixtures.py tests/test_compute_assistant.py tests/test_mt_roll_converter.py tests/test_mt_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(mindtrainer): RollLoraConverter 导出 vLLM LoRA 适配器

- 助手新增 get/rename/rm-tmp 三个命令,HELPER_SHA256 重新钉死
- 客户端包装三个命令;run_job 加 lease 开关,纯 CPU 作业不占 GPU 租约
- 转换器:内容寻址的 exports/<sha8>、full 口径哈希、完成标记随改名落位、幂等
- 清理范围严格限制在自己创建的 .tmp,绝不碰已完成的产物
EOF
)"
```

**期望测试数:** Task 8 新增助手冒烟 5 个、转换器单测 12 个、集成 1 个,共 **18**。

**累计:** 基线 B = 2642,各任务增量 Task1 10、Task2 30、Task3 8、Task4 7、Task5 6、Task6 12、Task7 24、Task8 18,合计 **115**,全量应收集 **2757** 个。增量以各任务末尾的"期望"为准;B 若因别的计划先落地而变,绝对数会变。

---

## Task 9: 设计文档收尾

**Files:** 改 `docs/design/` 下的文档与 `_shared/`、`diagrams/`;不动 `src/`、`tests/`。

**为什么是最后一步而不是第一步。** 权威文档集是 `docs/design/`(`README.md` 声明它取代
`docs/superpowers/specs/` 下的旧设计,冲突以它为准)。真实后端的设计在本计划开工之前已经
先并入 `docs/design/`(V1 边界、延后通道、跨模式续训的分级、单轮限制都在那一轮定稿),所以
本任务只做"代码落地之后才知道的那部分":模块表、依赖表、产物表、测试数这些跟着实现走的条目。
**开工前先确认那一轮已经合入**;若没有,先做它,不要在这里补设计。

- [ ] **Step 1: 按实现回填**

逐篇过一遍,只改与本计划的实现对不上的地方:

| 文档 | 要对齐的内容 |
|---|---|
| `00-overview.md` | 依赖表加 `src/compute` 一行、`mindtrainer` 那行的可导入项补 `compute`;分层表给 `compute` 归层;代码地图加它 |
| `10-controlplane.md` | 产物表加 `compute/endpoints.yaml`(人工写、`write_config` 写不了、只有读侧做凭证字段检查);`DeferredError` 与延后通道写进调和循环那节与失败分级 |
| `30-mindtrainer.md` | 模块表加 `roll_executor`、`roll_converter`;阶段协议(`run_sft_stage`、`stage_end`、`preflight`、`validate_sft_data`)写进接口;失败表确认"存点损坏判 T3、记录不改"与实现一致;测试数按实测填 |
| `90-factory-and-cli.md` | 模块表加 `compute_config`;"只用标准库"一条按实际改(`compute_config` 用 `yaml`);手写文件清单加 `endpoints.yaml` |

**测试数一律实测,不要推算:**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest --collect-only -q -p no:cacheprovider 2>/dev/null | tail -1
```

- [ ] **Step 2: 跑工具,错误为 0**

```bash
cd docs/design
python3 tools/sync_docs.py              # 改过 _shared/ 或图就必须跑,它整段重写标记区块
python3 tools/check_docs.py             # 期望:0 个错误
python3 tools/check_consistency.py      # 期望:0 个错误
```

`sync_docs.py --check` 的"不同步"不是告警,是会让 `check_docs.py` 报错的硬失败。

- [ ] **Step 3: 改过图就目检**

动了 `diagrams/` 下的 `.py` 就重新渲染,逐张打开看一眼:节点没有重叠、文字没有出框。
图只画目标系统,不标"计划中/已落地"(进展只写在正文的状态标记里)。

- [ ] **Step 4: Commit**

```bash
git add docs/design
git commit -m "docs: 设计文档对齐远端计算层的实现(模块表、依赖表、产物表、测试数)"
```
