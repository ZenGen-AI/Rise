import dataclasses

import pytest

from controlplane.envelope import (
    CURRENT_SCHEMA_VERSION, SHA256_HEX, BigRef, Envelope, InvalidBigRefError, OwnerRef, bump,
)

HEX = "a" * 64


def test_envelope_defaults_carry_extensibility_tax():
    env = Envelope(
        id="run-001",
        type="directive",
        version=1,
        created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(),
        status="pending",
        schema_version=CURRENT_SCHEMA_VERSION,
    )
    assert env.created_by.owner == "default"
    assert env.created_by.visibility == "shared"
    assert env.schema_version == 1
    assert env.links == []


def test_envelope_round_trip():
    env = Envelope(
        id="cand-001",
        type="candidate_checkpoint",
        version=1,
        created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(owner="alice", visibility="private"),
        status="pending_review",
        schema_version=1,
        links=["run-001"],
        payload={"note": "hi"},
    )
    restored = Envelope.from_dict(env.to_dict())
    assert restored == env


def test_bigref_fetch_hint_optional():
    ref = BigRef(uri="file:///ckpt", sha256=HEX, size_bytes=10)
    assert ref.fetch_hint is None
    d = ref.to_dict()
    assert d["fetch_hint"] is None
    assert BigRef.from_dict(d) == ref


# ---- I8:BigRef 构造即校验并冻结(CP-16) ----

def _ok(**kw):
    base = dict(uri="ssh://gpu-1/srv/ckpt", sha256=HEX, size_bytes=0, fetch_hint=None)
    base.update(kw)
    return base


@pytest.mark.parametrize("field, value", [
    ("uri", ""), ("uri", None), ("uri", 3),
    ("sha256", ""), ("sha256", "abc"), ("sha256", "A" * 64), ("sha256", "g" * 64),
    ("sha256", "../../.." + "/x" * 28), ("sha256", HEX + "\n"), ("sha256", 123), ("sha256", None),
    ("size_bytes", -1), ("size_bytes", True), ("size_bytes", 1.0), ("size_bytes", "10"), ("size_bytes", None),
    ("fetch_hint", []), ("fetch_hint", "mock"),
])
def test_a_reference_with_a_bad_field_cannot_be_constructed(field, value):
    with pytest.raises(InvalidBigRefError, match=field):
        BigRef(**_ok(**{field: value}))


@pytest.mark.parametrize("field, value", [("sha256", "abc"), ("size_bytes", -1), ("uri", "")])
def test_reading_a_bad_reference_from_a_dict_fails_the_same_way(field, value):
    with pytest.raises(InvalidBigRefError, match=field):
        BigRef.from_dict(_ok(**{field: value}))


def test_the_error_is_a_type_error_so_existing_unreadable_branches_catch_it():
    """每个读树点都把 TypeError 当成确定性读不出;ValueError 会漏出去被当成瞬态。"""
    assert issubclass(InvalidBigRefError, TypeError)
    assert not issubclass(InvalidBigRefError, ValueError)


def test_replace_validates_too():
    with pytest.raises(InvalidBigRefError, match="sha256"):
        dataclasses.replace(BigRef(**_ok()), sha256="../../etc")


def test_a_reference_is_immutable():
    ref = BigRef(**_ok())
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.sha256 = "../../etc"


def test_a_valid_reference_round_trips_and_keeps_its_hint():
    ref = BigRef(**_ok(size_bytes=12, fetch_hint={"type": "roll_ssh", "format": "hf"}))
    assert BigRef.from_dict(ref.to_dict()) == ref


def test_the_error_does_not_echo_an_oversized_value():
    """错误信息会进日志与失败记录:不把整段坏值带出去。"""
    with pytest.raises(InvalidBigRefError) as info:
        BigRef(**_ok(sha256="z" * 5000))
    assert len(str(info.value)) < 300


def test_the_hex_rule_is_the_one_shared_constant():
    assert SHA256_HEX.match(HEX) and not SHA256_HEX.match(HEX.upper())


# ---- 改写时升信封版本 ----

def _env(version=1, payload=None, **kw):
    base = dict(id="p-1", type="skill_memory_proposal", version=version, created_at="2026-10-09T00:00:00Z",
                created_by=OwnerRef(), status="pending_review", schema_version=CURRENT_SCHEMA_VERSION,
                links=["r"], payload=payload if payload is not None else {"a": 1})
    base.update(kw)
    return Envelope(**base)


def test_bump_raises_the_version_by_one_over_the_previous_and_leaves_the_rest_alone():
    previous, new = _env(version=3), _env(version=1, payload={"a": 2})
    out = bump(new, previous)
    assert out.version == 4 and out is not new and new.version == 1 and previous.version == 3   # 不改入参
    assert out.to_dict() == {**new.to_dict(), "version": 4}
    assert out.schema_version == CURRENT_SCHEMA_VERSION      # 动的是产物版本,不是迁移器链的版本


def test_bump_of_a_first_write_is_the_envelope_itself():
    new = _env()
    assert bump(new, None) is new


def test_bump_never_lowers_a_version_the_caller_already_set_higher():
    assert bump(_env(version=9), _env(version=3)).version == 9
    assert bump(_env(version=4), _env(version=3)).version == 4
