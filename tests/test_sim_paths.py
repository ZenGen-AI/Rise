from controlplane.paths import (
    SUBDIRS,
    scenario_file,
    persona_file,
    episodes_dir,
    episode_file,
)


def test_subdirs_include_template_pools():
    assert "scenarios" in SUBDIRS
    assert "personas" in SUBDIRS


def test_scenario_file_path():
    assert scenario_file("scen-001") == "control-plane/scenarios/scen-001.yaml"


def test_persona_file_path():
    assert persona_file("pers-001") == "control-plane/personas/pers-001.yaml"


def test_episodes_dir_under_run():
    assert episodes_dir("run-001") == "control-plane/runs/run-001/episodes"


def test_episode_file_path():
    assert (
        episode_file("run-001", "ep-001")
        == "control-plane/runs/run-001/episodes/ep-001.yaml"
    )
