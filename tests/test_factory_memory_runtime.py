"""组合根的替身记忆解析器(设计二 7.1 节):--interact 装配时注入,先于策略上下文解出起点记忆。"""
from __future__ import annotations

import pytest

from simple_actio.memory import TupleMemory
from controlplane.daemon import DirectiveFailedError
from factory.memory_runtime import STAND_IN_MEMORY_RUNTIME, stand_in_memory_resolver
from memory_fixtures import STAND_IN_RUNTIME  # Task 24 的测试夹具先于本模块存在,这里钉住两份相等


def test_the_stand_in_runtime_identity_is_the_one_the_design_fixes():
    assert STAND_IN_MEMORY_RUNTIME == STAND_IN_RUNTIME
    assert STAND_IN_MEMORY_RUNTIME == {
        "backend": "stand-in", "starling_version": None, "extraction": None, "embedding": None,
        "trace_schema": "simple_actio.trace.v2",
    }


def test_every_scenario_starts_from_the_empty_stand_in_world():
    digests, runtime = stand_in_memory_resolver(None, ("s1", "s2"))
    assert digests == {"s1": TupleMemory().digest(), "s2": TupleMemory().digest()}
    assert runtime == STAND_IN_MEMORY_RUNTIME


def test_the_returned_identity_is_a_copy():
    _, runtime = stand_in_memory_resolver(None, ("s1",))
    runtime["backend"] = "x"
    assert STAND_IN_MEMORY_RUNTIME["backend"] == "stand-in"


def test_a_seed_needs_a_memory_config():
    with pytest.raises(DirectiveFailedError, match="needs a memory config") as info:
        stand_in_memory_resolver("seed-1", ("s1",))
    assert "RISE_MEMORY_CONFIG" in info.value.reason and info.value.category == "T2"


# ---------------- Starling 那一路:起点摘要解析器走服务(30 篇 MT-32,90 篇解析器) ----------------

import subprocess
import sys
from pathlib import Path

from controlplane.deferred import DeferredError
from simple_actio.memory_port import MemoryFailureError

SRC = Path(__file__).resolve().parents[1] / "src"


def test_importing_the_module_does_not_need_the_client():
    """没装 extra memory 的环境只跑替身路径:`import factory.memory_runtime` 不能依赖 `starling_client`,替身照常可用。
    子进程里把 starling_client 置成 None,装没装都一样。"""
    code = (
        "import sys\n"
        "sys.modules['starling_client'] = None\n"
        "sys.modules['starling'] = None\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "from factory.memory_runtime import StandInTrainingWorlds, StarlingTrainingWorlds, starling_memory_resolver\n"
        "assert 'simple_actio.memory_starling' not in sys.modules\n"
        "w = StandInTrainingWorlds(); w.bind('r', None)\n"
        "w.close_rollout('e', [w.open_rollout('r', 's1', 'e', 'pa')], wait_s=1.0)\n"
        "print('ok')\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False, timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    assert out.stdout.strip() == "ok"


class _Session:
    """解析器只用到 `admin`;`fail` 给定时每次管理调用抛它。"""

    def __init__(self, digest="e" * 64, fail=None):
        self.digest, self.fail, self.calls = digest, fail, []

    def admin(self, op, body):
        self.calls.append((op, body))
        if self.fail is not None:
            raise self.fail
        return {"worlds": [{"name": "rise.seed-empty", "digest": self.digest}], "total": 1}


def _resolver(session, runtime=None):
    from factory.memory_runtime import starling_memory_resolver
    return starling_memory_resolver(session, prefix="rise", empty_seed_world="rise.seed-empty",
                                    runtime={"backend": "starling"} if runtime is None else runtime)


def test_without_a_seed_every_scenario_starts_from_the_empty_seed_digest():
    session = _Session()
    digests, rt = _resolver(session)(None, ("t1", "t2"))
    assert digests == {"t1": "e" * 64, "t2": "e" * 64} and rt == {"backend": "starling"}
    (op, body), = session.calls                                          # 只问一次,不开句柄
    assert op == "list_worlds" and body["labels"] == {"repo": "rise", "kind": "seed-empty"} and body["with_digest"]


def test_a_seed_is_refused_until_step_5_as_a_readable_t2():
    with pytest.raises(DirectiveFailedError, match="memory.seed 'k': seeds arrive in step 5") as info:
        _resolver(_Session())("k", ("t1",))
    assert info.value.category == "T2"


def test_a_transient_service_failure_defers_the_directive():
    session = _Session(fail=MemoryFailureError("starling service_unavailable (ServiceUnavailable)", transient=True))
    with pytest.raises(DeferredError, match="service_unavailable"):
        _resolver(session)(None, ("t1",))


def test_a_permanent_service_failure_is_left_for_the_dispatcher_to_judge_t2():
    session = _Session(fail=MemoryFailureError("starling world_read_only (WorldError)", transient=False))
    with pytest.raises(MemoryFailureError) as info:
        _resolver(session)(None, ("t1",))
    assert info.value.transient is False


def test_a_missing_empty_seed_is_permanent():
    session = _Session()
    session.admin = lambda op, body: {"worlds": [], "total": 0}
    with pytest.raises(MemoryFailureError, match="rise.seed-empty has no digest"):
        _resolver(session)(None, ("t1",))


def test_the_resolver_returns_a_copy_of_the_identity_it_was_built_with():
    runtime = {"backend": "starling", "extraction": {"model": "m"}}
    resolve = _resolver(_Session(), runtime)
    runtime["extraction"]["model"] = "changed-after-the-fact"            # 造完解析器再改原字典,不影响解出的身份
    first = resolve(None, ("t1",))[1]
    first["extraction"]["model"] = "changed-by-the-caller"               # 调用方改返回值,也不影响下一次
    assert resolve(None, ("t1",))[1] == {"backend": "starling", "extraction": {"model": "m"}}


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    pytest.importorskip("starling_client"); pytest.importorskip("starling.service")
    from starling_service import start_service

    proc = start_service(tmp_path_factory.mktemp("svc"))
    yield proc
    proc.kill()


def test_the_resolver_reads_the_empty_seed_on_the_real_service(service, tmp_path):
    from starling_fakes import FakeOpenAI
    from starling_service import deployment_store, open_session

    session = open_session(service)
    try:
        with FakeOpenAI(dim=256) as fake:
            store = deployment_store(session, fake, tmp_path / "mem" / "deploy")
            from factory.memory_runtime import starling_memory_resolver
            resolve = starling_memory_resolver(session, prefix=store.prefix,
                                               empty_seed_world=f"{store.prefix}.seed-empty",
                                               runtime={"backend": "starling"})
            assert resolve(None, ("t1",))[0] == {"t1": store.empty_seed_digest}
    finally:
        session.close()


def test_a_changed_embedding_vector_changes_the_world_digest(service, tmp_path):
    """Review Focus:嵌入端点换了向量 → 世界摘要变 → 起点摘要与 config_hash 变 → 续训升 T3(T3 由
    test_the_resolved_memory_is_part_of_the_config_hash 钉住;这里钉住前半段:同模型名、不同向量,摘要不同)。"""
    from starling_fakes import FakeOpenAI
    from starling_service import deployment_store, open_session

    out = []
    session = open_session(service)
    try:
        for salt in ("", "v2"):
            with FakeOpenAI(dim=256) as fake:
                fake.salt = salt
                store = deployment_store(session, fake, tmp_path / (salt or "plain") / "deploy")
                store.remember_event("t1", "pa", "seed#0@pa", "pb：我担心预算超支", interlocutor="pb")
                out.append(store.digest("t1"))
    finally:
        session.close()
    assert out[0] != out[1]
