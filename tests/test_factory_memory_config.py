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
    read_memory_config, client_install_commit,
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
                                 "embed.example/v1", "https:///v1", "https://embed.example/v1?k=1", "", 3,
                                 "https://embed.example:0/v1", "http://[::1]:0/v1"])
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


@pytest.mark.parametrize("timeout_ms, max_retries", [(4000, 0), (2000, 1), (1000, 3)])
def test_a_memory_call_budget_up_to_four_seconds_is_accepted(tmp_path, timeout_ms, max_retries):
    cfg = _read(tmp_path, _write(tmp_path, _payload(tmp_path, timeout_ms=timeout_ms, max_retries=max_retries)))
    assert (cfg.timeout_ms, cfg.max_retries) == (timeout_ms, max_retries)


@pytest.mark.parametrize("change", [
    {"timeout_ms": 4001}, {"timeout_ms": 2001, "max_retries": 1}, {"timeout_ms": 10000},
    {"max_retries": 1},                                    # 默认 timeout_ms=4000 加一次重试即 8 秒
])
def test_a_memory_call_budget_over_four_seconds_is_refused(tmp_path, change):
    """外层时限(部署 40 秒、对手 25 秒、--interact 90 秒)按每次记忆网络调用至多 4 秒折算;超出就不再是内层先超时。"""
    with pytest.raises(FactoryConfigError) as info:
        _read(tmp_path, _write(tmp_path, _payload(tmp_path, **change)))
    msg = str(info.value)
    assert "timeout_ms" in msg and "max_retries" in msg and "4000" in msg
    if "timeout_ms" in change:
        assert str(change["timeout_ms"]) not in msg                  # 只写键名与上限,不回显取值


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


def _git(commit="a" * 40, **extra):
    return _Dist({"direct_url.json": json.dumps({"url": "https://github.com/x/starling",
                                                 "vcs_info": {"vcs": "git", "commit_id": commit}, **extra})})


def test_the_install_commit_comes_from_the_git_vcs_info():
    assert client_install_commit("starling-client", distribution=_git()) == "a" * 40


@pytest.mark.parametrize("dist, needle", [
    (_Dist({"direct_url.json": json.dumps({"url": "file:///src", "dir_info": {"editable": True}})}), "editable"),
    (_Dist({"RECORD": "starling_client/__init__.py,sha256=x,1\n"}), "git commit"),           # record: 不再接受
    (_Dist({"direct_url.json": json.dumps({"url": "file:///src", "dir_info": {}})}), "git commit"),
    (_Dist({}), "git commit"),
    (_git(""), "git commit"),
])
def test_an_editable_or_non_git_install_is_refused(dist, needle):
    with pytest.raises(FactoryConfigError, match=needle) as info:
        client_install_commit("starling-client", distribution=dist)
    assert "starling-client" in str(info.value)


def test_a_missing_client_is_refused_with_the_install_hint(monkeypatch):
    """Review Focus:设了记忆配置但客户端未安装 → 装配期退出码 2,信息说明 pip install '.[memory]'。"""
    import importlib.metadata as md
    monkeypatch.setattr(md, "distribution", lambda name: (_ for _ in ()).throw(md.PackageNotFoundError(name)))
    with pytest.raises(FactoryConfigError) as info:
        client_install_commit("starling-client")
    assert "pip install '.[memory]'" in str(info.value) and MEMORY_CONFIG_ENV in str(info.value)


def test_the_installed_client_and_server_report_the_same_git_commit():
    """本机真装的两个包按同一提交装:不传 distribution 时走 direct_url.json 的 vcs_info.commit_id。"""
    pytest.importorskip("starling_client")
    out = client_install_commit("starling-client")
    assert len(out) == 40 and int(out, 16) >= 0
    pytest.importorskip("starling.service")
    assert client_install_commit("starling-memory") == out


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
    with pytest.raises(FactoryConfigError, match=r"\.owner is empty") as info:
        claim_memory_root(root, tmp_path / "repo", read_back_timeout_s=0.1)
    assert str(root / ".owner") in str(info.value) and "delete" in str(info.value)


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


_PAIRS = "root: {root}\nextraction_base_url: https://extract.example/v1\nextraction_model: m\n" \
         "embedding_base_url: https://embed.example/v1\nembedding_dim: 8\n"


@pytest.mark.parametrize("extra", [
    "embedding_model: 2024-13-45\n",          # 值是解析不出的日期
    "embedding_model: m\n2024-13-45: x\n",    # 键是解析不出的日期
    "embedding_model: 2024-02-31\n",
], ids=["bad-date-value", "bad-date-key", "bad-day-of-month"])
def test_a_yaml_scalar_the_loader_cannot_build_is_a_config_error_not_a_crash(tmp_path, extra):
    """PyYAML 的 timestamp 构造器抛 ValueError(不是 YAMLError);不转成 FactoryConfigError 就会以退出码 1 崩出。"""
    path = tmp_path / "memory.yaml"
    path.write_text(_PAIRS.format(root=tmp_path / "mem") + extra, encoding="utf-8"); os.chmod(path, 0o600)
    with pytest.raises(FactoryConfigError) as info:
        _read(tmp_path, path)
    assert "cannot be read as YAML" in str(info.value) and "ValueError" in str(info.value)
    assert "13-45" not in str(info.value) and "02-31" not in str(info.value)


def test_root_must_not_contain_the_control_plane_repository(tmp_path):
    """记忆根目录只属于一个仓库,启动清扫会删 <root>/runs 下的所有子目录:根不能是仓库的上级。"""
    (tmp_path / "repo").mkdir()
    for root in (tmp_path, tmp_path.parent, Path(tmp_path.anchor)):
        with pytest.raises(FactoryConfigError, match="root must not contain the control-plane repository"):
            _read(tmp_path, _write(tmp_path, _payload(tmp_path, root=str(root))))
    # 经符号链接指到上级也一样
    link = tmp_path.parent / f"{tmp_path.name}-up"
    link.symlink_to(tmp_path)
    try:
        with pytest.raises(FactoryConfigError, match="root must not contain"):
            _read(tmp_path, _write(tmp_path, _payload(tmp_path, root=str(link))))
    finally:
        link.unlink()
    # 与仓库同级、名字只是前缀相同的目录是允许的
    assert _read(tmp_path, _write(tmp_path, _payload(tmp_path, root=str(tmp_path / "repo-mem")))).root == tmp_path / "repo-mem"


def _leftovers(root):
    return sorted(p.name for p in root.iterdir())


def test_claiming_leaves_no_temporary_files_behind(tmp_path):
    root, a, b = tmp_path / "mem", tmp_path / "repo-a", tmp_path / "repo-b"
    claim_memory_root(root, a)
    assert _leftovers(root) == [".owner"]
    with pytest.raises(FactoryConfigError):
        claim_memory_root(root, b)
    assert _leftovers(root) == [".owner"]


@pytest.mark.parametrize("boom, raised", [(OSError(5, "io error"), FactoryConfigError), (KeyboardInterrupt(), KeyboardInterrupt)],
                         ids=["oserror", "interrupt"])
def test_a_failure_before_publishing_leaves_no_owner_and_no_temporary_file(tmp_path, monkeypatch, boom, raised):
    """.owner 只会不存在或内容完整:fsync 前被打断(含 Ctrl-C)不留 0 字节的 .owner,也不留临时文件。"""
    root = tmp_path / "mem"

    def fail(fd):
        raise boom

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail)
        with pytest.raises(raised):
            claim_memory_root(root, tmp_path / "repo")
    assert _leftovers(root) == []
    claim_memory_root(root, tmp_path / "repo")                 # 之后照常能占
    assert _leftovers(root) == [".owner"]


def test_a_filesystem_without_hard_links_is_a_config_error(tmp_path, monkeypatch):
    root = tmp_path / "mem"

    def no_link(src, dst, **kw):
        raise OSError(1, "operation not permitted")

    monkeypatch.setattr(os, "link", no_link)
    with pytest.raises(FactoryConfigError, match=r"cannot write \.owner \(PermissionError\)"):
        claim_memory_root(root, tmp_path / "repo")
    assert _leftovers(root) == []


def test_an_empty_owner_deleted_by_hand_while_waiting_is_replaced(tmp_path):
    import threading
    root, repo = tmp_path / "mem", tmp_path / "repo"
    root.mkdir()
    owner = root / ".owner"
    owner.write_text("", encoding="utf-8")
    timer = threading.Timer(0.1, owner.unlink)
    timer.start()
    try:
        claim_memory_root(root, repo, read_back_timeout_s=2.0)
    finally:
        timer.join()
    assert owner.read_text(encoding="utf-8") == f"{repo.resolve()}\n"
    assert _leftovers(root) == [".owner"]


# ---------------- 接记忆服务后的三个键(设计四 Task 9;90 篇配置键表、第 20 条) ----------------

def _token_file(tmp_path, mode=0o600, name="dash.token"):
    path = tmp_path / name
    path.write_text("tok-s3cret\n", encoding="utf-8")
    os.chmod(path, mode)
    return path


def _external(tmp_path, **over):
    """外部模式的配置:只有 root、url、token_file(与可选的 world_prefix)。"""
    data = {"root": str(tmp_path / "mem"), "url": "http://127.0.0.1:8787", "token_file": str(_token_file(tmp_path))}
    data.update(over)
    return {k: v for k, v in data.items() if v is not ...}


def test_both_service_keys_mean_external_mode_and_neither_means_spawn(tmp_path):
    token = _token_file(tmp_path)
    cfg = _read(tmp_path, _write(tmp_path, _external(tmp_path, url="http://127.0.0.1:8787/", token_file=str(token))))
    assert (cfg.mode, cfg.url, cfg.token_file, cfg.world_prefix) == ("external", "http://127.0.0.1:8787", token, "rise")
    assert (cfg.extraction_base_url, cfg.extraction_model, cfg.embedding_base_url, cfg.embedding_model,
            cfg.embedding_dim, cfg.timeout_ms, cfg.max_retries) == (None,) * 7      # 端点跟 Starling 走
    spawn = _read(tmp_path, _write(tmp_path, _payload(tmp_path, world_prefix="lab_2")))
    assert (spawn.mode, spawn.url, spawn.token_file, spawn.world_prefix) == ("spawn", None, None, "lab_2")


@pytest.mark.parametrize("key", ["extraction_base_url", "extraction_model", "embedding_base_url", "embedding_model",
                                 "embedding_dim", "timeout_ms", "max_retries"])
def test_external_mode_refuses_every_endpoint_and_call_key_naming_only_the_key(tmp_path, key):
    """外部模式下抽取、嵌入的端点、模型、超时、重试都取自 Starling 默认世界:配置里出现任一个都拒绝,只报键名。"""
    secret = "https://alice:s3cret@embed.example/v1"
    with pytest.raises(FactoryConfigError) as info:
        _read(tmp_path, _write(tmp_path, _external(tmp_path, **{key: secret})))
    msg = str(info.value)
    assert key in msg and "default world" in msg and "s3cret" not in msg and "alice" not in msg


def test_external_mode_needs_only_root_url_and_token_file(tmp_path):
    msg = _refused_external(tmp_path, root=...)
    assert "missing ['root']" in msg
    msg = _refused_external(tmp_path, extra=1)
    assert "unknown ['extra']" in msg
    cfg = _read(tmp_path, _write(tmp_path, _external(tmp_path, world_prefix="lab_2")))
    assert cfg.world_prefix == "lab_2"


def test_external_mode_skips_the_four_second_spawn_check(tmp_path):
    """4 秒那条只管拉起模式:外部模式没有 timeout_ms 与 max_retries,也就没有可查的。"""
    cfg = _read(tmp_path, _write(tmp_path, _external(tmp_path)))
    assert cfg.mode == "external" and cfg.timeout_ms is None and cfg.max_retries is None


def _refused_external(tmp_path, **change):
    with pytest.raises(FactoryConfigError) as info:
        _read(tmp_path, _write(tmp_path, _external(tmp_path, **change)))
    return str(info.value)


@pytest.mark.parametrize("url", ["http://localhost:8787", "http://127.0.0.2:8787", "http://[::1]:8787"])
def test_every_loopback_host_is_accepted(tmp_path, url):
    cfg = _read(tmp_path, _write(tmp_path, _external(tmp_path, url=url)))
    assert cfg.mode == "external"


def _refused(tmp_path, **change):
    """服务键的错误:给了 url 或 token_file 就按外部模式写(不带端点键),否则按拉起模式。"""
    if {"url", "token_file"} & set(change):
        payload = {k: v for k, v in {"root": str(tmp_path / "mem"), **change}.items() if v is not ...}
    else:
        payload = _payload(tmp_path, **change)
    with pytest.raises(FactoryConfigError) as info:
        _read(tmp_path, _write(tmp_path, payload))
    return str(info.value)


@pytest.mark.parametrize("change, needle", [
    ({"url": "http://127.0.0.1:8787"}, "url and token_file"),                              # 只给一个
    ({"token_file": "TOKEN"}, "url and token_file"),
    ({"url": "http://10.0.0.1:8787", "token_file": "TOKEN"}, "loopback"),                  # 不是回环
    ({"url": "https://127.0.0.1:8787", "token_file": "TOKEN"}, "loopback"),                # https
    ({"url": "http://alice:s3cret@127.0.0.1:8787", "token_file": "TOKEN"}, "user:password"),  # userinfo
    ({"url": "http://dash.example:8787", "token_file": "TOKEN"}, "loopback"),
    ({"url": "http://127.0.0.1:8787", "token_file": "relative.token"}, "token_file must be an absolute path"),
    ({"world_prefix": "ri.se"}, "world_prefix"),                                           # 含 `.`
    ({"world_prefix": "-rise"}, "world_prefix"),
    ({"world_prefix": "r" * 17}, "world_prefix"),
])
def test_bad_service_keys_are_refused_without_echoing_the_value(tmp_path, change, needle):
    token = _token_file(tmp_path)
    change = {k: (str(token) if v == "TOKEN" else v) for k, v in change.items()}
    msg = _refused(tmp_path, **change)
    assert needle in msg
    for value in change.values():
        assert str(value) not in msg
    assert "s3cret" not in msg and "10.0.0.1" not in msg and str(tmp_path) not in msg


def test_a_token_file_too_open_inside_the_repo_or_missing_is_refused_naming_only_the_key(tmp_path):
    url = "http://127.0.0.1:8787"
    loose = _token_file(tmp_path, mode=0o644)
    msg = _refused(tmp_path, url=url, token_file=str(loose))
    assert "token_file" in msg and "chmod 600" in msg and str(loose) not in msg
    (tmp_path / "repo").mkdir()
    inside = _token_file(tmp_path / "repo")
    msg = _refused(tmp_path, url=url, token_file=str(inside))
    assert "token_file must live outside" in msg and str(inside) not in msg
    msg = _refused(tmp_path, url=url, token_file=str(tmp_path / "gone.token"))
    assert "token_file does not exist" in msg and "gone.token" not in msg
    link = tmp_path / "link.token"
    link.symlink_to(_token_file(tmp_path, name="target.token"))
    msg = _refused(tmp_path, url=url, token_file=str(link))
    assert "token_file must point at a regular file, not a symlink" in msg
    msg = _refused(tmp_path, url=url, token_file=str(tmp_path))
    assert "token_file must point at a regular file" in msg


def test_the_token_file_content_is_never_read_by_the_config_reader(tmp_path):
    cfg = _read(tmp_path, _write(tmp_path, _external(tmp_path)))
    assert "tok-s3cret" not in repr(cfg)
