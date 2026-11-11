"""只读子命令:trace、cost 用 controlplane.observe 的现有函数;logs 按 log_refs 打印。三者都不提交。"""
import hashlib
import subprocess

import pytest

from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.ledger import record_cost, record_failure
from controlplane.observe import cost as observe_cost, trace as observe_trace, trace_run as observe_trace_run
from controlplane.paths import run_dir
from factory import readonly
from factory.readonly import collect_log_refs, render_logs

RUN = "run-001"


def _head_and_status(store):
    head = subprocess.run(["git", "-C", str(store.repo_root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout
    status = subprocess.run(["git", "-C", str(store.repo_root), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    return head, status


def _log_file(tmp_path, name, text):
    path = tmp_path / "logs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    path.write_bytes(data)
    return BigRef(uri=f"file://{path}", sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path / "cp"))
    s.init()
    env = Envelope(id=RUN, type="directive", version=1, created_at="2026-10-09T00:00:00Z",
                   created_by=OwnerRef(), status="executed", schema_version=1,
                   payload={"target_dimension": "tom", "recipe_ref": ""})
    s.write_product(f"{run_dir(RUN)}/directive.yaml", env, "seed directive")
    record_cost(s, RUN, task_id="plan-1", cost_type="external_inference", calls=1)
    record_cost(s, RUN, task_id="train", cost_type="gpu", gpu_hours=1.5)
    return s


def test_trace_and_cost_are_the_observe_functions_themselves():
    assert readonly.trace is observe_trace
    assert readonly.trace_run is observe_trace_run
    assert readonly.cost is observe_cost


def test_trace_run_says_whether_the_run_has_products(store):
    assert observe_trace_run(store, RUN) == (observe_trace(store, RUN), True)
    assert observe_trace_run(store, "run-404") == ("run run-404: no products found", False)


def test_cost_sums_omitted_tokens_as_zero(store):
    assert "total: gpu_hours=1.5 tokens=0" in readonly.cost(store, RUN)


def test_logs_without_any_log_refs(store):
    assert render_logs(store, RUN) == (f"run {RUN}: no log_refs", 0)


def test_logs_of_a_missing_run(store):
    assert render_logs(store, "run-404") == ("run run-404: no products found", 1)


def test_logs_print_the_referenced_file(store, tmp_path):
    ref = _log_file(tmp_path, "plan-1-1.log", "--- prompt ---\nhello\n")
    record_failure(store, RUN, task_id="plan-1", category="T3", point="trainscheduler.plan",
                   retries=0, reason="bridge down", log_ref=ref)
    text, code = render_logs(store, RUN)
    assert code == 0
    assert f"log_ref [ok] {ref.uri}" in text
    assert "--- prompt ---\nhello" in text


def test_an_unreachable_reference_is_named_not_skipped(store):
    ref = BigRef(uri="file:///compute/logs/run-001/train-1.log", sha256="a" * 64, size_bytes=1)
    record_failure(store, RUN, task_id="train", category="T1", point="mindtrainer", retries=3,
                   reason="lost", log_ref=ref)
    text, code = render_logs(store, RUN)
    assert code == 1
    assert f"log_ref [unreachable] {ref.uri}" in text
    assert "contents not shown" in text


def test_a_non_file_uri_is_unreachable(store):
    ref = BigRef(uri="s3://bucket/run-001.log", sha256="a" * 64, size_bytes=1)
    record_failure(store, RUN, task_id="train", category="T1", point="x", retries=0, reason="r", log_ref=ref)
    text, code = render_logs(store, RUN)
    assert (code, "[unreachable] s3://bucket/run-001.log" in text) == (1, True)


def test_a_hash_mismatch_is_marked(store, tmp_path):
    ref = _log_file(tmp_path, "plan-1-1.log", "original\n")
    (tmp_path / "logs" / "plan-1-1.log").write_text("tampered\n", encoding="utf-8")
    record_failure(store, RUN, task_id="plan-1", category="T3", point="p", retries=0, reason="r", log_ref=ref)
    text, code = render_logs(store, RUN)
    assert code == 1
    assert "[sha256 mismatch]" in text and "tampered" in text


def test_task_filter_uses_the_record_task_id_or_the_bridge_log_name(store, tmp_path):
    a = _log_file(tmp_path, "plan-1-1.log", "A\n")
    b = _log_file(tmp_path, "agent-1-2.log", "B\n")
    record_failure(store, RUN, task_id="plan-1", category="T3", point="p", retries=0, reason="r", log_ref=a)
    trace_rel = f"{run_dir(RUN)}/run_trace.yaml"
    env = store.read_product(trace_rel)
    env.payload["log_refs"] = [b.to_dict()]
    store.write_product(trace_rel, env, "seed: bridge log ref on run_trace")
    assert [r["uri"] for _, r in collect_log_refs(store, RUN, task_id="plan-1")] == [a.uri]
    assert [r["uri"] for _, r in collect_log_refs(store, RUN, task_id="agent-1")] == [b.uri]
    assert [r["uri"] for _, r in collect_log_refs(store, RUN)] == [a.uri, b.uri]
    assert render_logs(store, RUN, task_id="nope") == (f"run {RUN}: no log_refs for task nope", 0)


def test_reading_never_commits(store, tmp_path):
    ref = BigRef(uri="file:///nowhere.log", sha256="a" * 64, size_bytes=1)
    record_failure(store, RUN, task_id="x", category="T1", point="p", retries=0, reason="r", log_ref=ref)
    before = _head_and_status(store)
    readonly.trace(store, RUN)
    readonly.cost(store, RUN)
    render_logs(store, RUN)
    render_logs(store, RUN, task_id="x")
    render_logs(store, "run-404")
    assert _head_and_status(store) == before


# ---- logs 是排查问题时最需要的命令:树里有坏东西时它自己不能崩 ----

def _put_payload(store, name, payload):
    env = Envelope(id=name, type="failure_record", version=1, created_at="2026-10-09T00:00:00Z",
                   created_by=OwnerRef(), status="executed", schema_version=1, payload=payload)
    store.write_product(f"{run_dir(RUN)}/{name}.yaml", env, f"seed {name}")


def test_an_unreadable_product_in_the_run_is_named_and_the_rest_still_print(store, tmp_path):
    good = _log_file(tmp_path, "train-1.log", "ok line\n")
    record_failure(store, RUN, task_id="train", category="T1", point="x", retries=0, reason="r", log_ref=good)
    bad = store.repo_root / run_dir(RUN) / "broken.yaml"
    bad.write_text("<<<<<<< HEAD\nid: broken\n", encoding="utf-8")
    text, code = render_logs(store, RUN)
    assert code == 1
    assert "ok line" in text
    assert f"{run_dir(RUN)}/broken.yaml [unreadable]" in text


@pytest.mark.parametrize("ref", [{"sha256": "a" * 64}, {"uri": 5}, "not-a-mapping"],
                         ids=["no-uri", "uri-not-a-string", "ref-not-a-mapping"])
def test_a_malformed_reference_is_named_not_a_crash(store, ref):
    _put_payload(store, "failure-train", {"task_id": "train", "log_refs": [ref]})
    text, code = render_logs(store, RUN)
    assert code == 1
    assert "[malformed]" in text


def test_a_log_file_that_cannot_be_read_is_unreachable(store, tmp_path, monkeypatch):
    ref = _log_file(tmp_path, "train-1.log", "secret-free line\n")
    record_failure(store, RUN, task_id="train", category="T1", point="x", retries=0, reason="r", log_ref=ref)
    real = readonly.Path.read_bytes

    def denied(self):
        if self.name == "train-1.log":
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(readonly.Path, "read_bytes", denied)
    text, code = render_logs(store, RUN)
    assert code == 1
    assert f"log_ref [unreachable] {ref.uri}" in text
    assert "PermissionError" in text


def test_a_percent_encoded_file_uri_is_decoded(store, tmp_path):
    """file:// 地址里的空格等字符按 RFC 8089 是百分号编码的。"""
    path = tmp_path / "logs dir" / "train-1.log"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"spaced\n")
    ref = BigRef(uri=f"file://{str(path).replace(' ', '%20')}", sha256=hashlib.sha256(b"spaced\n").hexdigest(), size_bytes=7)
    record_failure(store, RUN, task_id="train", category="T1", point="x", retries=0, reason="r", log_ref=ref)
    text, code = render_logs(store, RUN)
    assert (code, "spaced" in text) == (0, True)
