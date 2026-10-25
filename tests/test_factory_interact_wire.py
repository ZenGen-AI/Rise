"""`--interact` 装配(设计 90 §6.1 的 4c,十一条拒绝项)。"""
from __future__ import annotations

import pytest

from controlplane.gitstore import GitStore
from factory.cli import _options, build_parser, main
from factory.errors import FactoryConfigError
from factory.wire import FactoryOptions, assemble


@pytest.fixture
def repo(tmp_path):
    s = GitStore(str(tmp_path / "cp"))
    s.init()
    return s


def _commit_count(store) -> int:
    """空仓库安全:还没有提交时 rev-list HEAD 会抛;--all 返回 0。"""
    return int(store._git("rev-list", "--all", "--count").strip())


# ---------------- Part 1:选项与命令行 ----------------

def test_interact_defaults_to_off():
    assert FactoryOptions().interact is False
    args = build_parser().parse_args(["run", "/tmp/x"])
    assert args.interact is False


@pytest.mark.parametrize("command", [["run"], ["reconcile", "--once"]])
def test_the_flag_parses_on_run_and_reconcile_and_reaches_the_options(command):
    args = build_parser().parse_args([command[0], "/tmp/x", "--interact", *command[1:]])
    assert args.interact is True
    assert _options(args).interact is True


def test_interact_with_the_roll_executor_is_refused_at_option_level(repo):
    with pytest.raises(FactoryConfigError, match=r"--interact.*--executor roll"):
        assemble(repo, FactoryOptions(interact=True, executor="roll"))


def test_interact_with_roll_exits_2_with_one_line_and_writes_nothing(repo, capsys):
    before = _commit_count(repo)
    code = main(["run", str(repo.repo_root), "--interact", "--executor", "roll", "--max-ticks", "1"], env={})
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("factory: error:") and "--executor roll" in err
    assert len(err.strip().splitlines()) == 1
    assert _commit_count(repo) == before


def test_an_endpoint_name_is_now_allowed_with_interact_alone(repo):
    """放宽后,点名端点的判断不再在选项层挡住 --interact;后续装配步骤才读端点配置。"""
    with pytest.raises(FactoryConfigError) as caught:
        assemble(repo, FactoryOptions(endpoint="gpu-1"))
    assert "--interact" in str(caught.value)             # 报错同时点名两种允许的用法
    try:
        assemble(repo, FactoryOptions(endpoint="gpu-1", interact=True))
    except FactoryConfigError as exc:
        assert "only makes sense" not in str(exc)      # 不再是选项层的用法错误


# ---------------- Part 2:一个共用的运行时 ----------------

@pytest.fixture
def runtimes(monkeypatch):
    """记下每个被构造的 AgentRuntime(按构造次数数,而不是按持有者数)。"""
    from factory.agent import AgentRuntime

    made = []
    real_init = AgentRuntime.__init__

    def init(self, *args, **kwargs):
        made.append(self)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(AgentRuntime, "__init__", init)
    return made


@pytest.fixture
def dispatcher_kwargs(monkeypatch):
    import factory.wire as wire

    seen = []
    real = wire.make_mindtrainer_dispatcher

    def spy(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(wire, "make_mindtrainer_dispatcher", spy)
    return seen


@pytest.fixture
def handler_kwargs(monkeypatch):
    import factory.handlers as handlers

    seen = []

    def spy(**kwargs):
        seen.append(kwargs)
        return lambda store, action: None

    monkeypatch.setattr(handlers, "make_start_round_handler", spy)
    return seen


@pytest.fixture
def defaults_repo(tmp_path):
    from factory_fixtures import closed_loop_repo

    return closed_loop_repo(str(tmp_path / "cp"), run_id=None)


def test_interact_and_the_agent_switch_share_one_runtime(defaults_repo, runtimes, dispatcher_kwargs, handler_kwargs):
    asm = assemble(defaults_repo, FactoryOptions(switches=("agent",), interact=True), env={})
    try:
        assert len(runtimes) == 1
        from_dispatcher = dispatcher_kwargs[-1]["client_factory"].__self__
        from_handler = handler_kwargs[-1]["client_factory"].__self__
        assert from_dispatcher is from_handler is runtimes[0]
        assert [close.__self__ for close in asm.closers] == [runtimes[0]]      # 只登记一次收尾
    finally:
        asm.close()


def test_interact_alone_builds_one_runtime_and_a_longer_turn_timeout(defaults_repo, runtimes, dispatcher_kwargs):
    asm = assemble(defaults_repo, FactoryOptions(interact=True), env={})
    try:
        assert len(runtimes) == 1
        assert dispatcher_kwargs[-1]["client_factory"].__self__ is runtimes[0]
        assert runtimes[0]._timeout == 30.0
        assert type(dispatcher_kwargs[-1]["data_producer"]).__name__ == "TemplateDataProducer"
    finally:
        asm.close()


def test_the_agent_switch_alone_keeps_the_default_turn_timeout_and_builds_one_runtime(
        defaults_repo, runtimes, handler_kwargs):
    asm = assemble(defaults_repo, FactoryOptions(switches=("agent",)), env={})
    try:
        assert len(runtimes) == 1
        assert handler_kwargs[-1]["client_factory"].__self__ is runtimes[0]
        assert runtimes[0]._timeout == 5.0
        assert [close.__self__ for close in asm.closers] == [runtimes[0]]
    finally:
        asm.close()


def test_without_either_there_is_no_runtime(defaults_repo, runtimes):
    asm = assemble(defaults_repo, FactoryOptions(switches=("track",)), env={})
    assert runtimes == [] and asm.closers == ()


def test_the_context_carries_the_runtime_to_every_builder(defaults_repo, runtimes):
    from factory_fixtures import Recorder, recording_builders

    seen: list = []
    asm = assemble(defaults_repo, FactoryOptions(switches=("agent", "track"), interact=True), env={},
                   builders=recording_builders(Recorder(), seen_contexts=seen))
    try:
        assert seen and all(ctx.runtime is runtimes[0] for ctx in seen)
    finally:
        asm.close()


def test_the_runtime_is_closed_when_interact_runs_without_any_switch(defaults_repo, runtimes, monkeypatch):
    """--interact 不带 --enable 时没有处理器,但运行时仍要收尾:reconcile 与 run 两条路径都得关它。"""
    from factory.agent import AgentRuntime
    from factory.wire import reconcile_one_tick, run

    closed: list = []
    real_close = AgentRuntime.close
    monkeypatch.setattr(AgentRuntime, "close", lambda self: (closed.append(self), real_close(self)))
    reconcile_one_tick(str(defaults_repo.repo_root), FactoryOptions(interact=True), env={})
    assert len(runtimes) == 1 and closed == runtimes
    run(str(defaults_repo.repo_root), FactoryOptions(interact=True), max_ticks=1, interval_s=0.0, env={})
    assert len(runtimes) == 2 and closed == runtimes


def test_a_failed_handler_build_closes_the_runtime(defaults_repo, runtimes, monkeypatch):
    from factory.agent import AgentRuntime

    closed: list = []
    monkeypatch.setattr(AgentRuntime, "close", lambda self: closed.append(self))

    def boom(ctx):
        raise FactoryConfigError("builder exploded")

    with pytest.raises(FactoryConfigError, match="builder exploded"):
        assemble(defaults_repo, FactoryOptions(switches=("track",), interact=True), env={},
                 builders={"record_track": boom})
    assert closed == runtimes and len(runtimes) == 1
