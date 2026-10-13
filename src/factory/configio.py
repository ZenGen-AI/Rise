"""装配期读配置文件的唯一入口:把"这份文件读不了"的各种形态统一成 FactoryConfigError(命令行退出码 2)。"""
from __future__ import annotations

from controlplane.config import ConfigTypeError, CredentialInGitTreeError, read_config
from controlplane.gitstore import GitStore
from factory.errors import FactoryConfigError


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
