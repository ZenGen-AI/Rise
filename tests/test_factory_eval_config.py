"""树外本机评测配置:权限、路径、键的校验;报错不回显任何取值。"""
from __future__ import annotations

import os

import pytest
import yaml

from factory.errors import FactoryConfigError
from factory.eval_config import EVAL_CONFIG_ENV, EvalConfig, read_eval_config

SECRET = "sk-judge-should-never-leak"


def _payload(**over):
    data = {
        "someval_root": "/opt/SoMEval",
        "someval_python": "/opt/SoMEval/.venv/bin/python",
        "work_root": "/var/tmp/forge-eval",
        "index_path": "/var/tmp/forge-eval/sombench.idx",
        "judge_base_url": "https://judge.example/v1",
        "judge_api_key": SECRET,
    }
    data.update(over)
    return data


def _write(tmp_path, payload=None, mode=0o600):
    path = tmp_path / "eval.yaml"
    path.write_text(yaml.safe_dump(payload or _payload()), encoding="utf-8")
    os.chmod(path, mode)
    return path


def _read(tmp_path, path, *, repo_root=None):
    return read_eval_config(env={EVAL_CONFIG_ENV: str(path)},
                            repo_root=repo_root or (tmp_path / "repo"))


def test_a_clean_config_is_read(tmp_path):
    cfg = _read(tmp_path, _write(tmp_path))
    assert isinstance(cfg, EvalConfig)
    assert cfg.serve_port == 18081 and cfg.content_none_ratio_max == 0.01
    assert cfg.judge_api_key == SECRET


def test_the_repr_never_shows_the_key(tmp_path):
    assert SECRET not in repr(_read(tmp_path, _write(tmp_path)))


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_a_group_or_world_readable_file_is_refused(tmp_path, mode):
    with pytest.raises(FactoryConfigError, match="chmod 600"):
        _read(tmp_path, _write(tmp_path, mode=mode))


def test_a_file_inside_the_repository_is_refused(tmp_path):
    path = _write(tmp_path, _payload(work_root=str(tmp_path / "repo" / "work")))
    with pytest.raises(FactoryConfigError, match="outside") as caught:
        _read(tmp_path, path)
    assert SECRET not in str(caught.value)


def test_a_relative_path_is_refused(tmp_path):
    with pytest.raises(FactoryConfigError, match="absolute"):
        _read(tmp_path, _write(tmp_path, _payload(work_root="relative/dir")))


def test_an_unset_environment_variable_is_refused():
    with pytest.raises(FactoryConfigError, match=EVAL_CONFIG_ENV):
        read_eval_config(env={})


def test_a_missing_key_is_named_but_no_value_is_shown(tmp_path):
    payload = _payload()
    del payload["judge_api_key"]
    with pytest.raises(FactoryConfigError) as caught:
        _read(tmp_path, _write(tmp_path, payload))
    assert "judge_api_key" in str(caught.value)


def test_an_unknown_key_is_refused(tmp_path):
    with pytest.raises(FactoryConfigError, match="unknown"):
        _read(tmp_path, _write(tmp_path, _payload(extra="x")))


@pytest.mark.parametrize("port", [0, 70000, "8080", True])
def test_a_bad_port_is_refused(tmp_path, port):
    with pytest.raises(FactoryConfigError, match="serve_port"):
        _read(tmp_path, _write(tmp_path, _payload(serve_port=port)))


@pytest.mark.parametrize("ratio", [-0.1, 1.5, "x"])
def test_a_bad_ratio_is_refused(tmp_path, ratio):
    with pytest.raises(FactoryConfigError, match="content_none_ratio_max"):
        _read(tmp_path, _write(tmp_path, _payload(content_none_ratio_max=ratio)))


def test_a_symlink_is_refused(tmp_path):
    real = _write(tmp_path)
    link = tmp_path / "link.yaml"
    link.symlink_to(real)
    with pytest.raises(FactoryConfigError, match="symlink"):
        _read(tmp_path, link)


@pytest.mark.parametrize("extra", ["x: 2024-13-45\n", "2024-02-31: x\n"], ids=["bad-date-value", "bad-date-key"])
def test_a_yaml_date_the_loader_cannot_build_is_a_config_error_not_a_crash(tmp_path, extra):
    """PyYAML 的 timestamp 构造器抛 ValueError(不是 YAMLError);与 memory_config 同口径转成 FactoryConfigError。"""
    path = _write(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")
    with pytest.raises(FactoryConfigError) as info:
        _read(tmp_path, path)
    msg = str(info.value)
    assert "cannot be read as YAML" in msg and "ValueError" in msg
    assert "13-45" not in msg and "02-31" not in msg and SECRET not in msg
