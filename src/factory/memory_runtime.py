"""组合根的记忆后端:替身运行时身份与替身解析器(设计二 7.1 节)。

派发器在进入策略上下文之前调用注入的 `memory_resolver(seed, persona_ids)`,拿回每个人格的起点记忆摘要与
记忆运行时身份,两者一起进 config_hash 与训练记录。本模块给的是替身那一路:每个人格都从空库起步,
没有任何种子库可用。计划 B 第 7 步在同一模块按 RISMFORGE_MEMORY_CONFIG 加 Starling 那一路。
"""
from __future__ import annotations

import copy

from actio.memory import TupleMemory
from actio.trace import TRACE_SCHEMA_V2
from controlplane.daemon import DirectiveFailedError

STAND_IN_MEMORY_RUNTIME: dict = {
    "backend": "stand-in",
    "starling_version": None,
    "extraction": None,
    "embedding": None,
    "trace_schema": TRACE_SCHEMA_V2,
}


def stand_in_memory_resolver(seed: str | None, persona_ids: tuple[str, ...]) -> tuple[dict[str, str], dict]:
    """替身解析:seed 为 null 时每个人格得到空库摘要,runtime 是身份的一份拷贝(调用方改它不伤常量)。

    seed 非 null 抛 DirectiveFailedError 而不是普通异常:派发器对普通异常只报类名,而种子需要记忆配置
    这件事要原样告诉人。原因里只有种子 id 与变量名,不含路径。"""
    if seed is not None:
        raise DirectiveFailedError(
            f"memory.seed {seed!r} needs a memory config: set RISMFORGE_MEMORY_CONFIG "
            "(the stand-in memory has no seed libraries)"
        )
    empty = TupleMemory().digest()
    return {p: empty for p in persona_ids}, copy.deepcopy(STAND_IN_MEMORY_RUNTIME)
