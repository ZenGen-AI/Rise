import pytest

from controlplane.envelope import Envelope, OwnerRef
from controlplane.validate import validate_envelope, EnvelopeValidationError


def _valid_dict() -> dict:
    return Envelope(
        id="run-001",
        type="directive",
        version=1,
        created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(),
        status="pending",
        schema_version=1,
    ).to_dict()


def test_valid_envelope_passes():
    validate_envelope(_valid_dict())  # 不抛异常


def test_missing_schema_version_fails():
    d = _valid_dict()
    del d["schema_version"]
    with pytest.raises(EnvelopeValidationError):
        validate_envelope(d)


def test_missing_owner_fails():
    d = _valid_dict()
    del d["created_by"]["owner"]
    with pytest.raises(EnvelopeValidationError):
        validate_envelope(d)
