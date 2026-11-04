"""训练路副本(设计二 5.3、6.2、7.2 节):副本从同一起点出发、目录名里有 session_id、终态即删,
端口没关上就不删;清扫只动 runs/。"""
from __future__ import annotations

import logging
import os
import threading

import pytest

pytest.importorskip("starling")
from actio.memory_port import MemoryClosedError, MemoryEvent, MemoryFailureError  # noqa: E402
from actio.memory_starling import StarlingDeploymentStore, empty_digest  # noqa: E402
from factory.memory_runtime import StarlingLayout, TrainingCopies  # noqa: E402
from starling_fakes import FakeOpenAI, offline_api_key  # noqa: E402

EV = MemoryEvent("run-1-grpo0-s1-r0#1", "pb：我担心预算超支", "pb")


@pytest.fixture
def env(tmp_path):
    with FakeOpenAI(dim=256) as fake, offline_api_key():
        lay = StarlingLayout(tmp_path / "mem")
        seeds = StarlingDeploymentStore(tmp_path / "mem" / "seeds" / "s1", endpoints=fake.endpoints())
        seeds.remember_event("pa", "pa", "seed#0@pa", "pb：人手也不够", interlocutor="pb")
        seeded = seeds.digest("pa")
        # 种子真的有内容:否则"复制了种子"和"建了空库"分不出来,下面几条"同一起点"的断言会空转
        assert seeded != empty_digest()
        yield lay, TrainingCopies(lay, fake.endpoints(), close_timeout_s=0.2), fake, seeded


def test_copies_of_one_group_start_from_the_same_seed_and_leave_the_seed_alone(env):
    lay, copies, _, seeded = env
    copies.bind("run-1", "s1")
    before = lay.seed("s1", "pa").read_bytes()
    a = copies.open_client_copy("run-1", "run-1-grpo0-s1-r0", "a", "pa")
    b = copies.open_client_copy("run-1", "run-1-grpo0-s1-r1", "a", "pa")
    c = copies.open_session_copy("run-1", "sess-1", "b", "pb")              # 种子里没有 pb:空库
    assert a.digest() == b.digest() == seeded and c.digest() == empty_digest()
    a.remember(EV)
    assert a.digest() != seeded                                                # 写入真的生效了
    assert b.digest() == seeded                                                # 两份互不可见
    assert lay.client_copy("run-1", "run-1-grpo0-s1-r0", "a").exists()
    assert lay.session_copy("run-1", "sess-1", "b").exists()                  # session_id 在目录名里
    assert lay.seed("s1", "pa").read_bytes() == before
    for p in (a, b, c):
        p.close()


def test_a_read_only_seed_is_still_copied_and_stays_untouched(env):
    """5.3 节:种子只读,人放进 seeds/ 的种子很可能就是只读权限。从种子取副本用文件复制,不经 backup_to
    (它要在种子上建 -wal、-shm、跑迁移,只读时失败)。"""
    lay, copies, _, seeded = env
    copies.bind("run-1", "s1")
    seed, seed_dir = lay.seed("s1", "pa"), lay.seed_dir("s1")
    before, mtime = seed.read_bytes(), seed.stat().st_mtime_ns
    os.chmod(seed, 0o444)
    os.chmod(seed_dir, 0o555)
    try:
        port = copies.open_client_copy("run-1", "run-1-grpo0-s1-r0", "a", "pa")
        try:
            assert port.digest() == seeded
        finally:
            port.close()
        assert sorted(p.name for p in seed_dir.iterdir()) == ["pa.db"]        # 种子旁没留 -wal、-shm
    finally:
        os.chmod(seed_dir, 0o755)                                              # 不改回来 tmp_path 清理会失败
        os.chmod(seed, 0o644)
    assert seed.read_bytes() == before and seed.stat().st_mtime_ns == mtime


def test_release_removes_the_directory_only_after_every_port_closed(env):
    lay, copies, _, _ = env
    copies.bind("run-1", None)
    p = copies.open_session_copy("run-1", "sess-1", "b", "pb")
    q = copies.open_session_copy("run-1", "sess-1", "c", "pc")
    assert copies.release([p, q], copies.session_dir("run-1", "sess-1")) is True
    assert not copies.session_dir("run-1", "sess-1").exists()


def test_releasing_a_directory_that_was_never_created_is_fine(env):
    """会话在开出第一份副本之前就失败时,目录不存在:不是错误,也不记 warning。"""
    _, copies, _, _ = env
    assert copies.release([], copies.session_dir("run-1", "sess-never")) is True


def test_a_copy_whose_starling_call_has_not_returned_is_not_deleted(env, caplog):
    """Review Focus:副本在 Starling 调用未返回时被删。close(timeout) 等不到在途调用 → 不删目录,
    重抛非瞬态失败;调用返回后再 release 才删。"""
    lay, copies, fake, _ = env
    copies.bind("run-1", None)
    port = copies.open_session_copy("run-1", "sess-1", "b", "pb")
    fake.chat_delay_s = 1.0
    t = threading.Thread(target=port.remember, args=(EV,)); t.start()
    fake.wait_for_chat_call(timeout_s=2)
    d = copies.session_dir("run-1", "sess-1")
    with caplog.at_level(logging.WARNING, logger="factory.memory_runtime"), pytest.raises(MemoryFailureError):
        copies.release([port], d)
    assert d.exists() and "kept" in caplog.text
    t.join()
    assert copies.release([port], d) is True and not d.exists()


def test_release_still_closes_the_remaining_ports_when_one_does_not_close(env):
    """不关剩下的端口,它们的 SQLite 连接会一直开到进程退出。目录仍不能删:第一个端口还在写。"""
    lay, copies, fake, _ = env
    copies.bind("run-1", None)
    p = copies.open_session_copy("run-1", "sess-1", "b", "pb")
    q = copies.open_session_copy("run-1", "sess-1", "c", "pc")
    fake.chat_delay_s = 1.0
    t = threading.Thread(target=p.remember, args=(EV,)); t.start()
    fake.wait_for_chat_call(timeout_s=2)
    d = copies.session_dir("run-1", "sess-1")
    with pytest.raises(MemoryFailureError):
        copies.release([p, q], d)                          # p 关不上,q 排在它后面
    with pytest.raises(MemoryClosedError):
        q.digest()                                         # q 已经关了
    assert d.exists()
    t.join()
    assert copies.release([p, q], d) is True and not d.exists()


def test_release_keeps_the_directory_and_does_not_raise_when_removal_fails(env, caplog, monkeypatch):
    lay, copies, _, _ = env
    copies.bind("run-1", None)
    p = copies.open_session_copy("run-1", "sess-1", "b", "pb")
    d = copies.session_dir("run-1", "sess-1")

    def refuse(path, *args, **kwargs):
        raise OSError(f"cannot remove {path}")

    monkeypatch.setattr("factory.memory_runtime.shutil.rmtree", refuse)
    with caplog.at_level(logging.WARNING, logger="factory.memory_runtime"):
        assert copies.release([p], d) is False
    assert d.exists() and "kept" in caplog.text and "OSError" in caplog.text
    assert str(d) not in caplog.text                       # 只记目录名与类名,不记路径与异常文字
    monkeypatch.undo()
    assert copies.release([p], d) is True and not d.exists()


def test_a_client_retry_recreates_its_copy_from_the_start(env):
    lay, copies, _, seeded = env
    copies.bind("run-1", "s1")
    first = copies.open_client_copy("run-1", "run-1-grpo0-s1-r0", "a", "pa")
    first.remember(EV)
    assert first.digest() != seeded                       # 上一次尝试真的写了东西
    first.close()                                         # 上一次尝试:副本留着(close 后才轮到 release)
    again = copies.open_client_copy("run-1", "run-1-grpo0-s1-r0", "a", "pa")
    assert again.digest() == seeded
    again.close()


def test_an_unbound_run_has_no_seed_and_cannot_open_copies(env):
    _, copies, _, _ = env
    with pytest.raises(KeyError):
        copies.seed_of("run-1")
    with pytest.raises(KeyError):
        copies.open_session_copy("run-1", "sess-1", "b", "pb")
    with pytest.raises(KeyError):
        copies.open_client_copy("run-1", "run-1-grpo0-s1-r0", "a", "pa")
    copies.bind("run-1", "s1")
    assert copies.seed_of("run-1") == "s1"
    copies.bind("run-2", None)
    assert copies.seed_of("run-2") is None
    copies.unbind("run-1")
    copies.unbind("run-1")                                 # 解绑两次不报错
    with pytest.raises(KeyError):
        copies.seed_of("run-1")
    assert copies.seed_of("run-2") is None


def test_sweep_only_empties_runs(env):
    lay, copies, _, _ = env
    copies.bind("run-1", None)
    copies.open_session_copy("run-1", "sess-1", "b", "pb").close()
    (lay.root / "snapshots" / "r1").mkdir(parents=True)
    assert copies.sweep() == 1
    assert list(lay.runs_dir().iterdir()) == []
    assert (lay.root / "seeds" / "s1").exists() and (lay.root / "snapshots" / "r1").exists()


def test_sweep_with_no_runs_directory_removes_nothing(env):
    lay, copies, _, _ = env
    assert not lay.runs_dir().exists() and copies.sweep() == 0


def test_sweep_unlinks_a_symlink_and_never_follows_it(env, tmp_path):
    """Python 3.14 的 shutil.rmtree 对符号链接抛 OSError;跟着链接删更会删到 runs/ 之外。"""
    lay, copies, _, _ = env
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    lay.runs_dir().mkdir(parents=True)
    (lay.runs_dir() / "run-link").symlink_to(outside, target_is_directory=True)
    (lay.runs_dir() / "stray.txt").write_text("stray")
    assert copies.sweep() == 2
    assert list(lay.runs_dir().iterdir()) == []
    assert outside.is_dir() and (outside / "keep.txt").read_text() == "keep"


def _hold_a_write_in_flight(fake, port):
    """让 port 上有一次 Starling 调用还没返回(假端点把抽取拖 1 秒),返回那条线程。"""
    fake.chat_delay_s = 1.0
    t = threading.Thread(target=port.remember, args=(EV,))
    t.start()
    fake.wait_for_chat_call(timeout_s=2)
    return t


def test_a_client_directory_kept_for_a_port_that_did_not_close_is_not_deleted_by_a_reopen(env):
    """I-1:release 因 close 超时保留了目录,Starling 工作线程还在写。同一 episode id 再来开副本,不能 rmtree 它。
    今天的调用方(轨迹源只重试瞬态失败)走不到这里,但防线要在 TrainingCopies 自己身上。"""
    lay, copies, fake, seeded = env
    copies.bind("run-1", "s1")
    episode = "run-1-grpo0-s1-r0"
    port = copies.open_client_copy("run-1", episode, "a", "pa")
    t = _hold_a_write_in_flight(fake, port)
    d = copies.client_dir("run-1", episode)
    with pytest.raises(MemoryFailureError):
        copies.release([port], d)
    with pytest.raises(MemoryFailureError, match="still held") as held:
        copies.open_client_copy("run-1", episode, "a", "pa")
    assert held.value.transient is False and episode in str(held.value) and str(lay.root) not in str(held.value)
    assert lay.client_copy("run-1", episode, "a").exists()                   # 没有 rmtree
    t.join()
    assert copies.release([port], d) is True and not d.exists()               # 在途调用返回、release 成功:登记撤销
    fake.chat_delay_s = 0.0
    again = copies.open_client_copy("run-1", episode, "a", "pa")
    try:
        assert again.digest() == seeded
    finally:
        again.close()


def test_a_session_directory_kept_for_a_port_that_did_not_close_refuses_further_copies(env):
    lay, copies, fake, _ = env
    copies.bind("run-1", None)
    port = copies.open_session_copy("run-1", "sess-1", "b", "pb")
    t = _hold_a_write_in_flight(fake, port)
    d = copies.session_dir("run-1", "sess-1")
    with pytest.raises(MemoryFailureError):
        copies.release([port], d)
    with pytest.raises(MemoryFailureError, match="still held") as held:
        copies.open_session_copy("run-1", "sess-1", "c", "pc")
    assert held.value.transient is False
    assert not lay.session_copy("run-1", "sess-1", "c").exists()
    t.join()
    assert copies.release([port], d) is True
    fake.chat_delay_s = 0.0
    copies.open_session_copy("run-1", "sess-1", "c", "pc").close()


def test_a_copy_that_cannot_be_made_fails_as_a_permanent_memory_failure(env, monkeypatch):
    """磁盘满、权限不够:复制抛的 OSError 译成非瞬态的 MemoryFailureError,文字只含类名,不留半建的目录。
    不译的话它是裸 OSError,训练位一侧不被当成记忆失败,run 会被反复重派。"""
    lay, copies, _, _ = env
    copies.bind("run-1", "s1")

    def full(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("actio.memory_starling.copy_library", full)
    with pytest.raises(MemoryFailureError) as caught:
        copies.open_client_copy("run-1", "run-1-grpo0-s1-r0", "a", "pa")
    assert caught.value.transient is False and "OSError" in str(caught.value)
    assert "No space" not in str(caught.value) and str(lay.root) not in str(caught.value)
    assert not copies.client_dir("run-1", "run-1-grpo0-s1-r0").exists()


def test_sweep_refuses_a_runs_directory_that_is_itself_a_symlink(env, tmp_path):
    """M-1:runs/ 本身是指向别处的符号链接时,遍历会跟进去,启动一次就清空那个目录。"""
    lay, copies, _, _ = env
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "keep.txt").write_text("keep")
    lay.runs_dir().symlink_to(outside, target_is_directory=True)
    with pytest.raises(MemoryFailureError, match="symlink") as caught:
        copies.sweep()
    assert caught.value.transient is False
    assert (outside / "keep.txt").read_text() == "keep" and (outside / "sub").is_dir()
    assert lay.runs_dir().is_symlink()


# ---------------- 接到运行时上 ----------------

from simulator.datamodel import RoleAssignment, RoleBinding, RoleSlot, Scenario  # noqa: E402
from simulator.realtime.resources import SessionInfo  # noqa: E402

from factory.agent import AgentRuntime  # noqa: E402


@pytest.fixture
def store_with_scenario(tmp_path):
    """(树, 情境, 分配):情境 s1 有 a、b、c 三个位,a 是客户端位,b、c 是对手位。"""
    from factory_fixtures import closed_loop_repo

    store = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    scenario = Scenario("s1", "bg", [RoleSlot("a", "a"), RoleSlot("b", "b"), RoleSlot("c", "c")],
                        "c", ["n"], "training")
    assignment = RoleAssignment("s1", [RoleBinding("a", "pa", "self_play"),
                                       RoleBinding("b", "pb", "frozen_reference"),
                                       RoleBinding("c", "pc", "frozen_reference")], 4)
    return store, scenario, assignment


def _info(scenario, assignment, *, session_id, client_slot):
    return SessionInfo(scenario, assignment, {}, client_slot, session_id, f"ep-{session_id}")


def test_a_registered_training_session_gets_starling_opponents_and_its_copy_goes_at_close(env, store_with_scenario):
    lay, copies, _, _ = env
    store, scenario, assignment = store_with_scenario
    runtime = AgentRuntime(store, ("hi",), training_copies=copies)
    runtime.register_session(scenario.id, "run-1")
    copies.bind("run-1", None)
    res = runtime._backends(_info(scenario, assignment, session_id="sess-9", client_slot="a"))
    opponents = [s for s in res.backends]
    assert opponents == ["b", "c"]
    assert all(type(res.backends[s].instance.memory).__name__ == "StarlingMemory" for s in opponents)
    assert copies.session_dir("run-1", "sess-9").exists()
    res.close()
    assert not copies.session_dir("run-1", "sess-9").exists() and runtime._session_runs == {}


def test_each_opponent_copy_is_the_slots_own_persona_library(env, store_with_scenario):
    """对手位的副本按该位绑定的人格取种子:b 绑 pa 时从种子里的 pa 起步,c 绑 pc 没有种子从空库起。"""
    lay, copies, _, seeded = env
    store, scenario, _ = store_with_scenario
    assignment = RoleAssignment("s1", [RoleBinding("a", "pz", "self_play"),
                                       RoleBinding("b", "pa", "frozen_reference"),
                                       RoleBinding("c", "pc", "frozen_reference")], 4)
    runtime = AgentRuntime(store, ("hi",), training_copies=copies)
    runtime.register_session(scenario.id, "run-1")
    copies.bind("run-1", "s1")
    res = runtime._backends(_info(scenario, assignment, session_id="sess-9", client_slot="a"))
    try:
        assert res.backends["b"].instance.memory.digest() == seeded
        assert res.backends["c"].instance.memory.digest() == empty_digest()
    finally:
        res.close()
    assert not copies.session_dir("run-1", "sess-9").exists()


def test_an_unregistered_deployment_session_keeps_empty_tuple_opponents(env, store_with_scenario):
    lay, copies, _, _ = env
    store, scenario, assignment = store_with_scenario
    runtime = AgentRuntime(store, ("hi",), training_copies=copies)
    res = runtime._backends(_info(scenario, assignment, session_id="sess-d", client_slot="a"))
    assert all(type(b.instance.memory).__name__ == "TupleMemory" for b in res.backends.values())
    assert not lay.runs_dir().exists() or list(lay.runs_dir().iterdir()) == []
    res.close()


def test_a_failure_while_building_releases_the_copies_already_opened_and_the_route(
        env, store_with_scenario, monkeypatch):
    """6.2 节:工厂自己抛异常时不留半开的库。第二个对手位的副本开不出来,第一个位的端口要关、会话目录要删、
    路由要撤,原异常原样抛出。"""
    lay, copies, _, _ = env
    store, scenario, assignment = store_with_scenario
    opened = []
    original = copies.open_session_copy

    def flaky(run_id, session_id, slot_id, persona_id):
        if slot_id == "c":
            raise RuntimeError("no copy for c")
        port = original(run_id, session_id, slot_id, persona_id)
        opened.append(port)
        return port

    monkeypatch.setattr(copies, "open_session_copy", flaky)
    runtime = AgentRuntime(store, ("hi",), training_copies=copies)
    runtime.register_session(scenario.id, "run-1")
    copies.bind("run-1", None)
    with pytest.raises(RuntimeError, match="no copy for c"):
        runtime._backends(_info(scenario, assignment, session_id="sess-f", client_slot="a"))
    assert len(opened) == 1
    with pytest.raises(MemoryClosedError):
        opened[0].digest()                                 # b 的端口已关
    assert not copies.session_dir("run-1", "sess-f").exists() and runtime._session_runs == {}


def test_a_training_session_whose_run_was_never_bound_fails_like_any_failed_build(env, store_with_scenario):
    """正常顺序里 bind 先于 register_session,不会发生;发生了就按造后端失败处理,不留路由与目录。"""
    lay, copies, _, _ = env
    store, scenario, assignment = store_with_scenario
    runtime = AgentRuntime(store, ("hi",), training_copies=copies)
    runtime.register_session(scenario.id, "run-1")
    with pytest.raises(KeyError):
        runtime._backends(_info(scenario, assignment, session_id="sess-u", client_slot="a"))
    assert runtime._session_runs == {}
    assert not lay.runs_dir().exists() or list(lay.runs_dir().iterdir()) == []
