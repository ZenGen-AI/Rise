"""训练 rollout 世界(设计 5.1、5.2、6.3 节):一条 rollout 一个世界,训练位与对手位都是世界里的主体。
客户端建世界、开训练位端口;服务端按 episode id 加入、开对手位端口;客户端等服务端端口关完才收尾世界(替身删,
Starling 只关),等不到就留在保留表里,同名重试判瞬态。替身与 Starling 两个实现过同一套契约。

Starling 参数连模块级共用的真 starling-serve,每条用例一个世界前缀;rollout 世界从空种子分叉(90 篇分叉的命名与重试表)。"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

import pytest

from simple_actio.memory import TupleMemory
from simple_actio.memory_port import MemoryClosedError, MemoryEvent, MemoryFailureError
from factory.memory_runtime import StandInTrainingWorlds

EP = "run-1-grpo0-s1-r0"
EV = MemoryEvent(f"{EP}#1@pa", "pb：我担心预算超支", "pb")


@dataclass
class Case:
    worlds: object
    start: str                       # 这个 run 的 rollout 世界的起点摘要
    session: object | None           # 只有 Starling 有
    fake: object | None


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    pytest.importorskip("starling_client"); pytest.importorskip("starling.service")
    from starling_service import start_service

    proc = start_service(tmp_path_factory.mktemp("svc"))
    yield proc
    proc.kill()


@pytest.fixture
def starling_worlds(service, tmp_path):
    """`make(prefix=None, admin_wrap=None, **kw)` 在真服务上造会话、空种子与 `StarlingTrainingWorlds`,并 `bind("run-1", None)`;
    返回 `(worlds, session, admin)`。`admin_wrap` 给了就用它包住会话的管理调用、交给世界,`admin` 是包好的那个
    (没给就是 `session.admin`)。同名重试等保留世界的期限压到 0.1 秒。"""
    from factory import starling_names
    from factory.memory_runtime import StarlingTrainingWorlds
    from simple_actio.memory_starling import ensure_empty_seed
    from starling_fakes import FakeOpenAI
    from starling_service import fake_memory_config, open_session, unique_prefix

    made = []
    fake = FakeOpenAI(dim=256).start()

    def make(prefix=None, admin_wrap=None, **kw):
        session = open_session(service)
        made.append(session)
        prefix = prefix or unique_prefix()
        root = tmp_path / "mem"
        label = starling_names.root_label(root)
        settings = starling_names.rise_settings(fake_memory_config(fake, root))
        seed = ensure_empty_seed(session, names=starling_names, prefix=prefix, root_label=label, settings=settings)
        admin = admin_wrap(session) if admin_wrap is not None else session.admin
        kw.setdefault("close_timeout_s", 0.2)
        kw.setdefault("held_wait_s", 0.1)
        worlds = StarlingTrainingWorlds(session, prefix=prefix, root_label=label, settings=settings,
                                        empty_seed=seed, admin=admin, **kw)
        worlds.bind("run-1", None)
        return worlds, session, admin

    make.fake = fake
    yield make
    for session in made:
        session.close()
    fake.stop()


@pytest.fixture(params=["stand-in", "starling"])
def case(request):
    """run-1 已绑定,从空世界起。同名重试等保留世界的期限压到 0.1 秒,省得撞保留表的用例真等 70 秒。"""
    if request.param == "stand-in":
        worlds = StandInTrainingWorlds(held_wait_s=0.1)
        worlds.bind("run-1", None)
        yield Case(worlds, TupleMemory().digest(), None, None)
        return
    make = request.getfixturevalue("starling_worlds")
    worlds, session, _ = make()
    yield Case(worlds, worlds.empty_seed[1], session, make.fake)


@pytest.fixture
def worlds(case):
    return case.worlds


# ---------------- 契约:两个实现一样 ----------------

def test_the_trained_and_opponent_slots_of_one_rollout_share_a_world_but_not_their_recall(worlds):
    a = worlds.open_rollout("run-1", "s1", EP, "pa")
    b = worlds.join_rollout(EP, "pb")
    before = a.digest()
    assert b.digest() == before                                   # 同一个世界
    a.remember(MemoryEvent(f"{EP}#1@pa", "pb：预算砍一半", "pb"))
    b.remember(MemoryEvent(f"{EP}#0@pb", "pa：我们先谈预算", "pa"))
    a.embed_pending()
    b.embed_pending()
    assert a.digest() == b.digest() != before                     # 世界摘要覆盖两个主体
    from_a, from_b = a.recall("预算", limit=5), b.recall("预算", limit=5)
    assert from_a and all("砍一半" in h.text for h in from_a)
    assert from_b and all("先谈" in h.text for h in from_b)
    worlds.release_session(EP, [b])
    assert worlds.close_rollout(EP, [a], wait_s=3.0) is True


def test_two_rollouts_of_a_group_start_from_the_same_seed_and_do_not_see_each_other(case):
    worlds = case.worlds
    r0 = worlds.open_rollout("run-1", "s1", EP, "pa")
    r1 = worlds.open_rollout("run-1", "s1", "run-1-grpo0-s1-r1", "pa")
    assert r0.digest() == r1.digest() == case.start
    r0.remember(EV)
    assert r0.digest() != case.start and r1.digest() == case.start
    assert worlds.close_rollout(EP, [r0], wait_s=1.0) is True
    assert worlds.close_rollout("run-1-grpo0-s1-r1", [r1], wait_s=1.0) is True


def test_a_server_that_cannot_find_the_rollout_world_is_a_permanent_memory_failure(worlds):
    with pytest.raises(MemoryFailureError) as exc:
        worlds.join_rollout("run-1-grpo0-s1-r0", "pb")
    assert exc.value.transient is False


def test_the_client_waits_for_a_server_port_that_closes_late(worlds):
    a = worlds.open_rollout("run-1", "s1", "run-1-grpo0-s1-r0", "pa")
    b = worlds.join_rollout("run-1-grpo0-s1-r0", "pb")
    threading.Timer(0.5, lambda: worlds.release_session("run-1-grpo0-s1-r0", [b])).start()
    started = time.monotonic()
    assert worlds.close_rollout("run-1-grpo0-s1-r0", [a], wait_s=3.0) is True
    assert time.monotonic() - started >= 0.4                     # 真的等了服务端的端口
    with pytest.raises(MemoryFailureError):                       # 世界已删、表项已摘
        worlds.join_rollout("run-1-grpo0-s1-r0", "pb")


def test_a_retry_while_the_world_is_still_held_is_transient(case):
    worlds = case.worlds
    a = worlds.open_rollout("run-1", "s1", "run-1-grpo0-s1-r0", "pa")
    b = worlds.join_rollout("run-1-grpo0-s1-r0", "pb")          # 不关
    assert worlds.close_rollout("run-1-grpo0-s1-r0", [a], wait_s=0.1) is False
    with pytest.raises(MemoryFailureError) as exc:
        worlds.open_rollout("run-1", "s1", "run-1-grpo0-s1-r0", "pa")
    assert exc.value.transient is True
    assert "run-1-grpo0-s1-r0" in str(exc.value)
    with pytest.raises(MemoryFailureError) as joined:             # 保留的世界不再让服务端加入
        worlds.join_rollout("run-1-grpo0-s1-r0", "pc")
    assert joined.value.transient is False
    b.remember(EV)                                                # 服务端端口还开着,还能写
    worlds.release_session("run-1-grpo0-s1-r0", [b])              # 回收线程终于关上了它
    again = worlds.open_rollout("run-1", "s1", "run-1-grpo0-s1-r0", "pa")
    assert again.digest() == case.start                           # 先删后建:从起点重来
    assert worlds.close_rollout("run-1-grpo0-s1-r0", [again], wait_s=1.0) is True


def test_a_retry_waits_for_a_held_world_whose_server_port_closes_late(case):
    # 服务端 join 超时时回收线程最早约 120 秒才关端口,客户端只等了 70 秒就把世界放进保留表。轨迹源的重试没有间隔,
    # 所以同名 open_rollout 先有界等端口关上,关上了就先删后建,不把重试预算一下子耗光
    worlds = case.worlds
    worlds.held_wait_s = 3.0
    a = worlds.open_rollout("run-1", "s1", EP, "pa")
    b = worlds.join_rollout(EP, "pb")
    assert worlds.close_rollout(EP, [a], wait_s=0.05) is False      # 世界进保留表
    b.remember(EV)                                                # 服务端写的东西不该带进重建的世界
    timer = threading.Timer(0.3, lambda: worlds.release_session(EP, [b]))
    timer.start()
    started = time.monotonic()
    again = worlds.open_rollout("run-1", "s1", EP, "pa")
    assert time.monotonic() - started >= 0.25                     # 真的等了服务端的端口
    assert again.digest() == case.start
    timer.join()
    assert worlds.close_rollout(EP, [again], wait_s=1.0) is True


def test_a_reopen_while_the_previous_attempt_is_still_open_does_not_wait(case):
    # 只等保留表里的世界:世界表里的同名项还开着,说明上一次尝试的 close_rollout 还没跑完;等它的话会和
    # close_rollout 抢着醒来,新建的世界随后被 close_rollout 摘掉、删掉。所以照旧立即判瞬态
    worlds = case.worlds
    worlds.held_wait_s = 3.0
    a = worlds.open_rollout("run-1", "s1", EP, "pa")
    started = time.monotonic()
    with pytest.raises(MemoryFailureError) as exc:
        worlds.open_rollout("run-1", "s1", EP, "pa")
    assert exc.value.transient is True and time.monotonic() - started < 1.0
    assert worlds.close_rollout(EP, [a], wait_s=1.0) is True


def test_a_world_kept_after_the_wait_is_logged_by_its_episode_id_only(case, caplog):
    worlds = case.worlds
    a = worlds.open_rollout("run-1", "s1", EP, "pa")
    b = worlds.join_rollout(EP, "pb")
    with caplog.at_level(logging.WARNING, logger="factory.memory_runtime"):
        assert worlds.close_rollout(EP, [a], wait_s=0.05) is False
    assert EP in caplog.text and "kept" in caplog.text
    worlds.release_session(EP, [b])


def test_closing_one_port_twice_counts_it_once(worlds):
    """两条线程各关一次同一个端口时多减的那次会把别的端口的计数也扣掉,客户端就在服务端端口开着时删世界。"""
    a = worlds.open_rollout("run-1", "s1", EP, "pa")
    b, c = worlds.join_rollout(EP, "pb"), worlds.join_rollout(EP, "pc")
    b.close()
    b.close()
    assert worlds.close_rollout(EP, [a], wait_s=0.1) is False      # c 还开着
    worlds.release_session(EP, [c])


def test_release_session_closes_every_port_even_after_one_fails(worlds):
    a = worlds.open_rollout("run-1", "s1", EP, "pa")
    b, c = worlds.join_rollout(EP, "pb"), worlds.join_rollout(EP, "pc")

    class Stuck:
        def close(self):
            raise MemoryFailureError("starling memory failed: TimeoutError", transient=False)

    with pytest.raises(MemoryFailureError):
        worlds.release_session(EP, [Stuck(), b, c])
    for port in (b, c):
        with pytest.raises(MemoryClosedError):
            port.digest()
    assert worlds.close_rollout(EP, [a], wait_s=1.0) is True


def test_a_client_port_that_does_not_close_keeps_the_world_and_reraises(worlds):
    """客户端端口关不上(Starling 的在途调用没返回):不等、不删,世界进保留表,原异常抛出(训练位记忆失败判 T2)。"""
    a = worlds.open_rollout("run-1", "s1", EP, "pa")
    failure = MemoryFailureError("starling memory failed: TimeoutError", transient=False)

    class Stuck:
        def close(self):
            raise failure

    with pytest.raises(MemoryFailureError) as caught:
        worlds.close_rollout(EP, [Stuck(), a], wait_s=3.0)
    assert caught.value is failure
    with pytest.raises(MemoryClosedError):
        a.digest()                                                 # 后面的端口照样关了


def test_an_unbound_run_has_no_seed_and_cannot_open_a_rollout(worlds):
    with pytest.raises(KeyError):
        worlds.seed_of("run-2")
    with pytest.raises(KeyError):
        worlds.open_rollout("run-2", "s1", "run-2-grpo0-s1-r0", "pa")
    worlds.bind("run-2", None)
    assert worlds.seed_of("run-2") is None
    worlds.unbind("run-2")
    worlds.unbind("run-2")                                         # 解绑两次不报错
    with pytest.raises(KeyError):
        worlds.seed_of("run-2")


def test_closing_a_rollout_that_was_never_opened_is_fine(worlds):
    assert worlds.close_rollout("run-1-grpo0-s1-r9", [], wait_s=0.1) is True


# ---------------- 替身 ----------------

def test_the_stand_in_refuses_a_seed():
    worlds = StandInTrainingWorlds()
    worlds.bind("run-1", "s1")
    with pytest.raises(MemoryFailureError) as caught:
        worlds.open_rollout("run-1", "s1", EP, "pa")
    assert caught.value.transient is False
    worlds.bind("run-1", None)
    assert worlds.close_rollout(EP, [worlds.open_rollout("run-1", "s1", EP, "pa")], wait_s=1.0) is True


# ---------------- Starling ----------------

def _errors():
    from starling_client import errors
    return errors


class TimeoutOnceAfterCreating:
    """fork 在服务端照常做完,第一次却对客户端抛 service_timeout(超时发生在响应回来之前)。"""

    def __init__(self, session):
        self.session, self.fired, self.names = session, False, []

    def __call__(self, op, body):
        out = self.session.admin(op, body)
        if op == "fork":
            self.names.append(body["name"])
            if not self.fired:
                self.fired = True
                raise _errors().ServiceTimeout("service request timed out")
        return out


def _rollouts(session, prefix):
    return session.admin("list_worlds", {"labels": {"repo": prefix, "kind": "rollout"}, "page": 1, "page_size": 50,
                                         "with_digest": True})["worlds"]


def test_a_fork_that_timed_out_after_creating_its_world_is_reused_by_exact_name(starling_worlds):
    worlds, session, admin = starling_worlds(admin_wrap=TimeoutOnceAfterCreating)
    port = worlds.open_rollout("run-1", "s1", "run-1-grpo1-s1-r1", "pa")
    assert len(admin.names) == 1 and port._remote.world == admin.names[0]          # 只分叉一次
    assert [w["name"] for w in _rollouts(session, worlds.prefix)] == admin.names
    worlds.close_rollout("run-1-grpo1-s1-r1", [port])


def test_a_world_found_by_name_with_another_digest_is_not_reused(starling_worlds):
    def wrap(session):
        inner = TimeoutOnceAfterCreating(session)

        def written_meanwhile(op, body):
            out = inner(op, body)
            if op == "list_worlds":
                for item in out["worlds"]:
                    item["digest"] = "f" * 64                              # 这个名字的世界已有别的内容
            return out

        written_meanwhile.names = inner.names
        return written_meanwhile

    worlds, session, admin = starling_worlds(admin_wrap=wrap)
    port = worlds.open_rollout("run-1", "s1", "run-1-grpo1-s1-r1", "pa")
    assert len(admin.names) == 2 and port._remote.world == admin.names[1]          # 换后缀重建
    worlds.close_rollout("run-1-grpo1-s1-r1", [port])


def test_a_previous_processs_world_for_the_same_episode_is_never_claimed(starling_worlds):
    worlds, session, admin = starling_worlds(admin_wrap=TimeoutOnceAfterCreating)
    old, _, _ = starling_worlds(prefix=worlds.prefix)                    # 上一个进程:同 episode,attempt=1
    left = old.open_rollout("run-1", "s1", "run-1-grpo1-s1-r1", "pa")
    left.remember(MemoryEvent("run-1-grpo1-s1-r1#0@pa", "pb：上一个进程写的", "pb"))
    left.close()
    port = worlds.open_rollout("run-1", "s1", "run-1-grpo1-s1-r1", "pa")
    assert port._remote.world != left._remote.world and port.digest() == worlds.empty_seed[1]
    worlds.close_rollout("run-1-grpo1-s1-r1", [port])


def test_world_exists_takes_another_suffix_up_to_three_times(starling_worlds, monkeypatch):
    from factory import starling_names
    worlds, session, _ = starling_worlds()
    taken = starling_names.rollout_world(worlds.prefix, EP, "0" * 8)
    session.admin("fork", {"source": worlds.empty_seed[0], "name": taken, "labels": {"repo": "other"},
                           "reclaimable": True, "settings": worlds._settings})
    suffixes = iter(["0" * 8, "0" * 8, "1" * 8])
    monkeypatch.setattr(starling_names, "new_suffix", lambda: next(suffixes))
    port = worlds.open_rollout("run-1", "s1", EP, "pa")
    assert port._remote.world == starling_names.rollout_world(worlds.prefix, EP, "1" * 8)
    worlds.close_rollout(EP, [port])
    monkeypatch.setattr(starling_names, "new_suffix", lambda: "0" * 8)       # 每次都撞上同一个名字
    with pytest.raises(MemoryFailureError, match="fork failed 3 times") as info:
        worlds.open_rollout("run-1", "s1", EP, "pa")
    assert info.value.transient is False


def test_closing_a_rollout_only_closes_its_world_with_rise_labels(starling_worlds):
    worlds, session, _ = starling_worlds()
    port = worlds.open_rollout("run-1", "s1", "run-1-grpo1-s1-r1", "pa")
    name = port._remote.world
    assert worlds.close_rollout("run-1-grpo1-s1-r1", [port]) is True
    (item,) = [w for w in _rollouts(session, worlds.prefix) if w["name"] == name]
    assert item["reclaimable"] is True and item["retention_group"] == "run-1" and item["status"] == "closed"
    assert item["labels"] == {"repo": worlds.prefix, "root": worlds._root_label, "kind": "rollout",
                              "run_id": "run-1", "scenario_id": "s1", "episode_id": "run-1-grpo1-s1-r1",
                              "seed_id": "", "attempt": "1"}                # 世界还在,只是关了


def test_a_close_world_failure_is_only_a_warning_naming_the_world(starling_worlds, caplog):
    def wrap(session):
        def refuse_close(op, body):
            if op == "close_world":
                raise _errors().WorldError("world_busy", code="world_busy", transient=True)
            return session.admin(op, body)
        return refuse_close

    worlds, _, _ = starling_worlds(admin_wrap=wrap)
    port = worlds.open_rollout("run-1", "s1", EP, "pa")
    name = port._remote.world
    with caplog.at_level(logging.WARNING, logger="factory.memory_runtime"):
        assert worlds.close_rollout(EP, [port], wait_s=1.0) is True
    assert name in caplog.text and "world_busy" in caplog.text


def test_a_kept_world_is_logged_with_its_physical_name(starling_worlds, caplog):
    worlds, _, _ = starling_worlds()
    a = worlds.open_rollout("run-1", "s1", EP, "pa")
    b = worlds.join_rollout(EP, "pb")
    with caplog.at_level(logging.WARNING, logger="factory.memory_runtime"):
        assert worlds.close_rollout(EP, [a], wait_s=0.05) is False
    assert f"{EP} (world {a._remote.world}) kept" in caplog.text
    worlds.release_session(EP, [b])


def test_a_seeded_run_is_refused_until_step_5(starling_worlds):
    worlds, _, _ = starling_worlds()
    worlds.bind("run-2", "k")
    with pytest.raises(MemoryFailureError) as info:
        worlds.open_rollout("run-2", "s1", "run-2-grpo1-s1-r1", "pa")
    assert info.value.transient is False


def test_a_client_port_whose_call_has_not_returned_holds_the_world(starling_worlds):
    """close(timeout) 等不到在途调用:端口不算关上,世界留在保留表里,同名重试判瞬态;
    调用返回后再关一次端口,计数归零,重试从起点重建。"""
    worlds, _, _ = starling_worlds()
    fake = starling_worlds.fake
    a = worlds.open_rollout("run-1", "s1", EP, "pa")
    fake.chat_delay_s = 1.0
    t = threading.Thread(target=a.remember, args=(EV,))
    t.start()
    try:
        fake.wait_for_chat_call(timeout_s=5)
        with pytest.raises(MemoryFailureError):
            worlds.close_rollout(EP, [a], wait_s=3.0)
        with pytest.raises(MemoryFailureError) as held:
            worlds.open_rollout("run-1", "s1", EP, "pa")
        assert held.value.transient is True
    finally:
        t.join()
        fake.chat_delay_s = 0.0
    a.close()
    again = worlds.open_rollout("run-1", "s1", EP, "pa")
    assert again.digest() == worlds.empty_seed[1] and again._remote.world != a._remote.world
    assert worlds.close_rollout(EP, [again], wait_s=1.0) is True


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


def _info(scenario, assignment, *, session_id="sess-9", episode_id=EP, client_slot="a"):
    return SessionInfo(scenario, assignment, {}, client_slot, session_id, episode_id)


def test_a_registered_training_session_joins_the_rollout_world_and_closes_its_ports_with_the_session(
        worlds, store_with_scenario):
    store, scenario, assignment = store_with_scenario
    runtime = AgentRuntime(store, ("hi",), training_worlds=worlds)
    runtime.register_session(scenario.id, "run-1")
    a = worlds.open_rollout("run-1", scenario.id, EP, "pa")
    res = runtime._backends(_info(scenario, assignment))
    assert sorted(res.backends) == ["b", "c"]
    before = a.digest()
    res.backends["b"].instance.memory.remember(MemoryEvent(f"{EP}#0@pb", "pa：你好", "pa"))
    assert a.digest() != before                                    # 对手位写进的是同一个世界
    res.close()
    assert runtime._session_runs == {}
    assert worlds.close_rollout(EP, [a], wait_s=0.5) is True       # 会话关了,对手位的端口都已关上


def test_the_client_deletes_the_world_only_after_the_session_closed(worlds, store_with_scenario):
    store, scenario, assignment = store_with_scenario
    runtime = AgentRuntime(store, ("hi",), training_worlds=worlds)
    runtime.register_session(scenario.id, "run-1")
    a = worlds.open_rollout("run-1", scenario.id, EP, "pa")
    res = runtime._backends(_info(scenario, assignment))
    threading.Timer(0.3, res.close).start()
    assert worlds.close_rollout(EP, [a], wait_s=3.0) is True
    for slot in ("b", "c"):
        with pytest.raises(MemoryClosedError):
            res.backends[slot].instance.memory.digest()
    assert runtime._session_runs == {}


def test_a_training_session_without_its_rollout_world_fails_like_any_failed_build(worlds, store_with_scenario):
    """只有绕过 policy_for_rollout 直接开会话的调用方会落到这里:造后端失败、不留路由(服务端据此报 role_backend_failure)。"""
    store, scenario, assignment = store_with_scenario
    runtime = AgentRuntime(store, ("hi",), training_worlds=worlds)
    runtime.register_session(scenario.id, "run-1")
    with pytest.raises(MemoryFailureError) as caught:
        runtime._backends(_info(scenario, assignment))
    assert caught.value.transient is False
    assert runtime._session_runs == {}


def test_a_failure_while_building_closes_the_ports_already_joined_and_drops_the_route(
        worlds, store_with_scenario, monkeypatch):
    """工厂自己抛异常时不留半开的端口:c 的端口开不出来,b 的端口要关(计数归零,客户端能删世界),路由要撤,原异常原样抛出。"""
    store, scenario, assignment = store_with_scenario
    opened = []
    original = worlds.join_rollout

    def flaky(episode_id, holder):
        if holder == "pc":
            raise RuntimeError("no port for pc")
        port = original(episode_id, holder)
        opened.append(port)
        return port

    monkeypatch.setattr(worlds, "join_rollout", flaky)
    runtime = AgentRuntime(store, ("hi",), training_worlds=worlds)
    runtime.register_session(scenario.id, "run-1")
    a = worlds.open_rollout("run-1", scenario.id, EP, "pa")
    with pytest.raises(RuntimeError, match="no port for pc"):
        runtime._backends(_info(scenario, assignment))
    assert len(opened) == 1
    with pytest.raises(MemoryClosedError):
        opened[0].digest()
    assert runtime._session_runs == {}
    assert worlds.close_rollout(EP, [a], wait_s=0.5) is True


def test_an_unregistered_deployment_session_keeps_empty_tuple_opponents(worlds, store_with_scenario):
    store, scenario, assignment = store_with_scenario
    runtime = AgentRuntime(store, ("hi",), training_worlds=worlds)
    res = runtime._backends(_info(scenario, assignment, session_id="sess-d", episode_id="ep-sess-d"))
    assert all(type(b.instance.memory) is TupleMemory for b in res.backends.values())
    res.close()


def test_closing_the_session_ports_never_waits_for_the_runtime_lock(worlds, store_with_scenario):
    """服务端关对手位端口跑在处理器线程的收尾里,那时 runtime.close() 可能正持着 _lock 等处理器退出:
    关端口(连同计数减一、唤醒客户端)不能取 _lock。"""
    store, scenario, assignment = store_with_scenario
    runtime = AgentRuntime(store, ("hi",), training_worlds=worlds)
    runtime.register_session(scenario.id, "run-1")
    a = worlds.open_rollout("run-1", scenario.id, EP, "pa")
    res = runtime._backends(_info(scenario, assignment))
    worker = threading.Thread(target=res.close, daemon=True)
    with runtime._lock:
        worker.start()
        worker.join(timeout=5.0)
        finished = not worker.is_alive()
    worker.join(timeout=5.0)
    assert finished, "closing the session ports blocked on the runtime lock"
    assert worlds.close_rollout(EP, [a], wait_s=0.5) is True
