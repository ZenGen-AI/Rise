"""装配期读配置文件与预检的入口:把"这份文件读不了"的各种形态统一成 FactoryConfigError(命令行退出码 2)。"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import TypeVar

from controlplane.config import ConfigTypeError, CredentialInGitTreeError, read_config
from controlplane.gitstore import GitStore
from factory.errors import FactoryConfigError

T = TypeVar("T")

PLACEHOLDER = "<value>"
_QUOTES = "'\""
_OPEN = {"[": "]", "{": "}", "(": ")"}
_RANGE = re.compile(r"\s*[-+]?[0-9]+(?:\.[0-9]+)?\s*,\s*[-+]?[0-9]+(?:\.[0-9]+)?\s*")
_AFTER_IN = re.compile(r"(?<!\S)in\s+\Z")
_LOOKBACK = 16


def read_tree_config(store: GitStore, rel: str) -> dict:
    """read_config 的包装。文件不存在照旧抛 FileNotFoundError,由调用方按场景措辞;其余读不了的情形都是 FactoryConfigError:

    - 载荷里有凭证值字段:沿用原信息,里面只有路径与键名,没有值。
    - 文件不是 config 信封:只写路径。信封的 type 字段是个自由字符串,可能被误填成凭证,所以不写它的取值。
    - 其他情形(YAML 语法错误、信封不合 schema、schema_version 不认识、顶层不是映射、读文件失败):
      只写路径与异常类名。yaml 与 jsonschema 的信息会引用文件内容,可能带出凭证,所以不回显。
    """
    try:
        return read_config(store, rel)
    except FileNotFoundError:
        raise
    except ConfigTypeError:
        raise FactoryConfigError(f"{rel} is not a configuration product (its type field is wrong)") from None
    except CredentialInGitTreeError as exc:
        raise FactoryConfigError(str(exc)) from None
    except Exception as exc:
        raise FactoryConfigError(
            f"{rel} cannot be read as a configuration product ({type(exc).__name__})"
        ) from None


def _skip_quote(text: str, i: int) -> int:
    """text[i] 是引号时,返回这个字符串结束后的位置;没有闭合就一直到结尾。反斜杠转义下一个字符。"""
    delim = text[i]
    j = i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == delim:
            return j + 1
        j += 1
    return len(text)


def _skip_group(text: str, i: int) -> tuple[int, bool]:
    """text[i] 是 [ { ( 之一时,返回这一组结束后的位置和它是否闭合。组内的引号字符串整体跳过,组可以嵌套。"""
    closers = [_OPEN[text[i]]]
    j = i + 1
    while j < len(text) and closers:
        c = text[j]
        if c in _QUOTES:
            j = _skip_quote(text, j)
            continue
        if c in _OPEN:
            closers.append(_OPEN[c])
        elif c == closers[-1]:
            closers.pop()
        j += 1
    return j, not closers


def redact_message(text: str) -> str:
    """把信息里的字面量换成 <value>:引号括起的字符串,以及括号括起的一整组(可嵌套)。
    只有紧跟在单词 in 之后、恰好是两个数字(整数或小数,可带正负号)用逗号隔开的括号组,是校验信息自己写的区间(如 in [0, 1]),原样保留;
    其余括号组一律换掉,哪怕里面全是数字:一个数字列表(如 [123456789])可能正是被误填的取值。引号或括号没有闭合时,从它开始到结尾都换掉。
    限制:被误填的取值若恰好是紧跟在 in 之后的两个数字(如 in [3, 4]),和校验信息自己写的区间长得一样,会被保留。花括号组一律换掉。"""
    out: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c in _QUOTES:
            i = _skip_quote(text, i)
            out.append(PLACEHOLDER)
        elif c in _OPEN:
            j, closed = _skip_group(text, i)
            if closed and c in "[(" and _AFTER_IN.search(text, max(0, i - _LOOKBACK), i) and _RANGE.fullmatch(text[i + 1:j - 1]):
                out.append(text[i:j])
            else:
                out.append(PLACEHOLDER)
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def key_names(keys: Iterable[object]) -> list[str]:
    """给错误信息列键名。键不一定是字符串(yaml 里可以写 1: 2),字符串和整数混着排序会抛 TypeError,所以先转成字符串。"""
    return sorted(str(k) for k in keys)


def describe(exc: BaseException) -> str:
    """把外来异常说成一句不带配置取值的话,给 FactoryConfigError 的信息用。

    - ValueError(各包的校验错误):类名加信息。先把信息截在 ", got " 之前(校验信息通常是"<字段> must be ..., got <取值>"),
      再用 redact_message 把剩下的引号字符串与括号组换成 <value>:有的校验器把取值写在分隔符之前,甚至没有分隔符。
      紧跟在 in 之后的两个数字的区间(如 in [0, 1])是校验信息自己写的,保留;别的数字组一律换成 <value>。
    - KeyError:键名是标识符时写出键名(缺了哪个键有用),否则只写类名。
    - 其余(TypeError、AttributeError、OverflowError、yaml 与 jsonschema 的错误):只写类名,它们的信息可能引用取值。
    版本号、变量名、路径里的名字这类标识符出现在信息里是有意的:操作者要靠它们找到文件。没有引号也没有括号的裸取值
    (例如 unknown mode x;或 backend 写成整数 123456789,被 !r 回显成 123456789)挡不住,新写的校验器要把取值放进 repr,或放在 ", got " 之后。"""
    name = type(exc).__name__
    if isinstance(exc, KeyError) and exc.args and isinstance(exc.args[0], str) and exc.args[0].isidentifier():
        return f"{name}: {exc.args[0]!r}"
    if isinstance(exc, ValueError):
        text = str(exc)
        cut = text.find(", got ")
        return f"{name}: {redact_message(text if cut == -1 else text[:cut])}"
    return name


def preflight(label: str, load: Callable[[], T]) -> T:
    """装配期预检:调一次只读的加载函数,失败一律转成 FactoryConfigError(命令行退出码 2,树里什么都不写)。
    返回加载的结果,但不缓存:处理器仍在每次调用时自己重读(计划 5,计划 9 Ruling 21),所以人改了配置下一次动作就生效。"""
    try:
        return load()
    except FactoryConfigError:
        raise
    except ConfigTypeError:
        raise FactoryConfigError(f"{label}: not a configuration product (its type field is wrong)") from None
    except CredentialInGitTreeError as exc:
        raise FactoryConfigError(f"{label}: {exc}") from None
    except Exception as exc:
        raise FactoryConfigError(f"{label}: {describe(exc)}") from None
