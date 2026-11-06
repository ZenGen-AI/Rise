"""修补任务 110:终审发现的缺口与导入面问题(计划 9 Ruling 26)。

- 闭环模式的 run 经 run_daemon 把 handlers、evaluator、accept_mock_evidence 交给守护进程,此前没有测试钉住。
- 装配期读配置文件时,凭证值字段、"不是配置信封"、文件原文损坏曾以回溯与退出码 1 漏出,应为一行错误与退出码 2(Ruling 7)。
- 不带 --enable 的命令不导入计划 3、5、7 的包(Ruling 16)。
"""
import subprocess
import sys
from pathlib import Path

import pytest

from controlplane.envelope import Envelope, OwnerRef
from controlplane.evidence import diagnostic_grade
from controlplane.gitstore import GitStore
from controlplane.paths import backends_config_file, candidate_checkpoint_file, diagnostic_report_file
from diagnostic.paths import item_pool_file, taxonomy_file
from factory.agent import AGENT_CONFIG
from factory.cli import main
from factory.defaults import DEFAULT_AGENT_CONFIG, DEFAULT_EVALUATOR_CONFIG
from factory.evaluation import EVALUATOR_CONFIG
from factory_fixtures import (
    Recorder, approve, closed_loop_repo, git_head, held_lock, recording_builders, tree_files,
)

RUN = "run-001"
SECRET = "sekrit-value-0042"
LOOP = ["--enable", "plan,nominate,track", "--allow-mock-evidence", "--interval-s", "0"]


def test_closed_loop_run_hands_the_daemon_its_handlers_evaluator_and_evidence_switch(tmp_path):
    store = closed_loop_repo(str(tmp_path / "cp"))
    rec = Recorder()
    root = str(store.repo_root)
    # 派发、诊断、待审:报告必须出自注入的 mock 评估器,占位评估器写出来的是 stub
    assert main(["run", root, *LOOP, "--max-ticks", "3"], env={}, builders=recording_builders(rec)) == 0
    assert diagnostic_grade(store.read_product(diagnostic_report_file(RUN)).payload) == "mock"
    approve(store, RUN)
    # 晋级,再下一轮:假的提名处理器只有在 handlers 与 accept_mock_evidence 都传到时才会被调用
    assert main(["run", root, *LOOP, "--max-ticks", "2"], env={}, builders=recording_builders(rec)) == 0
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "effective"
    assert rec.calls == [("nominate_recipe", RUN)]


def test_closed_loop_run_exits_2_when_the_repository_is_locked_and_writes_nothing(tmp_path, capsys):
    store = closed_loop_repo(str(tmp_path / "cp"))
    head, files = git_head(store), tree_files(store)
    with held_lock(store.repo_root):
        code = main(["run", str(store.repo_root), *LOOP, "--max-ticks", "2"],
                    env={}, builders=recording_builders(Recorder()))
    assert code == 2
    err = capsys.readouterr().err
    assert err.startswith("factory: error: repository is locked by another process: ")
    assert "rise.lock" in err
    assert (git_head(store), tree_files(store)) == (head, files)


def _put(store, rel, type_, payload):
    """把 rel 换成一份指定类型的信封(先从工作树删掉旧文件,再经 write_product 写入并提交)。"""
    path = store.repo_root / rel
    if path.exists():
        path.unlink()
    env = Envelope(id=Path(rel).stem, type=type_, version=1, created_at="2026-10-09T00:00:00Z",
                   created_by=OwnerRef(), status="effective", schema_version=1, payload=payload)
    store.write_product(rel, env, f"test: {rel} as {type_}")


CONFIG_DEFECTS = [
    pytest.param(EVALUATOR_CONFIG, "config", {**DEFAULT_EVALUATOR_CONFIG, "token": SECRET}, "plan",
                 id="evaluator-with-a-credential-field"),
    pytest.param(EVALUATOR_CONFIG, "directive", {}, "plan", id="evaluator-that-is-not-a-config"),
    pytest.param(backends_config_file(), "config", {"agent": {"token": SECRET}}, "agent",
                 id="backends-with-a-credential-field-agent-switch"),
    pytest.param(backends_config_file(), "directive", {}, "agent",
                 id="backends-that-is-not-a-config-agent-switch"),
    pytest.param(backends_config_file(), "directive", {}, "plan",
                 id="backends-that-is-not-a-config-planner-bridge"),
    pytest.param(AGENT_CONFIG, "config", {**DEFAULT_AGENT_CONFIG, "token": SECRET}, "agent",
                 id="agent-with-a-credential-field"),
    pytest.param(AGENT_CONFIG, "directive", {}, "agent", id="agent-that-is-not-a-config"),
    pytest.param(taxonomy_file(DEFAULT_EVALUATOR_CONFIG["taxonomy_version"]), "diagnostic_taxonomy", {}, "plan",
                 id="taxonomy-with-an-empty-payload"),
    pytest.param(item_pool_file(DEFAULT_EVALUATOR_CONFIG["item_pool_version"]), "diagnostic_item_pool", {}, "plan",
                 id="item-pool-with-an-empty-payload"),
]


@pytest.mark.parametrize("rel,type_,payload,enable", CONFIG_DEFECTS)
def test_config_file_defects_found_during_assembly_exit_2_with_one_line(tmp_path, capsys, rel, type_, payload, enable):
    store = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    _put(store, rel, type_, payload)
    head, files = git_head(store), tree_files(store)
    code = main(["reconcile", str(store.repo_root), "--once", "--enable", enable], env={})
    assert code == 2
    err = capsys.readouterr().err
    assert err.startswith("factory: error: ")
    assert len(err.splitlines()) == 1
    assert SECRET not in err
    assert (git_head(store), tree_files(store)) == (head, files)


# yaml 与 jsonschema 的异常信息会引用文件内容(语法错误处的原文、整份信封),所以装配错误里只许出现路径与异常类名
RAW_DEFECTS = [
    pytest.param(EVALUATOR_CONFIG, f"id: evaluator\nbad: {SECRET}: [\n", "plan",
                 id="evaluator-with-a-yaml-syntax-error"),
    pytest.param(backends_config_file(), f"id: backends\ntype: config\nschema_version: 1\nstatus: {SECRET}\n", "agent",
                 id="backends-that-fails-the-envelope-schema-agent-switch"),
    pytest.param(AGENT_CONFIG, f"id: agent\nbad: {SECRET}: [\n", "agent",
                 id="agent-with-a-yaml-syntax-error"),
    pytest.param(taxonomy_file(DEFAULT_EVALUATOR_CONFIG["taxonomy_version"]), f"id: taxonomy\nbad: {SECRET}: [\n", "plan",
                 id="taxonomy-with-a-yaml-syntax-error"),
]


@pytest.mark.parametrize("rel,text,enable", RAW_DEFECTS)
def test_unreadable_config_files_exit_2_without_quoting_their_content(tmp_path, capsys, rel, text, enable):
    store = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    (store.repo_root / rel).write_text(text, encoding="utf-8")
    files = tree_files(store)
    code = main(["reconcile", str(store.repo_root), "--once", "--enable", enable], env={})
    assert code == 2
    err = capsys.readouterr().err
    assert err.startswith("factory: error: ")
    assert len(err.splitlines()) == 1
    assert SECRET not in err
    assert tree_files(store) == files


def test_the_default_path_does_not_load_the_plan_3_5_7_packages():
    """Ruling 16:不带 --enable 的命令(含只读子命令)不导入 recipestore、trainplanner、trainscheduler、mindagent。"""
    src = Path(__file__).resolve().parents[1] / "src"
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(src)!r})\n"
        "import factory.cli\n"
        "print(sorted(m for m in ('recipestore', 'trainplanner', 'trainscheduler', 'mindagent') if m in sys.modules))\n"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "[]"


def test_install_defaults_subcommand_prints_the_written_paths_and_can_run_again(tmp_path, capsys):
    store = GitStore(str(tmp_path / "cp"))
    store.init()
    assert main(["install-defaults", str(store.repo_root)]) == 0
    first = capsys.readouterr().out.splitlines()
    assert EVALUATOR_CONFIG in first
    files = tree_files(store)
    assert main(["install-defaults", str(store.repo_root)]) == 0
    assert capsys.readouterr().out.splitlines() == first
    assert tree_files(store) == files
