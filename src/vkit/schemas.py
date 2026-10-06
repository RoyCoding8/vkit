"""JSON Schema validation for manifests and check artifacts, using the schemas shipped in the
package.
"""
from __future__ import annotations

import json
from collections import deque
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

MANIFEST_V2 = "manifest.v2.json"
CHECK_ARTIFACT = "check-artifact.v1.json"


class SchemaValidationError(Exception):
    """External data did not match its schema; `reason` is quotable in a BLOCKED detail."""

    def __init__(self, subject: str, message: str):
        self.subject = subject
        self.reason = message
        super().__init__(f"{subject}: {message}")


@lru_cache(maxsize=None)
def _schema_dir():
    """Locate the schemas: the `vkit._schemas` package in a wheel, else the checkout's `schemas/`
    (editable installs).
    """
    try:
        packaged = resources.files("vkit._schemas")
        if packaged.is_dir():
            return packaged
    except (ModuleNotFoundError, TypeError):
        pass
    checkout = Path(__file__).resolve().parents[2] / "schemas"
    if checkout.is_dir():
        return checkout
    raise FileNotFoundError(
        "the vkit schemas are not installed; reinstall the package or run from a checkout"
    )


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    text = _schema_dir().joinpath(name).read_text(encoding="utf-8")
    return Draft202012Validator(json.loads(text))


def _describe(error: ValidationError, depth: int = 0) -> str:
    """The most specific message for an error. For a failed `oneOf` the branch is chosen by the
    document's own `kind`, since the generic message names no branch.
    """
    if error.validator in ("oneOf", "anyOf") and depth < 2 and error.context:
        chosen = _branch_for_discriminator(error, error.instance)
        if chosen is not None:
            return _describe(chosen, depth + 1)
        best = min(error.context, key=lambda e: len(list(e.context)) if e.context else 1)
        return _describe(best, depth + 1)
    where = "/".join(str(p) for p in error.absolute_path) or "<root>"
    return f"{where}: {error.message}"


def _branch_for_discriminator(error: ValidationError, instance: Any) -> ValidationError | None:
    """The sub-error for the branch whose `kind` const equals the document's, else None.

    `schema_path` leads with the `oneOf` index because jsonschema resolves `$ref` before
    building the error, so the branch cannot be identified from the sub-error's own schema.
    """
    if not isinstance(instance, dict):
        return None
    declared = instance.get("kind")
    if declared is None:
        return None
    for sub in error.context or ():
        branch = _branch_at(sub.schema_path)
        if branch is not None and branch.get("properties", {}).get("kind", {}).get("const") == declared:
            return sub
    return None


def _branch_at(schema_path: deque) -> dict | None:
    """The `$defs` entry selected by a sub-error's leading `oneOf` index."""
    if not schema_path:
        return None
    return _branches()[schema_path[0]] if schema_path[0] < len(_branches()) else None


@lru_cache(maxsize=None)
def _branches() -> tuple[dict, ...]:
    """The check branches of the v2 manifest in `oneOf` order, which is the order `schema_path`
    indices refer to.
    """
    one_of = _validator(MANIFEST_V2).schema["properties"]["checks"]["items"]["oneOf"]
    defs = _validator(MANIFEST_V2).schema["$defs"]
    resolved = []
    for entry in one_of:
        resolved.append(defs[entry["$ref"].rsplit("/", 1)[-1]])
    return tuple(resolved)


def _first_error(validator: Draft202012Validator, instance: Any) -> str | None:
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    if not errors:
        return None
    return _describe(errors[0])


def validate(subject: str, schema_name: str, instance: Any) -> None:
    """Raise SchemaValidationError on the first problem, or return None."""
    reason = _first_error(_validator(schema_name), instance)
    if reason is not None:
        raise SchemaValidationError(subject, reason)


def parse_artifact(raw: bytes) -> Any:
    """Decode a check artifact's UTF-8 JSON bytes."""
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaValidationError("check artifact", f"not valid UTF-8 JSON: {exc}") from exc
