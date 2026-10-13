"""端到端框架(任务 105):带清单与默认配置的仓库,闭环模式跑通;mock 证据默认不推动闭环。"""
from controlplane.evidence import diagnostic_grade
from controlplane.paths import candidate_checkpoint_file, diagnostic_report_file, model_file, run_dir
from factory.cli import main
from factory.wire import FactoryOptions, run
from factory_fixtures import Recorder, approve, closed_loop_repo, recording_builders

RUN = "run-001"


def test_an_empty_closed_loop_runs_a_tick_without_error(tmp_path):
    store = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    rec = Recorder()
    run(str(store.repo_root), FactoryOptions(switches=("plan", "nominate", "track", "agent", "admit")),
        interval_s=0, max_ticks=1, env={}, builders=recording_builders(rec))
    assert rec.calls == []


def _to_promotion(store, argv_tail, rec):
    """指令 → 训练 → mock 诊断 → 待审 → 人工批准 → 晋级;每步都走 factory 的 reconcile --once。"""
    root = str(store.repo_root)
    for _ in range(3):
        assert main(["reconcile", root, "--once", *argv_tail], env={}, builders=recording_builders(rec)) == 0
    approve(store, RUN)
    assert main(["reconcile", root, "--once", *argv_tail], env={}, builders=recording_builders(rec)) == 0


def test_closed_loop_mode_trains_with_a_mock_graded_diagnostic(tmp_path, capsys):
    store = closed_loop_repo(str(tmp_path / "cp"))
    _to_promotion(store, ["--enable", "plan"], Recorder())
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "effective"
    assert store.exists(model_file(1))
    payload = store.read_product(diagnostic_report_file(RUN)).payload
    assert diagnostic_grade(payload) == "mock"
    out = capsys.readouterr().out.splitlines()
    assert out == [f"dispatch_directive {RUN}", f"request_diagnostic cand-{RUN}",
                   f"await_review cand-{RUN}", f"promote_candidate cand-{RUN}"]


def test_mock_evidence_does_not_turn_the_loop_unless_allowed(tmp_path, capsys):
    store = closed_loop_repo(str(tmp_path / "cp"))
    rec = Recorder()
    _to_promotion(store, ["--enable", "plan,nominate,track"], rec)
    capsys.readouterr()
    root = str(store.repo_root)
    assert main(["reconcile", root, "--once", "--enable", "plan,nominate,track"],
                env={}, builders=recording_builders(rec)) == 0
    assert capsys.readouterr().out == "no actions\n"
    assert rec.calls == []
    assert main(["reconcile", root, "--once", "--enable", "plan,nominate,track", "--allow-mock-evidence"],
                env={}, builders=recording_builders(rec)) == 0
    # 假处理器不写提名标记,所以规划还在等它(CP-11):只出现 nominate_recipe
    assert rec.calls == [("nominate_recipe", RUN)]
    assert capsys.readouterr().out == f"nominate_recipe {RUN}\n"


def test_the_recorder_handlers_write_nothing_so_the_tree_only_holds_the_round(tmp_path):
    store = closed_loop_repo(str(tmp_path / "cp"))
    _to_promotion(store, ["--enable", "plan", "--allow-mock-evidence"], Recorder())
    names = sorted(p.split("/")[-1] for p in store.list_products(run_dir(RUN)))
    assert "planner_decision.yaml" not in names and "nomination.yaml" not in names
