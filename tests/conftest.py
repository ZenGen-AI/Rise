from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_memory_config_from_the_environment(request, monkeypatch):
    """开发机的 shell 里若设了 RISE_MEMORY_CONFIG,不带 env 的 assemble(...)/main(...) 用例会读 os.environ,
    换成 Starling 路径(要密钥、要占记忆根目录)。除标了 memory_live 的用例(它们自己带真配置)外一律先清掉。"""
    if request.node.get_closest_marker("memory_live") is None:
        monkeypatch.delenv("RISE_MEMORY_CONFIG", raising=False)
