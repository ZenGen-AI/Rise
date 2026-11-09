"""客户端异常的错误映射(设计四 Task 10;40 篇第 8 节码表、AC-33):按 code 与 transient 映射,三处由 rise 改判。"""
import pytest

errors = pytest.importorskip("starling_client.errors")
from simple_actio.memory_port import MemoryClosedError, MemoryEventConflict, MemoryFailureError
from simple_actio.memory_starling import translate_client_failure


def _t(exc, *, closed=False, drain=False):
    return translate_client_failure(exc, port_closed_by_rise=closed, in_drain_window=drain)


@pytest.mark.parametrize("exc, cls, transient", [
    (errors.EventConflict("event_conflict"), MemoryEventConflict, False),
    (errors.SessionExpired("session_expired"), MemoryFailureError, True),
    (errors.RecallDegradedError("recall_degraded", transient=True), MemoryFailureError, True),
    (errors.ServiceUnavailable("service unreachable"), MemoryFailureError, True),
    (errors.ServiceTimeout("service request timed out"), MemoryFailureError, True),
    (errors.ServiceError("world_busy", code="world_busy", transient=True), MemoryFailureError, True),
    (errors.WorldError("world_settings_mismatch", code="world_settings_mismatch", transient=False), MemoryFailureError, False),
    (errors.EventNotFound("event_not_found"), MemoryFailureError, False),
    (errors.WorldError("brand_new_code", code="brand_new_code", transient=True), MemoryFailureError, True),   # 信 Starling 的标记
    (errors.WorldError("worlds_unavailable", code="worlds_unavailable", transient=True), MemoryFailureError, True),
    (errors.WorldError("world_read_only", code="world_read_only", transient=False), MemoryFailureError, False),
    (errors.WorldError("snapshot_not_found", code="snapshot_not_found", transient=False), MemoryFailureError, False),
    (errors.WorldError("restore_digest_mismatch", code="restore_digest_mismatch"), MemoryFailureError, False),
    (errors.Unauthorized("unauthorized"), MemoryFailureError, False),
    (errors.ProtocolMismatch("protocol_mismatch"), MemoryFailureError, False),
])
def test_each_code_maps_to_its_class_and_transient_flag(exc, cls, transient):
    out = _t(exc)
    assert type(out) is cls and out.transient is transient
    assert str(out).startswith(f"starling {exc.code} (")
    assert str(out) == f"starling {exc.code} ({type(exc).__name__})"


def test_handle_closed_is_permanent_only_when_rise_closed_the_port():
    exc = errors.MemoryClosed("handle_closed")
    assert type(_t(exc, closed=True)) is MemoryClosedError
    assert type(_t(exc, closed=False)) is MemoryFailureError and _t(exc, closed=False).transient is True


def test_world_held_is_transient_only_inside_the_drain_window():
    exc = errors.WorldError("world_held", code="world_held", transient=False)
    assert _t(exc, drain=True).transient is True and _t(exc, drain=False).transient is False
    assert type(_t(exc, drain=True)) is MemoryFailureError


def test_the_drain_window_and_the_closed_flag_only_touch_their_own_codes():
    busy = errors.WorldError("world_busy", code="world_busy", transient=True)
    assert _t(busy, closed=True, drain=True).transient is True
    mismatch = errors.WorldError("world_settings_mismatch", code="world_settings_mismatch", transient=False)
    assert _t(mismatch, closed=True, drain=True).transient is False


def test_a_close_timeout_and_a_surprise_are_permanent_and_name_only_the_class():
    out = _t(TimeoutError("waited 30 s on http://127.0.0.1:8787"))
    assert type(out) is MemoryFailureError and out.transient is False and "8787" not in str(out)
    surprise = _t(RuntimeError("boom at http://127.0.0.1:8787 Bearer s3cr3t"))
    assert str(surprise) == "starling client failed: RuntimeError" and surprise.transient is False


def test_the_message_carries_no_detail_url_or_token():
    exc = errors.WorldError("world_held", code="world_held")
    exc.args = ("world_held http://127.0.0.1:8787 Bearer s3cr3t",)
    assert "8787" not in str(_t(exc)) and "s3cr3t" not in str(_t(exc))
