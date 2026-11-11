"""正式技能库的读取,以及库条目 id 的确定性推导(闭环集成设计 R11)。设计二起记忆不在这里:它在树外的部署库里。"""
from __future__ import annotations

from dataclasses import dataclass

from simple_actio.skills import Skill
from simple_actio.trace import normalize_library_version
from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT, library_state_file
from mindagent.errors import MindAgentDataError
from mindagent.failures import UNREADABLE

LIBRARY_STATE_ID = "library-state"
_PROPOSAL_PREFIX = "proposal-"


@dataclass(frozen=True)
class LibraryState:
    skills: tuple[Skill, ...]
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


def _read_entry(store: GitStore, rel: str):
    """库文件读不出(YAML 坏、信封校验失败、schema 版本无法迁移)转成 MindAgentDataError,点名文件。
    库文件不在调和循环的视图里,控制面 CP-17 的暂停规则管不到它;轮次处理器只接 MindAgentDataError
    与 SimpleActioDataError,别的异常会从处理器漏出去打崩守护进程。OSError 不在 UNREADABLE 里,原样上抛。"""
    try:
        return store.read_product(rel)
    except UNREADABLE as exc:
        raise MindAgentDataError(f"library file {rel} is unreadable ({type(exc).__name__})") from exc


def load_library(store: GitStore) -> LibraryState:
    """读当前正式库:skill_library/ 下状态为 effective 的条目,按 id 排序;版本取自 library_state。
    不读 memory_library/:设计二起记忆在树外的部署库里(设计二第 9 节"旧数据"),旧目录由
    check_memory_library_empty 在装配期把关。条目结构不合法抛 simple_actio 的 SimpleActioDataError,
    文件读不出抛 MindAgentDataError(都是确定性错误)。"""
    skills = []
    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/skill_library"):
        env = _read_entry(store, rel)
        if env.status == "effective":
            skills.append(Skill.from_dict(env.payload))
    state = read_library_versions(store)
    return LibraryState(
        skills=tuple(sorted(skills, key=lambda s: s.id)),
        # 设计 §8.3:版本一律是字符串,经 simple_actio 的 normalize_library_version 规范化(整数 3 → "3")
        version=normalize_library_version({"skill": state["skill_version"], "memory": state["memory_version"]}),
    )


def check_memory_library_empty(store: GitStore) -> None:
    """控制面的 memory_library/ 必须为空(设计二第 0、9 节):记忆改放树外的部署库,旧条目留在树里会被
    误当成已收录。只列文件、不读内容,所以坏文件也会被点名。非空抛 MindAgentDataError,由人工把文件移走。"""
    found = store.list_products(f"{CONTROL_PLANE_ROOT}/memory_library")
    if found:
        raise MindAgentDataError(
            f"memory_library/ must be empty (design 2 §0, §9): found {len(found)} file(s), "
            f"e.g. {found[0]}; move them out by hand"
        )


def _stem(proposal_id: str) -> str:
    if not proposal_id.startswith(_PROPOSAL_PREFIX) or len(proposal_id) == len(_PROPOSAL_PREFIX):
        raise ValueError(f"proposal id must start with {_PROPOSAL_PREFIX!r}, got {proposal_id!r}")
    return proposal_id[len(_PROPOSAL_PREFIX):]


def skill_id_for(proposal_id: str, k: int) -> str:
    """提案里第 k 个(从 1 起)增量收录后的技能 id:proposal-X → skill-X-k。"""
    return f"skill-{_stem(proposal_id)}-{k}"
