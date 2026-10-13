"""收录处理器 admit_proposal(闭环集成设计 §3.2、§8.3):approved 的提案写进正式库。

顺序:库条目(skill_file / memory_file,effective)→ library_state(版本 +1、记来源提案)→ 提案 effective。
每一步重入安全:条目已在就不重写;library_state 已记过该提案就不再递增;所以崩在任何两步之间重跑都收敛。
"""
from __future__ import annotations

from datetime import datetime, timezone

from actio.errors import ActioDataError
from actio.memory import MemoryEntry
from actio.skills import Skill
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import library_state_file, memory_file, proposal_file, skill_file
from controlplane.reconcile import Action
from controlplane.states import assert_transition
from mindagent.errors import MindAgentDataError
from mindagent.failures import fail_once
from mindagent.library import LIBRARY_STATE_ID, memory_id_for, read_library_versions, skill_id_for


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entries(proposal_id: str, increments: object) -> list[tuple[str, str, dict]]:
    """[(kind, 条目 id, 条目载荷)]。任何一条不合法抛 ValueError(在写任何东西之前)。"""
    if not isinstance(increments, list) or not increments:
        raise ValueError("increments must be a non-empty list")
    out = []
    for k, inc in enumerate(increments, start=1):
        if not isinstance(inc, dict) or not isinstance(inc.get("definition"), dict):
            raise ValueError(f"increment {k} is malformed: {inc!r}")
        kind = inc.get("kind")
        definition = dict(inc["definition"])
        try:
            if kind == "skill":
                definition["id"] = skill_id_for(proposal_id, k)
                out.append(("skill", definition["id"], Skill.from_dict(definition).to_dict()))
            elif kind == "memory":
                definition["id"] = memory_id_for(proposal_id, k)
                out.append(("memory", definition["id"], MemoryEntry.from_dict(definition).to_dict()))
            else:
                raise ValueError(f"increment {k}: unknown kind {kind!r}")
        except ActioDataError as exc:
            raise ValueError(f"increment {k}: {exc}") from None
    return out


def _entry_envelope(kind: str, entry_id: str, payload: dict, proposal_id: str) -> Envelope:
    return Envelope(
        id=entry_id, type=kind, version=1, created_at=_now(), created_by=OwnerRef(),
        status="effective", schema_version=1, links=[proposal_id], payload=payload,
    )


def handle_admit_proposal(store: GitStore, action: Action) -> None:
    proposal_id = action.target_id
    rel = proposal_file(proposal_id)
    proposal = store.read_product(rel)
    if proposal.status != "approved":
        return  # effective 已收录;其余状态不归本处理器
    task_id = f"admit-{proposal_id}"
    run_id = proposal.payload.get("run_id")
    try:
        entries = _entries(proposal_id, proposal.payload.get("increments"))
        # 库版本在写任何条目之前就校验:不是十进制数字串时无法递增,按确定性失败处理
        state = read_library_versions(store)
    except (ValueError, MindAgentDataError) as exc:
        # 审核类状态机里 approved 只能去 effective,无处可放"失败":记一次 T2(确定性数据错误,处理器
        # 空操作,设计 §11),提案原样留在 approved 等人处理。run_id 不是单个路径段或 run 目录不在时
        # 不写失败记录(fail_once 的守卫)。之后每个 tick 重入都只是读一遍、什么都不写。
        fail_once(store, run_id, task_id=task_id, category="T2",
                  point="mindagent.admit", retries=0, reason=str(exc))
        return

    for kind, entry_id, payload in entries:
        path = skill_file(entry_id) if kind == "skill" else memory_file(entry_id)
        if not store.exists(path):
            store.write_product(path, _entry_envelope(kind, entry_id, payload, proposal_id),
                                f"mindagent: admit {entry_id} from {proposal_id}")

    admitted = list(state["admitted"])
    if all(a.get("proposal_id") != proposal_id for a in admitted):
        kinds = {kind for kind, _, _ in entries}
        # 版本存成十进制字符串(设计 §8.3);递增时按整数解析
        skill_v = str(int(state["skill_version"]) + (1 if "skill" in kinds else 0))
        memory_v = str(int(state["memory_version"]) + (1 if "memory" in kinds else 0))
        admitted.append({
            "proposal_id": proposal_id,
            "skill_version": skill_v,
            "memory_version": memory_v,
            "entries": [entry_id for _, entry_id, _ in entries],
        })
        lib_rel = library_state_file()
        version = store.read_product(lib_rel).version + 1 if store.exists(lib_rel) else 1
        env = Envelope(
            id=LIBRARY_STATE_ID, type="library_state", version=version, created_at=_now(),
            created_by=OwnerRef(), status="executed", schema_version=1, links=[proposal_id],
            payload={"skill_version": skill_v, "memory_version": memory_v, "admitted": admitted},
        )
        store.write_product(lib_rel, env, f"mindagent: library -> skill v{skill_v}, memory v{memory_v}")

    assert_transition("review", proposal.status, "effective")
    proposal.status = "effective"
    store.write_product(rel, proposal, f"mindagent: {proposal_id} effective")
