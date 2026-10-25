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
