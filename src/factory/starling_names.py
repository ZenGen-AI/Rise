"""接记忆服务后的世界名、标签与 rise 设置(设计四;00 篇世界表与世界名规则,90 篇世界设置表)。纯函数。

- 世界名以前缀 `world_prefix`(缺省 `rise`,不含 `.`)开头,拼上种类与 id,所以能无歧义地拆开。Starling 只收首字符
  字母或数字、其余 `[A-Za-z0-9._-]`、1 到 128 个字符(`S:src/governance/world_store.cpp:47-60`)。
- 超过 128 个字符时把中间的 id 段换成它的 sha256 前 24 位;标签里照旧是完整的 id。
- 不提供"按名字判世界种类"的函数:重放世界只经管理面的 `open_replay` 打开,部署世界只经 `open`(40 篇 AC-34)。
- rise 设置写出 C++ 结构 WorldSettings 的每一个叶子(`S:include/starling/governance/world_settings.hpp:19-61`):
  建世界与分叉都按叶子合并,没写出的叶子会继承默认世界或来源世界的值,用户在看板一改就不一致。

本模块不导入 `starling`、`starling_client`;只用标准库、`simple_actio.memory_port.PERSONA_ID_PATTERN` 与
`factory.memory_config`(`WORLD_PREFIX_PATTERN` 的单一来源在那里,这里转出;第 3 步 memory_config 不导入本模块)。
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from pathlib import Path

from factory.memory_config import WORLD_PREFIX_PATTERN, MemoryConfig
from simple_actio.memory_port import PERSONA_ID_PATTERN

WORLD_NAME_MAX = 128
LONG_ID_DIGEST_CHARS = 24
SUFFIX_HEX_CHARS = 8
ROOT_LABEL_CHARS = 16
_SUFFIX_PATTERN = re.compile(r"[0-9a-f]{8}")

# source lane 的七个叶子:enabled 关掉(进程内 Memory 没有来源 lane,不关就改了召回语义),其余六个照 C++ 默认值,
# 逐个对过 S:include/starling/governance/world_settings.hpp:40-49(SourceLaneSettings)。看板把七个子键都写进默认世界
# (S:python/starling/dashboard/config.py:270-278),只写 enabled 时其余继承看板的值。
_SOURCE_LANE = {
    "enabled": False,
    "render_time": False,             # world_settings.hpp:42
    "tau_source": 0.56,               # world_settings.hpp:43
    "model": "text-embedding-v4",     # world_settings.hpp:44
    "dim": 1024,                      # world_settings.hpp:45
    "k_source": 30,                   # world_settings.hpp:46
    "chunk_byte_budget": 32000,       # world_settings.hpp:47
}
# 默认世界的后台开着(tick 30 秒、ingest、每 24 小时自动快照),不关的话部署世界会自己写,摘要漂移
_BACKGROUND = {"tick_interval_s": 0, "ingest": False, "auto_snapshot": {"on_session": False, "interval_hours": 0}}


def _check_prefix(prefix: str) -> str:
    if not isinstance(prefix, str) or not WORLD_PREFIX_PATTERN.fullmatch(prefix):
        raise ValueError("world prefix must match [A-Za-z0-9][A-Za-z0-9_-]{0,15}")
    return prefix


def _check_id(kind: str, value: str) -> str:
    if not isinstance(value, str) or not PERSONA_ID_PATTERN.fullmatch(value):
        raise ValueError(f"{kind} must match PERSONA_ID_PATTERN")
    return value


def _check_suffix(suffix: str) -> str:
    if not isinstance(suffix, str) or not _SUFFIX_PATTERN.fullmatch(suffix):
        raise ValueError("world suffix must be 8 lowercase hex chars")
    return suffix


def _short(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:LONG_ID_DIGEST_CHARS]


def _join(head: list[str], ids: list[str], tail: list[str], order: list[int]) -> str:
    """拼 `head + ids + tail`(以 `.` 连接)。超过 128 个字符时按 `order` 给出的下标逐段把 id 换成 sha256 前 24 位,
    直到不超长。"""
    ids = list(ids)
    name = ".".join([*head, *ids, *tail])
    for index in order:
        if len(name) <= WORLD_NAME_MAX:
            break
        ids[index] = _short(ids[index])
        name = ".".join([*head, *ids, *tail])
    if len(name) > WORLD_NAME_MAX:                       # 前缀至多 16 个字符,全换成摘要一定放得下
        raise ValueError("world name cannot be shortened to 128 characters")
    return name


def _longest_first(ids: list[str]) -> list[int]:
    return sorted(range(len(ids)), key=lambda i: -len(ids[i]))


def root_label(memory_root: Path) -> str:
    """`root` 标签:记忆根绝对路径(解开符号链接)的 sha256 前 16 位。跨记忆根不共用世界。"""
    return hashlib.sha256(str(Path(memory_root).resolve()).encode("utf-8")).hexdigest()[:ROOT_LABEL_CHARS]


def new_suffix() -> str:
    """rollout 与重放世界的随机后缀,8 位十六进制;在调用 `fork` 之前定下,超时后按确切世界名查。"""
    return secrets.token_hex(SUFFIX_HEX_CHARS // 2)


def deploy_world(prefix: str, scenario_id: str) -> str:
    """`<前缀>.deploy.<scenario_id>`。"""
    return _join([_check_prefix(prefix), "deploy"], [_check_id("scenario_id", scenario_id)], [], [0])


def empty_seed_world(prefix: str) -> str:
    """`<前缀>.seed-empty`:没有种子的情境从它分叉;`-` 让它不会和人起的种子 id 撞。"""
    return f"{_check_prefix(prefix)}.seed-empty"


def seed_world(prefix: str, seed_id: str, scenario_id: str, version: int) -> str:
    """`<前缀>.seed.<seed_id>.<scenario_id>.v<n>`;超长时 `seed_id` 与 `scenario_id` 从长到短逐段换。"""
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("seed version must be a positive integer")
    ids = [_check_id("seed_id", seed_id), _check_id("scenario_id", scenario_id)]
    return _join([_check_prefix(prefix), "seed"], ids, [f"v{version}"], _longest_first(ids))


def rollout_world(prefix: str, episode_id: str, suffix: str) -> str:
    """`<前缀>.ro.<episode_id>.<后缀>`;超长时 `episode_id` 换成摘要。"""
    return _join([_check_prefix(prefix), "ro"], [_check_id("episode_id", episode_id)], [_check_suffix(suffix)], [0])


def replay_world(prefix: str, round_id: str, scenario_id: str, suffix: str) -> str:
    """`<前缀>.verify.<round_id>.<scenario_id>.<后缀>`;超长时先换 `round_id`,再换 `scenario_id`。"""
    ids = [_check_id("round_id", round_id), _check_id("scenario_id", scenario_id)]
    return _join([_check_prefix(prefix), "verify"], ids, [_check_suffix(suffix)], [0, 1])


def labels(prefix: str, root: str, kind: str, **extra: str) -> dict[str, str]:
    """世界标签:公共的 `repo`、`root` 加 `kind`,再加这类世界的 id(完整的,不随世界名缩短)。"""
    return {"repo": _check_prefix(prefix), "root": root, "kind": kind, **extra}


def _endpoint(base_url: str, model: str, dim: int, config: MemoryConfig) -> dict:
    # provider_name 留空:非空时 Starling 改取那个 provider 的密钥与地址(S:python/starling/dashboard/engine.py:279-283);
    # enable_thinking 按叶子合并删不掉,写死 False,不抄默认世界的值
    return {"provider": "openai", "provider_name": "", "base_url": base_url, "model": model, "dim": dim,
            "max_tokens": 0, "timeout_ms": config.timeout_ms, "max_retries": config.max_retries,
            "enable_thinking": False, "thinking_type": ""}


def rise_settings(config: MemoryConfig) -> dict:
    """rise 设置(90 篇世界设置表):建世界、分叉、开句柄都传同一份。每次返回一份新字典。

    `chat`、`consolidation`、`gist_thresholds` 写 `None`:rise 不用,`None` 删掉端点、阈值回到 C++ 默认
    (`S:include/starling/governance/world_settings.hpp:67-68`)。"""
    return {
        "extraction": _endpoint(config.extraction_base_url, config.extraction_model, 0, config),
        "embedding": _endpoint(config.embedding_base_url, config.embedding_model, config.embedding_dim, config),
        "chat": None, "consolidation": None, "gist_thresholds": None,
        "vector_backend": "sqlite",
        "source_lane": dict(_SOURCE_LANE),
        "background": json.loads(json.dumps(_BACKGROUND)),
    }


def canonical_digest(obj: object) -> str:
    """规范化 JSON(键排序、无空白、不转义非 ASCII)的 sha256。rise 设置的摘要、服务抽取配置的摘要都用它。"""
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def settings_digest(settings: dict) -> str:
    """rise 设置规范化 JSON 的 sha256(进运行时身份的 `extraction.world_settings`),与键序无关。"""
    return canonical_digest(settings)
