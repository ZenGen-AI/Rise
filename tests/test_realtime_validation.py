import pytest

from simulator.datamodel import Persona, RoleBinding, RoleAssignment
from simulator.realtime.validation import validate_create_session


def _ra(bindings, max_turns=4):
    return RoleAssignment(scenario_id="s1", bindings=bindings, max_turns=max_turns)


def _personas(*ids):
    return {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ids}


def _ok_bindings():
    return [RoleBinding("a", "pa", "self_play"), RoleBinding("b", "pb", "frozen_reference")]


def test_valid_request_returns_none():
    assert validate_create_session(
        _ra(_ok_bindings()), _personas("pa", "pb"), "a", max_turns_cap=200,
    ) is None


def test_empty_bindings_rejected():
    reason = validate_create_session(_ra([]), {}, "a", max_turns_cap=200)
    assert reason is not None
    assert "binding" in reason


def test_unbound_client_slot_rejected():
    # 曾导致无限自主轮次的坑：client_driven_slot 不在 bindings 里
    reason = validate_create_session(
        _ra(_ok_bindings()), _personas("pa", "pb"), "ghost", max_turns_cap=200,
    )
    assert reason is not None
    assert "ghost" in reason


def test_missing_persona_rejected_naming_the_persona():
    # 曾被误判为 role_backend_failure 的坑：persona 缺失
    reason = validate_create_session(
        _ra(_ok_bindings()), _personas("pa"), "a", max_turns_cap=200,
    )
    assert reason is not None
    assert "pb" in reason


def test_max_turns_zero_rejected():
    reason = validate_create_session(
        _ra(_ok_bindings(), max_turns=0), _personas("pa", "pb"), "a", max_turns_cap=200,
    )
    assert reason is not None
    assert "max_turns" in reason


def test_max_turns_over_cap_rejected():
    reason = validate_create_session(
        _ra(_ok_bindings(), max_turns=201), _personas("pa", "pb"), "a", max_turns_cap=200,
    )
    assert reason is not None
    assert "200" in reason


def test_max_turns_at_cap_allowed():
    assert validate_create_session(
        _ra(_ok_bindings(), max_turns=200), _personas("pa", "pb"), "a", max_turns_cap=200,
    ) is None
