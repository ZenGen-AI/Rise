from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import grpc

from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.servicer import BackendsFactory, EnvironmentServicer


def create_server(
    backends_factory: BackendsFactory,
    *,
    port: int = 0,
    turn_timeout_s: float = 30.0,
    on_session_closed=None,
    max_workers: int = 16,
) -> grpc.Server:
    """组装并绑定（不启动）gRPC 服务端。调用方负责 .start()/.stop()。
    port=0 让操作系统分配空闲端口，实际端口经 server.bound_port 取得。"""
    server = grpc.server(ThreadPoolExecutor(max_workers=max_workers))
    servicer = EnvironmentServicer(
        backends_factory, turn_timeout_s=turn_timeout_s, on_session_closed=on_session_closed,
    )
    pb2_grpc.add_EnvironmentServiceServicer_to_server(servicer, server)
    bound_port = server.add_insecure_port(f"127.0.0.1:{port}")
    server.bound_port = bound_port
    return server
