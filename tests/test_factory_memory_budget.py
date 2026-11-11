"""记忆时间预算(factory.memory_budget;90 篇时间预算表、FC-50、FC-51)。纯函数,不起服务。"""
import dataclasses
from pathlib import Path

import pytest

from factory.memory_budget import (
    BASE_LAYERS,
    EXTRACTION_CALLS_PER_REMEMBER,
    call_ms,
    embed_pending_calls,
    endpoint_call_ms,
    memory_time_budget,
    memory_worst_case,
)
from factory.memory_config import DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT_MS, MAX_MEMORY_CALL_MS, MemoryConfig
from factory.starling_names import rise_settings

# 8787 现在的两个角色端点(rise 设置里写出的叶子):抽取 timeout_ms 120000、max_retries 没写;嵌入 timeout_ms 0、没写
EXTRACTION_8787 = {"provider": "openai", "provider_name": "default", "model": "deepseek-v4-pro", "timeout_ms": 120000}
EMBEDDING_8787 = {"provider": "openai", "provider_name": "embedder", "model": "text-embedding-v4", "dim": 1024,
                  "timeout_ms": 0}


@pytest.mark.parametrize("endpoint, want", [
    ({"timeout_ms": 4000, "max_retries": 0}, 4000),                       # 一次,不退避
    ({"timeout_ms": 2000, "max_retries": 1}, 2 * 2000 + 1000),            # 两次加 1 秒
    ({"timeout_ms": 1000, "max_retries": 3}, 4 * 1000 + 1000 + 2000 + 4000),
    ({"timeout_ms": 120000}, 4 * 120000 + 7000),                          # max_retries 缺取 3
    ({"timeout_ms": 0}, 4 * 60000 + 7000),                                # timeout_ms 为 0 取 60 秒
    ({}, 4 * 60000 + 7000),
    ({"timeout_ms": 10, "max_retries": 7}, 8 * 10 + 1000 + 2000 + 4000 + 8000 + 16000 + 30000 + 30000),   # 封顶 30 秒
    ({"provider": "anthropic", "timeout_ms": 1000, "max_retries": 0}, 4 * 60000 + 7000),   # anthropic 不读这两项
])
def test_one_http_call_worst_case_is_attempts_times_timeout_plus_backoff(endpoint, want):
    assert endpoint_call_ms(endpoint) == want


def test_a_negative_leaf_is_refused():
    with pytest.raises(ValueError):
        endpoint_call_ms({"timeout_ms": 1000, "max_retries": -1})


@pytest.mark.parametrize("timeout_ms, max_retries, want", [
    (4000, 0, 4000),
    (1500, 1, 4000),                                        # 2 × 1500 + 1000
    (333, 2, 3999),                                         # 3 × 333 + 1000 + 2000
    (1, 5, 6 * 1 + 1000 + 2000 + 4000 + 8000 + 16000),      # 退避还没到顶
    (1, 6, 7 * 1 + 1000 + 2000 + 4000 + 8000 + 16000 + 30000),   # 第 6 次重试之前的退避 32 秒,封顶 30 秒
])
def test_call_ms_is_attempts_times_timeout_plus_capped_backoff(timeout_ms, max_retries, want):
    assert call_ms(timeout_ms, max_retries) == want
    assert endpoint_call_ms({"timeout_ms": timeout_ms, "max_retries": max_retries}) == want    # 端点走同一处算术


def test_call_ms_matches_the_term_by_term_sum_and_does_not_loop_over_a_huge_retry_count():
    for t in (1, 10, 4000):
        for r in range(60):
            assert call_ms(t, r) == (r + 1) * t + sum(min(1000 * 2 ** i, 30000) for i in range(r))
    # 前五次重试退避 1 + 2 + 4 + 8 + 16 = 31 秒,第六次起每次 30 秒(32 秒封顶),一次加完
    assert call_ms(1, 10 ** 12) == (10 ** 12 + 1) + 31000 + 30000 * (10 ** 12 - 5)


@pytest.mark.parametrize("timeout_ms, max_retries", [(-1, 0), (1000, -1)])
def test_call_ms_refuses_a_negative_argument(timeout_ms, max_retries):
    with pytest.raises(ValueError):
        call_ms(timeout_ms, max_retries)


def test_remember_event_is_seven_extraction_calls_and_embed_pending_is_four_batched_calls():
    assert EXTRACTION_CALLS_PER_REMEMBER == 3 + 3 + 1
    assert embed_pending_calls() == 4                                     # ceil(32 / 10)


def test_the_worst_case_for_the_8787_endpoints():
    w = memory_worst_case(EXTRACTION_8787, EMBEDDING_8787)
    assert (w.extraction_call_s, w.embedding_call_s) == (487.0, 247.0)
    assert (w.recall_s, w.remember_s, w.embed_pending_s) == (247.0, 3409.0, 988.0)
    assert w.handle_call_s == 3409.0
    assert w.memory_turn_s == 247.0 + 2 * 3409.0 + 988.0 == 8053.0


def test_the_layers_for_the_8787_endpoints_each_strictly_contain_the_one_inside():
    b = memory_time_budget(EXTRACTION_8787, EMBEDDING_8787)
    assert b.derived and b.violations() == []
    assert (b.handle_s, b.drain_window_s, b.port_close_s) == (3439.0, 3449.0, 3449.0)
    assert b.deploy_turn_s == 8073.0 and b.deploy_per_turn_s == 8123.0
    assert (b.backend_s, b.interact_turn_s, b.message_s) == (8123.0, 8183.0, 8153.0)
    assert b.per_turn_deadline_s == 8183.0
    assert (b.join_s, b.close_wait_s, b.admission_wait_s) == (8133.0, 8143.0, 8138.0)
    w = b.worst
    assert b.handle_s > w.handle_call_s and b.drain_window_s > b.handle_s and b.port_close_s > b.handle_s
    assert b.deploy_turn_s > w.memory_turn_s
    assert b.backend_s > b.opponent_s + w.memory_turn_s and b.interact_turn_s > b.policy_s + w.memory_turn_s
    assert b.message_s > b.backend_s and b.per_turn_deadline_s >= max(b.interact_turn_s, b.backend_s)
    assert b.join_s > max(b.backend_s, b.handle_s)
    assert b.close_wait_s > b.join_s and b.admission_wait_s > b.join_s


_LAYERS = ("handle_s", "drain_window_s", "port_close_s", "deploy_turn_s", "deploy_per_turn_s", "backend_s",
           "interact_turn_s", "message_s", "per_turn_deadline_s", "join_s", "close_wait_s", "admission_wait_s")


def test_fast_endpoints_never_shrink_a_layer_below_the_base_constant():
    fast = {"timeout_ms": 100, "max_retries": 0}
    b = memory_time_budget(fast, fast)
    for field in _LAYERS:
        assert getattr(b, field) >= getattr(BASE_LAYERS, field), field
    assert b.violations() == []


def _spawn_budget(**call):
    """拉起模式:按 rise 自己写进 rise 设置的两个端点推出(与 `open_memory_service` 同一条路)。"""
    config = MemoryConfig(root=Path("/mem"), extraction_base_url="http://127.0.0.1:9/v1", extraction_model="x",
                          embedding_base_url="http://127.0.0.1:9/v1", embedding_model="e", embedding_dim=8, **call)
    assert config.mode == "spawn"
    settings = rise_settings(config)
    return memory_time_budget(settings["extraction"], settings["embedding"])


def _assert_every_layer_strictly_contains_the_one_inside(b):
    w = b.worst
    assert b.derived and b.violations() == []
    assert b.handle_s > w.handle_call_s and b.drain_window_s > b.handle_s and b.port_close_s > b.handle_s
    assert b.deploy_turn_s > w.memory_turn_s
    assert b.backend_s > b.opponent_s + w.memory_turn_s and b.interact_turn_s > b.policy_s + w.memory_turn_s
    assert b.message_s > b.backend_s
    assert b.per_turn_deadline_s >= max(b.interact_turn_s, b.backend_s)
    assert b.deploy_per_turn_s >= max(b.deploy_turn_s, b.backend_s)
    assert b.join_s > max(b.backend_s, b.handle_s)
    assert b.close_wait_s > b.join_s and b.admission_wait_s > b.join_s
    for field in _LAYERS:
        assert getattr(b, field) >= getattr(BASE_LAYERS, field), field          # 不比接服务之前短


def test_spawn_mode_at_the_default_config_derives_layers_that_each_contain_the_one_inside():
    """拉起模式缺省 timeout_ms 4000、max_retries 0。推出的数(秒):

    | 层 | 值 | 算法 |
    |---|---|---|
    | 抽取一次 E、嵌入一次 M | 4、4 | 一次尝试,不退避 |
    | 一次句柄调用、一轮记忆 | 28、76 | max(M, 7E, 4M);M + 2 × 7E + 4M |
    | 句柄超时 | 58 | 28 + 30 |
    | 排空窗口、关端口 | 68、68 | 58 + 10 |
    | 部署等提交 | 96 | 76 + 20(原 40,不包住 76) |
    | 后端调用 | 146 | 60 + 76 + 10 |
    | 训练位等提交 | 206 | 120 + 76 + 10 |
    | 相邻消息 | 176 | 146 + 30 |
    | 训练流每轮、部署流每轮 | 206、146 | 较大者 |
    | 收尾 join | 156 | max(146, 58) + 10(原 60,低于后端 90) |
    | 等端口全关、准入退避 | 166、161 | join + 10、join + 5 |"""
    b = _spawn_budget()
    assert (DEFAULT_TIMEOUT_MS, DEFAULT_MAX_RETRIES) == (4000, 0)
    w = b.worst
    assert (w.extraction_call_s, w.embedding_call_s, w.handle_call_s, w.memory_turn_s) == (4.0, 4.0, 28.0, 76.0)
    assert (b.handle_s, b.drain_window_s, b.port_close_s) == (58.0, 68.0, 68.0)
    assert (b.deploy_turn_s, b.deploy_per_turn_s) == (96.0, 146.0)
    assert (b.opponent_s, b.policy_s) == (60.0, 120.0)
    assert (b.backend_s, b.interact_turn_s, b.message_s, b.per_turn_deadline_s) == (146.0, 206.0, 176.0, 206.0)
    assert (b.join_s, b.close_wait_s, b.admission_wait_s) == (156.0, 166.0, 161.0)
    _assert_every_layer_strictly_contains_the_one_inside(b)


@pytest.mark.parametrize("timeout_ms, max_retries, call_s, turn_s, deploy_turn_s, join_s", [
    # 记忆配置的上限是 call_ms(timeout_ms, max_retries) ≤ 4000,重试之间的退避计在内;一次调用最坏不超过 4 秒,
    # 一轮记忆不超过缺省配置的 76 秒。max_retries 至多 2(三次重试仅退避就 7 秒),timeout_ms 随之收紧:
    (1500, 1, 4.0, 76.0, 96.0, 156.0),                      # 2 × 1.5 秒 + 退避 1 秒,恰在上限上,与缺省配置同值
    (333, 2, 3.999, 75.981, 95.981, 155.981),               # 3 × 0.333 秒 + 退避 1 + 2 秒,max_retries 取最大时 timeout_ms 的最大值
    (1, 2, 3.003, 57.057, 77.057, 137.057),                 # 3 × 1 毫秒 + 退避 3 秒
])
def test_spawn_mode_at_the_config_maximum_still_derives_layers_that_each_contain_the_one_inside(
        timeout_ms, max_retries, call_s, turn_s, deploy_turn_s, join_s):
    assert call_ms(timeout_ms, max_retries) <= MAX_MEMORY_CALL_MS
    b = _spawn_budget(timeout_ms=timeout_ms, max_retries=max_retries)
    assert b.worst.extraction_call_s == b.worst.embedding_call_s == pytest.approx(call_s)
    assert b.worst.memory_turn_s == pytest.approx(turn_s)
    assert (b.deploy_turn_s, b.join_s) == pytest.approx((deploy_turn_s, join_s))
    _assert_every_layer_strictly_contains_the_one_inside(b)


def test_a_broken_containment_is_reported_naming_both_layers():
    b = dataclasses.replace(memory_time_budget(EXTRACTION_8787, EMBEDDING_8787), join_s=1.0)
    problems = b.violations()
    assert "join_s (1s) must exceed backend_s (8123s)" in problems
    assert "close_wait_s (8143s) must exceed join_s (1s)" not in problems         # 外层大于 1 秒,这条成立


def test_describe_names_the_mode_and_every_layer():
    text = memory_time_budget(EXTRACTION_8787, EMBEDDING_8787).describe()
    assert "derived from the memory service's endpoints" in text and "memory turn worst case 8053s" in text
    assert "join 8133s" in text and "admission wait 8138s" in text
    assert "fixed stand-in layers" in BASE_LAYERS.describe()


def test_the_base_layers_are_the_constants_the_wiring_used_before():
    """BASE_LAYERS 与各模块里的常量是同一组数:替身的行为不变,推出的各层也以它们为下限。"""
    import factory.interact_policy as ip
    import factory.starling_session as ss
    import factory.vllm_policy as vp
    import factory.wire as wire
    from mindagent import deployment
    from mindtrainer import trajectory_source as ts
    from simulator.realtime.server import create_server
    import inspect

    join = inspect.signature(create_server).parameters["resource_join_timeout_s"].default
    assert (BASE_LAYERS.policy_s, BASE_LAYERS.opponent_s) == (vp.POLICY_TIMEOUT_S, ip.OPPONENT_TIMEOUT_S)
    assert (BASE_LAYERS.interact_turn_s, BASE_LAYERS.backend_s, BASE_LAYERS.message_s) == (
        wire.INTERACT_TURN_TIMEOUT_S, wire.INTERACT_BACKEND_TIMEOUT_S, wire.INTERACT_MESSAGE_TIMEOUT_S)
    assert (BASE_LAYERS.handle_s, BASE_LAYERS.drain_window_s) == (ss.HANDLE_TIMEOUT_S, ss.DRAIN_WINDOW_S)
    assert BASE_LAYERS.per_turn_deadline_s == ts.PER_TURN_DEADLINE_S
    assert (BASE_LAYERS.join_s, BASE_LAYERS.close_wait_s, BASE_LAYERS.admission_wait_s) == (
        join, deployment.SESSION_CLOSE_WAIT_S, deployment.ADMISSION_WAIT_S)
    assert ts.ADMISSION_WAIT_S == deployment.ADMISSION_WAIT_S
