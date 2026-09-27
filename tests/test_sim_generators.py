from simulator.datamodel import Persona, Scenario
from simulator.backends import TemplateGenerationBackend
from simulator.generators import (
    generate_persona,
    generate_scenario,
    load_persona,
    load_scenario,
)


def test_generate_persona_marks_auto_source():
    p = generate_persona(TemplateGenerationBackend(), seed=1)
    assert isinstance(p, Persona)
    assert p.source == "auto"


def test_generate_scenario_carries_purpose():
    s = generate_scenario(TemplateGenerationBackend(), seed=1, purpose="training")
    assert isinstance(s, Scenario)
    assert s.purpose == "training"
    assert s.source == "auto"


def test_load_persona_forces_external_source():
    raw = {
        "id": "pers-ext-1",
        "traits": ["理性"],
        "motivation": "解决问题",
        "speech_style": "克制",
        "values": ["真相优先"],
        "source": "auto",  # 即使外部数据写了 auto，加载路也强制标 external
    }
    p = load_persona(raw)
    assert p.id == "pers-ext-1"
    assert p.source == "external"


def test_load_scenario_forces_external_source():
    raw = {
        "id": "scen-ext-1",
        "background": "人工预置情境",
        "role_slots": [{"slot_id": "x", "description": "角色X"}],
        "conflict": "预置冲突",
        "norms": ["规范一"],
        "purpose": "deployment_simulation",
    }
    s = load_scenario(raw)
    assert s.source == "external"
    assert s.purpose == "deployment_simulation"
    assert s.role_slots[0].slot_id == "x"


def test_both_paths_produce_same_type():
    auto = generate_scenario(TemplateGenerationBackend(), seed=2, purpose="training")
    ext = load_scenario(
        {
            "id": "s2",
            "background": "b",
            "role_slots": [],
            "conflict": "c",
            "norms": [],
            "purpose": "training",
        }
    )
    assert type(auto) is type(ext) is Scenario
