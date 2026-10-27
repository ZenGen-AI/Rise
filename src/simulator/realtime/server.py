from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import grpc

from simulator.datamodel import Scenario
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.servicer import BackendsFactory, EnvironmentServicer


def create_server(
    backends_factory: BackendsFactory,
    *,
    port: int = 0,
    turn_timeout_s: float = 30.0,
    on_session_closed=None,
    max_workers: int = 16,
    scenario_resolver: Callable[[str], Scenario] | None = None,
    max_turns_cap: int = 200,
    create_timeout_s: float = 10.0,
    backend_timeout_s: float = 60.0,
    resource_join_timeout_s: float = 60.0,
    linger_retry_s: float = 60.0,
    keepalive_time_ms: int = 30_000,
    keepalive_timeout_ms: int = 10_000,
) -> grpc.Server:
    """组装并绑定（不启动）gRPC 服务端。调用方负责 .start()/.stop()。
    port=0 让操作系统分配空闲端口，实际端口经 server.bound_port 取得。

    资源上限（§2.6.5 / 面向网络暴露前的必要边界）：
    - maximum_concurrent_rpcs 与 max_workers 对齐：每个活跃会话独占一个
      worker 线程贯穿整个生命周期，第 N+1 个会话会被 RESOURCE_EXHAUSTED
      拒绝，而不是在池后无声排队直到有人释放。
    - keepalive：让服务端能在客户端进程崩溃/网络中断时主动发现死连接，
      而不是依赖客户端配合（§2.6.5 客户端侧异常由服务端自行检测）。
    - max_turns_cap / create_timeout_s / backend_timeout_s 透传给 servicer。
    - resource_join_timeout_s / linger_retry_s 也透传给 servicer：会话终态收尾时
      join 在途后端调用的上限，以及滞留表每隔多久重试一次 join(0)。默认值取自设计源
      6.2 节（60 秒、60 秒），参数只为测试能用小值。
    - server.servicer 指向 servicer，调用方可读 lingering_count()。
    """
    server = grpc.server(
        ThreadPoolExecutor(max_workers=max_workers),
        maximum_concurrent_rpcs=max_workers,
        options=[
            ("grpc.keepalive_time_ms", keepalive_time_ms),
            ("grpc.keepalive_timeout_ms", keepalive_timeout_ms),
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.http2.max_pings_without_data", 0),
        ],
    )
    servicer = EnvironmentServicer(
        backends_factory,
        turn_timeout_s=turn_timeout_s,
        on_session_closed=on_session_closed,
        scenario_resolver=scenario_resolver,
        max_turns_cap=max_turns_cap,
        create_timeout_s=create_timeout_s,
        backend_timeout_s=backend_timeout_s,
        resource_join_timeout_s=resource_join_timeout_s,
        linger_retry_s=linger_retry_s,
    )
    pb2_grpc.add_EnvironmentServiceServicer_to_server(servicer, server)
    bound_port = server.add_insecure_port(f"127.0.0.1:{port}")
    server.bound_port = bound_port
    server.servicer = servicer
    return server
