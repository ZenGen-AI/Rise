"""规划处理器(设计 §7.2、§4.1、§3.2):plan_next_round 动作的 Handler。

流程:装配上下文 → plan(validator = validate_decision)→ 分配 run id → 写 planner_decision(pending,
带 next_run_id)→ 解析动作 → 写指令 → 决策置 executed。

重入(崩在任何一步之后再被调用):
  (a) 决策 pending、带 next_run_id、那条指令还不在:按记下的决策与 run id 续写,不重新规划、不重新分配。
  (b) 决策 pending、next_run_id 的指令已在:只把决策补成 executed,不写第二条指令。
  (c) 决策已是 executed 或 failed:空操作。

失败:
  - PlannerFailedError(规划耗尽)、装配/解析/构造的确定性错误(SchedulerError)、任何 BridgeError
    (T1 与 T2 都算,设计 R18:T1 表示桥接已用尽自己的退避重试):决策置 failed
    (payload.failure = {category: T3, reason, attempts}),record_failure 写 T3,task_id = plan-<run>,不上抛。
  - 非桥接的异常(OSError、子进程错误、配置错 SchedulerConfigError、模板错 TemplateError 等):原样上抛,
    决策保持原状(没写过就不写,已是 pending 就仍是 pending),下一轮重来。
"""
from __future__ import annotations

from collections.abc import Callable

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.ledger import record_failure
from controlplane.paths import planner_decision_file
from controlplane.reconcile import Action
from controlplane.states import NONREVIEW, assert_transition
from inference_bridge import BridgeError
from trainplanner.backends import DecisionBackend
from trainplanner.decision import DecisionParseError, PlannerDecision
from trainplanner.planner import PlannerFailedError, plan
from trainplanner.templates import PromptTemplate, load_template
from trainscheduler.assemble import assemble_context, directive_file
from trainscheduler.config import (
    SchedulerConfig,
    TrainingDefaults,
    load_scheduler_config,
    load_training_defaults,
)
from trainscheduler.directive import construct_directive
from trainscheduler.errors import ResolveError, SchedulerError
from trainscheduler.experience_port import record_and_recompute
from trainscheduler.resolve import resolve_action, validate_decision
from trainscheduler.run_ids import allocate_run_id, run_number

Handler = Callable[[GitStore, Action], None]
TemplateLoader = Callable[[GitStore, str], PromptTemplate]
ConfigLoader = Callable[[GitStore], SchedulerConfig]
DefaultsLoader = Callable[[GitStore], TrainingDefaults]

NOW = "2026-10-09T00:00:00Z"
FAILURE_CATEGORY = "T3"
FAILURE_POINT = "trainscheduler.plan"


def decision_id(run_id: str) -> str:
    return f"planner-{run_id}"


def plan_task_id(run_id: str) -> str:
    return f"plan-{run_id}"


def _decision_envelope(source_run_id: str, payload: dict, status: str) -> Envelope:
    links = [source_run_id] + ([payload["next_run_id"]] if payload.get("next_run_id") else [])
    return Envelope(
        id=decision_id(source_run_id), type="planner_decision", version=1, created_at=NOW,
        created_by=OwnerRef(), status=status, schema_version=CURRENT_SCHEMA_VERSION,
        links=links, payload=payload,
    )


def _base_payload(source_run_id: str, template_version: str, backend: str) -> dict:
    return {
        "source_run_id": source_run_id,
        "next_run_id": None,
        "template_version": template_version,
        "backend": backend,
        "decision": None,
        "rationale": "",
        "failure": None,
    }


def _fail(
    store: GitStore, run_id: str, payload: dict, reason: str, attempts: int, *, log_ref=None
) -> None:
    """把决策置 failed(没写过就直接以 failed 写出),并写一份 T3 失败记录。"""
    path = planner_decision_file(run_id)
    payload = dict(payload)
    payload["failure"] = {"category": FAILURE_CATEGORY, "reason": reason, "attempts": attempts}
    if store.exists(path):
        assert_transition(NONREVIEW, store.read_product(path).status, "failed")
    store.write_product(
        path, _decision_envelope(run_id, payload, "failed"), f"trainscheduler: plan {run_id} failed"
    )
    record_failure(
        store, run_id, task_id=plan_task_id(run_id), category=FAILURE_CATEGORY,
        point=FAILURE_POINT, retries=max(attempts - 1, 0), reason=reason, log_ref=log_ref,
    )


def _mark_executed(store: GitStore, run_id: str) -> None:
    path = planner_decision_file(run_id)
    env = store.read_product(path)
    assert_transition(NONREVIEW, env.status, "executed")
    env.status = "executed"
    store.write_product(path, env, f"trainscheduler: plan {run_id} executed")


def _recorded_decision(payload: dict) -> PlannerDecision:
    """从 pending 决策的载荷里读回决策。载荷被人改坏(缺键、类型不对、过不了解析规则)抛 ResolveError,
    不让 DecisionParseError、TypeError 之类每个 tick 逃出守护循环。"""
    decision = payload.get("decision")
    if not isinstance(decision, dict):
        raise ResolveError(f"recorded decision is not a mapping: {type(decision).__name__}")
    try:
        return PlannerDecision.from_dict({**decision, "rationale": payload.get("rationale")})
    except DecisionParseError as exc:
        raise ResolveError(f"recorded decision is malformed: {exc.reason}") from exc


def _usable_next_run_id(value: object) -> bool:
    """只接受 run-<数字>:分配器只产出这种形状;空值、None、带斜杠的路径都是被改坏的载荷。"""
    return isinstance(value, str) and run_number(value) is not None


def _execute(store: GitStore, run_id: str, payload: dict, config: SchedulerConfig, defaults: TrainingDefaults) -> None:
    """决策已以 pending 写下:解析、写指令、标记 executed。确定性错误把决策置 failed。"""
    try:
        if not _usable_next_run_id(payload.get("next_run_id")):
            raise ResolveError(f"pending decision has no usable next_run_id: {payload.get('next_run_id')!r}")
        resolved = resolve_action(store, _recorded_decision(payload), config=config, defaults=defaults)
        construct_directive(
            store, resolved, new_run_id=payload["next_run_id"],
            confirm=config.confirm_directives, planner_decision_id=decision_id(run_id),
        )
    except SchedulerError as exc:
        _fail(store, run_id, payload, exc.reason, attempts=0)
        return
    _mark_executed(store, run_id)


class _CountingBackend:
    """透传给真实后端,只数调用次数:桥接失败时写进 failure.attempts 的是"规划已问了几次"。"""

    def __init__(self, inner: DecisionBackend) -> None:
        self._inner = inner
        self.name = inner.name
        self.calls = 0

    def decide(self, packed, *, run_id: str, task_id: str) -> str:
        self.calls += 1
        return self._inner.decide(packed, run_id=run_id, task_id=task_id)


def make_plan_handler(
    *,
    backend: DecisionBackend,
    template_loader: TemplateLoader = load_template,
    config_loader: ConfigLoader = load_scheduler_config,
    defaults_loader: DefaultsLoader = load_training_defaults,
) -> Handler:
    """三个 loader 每次调用处理器时各调一次,所以人改了树里的配置,下一次规划就生效。"""

    def handle(store: GitStore, action: Action) -> None:
        run_id = action.target_id
        path = planner_decision_file(run_id)
        if store.exists(path):
            env = store.read_product(path)
            if env.status != "pending":
                return  # (c)
            next_run = env.payload.get("next_run_id")
            if _usable_next_run_id(next_run) and store.exists(directive_file(next_run)):
                _mark_executed(store, run_id)  # (b)
                return
            _execute(store, run_id, env.payload, config_loader(store), defaults_loader(store))  # (a)
            return

        config = config_loader(store)
        try:
            record_and_recompute(store, run_id, config=config)
        except Exception:
            # 落账是旁路:它坏了不能挡住规划。坏输入已在 collect_record 里降级为 ignored
            pass
        defaults = defaults_loader(store)
        template = template_loader(store, config.template_version)
        payload = _base_payload(run_id, template.version, backend.name)
        counted = _CountingBackend(backend)
        try:
            ctx = assemble_context(store, run_id, config=config)
            result = plan(
                ctx, template.text, template.version, counted, run_id=run_id,
                max_parse_retries=config.max_plan_attempts - 1,
                validator=lambda d: validate_decision(store, d, config=config, defaults=defaults),
            )
        except PlannerFailedError as exc:
            _fail(store, run_id, payload, exc.reason, attempts=exc.attempts)
            return
        except SchedulerError as exc:
            _fail(store, run_id, payload, exc.reason, attempts=0)
            return
        except BridgeError as exc:
            # R18:T1 与 T2 一律升级为 T3。桥接已退避重试过,留作 pending 会让守护循环重启后无限空转外部推理
            _fail(
                store, run_id, payload,
                f"bridge {exc.category} after {exc.attempts} bridge attempt(s): {exc.reason}",
                attempts=counted.calls, log_ref=exc.log_ref,
            )
            return
        payload.update(result.record_fields())
        try:
            payload["next_run_id"] = allocate_run_id(store)
        except SchedulerError as exc:
            _fail(store, run_id, payload, exc.reason, attempts=result.attempts)
            return
        store.write_product(
            path, _decision_envelope(run_id, payload, "pending"),
            f"trainscheduler: plan {run_id} -> {payload['next_run_id']}",
        )
        _execute(store, run_id, payload, config, defaults)

    return handle
