"""起服务前的显存核算:真机实测到的那几组配置要被提前判死,算得出来的才放行。"""
from __future__ import annotations

import pytest

from compute.errors import ComputeError
from compute.sizing import (
    COMPILE_HEADROOM_GIB,
    DEFAULT_OVERHEAD_GIB,
    MemorySizing,
    check_memory_fits,
    weights_gib_from_index,
)

REAL = dict(weights_gib=335.276, cards=8, card_gib=79.17, utilization=0.85)


def _sizing(**kw):
    return MemorySizing(**{**REAL, "enforce_eager": False, **kw})


def test_the_reported_numbers_add_up():
    report = _sizing().report()
    assert report["per_card_weights_gib"] == pytest.approx(41.91, abs=0.01)
    assert report["usable_per_card_gib"] == pytest.approx(67.29, abs=0.01)
    assert report["required_per_card_gib"] == pytest.approx(41.91 + DEFAULT_OVERHEAD_GIB + COMPILE_HEADROOM_GIB, abs=0.02)
    assert report["slack_gib"] == pytest.approx(67.29 - report["required_per_card_gib"], abs=0.02)


@pytest.mark.parametrize("eager", [False, True])
def test_the_configuration_measured_on_the_real_host_is_refused(eager):
    """真机实测:335 GiB / 8 卡、0.85,开图优化启动 OOM,关图优化也分不出 KV 缓存块。"""
    with pytest.raises(ComputeError, match="does not fit"):
        check_memory_fits(_sizing(enforce_eager=eager))


def test_a_higher_utilization_leaves_enough_room():
    assert check_memory_fits(_sizing(enforce_eager=True, utilization=0.95))["fits"] is True


def test_more_cards_fix_the_fit():
    assert check_memory_fits(_sizing(cards=16, enforce_eager=True))["fits"] is True


def test_a_smaller_model_fits():
    assert check_memory_fits(_sizing(weights_gib=120.0))["fits"] is True


def test_the_overhead_is_configurable_because_it_depends_on_the_model():
    """开销随模型与并行方式变化,所以它是输入而不是写死的常量。"""
    report = check_memory_fits(_sizing(enforce_eager=True, overhead_gib=10.0))
    assert report["fits"] is True and report["overhead_gib"] == 10.0


def test_the_message_names_every_term_of_the_arithmetic():
    with pytest.raises(ComputeError) as caught:
        check_memory_fits(_sizing())
    message = str(caught.value)
    for fragment in ("335.28", "8 card", "41.91", "26", "12", "67.29", "enforce_eager"):
        assert fragment in message


@pytest.mark.parametrize("bad", [dict(cards=0), dict(card_gib=0.0), dict(overhead_gib=-1.0)])
def test_an_impossible_sizing_is_refused(bad):
    with pytest.raises(ComputeError):
        check_memory_fits(_sizing(**bad))


def test_weights_are_read_from_the_index_metadata():
    assert weights_gib_from_index({"metadata": {"total_size": 360000192888}}) == pytest.approx(335.28, abs=0.01)


@pytest.mark.parametrize("payload", [{}, {"metadata": {}}, {"metadata": {"total_size": 0}},
                                     {"metadata": {"total_size": "big"}}, {"metadata": {"total_size": True}}])
def test_an_index_without_a_usable_size_yields_none(payload):
    assert weights_gib_from_index(payload) is None
