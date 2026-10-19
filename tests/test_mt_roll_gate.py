"""训练数据准入检查的注入点:命中就在上传之前失败,远端什么都不写,原因里只有计数。"""
from __future__ import annotations

import json

import pytest
from compute_fixtures import FakeClient, fake_endpoint
from controlplane.envelope import BigRef
from controlplane.deferred import DeferredError
from mindtrainer.roll_executor import RollExecutor, TrainingDataRejectedError
from simulator.datamodel import Episode, RoleAssignment, RoleBinding, TurnRecord

RUN = "run-1"


def _episode(ep_id="e0"):
    assignment = RoleAssignment(
        "s1",
        [RoleBinding("a", "pa", "self_play"),
         RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")],
        max_turns=4,
    )
    records = [TurnRecord(0, "a", "你好"), TurnRecord(1, "b", "你也好")]
    return Episode(ep_id, "s1", assignment, records, "max_turns")


def _loaded(client: FakeClient, **kw) -> RollExecutor:
    endpoint = fake_endpoint()
    executor = RollExecutor(client=client, endpoint=endpoint, run_id=RUN, **kw)
    digest = client.seed_dir(endpoint.base_model_path)
    executor.load(BigRef(
        uri=f"ssh://{endpoint.endpoint_id}{endpoint.base_model_path}",
        sha256=digest, size_bytes=0, fetch_hint={"type": "roll_ssh"},
    ))
    return executor


def _uploaded(client: FakeClient, executor: RollExecutor):
    client.files.clear()
    client.job_hook = None
    with pytest.raises(Exception):
        # 作业本身在替身里不会产出存点,所以这里只关心上传是否发生
        executor.run_sft_stage([[_episode()]], first_step=0, trained_slot="a", prior_batches=[])
    return dict(client.files)


def test_without_a_gate_the_upload_is_unchanged():
    client = FakeClient()
    executor = _loaded(client)
    files = _uploaded(client, executor)
    assert any(path.endswith("train.jsonl") for path in files)


def test_a_clean_gate_lets_the_upload_through():
    seen = []

    def gate(data: bytes) -> tuple[int, int]:
        seen.append(data)
        return (0, 0)

    client = FakeClient()
    executor = _loaded(client, row_gate=gate)
    files = _uploaded(client, executor)
    train = [v for k, v in files.items() if k.endswith("train.jsonl")][0]
    assert seen == [train]
    assert json.loads(train.decode().splitlines()[0])["output"] == "你好"


def test_a_hit_fails_before_anything_is_written_remotely():
    client = FakeClient()
    executor = _loaded(client, row_gate=lambda data: (2, 7))
    written_before = dict(client.files)
    with pytest.raises(TrainingDataRejectedError) as caught:
        executor.run_sft_stage([[_episode()]], first_step=0, trained_slot="a", prior_batches=[])
    assert client.files == written_before          # 没有 train.jsonl,也没有作业目录
    assert not any(path.endswith("train.jsonl") for path in client.files)
    assert "2" in str(caught.value) and "7" in str(caught.value)
    assert "你好" not in str(caught.value)


def test_the_gate_sees_the_prefix_rows_too():
    seen = []

    def gate(data: bytes):
        seen.append(data)
        return (0, 0)

    client = FakeClient()
    executor = _loaded(client, row_gate=gate)
    _uploaded(client, executor)
    assert len(seen) == 1
    with pytest.raises(Exception):
        executor.run_sft_stage([[_episode()]], first_step=1, trained_slot="a",
                               prior_batches=[[_episode("e1")]])
    assert len(seen) == 2
    assert seen[1] != seen[0]


def test_the_rejection_is_deterministic_not_deferred():
    assert not issubclass(TrainingDataRejectedError, DeferredError)
