"""vLLM 策略后端与起停上下文:训练中的策略模型,以及可选地同一个服务里的对手适配器。

一个 vLLM 进程至多挂两个 LoRA(见 compute.vllm_service.MAX_ADAPTERS):被训练的策略一个、冻结的对手一个。
- policy_context:起服务(经 compute.service.serve,租约、隧道、就绪探测、收尾都在那里)并产出 VllmPolicy;
  退出时服务停掉、租约放掉,体内抛了异常也一样。
- VllmPolicy:ModelBackend,把一次发言经本机隧道 POST 给服务,model 填适配器名,或基座名 base。
- vllm_opponent_factory:同一个服务上的对手后端,按 binding.model_ref 选适配器。

HTTP 与失败处理全在 factory.opponents.post_chat(与外部 API 共用):密钥取自 ServiceHandle,
绝不出现在任何异常信息或回溯里。本模块不碰 git 树,model_ref 到适配器导出路径的解析(resolve_model_ref)由组合根做。
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager

from compute.client import ComputeClient
from compute.endpoint import ComputeEndpoint
from compute.errors import ComputeError
from compute.service import ServiceHandle, serve as real_serve
from compute.tunnel import http_probe, tunnel_factory_for
from compute.vllm_service import BASE_MODEL_NAME, VllmServeConfig, build_serve_job
from factory.opponents import ModelRefError, chat_messages, post_chat, require_timeout
from simulator.backends import ModelBackend
from simulator.datamodel import Persona, RoleBinding, Scenario, TurnRecord

# 一次发言的等待上限。服务端另有按回合的限时(TimeoutBackend),这一层只是不让 HTTP 无限挂着
POLICY_TIMEOUT_S = 120.0


class VllmPolicy:
    """ModelBackend:向 vLLM 服务的 /v1/chat/completions 要一次发言。一次 respond 一次 POST,不重试
    (重试与降级是调用方的事)。model_name 是服务里的适配器名,或 BASE_MODEL_NAME。

    handle 公开,是为了让组合根在同一个服务上再造对手后端(vllm_opponent_factory(policy.handle, ...))。"""

    def __init__(self, handle: ServiceHandle, model_name: str, *, timeout_s: float) -> None:
        self._timeout = require_timeout(timeout_s)
        if model_name not in handle.model_names:
            raise ComputeError(
                f"the vLLM service does not serve a model named {model_name!r}; it serves {list(handle.model_names)}"
            )
        self.handle = handle
        self._model = model_name
        self._label = f"the vLLM service {handle.job_id}"

    def respond(
        self,
        persona: Persona,
        scenario: Scenario,
        history: list[TurnRecord],
        speaker_slot: str,
    ) -> tuple[str, str | None]:
        return self.respond_guided(persona, scenario, history, speaker_slot, ())

    def respond_guided(
        self,
        persona: Persona,
        scenario: Scenario,
        history: list[TurnRecord],
        speaker_slot: str,
        guidance: Sequence[str],
    ) -> tuple[str, str | None]:
        """带指导语的发言:guidance 并入 system 消息(见 chat_messages),其余与 respond 同一条路径。"""
        content = post_chat(
            self.handle.base_url, self._model, self.handle.api_key,
            chat_messages(persona, scenario, history, speaker_slot, guidance),
            timeout_s=self._timeout, host_label=self._label,
        )
        return content, None


@contextmanager
def policy_context(
    client: ComputeClient,
    endpoint: ComputeEndpoint,
    adapters: Sequence[tuple[str, str]],
    *,
    serve_port: int,
    trained_adapter: str | None,
    serve: Callable[..., object] = real_serve,
    build_job: Callable[[VllmServeConfig], object] = build_serve_job,
    tunnel_factory: Callable[[], object] | None = None,
    probe: Callable[[str, str], bool] = http_probe,
    timeout_s: float = POLICY_TIMEOUT_S,
) -> Iterator[VllmPolicy]:
    """起一个挂着 adapters 的 vLLM 服务,产出绑定到 trained_adapter 的策略;退出时停服务放租约。

    trained_adapter 是 adapters 里某个适配器的名字,或 None(用基座说话,此时 adapters 里只剩对手的)。
    不在其中是配置错误,在占用 GPU 租约之前就抛 ComputeError。进入服务时的失败(租约被占、起不来)原样上抛,
    其中的延后类异常由调用方照延后通道处理。serve、build_job、tunnel_factory、probe 可注入,测试里不连 SSH。"""
    require_timeout(timeout_s)
    cfg = VllmServeConfig(
        model_path=endpoint.base_model_path, adapters=tuple(adapters), remote_port=serve_port,
    )
    served = [name for name, _ in cfg.adapters]
    if trained_adapter is not None and trained_adapter not in served:
        raise ComputeError(f"trained_adapter {trained_adapter!r} is not among the served adapters {served}")
    factory = tunnel_factory if tunnel_factory is not None else tunnel_factory_for(client, cfg.remote_port)
    with serve(client, cfg, build_job=build_job, tunnel_factory=factory, probe=probe) as handle:
        yield VllmPolicy(
            handle, BASE_MODEL_NAME if trained_adapter is None else trained_adapter, timeout_s=timeout_s,
        )


def vllm_opponent_factory(
    handle: ServiceHandle,
    adapter_for_ref: Mapping[str, str],
    *,
    timeout_s: float,
) -> Callable[[RoleBinding, Scenario], ModelBackend]:
    """AgentRuntime(opponent_backend_factory=...) 要的工厂:每次给对手位一个新的后端,绑定到
    adapter_for_ref[binding.model_ref] 这个适配器。model_ref 不在映射里抛 ModelRefError(确定性,重试无益)。

    映射在构造时拷贝并核对:适配器必须是服务里真有的,且不能是基座(对手就是要加载 model_ref 指向的导出)。
    这样配置错在起训练之前就暴露,而不是某次对手发言时才 404。"""
    require_timeout(timeout_s)
    mapping = dict(adapter_for_ref)
    for ref, adapter in mapping.items():
        if adapter == BASE_MODEL_NAME:
            raise ComputeError(f"model_ref {ref!r} maps to the base model {BASE_MODEL_NAME!r}, not an adapter")
        if adapter not in handle.model_names:
            raise ComputeError(
                f"model_ref {ref!r} maps to adapter {adapter!r}, which the vLLM service does not serve "
                f"(it serves {list(handle.model_names)}); the adapter is missing"
            )

    def make(binding: RoleBinding, scenario: Scenario) -> ModelBackend:
        ref = binding.model_ref
        if not isinstance(ref, str) or ref not in mapping:
            raise ModelRefError(
                f"model_ref {ref!r} of slot {binding.slot_id!r} has no adapter on the vLLM service "
                f"(known model_refs: {sorted(mapping)})"
            )
        return VllmPolicy(handle, mapping[ref], timeout_s=timeout_s)

    return make
