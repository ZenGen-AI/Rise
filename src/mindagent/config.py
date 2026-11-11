"""AgentConfig:注入轮次处理器工厂的冻结配置。不落进 git 树(树内持久化的 agent 配置不在本期)。"""
from __future__ import annotations

from dataclasses import dataclass

from simulator.datamodel import RoleBinding


def agent_slot_of(bindings: list[RoleBinding] | tuple[RoleBinding, ...]) -> str:
    """SimpleActio 实例占的角色位:唯一一个 model_backend 为 self_play 的绑定(被部署的就是它)。"""
    slots = [b.slot_id for b in bindings if b.model_backend == "self_play"]
    if len(slots) != 1:
        raise ValueError(f"bindings must contain exactly one self_play binding (the agent), got {slots}")
    return slots[0]


def _check_int(name: str, value: object, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an int >= {minimum}, got {value!r}")


def _check_ratio(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be a number in [0, 1], got {value!r}")


@dataclass(frozen=True)
class AgentConfig:
    scenario_ids: tuple[str, ...]
    bindings: tuple[RoleBinding, ...]
    max_turns: int = 6
    episodes_per_scenario: int = 1
    max_retries_per_episode: int = 1
    min_support: int = 3
    min_ratio: float = 0.5
    min_fired_on_motivating: float = 0.8
    max_fired_elsewhere: float = 0.2
    max_parse_retries: int = 2

    def __post_init__(self) -> None:
        if not self.scenario_ids or any(not isinstance(s, str) or not s for s in self.scenario_ids):
            raise ValueError(f"scenario_ids must be a non-empty tuple of ids, got {self.scenario_ids!r}")
        if len(set(self.scenario_ids)) != len(self.scenario_ids):
            raise ValueError(f"scenario_ids must not repeat, got {self.scenario_ids!r}")
        # 情境 id 拼进替身的文件名(deploy/<scenario>.json 等):大小写不敏感的文件系统上只差大小写的两个 id 撞文件
        if len({s.casefold() for s in self.scenario_ids}) != len(self.scenario_ids):
            raise ValueError(f"scenario_ids must not repeat ignoring case, got {self.scenario_ids!r}")
        agent_slot_of(self.bindings)
        _check_int("max_turns", self.max_turns, 1)
        _check_int("episodes_per_scenario", self.episodes_per_scenario, 1)
        _check_int("max_retries_per_episode", self.max_retries_per_episode, 0)
        _check_int("min_support", self.min_support, 1)
        _check_int("max_parse_retries", self.max_parse_retries, 0)
        for name in ("min_ratio", "min_fired_on_motivating", "max_fired_elsewhere"):
            _check_ratio(name, getattr(self, name))

    @property
    def agent_slot(self) -> str:
        return agent_slot_of(self.bindings)
