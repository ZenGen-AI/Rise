"""记忆段(TrainingConfig.memory)测试共用的夹具(不是测试文件,pytest 不收集)。"""
from __future__ import annotations

from mindtrainer.dispatch import make_mindtrainer_dispatcher

STAND_IN_RUNTIME = {
    "backend": "stand-in", "starling_version": None, "extraction": None, "embedding": None,
    "trace_schema": "simple_actio.trace.v2",
}


def unresolved_memory(seed=None) -> dict:
    return {"seed": seed, "digests": {}, "runtime": {}}


def resolved_memory(scenario_ids, *, seed=None, digest="d0") -> dict:
    """digests 的键是情境 id,不是人格 id(起点是每个情境的世界)。"""
    return {"seed": seed, "digests": {s: digest for s in scenario_ids}, "runtime": dict(STAND_IN_RUNTIME)}


def fake_memory_resolver(seed, scenario_ids):
    return {s: f"digest-{s}" for s in scenario_ids}, dict(STAND_IN_RUNTIME)


def interact_dispatcher(**kw):
    """interact 指令从此必带 memory,派发器也就必须有解析器;调用方没给就补替身解析器。"""
    kw.setdefault("memory_resolver", fake_memory_resolver)
    return make_mindtrainer_dispatcher(**kw)
