import pytest

from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from controlplane.paths import run_trace_file
from inference_bridge import (
    BridgeError,
    BridgeResult,
    InferenceBridge,
    ScriptedBridge,
    ScriptExhaustedError,
)


def test_bridge_error_carries_category_reason_attempts_and_log_ref():
    ref = BigRef(uri="file:///x.log", sha256="c" * 64, size_bytes=1)
    exc = BridgeError("T1", "timed out", attempts=3, log_ref=ref)
    assert (exc.category, exc.reason, exc.attempts, exc.log_ref) == ("T1", "timed out", 3, ref)
    assert str(exc) == "T1: timed out"
    assert BridgeError("T2").reason == "" and BridgeError("T2").attempts == 0


@pytest.mark.parametrize("bad", ["T3", "t1", ""])
def test_bridge_error_only_knows_t1_and_t2(bad):
    with pytest.raises(ValueError):
        BridgeError(bad)


def test_bridge_result_is_frozen():
    r = BridgeResult(text="hi", input_tokens=None, output_tokens=None, model="m", log_ref=None)
    with pytest.raises(AttributeError):
        r.text = "changed"


def test_scripted_bridge_answers_in_order_and_records_calls():
    b = ScriptedBridge(["first", "second"])
    assert isinstance(b, InferenceBridge)
    r1 = b.complete("p1", run_id="run-1", task_id="plan-1")
    r2 = b.complete("p2", run_id="run-1", task_id="plan-2")
    assert (r1.text, r2.text) == ("first", "second")
    assert r1 == BridgeResult(text="first", input_tokens=None, output_tokens=None, model="scripted", log_ref=None)
    assert b.calls == [("p1", "run-1", "plan-1"), ("p2", "run-1", "plan-2")]
    assert b.remaining == 0


def test_scripted_bridge_raises_injected_failures_in_sequence():
    b = ScriptedBridge([BridgeError("T1", "flaky"), "ok", BridgeError("T2", "bad")])
    with pytest.raises(BridgeError) as e1:
        b.complete("p", run_id="run-1", task_id="t1")
    assert e1.value.category == "T1"
    assert b.complete("p", run_id="run-1", task_id="t2").text == "ok"
    with pytest.raises(BridgeError) as e2:
        b.complete("p", run_id="run-1", task_id="t3")
    assert e2.value.category == "T2"
    assert len(b.calls) == 3  # 失败的调用也记下


def test_scripted_bridge_can_return_a_full_result():
    canned = BridgeResult(text="{}", input_tokens=10, output_tokens=5, model="claude-x", log_ref=None)
    assert ScriptedBridge([canned]).complete("p", run_id="r", task_id="t") is canned


def test_running_out_of_script_is_loud_and_not_a_bridge_error():
    b = ScriptedBridge(["only"])
    b.complete("p", run_id="r", task_id="t")
    with pytest.raises(ScriptExhaustedError):
        b.complete("p", run_id="r", task_id="t2")
    assert not issubclass(ScriptExhaustedError, BridgeError)


def test_scripted_bridge_rejects_unknown_script_items():
    with pytest.raises(TypeError):
        ScriptedBridge([42])


def test_scripted_bridge_records_cost_when_given_a_store(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    canned = BridgeResult(text="x", input_tokens=10, output_tokens=5, model="m", log_ref=None)
    b = ScriptedBridge(["a", canned, BridgeError("T1")], store=store)
    b.complete("p", run_id="run-1", task_id="plan-1")
    b.complete("p", run_id="run-1", task_id="plan-2")
    with pytest.raises(BridgeError):
        b.complete("p", run_id="run-1", task_id="plan-3")
    recs = store.read_product(run_trace_file("run-1")).payload["cost_records"]
    assert [(r["task_id"], r.get("tokens"), r["calls"]) for r in recs] == [
        ("plan-1", None, 1), ("plan-2", 15, 1),
    ]
    assert all(r["cost_type"] == "external_inference" for r in recs)

@pytest.mark.parametrize(("run_id", "task_id"), [("", "t"), ("r", "a/b"), ("..", "t"), ("r", ".")])
def test_scripted_bridge_checks_ids_like_the_cli_bridge(run_id, task_id):
    b = ScriptedBridge(["unused"])
    with pytest.raises(ValueError):
        b.complete("p", run_id=run_id, task_id=task_id)
    assert b.calls == [] and b.remaining == 1  # 没有消耗脚本
