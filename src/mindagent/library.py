"""正式技能库与记忆库的读取,以及库条目 id 的确定性推导(闭环集成设计 R11)。"""
from __future__ import annotations

from dataclasses import dataclass

from actio.memory import MemoryEntry
from actio.skills import Skill
from actio.trace import normalize_library_version
from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT, library_state_file
from mindagent.errors import MindAgentDataError

LIBRARY_STATE_ID = "library-state"
_PROPOSAL_PREFIX = "proposal-"


@dataclass(frozen=True)
class LibraryState:
    skills: tuple[Skill, ...]
    memories: tuple[MemoryEntry, ...]
    version: dict  # {"skill": str, "memory": str},与激活轨迹的 library_version 同型;空库是 "0"


def _decimal(value: object, what: str) -> str:
    """库版本必须是十进制数字串(手写的非负整数也接受,规范成字符串)。收录要按整数递增它,
    计划 6 允许的 "lib-3" 之类标识在正式库里不合法,抛 MindAgentDataError(确定性错误)。"""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    if isinstance(value, str) and value.isdecimal() and value.isascii():
        return value
    raise MindAgentDataError(f"library_state.{what} must be a decimal string, got {value!r}")


def read_library_versions(store: GitStore) -> dict:
    """library_state 的载荷;文件不存在时是空库 {skill_version: "0", memory_version: "0", admitted: []}。
    两个版本都校验为十进制数字串并规范成字符串;不合法抛 MindAgentDataError。"""
    rel = library_state_file()
    if not store.exists(rel):
        return {"skill_version": "0", "memory_version": "0", "admitted": []}
    state = dict(store.read_product(rel).payload)
    for key in ("skill_version", "memory_version"):
        state[key] = _decimal(state.get(key), key)
    if not isinstance(state.get("admitted", []), list):
        raise MindAgentDataError(f"library_state.admitted must be a list, got {state.get('admitted')!r}")
    state["admitted"] = list(state.get("admitted", []))
    return state


def load_library(store: GitStore) -> LibraryState:
    """读当前正式库:skill_library/、memory_library/ 下状态为 effective 的条目,按 id 排序;
    版本取自 library_state。条目结构不合法抛 actio 的 ActioDataError(确定性错误)。"""
    skills = []
    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/skill_library"):
        env = store.read_product(rel)
        if env.status == "effective":
            skills.append(Skill.from_dict(env.payload))
    memories = []
    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/memory_library"):
        env = store.read_product(rel)
        if env.status == "effective":
            memories.append(MemoryEntry.from_dict(env.payload))
    state = read_library_versions(store)
    return LibraryState(
        skills=tuple(sorted(skills, key=lambda s: s.id)),
        memories=tuple(sorted(memories, key=lambda m: m.id)),
        # 设计 §8.3:版本一律是字符串,经 actio 的 normalize_library_version 规范化(整数 3 → "3")
        version=normalize_library_version({"skill": state["skill_version"], "memory": state["memory_version"]}),
    )


def _stem(proposal_id: str) -> str:
    if not proposal_id.startswith(_PROPOSAL_PREFIX) or len(proposal_id) == len(_PROPOSAL_PREFIX):
        raise ValueError(f"proposal id must start with {_PROPOSAL_PREFIX!r}, got {proposal_id!r}")
    return proposal_id[len(_PROPOSAL_PREFIX):]


def skill_id_for(proposal_id: str, k: int) -> str:
    """提案里第 k 个(从 1 起)增量收录后的技能 id:proposal-X → skill-X-k。"""
    return f"skill-{_stem(proposal_id)}-{k}"


def memory_id_for(proposal_id: str, k: int) -> str:
    """提案里第 k 个(从 1 起)增量收录后的记忆 id:proposal-X → mem-X-k。"""
    return f"mem-{_stem(proposal_id)}-{k}"
