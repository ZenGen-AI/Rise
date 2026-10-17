"""compute 脚手架:延后通道基类、异常层次、端点文件路径。"""
from __future__ import annotations

import pytest

from compute.errors import (ComputeBusyError, ComputeError, RemoteJobFailedError,
                            RemoteJobTimeoutError, TransportError)
from controlplane.deferred import DeferredError
from controlplane.paths import compute_endpoints_file

DEFERRED = [ComputeBusyError, TransportError, RemoteJobFailedError, RemoteJobTimeoutError]


def test_deferred_error_carries_reason_and_retry_hint():
    exc = DeferredError("gpu busy", retry_after_seconds=90)
    assert exc.reason == "gpu busy" and exc.retry_after_seconds == 90 and str(exc) == "gpu busy"
    assert DeferredError("x").retry_after_seconds is None


@pytest.mark.parametrize("cls", DEFERRED)
def test_retryable_errors_ride_the_deferred_channel(cls):
    exc = cls("why")
    assert isinstance(exc, DeferredError) and not isinstance(exc, ComputeError)


def test_deterministic_error_is_not_deferred():
    assert not issubclass(ComputeError, DeferredError)


def test_remote_job_failed_error_keeps_the_log_tail_out_of_the_reason():
    exc = RemoteJobFailedError("job j1 exited with code 3", exit_code=3, log_tail="boom")
    assert exc.exit_code == 3 and exc.log_tail == "boom"
    assert "boom" not in exc.reason and "boom" not in str(exc)


@pytest.mark.parametrize("module", ["mindtrainer.dispatch", "mindtrainer.orchestrator"])
def test_deferred_errors_are_not_in_the_deterministic_sets(module):
    import importlib
    det = tuple(importlib.import_module(module)._DETERMINISTIC)
    assert not any(issubclass(cls, det) for cls in DEFERRED)


def test_endpoints_file_sits_beside_the_other_control_plane_products():
    assert compute_endpoints_file() == "control-plane/compute/endpoints.yaml"
