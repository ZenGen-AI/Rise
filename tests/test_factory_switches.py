"""--enable 开关到六个处理器键的映射(闭环集成设计 §10.2)。"""
import pytest

from controlplane.reconcile import CLOSED_LOOP_ACTIONS
from factory.switches import ENABLE_MAP, SWITCHES, UnknownSwitchError, handler_keys, parse_enable


def test_the_switch_table_is_fixed():
    assert ENABLE_MAP == {
        "plan": ("plan_next_round",),
        "nominate": ("nominate_recipe",),
        "track": ("record_track",),
        "agent": ("start_agent_round",),
        "admit": ("admit_recipe", "admit_proposal"),
    }
    assert SWITCHES == ("plan", "nominate", "track", "agent", "admit")


def test_the_switches_cover_exactly_the_six_closed_loop_actions():
    keys = [k for v in ENABLE_MAP.values() for k in v]
    assert len(keys) == len(set(keys)) == 6
    assert set(keys) == set(CLOSED_LOOP_ACTIONS)


def test_admit_turns_on_both_admissions():
    assert handler_keys(["admit"]) == {"admit_recipe", "admit_proposal"}


@pytest.mark.parametrize("switch,keys", [
    ("plan", {"plan_next_round"}), ("nominate", {"nominate_recipe"}),
    ("track", {"record_track"}), ("agent", {"start_agent_round"}),
])
def test_each_other_switch_maps_to_one_key(switch, keys):
    assert handler_keys([switch]) == keys


def test_all_switches_give_all_six_keys():
    assert handler_keys(parse_enable("plan,nominate,track,agent,admit")) == CLOSED_LOOP_ACTIONS


def test_no_enable_means_no_switches_and_no_keys():
    assert parse_enable(None) == ()
    assert handler_keys(()) == frozenset()


def test_parse_keeps_the_fixed_order_and_drops_duplicates():
    assert parse_enable(" admit , plan,admit") == ("plan", "admit")


@pytest.mark.parametrize("text", ["", "plan,,admit", "plan,", "plann", "plan,dispatch", "plan_next_round"])
def test_empty_items_and_unknown_names_are_rejected(text):
    with pytest.raises(UnknownSwitchError):
        parse_enable(text)


def test_handler_keys_rejects_unknown_switches():
    with pytest.raises(UnknownSwitchError, match="nominatee"):
        handler_keys(["nominatee"])
