"""收录处理器 admit_proposal(闭环集成设计 §3.2、§8.3,设计二第 9 节):approved 的提案写进正式库。

顺序(make_admit_proposal_handler 返回的处理器):
1. 读提案,不是 approved 就返回;增量与库版本先校验,不合法记一次 T2、提案留在 approved。
2. 记忆增量所涉人格逐个拿部署库锁,并查该人格有没有未提交的部署起点副本:锁被占或有在途 episode
   就抛 DeferredError(守护进程不写树、按退避重试),否则轮次重入覆盖部署库时会抹掉刚收录的记忆。
   任何人格有未提交的起点副本时,只含技能的提案也延后:在途轮次重入会重读库,技能版本一变,已提交的
   episode 就换新 id 重跑、记下新记忆,随后在途那条的起点副本覆盖回去,把这些记忆悄悄抹掉。
3. 记忆增量按位置逐个 remember_event 写进该人格的部署库,事件 key 是 <proposal_id>#<k>。记忆先于技能写:
   抽取失败要整份提案转 rejected,技能条目已写成 effective 就收不回了。
   - remember_event 抛错(库打不开、③ 写失败):记一次失败记录,提案留在 approved,不放弃事件;下一 tick
     重入时同 key 同文本返回幂等的同一批语句 id,不二次抽取。
   - 抽取失败按 failure_category 分两路(终审 I-1)。timeout、transport_failure 是上游网络的瞬态:不论前面
     有没有提交过,都不放弃事件,记 T1(原因含 key 与类别),抛 DeferredError 退避;重入时同 key 再抽一次。
   - 其余类别(含未知的)是确定性的。本提案没有已提交的记忆:放弃该事件(终态),记 T2,提案转 rejected 并带
     reject_reason。前面已有记忆提交(部分收录):不转 rejected(已提交的放弃不了)、也不放弃这一条,记 T3
     (partial admission),提案留在 approved,不抛延后,直接返回,等人处理。
   - 部分收录的 T3 记在单独的 task id admit-<proposal_id>-partial 下:fail_once 每个 (run, task) 只写一次,
     同一份提案先前的 T1(瞬态延后)、T2(抛异常)记录会挡住它。调和循环对 approved 提案每个 tick 都派一次
     admit_proposal,所以处理器一开始(拿锁之前)查这份记录,类别是 T3 就直接返回,不再每 tick 抽取一次。
     T1、T2 的记录不挡重入。run_id 过不了 fail_once 的守卫时 T3 写不下,之后每 tick 会再抽一次。
   - remember_event 撞上 MemoryEventConflict 且本提案还没有已提交的记忆:多半是上一次放弃了事件、
     没来得及写 rejected 就崩了。再调一次 abandon_event(已放弃的是空操作、已提交的会抛),成功就照常
     转 rejected;它抛就按上面的"抛错"记一次失败记录,提案不动。
4. 技能条目写 skill_file(effective);条目已在就不重写。
5. library_state 版本 +1、记来源提案,entries 里记忆一项就是事件 key;已记过该提案就不再递增。
6. 提案载荷加 admitted_statements(只作证据,真正的收据在部署库的事件表里),转 effective。
崩在任何两步之间重跑都收敛。
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timezone

from actio.errors import ActioDataError
from actio.memory_port import PERSONA_ID_PATTERN, DeploymentMemoryStore, MemoryEventConflict, MemoryFailureError
from actio.skills import Skill
from controlplane.deferred import DeferredError
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.lock import FileLockBusyError
from controlplane.paths import library_state_file, proposal_file, skill_file
from controlplane.reconcile import Action
from controlplane.states import assert_transition
from mindagent.errors import MindAgentDataError
from mindagent.failures import fail_once, recorded_category
from mindagent.library import LIBRARY_STATE_ID, read_library_versions, skill_id_for
from mindagent.memory_layout import MemoryLayout

MEMORY_DEFINITION_KEYS = frozenset({"persona_id", "text", "interlocutor"})
# Starling 的抽取失败类别里上游网络的瞬态(设计源 M21);其余已知类别与未知类别都是确定性的
TRANSIENT_EXTRACTION_CATEGORIES = frozenset({"timeout", "transport_failure"})

Handler = Callable[[GitStore, Action], None]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _memory_definition(k: int, definition: dict) -> dict:
    """记忆增量的 definition:恰好三个键;persona_id 拼进库文件名,要过 PERSONA_ID_PATTERN;
    text 非空;interlocutor 是字符串,空串表示一般事实。"""
    if set(definition) != MEMORY_DEFINITION_KEYS:
        raise ValueError(f"increment {k}: memory definition must have exactly {sorted(MEMORY_DEFINITION_KEYS)}")
    pid = definition["persona_id"]
    if not isinstance(pid, str) or not PERSONA_ID_PATTERN.fullmatch(pid):
        raise ValueError(f"increment {k}: memory persona_id is not a valid persona id: {pid!r}")
    if not isinstance(definition["text"], str) or not definition["text"]:
        raise ValueError(f"increment {k}: memory text must be a non-empty string")
    if not isinstance(definition["interlocutor"], str):
        raise ValueError(f"increment {k}: memory interlocutor must be a string")
    return definition


def _entries(proposal_id: str, increments: object) -> list[tuple[str, str, dict]]:
    """[(kind, 条目 id, 条目载荷)]。技能的条目 id 是 skill_id_for(...),记忆的是事件 key
    <proposal_id>#<k>(k 是增量在提案里的位置,从 1 起)。任何一条不合法抛 ValueError(在写任何东西之前)。"""
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
                out.append(("memory", f"{proposal_id}#{k}", _memory_definition(k, definition)))
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


def make_admit_proposal_handler(*, memory_store: DeploymentMemoryStore, memory_layout: MemoryLayout) -> Handler:
    """memory_store 与 memory_layout 是部署记忆,与轮次处理器共用同一份(锁与"打开着的端口"都按它算)。"""

    def handle(store: GitStore, action: Action) -> None:
        proposal_id = action.target_id
        rel = proposal_file(proposal_id)
        proposal = store.read_product(rel)
        if proposal.status != "approved":
            return  # effective 已收录;其余状态不归本处理器
        task_id = f"admit-{proposal_id}"
        partial_task_id = f"{task_id}-partial"
        run_id = proposal.payload.get("run_id")
        if recorded_category(store, run_id, partial_task_id) == "T3":
            return  # 部分收录已确定性失败、等人处理:不拿锁、不再抽取(见模块 docstring 第 3 步)
        try:
            entries = _entries(proposal_id, proposal.payload.get("increments"))
            # 库版本在写任何条目之前就校验:不是十进制数字串时无法递增,按确定性失败处理
            state = read_library_versions(store)
        except (ValueError, MindAgentDataError) as exc:
            # 记一次 T2(确定性数据错误,处理器空操作,设计 §11),提案原样留在 approved 等人处理。
            # run_id 不是单个路径段或 run 目录不在时不写失败记录(fail_once 的守卫)。之后每个 tick 重入
            # 都只是读一遍、什么都不写。
            fail_once(store, run_id, task_id=task_id, category="T2",
                      point="mindagent.admit", retries=0, reason=str(exc))
            return

        memories = [(key, d) for kind, key, d in entries if kind == "memory"]
        with ExitStack() as locks:
            persona_ids = sorted({d["persona_id"] for _, d in memories})
            for pid in persona_ids:
                try:
                    locks.enter_context(memory_layout.persona_lock(pid))
                except FileLockBusyError as exc:
                    raise DeferredError(f"persona library locked: {exc}") from exc
            for pid in persona_ids:
                in_flight = memory_layout.uncommitted_snapshots(pid)
                if in_flight:
                    names = ", ".join(p.name for p in in_flight)
                    raise DeferredError(f"persona {pid} has an uncommitted deployment episode: {names}")
            in_flight = memory_layout.any_uncommitted_snapshots()
            if in_flight:
                # 只含技能的提案也延后:库版本在轮次在途时不许变(见模块 docstring 第 2 步)
                names = ", ".join(f"{p.parent.name}/{p.name}" for p in in_flight)
                raise DeferredError(f"admission waits for uncommitted deployment episodes: {names}")

            def reject(reason: str) -> None:
                fail_once(store, run_id, task_id=task_id, category="T2", point="mindagent.admit.memory",
                          retries=0, reason=reason)
                assert_transition("review", proposal.status, "rejected")
                proposal.status = "rejected"
                proposal.payload["reject_reason"] = reason
                store.write_product(rel, proposal, f"mindagent: {proposal_id} rejected (memory extraction)")

            statements: dict[str, list[str]] = {}
            for key, d in memories:
                pid = d["persona_id"]
                try:
                    try:
                        outcome = memory_store.remember_event(pid, key, d["text"], interlocutor=d["interlocutor"])
                    except MemoryEventConflict:
                        if statements:
                            raise
                        # 上一次放弃了这条、没写成 rejected 就崩了:再放弃一次(已放弃是空操作,已提交会抛)
                        memory_store.abandon_event(pid, key)
                        reject(f"extraction failed for {key}: event was abandoned earlier")
                        return
                    transient = outcome.failed and outcome.category in TRANSIENT_EXTRACTION_CATEGORIES
                    if outcome.failed and not statements and not transient:
                        memory_store.abandon_event(pid, key)
                except (MemoryFailureError, OSError, ActioDataError) as exc:
                    # 不改提案、不放弃事件:留在 approved,下一 tick 重入同 key 补收据(只报类名)。
                    # ActioDataError:库拒收这条事件(替身要求 interlocutor 非空),登记之前就拒,库没动;
                    # 不接的话它从处理器漏出去打崩守护进程。冲突后 abandon_event 也抛(同 key 已提交)时落在这里
                    why = f"remember {key}: {type(exc).__name__}"
                    if isinstance(exc, ActioDataError) and d["interlocutor"] == "":
                        # 终审 M-6:点明原因,否则人看不出提案为什么一直停在 approved(仍不带正文)
                        why += " (the stand-in library cannot store a general fact: interlocutor is empty)"
                    fail_once(store, run_id, task_id=task_id,
                              category="T1" if getattr(exc, "transient", False) else "T2",
                              point="mindagent.admit.memory", retries=0, reason=why)
                    return
                if transient:
                    # 不放弃事件、不改提案:同 key 留 pending,退避后重入再抽一次(连续延后有上限)
                    fail_once(store, run_id, task_id=task_id, category="T1", point="mindagent.admit.memory",
                              retries=0, reason=f"extraction {key}: {outcome.category}")
                    raise DeferredError(f"extraction of {key} failed transiently: {outcome.category}")
                if outcome.failed and statements:
                    # 确定性的部分收录:记 T3、留 approved、不抛延后;下一 tick 入口见到这份记录就返回
                    fail_once(store, run_id, task_id=partial_task_id, category="T3",
                              point="mindagent.admit.memory", retries=0,
                              reason=f"partial admission: {key} {outcome.category} after {len(statements)} committed")
                    return
                if outcome.failed:
                    reject(f"extraction failed for {key}: {outcome.category}")
                    return
                statements[key] = list(outcome.stored)

            for kind, entry_id, payload in entries:
                if kind != "skill":
                    continue
                path = skill_file(entry_id)
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

            proposal.payload["admitted_statements"] = statements
            assert_transition("review", proposal.status, "effective")
            proposal.status = "effective"
            store.write_product(rel, proposal, f"mindagent: {proposal_id} effective")

    return handle
