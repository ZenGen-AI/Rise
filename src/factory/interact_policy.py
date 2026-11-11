"""`--interact` 的策略工厂与对手后端切换(设计 90 §6.1 的"注入什么"与"对手位")。

派发器每派一条 interact 指令调用一次 `policy_factory(run_id, cfg)`,拿到一个上下文管理器。进入时按顺序:
1. 部署情境撞车检查(Ruling 24):本 run 的情境 id 若也在 agent.yaml 的部署情境里,判 failed,什么都不登记;
2. scripted 对手位的台词检查:某个情境的剧本里没有该位的台词、运行时的全局台词也是空的,判 failed(否则对手只会
   说"(无更多发言)",产出的是垃圾 rollout)。生成路的情境在派发器写回之后才有 id,所以检查放在这里而不是装配期;
3. 解析训练位适配器与站内对手的适配器(都在占租约之前,确定性的错在这里就暴露);
4. 本 run 有 external_api 对手位时,现在才读 `training_defaults.yaml` 的 opponent 段并造那个后端(每次进入都重读,
   改了不必重启守护进程;读不到或写坏了判 failed,原因只报段名与异常类型);
5. 起 vLLM 服务(`policy_context`),在同一服务上造 vLLM 对手工厂;
6. 把每个情境 id 登记到运行时(落盘钩子据此把 episode 写进本 run),交出 `policy_for_rollout`:
   `policy_for_rollout(episode_id, scenario_id)` 是上下文管理器,每条 rollout 进出一次。进入时建这条 rollout 的世界
   (`TrainingWorlds.open_rollout`,从种子里该情境的世界复制起点),在 `VllmPolicy` 外面新造一个带记忆的 SimpleActio 实例
   (`memory_role_backend`,端口是训练位人格在这个世界里的端口);服务端随后按 episode id 加入同一个世界开对手位端口。
   退出时(正常、异常都一样)`close_rollout`:关训练位端口,等服务端端口关完再删世界。
退出时(正常、训练抛异常都一样)先注销情境,再由 `policy_context` 停服务放租约。

对手后端经 `OpponentSwitch` 交给运行时:运行时在装配期就造好(它还要服务 MindAgent 部署),而 vLLM 对手要用
会话里才有的 `ServiceHandle`,所以运行时拿到的是这个可切换的工厂,策略上下文进入时装上真工厂、退出时卸下。
卸下之后(部署会话)它给的就是运行时原来的罐头台词后端,部署行为不变。

异常分类:延后类(租约被占、服务起不来)原样上抛,走守护进程的延后通道;其余计算层错误、`ModelRefError`、
`OpponentCredentialError` 都是确定性的,译成 `DirectiveFailedError`,原因点名异常类型与文字。
这些异常的文字已经抹掉密钥(见 factory.opponents),凭证只报环境变量名。

`ServeStopError`(停服务后作业仍在运行,租约保留)虽属延后类,却单独处理:它在进入、训练中、退出时都可能冒出来,
统一在整个服务上下文之外接住,译成 T2 的 `DirectiveFailedError`,原因点名作业 id 并要求人工停掉占着显存的作业。
作业停不下来,重试只会撞上同一个租约;若退出时才冒出来,训练其实已经完成,延后重试还会在已完成的训练上反复起服务。
它冒出来时登记注销、对手开关解绑都已在各自的 finally 里做完,不会被跳过。

训练体里抛出的真推理失败也分两路(Ruling 27):`RolloutFailedError`(确定性拒绝除外)与 `OpponentApiError`
(训练位策略的 `OpponentRejectedError` 除外)译成 `RolloutDeferredError`(`DeferredError` 的子类),走延后通道,
指令保持 pending,守护进程不会被它带走。确定性拒绝(`invalid_session`、`role_backend_rejected`、`role_memory_failure`,
见 `mindtrainer.orchestrator` 的 `_DETERMINISTIC_ROLLOUT_REASONS`)与训练位策略的 `OpponentRejectedError`
原样放行:编排器已把 training_run 写成 failed,派发器据此把指令判为 T2。
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager
from typing import TYPE_CHECKING

from simple_actio.memory_port import MemoryFailureError
from compute.client import ComputeClient
from compute.endpoint import ComputeEndpoint
from compute.errors import ComputeError, ServeStopError
from compute.service import serve as real_serve
from controlplane.deferred import DeferredError
from controlplane.envelope import BigRef
from controlplane.gitstore import UNREADABLE_PRODUCT_ERRORS, GitStore
from controlplane.paths import scenario_file
from factory.agent import AGENT_CONFIG, AgentRuntime, read_agent_settings
from factory.errors import FactoryConfigError
from factory.eval_config import DEFAULT_SERVE_PORT
from factory.interact_checks import has_userinfo
from factory.memory_translate import (
    FactoryMemoryFailureError,
    FactoryMemoryUnavailableError,
    MemoryTranslatingBackend,
    memory_role_backend,
)
from factory.opponents import (
    ExternalApiBackend,
    ModelRefError,
    OpponentApiError,
    OpponentCredentialError,
    OpponentRejectedError,
    driver_for,
    resolve_model_ref,
)
from factory.sombench_runner import EXPORT_FORMAT
from factory.vllm_policy import POLICY_TIMEOUT_S, policy_context, vllm_opponent_factory
from mindtrainer.dispatch import DirectiveFailedError
from mindtrainer.orchestrator import _DETERMINISTIC_ROLLOUT_REASONS
from mindtrainer.training_config import TrainingConfig
from mindtrainer.trajectory_source import PolicyForRollout, RolloutFailedError
from simulator.backends import ModelBackend, ScriptedBackend
from simulator.datamodel import RoleBinding, Scenario
from simulator.store_adapter import read_scenario
from trainscheduler.config import load_opponent_section

if TYPE_CHECKING:       # 只给类型注解用:运行时不导入 factory.memory_runtime,没装 Starling 时本模块照常可用
    from factory.memory_runtime import TrainingWorlds

TRAINED_ADAPTER = "trained"          # 训练位在 vLLM 服务里的适配器名;站内对手的适配器名就是它的 model_ref
OPPONENT_TIMEOUT_S = 60.0            # 对手发言的 HTTP 上限:60 秒加一轮记忆最坏 20 秒,低于服务端后端调用上限 90 秒(factory.wire)

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


def _stuck_service(run_id: str, exc: ServeStopError) -> DirectiveFailedError:
    """作业停不下来:T2。文字里只有作业 id(见 compute.service),没有密钥与远端地址。

    训练体先抛了错、退出时又停不下来,前一个异常会被顶掉:只把它的类名补进原因,不带它的文字
    (文字里可能有远端地址),让人知道这条 run 本身也没跑完。"""
    displaced = exc.__context__
    note = f" (the training had already failed: {type(displaced).__name__})" if displaced is not None else ""
    return DirectiveFailedError(
        f"directive {run_id}: the vLLM service could not be stopped ({exc}); stop that job on the compute host "
        f"manually, because it is still holding GPU memory and the lease{note}"
    )


class RolloutDeferredError(DeferredError):
    """真推理的 rollout 这一次没成(对手 API 或 vLLM 服务连不上、角色位后端出错、超时用尽、落盘失败):
    指令保持 pending,守护进程按退避重试,不被它带走。

    重试有界:training_run 此时仍是 pending,每次重派 `run_training` 先把 resume_attempts 加一并落盘,
    到 `max_resume_attempts` 由 `decide_resume` 升级为 NeedsHumanError,派发器判 T3。interact 的配置校验要求
    `max_resume_attempts` 小于守护进程的连续延后上限(`controlplane.daemon.MAX_CONSECUTIVE_DEFERRALS`,20),所以
    一直失败的 run 先到续训上限,最终判 T3,不会先撞上 `DeferredExhaustedError` 让守护进程退出。
    reason 只写原异常的类名与 reason 码,不带它的文字(文字里可能有远端地址)。"""


def _rollout_deferral(exc: RolloutFailedError | OpponentApiError) -> RolloutDeferredError:
    if isinstance(exc, RolloutFailedError):
        return RolloutDeferredError(f"a real-inference rollout failed ({type(exc).__name__}: {exc.reason})")
    return RolloutDeferredError(f"an opponent backend failed during a rollout ({type(exc).__name__})")


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


def _opponent_section(store: GitStore, run_id: str) -> Mapping[str, str]:
    """现在从树里读 training_defaults.yaml 的 opponent 段(每次进入策略上下文都读,装配之后改了也生效)。

    装配期的检查(interact_checks)是启动门槛,保留不变;这里是派发期的第二道:文件被删、写坏、
    opponent 段被删、地址被改成带 user:password@ 的,都在占租约之前判 T2。原因只报段名与异常类型,不带异常文字:
    解析错误的文字、地址都可能带着远端地址。读失败一律译成 T2,磁盘瞬态错误也在内:重试这条指令前先要有人看一眼配置。
    只解析 opponent 段:其余段落写坏了不拖垮这里,它们由装配期与调度时的整份校验把关。"""
    try:
        section = load_opponent_section(store)
    except Exception as exc:
        raise DirectiveFailedError(
            f"directive {run_id}: the opponent section of training_defaults.yaml cannot be read ({type(exc).__name__})"
        ) from None
    if section is None:
        raise DirectiveFailedError(
            f"directive {run_id}: an opponent slot uses external_api but training_defaults.yaml has no opponent section"
        )
    if has_userinfo(section["base_url"]):
        raise DirectiveFailedError(
            f"directive {run_id}: the opponent section of training_defaults.yaml must not carry credentials in "
            "base_url (user:password@); put the key in the environment variable named by api_key_env"
        )
    return section


def _external_backend(
    run_id: str, cfg: TrainingConfig, store: GitStore, env: Mapping[str, str], timeout_s: float,
) -> Callable[[], ExternalApiBackend] | None:
    """本 run 有 external_api 对手位时返回造外部后端的零参函数,否则 None(这时不读 opponent 段:
    一个不相干的 run 不该被坏掉的 opponent 段拖垮)。密钥现在才从环境变量读。

    进入时照旧先造一个,让凭证与配置错误照旧在进入时抛出;这一个只用来校验,不交给任何会话。
    之后每个会话的每个外部对手位调一次返回的函数,各拿一个新造的后端,不跨会话共享。"""
    if not any(driver == "external_api" for _, driver in _opponent_bindings(cfg)):
        return None
    opponent = _opponent_section(store, run_id)
    name = opponent["api_key_env"]
    base_url, model, api_key = opponent["base_url"], opponent["model"], env.get(name, "")

    def make() -> ExternalApiBackend:
        return ExternalApiBackend(base_url, model, api_key=api_key, api_key_env=name, timeout_s=timeout_s)

    try:
        make()
    except ValueError as exc:
        # 构造器的 ValueError 文字里会带 base_url;只报类型,配置里的地址不进指令的失败原因
        raise DirectiveFailedError(
            f"directive {run_id}: the opponent API settings in training_defaults.yaml are unusable ({type(exc).__name__})"
        ) from None
    return make


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


def _scripted_gaps(store: GitStore, runtime: AgentRuntime, cfg: TrainingConfig) -> list[str]:
    """没有台词可说的 scripted 对手位,形如 `情境id:角色位`。

    scripted 驱动的对手位台词取情境剧本里该位的一份,没有或为空取运行时的全局台词(AgentRuntime._backends);
    两样都没有时 ScriptedBackend 只会说"(无更多发言)"。全局台词来自装配期读的 agent.yaml,只开 --interact
    而没有 agent.yaml 时是空的。训练位以外只有显式写了 scripted 的绑定才走这条路(driver_for 的默认是 vllm 或
    external_api)。树里没有或读不出的情境不在这里报:会话开不起来时服务端自会判 invalid_session。"""
    slots = [b.slot_id for b, driver in _opponent_bindings(cfg) if driver == "scripted"]
    if not slots or runtime.opponent_lines:
        return []
    gaps: list[str] = []
    for sid in cfg.scenario_ids:
        if not store.exists(scenario_file(sid)):
            continue
        try:
            script = read_scenario(store, sid).opponent_script
        except UNREADABLE_PRODUCT_ERRORS:
            continue
        gaps.extend(f"{sid}:{slot}" for slot in slots if not script.get(slot))
    return gaps


def _dispatching(vllm_make: OpponentFactory, make_external: Callable[[], ExternalApiBackend] | None) -> OpponentFactory:
    """按驱动分派:vllm 走同一服务上的适配器,external_api 每个会话新造一个外部后端。scripted 不会来到这里
    (运行时自己用 ScriptedBackend 处理)。"""

    def make(binding: RoleBinding, scenario: Scenario) -> ModelBackend:
        driver = driver_for(binding)
        if driver == "vllm":
            return vllm_make(binding, scenario)
        if driver == "external_api" and make_external is not None:
            return make_external()
        raise ModelRefError(f"slot {binding.slot_id!r} uses the {driver} driver, which this run did not set up")

    return make


def make_policy_factory(
    *,
    store: GitStore,
    runtime: AgentRuntime,
    switch: OpponentSwitch,
    client: ComputeClient,
    endpoint: ComputeEndpoint,
    env: Mapping[str, str],
    serve_port: int = DEFAULT_SERVE_PORT,
    serve: Callable[..., object] | None = None,
    policy_timeout_s: float = POLICY_TIMEOUT_S,
    opponent_timeout_s: float = OPPONENT_TIMEOUT_S,
    training_worlds: TrainingWorlds,
) -> Callable[[str, TrainingConfig], AbstractContextManager[PolicyForRollout]]:
    """派发器要的 `policy_factory(run_id, cfg)`。serve 可注入,测试里不连 SSH。
    上下文交出的是 `policy_for_rollout(episode_id, scenario_id)`,不是后端:轨迹源每条 rollout 进出一次,拿到该条
    rollout 自己的、包着同一个 `VllmPolicy` 的 SimpleActio 实例。
    外部对手的地址与模型不在这里传:每次进入上下文时从 `store` 里的 training_defaults.yaml 读(见 _opponent_section)。
    opponent_timeout_s 同时是站内 vLLM 对手与外部 API 对手的单次 HTTP 上限。
    training_worlds 必须是 `runtime` 手里的同一个对象(组合根只造一个,替身或 Starling):客户端在这里建 rollout 世界,
    服务端在运行时里按 episode id 加入它。进入上下文先 bind(run_id, 种子, 起点摘要),退出时在注销情境之后 unbind;
    每条 rollout 一个世界,退出时等服务端端口关完再删(见 policy_for_rollout)。"""

    @contextmanager
    def scope(run_id: str, cfg: TrainingConfig) -> Iterator[PolicyForRollout]:
        overlap = _deployment_overlap(store, run_id, cfg)
        if overlap:
            raise DirectiveFailedError(
                f"directive {run_id}: scenario id(s) {overlap} are also deployment scenarios in agent.yaml; "
                "the shared runtime would write their deployment episodes into this training run "
                "(use scenarios that are not listed there)"
            )
        gaps = _scripted_gaps(store, runtime, cfg)
        if gaps:
            raise DirectiveFailedError(
                f"directive {run_id}: scripted opponent slot(s) {gaps} (scenario:slot) have no lines to say, so the "
                "opponent would only answer \"(无更多发言)\" and the rollouts would be garbage; give the opponent "
                "lines in agent.yaml (opponent_lines) or give each of those scenarios an opponent_script for the slot"
            )
        try:
            trained, adapters, refs = _plan_adapters(store, endpoint, cfg)
        except ModelRefError as exc:
            raise _failed(run_id, exc) from exc
        try:
            make_external = _external_backend(run_id, cfg, store, env, opponent_timeout_s)
        except OpponentCredentialError as exc:
            raise _failed(run_id, exc) from exc          # 文字里只有环境变量名
        try:
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
                stack.enter_context(switch.bound(_dispatching(vllm_make, make_external)))
                interlocutors = {b.slot_id: b.persona_id for b in cfg.bindings}

                @contextmanager
                def policy_for_rollout(episode_id: str, scenario_id: str) -> Iterator[MemoryTranslatingBackend]:
                    # 训练位与对手位同一写法:底座(VllmPolicy 有 respond_guided,不套 IgnoreGuidance)外面包带记忆的
                    # SimpleActio 实例。记忆是这条 rollout 的世界里训练位人格的端口:open_rollout 先删后建,重试也从起点出发;
                    # 服务端开会话时按 episode id 加入同一个世界开对手位端口。退出时(正常、异常都一样)close_rollout:
                    # 关训练位端口,等服务端端口关完再删世界,等不到就留给保留表与启动清扫,这条 rollout 照常结束。
                    # 训练位端口关不上时 close_rollout 重抛 FactoryMemoryFailureError(转换后端 close 译的)不接:
                    # 它是 RoleMemoryFailureError 的子类,穿出轨迹源后由编排器写 failed、派发器判 T2。
                    holder = interlocutors[cfg.trained_slot]
                    try:
                        memory = training_worlds.open_rollout(run_id, scenario_id, episode_id, holder)
                    except MemoryFailureError as exc:
                        # 打不开世界(种子坏了、磁盘满、同名世界还被服务端端口占着)发生在转换后端造出来之前,要照
                        # memory_translate 的口径自己译:编排器只认 RoleMemoryFailureError 判 T2,裸的
                        # MemoryFailureError 会让 run 保持 pending 被反复重派。瞬态的(同名世界还被占着)译成
                        # FactoryMemoryUnavailableError,轨迹源记一次 role_memory_unavailable 再重跑。文字只放类名。
                        message = f"trained slot rollout world could not be opened: {type(exc).__name__}"
                        if exc.transient:
                            raise FactoryMemoryUnavailableError(message) from exc
                        raise FactoryMemoryFailureError(message) from exc
                    try:
                        backend = memory_role_backend(
                            policy, memory=memory, episode_id=episode_id, interlocutors=interlocutors, holder=holder,
                        )
                    except BaseException:
                        # 端口已开、实例没造成:关上并删世界(会话还没开,没有服务端端口要等);收尾的失败不盖掉原异常
                        try:
                            training_worlds.close_rollout(episode_id, [memory])
                        except Exception:
                            pass        # close_rollout 已记 warning,世界留给清扫
                        raise
                    try:
                        yield backend
                    finally:
                        training_worlds.close_rollout(episode_id, [backend])

                registered: list[str] = []
                # 先绑定再交出 policy_for_rollout:建世界要从钉住的种子分叉。digests 是派发器解出的起点摘要,
                # bind 按它钉住解析时那一版种子。这时还没有训练记录:永久失败(一份种子都对不上)直接判 T2,
                # 瞬态(服务连不上)延后;不译的话裸异常让指令留 pending 每轮重派。文字只有码、种子 id 与情境 id
                try:
                    training_worlds.bind(run_id, cfg.memory["seed"], cfg.memory["digests"])
                except MemoryFailureError as exc:
                    if exc.transient:
                        raise DeferredError(f"directive {run_id}: memory service: {exc}") from None
                    raise _failed(run_id, exc) from None
                try:
                    # 先登记再放行第一个会话
                    for sid in cfg.scenario_ids:
                        runtime.register_session(sid, run_id)
                        registered.append(sid)
                    try:
                        yield policy_for_rollout
                    except RolloutFailedError as exc:
                        if exc.reason in _DETERMINISTIC_ROLLOUT_REASONS:
                            # invalid_session(请求本身有问题)、role_backend_rejected(对手 API 明确拒绝)、
                            # role_memory_failure(对手位记忆确定性失败):编排器已写 failed,派发器据此判 T2
                            raise
                        # from None:异常文字(含服务端转述的细节)不跟着回溯走
                        raise _rollout_deferral(exc) from None
                    except OpponentRejectedError:
                        # 训练位策略的 4xx(408、425、429 除外):服务已过就绪探测,这时的 4xx 是本 run 的 vLLM 明确拒绝
                        # (适配器名、密钥、请求体不对),重试无益。编排器已写 failed(RoleBackendRejectedError 在它的
                        # 确定性集合里),派发器据此判 T2
                        raise
                    except OpponentApiError as exc:
                        # 训练位策略的超时、暂时不可用、回复形状不对:重试有界,最终升 T3
                        raise _rollout_deferral(exc) from None
                finally:
                    for sid in registered:
                        runtime.unregister_session(sid)
                    training_worlds.unbind(run_id)
        except ServeStopError as exc:
            # 进入、训练中、退出时冒出来都在这里:各 finally 与服务上下文已经展开完,没有清理被跳过
            raise _stuck_service(run_id, exc) from exc

    return scope
