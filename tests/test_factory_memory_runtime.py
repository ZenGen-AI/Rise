"""组合根的替身记忆解析器(设计二 7.1 节):--interact 装配时注入,先于策略上下文解出起点记忆。"""
from __future__ import annotations

import pytest

from actio.memory import TupleMemory
from controlplane.daemon import DirectiveFailedError
from factory.memory_runtime import STAND_IN_MEMORY_RUNTIME, stand_in_memory_resolver
from memory_fixtures import STAND_IN_RUNTIME  # Task 24 的测试夹具先于本模块存在,这里钉住两份相等


def test_the_stand_in_runtime_identity_is_the_one_the_design_fixes():
    assert STAND_IN_MEMORY_RUNTIME == STAND_IN_RUNTIME
    assert STAND_IN_MEMORY_RUNTIME == {
        "backend": "stand-in", "starling_version": None, "extraction": None, "embedding": None,
        "trace_schema": "actio.trace.v2",
    }


def test_every_persona_starts_from_the_empty_stand_in_library():
    digests, runtime = stand_in_memory_resolver(None, ("pa", "pb"))
    assert digests == {"pa": TupleMemory().digest(), "pb": TupleMemory().digest()}
    assert runtime == STAND_IN_MEMORY_RUNTIME


def test_the_returned_identity_is_a_copy():
    _, runtime = stand_in_memory_resolver(None, ("pa",))
    runtime["backend"] = "x"
    assert STAND_IN_MEMORY_RUNTIME["backend"] == "stand-in"


def test_a_seed_needs_a_memory_config():
    with pytest.raises(DirectiveFailedError, match="needs a memory config") as info:
        stand_in_memory_resolver("seed-1", ("pa",))
    assert "RISMFORGE_MEMORY_CONFIG" in info.value.reason and info.value.category == "T2"


# ---------------- Starling 那一路:运行时身份、目录布局、起点摘要解析器(设计二 5.2、5.3、7.1 节) ----------------

import copy
import json
import subprocess
import sys
from pathlib import Path

from actio.memory_port import MemoryFailureError
from factory.memory_config import MemoryConfig
from factory.memory_runtime import StarlingLayout, starling_memory_runtime
from mindtrainer.training_config import MEMORY_RUNTIME_KEYS

SRC = Path(__file__).resolve().parents[1] / "src"


def _cfg(tmp_path, **over):
    base = dict(root=tmp_path / "mem", extraction_base_url="https://extract.example/v1", extraction_model="m-x",
                embedding_base_url="https://embed.example/v1", embedding_model="m-e", embedding_dim=256,
                timeout_ms=4000, max_retries=0)
    base.update(over)
    return MemoryConfig(**base)


def test_the_starling_runtime_identity_has_the_five_keys_and_no_secrets(tmp_path):
    rt = starling_memory_runtime(_cfg(tmp_path), version="0.0.1", install_source="git:" + "a" * 40, prompts_digest="p" * 64)
    assert set(rt) == set(STAND_IN_MEMORY_RUNTIME)
    assert sorted(rt) == list(MEMORY_RUNTIME_KEYS)                          # 派发器的严格校验要的就是这五个键
    assert rt == {
        "backend": "starling",
        "starling_version": "0.0.1+git:" + "a" * 40,
        "extraction": {"base_url": "https://extract.example/v1", "model": "m-x", "timeout_ms": 4000,
                       "max_retries": 0, "prompts": "p" * 64},
        "embedding": {"base_url": "https://embed.example/v1", "model": "m-e", "dim": 256,
                      "timeout_ms": 4000, "max_retries": 0},
        "trace_schema": "actio.trace.v2",
    }
    assert "root" not in repr(rt) and str(tmp_path) not in repr(rt)        # 根目录路径不进哈希
    assert json.loads(json.dumps(rt)) == rt                                 # 进 config_hash,必须能序列化


def test_moving_the_memory_root_does_not_change_the_identity(tmp_path):
    """换机器、换目录续训必须放行:根目录是位置,不是实验的一部分。"""
    kw = dict(version="0.0.1", install_source="git:x", prompts_digest="p")
    here = starling_memory_runtime(_cfg(tmp_path, root=tmp_path / "here"), **kw)
    there = starling_memory_runtime(_cfg(tmp_path, root=Path("/another/machine/mem")), **kw)
    assert here == there


def test_every_call_returns_a_fresh_identity(tmp_path):
    a = starling_memory_runtime(_cfg(tmp_path), version="0.0.1", install_source="git:x", prompts_digest="p")
    a["extraction"]["model"] = "changed"
    b = starling_memory_runtime(_cfg(tmp_path), version="0.0.1", install_source="git:x", prompts_digest="p")
    assert b["extraction"]["model"] == "m-x"


@pytest.mark.parametrize("change", [
    {"embedding_model": "m-e2"}, {"embedding_dim": 512}, {"embedding_base_url": "https://other.example/v1"},
    {"extraction_model": "m-x2"}, {"extraction_base_url": "https://other.example/v1"},
    {"timeout_ms": 3000}, {"max_retries": 1},
])
def test_any_endpoint_change_changes_the_identity(tmp_path, change):
    a = starling_memory_runtime(_cfg(tmp_path), version="0.0.1", install_source="git:x", prompts_digest="p")
    b = starling_memory_runtime(_cfg(tmp_path, **change), version="0.0.1", install_source="git:x", prompts_digest="p")
    assert a != b


@pytest.mark.parametrize("change", [
    {"version": "0.0.2"}, {"install_source": "git:y"}, {"install_source": "record:x"}, {"prompts_digest": "q"},
])
def test_a_different_starling_build_or_prompt_set_changes_the_identity(tmp_path, change):
    """`__version__` 区分不了两个提交,所以安装来源也进身份;抽取提示随 Starling 升级变,身份跟着变。"""
    kw = dict(version="0.0.1", install_source="git:x", prompts_digest="p")
    a = starling_memory_runtime(_cfg(tmp_path), **kw)
    b = starling_memory_runtime(_cfg(tmp_path), **{**kw, **change})
    assert a != b


@pytest.mark.parametrize("bad", [".", "..", "a/b", "", "x" * 129, "a\n", "../deploy"])
def test_layout_refuses_unsafe_names_in_every_position(tmp_path, bad):
    lay = StarlingLayout(tmp_path)
    for call in (lambda: lay.rollout_world("run-1", bad), lambda: lay.rollout_world(bad, "ep-1")):
        with pytest.raises(ValueError):
            call()


@pytest.mark.parametrize("bad", ["", ".hidden", "a/b", "..", "p" * 129, "pa\n", None, 7])
def test_layout_refuses_unsafe_persona_and_scenario_ids(tmp_path, bad):
    with pytest.raises(ValueError):
        StarlingLayout(tmp_path).seed("s1", bad)
    with pytest.raises(ValueError):
        StarlingLayout(tmp_path).scenario_seed("s1", bad)


def test_layout_refuses_an_unsafe_seed_id_as_a_readable_t2(tmp_path):
    for bad in ("..", "a/b", "../deploy", "", "x" * 129, None):
        for call in (lambda: StarlingLayout(tmp_path).seed(bad, "pa"),
                     lambda: StarlingLayout(tmp_path).scenario_seed(bad, "s1")):
            with pytest.raises(DirectiveFailedError, match="memory.seed") as info:
                call()
            assert info.value.category == "T2"
            assert str(tmp_path) not in info.value.reason                  # 原因里没有根目录的绝对路径


def test_layout_paths_follow_section_5_3(tmp_path):
    lay = StarlingLayout(tmp_path)
    assert lay.seed("s1", "pa") == tmp_path / "seeds" / "s1" / "pa.db"
    assert lay.scenario_seed("s1", "t1") == tmp_path / "seeds" / "s1" / "t1.db"
    assert lay.rollout_world("run-1", "run-1-grpo0-s1-r0") == tmp_path / "runs" / "run-1" / "run-1-grpo0-s1-r0.db"
    assert lay.deploy_dir() == tmp_path / "deploy"
    assert lay.runs_dir() == tmp_path / "runs"
    assert lay.seed_dir("s1") == tmp_path / "seeds" / "s1"


def test_importing_the_module_does_not_need_starling():
    """没装 Starling 的环境只跑替身路径:`import factory.memory_runtime` 不能依赖它,身份与布局照常可用;
    建解析器才要它,所以缺依赖在装配期就响,不拖到派发时。子进程里把 starling 置成 None,装没装都一样。"""
    code = (
        "import sys\n"
        "sys.modules['starling'] = None\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "from pathlib import Path\n"
        "from factory.memory_config import MemoryConfig\n"
        "from factory.memory_runtime import StandInTrainingWorlds, StarlingLayout, StarlingTrainingWorlds\n"
        "from factory.memory_runtime import starling_memory_resolver, starling_memory_runtime\n"
        "assert 'actio.memory_starling' not in sys.modules\n"
        "assert StarlingLayout(Path('/m')).seed('s1', 'pa') == Path('/m/seeds/s1/pa.db')\n"
        "assert StarlingLayout(Path('/m')).rollout_world('r', 'e') == Path('/m/runs/r/e.db')\n"
        "w = StandInTrainingWorlds(); w.bind('r', None)\n"
        "w.close_rollout('e', [w.open_rollout('r', 's1', 'e', 'pa')], wait_s=1.0)\n"
        "cfg = MemoryConfig(root=Path('/m'), extraction_base_url='https://e.example/v1', extraction_model='x',\n"
        "                   embedding_base_url='https://b.example/v1', embedding_model='y', embedding_dim=8)\n"
        "assert starling_memory_runtime(cfg, version='1', install_source='git:x', prompts_digest='p')['backend'] == 'starling'\n"
        "try:\n"
        "    starling_memory_resolver(StarlingLayout(Path('/m')), {})\n"
        "except ImportError:\n"
        "    print('ok')\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False, timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    assert out.stdout.strip() == "ok"


starling_only = pytest.mark.skipif(__import__("importlib.util").util.find_spec("starling") is None,
                                   reason="Starling 未安装")


@starling_only
def test_the_resolver_digests_seed_libraries_and_empty_starts(tmp_path):
    from actio.memory_starling import StarlingDeploymentStore, empty_digest
    from factory.memory_runtime import starling_memory_resolver
    from starling_fakes import FakeOpenAI, offline_api_key
    lay = StarlingLayout(tmp_path)
    with FakeOpenAI(dim=256) as fake, offline_api_key():
        seeds = StarlingDeploymentStore(tmp_path / "seeds" / "s1", endpoints=fake.endpoints())
        seeds.remember_event("pa", "pa", "seed#0@pa", "pb：我担心预算超支", interlocutor="pb")
        seeded = seeds.digest("pa")
        before = (tmp_path / "seeds" / "s1" / "pa.db").read_bytes()
        resolve = starling_memory_resolver(lay, {"backend": "starling"})
        digests, rt = resolve("s1", ("pa", "pb"))
        assert digests == {"pa": seeded, "pb": empty_digest()}    # 种子里没有的人格从空库起
        assert seeded != empty_digest()                           # 上一句有分辨力:种子库不是空库
        assert rt == {"backend": "starling"} and rt is not resolve("s1", ("pa",))[1]
        assert (tmp_path / "seeds" / "s1" / "pa.db").read_bytes() == before   # 种子只读
        assert sorted(p.name for p in (tmp_path / "seeds" / "s1").iterdir()) == ["pa.db"]   # 旁边没留 -wal、-shm
        assert resolve(None, ("pa",)) == ({"pa": empty_digest()}, {"backend": "starling"})


@starling_only
def test_the_resolver_returns_a_copy_of_the_identity_it_was_built_with(tmp_path):
    from factory.memory_runtime import starling_memory_resolver
    runtime = {"backend": "starling", "extraction": {"model": "m"}}
    resolve = starling_memory_resolver(StarlingLayout(tmp_path), runtime)
    runtime["extraction"]["model"] = "changed-after-the-fact"            # 造完解析器再改原字典,不影响解出的身份
    first = resolve(None, ("pa",))[1]
    first["extraction"]["model"] = "changed-by-the-caller"               # 调用方改返回值,也不影响下一次
    assert resolve(None, ("pa",))[1] == {"backend": "starling", "extraction": {"model": "m"}}


@starling_only
def test_a_missing_or_unsafe_seed_is_a_readable_t2(tmp_path):
    from factory.memory_runtime import starling_memory_resolver
    resolve = starling_memory_resolver(StarlingLayout(tmp_path), {"backend": "starling"})
    with pytest.raises(DirectiveFailedError, match="memory.seed 'nope' has no directory") as info:
        resolve("nope", ("pa",))
    assert info.value.category == "T2" and str(tmp_path) not in info.value.reason
    with pytest.raises(DirectiveFailedError, match="memory.seed"):
        resolve("../deploy", ("pa",))
    (tmp_path / "seeds").mkdir()
    (tmp_path / "seeds" / "afile").write_text("x")                       # 种子位置上是个文件,不是目录
    with pytest.raises(DirectiveFailedError, match="memory.seed 'afile' has no directory"):
        resolve("afile", ("pa",))


@starling_only
def test_a_seed_library_that_cannot_be_read_is_never_taken_for_an_empty_start(tmp_path):
    """库文件有问题(悬空链接、0 字节、不是库)时,不能静默当成"该人格从空库起":那会让一个点名了种子的实验
    悄悄从空记忆开始。异常不接,计划 A 的 _resolve_memory 只报类名、判 T2。"""
    from factory.memory_runtime import starling_memory_resolver
    seed = tmp_path / "seeds" / "s1"
    seed.mkdir(parents=True)
    resolve = starling_memory_resolver(StarlingLayout(tmp_path), {"backend": "starling"})
    (seed / "pa.db").symlink_to(tmp_path / "gone.db")                     # 悬空
    (seed / "pb.db").write_bytes(b"")                                     # 0 字节
    (seed / "pc.db").write_bytes(b"not a sqlite file at all")
    for persona in ("pa", "pb", "pc"):
        with pytest.raises(MemoryFailureError):
            resolve("s1", (persona,))


@starling_only
@pytest.mark.skipif(hasattr(__import__("os"), "geteuid") and __import__("os").geteuid() == 0,
                    reason="root 不受目录权限限制")
def test_an_unreadable_seed_directory_is_not_taken_for_an_empty_start(tmp_path):
    """种子目录在、但进不去:lstat 报的是 PermissionError 不是 FileNotFoundError,不能当"没有这个库"。"""
    from factory.memory_runtime import starling_memory_resolver
    seed = tmp_path / "seeds" / "s1"
    seed.mkdir(parents=True)
    resolve = starling_memory_resolver(StarlingLayout(tmp_path), {"backend": "starling"})
    seed.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            resolve("s1", ("pa",))
    finally:
        seed.chmod(0o700)


@starling_only
def test_a_changed_embedding_vector_changes_the_seed_digest(tmp_path):
    """Review Focus:嵌入端点换了向量 → 起点摘要变 → config_hash 变 → 续训升 T3(T3 本身由 计划 A Task 25 的
    test_the_resolved_memory_is_part_of_the_config_hash 钉住;这里钉住前半段:同模型名、不同向量,摘要不同)。"""
    from actio.memory_starling import StarlingDeploymentStore
    from starling_fakes import FakeOpenAI, offline_api_key
    out = []
    for salt in ("", "v2"):
        with FakeOpenAI(dim=256) as fake, offline_api_key():
            fake.salt = salt
            store = StarlingDeploymentStore(tmp_path / salt / "seeds", endpoints=fake.endpoints())
            store.remember_event("pa", "pa", "seed#0@pa", "pb：我担心预算超支", interlocutor="pb")
            port = store.open("pa", "pa")
            port.embed_pending()
            port.close()
            out.append(store.digest("pa"))
    assert out[0] != out[1]
