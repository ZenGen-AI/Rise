"""推理服务的生命周期(设计 DX-13 到 DX-15)。

服务是一个不会退出的远端作业,由 ComputeClient 的 start/status/stop 托管,不新增助手命令。
这里只管顺序:挑尝试序号 -> 取租约 -> 起服务 -> 开隧道 -> 等就绪 -> 交给调用方 -> 收尾。

收尾顺序固定为「关隧道、停服务、放租约」,任何一步失败都不挡后面几步,
但放租约前要确认作业确已不在运行:服务占着显存,租约放早了训练会撞上它。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any

from compute.client import JobSpec
from compute.errors import ComputeBusyError, ServeStartupError, TransportError
from compute.vllm_service import BASE_MODEL_NAME

MAX_ATTEMPTS = 8  # 同一份配置下的尝试序号上限;满了说明旧的作业目录没人清,该人看一眼


@dataclass(frozen=True)
class ServiceHandle:
    job_id: str
    base_url: str          # 本机侧的隧道地址,形如 http://127.0.0.1:<端口>
    api_key: str           # 只对这一次启动有效
    model_names: tuple[str, ...]


def _pick_attempt(client: Any, spec: JobSpec) -> str:
    """挑一个从没用过的尝试序号。绝不接管旧作业:密钥每次现生成,接上旧服务就是接上一个认证不了的东西(DX-14)。"""
    digest = hashlib.sha256(
        json.dumps({"argv": list(spec.argv), "env": dict(spec.env), "secret_env": spec.secret_env},
                   sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    base = f"serve-{digest}"
    for n in range(1, MAX_ATTEMPTS + 1):
        job_id = f"{base}-a{n}"
        if client.status(job_id).state == "missing":
            return job_id
    raise ServeStartupError(
        f"all {MAX_ATTEMPTS} attempt slots for {base} are taken; a human should clear the old job directories"
    )


def _tail(client: Any, job_id: str) -> str:
    try:
        return str(client.tail(job_id, 80))
    except Exception:  # noqa: BLE001  取不到日志不该盖住真正的失败原因
        return ""


@contextmanager
def serve(
    client: Any,
    cfg: Any,
    *,
    build_job: Callable[[Any], JobSpec],
    tunnel_factory: Callable[[], Any],
    probe: Callable[[str, str], bool],
    startup_timeout_s: float = 900.0,
    poll_s: float = 5.0,
    stop_grace_s: float = 30.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[ServiceHandle]:
    spec = build_job(cfg)
    job_id = _pick_attempt(client, spec)
    if not client.lease_acquire(job_id):
        holder, _ = client.lease_holder()
        raise ComputeBusyError(f"the GPU lease is held by job {holder or 'unknown'}")

    api_key = secrets.token_urlsafe(24)
    tunnels = ExitStack()
    started = False
    try:
        try:
            started = True  # start 抛了也要当作可能已起来,收尾时 stop 一次(对不存在的作业是空操作)
            client.start(job_id, spec, secret=api_key)
            try:
                tunnel = tunnels.enter_context(tunnel_factory())
            except (OSError, TransportError) as exc:
                raise ServeStartupError(
                    f"could not open the ssh tunnel to the server ({type(exc).__name__})"
                ) from None
            base_url = f"http://127.0.0.1:{tunnel.local_port}"
            _wait_ready(client, job_id, f"{base_url}/health", api_key, probe,
                        startup_timeout_s, poll_s, clock, sleep)
        except ServeStartupError as exc:
            if not exc.log_tail:
                exc.log_tail = _tail(client, job_id)
            raise
        yield ServiceHandle(job_id, base_url, api_key, (BASE_MODEL_NAME, getattr(cfg, "adapter_name")))
    finally:
        try:
            tunnels.close()
        except Exception:  # noqa: BLE001  关隧道失败不挡住停服务
            pass
        if started:
            if client.stop(job_id, grace_s=stop_grace_s).state == "running":
                # 服务还占着显存:不放租约,让训练撞不上它。租约在作业退出或丢失后会被助手判为陈旧。
                raise ServeStartupError(f"job {job_id} is still running after stop; the GPU lease was kept")
        client.lease_release(job_id)


def _wait_ready(client, job_id, health_url, api_key, probe, timeout_s, poll_s, clock, sleep) -> None:
    started_at = clock()
    while True:
        status = client.status(job_id)
        if status.state != "running":
            raise ServeStartupError(
                f"the server job {status.state} before it was ready (exit code {status.exit_code})"
            )
        if probe(health_url, api_key):
            return
        if clock() - started_at >= timeout_s:
            raise ServeStartupError(f"the server was not ready within {timeout_s:g}s")
        sleep(poll_s)
