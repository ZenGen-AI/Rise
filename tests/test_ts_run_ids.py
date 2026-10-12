"""run id 的分配:run-NNN,至少三位补零,取现有最大序号加一;只数 run-<数字>。"""
import pytest

from controlplane.paths import run_dir
from trainscheduler.errors import RunIdOrderError, SchedulerError
from trainscheduler.run_ids import (
    allocate_run_id,
    blocking_run_dirs,
    format_run_id,
    list_run_dirs,
    next_run_id,
    run_number,
    run_order_key,
)
from ts_fixtures import make_store, write_directive


@pytest.mark.parametrize(
    "name, expected",
    [("run-001", 1), ("run-010", 10), ("run-1", 1), ("run-1000", 1000),
     ("run-", None), ("run-1b", None), ("exp-001", None), ("RUN-001", None), ("run-+1", None), ("run--1", None)],
)
def test_run_number_only_reads_run_dash_digits(name, expected):
    assert run_number(name) == expected


def test_format_pads_to_at_least_three_digits():
    assert [format_run_id(n) for n in (1, 9, 10, 999, 1000)] == [
        "run-001", "run-009", "run-010", "run-999", "run-1000"]


@pytest.mark.parametrize("bad", [0, -1, True, 1.0, "1"])
def test_format_rejects_non_positive_ints(bad):
    with pytest.raises(ValueError):
        format_run_id(bad)


def test_next_run_id_on_nothing_is_run_001():
    assert next_run_id([]) == "run-001"


def test_next_run_id_takes_the_largest_number_not_the_last_string():
    assert next_run_id(["run-001", "run-010", "run-002"]) == "run-011"
    assert next_run_id(["run-9", "run-10"]) == "run-011"


def test_next_run_id_ignores_hand_named_runs():
    assert next_run_id(["exp-a", "run-1b", "zzz"]) == "run-001"
    assert next_run_id(["run-003", "manual-099", "run-03x"]) == "run-004"


def test_allocate_scans_the_tree(tmp_path):
    store = make_store(tmp_path)
    assert allocate_run_id(store) == "run-001"
    for run_id in ("run-001", "run-010", "my-experiment"):
        write_directive(store, run_id)
    assert list_run_dirs(store) == ["my-experiment", "run-001", "run-010"]
    assert allocate_run_id(store) == "run-011"


def test_allocate_counts_directories_not_files(tmp_path):
    store = make_store(tmp_path)
    (store.repo_root / run_dir("run-005")).mkdir(parents=True)
    (store.repo_root / "control-plane/runs/run-900.yaml").write_text("x", encoding="utf-8")
    assert allocate_run_id(store) == "run-006"


def test_order_key_puts_numbered_runs_in_numeric_order_after_the_rest():
    names = ["run-010", "manual", "run-002", "run-1000", "run-999"]
    assert sorted(names, key=run_order_key) == ["manual", "run-002", "run-010", "run-999", "run-1000"]


def test_blocking_run_dirs_uses_numbers_and_ignores_other_names():
    assert blocking_run_dirs(["run-001", "manual", "exp-a"], "run-002") == []
    assert blocking_run_dirs(["run-9", "run-010", "zz", "test-x"], "run-011") == []
    assert blocking_run_dirs(["run-999"], "run-1000") == []  # 数值序:四位排在三位之后


def test_only_a_run_dir_numbered_at_least_the_new_id_blocks():
    assert blocking_run_dirs(["run-050"], "run-050") == ["run-050"]  # 编号相等就算挡
    assert blocking_run_dirs(["run-50", "run-051", "run-049"], "run-050") == ["run-051", "run-50"]


def test_blocking_run_dirs_needs_a_run_id():
    with pytest.raises(ValueError):
        blocking_run_dirs(["run-001"], "zz")


@pytest.mark.parametrize(
    "dirs, expected",
    [
        (["run-9"], "run-010"),                       # 不补零的老名字照样按数值参与
        (["run-9", "run-010", "test-x", "zz"], "run-011"),
        (["run-001", "test-x"], "run-002"),           # 手工起的名字不参与比较
        (["run-999"], "run-1000"),                    # 超过 999 轮按数值继续
    ],
)
def test_allocate_succeeds_on_trees_that_used_to_look_out_of_order(tmp_path, dirs, expected):
    store = make_store(tmp_path)
    for name in dirs:
        (store.repo_root / run_dir(name)).mkdir(parents=True)
    assert allocate_run_id(store) == expected


def test_allocate_refuses_when_a_hand_made_dir_has_the_same_number(tmp_path, monkeypatch):
    """正常路径走不到(新编号恒为最大编号加一)。这里让编号计算"看漏"一个人手建的 run-050,
    模拟树被手工改过:分配器不覆盖它,判确定性失败。"""
    import trainscheduler.run_ids as rid

    store = make_store(tmp_path)
    (store.repo_root / run_dir("run-050")).mkdir(parents=True)
    monkeypatch.setattr(rid, "next_run_id", lambda existing: "run-050")
    with pytest.raises(RunIdOrderError, match=r"not numerically greater than existing run dir\(s\) \['run-050'\]"):
        allocate_run_id(store)
    assert issubclass(RunIdOrderError, SchedulerError)
