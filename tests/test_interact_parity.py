"""被复制或各写一份的表与判法,钉在一起(--interact 的最终审查)。

这几处分属不同的包,按依赖方向不能互相导入(trainscheduler 不得导入 simulator;mindtrainer 的派发器、
factory 的策略工厂与对手解析各自判一遍"是不是适配器导出"),代码里只能靠注释叮嘱"改这里要一起改"。
本文件是测试,可以导入所有的包:一边改了另一边没改,这里就红。
"""
from __future__ import annotations

import pytest
from factory_fixtures import closed_loop_repo

from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.paths import model_file
from factory.interact_policy import _is_adapter_export as policy_is_adapter_export
from factory.opponents import ModelRefError, driver_for, resolve_model_ref
from factory.sombench_runner import EXPORT_FORMAT as RUNNER_EXPORT_FORMAT
from mindagent.deployment import TRANSIENT_REASONS as DEPLOYMENT_TRANSIENT_REASONS
from mindtrainer.dispatch import _DEFAULT_OPPONENT_DRIVER
from mindtrainer.dispatch import _is_adapter_export as dispatch_is_adapter_export
from mindtrainer.roll_converter import EXPORT_FORMAT as CONVERTER_EXPORT_FORMAT
from mindtrainer.training_config import OPPONENT_MODEL_BACKENDS
from mindtrainer.trajectory_source import _TRANSIENT_REASONS as TRAJECTORY_TRANSIENT_REASONS
from simulator.datamodel import OPPONENT_DRIVERS, RoleBinding
from trainscheduler.config import _SLOT_DRIVERS


def test_the_scheduler_copy_of_the_slot_drivers_equals_the_simulator_table():
    assert set(_SLOT_DRIVERS) == set(OPPONENT_DRIVERS)


@pytest.mark.parametrize("backend", sorted(_DEFAULT_OPPONENT_DRIVER))
def test_the_dispatchers_default_driver_agrees_with_driver_for(backend):
    """派发器记进 training_run 的对手驱动(没写 driver 时按 model_backend 取默认)与对手分派用的是同一个答案。"""
    binding = RoleBinding("b", "pb", backend, model_ref="zengen-v1")
    assert _DEFAULT_OPPONENT_DRIVER[backend] == driver_for(binding)


def test_the_dispatchers_default_table_covers_exactly_the_backends_driver_for_can_default():
    """driver_for 对哪些 model_backend 能不靠显式 driver 取默认,派发器的表就列哪些,多一个少一个都不行。
    候选从 validate_training_config 放行的非训练位取值(OPPONENT_MODEL_BACKENDS)推出来,另加训练位 self_play
    与一个不存在的名字作反例:放行名单多了新取值,这里自动把它纳入,而不是漏测。"""
    defaultable = set()
    for backend in (*OPPONENT_MODEL_BACKENDS, "self_play", "no-such-backend"):
        try:
            driver_for(RoleBinding("b", "pb", backend, model_ref=None if backend == "self_play" else "x"))
        except ValueError:
            continue
        defaultable.add(backend)
    assert defaultable == set(_DEFAULT_OPPONENT_DRIVER)


def test_every_backend_the_training_config_allows_for_an_opponent_has_a_default_driver():
    """配置校验放行的对手位 model_backend,没写 driver 时 driver_for 都得有默认可取:放行了却取不到默认驱动,
    指令就会在派发时才因为 driver_for 抛 ValueError 而失败。"""
    for backend in OPPONENT_MODEL_BACKENDS:
        assert driver_for(RoleBinding("b", "pb", backend, model_ref="x")) in OPPONENT_DRIVERS


# 适配器导出的判法:fetch_hint 的 type 是 roll_ssh 且 format 是 EXPORT_FORMAT。只看其一都不算。
EXPORT_CASES = [
    pytest.param({"type": "roll_ssh", "format": CONVERTER_EXPORT_FORMAT}, True, id="export-format"),
    pytest.param({"type": "roll_ssh", "format": "roll_lora_legacy"}, False, id="roll-lora-legacy"),
    pytest.param({"type": "roll_ssh"}, False, id="type-only"),
    pytest.param({"format": CONVERTER_EXPORT_FORMAT}, False, id="format-only"),
    pytest.param({"type": "http", "format": CONVERTER_EXPORT_FORMAT}, False, id="other-type"),
    pytest.param({}, False, id="empty-hint"),
    pytest.param(None, False, id="no-hint"),
]


def _ref(hint):
    return BigRef("ssh://gpu-1/srv/rise/work/exports/x", "c" * 64, 10, fetch_hint=hint)


@pytest.fixture
def store(tmp_path):
    return closed_loop_repo(str(tmp_path / "cp"), run_id=None)


def _accepted_by_resolve_model_ref(store, hint) -> bool:
    store.write_product(model_file(1), Envelope(
        id="zengen-v1", type="zengen_model", version=1, created_at="t", created_by=OwnerRef(),
        status="effective", schema_version=1, payload={"checkpoint_ref": _ref(hint).to_dict()}), "seed zengen-v1")
    try:
        resolve_model_ref(store, "zengen-v1")
    except ModelRefError as exc:
        assert "not an adapter export" in str(exc)           # 拒绝的原因就是这一条,不是别的
        return False
    return True


@pytest.mark.parametrize("hint, expected", EXPORT_CASES)
def test_the_three_adapter_export_predicates_agree(store, hint, expected):
    ref = _ref(hint)
    assert dispatch_is_adapter_export(ref) is expected
    assert policy_is_adapter_export(ref) is expected
    assert _accepted_by_resolve_model_ref(store, hint) is expected


def test_the_converter_and_the_runner_name_the_export_format_the_same():
    assert CONVERTER_EXPORT_FORMAT == RUNNER_EXPORT_FORMAT


def test_the_deployment_copy_of_the_transient_reasons_equals_the_trajectory_sources():
    """mindagent 不导入 mindtrainer,瞬态终止原因集合各写一份:一边加了另一边没加,部署与训练对同一个原因就会判得不一样。"""
    assert DEPLOYMENT_TRANSIENT_REASONS == TRAJECTORY_TRANSIENT_REASONS
