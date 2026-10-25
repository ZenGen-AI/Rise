"""MindAgent 轮次处理器的装配材料:agent.yaml、进程内 loopback 实时服务端、V1 罐头模型后端。

agent.yaml 是组合根自己的配置信封(control-plane/config/agent.yaml),载荷键:
- 必填 scenario_ids(非空字符串列表)、bindings(RoleBinding.to_dict() 的列表)、opponent_lines(非空字符串列表);
- 可选 AgentConfig 的其余字段(max_turns、episodes_per_scenario、max_retries_per_episode、min_support、
  min_ratio、min_fired_on_motivating、max_fired_elsewhere、max_parse_retries),缺省用 AgentConfig 的默认值。
未知键、类型错都抛 FactoryConfigError;AgentConfig 自己的校验错误也转成 FactoryConfigError。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import fields

import grpc

from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT
from factory.configio import describe, key_names, read_tree_config
from factory.errors import FactoryConfigError
from factory.opponents import driver_for
from mindagent.config import AgentConfig
from simulator.backends import ModelBackend, ScriptedBackend
from simulator.datamodel import RoleBinding, Scenario
from simulator.realtime.client import EnvironmentServiceClient
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server
from simulator.store_adapter import read_persona, read_scenario

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
    return config, tuple(lines)


INTERACT_DEADLINE_S = 15.0


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
    对手位的后端按各自绑定的驱动分派(见 _backends);情境从树里读(scenario_resolver)。
    每条 Interact 流带 interact_deadline_s 的 deadline(DeadlineStub);stub_factory 只为测试注入。

    opponent_backend_factory(binding, scenario) 给 vllm 与 external_api 驱动的对手位造后端,没传(None)时
    这两种也和过去一样用 ScriptedBackend 说 opponent_lines:MindAgent 部署会话的绑定多是 frozen_reference,
    没有真模型可加载,行为必须原样不变。"""

    def __init__(self, store: GitStore, opponent_lines: tuple[str, ...], *, turn_timeout_s: float = 5.0,
                 interact_deadline_s: float = INTERACT_DEADLINE_S,
                 stub_factory=pb2_grpc.EnvironmentServiceStub,
                 opponent_backend_factory: Callable[[RoleBinding, Scenario], ModelBackend] | None = None) -> None:
        self._store = store
        self._lines = list(opponent_lines)
        self._timeout = turn_timeout_s
        self._deadline = interact_deadline_s
        self._stub_factory = stub_factory
        self._opponent_backend_factory = opponent_backend_factory
        self._server = None
        # (客户端, 通道) 成对记下:会话结束的客户端,通道在下一次发客户端时关掉(见 client_factory)
        self._channels: list = []
        self._lock = threading.Lock()

    def _driver_of(self, binding: RoleBinding) -> str | None:
        """没传工厂时,只有显式写了 scripted 的绑定才改变行为,其余一律不判驱动、给原来的罐头后端:
        agent.yaml 不校验 model_backend 的取值,driver_for 对 self_play 与未知后端名会抛 ValueError,
        而过去的 _backends 从不看绑定。传了工厂就是训练路,绑定已过 validate_training_config,照实判。"""
        if self._opponent_backend_factory is None and binding.driver != "scripted":
            return None
        return driver_for(binding)

    def _backends(self, scenario, assignment, personas, client_slot):
        """每个非客户端角色位一个全新的后端(服务端要求每次调用返回新实例,游标不能跨会话共享)。
        scripted 驱动:ScriptedBackend(loop=True),台词取情境剧本里该角色位的一份,没有或为空取全局台词;
        其余驱动:有工厂就交给工厂,没有就是原来的 ScriptedBackend(不循环,全局台词)。"""
        backends = {}
        for binding in assignment.bindings:
            slot = binding.slot_id
            if slot == client_slot:
                continue
            if self._driver_of(binding) == "scripted":
                lines = scenario.opponent_script.get(slot) or self._lines
                backends[slot] = ScriptedBackend({slot: list(lines)}, loop=True)
            elif self._opponent_backend_factory is not None:
                backends[slot] = self._opponent_backend_factory(binding, scenario)
            else:
                backends[slot] = ScriptedBackend({slot: list(self._lines)})
        return backends

    def client_factory(self) -> EnvironmentServiceClient:
        with self._lock:
            if self._server is None:
                self._server = create_server(
                    self._backends, port=0, turn_timeout_s=self._timeout,
                    scenario_resolver=lambda sid: read_scenario(self._store, sid),
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
            client = EnvironmentServiceClient(DeadlineStub(self._stub_factory(channel), self._deadline))
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
