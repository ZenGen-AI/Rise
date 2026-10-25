"""`--interact` 的策略工厂与对手后端切换(设计 90 §6.1 的"注入什么"与"对手位")。

派发器每派一条 interact 指令调用一次 `policy_factory(run_id, cfg)`,拿到一个上下文管理器。进入时按顺序:
1. 部署情境撞车检查(Ruling 24):本 run 的情境 id 若也在 agent.yaml 的部署情境里,判 failed,什么都不登记;
2. 解析训练位适配器与站内对手的适配器(都在占租约之前,确定性的错在这里就暴露);
3. 起 vLLM 服务(`policy_context`),在同一服务上造 vLLM 对手工厂,按需造外部 API 对手;
4. 把每个情境 id 登记到运行时(落盘钩子据此把 episode 写进本 run),调长流 deadline,交出策略。
退出时(正常、训练抛异常都一样)先注销情境、还原 deadline,再由 `policy_context` 停服务放租约。

对手后端经 `OpponentSwitch` 交给运行时:运行时在装配期就造好(它还要服务 MindAgent 部署),而 vLLM 对手要用
会话里才有的 `ServiceHandle`,所以运行时拿到的是这个可切换的工厂,策略上下文进入时装上真工厂、退出时卸下。
卸下之后(部署会话)它给的就是运行时原来的罐头台词后端,部署行为不变。

异常分类:延后类(租约被占、服务起不来)原样上抛,走守护进程的延后通道;其余计算层错误、`ModelRefError`、
`OpponentCredentialError` 都是确定性的,译成 `DirectiveFailedError`,原因点名异常类型与文字。
这些异常的文字已经抹掉密钥(见 factory.opponents),凭证只报环境变量名。
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager

from compute.endpoint import ComputeEndpoint
from compute.errors import ComputeError
from compute.service import serve as real_serve
from controlplane.deferred import DeferredError
from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from factory.agent import AGENT_CONFIG, AgentRuntime, read_agent_settings
from factory.errors import FactoryConfigError
from factory.eval_config import DEFAULT_SERVE_PORT
from factory.opponents import (
    ExternalApiBackend,
    ModelRefError,
    OpponentCredentialError,
    driver_for,
    resolve_model_ref,
)
from factory.sombench_runner import EXPORT_FORMAT
from factory.vllm_policy import POLICY_TIMEOUT_S, policy_context, vllm_opponent_factory
from mindtrainer.dispatch import DirectiveFailedError
from mindtrainer.training_config import TrainingConfig
from simulator.backends import ModelBackend, ScriptedBackend
from simulator.datamodel import RoleBinding, Scenario

TRAINED_ADAPTER = "trained"          # 训练位在 vLLM 服务里的适配器名;站内对手的适配器名就是它的 model_ref
PER_TURN_DEADLINE_S = 30.0           # 长流 deadline 按每回合 30 秒算:max_turns_per_episode * 30
OPPONENT_TIMEOUT_S = 25.0            # 对手发言的 HTTP 上限,低于服务端每回合 30 秒的限时,免得线程在服务端放弃后还挂着

OpponentFactory = Callable[[RoleBinding, Scenario], ModelBackend]


class OpponentSwitch:
    """AgentRuntime(opponent_backend_factory=...) 收到的可切换工厂。没装真工厂时给运行时原来的罐头后端
    (ScriptedBackend 说全局台词、不循环),与没传工厂时逐字相同。"""

    def __init__(self, fallback_lines: Sequence[str]) -> None:
        self._lines = list(fallback_lines)
        self._lock = threading.Lock()
        self._current: OpponentFactory | None = None

    def __call__(self, binding: RoleBinding, scenario: Scenario) -> ModelBackend:
        with self._lock:
            current = self._current
        if current is None:
            return ScriptedBackend({binding.slot_id: list(self._lines)})
        return current(binding, scenario)

    @contextmanager
    def bound(self, factory: OpponentFactory) -> Iterator[None]:
        with self._lock:
            self._current = factory
        try:
            yield
        finally:
            with self._lock:
                self._current = None


def _failed(run_id: str, exc: BaseException) -> DirectiveFailedError:
    return DirectiveFailedError(f"directive {run_id}: {type(exc).__name__}: {exc}")


def _is_adapter_export(ref: BigRef) -> bool:
    """与 mindtrainer.dispatch._is_adapter_export、resolve_model_ref、SomevalRunner 同一个两条件判法。"""
    hint = ref.fetch_hint or {}
    return hint.get("type") == "roll_ssh" and hint.get("format") == EXPORT_FORMAT


def _adapter_path(ref: BigRef, endpoint: ComputeEndpoint, what: str) -> str:
    """导出在远端的绝对路径。导出必须在本策略所连的端点上:另一台机器上的导出,这个服务挂不上。"""
    prefix = f"ssh://{endpoint.endpoint_id}/"
    if not ref.uri.startswith(prefix):
        raise ModelRefError(f"{what} lives on another endpoint than {endpoint.endpoint_id!r}")
    return "/" + ref.uri[len(prefix):]


def _opponent_bindings(cfg: TrainingConfig) -> list[tuple[RoleBinding, str]]:
    """除训练位以外的绑定及其驱动。cfg 已过严格校验,driver_for 对它们不会抛。"""
    return [(b, driver_for(b)) for b in cfg.bindings if b.slot_id != cfg.trained_slot]


def _plan_adapters(
    store: GitStore, endpoint: ComputeEndpoint, cfg: TrainingConfig,
) -> tuple[str | None, list[tuple[str, str]], list[str]]:
    """(训练位适配器名或 None, 要挂的适配器列表, vllm 对手的 model_ref 列表)。同一 model_ref 只挂一次。"""
    adapters: list[tuple[str, str]] = []
    trained: str | None = None
    if _is_adapter_export(cfg.base_checkpoint_ref):
        adapters.append((TRAINED_ADAPTER, _adapter_path(cfg.base_checkpoint_ref, endpoint, "the trained checkpoint")))
        trained = TRAINED_ADAPTER
    refs: list[str] = []
    for binding, driver in _opponent_bindings(cfg):
        if driver != "vllm" or binding.model_ref in refs:
            continue
        ref = resolve_model_ref(store, binding.model_ref)
        adapters.append((binding.model_ref, _adapter_path(ref, endpoint, f"model_ref {binding.model_ref!r}")))
        refs.append(binding.model_ref)
    return trained, adapters, refs


def _external_backend(
    run_id: str, cfg: TrainingConfig, opponent: Mapping[str, str] | None, env: Mapping[str, str],
) -> ExternalApiBackend | None:
    """本 run 有 external_api 对手位时造那唯一的一个后端,否则 None。密钥现在才从环境变量读。"""
    if not any(driver == "external_api" for _, driver in _opponent_bindings(cfg)):
        return None
    if opponent is None:
        raise DirectiveFailedError(
            f"directive {run_id}: an opponent slot uses external_api but training_defaults.yaml has no opponent section"
        )
    name = opponent["api_key_env"]
    try:
        return ExternalApiBackend(
            opponent["base_url"], opponent["model"],
            api_key=env.get(name, ""), api_key_env=name, timeout_s=OPPONENT_TIMEOUT_S,
        )
    except ValueError as exc:
        # 构造器的 ValueError 文字里会带 base_url;只报类型,配置里的地址不进指令的失败原因
        raise DirectiveFailedError(
            f"directive {run_id}: the opponent API settings in training_defaults.yaml are unusable ({type(exc).__name__})"
        ) from None


def _deployment_overlap(store: GitStore, run_id: str, cfg: TrainingConfig) -> list[str]:
    """本 run 的情境 id 里同时是 agent.yaml 部署情境的那些。文件不在就没有部署情境,也就没有撞车。"""
    if not store.exists(AGENT_CONFIG):
        return []
    try:
        agent_config, _ = read_agent_settings(store)
    except FactoryConfigError as exc:
        raise DirectiveFailedError(
            f"directive {run_id}: cannot tell whether the scenarios overlap deployment ({exc})"
        ) from exc
    deployed = set(agent_config.scenario_ids)
    return [sid for sid in cfg.scenario_ids if sid in deployed]


def _dispatching(vllm_make: OpponentFactory, external: ExternalApiBackend | None) -> OpponentFactory:
    """按驱动分派:vllm 走同一服务上的适配器,external_api 走那唯一的外部后端。scripted 不会来到这里
    (运行时自己用 ScriptedBackend 处理)。"""

    def make(binding: RoleBinding, scenario: Scenario) -> ModelBackend:
        driver = driver_for(binding)
        if driver == "vllm":
            return vllm_make(binding, scenario)
        if driver == "external_api" and external is not None:
            return external
        raise ModelRefError(f"slot {binding.slot_id!r} uses the {driver} driver, which this run did not set up")

    return make


def make_policy_factory(
    *,
    store: GitStore,
    runtime: AgentRuntime,
    switch: OpponentSwitch,
    client: object,
    endpoint: ComputeEndpoint,
    opponent: Mapping[str, str] | None,
    env: Mapping[str, str],
    serve_port: int = DEFAULT_SERVE_PORT,
    serve: Callable[..., object] | None = None,
    policy_timeout_s: float = POLICY_TIMEOUT_S,
    opponent_timeout_s: float = OPPONENT_TIMEOUT_S,
) -> Callable[[str, TrainingConfig], AbstractContextManager[ModelBackend]]:
    """派发器要的 `policy_factory(run_id, cfg)`。serve 可注入,测试里不连 SSH。"""

    @contextmanager
    def scope(run_id: str, cfg: TrainingConfig) -> Iterator[ModelBackend]:
        overlap = _deployment_overlap(store, run_id, cfg)
        if overlap:
            raise DirectiveFailedError(
                f"directive {run_id}: scenario id(s) {overlap} are also deployment scenarios in agent.yaml; "
                "the shared runtime would write their deployment episodes into this training run "
                "(use scenarios that are not listed there)"
            )
        try:
            trained, adapters, refs = _plan_adapters(store, endpoint, cfg)
        except ModelRefError as exc:
            raise _failed(run_id, exc) from exc
        try:
            external = _external_backend(run_id, cfg, opponent, env)
        except OpponentCredentialError as exc:
            raise _failed(run_id, exc) from exc          # 文字里只有环境变量名
        with ExitStack() as stack:
            try:
                policy = stack.enter_context(policy_context(
                    client, endpoint, adapters, serve_port=serve_port, trained_adapter=trained,
                    serve=real_serve if serve is None else serve,   # 调用时才取模块里的名字,测试可替换
                    timeout_s=policy_timeout_s,
                ))
                vllm_make = vllm_opponent_factory(
                    policy.handle, {ref: ref for ref in refs}, timeout_s=opponent_timeout_s,
                )
            except DeferredError:
                raise          # 租约被占、服务起不来:守护进程退避重试,指令仍是 pending
            except (ComputeError, ModelRefError, OpponentCredentialError) as exc:
                raise _failed(run_id, exc) from exc
            stack.enter_context(switch.bound(_dispatching(vllm_make, external)))
            registered: list[str] = []
            previous: float | None = None
            try:
                # 先登记再放行第一个会话。注意不要在持有 runtime._lock 时碰 interact_deadline_s(同一把非重入锁)
                previous = runtime.interact_deadline_s
                for sid in cfg.scenario_ids:
                    runtime.register_session(sid, run_id)
                    registered.append(sid)
                runtime.interact_deadline_s = cfg.max_turns_per_episode * PER_TURN_DEADLINE_S
                yield policy
            finally:
                for sid in registered:
                    runtime.unregister_session(sid)
                if previous is not None:
                    runtime.interact_deadline_s = previous

    return scope
