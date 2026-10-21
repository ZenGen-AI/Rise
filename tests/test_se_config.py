# tests/test_se_config.py
"""scheduler.yaml 的 strategy_experience 开关(设计 2.3、SE-9)。"""
import pytest

from trainscheduler.config import SchedulerConfig, parse_scheduler_config
from trainscheduler.errors import SchedulerConfigError


def test_default_is_off():
    assert SchedulerConfig().strategy_experience is False


def test_bool_round_trips_through_to_dict():
    cfg = SchedulerConfig(strategy_experience=True, template_version="v2")
    assert cfg.to_dict()["strategy_experience"] is True
    assert parse_scheduler_config(cfg.to_dict()).strategy_experience is True


def test_absent_key_keeps_the_default():
    assert parse_scheduler_config({}).strategy_experience is False


def test_non_bool_is_rejected():
    with pytest.raises(SchedulerConfigError, match="strategy_experience"):
        parse_scheduler_config({"strategy_experience": "yes"})


def test_unknown_keys_are_still_rejected():
    with pytest.raises(SchedulerConfigError, match="unknown key"):
        parse_scheduler_config({"strategy_exp": True})


def test_switch_on_with_v1_template_is_rejected():
    """开关打开但模板不渲染第七段,就是"配置了却没生效":必须启动即报错,不许静默空转。"""
    with pytest.raises(SchedulerConfigError, match="only 'v2' renders"):
        SchedulerConfig(strategy_experience=True)


def test_switch_off_accepts_the_default_template():
    cfg = SchedulerConfig()
    assert cfg.template_version == "v1" and cfg.strategy_experience is False
