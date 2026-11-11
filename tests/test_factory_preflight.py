"""规划相关配置的启动检查(Codex 复核第 3 条):scheduler.yaml、training_defaults.yaml 与规划模板有毛病时,
装配期就是一行错误与退出码 2,树里什么都不写;处理器每次调用仍照计划 5 重读配置,预检不缓存。"""
from pathlib import Path

import pytest

from controlplane.config import write_config
from controlplane.envelope import Envelope, OwnerRef
from controlplane.paths import planner_prompt_file, scheduler_config_file, training_defaults_file
from controlplane.reconcile import Action
from factory.cli import main
from factory.configio import preflight
from factory.errors import FactoryConfigError
from factory.wire import FactoryOptions, assemble
from factory_fixtures import closed_loop_repo, git_head, tree_files
from trainscheduler.config import default_training_defaults_payload, load_scheduler_config
from trainscheduler.errors import SchedulerConfigError

SECRET = "sekrit-probe-0042"


def _repo(tmp_path, **kw):
    return closed_loop_repo(str(tmp_path / "cp"), **kw)


def _scheduler(store, **change):
    write_config(store, scheduler_config_file(), "scheduler",
                 dict(load_scheduler_config(store).to_dict(), **change), "test: scheduler.yaml")


def _training_defaults(store, mutate):
    payload = default_training_defaults_payload()
    mutate(payload)
    write_config(store, training_defaults_file(), "training-defaults", payload, "test: training_defaults.yaml")


def _as_envelope(store, rel, type_, payload):
    env = Envelope(id=Path(rel).stem, type=type_, version=1, created_at="2026-10-09T00:00:00Z",
                   created_by=OwnerRef(), status="effective", schema_version=1, payload=payload)
    store.write_product(rel, env, f"test: {rel} as {type_}")


def _remove(store, rel):
    (store.repo_root / rel).unlink()


def _reconcile(store, enable, capsys):
    code = main(["reconcile", str(store.repo_root), "--once", "--enable", enable,
                 "--planner-backend", "heuristic"], env={})
    return code, capsys.readouterr().err


def _assert_one_line_exit_2(code, err, fragment):
    assert code == 2
    assert err.startswith("factory: error: ")
    assert len(err.splitlines()) == 1
    assert fragment in err


BROKEN = [
    pytest.param("plan", lambda s: _scheduler(s, confirm_directives="yes"), "scheduler.confirm_directives",
                 id="plan-scheduler-bad-bool"),
    pytest.param("nominate", lambda s: _scheduler(s, confirm_directives="yes"), "scheduler.confirm_directives",
                 id="nominate-scheduler-bad-bool"),
    pytest.param("plan", lambda s: _scheduler(s, template_version="v99"), "planner prompt template",
                 id="plan-template-version-not-installed"),
    pytest.param("plan", lambda s: _remove(s, planner_prompt_file("v1")), "planner prompt template",
                 id="plan-template-file-missing"),
    pytest.param("plan", lambda s: _remove(s, training_defaults_file()), "training_defaults.yaml",
                 id="plan-training-defaults-missing"),
    pytest.param("plan", lambda s: _training_defaults(s, lambda p: p["training_config"].update(sft_steps="many")),
                 "sft_steps", id="plan-training-defaults-bad-type"),
]


@pytest.mark.parametrize("enable,break_it,fragment", BROKEN)
def test_a_bad_planning_input_ends_assembly_with_one_line_and_exit_2(tmp_path, capsys, enable, break_it, fragment):
    store = _repo(tmp_path, run_id=None)
    break_it(store)
    head, files = git_head(store), tree_files(store)
    code, err = _reconcile(store, enable, capsys)
    _assert_one_line_exit_2(code, err, fragment)
    assert (git_head(store), tree_files(store)) == (head, files)


RAW = [
    pytest.param(scheduler_config_file(),
                 lambda s, rel: (s.repo_root / rel).write_text(f"id: scheduler\nbad: {SECRET}: [\n", encoding="utf-8"),
                 "scheduler.yaml", id="scheduler-yaml-syntax-error"),
    pytest.param(training_defaults_file(),
                 lambda s, rel: (s.repo_root / rel).write_text(f"id: training\nbad: {SECRET}: [\n", encoding="utf-8"),
                 "training_defaults.yaml", id="training-defaults-yaml-syntax-error"),
    pytest.param(scheduler_config_file(), lambda s, rel: _as_envelope(s, rel, "directive", {}),
                 "scheduler.yaml", id="scheduler-that-is-not-a-config"),
    pytest.param(scheduler_config_file(),
                 lambda s, rel: _as_envelope(s, rel, "config", {**load_scheduler_config(s).to_dict(), "token": SECRET}),
                 "token", id="scheduler-with-a-credential-field"),
]


@pytest.mark.parametrize("rel,damage,fragment", RAW)
def test_unreadable_planning_files_exit_2_without_quoting_their_content(tmp_path, capsys, rel, damage, fragment):
    store = _repo(tmp_path, run_id=None)
    damage(store, rel)
    files = tree_files(store)
    code, err = _reconcile(store, "plan", capsys)
    _assert_one_line_exit_2(code, err, fragment)
    assert SECRET not in err
    assert tree_files(store) == files


VALUES = [
    pytest.param(lambda s: _scheduler(s, min_gain=SECRET), "scheduler.min_gain", id="scheduler-min-gain"),
    pytest.param(lambda s: _training_defaults(s, lambda p: p["training_config"].update(mode=SECRET)), "mode",
                 id="training-config-mode"),
    pytest.param(lambda s: _training_defaults(s, lambda p: p["data_spec"].update(purpose=SECRET)), "purpose",
                 id="data-spec-purpose"),
]


@pytest.mark.parametrize("break_it,fragment", VALUES)
def test_a_bad_planning_value_is_named_by_its_field_and_never_echoed(tmp_path, capsys, break_it, fragment):
    store = _repo(tmp_path, run_id=None)
    break_it(store)
    code, err = _reconcile(store, "plan", capsys)
    _assert_one_line_exit_2(code, err, fragment)
    assert SECRET not in err


@pytest.mark.parametrize("enable", ["track", "admit", "agent"])
def test_switches_that_never_read_the_scheduler_are_not_blocked_by_it(tmp_path, capsys, enable):
    store = _repo(tmp_path, run_id=None)
    _scheduler(store, confirm_directives="yes")
    assert _reconcile(store, enable, capsys) == (0, "")


def test_nominate_needs_neither_the_template_nor_the_training_defaults(tmp_path, capsys):
    store = _repo(tmp_path, run_id=None)
    _remove(store, planner_prompt_file("v1"))
    _remove(store, training_defaults_file())
    assert _reconcile(store, "nominate", capsys) == (0, "")


def test_a_good_tree_still_assembles_with_plan_and_nominate(tmp_path, capsys):
    store = _repo(tmp_path, run_id=None)
    assert _reconcile(store, "plan,nominate", capsys) == (0, "")


def test_run_does_not_start_ticking_when_a_planning_input_is_broken(tmp_path, capsys):
    """Codex 复核复现的场景:缺 training_defaults.yaml 时,装配过关、前几个 tick 派发训练并写诊断,
    到规划那一刻才回溯退出。预检之后一个 tick 都不该跑。"""
    store = _repo(tmp_path)
    _remove(store, training_defaults_file())
    head, files = git_head(store), tree_files(store)
    code = main(["run", str(store.repo_root), "--enable", "plan", "--planner-backend", "heuristic",
                 "--allow-mock-evidence", "--interval-s", "0", "--max-ticks", "5"], env={})
    err = capsys.readouterr().err
    _assert_one_line_exit_2(code, err, "training_defaults.yaml")
    assert (git_head(store), tree_files(store)) == (head, files)


def test_the_handler_still_rereads_scheduler_yaml_on_every_call(tmp_path):
    """预检不缓存:装配之后才改坏的配置,处理器下一次调用照样读到(计划 5 的设计)。"""
    store = _repo(tmp_path, run_id=None)
    asm = assemble(store, FactoryOptions(switches=("plan",), planner_backend="heuristic"), env={})
    try:
        _scheduler(store, confirm_directives="yes")
        action = Action(kind="plan_next_round", target_id="run-001", detail={"run_id": "run-001"})
        with pytest.raises(SchedulerConfigError, match="scheduler.confirm_directives"):
            asm.handlers["plan_next_round"](store, action)
    finally:
        asm.close()


# ---------------- 预检只转数据错误(计划 9 清理清单:preflight 把编程错误当成配置错误) ----------------

def _raises(exc):
    def load():
        raise exc
    return load


@pytest.mark.parametrize("exc", [
    RuntimeError("loader broke"), NameError("name 'x' is not defined"), ImportError("no module"),
    AssertionError("invariant"), ZeroDivisionError("division by zero"), NotImplementedError("todo"),
], ids=lambda e: type(e).__name__)
def test_preflight_lets_a_bug_in_the_loader_propagate_instead_of_exiting_2(exc):
    with pytest.raises(type(exc)) as info:
        preflight("scheduler.yaml", _raises(exc))
    assert info.value is exc and not isinstance(info.value, FactoryConfigError)


@pytest.mark.parametrize("exc", [
    ValueError("must be a bool"), SchedulerConfigError("scheduler.min_gain must be finite"), KeyError("template_version"),
    TypeError("BigRef.sha256 must be 64 lowercase hex chars"), AttributeError("'NoneType' object has no attribute 'x'"),
    OverflowError("int too large"), FileNotFoundError("scheduler.yaml"), RecursionError("maximum recursion depth exceeded"),
], ids=lambda e: type(e).__name__)
def test_preflight_still_converts_data_errors_to_a_one_line_config_error(exc):
    with pytest.raises(FactoryConfigError, match=r"^scheduler\.yaml: ") as info:
        preflight("scheduler.yaml", _raises(exc))
    assert type(exc).__name__ in str(info.value) and "\n" not in str(info.value)
    assert info.value.__suppress_context__ is True


def test_a_frozen_config_dataclass_type_error_is_still_a_config_error():
    """冻结配置(BackendConfig、BigRef 一类)构造时字段形状不对抛的 TypeError 是数据错误,照旧退出码 2 那一路。"""
    from controlplane.envelope import BigRef

    with pytest.raises(FactoryConfigError, match="InvalidBigRefError"):       # TypeError 的子类
        preflight("training_defaults.yaml", lambda: BigRef(uri="x", sha256="nope", size_bytes=0))


def test_an_unexpected_runtime_error_in_the_loader_is_not_exit_code_2(tmp_path, monkeypatch):
    """端到端:加载函数里的编程错误不再变成"factory: error: ...: RuntimeError"加退出码 2,而是带回溯上抛。"""
    store = _repo(tmp_path, run_id=None)
    import factory.handlers as handlers

    def broken(_store):
        raise RuntimeError("loader bug")

    monkeypatch.setattr(handlers, "load_training_defaults", broken)
    with pytest.raises(RuntimeError, match="loader bug"):
        main(["reconcile", str(store.repo_root), "--once", "--enable", "plan", "--planner-backend", "heuristic"], env={})
