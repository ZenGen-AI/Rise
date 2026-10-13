"""组合根:不传 --enable 时与 mindtrainer.wire 逐字节相同;传了才注入评估器、数据生产器与处理器。"""
import shutil

import pytest

from controlplane.daemon import reconcile_once
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, diagnostic_report_file, run_dir
from controlplane.reconcile import CLOSED_LOOP_ACTIONS
from factory.cli import build_parser, dev_main, main
from factory.evaluation import EVALUATOR_CONFIG
from factory.wire import FactoryOptions, assemble
from factory_fixtures import (
    DATA_SPEC, Recorder, closed_loop_repo, git_head, git_log_messages, held_lock, produce_datasets,
    recording_builders, tree_files, write_directive,
)
from mindtrainer import wire as mt_wire
from mindtrainer.dispatch import make_mindtrainer_dispatcher

RUN = "run-001"


@pytest.fixture
def twin(tmp_path):
    """两棵逐字节相同的仓库(含 .git):一条 pending 指令,数据集清单已在树里,没有任何默认配置。"""
    a = GitStore(str(tmp_path / "a"))
    a.init()
    produce_datasets(a, write_directive(a, RUN))
    shutil.copytree(tmp_path / "a", tmp_path / "b")
    return a, GitStore(str(tmp_path / "b"))


def _same(a, b):
    assert tree_files(a) == tree_files(b)
    assert git_log_messages(a) == git_log_messages(b)


def test_without_enable_run_is_byte_identical_to_mindtrainer_wire(twin):
    a, b = twin
    mt_wire.run(str(a.repo_root), interval_s=0, max_ticks=3)
    assert main(["run", str(b.repo_root), "--interval-s", "0", "--max-ticks", "3"]) == 0
    assert a.exists(diagnostic_report_file(RUN))
    _same(a, b)


def test_without_enable_the_entry_scripts_match_through_promotion(twin, capsys):
    a, b = twin
    assert mt_wire.main([str(a.repo_root), "--interval-s", "0", "--max-ticks", "3"]) == 0
    assert main(["run", str(b.repo_root), "--interval-s", "0", "--max-ticks", "3"]) == 0
    for store in (a, b):
        cand = store.read_product(candidate_checkpoint_file(RUN))
        cand.status = "approved"
        store.write_product(candidate_checkpoint_file(RUN), cand, "human: approve")
    reconcile_once(a, dispatcher=make_mindtrainer_dispatcher())
    assert main(["reconcile", str(b.repo_root), "--once"]) == 0
    assert capsys.readouterr().out == f"promote_candidate cand-{RUN}\n"
    _same(a, b)


def test_without_enable_stub_diagnostics_stay_stub_and_no_config_is_read(twin):
    _, b = twin
    assert not b.exists(EVALUATOR_CONFIG)
    assert main(["run", str(b.repo_root), "--interval-s", "0", "--max-ticks", "2"]) == 0
    assert b.read_product(diagnostic_report_file(RUN)).payload["stub"] is True


def test_the_default_assembly_injects_nothing_but_the_dispatcher(twin):
    a, _ = twin
    asm = assemble(a, FactoryOptions())
    assert (asm.handlers, asm.evaluator, asm.accept_mock_evidence, asm.closed_loop) == ({}, None, False, False)


def test_allow_mock_and_confirm_flags_alone_change_nothing(twin):
    a, b = twin
    mt_wire.run(str(a.repo_root), interval_s=0, max_ticks=2)
    assert main(["run", str(b.repo_root), "--interval-s", "0", "--max-ticks", "2",
                 "--allow-mock-evidence", "--no-confirm-directives"]) == 0
    _same(a, b)


def test_without_a_data_producer_a_data_spec_directive_fails_in_default_mode(tmp_path):
    store = GitStore(str(tmp_path / "cp"))
    store.init()
    write_directive(store, RUN, data_spec=DATA_SPEC)
    assert main(["reconcile", str(store.repo_root), "--once"]) == 0
    d = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert d.status == "failed" and "data_producer" in d.payload["failure"]["reason"]


# ---- 闭环模式的装配 ----

def _closed(tmp_path, **kw):
    return closed_loop_repo(str(tmp_path / "cp"), **kw)


def test_enabled_assembly_injects_evaluator_handlers_and_data_producer(tmp_path):
    store = _closed(tmp_path, run_id=None)
    write_directive(store, RUN, data_spec=DATA_SPEC)  # 没有清单:要靠注入的数据生产器
    rec, seen = Recorder(), []
    asm = assemble(store, FactoryOptions(switches=("admit",), allow_mock_evidence=True,
                                         confirm_directives=False, planner_backend="heuristic"),
                   env={}, builders=recording_builders(rec, seen_contexts=seen))
    assert set(asm.handlers) == {"admit_recipe", "admit_proposal"}
    assert asm.evaluator is not None and asm.accept_mock_evidence is True
    assert [(c.accept_mock_evidence, c.confirm_directives, c.planner_backend) for c in seen] == \
        [(True, False, "heuristic")] * 2
    asm.dispatcher(store, store.read_product(f"{run_dir(RUN)}/directive.yaml"))
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"


def test_only_the_mapped_keys_are_built(tmp_path):
    store = _closed(tmp_path, run_id=None)
    rec = Recorder()
    for switches, keys in ((("plan",), {"plan_next_round"}), (("track", "nominate"), {"record_track", "nominate_recipe"}),
                           (("plan", "nominate", "track", "agent", "admit"), set(CLOSED_LOOP_ACTIONS))):
        asm = assemble(store, FactoryOptions(switches=switches), env={}, builders=recording_builders(rec))
        assert set(asm.handlers) == keys


def test_a_missing_builder_is_an_assembly_error(tmp_path, capsys):
    store = _closed(tmp_path, run_id=None)
    rec = Recorder()
    code = main(["reconcile", str(store.repo_root), "--once", "--enable", "plan,agent"],
                env={}, builders=recording_builders(rec, keys=["plan_next_round"]))
    assert code == 2
    assert "no handler builder for ['start_agent_round']" in capsys.readouterr().err


def test_closed_loop_mode_without_evaluator_config_exits_2_and_commits_nothing(tmp_path, capsys):
    store = GitStore(str(tmp_path / "cp"))
    store.init()
    write_directive(store, RUN)
    head = git_head(store)
    code = main(["run", str(store.repo_root), "--enable", "plan", "--max-ticks", "1"],
                env={}, builders=recording_builders(Recorder()))
    assert code == 2
    assert "evaluator.yaml is missing" in capsys.readouterr().err
    assert git_head(store) == head


def test_unknown_switch_exits_2(tmp_path, capsys):
    store = _closed(tmp_path, run_id=None)
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "plan,nominatee"]) == 2
    assert "nominatee" in capsys.readouterr().err


def test_reconcile_requires_once(tmp_path):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["reconcile", str(tmp_path)])


def test_flag_defaults():
    args = build_parser().parse_args(["run", "/r"])
    assert (args.enable, args.allow_mock_evidence, args.confirm_directives, args.planner_backend,
            args.interval_s, args.max_ticks) == (None, False, None, "bridge", 2.0, None)
    assert build_parser().parse_args(["run", "/r", "--confirm-directives"]).confirm_directives is True
    assert build_parser().parse_args(["run", "/r", "--no-confirm-directives"]).confirm_directives is False


def test_dev_entry_turns_mock_evidence_on(tmp_path):
    store = _closed(tmp_path, run_id=None)
    seen = []
    assert dev_main(["reconcile", str(store.repo_root), "--once", "--enable", "plan"],
                    env={}, builders=recording_builders(Recorder(), seen_contexts=seen)) == 0
    assert seen[0].accept_mock_evidence is True
    seen.clear()
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "plan"],
                env={}, builders=recording_builders(Recorder(), seen_contexts=seen)) == 0
    assert seen[0].accept_mock_evidence is False


# ---- 仓库锁(设计 CP-12):run 的锁由 run_daemon 取,reconcile 的锁由 reconcile_one_tick 在装配之前取 ----

def _locked_message(capsys):
    err = capsys.readouterr().err
    assert err.startswith("factory: error: repository is locked by another process: ")
    assert "rismforge.lock" in err


def test_run_exits_2_when_the_repository_is_locked_and_writes_nothing(twin, capsys):
    a, _ = twin
    head, files = git_head(a), tree_files(a)
    with held_lock(a.repo_root):
        assert main(["run", str(a.repo_root), "--interval-s", "0", "--max-ticks", "2"]) == 2
    _locked_message(capsys)
    assert (git_head(a), tree_files(a)) == (head, files)


def test_reconcile_exits_2_when_the_repository_is_locked_and_writes_nothing(twin, capsys):
    a, _ = twin
    head, files = git_head(a), tree_files(a)
    with held_lock(a.repo_root):
        assert main(["reconcile", str(a.repo_root), "--once"]) == 2
    _locked_message(capsys)
    assert (git_head(a), tree_files(a)) == (head, files)


def test_closed_loop_reconcile_takes_the_lock_before_any_builder_runs(tmp_path, capsys):
    store = closed_loop_repo(str(tmp_path / "cp"))
    head, files, seen = git_head(store), tree_files(store), []
    with held_lock(store.repo_root):
        assert main(["reconcile", str(store.repo_root), "--once", "--enable", "plan"], env={},
                    builders=recording_builders(Recorder(), seen_contexts=seen)) == 2
    _locked_message(capsys)
    assert seen == []  # 锁在装配之前取:被占用时一个处理器构造器都没跑
    assert (git_head(store), tree_files(store)) == (head, files)


def test_the_lock_is_released_when_run_and_reconcile_finish(twin):
    a, _ = twin
    assert main(["run", str(a.repo_root), "--interval-s", "0", "--max-ticks", "1"]) == 0
    assert main(["reconcile", str(a.repo_root), "--once"]) == 0
    with held_lock(a.repo_root):  # 取得到,说明前两条没把锁留在身后
        pass
