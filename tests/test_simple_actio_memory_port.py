import dataclasses
import inspect

import pytest

from simple_actio.errors import SimpleActioDataError
from simple_actio.memory_port import (
    DeploymentMemoryStore, MemoryBackend, MemoryClosedError, MemoryEvent, MemoryEventConflict,
    MemoryFailureError, MemoryHit, RememberOutcome, SnapshotRef,
)


def test_port_value_types_are_frozen_with_exact_fields():
    assert [f.name for f in dataclasses.fields(MemoryHit)] == ["id", "text"]
    assert [f.name for f in dataclasses.fields(MemoryEvent)] == ["key", "text", "interlocutor"]  # 没有 seq
    assert [f.name for f in dataclasses.fields(RememberOutcome)] == ["stored", "failed", "category"]
    assert [f.name for f in dataclasses.fields(SnapshotRef)] == ["snapshot_id", "digest"]
    for value in (MemoryHit("mem-a", "pb: x"), MemoryEvent("ep#0", "pb：x", "pb"),
                  RememberOutcome(("mem-a",), False, None), SnapshotRef("snap-0", "d" * 64)):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, dataclasses.fields(value)[0].name, "y")


def test_memory_failure_is_its_own_exception_and_carries_transient():
    exc = MemoryFailureError("RecallDegradedError", transient=True)
    assert exc.transient is True and str(exc) == "RecallDegradedError"
    assert not isinstance(exc, SimpleActioDataError) and not isinstance(exc, ValueError)
    with pytest.raises(TypeError):
        MemoryFailureError("x")  # transient 必须显式给


@pytest.mark.parametrize("cls", [MemoryEventConflict, MemoryClosedError])
def test_conflict_and_closed_are_never_transient(cls):
    exc = cls("x")
    assert isinstance(exc, MemoryFailureError) and exc.transient is False


def test_both_protocols_are_checked_by_method_names():
    class Port:
        def recall(self, query, *, limit): return []
        def remember(self, event): return RememberOutcome((), False, None)
        def embed_pending(self): return 0
        def digest(self): return "0" * 64
        def close(self): return None

    class Store:
        def open(self, world_id, holder): return Port()
        def remember_event(self, world_id, holder, key, text, *, interlocutor): ...
        def abandon_event(self, world_id, holder, key): ...
        def pending_events(self, world_id): return []
        def snapshot(self, world_id, *, note): return SnapshotRef("snap-0", "")
        def restore(self, world_id, snapshot_id): return ""
        def delete_snapshot(self, world_id, snapshot_id): ...
        def fork_replay(self, world_id, snapshot_id, *, round_id): return ""
        def release_replay(self, replay_world_id): ...
        def open_replay(self, replay_world, holder): return Port()
        def digest(self, world_id): return ""
        def wait_closed(self, world_id, timeout_s): return True

    class StoreWithoutWaitClosed(Store):
        wait_closed = None  # 协议按方法名检查:缺了 wait_closed 就不是部署管理面

    assert isinstance(Port(), MemoryBackend) and not isinstance(object(), MemoryBackend)
    assert isinstance(Store(), DeploymentMemoryStore) and not isinstance(Port(), DeploymentMemoryStore)
    assert not isinstance(StoreWithoutWaitClosed(), DeploymentMemoryStore)
    assert "wait_closed" in dir(DeploymentMemoryStore)
    assert "backup_to" not in dir(DeploymentMemoryStore) and "restore_from" not in dir(DeploymentMemoryStore)
    assert "fork_replay" in dir(DeploymentMemoryStore) and "open_replay" in dir(DeploymentMemoryStore)
    # 40 篇 AC-34:open 只收部署世界 id,没有 read_only;重放世界只经 open_replay
    assert list(inspect.signature(DeploymentMemoryStore.open).parameters) == ["self", "world_id", "holder"]
