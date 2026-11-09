"""接记忆服务后的服务会话与运行时身份(设计四;90 篇运行时身份表、"服务会话 ServiceSession"一节、FC-42、FC-46、FC-47)。

第 3 步不接线:装配不导入本模块。`starling_client` 只由本模块与 `simple_actio/memory_starling.py` 导入
(`tests/test_factory_cross_plan.py` 守着),且一律在函数里惰性导入:没装 extra `memory` 时本模块照样能导入。
"""
from __future__ import annotations

from factory.memory_config import MemoryConfig
from factory.starling_names import canonical_digest, settings_digest
from simple_actio.trace import TRACE_SCHEMA_V2

# 90 篇"客户端超时"表(FC-46):句柄调用用 connect 的缺省 30 秒;管理调用(建世界、分叉、快照、恢复)要拷库,给 300 秒
HANDLE_TIMEOUT_S = 30.0
ADMIN_TIMEOUT_S = 300.0
# 排空窗口:一个会话丢失后 60 秒内,在它丢失时有可写句柄的世界上开句柄收到 world_held 判瞬态
DRAIN_WINDOW_S = 60.0


def service_runtime(info: dict, settings: dict, config: MemoryConfig) -> dict:
    """接服务后的记忆运行时身份(90 篇运行时身份表):五个顶层键不变,所以替身的身份常量不用动。

    - `starling_version` 取服务 `info()` 的版本、提交与协议;安装来源不再进身份,只在装配期与服务报的提交比对。
    - `extraction.prompts` 改为 `info()["extraction_config"]` 规范化 JSON 的 sha256;另加 `world_settings`:rise 设置的摘要。
    - 两个端点地址用配置里已规范化的值;`config.root` 不进身份。
    每次返回一份新字典。"""
    return {
        "backend": "starling",
        "starling_version": f"{info['version']}+git:{info['commit']}+protocol:{info['protocol']}",
        "extraction": {
            "base_url": config.extraction_base_url, "model": config.extraction_model,
            "timeout_ms": config.timeout_ms, "max_retries": config.max_retries,
            "prompts": canonical_digest(info["extraction_config"]),
            "world_settings": settings_digest(settings),
        },
        "embedding": {
            "base_url": config.embedding_base_url, "model": config.embedding_model, "dim": config.embedding_dim,
            "timeout_ms": config.timeout_ms, "max_retries": config.max_retries,
        },
        "trace_schema": TRACE_SCHEMA_V2,
    }
