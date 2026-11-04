"""MindAgent 轮次处理器的装配材料:agent.yaml、进程内 loopback 实时服务端、V1 罐头模型后端。

agent.yaml 是组合根自己的配置信封(control-plane/config/agent.yaml),载荷键:
- 必填 scenario_ids(非空字符串列表)、bindings(RoleBinding.to_dict() 的列表)、opponent_lines(非空字符串列表);
- 可选 AgentConfig 的其余字段(max_turns、episodes_per_scenario、max_retries_per_episode、min_support、
  min_ratio、min_fired_on_motivating、max_fired_elsewhere、max_parse_retries),缺省用 AgentConfig 的默认值。
未知键、类型错都抛 FactoryConfigError;AgentConfig 自己的校验错误也转成 FactoryConfigError。
绑定的 persona_id 要合 PERSONA_ID_PATTERN:它拼进部署库与人格锁的文件名,不合法的在装配期就判 FactoryConfigError
(退出码 2),不留到轮次处理器取锁时才抛。
"""
from __future__ import annotations

import logging
import math
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import fields
from typing import TYPE_CHECKING

import grpc

from actio.memory import TupleMemory
from actio.memory_port import PERSONA_ID_PATTERN
from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT
from factory.configio import describe, key_names, read_tree_config
from factory.errors import FactoryConfigError
from factory.memory_translate import MemoryTranslatingBackend, memory_role_backend
from factory.opponents import driver_for
from mindagent.config import AgentConfig
from simulator.backends import ModelBackend, ScriptedBackend
from simulator.datamodel import Episode, RoleBinding, Scenario
from simulator.realtime.client import EnvironmentServiceClient
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.resources import SessionInfo, SessionResources
from simulator.realtime.server import create_server
from simulator.store_adapter import read_persona, read_scenario, write_episode

if TYPE_CHECKING:       # 只给类型注解用:运行时不导入 factory.memory_runtime 里要用 Starling 的那部分
    from factory.memory_runtime import TrainingCopies

logger = logging.getLogger(__name__)

AGENT_CONFIG = f"{CONTROL_PLANE_ROOT}/config/agent.yaml"
AGENT_BACKENDS = ("canned",)
_REQUIRED = ("scenario_ids", "bindings", "opponent_lines")
_OPTIONAL = tuple(f.name for f in fields(AgentConfig) if f.name not in ("scenario_ids", "bindings"))


class CannedGuidedBackend:
    """V1 唯一的智能体模型后端:不加载 checkpoint,把 Actio 渲染的指导语拼成话语。
    技能是否生效在落盘的 episode 里直接看得见;它不代表任何模型能力。"""

    def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
        return ("|".join(guidance) or "plain", f"saw {len(history)}")


def canned_backend_factory(model_id: str, checkpoint_ref) -> CannedGuidedBackend:
    return CannedGuidedBackend()


def personas_from_tree(store: GitStore, persona_ids) -> dict:
    """personas_loader:从树里读人格(simulator.store_adapter)。缺了由轮次处理器的输入检查判 failed。"""
    return {pid: read_persona(store, pid) for pid in persona_ids}


def _str_list(payload: dict, key: str) -> list[str]:
    value = payload[key]
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise FactoryConfigError(f"{AGENT_CONFIG}: {key} must be a non-empty list of non-empty strings")
    return list(value)


def read_agent_settings(store: GitStore) -> tuple[AgentConfig, tuple[str, ...]]:
    """返回 (AgentConfig, 对手罐头台词)。"""
    try:
        payload = read_tree_config(store, AGENT_CONFIG)
    except FileNotFoundError:
        raise FactoryConfigError(
            f"{AGENT_CONFIG} is missing; the agent switch needs it (run install-defaults)"
        ) from None
    keys = set(payload)
    missing = key_names(set(_REQUIRED) - keys)
    unknown = key_names(keys - set(_REQUIRED) - set(_OPTIONAL))
    if missing or unknown:
        raise FactoryConfigError(f"{AGENT_CONFIG}: missing {missing}, unknown {unknown}")
    scenario_ids = _str_list(payload, "scenario_ids")
    lines = _str_list(payload, "opponent_lines")
    raw_bindings = payload["bindings"]
    if not isinstance(raw_bindings, list) or not all(isinstance(b, dict) for b in raw_bindings):
        raise FactoryConfigError(f"{AGENT_CONFIG}: bindings must be a list of mappings")
    try:
        bindings = tuple(RoleBinding.from_dict(b) for b in raw_bindings)
        config = AgentConfig(
            scenario_ids=tuple(scenario_ids), bindings=bindings,
            **{k: payload[k] for k in _OPTIONAL if k in payload},
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise FactoryConfigError(f"{AGENT_CONFIG}: {describe(exc)}") from None
    bad = sorted({repr(b.persona_id) for b in bindings
                  if not isinstance(b.persona_id, str) or not PERSONA_ID_PATTERN.fullmatch(b.persona_id)})
    if bad:
        raise FactoryConfigError(
            f"{AGENT_CONFIG}: bindings persona_id {bad} must be 1 to 128 characters of [A-Za-z0-9._-] "
            "starting with a letter or digit (it names the deployment memory library and its lock)"
        )
    # 记忆按人格持有:两个角色位绑同一人格,就共用一个持有者、一份记忆和一组事件键。
    # 与训练配置(mindtrainer.training_config)同一句原因;三处调用方(装配两处、每次派发)都经过这里
    pids = [b.persona_id for b in bindings]
    if len(set(pids)) != len(pids):
        repeated = next(pid for pid in pids if pids.count(pid) > 1)
        raise FactoryConfigError(
            f"{AGENT_CONFIG}: bindings: persona {repeated!r} is bound to more than one slot"
        )
    return config, tuple(lines)


INTERACT_DEADLINE_S = 15.0
MAX_WRITE_FAILURES_KEPT = 100         # episode_write_failures 只留最近这么多条:守护进程长跑,不能无限增长


class DeadlineStub:
    """给每条 Interact 流加 gRPC deadline(计划 7 已知限制 4 与它测试夹具里的 _DeadlineStub 同一写法):
    服务端失联时客户端的 next() 以 DEADLINE_EXCEEDED 结束,而不是让守护进程或测试套件挂住。
    EnvironmentServiceClient 只调 stub.Interact,所以只包这一个方法。"""

    def __init__(self, stub, timeout_s: float = INTERACT_DEADLINE_S) -> None:
        self._stub = stub
        self.timeout_s = timeout_s

    def Interact(self, request_iterator):
        return self._stub.Interact(request_iterator, timeout=self.timeout_s)


class AgentRuntime:
    """进程内的 loopback 实时服务端,第一次 client_factory() 时才启动;close() 关通道、停服务端并等它停完。
    对手位的底座后端按各自绑定的驱动分派(见 _backends),再一律包成带记忆的 Actio 实例(每个位一份各自的记忆,
    外套记忆异常的转换后端):替身是空 TupleMemory;传了 training_copies 且会话登记给了训练 run 时,是从种子复制的
    Starling 私有副本(终态即删,见 _backends);情境从树里读(scenario_resolver)。
    每条 Interact 流带 deadline(DeadlineStub);流 deadline 可由调用方逐个给(client_factory(deadline_s=...)),
    interact_deadline_s 只是没给时的缺省。message_timeout_s 是客户端相邻两条服务端消息的最长间隔,运行时级,
    给每个客户端(None 即不设,与过去相同)。stub_factory 只为测试注入。

    opponent_backend_factory(binding, scenario) 给 vllm 与 external_api 驱动的对手位造后端,只服务登记给训练 run 的会话;
    没传(None)或会话没登记(MindAgent 部署会话)时,这两种也和过去一样用 ScriptedBackend 说 opponent_lines:
    部署会话的绑定多是 frozen_reference,没有真模型可加载,行为必须原样不变。

    会话登记(register_session / unregister_session):--interact 与 MindAgent 部署共用这一个服务端。登记表按情境 id
    记该情境属于哪个 run,只用来判定新开的会话归谁:工厂造资源时情境 id 已登记,就按 session_id 记一条路由(_session_runs),
    并把"删这条路由"放进资源的 closers。落盘钩子只查 session_id 的路由:登记过的会话把 episode 写进开会话时的那个 run,
    注销情境不影响已经在跑的会话;没登记的(部署会话)没有路由,钩子不碰,仍由 MindAgent 驱动器自己落盘,
    两边的 episode 不混写、不重复写。两把锁:_lock 守护登记表、通道与服务端的启停(close() 持它停服务端并等在途处理器退出);
    _routes_lock 只守护 _session_runs,_failures_lock 只守护 episode_write_failures 的追加。处理器收尾时跑的"删路由"、
    钩子里的路由查询与失败追加只取各自的小锁、绝不取 _lock,
    否则 close() 持 _lock 等处理器、处理器等 _lock,关停就挂住;close() 不取 _routes_lock。取锁顺序只有一个方向:
    先 _lock 后 _routes_lock(工厂读登记表与记路由在同一次持 _lock 里完成)。落盘失败先把描述记进 episode_write_failures(只含类名与 id,有上限的 deque,只留最近 100 条),
    再把原异常抛回 servicer:servicer 据此给客户端报 persistence_failed、不给 episode_id,轨迹源把它当瞬时失败重跑。"""

    def __init__(self, store: GitStore, opponent_lines: tuple[str, ...], *, turn_timeout_s: float = 5.0,
                 interact_deadline_s: float = INTERACT_DEADLINE_S,
                 message_timeout_s: float | None = None,
                 backend_timeout_s: float | None = None,
                 stub_factory=pb2_grpc.EnvironmentServiceStub,
                 opponent_backend_factory: Callable[[RoleBinding, Scenario], ModelBackend] | None = None,
                 training_copies: TrainingCopies | None = None) -> None:
        # 与 EnvironmentServiceClient 同一判法,在这里先判:否则要到第一次 client_factory() 才抛,那时 channel 已造好、
        # 还没进 _channels,每次失败漏一个
        if message_timeout_s is not None and not (
            isinstance(message_timeout_s, (int, float)) and not isinstance(message_timeout_s, bool)
            and math.isfinite(message_timeout_s) and message_timeout_s > 0
        ):
            raise ValueError(
                f"message_timeout_s must be a finite positive number or None, got {message_timeout_s!r}"
            )
        self._store = store
        self._lines = list(opponent_lines)
        self._timeout = turn_timeout_s
        self._deadline = interact_deadline_s
        self._message_timeout = message_timeout_s
        self._backend_timeout = backend_timeout_s     # None:服务端的默认 60 秒
        self._stub_factory = stub_factory
        self._opponent_backend_factory = opponent_backend_factory
        self._training_copies = training_copies
        self._server = None
        # (客户端, 通道) 成对记下:会话结束的客户端,通道在下一次发客户端时关掉(见 client_factory)
        self._channels: list = []
        self._lock = threading.Lock()
        self._sessions: dict[str, str] = {}              # 情境 id -> run id,只用来判定新会话归属
        # session_id -> run id,落盘钩子只查它;随会话资源关闭而删。有自己的锁(见类 docstring):删与查都在 gRPC 处理器线程里,
        # 可能与持 _lock 停服务端的 close() 同时发生,所以不能取 _lock。取锁顺序:先 _lock 后 _routes_lock。
        self._routes_lock = threading.Lock()
        self._session_runs: dict[str, str] = {}
        # 每条一句描述,只含类名与 id,不含异常文字;只留最近 MAX_WRITE_FAILURES_KEPT 条(最早的先丢)。
        # 追加在钩子(gRPC 处理器线程)里,有自己的小锁:不能取 _lock(见 _on_session_closed)
        self._failures_lock = threading.Lock()
        self.episode_write_failures: deque[str] = deque(maxlen=MAX_WRITE_FAILURES_KEPT)

    @property
    def opponent_lines(self) -> tuple[str, ...]:
        """运行时的全局对手台词,只读。没有剧本的 scripted 对手位说的就是它;为空时那个位只会说"(无更多发言)"。
        策略上下文据此在派发时判断某个情境的 scripted 对手位有没有台词可说。"""
        return tuple(self._lines)

    @property
    def interact_deadline_s(self) -> float:
        """不取锁:读一个 float 是原子的,而 `_lock` 不可重入,持锁的代码一旦碰到这个 getter 就是自死锁
        (client_factory 持锁读 `_deadline` 是直接读字段,正是这个原因)。setter 仍持锁,与别的状态变更串行。"""
        return self._deadline

    @interact_deadline_s.setter
    def interact_deadline_s(self, value: float) -> None:
        """改之后新造的、没给 deadline_s 的客户端(client_factory 构造 DeadlineStub 时读)用新值,已造好的不回头改。
        调用方要逐个给流 deadline 时用 client_factory(deadline_s=...),不改这个缺省;src 里已经没有调用方改它,
        只为测试与手工调试保留。"""
        with self._lock:
            self._deadline = value

    def register_session(self, scenario_id: str, run_id: str) -> None:
        """登记:此后新开的该情境 id 的会话属于 run_id,会话结束时钩子把 episode 写进这个 run。同 id 再登记则覆盖
        (只影响之后新开的会话,已经在跑的会话仍按它开会话时的 run 落盘)。"""
        with self._lock:
            self._sessions[scenario_id] = run_id

    def unregister_session(self, scenario_id: str) -> None:
        """注销;没登记过也不报错。此后新开的该情境 id 的会话不再被钩子写;已经在跑的会话不受影响。"""
        with self._lock:
            self._sessions.pop(scenario_id, None)

    def _on_session_closed(self, session_id: str, episode: Episode) -> None:
        """服务端的落盘钩子,跑在 gRPC 线程池的线程里。查路由只取 _routes_lock,写失败时往清单追加只取 _failures_lock,
        两处都不取 _lock,写文件在锁外:close() 持 _lock 停服务端并等它停完,钩子若取 _lock 或持锁写文件,两边会互相等。
        写失败记录后原样再抛。钩子在 servicer 收尾关资源之前调用,所以路由在落盘时一定还在。"""
        with self._routes_lock:
            run_id = self._session_runs.get(session_id)
        if run_id is None:
            return
        try:
            write_episode(self._store, run_id, episode)
        except Exception as exc:
            # 只记类名、episode id 与 run id:异常文字可能带远端地址或凭证。
            detail = f"{type(exc).__name__} writing episode {episode.id} for run {run_id}"
            logger.error("episode write failed: %s", detail)
            with self._failures_lock:
                self.episode_write_failures.append(detail)
            # 必须再抛:servicer 看到钩子抛错才会把结束原因改报 persistence_failed 并清空 episode_id,
            # 轨迹源据此整条重跑。吞掉的话客户端收到正常的结束原因与 episode id,把树里没有的 episode 当成已落盘。
            # 原样抛原异常,不包新类型;servicer 自己记日志并吞下,不会传出线程池。
            raise

    @staticmethod
    def _driver_of(binding: RoleBinding, *, judged: bool) -> str | None:
        """judged 为真是训练路:绑定已过 validate_training_config,照实用 driver_for 判。
        不判绑定时一律不调 driver_for(Ruling 15):agent.yaml 不校验 model_backend 的取值,driver_for 对 self_play
        与未知后端名会抛 ValueError,而过去的 _backends 从不看绑定。只有显式写了 scripted 的绑定改变行为。"""
        if judged:
            return driver_for(binding)
        return "scripted" if binding.driver == "scripted" else None

    def _backends(self, info: SessionInfo) -> SessionResources:
        """每个非客户端角色位一个全新的后端(服务端要求每次调用返回新实例,游标不能跨会话共享)。
        严格判驱动只用于"传了工厂、且该情境 id 登记给了某个训练 run"的会话(Ruling 28):--interact 下运行时的工厂
        一直是 OpponentSwitch,部署会话(没登记)若也被判,agent.yaml 里 self_play 或未知 model_backend 的非客户端位
        会让每一轮部署都失败。训练路:
        scripted 驱动:ScriptedBackend(loop=True),台词取情境剧本里该角色位的一份,没有或为空取全局台词;
        其余驱动:交给工厂。
        其余会话(没传工厂,或没登记):显式 scripted 同上,别的一律是原来的 ScriptedBackend(不循环,全局台词),
        不碰工厂、不调 driver_for。

        上面得到的只是底座。每个对手位的底座都再包成带记忆的 Actio 实例(memory_role_backend):每个位一份各自的
        记忆,情境对话方取自绑定表(槽位 -> 角色卡 id)。对话台词不变,每轮多一份 v2 轨迹。记忆有两种:
        - 默认(没传 training_copies,或会话没登记给训练 run,即部署会话):一份空 TupleMemory,与部署会话同一写法。
          closers 依次是每个已造后端的 close、最后(登记过时)是删路由;
        - 会话登记给了训练 run 且传了 training_copies:该位绑定人格的 Starling 私有副本(从 run 的种子复制,
          按会话一个目录)。closers 是 (release_all, 删路由):release_all 关掉所有端口,全部关上才删会话的副本目录
          (有端口关不上,说明 Starling 还有在途调用,目录留给下次启动的清扫);它抛的异常由 SessionResources.close
          记 warning 吞掉。run 还没 bind 种子(seed_of 抛 KeyError)按造后端失败处理。
        造到一半抛异常时,先关已造好的后端并(训练路)删副本目录,关失败只记类名;再删路由、原样重抛。

        落盘路由:情境 id 已登记时,在读登记表的同一次持 _lock 里(再取 _routes_lock)记 session_id -> run id,
        "删这条"放进资源的 closers,随会话资源一起关;造后端抛异常时先删再把原异常抛出去(工厂不留半开状态)。
        没登记的会话不记路由。关后端与删路由跑在处理器线程的收尾里,都不取 _lock(见类 docstring)。"""
        scenario, assignment, client_slot = info.scenario, info.assignment, info.client_slot
        with self._lock:                                     # 持锁只读登记表并记路由,判驱动与造后端都在锁外
            run_id = self._sessions.get(scenario.id)
            if run_id is not None:
                with self._routes_lock:                      # 顺序:先 _lock 后 _routes_lock
                    self._session_runs[info.session_id] = run_id

        def drop() -> None:
            with self._routes_lock:
                self._session_runs.pop(info.session_id, None)

        judged = run_id is not None and self._opponent_backend_factory is not None
        copies = self._training_copies if run_id is not None else None
        interlocutors = {b.slot_id: b.persona_id for b in assignment.bindings}
        backends: dict[str, MemoryTranslatingBackend] = {}
        orphans: list[object] = []     # 副本已开、还没包成后端就失败的端口:收尾时同样要关
        session_dir = None
        try:
            if copies is not None:
                session_dir = copies.session_dir(run_id, info.session_id)    # 会话 id 不合法在这里抛,走下面的失败收尾
            for binding in assignment.bindings:
                slot = binding.slot_id
                if slot == client_slot:
                    continue
                if self._driver_of(binding, judged=judged) == "scripted":
                    lines = scenario.opponent_script.get(slot) or self._lines
                    base = ScriptedBackend({slot: list(lines)}, loop=True)
                elif judged:
                    base = self._opponent_backend_factory(binding, scenario)
                else:
                    base = ScriptedBackend({slot: list(self._lines)})
                if copies is None:
                    memory = TupleMemory()
                else:
                    memory = copies.open_session_copy(run_id, info.session_id, slot, binding.persona_id)
                try:
                    backends[slot] = memory_role_backend(
                        base, memory=memory, episode_id=info.episode_id, interlocutors=interlocutors,
                        holder=binding.persona_id,
                    )
                except BaseException:
                    if copies is not None:
                        orphans.append(memory)
                    raise
        except Exception:
            if copies is None:
                for built in backends.values():
                    try:
                        built.close()
                    except Exception as exc:
                        logger.warning("opponent backend close failed: %s", type(exc).__name__)
            elif session_dir is not None:
                try:
                    copies.release([*backends.values(), *orphans], session_dir)
                except Exception as exc:
                    logger.warning("opponent backend close failed: %s", type(exc).__name__)
            drop()
            raise
        if copies is not None:
            def release_all() -> None:
                copies.release(list(backends.values()), session_dir)

            return SessionResources(backends=backends, closers=(release_all, drop))
        closers = tuple(built.close for built in backends.values())
        return SessionResources(backends=backends, closers=(*closers, drop) if run_id is not None else closers)

    def client_factory(self, *, deadline_s: float | None = None) -> EnvironmentServiceClient:
        """deadline_s 是这一个客户端的整条流 deadline;None 取运行时的 interact_deadline_s。
        只在持锁的临界区里读一次,不写任何属性:部署驱动器与轨迹源同时从一个运行时取客户端时各用各的值。"""
        with self._lock:
            if self._server is None:
                self._server = create_server(
                    self._backends, port=0, turn_timeout_s=self._timeout,
                    # 只放宽后端调用上限,收尾的 join 仍是服务端默认 60 秒:准入退避的 65 秒预算以它为前提
                    # (ADMISSION_WAIT_S ≥ join + 5)。等不到的在途线程进滞留表,由回收线程重试
                    **({} if self._backend_timeout is None else {"backend_timeout_s": self._backend_timeout}),
                    scenario_resolver=lambda sid: read_scenario(self._store, sid),
                    on_session_closed=self._on_session_closed,
                )
                self._server.start()
            # 守护进程长跑时每个轮次都要几个客户端;只追加不关的话,通道随轮次增长直到关停。
            # 两个驱动方(MindAgent 部署、MindTrainer 轨迹源)在一条 rollout 结束时都让客户端进入
            # closed(收完,或在 finally 里 abort),所以发新客户端之前把已结束的那些通道关掉。
            still_open = []
            for client, channel in self._channels:
                if client.closed:
                    channel.close()
                else:
                    still_open.append((client, channel))
            channel = grpc.insecure_channel(f"127.0.0.1:{self._server.bound_port}")
            client = EnvironmentServiceClient(
                DeadlineStub(self._stub_factory(channel), self._deadline if deadline_s is None else deadline_s),
                message_timeout_s=self._message_timeout,
            )
            self._channels = [*still_open, (client, channel)]
        return client

    @property
    def started(self) -> bool:
        return self._server is not None

    def close(self) -> None:
        with self._lock:
            for _, channel in self._channels:
                channel.close()
            self._channels.clear()
            if self._server is not None:
                self._server.stop(0).wait()
                self._server = None
