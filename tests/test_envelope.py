from controlplane.envelope import Envelope, OwnerRef, BigRef, CURRENT_SCHEMA_VERSION


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
    ref = BigRef(uri="file:///ckpt", sha256="abc", size_bytes=10)
    assert ref.fetch_hint is None
    d = ref.to_dict()
    assert d["fetch_hint"] is None
    assert BigRef.from_dict(d) == ref
