"""装配期读配置文件与预检的入口:把"这份文件读不了"的各种形态统一成 FactoryConfigError(命令行退出码 2)。"""
from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from controlplane.config import ConfigTypeError, CredentialInGitTreeError, read_config
from controlplane.gitstore import GitStore
from factory.errors import FactoryConfigError

T = TypeVar("T")


def read_tree_config(store: GitStore, rel: str) -> dict:
    """read_config 的包装。文件不存在照旧抛 FileNotFoundError,由调用方按场景措辞;其余读不了的情形都是 FactoryConfigError:

    - 载荷里有凭证值字段,或文件不是 config 信封:沿用原信息,里面只有路径与键名,没有值。
    - 其他情形(YAML 语法错误、信封不合 schema、schema_version 不认识、顶层不是映射、读文件失败):
      只写路径与异常类名。yaml 与 jsonschema 的信息会引用文件内容,可能带出凭证,所以不回显。
    """
    try:
        return read_config(store, rel)
    except FileNotFoundError:
        raise
    except (CredentialInGitTreeError, ConfigTypeError) as exc:
        raise FactoryConfigError(str(exc)) from None
    except Exception as exc:
        raise FactoryConfigError(
            f"{rel} cannot be read as a configuration product ({type(exc).__name__})"
        ) from None


def describe(exc: BaseException) -> str:
    """把外来异常说成一句不带配置取值的话,给 FactoryConfigError 的信息用。

    - ValueError(各包的校验错误):类名加信息,信息截在 ", got " 之前。校验信息的形状是"<字段> must be ..., got <取值>",
      期望的形状已经说完整,取值本身对操作者没有用,还可能是误填进来的凭证。
    - KeyError:键名是标识符时写出键名(缺了哪个键有用),否则只写类名。
    - 其余(TypeError、AttributeError、yaml 与 jsonschema 的错误):只写类名,它们的信息可能引用取值。
    版本号、变量名、路径里的名字这类标识符出现在信息里是有意的:操作者要靠它们找到文件。"""
    name = type(exc).__name__
    if isinstance(exc, KeyError) and exc.args and isinstance(exc.args[0], str) and exc.args[0].isidentifier():
        return f"{name}: {exc.args[0]!r}"
    if isinstance(exc, ValueError):
        text = str(exc)
        cut = text.find(", got ")
        return f"{name}: {text if cut == -1 else text[:cut]}"
    return name


def preflight(label: str, load: Callable[[], T]) -> T:
    """装配期预检:调一次只读的加载函数,失败一律转成 FactoryConfigError(命令行退出码 2,树里什么都不写)。
    返回加载的结果,但不缓存:处理器仍在每次调用时自己重读(计划 5,计划 9 Ruling 21),所以人改了配置下一次动作就生效。"""
    try:
        return load()
    except FactoryConfigError:
        raise
    except (CredentialInGitTreeError, ConfigTypeError) as exc:
        raise FactoryConfigError(f"{label}: {exc}") from None
    except Exception as exc:
        raise FactoryConfigError(f"{label}: {describe(exc)}") from None
