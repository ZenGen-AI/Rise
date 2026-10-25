"""推理服务的生命周期(设计 DX-13 到 DX-15):取租约、起新作业、等就绪、收尾顺序、失败也收尾。

全部用替身:客户端、隧道、健康探针都是注入的,不连任何远端。
"""
from __future__ import annotations

import pytest
from compute_fixtures import fake_endpoint

from compute.client import JobSpec, JobStatus
from compute.errors import ComputeBusyError, DeferredError, ServeStartupError
from compute.service import MAX_ATTEMPTS, ServiceHandle, serve
from compute.vllm_service import VllmServeConfig, build_serve_job

CFG = VllmServeConfig(model_path="/data/m", adapters=(("forge", "/data/a"),), remote_port=18081)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeServiceClient:
    """记下每次调用的顺序,并按脚本推进作业状态。"""

    def __init__(self, *, lease_free=True, states=None, stops=True):
        self.endpoint = fake_endpoint()
        self.calls: list[tuple] = []
        self.lease_free = lease_free
        self.jobs: dict[str, JobStatus] = {}
        self.script = list(states or ["running"])   # status() 依次返回这些状态,用完停在最后一个
        self.stops = stops                           # False:stop 之后作业仍显示 running
        self.started: list[tuple[str, JobSpec, str | None]] = []
        self.log = "server log line\n"

    def lease_acquire(self, holder, *, grace_s=None):
        self.calls.append(("lease_acquire", holder))
        return self.lease_free

    def lease_release(self, holder):
        self.calls.append(("lease_release", holder))
        return True

    def lease_holder(self):
        return "someone-else", False

    def status(self, job_id):
        self.calls.append(("status", job_id))
        if job_id not in self.jobs:
            return JobStatus("missing", None, None)
        if self.script:
            state = self.script.pop(0) if len(self.script) > 1 else self.script[0]
            return JobStatus(state, 1 if state == "exited" else None, None)
        return self.jobs[job_id]

    def start(self, job_id, spec, *, secret=None):
        self.calls.append(("start", job_id))
        self.started.append((job_id, spec, secret))
        self.jobs[job_id] = JobStatus("running", None, None)
        return self.jobs[job_id]

    def tail(self, job_id, lines=50):
        self.calls.append(("tail", job_id))
        return self.log

    def stop(self, job_id, *, grace_s=10.0):
        self.calls.append(("stop", job_id))
        state = "exited" if self.stops else "running"
        self.jobs[job_id] = JobStatus(state, 0 if state == "exited" else None, None)
        self.script = [state]
        return self.jobs[job_id]


class FakeTunnel:
    def __init__(self, trace, *, fails=False):
        self.trace, self.fails = trace, fails
        self.local_port = 49152

    def __enter__(self):
        self.trace.append(("tunnel_open",))
        if self.fails:
            raise OSError("cannot open tunnel")
        return self

    def __exit__(self, *exc):
        self.trace.append(("tunnel_close",))
        return False


def _run(client, *, healthy_after=1, clock=None, tunnel_fails=False, timeout_s=300.0, body=None, cfg=CFG):
    clock = clock or Clock()
    probes = []

    def probe(url, key):
        probes.append((url, key))
        return len(probes) >= healthy_after

    seen = {}
    with serve(
        client, cfg, build_job=build_serve_job, tunnel_factory=lambda: FakeTunnel(client.calls, fails=tunnel_fails),
        probe=probe, startup_timeout_s=timeout_s, poll_s=5.0, clock=clock, sleep=clock.sleep,
    ) as handle:
        seen["handle"] = handle
        if body:
            body(handle)
    return seen.get("handle"), probes


def _order(client):
    return [c[0] for c in client.calls if c[0] in {"lease_acquire", "start", "tunnel_open", "tunnel_close", "stop", "lease_release"}]


def test_success_path_acquires_starts_serves_then_tears_down_in_order():
    client = FakeServiceClient()
    handle, probes = _run(client)
    assert isinstance(handle, ServiceHandle)
    assert _order(client) == ["lease_acquire", "start", "tunnel_open", "tunnel_close", "stop", "lease_release"]


def test_the_handle_gives_a_local_address_and_the_key_for_this_start():
    client = FakeServiceClient()
    seen = {}
    _run(client, body=lambda h: seen.update(url=h.base_url, key=h.api_key, models=h.model_names))
    assert seen["url"] == "http://127.0.0.1:49152"
    assert seen["key"] and len(seen["key"]) >= 24
    assert seen["models"] == ("base", "forge")


def test_the_handle_lists_the_base_and_every_adapter_the_config_serves():
    two = VllmServeConfig(model_path="/data/m", adapters=(("trained", "/data/a"), ("opponent", "/data/b")),
                          remote_port=18081)
    seen = {}
    _run(FakeServiceClient(), cfg=two, body=lambda h: seen.update(models=h.model_names))
    assert seen["models"] == ("base", "trained", "opponent")
    bare = VllmServeConfig(model_path="/data/m", adapters=(), remote_port=18081)
    _run(FakeServiceClient(), cfg=bare, body=lambda h: seen.update(models=h.model_names))
    assert seen["models"] == ("base",)


def test_the_same_key_goes_to_the_server_and_to_the_health_probe():
    client = FakeServiceClient()
    _, probes = _run(client)
    _, spec, secret = client.started[0]
    assert secret and probes[0][1] == secret
    assert secret not in spec.argv and secret not in str(spec.env)      # 不进命令行与环境
    assert secret not in client.log                                      # 也不会被我们自己写进日志


def test_every_start_uses_a_fresh_key():
    first, second = FakeServiceClient(), FakeServiceClient()
    _run(first); _run(second)
    assert first.started[0][2] != second.started[0][2]


def test_a_busy_gpu_lease_fails_before_anything_is_started():
    client = FakeServiceClient(lease_free=False)
    with pytest.raises(ComputeBusyError):
        _run(client)
    assert _order(client) == ["lease_acquire"]           # 没起作业,也没放一个不是自己的租约


def test_the_job_id_is_a_fresh_attempt_never_a_running_one():
    """与训练执行器相反:密钥每次现生成,接上一个旧服务就是接上一个认证不了的东西(DX-14)。"""
    client = FakeServiceClient()
    client.jobs["serve-a"] = JobStatus("running", None, None)   # 占个位,下面按真实 id 重放
    first = FakeServiceClient()
    _run(first)
    base_id = first.started[0][0].rsplit("-a", 1)[0]
    taken = FakeServiceClient()
    for n in (1, 2):
        taken.jobs[f"{base_id}-a{n}"] = JobStatus("running", None, None)
    taken.script = ["running"]
    _run(taken)
    assert taken.started[0][0] == f"{base_id}-a3"                 # 跳过正在跑的 a1、a2


def test_all_attempt_slots_taken_is_a_deferred_failure_with_a_clear_reason():
    """租约持有者必须是真实的作业 id(助手靠它判断租约是否陈旧),所以先挑好尝试序号、再取租约。
    槽位用尽发生在取租约之前:没有租约可放,也不该为一个起不来的服务去占 GPU。"""
    client = FakeServiceClient()
    first = FakeServiceClient(); _run(first)
    base_id = first.started[0][0].rsplit("-a", 1)[0]
    for n in range(1, MAX_ATTEMPTS + 1):
        client.jobs[f"{base_id}-a{n}"] = JobStatus("exited", 1, None)
    with pytest.raises(ServeStartupError, match=r"attempt"):
        _run(client)
    assert _order(client) == []


def test_an_early_exit_is_reported_at_once_not_after_the_timeout():
    client = FakeServiceClient(states=["running", "exited"])
    clock = Clock()
    with pytest.raises(ServeStartupError, match=r"exited"):
        _run(client, healthy_after=999, clock=clock, timeout_s=3000.0)
    assert clock.now < 100                                  # 没有傻等到 3000 秒


def test_startup_timeout_is_a_deferred_error_and_still_cleans_up():
    client = FakeServiceClient()
    with pytest.raises(ServeStartupError, match=r"not ready within") as caught:
        _run(client, healthy_after=999, timeout_s=60.0)
    assert isinstance(caught.value, DeferredError)          # 守护进程按退避重试,不写树
    assert _order(client) == ["lease_acquire", "start", "tunnel_open", "tunnel_close", "stop", "lease_release"]


def test_the_log_tail_rides_on_the_exception_not_in_its_reason():
    client = FakeServiceClient()
    client.log = "Traceback ... prompt: 这里可能有请求内容\n"
    with pytest.raises(ServeStartupError) as caught:
        _run(client, healthy_after=999, timeout_s=30.0)
    assert "请求内容" in caught.value.log_tail
    assert "请求内容" not in caught.value.reason and "请求内容" not in str(caught.value)


def test_a_tunnel_that_cannot_open_is_a_deferred_failure_and_stops_the_server():
    client = FakeServiceClient()
    with pytest.raises(ServeStartupError, match=r"tunnel"):
        _run(client, tunnel_fails=True)
    assert "stop" in _order(client) and _order(client)[-1] == "lease_release"


def test_teardown_runs_even_when_the_caller_body_raises():
    client = FakeServiceClient()

    def boom(handle):
        raise RuntimeError("the evaluation crashed")

    with pytest.raises(RuntimeError, match="evaluation crashed"):
        _run(client, body=boom)
    assert _order(client)[-3:] == ["tunnel_close", "stop", "lease_release"]


def test_a_failing_stop_does_not_release_the_lease_while_the_server_may_still_hold_the_gpu():
    """DX-15:放租约前要核对作业确已不在运行。stop 之后作业仍是 running,就不能把 GPU 交出去。"""
    client = FakeServiceClient(stops=False)
    with pytest.raises(ServeStartupError, match=r"still running"):
        _run(client)
    assert "lease_release" not in _order(client)


def test_a_start_call_that_fails_midway_still_stops_the_job_it_may_have_launched():
    """start 是个 SSH 调用:远端已经起了进程、网络才断,调用方看到的是 TransportError,
    而服务其实在跑、占着 GPU。所以 start 抛了也要 stop 一次(对不存在的作业是空操作)。"""
    from compute.errors import TransportError

    client = FakeServiceClient()

    def flaky_start(job_id, spec, *, secret=None):
        client.calls.append(("start", job_id))
        client.jobs[job_id] = JobStatus("running", None, None)   # 远端确实起来了
        raise TransportError("ssh dropped after the helper had already launched the job")

    client.start = flaky_start
    with pytest.raises(TransportError):
        _run(client)
    assert _order(client) == ["lease_acquire", "start", "stop", "lease_release"]
    assert client.jobs[next(iter(client.jobs))].state == "exited"     # 那个可能起来了的作业被停掉了


def test_a_failing_tunnel_close_does_not_block_stopping_the_server():
    """任何一步收尾失败都不能挡住后面几步(DX-15)。"""
    client = FakeServiceClient()

    class BadClose(FakeTunnel):
        def __exit__(self, *exc):
            self.trace.append(("tunnel_close",))
            raise OSError("ssh would not die")

    clock = Clock()
    with serve(client, CFG, build_job=build_serve_job, tunnel_factory=lambda: BadClose(client.calls),
               probe=lambda u, k: True, startup_timeout_s=60.0, poll_s=5.0, clock=clock, sleep=clock.sleep):
        pass
    assert _order(client)[-2:] == ["stop", "lease_release"]


def test_the_lease_holder_is_the_job_id():
    client = FakeServiceClient()
    _run(client)
    holder = next(c[1] for c in client.calls if c[0] == "lease_acquire")
    assert holder == client.started[0][0]


# ---------------- 起服务前的显存核算(不占租约) ----------------

class SizingClient(FakeServiceClient):
    def __init__(self, *, card_gib=79.17, cards=8, **kw):
        super().__init__(**kw)
        self._card_gib, self._cards = card_gib, cards

    def gpu_memory_gib(self):
        return self._card_gib, self._cards


def _sized_cfg(**over):
    from dataclasses import replace

    return replace(CFG, model_weights_gib=335.276, **over)


def test_a_model_that_cannot_fit_fails_before_the_lease_is_taken():
    """真机实测的那组参数(335 GiB / 8 卡 / 0.85)要在这里被拦下:不占租约、不起作业。"""
    from compute.errors import ComputeError

    client = SizingClient()
    with pytest.raises(ComputeError, match="does not fit"):
        with serve(client, _sized_cfg(), build_job=build_serve_job, tunnel_factory=lambda: FakeTunnel(client.calls),
                   probe=lambda u, k: True, startup_timeout_s=60.0, poll_s=5.0,
                   clock=Clock(), sleep=Clock().sleep):
            pass
    assert _order(client) == []


def test_a_sizing_that_fits_starts_normally():
    client = SizingClient()
    _run_sized(client)
    assert _order(client)[0] == "lease_acquire"


def test_the_sizing_is_skipped_when_the_weight_size_is_unknown():
    client = FakeServiceClient()          # 没有 gpu_memory_gib
    _run(client)                          # CFG 没有 model_weights_gib,预检直接跳过
    assert _order(client)[0] == "lease_acquire"


def test_fewer_cards_than_tensor_parallel_is_refused():
    from compute.errors import ComputeError

    client = SizingClient(cards=4)
    with pytest.raises(ComputeError, match="4 GPU"):
        _run_sized(client)
    assert _order(client) == []


def _run_sized(client):
    clock = Clock()
    with serve(client, _sized_cfg(enforce_eager=True, gpu_memory_utilization=0.95), build_job=build_serve_job,
               tunnel_factory=lambda: FakeTunnel(client.calls), probe=lambda u, k: True,
               startup_timeout_s=60.0, poll_s=5.0, clock=clock, sleep=clock.sleep):
        pass
