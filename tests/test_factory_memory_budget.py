"""记忆时间预算(factory.memory_budget;90 篇时间预算表、FC-50、FC-51)。纯函数,不起服务。"""
import dataclasses

import pytest

from factory.memory_budget import (
    EXTRACTION_CALLS_PER_REMEMBER,
    SPAWN_LAYERS,
    embed_pending_calls,
    endpoint_call_ms,
    memory_time_budget,
    memory_worst_case,
    spawn_time_budget,
)

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


def test_fast_endpoints_never_shrink_a_layer_below_the_spawn_constant():
    fast = {"timeout_ms": 100, "max_retries": 0}
    b = memory_time_budget(fast, fast)
    for field in ("handle_s", "deploy_turn_s", "backend_s", "interact_turn_s", "message_s", "per_turn_deadline_s",
                  "join_s", "close_wait_s", "admission_wait_s"):
        assert getattr(b, field) >= getattr(SPAWN_LAYERS, field), field
    assert b.violations() == []


def test_spawn_mode_keeps_the_fixed_layers_and_only_reports_the_worst_case():
    spawn = {"timeout_ms": 4000, "max_retries": 0}
    b = spawn_time_budget(spawn, spawn)
    assert not b.derived and dataclasses.replace(b, worst=None) == SPAWN_LAYERS
    assert (b.handle_s, b.deploy_turn_s, b.backend_s, b.interact_turn_s, b.message_s, b.per_turn_deadline_s,
            b.join_s, b.close_wait_s, b.admission_wait_s) == (30.0, 40.0, 90.0, 150.0, 120.0, 150.0, 60.0, 70.0, 65.0)
    assert b.worst.memory_turn_s == 4 + 2 * 28 + 16 == 76.0


def test_the_fixed_spawn_layers_break_two_containments_that_external_mode_keeps():
    """拉起模式的常量是接服务之前定的:按真公式一轮记忆最坏 76 秒,超过部署等提交 40 秒;收尾 join 60 秒低于后端 90 秒。
    外部模式按公式推出,两条都成立。这里钉住现状,改常量时要一起改 90 篇的时间预算表。"""
    spawn = {"timeout_ms": 4000, "max_retries": 0}
    problems = spawn_time_budget(spawn, spawn).violations()
    assert any(p.startswith("deploy_turn_s (40s)") for p in problems)
    assert any(p.startswith("join_s (60s) must exceed backend_s (90s)") for p in problems)
    assert memory_time_budget(spawn, spawn).violations() == []


def test_a_broken_containment_is_reported_naming_both_layers():
    b = dataclasses.replace(memory_time_budget(EXTRACTION_8787, EMBEDDING_8787), join_s=1.0)
    problems = b.violations()
    assert "join_s (1s) must exceed backend_s (8123s)" in problems
    assert "close_wait_s (8143s) must exceed join_s (1s)" not in problems         # 外层大于 1 秒,这条成立


def test_describe_names_the_mode_and_every_layer():
    text = memory_time_budget(EXTRACTION_8787, EMBEDDING_8787).describe()
    assert "derived from the memory service's endpoints" in text and "memory turn worst case 8053s" in text
    assert "join 8133s" in text and "admission wait 8138s" in text
    assert "fixed spawn-mode layers" in SPAWN_LAYERS.describe()


def test_the_spawn_layers_are_the_constants_the_wiring_used_before():
    """SPAWN_LAYERS 与各模块里的常量是同一组数:拉起模式与替身的行为不变。"""
    import factory.interact_policy as ip
    import factory.starling_session as ss
    import factory.vllm_policy as vp
    import factory.wire as wire
    from mindagent import deployment
    from mindtrainer import trajectory_source as ts
    from simulator.realtime.server import create_server
    import inspect

    join = inspect.signature(create_server).parameters["resource_join_timeout_s"].default
    assert (SPAWN_LAYERS.policy_s, SPAWN_LAYERS.opponent_s) == (vp.POLICY_TIMEOUT_S, ip.OPPONENT_TIMEOUT_S)
    assert (SPAWN_LAYERS.interact_turn_s, SPAWN_LAYERS.backend_s, SPAWN_LAYERS.message_s) == (
        wire.INTERACT_TURN_TIMEOUT_S, wire.INTERACT_BACKEND_TIMEOUT_S, wire.INTERACT_MESSAGE_TIMEOUT_S)
    assert (SPAWN_LAYERS.handle_s, SPAWN_LAYERS.drain_window_s) == (ss.HANDLE_TIMEOUT_S, ss.DRAIN_WINDOW_S)
    assert SPAWN_LAYERS.per_turn_deadline_s == ts.PER_TURN_DEADLINE_S
    assert (SPAWN_LAYERS.join_s, SPAWN_LAYERS.close_wait_s, SPAWN_LAYERS.admission_wait_s) == (
        join, deployment.SESSION_CLOSE_WAIT_S, deployment.ADMISSION_WAIT_S)
    assert ts.ADMISSION_WAIT_S == deployment.ADMISSION_WAIT_S
