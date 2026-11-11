"""端到端框架(任务 105):带清单与默认配置的仓库,闭环模式跑通;mock 证据默认不推动闭环。"""
import pytest

from controlplane.evidence import diagnostic_grade
from controlplane.paths import candidate_checkpoint_file, diagnostic_report_file, model_file, run_dir
from factory.cli import main
from factory.wire import FactoryOptions, assemble, run
from factory_fixtures import (
    Recorder, approve, closed_loop_repo, produce_datasets, recording_builders, write_directive,
)

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


# ---- 任务 105 的测试缺口:recorder 用例分不出调用、空闭环用例分不出开关、plan 与 track 开关没证明生效、晋级后没断言 recorder 调用 ----

def test_the_recorder_handlers_are_not_called_before_promotion_and_the_tree_only_holds_the_round(tmp_path):
    """补任务 105 的缺口:现有的 recorder 用例只断言两个文件不存在,recorder 一次没被调用也能过。
    这里加两条:晋级前的三个 tick 里 recorder 没有被调用(只有 dispatcher 与评估器在动),
    晋级后它被调用的恰是 plan_next_round,目标是这条 run,而树里仍没有它会写的文件。"""
    store = closed_loop_repo(str(tmp_path / "cp"))
    rec = Recorder()
    root = str(store.repo_root)
    tail = ["--enable", "plan", "--allow-mock-evidence"]
    for _ in range(3):
        assert main(["reconcile", root, "--once", *tail], env={}, builders=recording_builders(rec)) == 0
    assert rec.calls == []
    approve(store, RUN)
    assert main(["reconcile", root, "--once", *tail], env={}, builders=recording_builders(rec)) == 0
    assert rec.calls == []                                      # 晋级这个 tick 本身还没规划
    assert main(["reconcile", root, "--once", *tail], env={}, builders=recording_builders(rec)) == 0
    assert rec.calls == [("plan_next_round", RUN)]
    names = sorted(p.split("/")[-1] for p in store.list_products(run_dir(RUN)))
    assert "planner_decision.yaml" not in names


@pytest.mark.parametrize("switches,keys", [
    (("plan",), ["plan_next_round"]),
    (("track",), ["record_track"]),
    (("nominate", "track"), ["nominate_recipe", "record_track"]),
    (("plan", "nominate", "track", "agent", "admit"),
     ["admit_proposal", "admit_recipe", "nominate_recipe", "plan_next_round", "record_track", "start_agent_round"]),
], ids=["plan", "track", "nominate-track", "all"])
def test_each_switch_reaches_the_builders_it_names_even_on_an_empty_loop(tmp_path, switches, keys):
    """补任务 105 的缺口:空闭环用例分不出开关有没有传到。这里经 run 的真路径记下哪些处理器键的构造器被调用,
    再核对装配结果里的处理器键:传了哪个开关就恰好构造哪几个键,tick 里没有任何动作也看得出来。"""
    store = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    seen = []
    rec = Recorder()
    run(str(store.repo_root), FactoryOptions(switches=switches), interval_s=0, max_ticks=1, env={},
        builders=recording_builders(rec, seen_contexts=seen))
    assert len(seen) == len(keys) and rec.calls == []
    asm = assemble(store, FactoryOptions(switches=switches), env={}, builders=recording_builders(rec))
    assert sorted(asm.handlers) == keys


def test_the_track_switch_fires_record_track_for_a_promoted_run_with_a_recipe(tmp_path, capsys):
    """补任务 105 的缺口:track 开关在这里没有被证明生效。指令带 recipe_ref 时,晋级后下一个 tick 触发 record_track,
    recorder 记下 (record_track, run);只开 plan 时 recorder 记下的是 plan_next_round,没有 record_track。"""
    def repo(name):
        store = closed_loop_repo(str(tmp_path / name), run_id=None)
        env = write_directive(store, RUN)
        env.payload["recipe_ref"] = "recipe-x"
        store.write_product(f"{run_dir(RUN)}/directive.yaml", env, "human: anchor a recipe")
        produce_datasets(store, env)
        return store

    def tick(store, rec, enable):
        assert main(["reconcile", str(store.repo_root), "--once", "--enable", enable, "--allow-mock-evidence"],
                    env={}, builders=recording_builders(rec)) == 0
        return capsys.readouterr().out.splitlines()

    # plan 开着时晋级后的下一个 tick 会规划,所以反例不比"没有任何调用",只比 record_track 没出现
    for enable, expected in (("track", [("record_track", RUN)]), ("plan", [("plan_next_round", RUN)])):
        store, rec = repo(f"cp-{enable}"), Recorder()
        for _ in range(3):
            tick(store, rec, enable)
        approve(store, RUN)
        assert tick(store, rec, enable) == [f"promote_candidate cand-{RUN}"]
        lines = tick(store, rec, enable)
        assert rec.calls == expected and lines == [f"{kind} {target}" for kind, target in expected]


def test_the_promotion_tick_never_calls_the_recorder_and_the_next_one_does_exactly_once(tmp_path):
    """补任务 105 的缺口:晋级用例没断言 recorder 的调用。晋级的 tick 不调,晋级后的那个 tick 恰好调一次(目标是这条 run),
    再往后不重复(recorder 不写标记,但同一 tick 内的动作列表只含一次)。"""
    store = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    env = write_directive(store, RUN)
    env.payload["recipe_ref"] = "recipe-x"
    store.write_product(f"{run_dir(RUN)}/directive.yaml", env, "human: anchor a recipe")
    produce_datasets(store, env)
    rec = Recorder()
    argv = ["reconcile", str(store.repo_root), "--once", "--enable", "track", "--allow-mock-evidence"]
    for _ in range(3):
        assert main(argv, env={}, builders=recording_builders(rec)) == 0
    approve(store, RUN)
    assert main(argv, env={}, builders=recording_builders(rec)) == 0
    assert rec.calls == []                                            # 晋级的 tick
    assert main(argv, env={}, builders=recording_builders(rec)) == 0
    assert rec.calls == [("record_track", RUN)]                       # 晋级之后的 tick:恰好一次
