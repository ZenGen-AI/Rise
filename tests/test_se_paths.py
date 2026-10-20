"""策略经验的产物路径与目录登记(设计 4.1)。"""
from controlplane import paths


def test_experience_dir_is_registered_last():
    assert paths.SUBDIRS[-1] == "experience"
    assert paths.SUBDIRS.count("experience") == 1


def test_note_file_lives_in_the_run_directory():
    assert paths.strategy_experience_note_file("run-003") == (
        "control-plane/runs/run-003/strategy_experience.yaml"
    )


def test_entry_file_lives_in_the_experience_directory():
    assert paths.strategy_entry_file("exp-tom-3f9c1a2b") == (
        "control-plane/experience/exp-tom-3f9c1a2b.yaml"
    )


def test_init_creates_the_experience_directory(tmp_path):
    from controlplane.gitstore import GitStore

    GitStore(str(tmp_path)).init()
    assert (tmp_path / paths.CONTROL_PLANE_ROOT / "experience").is_dir()
