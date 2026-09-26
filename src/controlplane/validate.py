from __future__ import annotations

import json
from pathlib import Path

import jsonschema

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "envelope.schema.json"


class EnvelopeValidationError(Exception):
    """产物信封不符合 JSON Schema。"""


def _load_schema() -> dict:
    with _SCHEMA_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def validate_envelope(d: dict) -> None:
    try:
        jsonschema.validate(instance=d, schema=_load_schema())
    except jsonschema.ValidationError as exc:
        raise EnvelopeValidationError(str(exc)) from exc
