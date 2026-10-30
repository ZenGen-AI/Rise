"""树外记忆配置(设计二 5.1、5.3 节):报错只写键名、路径与变量名,不回显端点地址与密钥。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from factory.errors import FactoryConfigError
from factory.memory_config import (
    MEMORY_CONFIG_ENV, MemoryConfig, check_api_key, claim_memory_root, normalize_base_url,
    read_memory_config, starling_install_source,
)

SRC = Path(__file__).resolve().parents[1] / "src"
SECRET_URL = "https://alice:s3cret@embed.example/v1"


def _payload(tmp_path, **over):
    data = {
        "root": str(tmp_path / "mem"),
        "extraction_base_url": "https://Extract.Example/v1/",
        "extraction_model": "gpt-4o-mini",
        "embedding_base_url": "https://embed.example/v1",
        "embedding_model": "text-embedding-3-small",
        "embedding_dim": 1536,
    }
    data.update(over)
    return {k: v for k, v in data.items() if v is not ...}


def _write(tmp_path, payload, mode=0o600):
    path = tmp_path / "memory.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    os.chmod(path, mode)
    return path


def _read(tmp_path, path):
    return read_memory_config(env={MEMORY_CONFIG_ENV: str(path)}, repo_root=tmp_path / "repo")


def test_an_unset_variable_means_the_stand_in(tmp_path):
    assert read_memory_config(env={}, repo_root=tmp_path) is None
    assert read_memory_config(env={MEMORY_CONFIG_ENV: ""}, repo_root=tmp_path) is None


def test_a_clean_config_is_read_with_defaults_and_normalized_urls(tmp_path):
    cfg = _read(tmp_path, _write(tmp_path, _payload(tmp_path)))
    assert cfg == MemoryConfig(
        root=tmp_path / "mem", extraction_base_url="https://extract.example/v1",
        extraction_model="gpt-4o-mini", embedding_base_url="https://embed.example/v1",
        embedding_model="text-embedding-3-small", embedding_dim=1536, timeout_ms=4000, max_retries=0,
    )


_NORMALIZED = [
    ("HTTPS://API.Example.COM/v1/", "https://api.example.com/v1"),
    ("http://127.0.0.1:8000/v1", "http://127.0.0.1:8000/v1"),
    ("https://api.example.com", "https://api.example.com"),
    ("https://api.example.com/Path/Keeps/Case/", "https://api.example.com/Path/Keeps/Case"),
    ("http://[::1]:8000/v1", "http://[::1]:8000/v1"),
    ("HTTP://[FE80::1]/v1/", "http://[fe80::1]/v1"),
]


@pytest.mark.parametrize("raw, want", _NORMALIZED)
def test_base_urls_are_normalized(raw, want):
    out = normalize_base_url("embedding_base_url", raw)
    assert out == want
    # 规范化后的值要被再解析、再拼路径:主机(含 IPv6 的方括号)不能被拼坏
    again, original = urlsplit(out), urlsplit(raw)
    assert again.hostname is not None and again.hostname == original.hostname.lower()
    assert again.port == original.port


@pytest.mark.parametrize("raw", [SECRET_URL, "https://token@embed.example/v1", "ftp://embed.example/v1",
                                 "embed.example/v1", "https:///v1", "https://embed.example/v1?k=1", "", 3])
def test_bad_base_urls_are_refused_without_echoing_the_value(raw):
    with pytest.raises(FactoryConfigError) as info:
        normalize_base_url("embedding_base_url", raw)
    assert "embedding_base_url" in str(info.value)
    assert "s3cret" not in str(info.value) and "alice" not in str(info.value) and "token" not in str(info.value)


@pytest.mark.parametrize("change, needle", [
    ({"root": ...}, "missing ['root']"),
    ({"extra": 1}, "unknown ['extra']"),
    ({"root": "relative/mem"}, "root must be an absolute path"),
    ({"embedding_dim": 0}, "embedding_dim"),
    ({"embedding_dim": True}, "embedding_dim"),
    ({"timeout_ms": 0}, "timeout_ms"),
    ({"max_retries": -1}, "max_retries"),
    ({"extraction_model": ""}, "extraction_model"),
    ({"embedding_base_url": SECRET_URL}, "user:password"),
])
def test_bad_keys_are_refused(tmp_path, change, needle):
    with pytest.raises(FactoryConfigError) as info:
        _read(tmp_path, _write(tmp_path, _payload(tmp_path, **change)))
    assert needle in str(info.value) and "s3cret" not in str(info.value)


def test_the_file_must_be_private_absolute_present_and_outside_the_repo(tmp_path):
    good = _payload(tmp_path)
    with pytest.raises(FactoryConfigError, match="chmod 600"):
        _read(tmp_path, _write(tmp_path, good, mode=0o644))
    with pytest.raises(FactoryConfigError, match="absolute"):
        read_memory_config(env={MEMORY_CONFIG_ENV: "memory.yaml"}, repo_root=tmp_path / "repo")
    with pytest.raises(FactoryConfigError, match="does not exist"):
        _read(tmp_path, tmp_path / "missing.yaml")
    inside = tmp_path / "repo" / "memory.yaml"
    inside.parent.mkdir()
    inside.write_text(yaml.safe_dump(good), encoding="utf-8"); os.chmod(inside, 0o600)
    with pytest.raises(FactoryConfigError, match="outside the control-plane repository"):
        _read(tmp_path, inside)
    with pytest.raises(FactoryConfigError, match="root must live outside"):
        _read(tmp_path, _write(tmp_path, _payload(tmp_path, root=str(tmp_path / "repo" / "mem"))))


def test_the_api_key_must_be_non_empty_and_only_its_name_is_reported():
    check_api_key({"OPENAI_API_KEY": "sk-x"})
    for env in ({}, {"OPENAI_API_KEY": ""}, {"OPENAI_API_KEY": "  "}):
        with pytest.raises(FactoryConfigError) as info:
            check_api_key(env)
        assert str(info.value).startswith("OPENAI_API_KEY ")


def test_the_root_is_claimed_once_and_a_second_repo_is_refused_naming_both_paths(tmp_path):
    root, a, b = tmp_path / "mem", tmp_path / "repo-a", tmp_path / "repo-b"
    claim_memory_root(root, a)
    claim_memory_root(root, a)                                 # 同一仓库再来:读回相同即放行
    assert (root / ".owner").read_text(encoding="utf-8") == f"{a.resolve()}\n"
    assert (root / ".owner").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FactoryConfigError) as info:
        claim_memory_root(root, b)
    assert str(a.resolve()) in str(info.value) and str(b.resolve()) in str(info.value)


_CLAIM = """
import sys, time
from pathlib import Path
from factory.errors import FactoryConfigError
from factory.memory_config import claim_memory_root
go = Path(sys.argv[3])
while not go.exists():
    time.sleep(0.005)
try:
    claim_memory_root(Path(sys.argv[1]), Path(sys.argv[2]))
except FactoryConfigError:
    sys.exit(2)          # 与 factory.cli.main 对 FactoryConfigError 的译法相同
"""


@pytest.mark.slow
def test_two_repos_racing_for_a_fresh_root_leave_exactly_one_owner(tmp_path):
    """Review Focus:记忆根目录被另一个仓库占用。两个子进程同时首次使用,只有一个创建 .owner。"""
    root, go = tmp_path / "mem", tmp_path / "go"
    env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1"}
    procs = [subprocess.Popen([sys.executable, "-c", _CLAIM, str(root), str(tmp_path / name), str(go)], env=env)
             for name in ("repo-a", "repo-b")]
    go.touch()
    codes = sorted(p.wait(timeout=30) for p in procs)
    assert codes == [0, 2]
    owner = (root / ".owner").read_text(encoding="utf-8").strip()
    assert owner in {str((tmp_path / n).resolve()) for n in ("repo-a", "repo-b")}


class _Dist:
    def __init__(self, files):
        self.files_ = files

    def read_text(self, name):
        return self.files_.get(name)


def test_install_source_comes_from_the_git_commit_or_the_record():
    commit = "a" * 40
    git = _Dist({"direct_url.json": json.dumps({"url": "https://github.com/x/starling", "vcs_info": {"vcs": "git", "commit_id": commit}})})
    assert starling_install_source(distribution=git) == f"git:{commit}"
    record = "starling/__init__.py,sha256=AAA,10\nstarling/_core.so,sha256=BBB,99\nstarling-0.0.1.dist-info/RECORD,,\n"
    a = starling_install_source(distribution=_Dist({"direct_url.json": json.dumps({"url": "file:///src", "dir_info": {}}), "RECORD": record}))
    b = starling_install_source(distribution=_Dist({"RECORD": "\n".join(reversed(record.strip().split("\n")))}))
    assert a == b and a.startswith("record:") and len(a) == len("record:") + 64
    assert starling_install_source(distribution=_Dist({"RECORD": record.replace("BBB", "CCC")})) != a


def test_an_editable_or_unknown_install_is_refused():
    editable = _Dist({"direct_url.json": json.dumps({"url": "file:///src", "dir_info": {"editable": True}})})
    with pytest.raises(FactoryConfigError, match="editable"):
        starling_install_source(distribution=editable)
    with pytest.raises(FactoryConfigError, match="RECORD"):
        starling_install_source(distribution=_Dist({}))


def test_a_missing_starling_is_refused_with_the_install_hint(monkeypatch):
    """Review Focus:设了记忆配置但 Starling 未安装 → 装配期退出码 2,信息说明 pip install '.[memory]'。"""
    import importlib.metadata as md
    monkeypatch.setattr(md, "distribution", lambda name: (_ for _ in ()).throw(md.PackageNotFoundError(name)))
    with pytest.raises(FactoryConfigError) as info:
        starling_install_source()
    assert "pip install '.[memory]'" in str(info.value) and MEMORY_CONFIG_ENV in str(info.value)


def test_the_installed_starling_reports_a_git_commit():
    """本机真装的 Starling 是按提交装的:不传参时走 direct_url.json 的 vcs_info.commit_id。"""
    pytest.importorskip("starling")
    out = starling_install_source()
    assert out.startswith("git:") and len(out) == len("git:") + 40
    int(out[len("git:"):], 16)


@pytest.mark.parametrize("body", [
    "- https://alice:s3cret@embed.example/v1\n",                      # 不是映射
    "s3cret\n",
    "root: [unclosed s3cret\n",                                      # YAML 解析失败
    "{1: s3cret, 2: s3cret}\n",                                      # 键类型不对
])
def test_a_broken_file_is_refused_without_echoing_its_content(tmp_path, body):
    path = tmp_path / "memory.yaml"
    path.write_text(body, encoding="utf-8"); os.chmod(path, 0o600)
    with pytest.raises(FactoryConfigError) as info:
        _read(tmp_path, path)
    assert "s3cret" not in str(info.value) and "alice" not in str(info.value)


@pytest.mark.parametrize("change", [
    {"embedding_dim": "s3cret"}, {"timeout_ms": "s3cret"}, {"max_retries": ["s3cret"]},
    {"extraction_model": {"s3cret": 1}}, {"embedding_model": ["s3cret"]}, {"root": ["s3cret"]},
    {"extraction_base_url": ["s3cret"]}, {"extraction_base_url": "https://[s3cret/v1"},
    {"embedding_base_url": "https://embed.example:s3cret/v1"}, {"embedding_base_url": "https://embed.example:99999/v1"},
])
def test_wrongly_typed_values_are_refused_without_echoing_them(tmp_path, change):
    with pytest.raises(FactoryConfigError) as info:
        _read(tmp_path, _write(tmp_path, _payload(tmp_path, **change)))
    assert "s3cret" not in str(info.value)


def test_a_symlink_cannot_smuggle_the_root_or_the_file_into_the_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "inner").mkdir(parents=True)
    link_root = tmp_path / "link-root"
    link_root.symlink_to(repo / "inner")
    with pytest.raises(FactoryConfigError, match="root must live outside"):
        _read(tmp_path, _write(tmp_path, _payload(tmp_path, root=str(link_root / "mem"))))
    real = repo / "memory.yaml"
    real.write_text(yaml.safe_dump(_payload(tmp_path)), encoding="utf-8"); os.chmod(real, 0o600)
    link_file = tmp_path / "link.yaml"
    link_file.symlink_to(real)
    with pytest.raises(FactoryConfigError, match="symlink"):
        _read(tmp_path, link_file)
    # 配置文件在仓库之外,但它所在的目录是指向仓库内的符号链接:resolve 之后落在仓库里
    link_dir = tmp_path / "link-dir"
    link_dir.symlink_to(repo)
    with pytest.raises(FactoryConfigError, match="outside the control-plane repository"):
        _read(tmp_path, link_dir / "memory.yaml")


def test_a_relative_repo_path_claims_under_its_resolved_form(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path)
    claim_memory_root(tmp_path / "mem", Path("repo"))
    assert (tmp_path / "mem" / ".owner").read_text(encoding="utf-8") == f"{(tmp_path / 'repo').resolve()}\n"


def test_an_empty_owner_file_is_refused_after_the_read_back_timeout(tmp_path):
    root = tmp_path / "mem"
    root.mkdir()
    (root / ".owner").write_text("", encoding="utf-8")
    with pytest.raises(FactoryConfigError, match=r"\.owner is empty"):
        claim_memory_root(root, tmp_path / "repo", read_back_timeout_s=0.1)


def test_an_owner_file_filled_in_during_the_wait_is_honoured(tmp_path):
    import threading
    root, repo = tmp_path / "mem", tmp_path / "repo"
    root.mkdir()
    owner = root / ".owner"
    owner.write_text("", encoding="utf-8")
    timer = threading.Timer(0.1, lambda: owner.write_text(f"{repo.resolve()}\n", encoding="utf-8"))
    timer.start()
    try:
        claim_memory_root(root, repo, read_back_timeout_s=2.0)
    finally:
        timer.join()


def test_record_rows_without_a_hash_are_ignored():
    base = "starling/__init__.py,sha256=AAA,10\n"
    noisy = base + "starling/__pycache__/x.pyc,,\nstarling-0.0.1.dist-info/RECORD,,\n"
    assert starling_install_source(distribution=_Dist({"RECORD": base})) == \
        starling_install_source(distribution=_Dist({"RECORD": noisy}))
    with pytest.raises(FactoryConfigError, match="RECORD"):
        starling_install_source(distribution=_Dist({"RECORD": "starling-0.0.1.dist-info/RECORD,,\n"}))
