"""组合根:不传 --enable 时与 mindtrainer.wire 逐字节相同;传了才注入评估器、数据生产器与处理器。"""
import shutil

import pytest

from controlplane.daemon import reconcile_once
from controlplane.gitstore import GitStore
from controlplane.lock import RepoLockedError
from controlplane.paths import candidate_checkpoint_file, diagnostic_report_file, run_dir
from controlplane.reconcile import CLOSED_LOOP_ACTIONS
from factory.cli import build_parser, dev_main, main
from factory.evaluation import EVALUATOR_CONFIG
from factory.errors import FactoryConfigError
from factory.wire import Assembly, BuildContext, FactoryOptions, assemble, resolve_repo_commit
from factory.wire import run as wire_run
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


def _assembly(**kw):
    return Assembly(dispatcher=None, handlers={}, evaluator=None,
                    accept_mock_evidence=False, **kw)


def test_a_failing_closer_does_not_skip_the_others():
    """收尾逆序跑完:中间一个抛异常,排在它前面的仍要被调用。"""
    called = []

    def ok(name):
        return lambda: called.append(name)

    def boom():
        raise RuntimeError("close exploded")

    asm = _assembly(closers=(ok("first"), boom, ok("third")))
    with pytest.raises(RuntimeError, match="closer"):
        asm.close()
    assert called == ["third", "first"]       # 逆序,且失败的那个没有拦住其余


def test_the_closer_failure_message_names_the_underlying_error():
    asm = _assembly(closers=(lambda: (_ for _ in ()).throw(ValueError("bad")),))
    with pytest.raises(RuntimeError) as info:
        asm.close()
    assert "ValueError" in str(info.value) and "bad" in str(info.value)


def test_build_context_repr_does_not_leak_credentials():
    """装配上下文会被日志与异常带出去:env 里的凭证不能出现在 repr 里。"""
    ctx = BuildContext(
        store=object(), accept_mock_evidence=True, confirm_directives=None,
        planner_backend="heuristic",
        env={"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-secret-value"},
        log_dir=None,
    )
    shown = repr(ctx)
    assert "sk-secret-value" not in shown
    assert "ANTHROPIC_API_KEY" not in shown          # 连变量名也不用出现
    assert ctx.env["ANTHROPIC_API_KEY"] == "sk-secret-value"   # 值本身仍可用


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


def test_the_dispatcher_gets_the_code_version_the_options_carry(twin, monkeypatch):
    """装配方是唯一知道代码版本的地方(派发器不得导入 factory 自己查)。--repo-commit 覆盖要一路传到
    派发器,再由它写进训练记录的 code_versions。这里钉的是默认档(不传 --enable 的那条)。"""
    import factory.wire as wire

    seen = []
    real = wire.make_mindtrainer_dispatcher

    def spy(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(wire, "make_mindtrainer_dispatcher", spy)
    a, _ = twin
    assemble(a, FactoryOptions(repo_commit="d" * 40))
    assemble(a, FactoryOptions())
    # 不给覆盖时取的是代码仓库的 HEAD:非空(None 只在不是 git 仓库时出现)
    assert [kw["repo_commit"] for kw in seen] == ["d" * 40, resolve_repo_commit(None)]
    assert seen[1]["repo_commit"] is not None


def test_an_enabled_assembly_also_hands_the_code_version_to_the_dispatcher(tmp_path, monkeypatch):
    import factory.wire as wire

    seen = []
    real = wire.make_mindtrainer_dispatcher

    def spy(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(wire, "make_mindtrainer_dispatcher", spy)
    assemble(_closed(tmp_path, run_id=None), FactoryOptions(switches=("track",), repo_commit="d" * 40),
             env={}, builders={"record_track": lambda ctx: (lambda store, action: None)})
    assert [kw["repo_commit"] for kw in seen] == ["d" * 40]


def test_the_roll_assembly_also_hands_the_code_version_to_the_dispatcher(real_repo, tmp_path, monkeypatch):
    import factory.wire as wire

    seen = []
    real = wire.make_mindtrainer_dispatcher

    def spy(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(wire, "make_mindtrainer_dispatcher", spy)
    _write_endpoints(real_repo, "gpu-1")
    assemble(real_repo, FactoryOptions(executor="roll", repo_commit="e" * 40),
             env=_hosts(tmp_path, "gpu-1: gpu-181\n") | _eval_env(tmp_path))
    assert seen[-1]["repo_commit"] == "e" * 40


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


# ---- 仓库锁(设计 CP-12):run 与 reconcile 的锁都在装配之前取(wire.run、reconcile_one_tick) ----

def _locked_message(capsys):
    err = capsys.readouterr().err
    assert err.startswith("factory: error: repository is locked by another process: ")
    assert "rise.lock" in err


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


def test_closed_loop_run_takes_the_lock_before_any_builder_runs(tmp_path, capsys):
    """run 与 reconcile 一样先取锁再装配:另一个守护进程占着树时,处理器构造器一个都不跑,树里不写东西。"""
    store = closed_loop_repo(str(tmp_path / "cp"))
    head, files, seen = git_head(store), tree_files(store), []
    builders = recording_builders(Recorder(), seen_contexts=seen)
    with held_lock(store.repo_root):
        with pytest.raises(RepoLockedError, match="rise.lock"):
            wire_run(str(store.repo_root), FactoryOptions(switches=("plan",)), max_ticks=1, env={}, builders=builders)
        assert main(["run", str(store.repo_root), "--max-ticks", "1", "--enable", "plan"], env={},
                    builders=builders) == 2
    _locked_message(capsys)
    assert seen == []  # 锁在装配之前取:被占用时一个处理器构造器都没跑
    assert (git_head(store), tree_files(store)) == (head, files)


def test_run_releases_the_lock_when_assembly_fails(twin):
    a, _ = twin
    with pytest.raises(FactoryConfigError, match="planner backend"):
        wire_run(str(a.repo_root), FactoryOptions(planner_backend="nope"), max_ticks=1, env={})
    with held_lock(a.repo_root):  # 取得到,说明装配失败后锁已放掉
        pass


def test_the_lock_is_released_when_run_and_reconcile_finish(twin):
    a, _ = twin
    assert main(["run", str(a.repo_root), "--interval-s", "0", "--max-ticks", "1"]) == 0
    assert main(["reconcile", str(a.repo_root), "--once"]) == 0
    with held_lock(a.repo_root):  # 取得到,说明前两条没把锁留在身后
        pass


# ---------------- --executor roll(设计 FC-15) ----------------

_ENDPOINT = {
    "endpoint_id": "gpu-1", "work_root": "/srv/rise/work", "roll_root": "/srv/roll",
    "launch_prefix": [], "base_model_path": "/srv/models/qwen-4b",
    "heldout_jsonl": "/srv/roll/data/heldout.jsonl", "sft_template": "/srv/roll/configs/sft_lora.yaml",
    "min_free_host_mem_gib": 12.0, "min_free_disk_gib": 50.0,
    "poll_seconds": 10.0, "job_timeout_seconds": 3600.0, "host_deps_root": "/srv/deps",
}


def _write_endpoints(store, *ids):
    from compute.endpoint import ComputeEndpoint  # noqa: F401  (确认包可导入)
    from controlplane.envelope import Envelope, OwnerRef
    from controlplane.paths import compute_endpoints_file

    env = Envelope(
        id="cfg-compute-endpoints", type="config", version=1, created_at="2026-10-17T00:00:00Z",
        created_by=OwnerRef(), status="effective", schema_version=1,
        payload={"endpoints": [{**_ENDPOINT, "endpoint_id": i} for i in ids]},
    )
    store.write_product(compute_endpoints_file(), env, "test: endpoints")


def _hosts(tmp_path, text):
    path = tmp_path / "hosts.yaml"
    path.write_text(text, encoding="utf-8")
    return {"RISE_COMPUTE_HOSTS": str(path)}


def _eval_env(tmp_path):
    """真训练路径现在还要本机评测配置(污染索引与 SoMBench 数据集)。

    SoMBench 的哈希在资产版本里,而这里不导入真实数据集,所以版本由同一个桩数据集算出:
    写一个小 parquet 替身,装配期算出的哈希与它一致。"""
    import os

    from diagnostic.contamination import build_index
    from factory.sombench import file_sha256, sombench_parquet

    root = tmp_path / "SoMEval"
    parquet = sombench_parquet(root)
    parquet.parent.mkdir(parents=True, exist_ok=True)
    parquet.write_bytes(b"stub parquet for the assembly tests")
    digest = file_sha256(parquet)
    index = tmp_path / "sombench.idx"
    index.write_bytes(build_index(["教学情境里的普通一句话，与评测无关。"], digest))
    config = tmp_path / "eval.yaml"
    config.write_text(
        f"someval_root: {root}\n"
        f"someval_python: {root}/.venv/bin/python\n"
        f"work_root: {tmp_path / 'work'}\n"
        f"index_path: {index}\n"
        "judge_base_url: https://judge.example/v1\n"
        "judge_api_key: sk-test\n",
        encoding="utf-8",
    )
    os.chmod(config, 0o600)
    return {"RISE_EVAL_CONFIG": str(config)}


@pytest.fixture
def real_repo(tmp_path):
    s = GitStore(str(tmp_path / "cp"))
    s.init()
    return s


def _commit_count(store):
    """空仓库安全:还没有提交时 rev-list HEAD 会抛;--all 返回 0。"""
    return int(store._git("rev-list", "--all", "--count").strip())


def test_executor_defaults_to_mock_and_changes_nothing(twin):
    """不传 --executor:装配结果与改动前逐字节相同,不读端点配置。"""
    a, _ = twin
    asm = assemble(a, FactoryOptions())
    assert asm.handlers == {} and asm.evaluator is None


def test_roll_assembles_a_dispatcher_whose_factory_builds_a_roll_executor(real_repo, tmp_path):
    from mindtrainer.roll_executor import RollExecutor

    _write_endpoints(real_repo, "gpu-1")
    asm = assemble(real_repo, FactoryOptions(executor="roll"), env=_hosts(tmp_path, "gpu-1: gpu-181\n") | _eval_env(tmp_path))
    assert asm.executor_factory is not None
    built = asm.executor_factory("run-007")
    assert isinstance(built, RollExecutor)
    assert built.run_id == "run-007"  # 指令 id 一路传到作业身份


def test_roll_pairs_the_executor_with_the_adapter_converter(real_repo, tmp_path):
    from mindtrainer.roll_converter import RollLoraConverter

    _write_endpoints(real_repo, "gpu-1")
    asm = assemble(real_repo, FactoryOptions(executor="roll"), env=_hosts(tmp_path, "gpu-1: gpu-181\n") | _eval_env(tmp_path))
    assert isinstance(asm.converter, RollLoraConverter)


def test_roll_picks_the_named_endpoint(real_repo, tmp_path):
    _write_endpoints(real_repo, "gpu-1", "gpu-2")
    env = _hosts(tmp_path, "gpu-1: gpu-181\ngpu-2: gpu-182\n") | _eval_env(tmp_path)
    asm = assemble(real_repo, FactoryOptions(executor="roll", endpoint="gpu-2"), env=env)
    assert asm.executor_factory("r").endpoint.endpoint_id == "gpu-2"


@pytest.mark.parametrize("what", ["no_endpoints_file", "no_hosts_env", "ambiguous", "unknown_name", "no_host_entry"])
def test_roll_misconfiguration_fails_at_assembly_not_at_the_first_tick(real_repo, tmp_path, what):
    """设计 4a:这些全在第一个 tick 之前报 FactoryConfigError,树里一个字节都不写。"""
    from factory.errors import FactoryConfigError

    env = _hosts(tmp_path, "gpu-1: gpu-181\n") | _eval_env(tmp_path)
    opts = FactoryOptions(executor="roll")
    if what == "no_endpoints_file":
        pass
    elif what == "no_hosts_env":
        _write_endpoints(real_repo, "gpu-1")
        env = {}
    elif what == "ambiguous":
        _write_endpoints(real_repo, "gpu-1", "gpu-2")
        env = _hosts(tmp_path, "gpu-1: a\ngpu-2: b\n")
    elif what == "unknown_name":
        _write_endpoints(real_repo, "gpu-1")
        opts = FactoryOptions(executor="roll", endpoint="gpu-9")
    elif what == "no_host_entry":
        _write_endpoints(real_repo, "gpu-1")
        env = _hosts(tmp_path, "other: somewhere\n")
    before = _commit_count(real_repo)
    with pytest.raises(FactoryConfigError):
        assemble(real_repo, opts, env=env)
    assert _commit_count(real_repo) == before  # 树里一个字节都没写


def test_roll_needs_host_deps_root_and_says_so_at_assembly(real_repo, tmp_path):
    """容器要只读挂 Megatron-LM 与 fla,它们在 host_deps_root 下;缺了就在第一个 tick 之前报错。
    不开 --executor roll 的端点(interact、SoMEval)不写这个键也照常读。"""
    from controlplane.envelope import Envelope, OwnerRef
    from controlplane.paths import compute_endpoints_file
    from factory.errors import FactoryConfigError

    item = {k: v for k, v in _ENDPOINT.items() if k != "host_deps_root"}
    real_repo.write_product(compute_endpoints_file(), Envelope(
        id="cfg-compute-endpoints", type="config", version=1, created_at="2026-10-17T00:00:00Z",
        created_by=OwnerRef(), status="effective", schema_version=1, payload={"endpoints": [item]}), "endpoints")
    with pytest.raises(FactoryConfigError, match="host_deps_root"):
        assemble(real_repo, FactoryOptions(executor="roll"), env=_hosts(tmp_path, "gpu-1: gpu-181\n") | _eval_env(tmp_path))


def test_roll_defers_the_deployment_to_the_first_dispatch(real_repo, tmp_path, monkeypatch):
    """装配期不碰远端:部署与建容器发生在执行器第一次 preflight;确定性失败判 T2(InvalidTrainingConfigError),
    执行器与转换器拿到的是同一份派生端点。"""
    import factory.wire as wire
    from compute.errors import ComputeError
    from mindtrainer.training_config import InvalidTrainingConfigError

    calls = []

    class FakeCache:
        def __init__(self, result):
            self.result = result

        def get(self):
            calls.append("get")
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    _write_endpoints(real_repo, "gpu-1")
    env = _hosts(tmp_path, "gpu-1: gpu-181\n") | _eval_env(tmp_path)

    monkeypatch.setattr(wire, "_roll_runtime_cache", lambda *a: FakeCache(ComputeError("image is not on the host")))
    asm = assemble(real_repo, FactoryOptions(executor="roll"), env=env)
    assert calls == []
    with pytest.raises(InvalidTrainingConfigError, match="image is not on the host"):
        asm.executor_factory("run-1").preflight()

    from compute.endpoint import ComputeEndpoint
    from compute.roll_deploy import RollRuntime
    from compute.roll_env import RollEnv
    derived = ComputeEndpoint(**{**_ENDPOINT, "launch_prefix": ["docker", "exec", "-i", "forge-roll-x"],
                                 "roll_root": "/srv/rise/work/deps/roll-8b94504ef476"})
    runtime = RollRuntime(derived, RollEnv(roll_commit="8b94504ef476373a05ce6b7196134b2d78cb0c42",
                                           roll_tree="0" * 64), "forge-roll-x")
    # 绑定先于预检:预检走的是绑上去的客户端,所以它只要有 preflight 就够(不连 ssh)
    bound_client = type("BoundClient", (), {"preflight": lambda self: None})()
    monkeypatch.setattr(wire, "_roll_runtime_cache", lambda *a: FakeCache((runtime, bound_client)))
    asm = assemble(real_repo, FactoryOptions(executor="roll"), env=env)
    executor = asm.executor_factory("run-2")
    executor.preflight()
    executor.preflight()
    assert calls == ["get", "get", "get"]          # 失败那次不缓存;成功后第二次 preflight 不再部署
    assert executor.endpoint.roll_root == "/srv/rise/work/deps/roll-8b94504ef476"
    assert asm.converter.endpoint is executor.endpoint and asm.converter.client is bound_client
    assert executor.backend_record["roll_commit"] == "8b94504ef476373a05ce6b7196134b2d78cb0c42"


def test_roll_and_a_closed_loop_switch_are_mutually_exclusive(real_repo, tmp_path):
    """设计保证 7:真训练同步占住循环数小时,闭环处理器的排队语义是按 mock 训练写的。"""
    from factory.errors import FactoryConfigError

    _write_endpoints(real_repo, "gpu-1")
    with pytest.raises(FactoryConfigError, match=r"--executor roll.*--enable"):
        assemble(real_repo, FactoryOptions(executor="roll", switches=("plan",)),
                 env=_hosts(tmp_path, "gpu-1: gpu-181\n") | _eval_env(tmp_path))


def test_an_endpoint_name_without_roll_is_a_usage_error(real_repo):
    from factory.errors import FactoryConfigError

    with pytest.raises(FactoryConfigError, match=r"--endpoint.*--executor roll"):
        assemble(real_repo, FactoryOptions(endpoint="gpu-1"))


def test_an_unknown_executor_is_refused(real_repo):
    from factory.errors import FactoryConfigError

    with pytest.raises(FactoryConfigError, match=r"executor must be one of"):
        assemble(real_repo, FactoryOptions(executor="gpu-magic"))


# ---------------- 命令行 ----------------

def test_the_parser_defaults_to_the_mock_executor():
    args = build_parser().parse_args(["run", "/tmp/x"])
    assert args.executor == "mock" and args.endpoint is None


def test_the_parser_accepts_roll_and_an_endpoint():
    args = build_parser().parse_args(["run", "/tmp/x", "--executor", "roll", "--endpoint", "gpu-1"])
    assert args.executor == "roll" and args.endpoint == "gpu-1"


def test_the_parser_rejects_an_unknown_executor(capsys):
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["run", "/tmp/x", "--executor", "gpu-magic"])
    assert caught.value.code == 2


def test_roll_without_endpoints_config_exits_2_with_one_line_and_writes_nothing(real_repo, capsys):
    """端到端:--executor roll 但树里没有 endpoints.yaml。一行错误、退出码 2、树里不写东西。"""
    before = _commit_count(real_repo)
    code = main(["run", str(real_repo.repo_root), "--executor", "roll", "--max-ticks", "1"], env={})
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("factory: error:") and "endpoints.yaml is missing" in err
    assert len(err.strip().splitlines()) == 1
    assert _commit_count(real_repo) == before


def test_roll_with_enable_exits_2(real_repo, tmp_path, capsys):
    _write_endpoints(real_repo, "gpu-1")
    code = main(["run", str(real_repo.repo_root), "--executor", "roll", "--enable", "plan", "--max-ticks", "1"],
                env=_hosts(tmp_path, "gpu-1: gpu-181\n") | _eval_env(tmp_path))
    assert code == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_roll_with_an_unbuildable_date_in_the_eval_config_exits_2(real_repo, tmp_path, capsys):
    """评估配置里有 2024-13-45:PyYAML 抛 ValueError,也要译成一行配置错误、退出码 2,而不是 traceback。"""
    _write_endpoints(real_repo, "gpu-1")
    env = _hosts(tmp_path, "gpu-1: gpu-181\n") | _eval_env(tmp_path)
    config = tmp_path / "eval.yaml"
    config.write_text(config.read_text(encoding="utf-8") + "x: 2024-13-45\n", encoding="utf-8")
    before = _commit_count(real_repo)
    code = main(["run", str(real_repo.repo_root), "--executor", "roll", "--max-ticks", "1"], env=env)
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("factory: error:") and "cannot be read as YAML" in err and "13-45" not in err
    assert _commit_count(real_repo) == before


def test_the_assembled_dispatcher_calls_its_factory_with_the_directive_id(real_repo, tmp_path, monkeypatch):
    """装配出的派发器不是摆设:它按指令 id 调装配时造的工厂,造出的是点名端点上的真实执行器。

    不伪造整套远端文件系统(真实执行器的 load 会先碰远端,够不着)。让指令在 load 处失败即可,
    这里只验证装配接对了线,所以直接用 asm.dispatcher,从外面观察 RollExecutor 的构造。"""
    from controlplane.daemon import reconcile_once
    from mindtrainer import roll_executor

    _write_endpoints(real_repo, "gpu-1")
    asm = assemble(real_repo, FactoryOptions(executor="roll"), env=_hosts(tmp_path, "gpu-1: gpu-181\n") | _eval_env(tmp_path))
    write_directive(real_repo, RUN)
    produce_datasets(real_repo, real_repo.read_product(f"{run_dir(RUN)}/directive.yaml"))

    seen = []
    original_init = roll_executor.RollExecutor.__init__

    def spy(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        seen.append((self.run_id, self.endpoint.endpoint_id))

    monkeypatch.setattr(roll_executor.RollExecutor, "__init__", spy)
    try:
        reconcile_once(real_repo, dispatcher=asm.dispatcher)
    except Exception:  # noqa: BLE001  load 碰不到远端而失败;怎么失败不是这条测试的事
        pass
    assert seen == [(RUN, "gpu-1")]


def test_the_default_executor_does_not_read_the_endpoint_config(twin):
    """不传 --executor:与改动前逐字节相同,树里没有 endpoints.yaml 也照常跑。"""
    a, b = twin
    mt_wire.run(str(a.repo_root), interval_s=0, max_ticks=3)
    assert main(["run", str(b.repo_root), "--interval-s", "0", "--max-ticks", "3"]) == 0
    _same(a, b)
