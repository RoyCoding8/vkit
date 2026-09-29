"""Load and validate the three boundary schemas.

Validation lives at the boundary so nothing downstream re-checks it. The schemas
ship inside the installed package, because a user who installs `vkit` outside
this source checkout still has to get a real rejection rather than an import
error.
"""
from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator

MANIFEST = "manifest.v1.json"
CHECK_ARTIFACT = "check-artifact.v1.json"
RUN_REPORT = "run-report.v1.json"

SCHEMA_VERSION = 1


class SchemaValidationError(Exception):
    """External data did not match its schema. Carries the human-readable reason
    so a BLOCKED outcome can quote it rather than asserting 'malformed'."""

    def __init__(self, subject: str, message: str):
        self.subject = subject
        self.reason = message
        super().__init__(f"{subject}: {message}")


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    text = resources.files("vkit._schemas").joinpath(name).read_text(encoding="utf-8")
    return Draft202012Validator(json.loads(text))


def _first_error(validator: Draft202012Validator, instance: Any) -> str | None:
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    if not errors:
        return None
    error = errors[0]
    where = "/".join(str(p) for p in error.absolute_path) or "<root>"
    return f"{where}: {error.message}"


def validate(subject: str, schema_name: str, instance: Any) -> None:
    """Raise SchemaValidationError on the first problem, or return None."""
    reason = _first_error(_validator(schema_name), instance)
    if reason is not None:
        raise SchemaValidationError(subject, reason)


def parse_artifact(raw: bytes) -> Any:
    """Decode a check artifact from the process that wrote it.

    Separate from validation because the two failures mean different things to a
    reader: undecodable bytes are a malformed artifact, a schema mismatch is
    also a malformed artifact but a different diagnosis.
    """
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaValidationError("check artifact", f"not valid UTF-8 JSON: {exc}") from exc
