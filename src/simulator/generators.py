from __future__ import annotations

from simulator.datamodel import Persona, Scenario
from simulator.backends import GenerationBackend


def generate_persona(backend: GenerationBackend, seed: int) -> Persona:
    """自动生成路：委托可插拔生成后端。产出 source="auto"。"""
    return backend.make_persona(seed)


def generate_scenario(backend: GenerationBackend, seed: int, purpose: str) -> Scenario:
    """自动生成路：委托可插拔生成后端，透传 purpose 标记。"""
    return backend.make_scenario(seed, purpose)


def load_persona(d: dict) -> Persona:
    """外部配置路：读取人工预置的人格定义。无论外部数据如何标记，
    加载路统一强制 source="external"，保证来源可追溯（§2.2 两路输入）。"""
    p = Persona.from_dict(d)
    p.source = "external"
    return p


def load_scenario(d: dict) -> Scenario:
    """外部配置路：读取人工预置的情境定义，强制 source="external"。"""
    s = Scenario.from_dict(d)
    s.source = "external"
    return s
