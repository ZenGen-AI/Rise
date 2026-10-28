"""记忆段(TrainingConfig.memory)测试共用的夹具(不是测试文件,pytest 不收集)。"""
from __future__ import annotations

STAND_IN_RUNTIME = {
    "backend": "stand-in", "starling_version": None, "extraction": None, "embedding": None,
    "trace_schema": "actio.trace.v2",
}


def unresolved_memory(seed=None) -> dict:
    return {"seed": seed, "digests": {}, "runtime": {}}


def resolved_memory(persona_ids, *, seed=None, digest="d0") -> dict:
    return {"seed": seed, "digests": {p: digest for p in persona_ids}, "runtime": dict(STAND_IN_RUNTIME)}


def fake_memory_resolver(seed, persona_ids):
    return {p: f"digest-{p}" for p in persona_ids}, dict(STAND_IN_RUNTIME)
