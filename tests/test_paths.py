from controlplane import paths


def test_run_dir():
    assert paths.run_dir("run-001") == "control-plane/runs/run-001"


def test_model_file():
    assert paths.model_file(3) == "control-plane/models/zengen-v3.yaml"


def test_backends_config_file():
    assert paths.backends_config_file() == "control-plane/config/backends.yaml"


def test_subdirs_match_spec():
    assert paths.SUBDIRS == ["runs", "recipes", "skill_memory_proposals", "models", "compute", "config", "scenarios", "personas"]
