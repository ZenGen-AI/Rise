from __future__ import annotations

import os

import pytest

# gRPC 的 fork 支持插件与 fork、多线程的测试进程里偶发的卡死有关。在任何 grpc 被导入之前显式关掉,
# 让整套测试(含起子进程的用例)的行为确定;开发机若已显式设了这个变量,以它为准。
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "false")


@pytest.fixture(autouse=True)
def _no_memory_config_from_the_environment(request, monkeypatch):
    """开发机的 shell 里若设了 RISE_MEMORY_CONFIG,不带 env 的 assemble(...)/main(...) 用例会读 os.environ,
    换成 Starling 路径(要密钥、要占记忆根目录)。除标了 memory_live 的用例(它们自己带真配置)外一律先清掉。"""
    if request.node.get_closest_marker("memory_live") is None:
        monkeypatch.delenv("RISE_MEMORY_CONFIG", raising=False)
