"""组合根的替身记忆解析器(设计二 7.1 节):--interact 装配时注入,先于策略上下文解出起点记忆。"""
from __future__ import annotations

import pytest

from actio.memory import TupleMemory
from controlplane.daemon import DirectiveFailedError
from factory.memory_runtime import STAND_IN_MEMORY_RUNTIME, stand_in_memory_resolver
from memory_fixtures import STAND_IN_RUNTIME  # Task 24 的测试夹具先于本模块存在,这里钉住两份相等


def test_the_stand_in_runtime_identity_is_the_one_the_design_fixes():
    assert STAND_IN_MEMORY_RUNTIME == STAND_IN_RUNTIME
    assert STAND_IN_MEMORY_RUNTIME == {
        "backend": "stand-in", "starling_version": None, "extraction": None, "embedding": None,
        "trace_schema": "actio.trace.v2",
    }


def test_every_persona_starts_from_the_empty_stand_in_library():
    digests, runtime = stand_in_memory_resolver(None, ("pa", "pb"))
    assert digests == {"pa": TupleMemory().digest(), "pb": TupleMemory().digest()}
    assert runtime == STAND_IN_MEMORY_RUNTIME


def test_the_returned_identity_is_a_copy():
    _, runtime = stand_in_memory_resolver(None, ("pa",))
    runtime["backend"] = "x"
    assert STAND_IN_MEMORY_RUNTIME["backend"] == "stand-in"


def test_a_seed_needs_a_memory_config():
    with pytest.raises(DirectiveFailedError, match="needs a memory config") as info:
        stand_in_memory_resolver("seed-1", ("pa",))
    assert "RISMFORGE_MEMORY_CONFIG" in info.value.reason and info.value.category == "T2"
