import ast
from pathlib import Path

import pytest

from simple_actio.skills import Skill
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import library_state_file, memory_file, skill_file
from mindagent.config import AgentConfig, agent_slot_of
from mindagent.errors import MindAgentDataError
from mindagent.library import (
    LibraryState,
    check_memory_library_empty,
    load_library,
    memory_id_for,
    read_library_versions,
    skill_id_for,
)
from simulator.datamodel import RoleBinding

SRC = Path(__file__).resolve().parents[1] / "src"
AGENT = RoleBinding("a", "pa", "self_play")
OPP = RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _env(id_, type_, status, payload):
    return Envelope(id=id_, type=type_, version=1, created_at="2026-10-09T00:00:00Z",
                    created_by=OwnerRef(), status=status, schema_version=1, payload=payload)


def _skill(sid, **kw):
    d = {"id": sid, "version": 1, "level": "micro", "trigger": {"signal": "emotion_high"},
         "policy": {"instructions": ["x"]}}
    d.update(kw)
    return d


def _memory(mid):
    return {"id": mid, "version": 1, "subject": "pb", "content": "对方住在北京",
            "timestamp": "2026-10-01T00:00:00Z", "confidence": 0.8}


def test_config_defaults_and_agent_slot():
    cfg = AgentConfig(scenario_ids=("s1",), bindings=(OPP, AGENT))
    assert cfg.agent_slot == "a"
    assert (cfg.max_turns, cfg.episodes_per_scenario, cfg.max_retries_per_episode) == (6, 1, 1)
    assert (cfg.min_support, cfg.min_ratio) == (3, 0.5)
    assert (cfg.min_fired_on_motivating, cfg.max_fired_elsewhere, cfg.max_parse_retries) == (0.8, 0.2, 2)
    with pytest.raises(AttributeError):
        cfg.min_support = 1


@pytest.mark.parametrize("kw,match", [
    ({"scenario_ids": ()}, "scenario_ids"),
    ({"scenario_ids": ("s1", "s1")}, "must not repeat"),
    ({"bindings": (OPP,)}, "exactly one self_play"),
    ({"bindings": (AGENT, RoleBinding("c", "pc", "self_play"))}, "exactly one self_play"),
    ({"max_turns": 0}, "max_turns"),
    ({"episodes_per_scenario": 0}, "episodes_per_scenario"),
    ({"max_retries_per_episode": -1}, "max_retries_per_episode"),
    ({"min_support": 0}, "min_support"),
    ({"min_support": True}, "min_support"),
    ({"min_ratio": 1.5}, "min_ratio"),
    ({"max_fired_elsewhere": -0.1}, "max_fired_elsewhere"),
    ({"max_parse_retries": -1}, "max_parse_retries"),
])
def test_config_rejects_nonsense(kw, match):
    base = {"scenario_ids": ("s1",), "bindings": (OPP, AGENT)}
    base.update(kw)
    with pytest.raises(ValueError, match=match):
        AgentConfig(**base)


def test_agent_slot_of_needs_exactly_one_self_play():
    assert agent_slot_of([AGENT, OPP]) == "a"
    with pytest.raises(ValueError):
        agent_slot_of([OPP])


def test_an_empty_tree_is_an_empty_library_at_version_zero(store):
    lib = load_library(store)
    assert lib == LibraryState(skills=(), version={"skill": "0", "memory": "0"})
    assert read_library_versions(store) == {"skill_version": "0", "memory_version": "0", "admitted": []}


def test_load_library_reads_effective_entries_sorted_and_the_versions(store):
    store.write_product(skill_file("skill-b"), _env("skill-b", "skill", "effective", _skill("skill-b")), "m")
    store.write_product(skill_file("skill-a"), _env("skill-a", "skill", "effective", _skill("skill-a")), "m")
    store.write_product(skill_file("skill-old"), _env("skill-old", "skill", "deprecated", _skill("skill-old")), "m")
    # memory_library/ 里的 effective 条目 load_library 也不读(设计二起记忆在树外的部署库)
    store.write_product(memory_file("mem-a"), _env("mem-a", "memory", "effective", _memory("mem-a")), "m")
    store.write_product(library_state_file(), _env("library-state", "library_state", "executed",
                        {"skill_version": "3", "memory_version": "1", "admitted": []}), "m")
    lib = load_library(store)
    assert [s.id for s in lib.skills] == ["skill-a", "skill-b"]
    assert isinstance(lib.skills[0], Skill)
    assert not hasattr(lib, "memories")
    assert lib.version == {"skill": "3", "memory": "1"}


def test_integer_versions_written_by_hand_are_normalized_to_strings(store):
    store.write_product(library_state_file(), _env("library-state", "library_state", "executed",
                        {"skill_version": 2, "memory_version": 0, "admitted": []}), "m")
    assert load_library(store).version == {"skill": "2", "memory": "0"}


@pytest.mark.parametrize("bad", ["lib-3", "", "-1", "3.0", None, True, -1, "３"])
def test_a_non_decimal_library_version_is_a_data_error(store, bad):
    store.write_product(library_state_file(), _env("library-state", "library_state", "executed",
                        {"skill_version": bad, "memory_version": "0", "admitted": []}), "m")
    with pytest.raises(MindAgentDataError, match="skill_version must be a decimal string"):
        read_library_versions(store)
    with pytest.raises(MindAgentDataError):
        load_library(store)


def test_entry_ids_are_derived_from_the_proposal_id():
    assert skill_id_for("proposal-agent-round-zengen-v3-1", 1) == "skill-agent-round-zengen-v3-1-1"
    assert memory_id_for("proposal-agent-round-zengen-v3-1", 2) == "mem-agent-round-zengen-v3-1-2"
    with pytest.raises(ValueError):
        skill_id_for("agent-round-zengen-v3-1", 1)
    with pytest.raises(ValueError):
        skill_id_for("proposal-", 1)


def test_every_mindagent_source_starts_with_future_annotations_and_controlplane_does_not_import_it():
    for path in sorted((SRC / "mindagent").glob("*.py")):
        body = ast.parse(path.read_text(encoding="utf-8")).body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        assert isinstance(body[0], ast.ImportFrom) and body[0].module == "__future__", path.name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mods = {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        mods |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not mods & {"mindtrainer", "diagnostic", "recipestore", "trainplanner", "trainscheduler", "factory"}, path.name
    for path in sorted((SRC / "controlplane").glob("*.py")):
        assert "mindagent" not in path.read_text(encoding="utf-8"), path.name


@pytest.mark.parametrize("which, text", [
    ("skill", "<<<<<<< HEAD\nid: skill-a\n"),                       # 合并冲突标记
    ("skill", "id: skill-a\npayload: [1, 2]\n"),                     # 信封校验失败
], ids=["skill-conflict", "skill-bad-envelope"])
def test_an_unreadable_library_file_is_a_mindagent_data_error_naming_it(store, which, text):
    """库文件不在调和循环的视图里,CP-17 的暂停规则管不到它;轮次处理器只接 MindAgentDataError
    与 SimpleActioDataError(轮次 failed),别的异常会从处理器漏出去打崩守护进程。"""
    rel = skill_file("skill-a")
    path = store.repo_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(MindAgentDataError, match=rel.rsplit("/", 1)[-1]):
        load_library(store)


def test_load_library_no_longer_reads_the_memory_library(store):
    store.write_product(memory_file("mem-a"), _env("mem-a", "memory", "effective", _memory("mem-a")), "m")
    lib = load_library(store)
    assert not hasattr(lib, "memories")
    assert lib.version == {"skill": "0", "memory": "0"}


def test_a_non_empty_memory_library_is_a_data_error_naming_it(store):
    check_memory_library_empty(store)          # 空目录或不存在:通过
    store.write_product(memory_file("mem-a"), _env("mem-a", "memory", "effective", _memory("mem-a")), "m")
    with pytest.raises(MindAgentDataError, match=r"memory_library/ must be empty.*mem-a\.yaml"):
        check_memory_library_empty(store)
