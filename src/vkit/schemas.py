"""Load and validate the boundary schemas.

Validation lives at the boundary so nothing downstream re-checks it. The schemas
ship inside the installed package, because a user who installs `vkit` outside
this source checkout still has to get a real rejection rather than an import
error.

There is more than one version in force, and that is a fact about the product
rather than an accident of the migration. The v1 files stay readable beside the
v2 ones: an artifact recorded under the old shape is still evidence, and
`SCHEMA_VERSIONS` is what says which file describes what. Resolution follows the
same rule for a wheel and a source checkout, because a user who pip-installs
`vkit` away from this tree has no other copy.
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

MANIFEST = "manifest.v1.json"
MANIFEST_V2 = "manifest.v2.json"
CHECK_ARTIFACT = "check-artifact.v1.json"
RUN_REPORT = "run-report.v1.json"
RECEIPT = "receipt.v2.json"

#: The version each schema is read at, keyed by the artifact it describes.
#:
#: This is a map rather than one constant because the schemas do not all move
#: together. `manifest` is at 2 and `receipt` is at 2, while the check artifact
#: and the run report stay at 1: the scenario variant is a first-class variant
#: that delegates to a driver, and the run report describes the process rather
#: than the evidence. A single constant would force one of two wrong things,
#: either freezing the manifest at 1 or pretending the v1 schemas were rewritten.
#:
#: The versions are per artifact, not per file name, because that is the fact a
#: caller needs: `CHECK_ARTIFACT` names a v1 format and `RECEIPT` a v2 one, and
#: the pair is what `version_of` reports. Looking up a name that is not in the
#: map raises rather than defaulting, because a default is a way of shipping a
#: version nobody chose.
SCHEMA_VERSIONS: dict[str, int] = {
    MANIFEST: 1,
    MANIFEST_V2: 2,
    CHECK_ARTIFACT: 1,
    RUN_REPORT: 1,
    RECEIPT: 2,
}


def schema_version(name: str) -> int:
    """The version one schema is read at, refusing a name that is not in the map."""
    try:
        return SCHEMA_VERSIONS[name]
    except KeyError:
        known = ", ".join(sorted(SCHEMA_VERSIONS))
        raise KeyError(
            f"no schema version is declared for {name!r}; known schemas: {known}"
        ) from None


class SchemaValidationError(Exception):
    """External data did not match its schema. Carries the human-readable reason
    so a BLOCKED outcome can quote it rather than asserting 'malformed'."""

    def __init__(self, subject: str, message: str):
        self.subject = subject
        self.reason = message
        super().__init__(f"{subject}: {message}")


@lru_cache(maxsize=None)
def _schema_dir():
    """Find the schemas whether vkit came from a wheel or a source checkout.

    A wheel force-includes them as the vkit._schemas package. An editable
    install has no such package, because the files are not inside the source
    tree, so fall back to the repository's schemas/ directory. Without the
    fallback, `vkit doctor` breaks the moment anyone pip-installs -e, which is
    the normal way a developer runs it.
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
    """The most specific message this error can carry.

    A `oneOf` that matches no branch reports only "not valid under any of the
    given schemas", which tells a reader nothing about what was wrong with their
    document. That is the message every manifest check is refused through, so a
    candidate who wrote a `property` check without a generator block would be told
    only that they were wrong.

    Choosing which branch to quote is the whole problem here, and two naive rules
    are both wrong. The shallowest branch is the `scenario` one, which requires
    `command`, so every non-scenario document is refused as if it had misspelled
    a command. The fewest-context branch is the one with the fewest nested
    failures, which is no more than a proxy.

    What identifies the right branch is the discriminator the author wrote. A
    document with `kind: "tlc"` belongs to the tlc branch whatever else it got
    wrong, so its message is the tlc branch's. A document with no recognised
    `kind` at all has no branch to speak for it, and the shallowest summary is
    then the honest answer.
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
    """The sub-error for the branch whose `kind` const equals the document's.

    Matching on `schema` does not work, because jsonschema resolves a `$ref` before
    the error is built: the sub-error's own schema carries no `properties`, so a
    branch cannot be identified from it. What survives is `schema_path`, whose
    leading integers are the index of the branch inside the `oneOf` array. That
    index is resolved against the same `$defs` list the branches were built from,
    so the lookup is the same table rather than a second spelling of it.

    Returns None when the document declares no `kind`, or a kind no branch claims,
    which are the two cases where no branch can speak for the document.
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
    """The `$defs` entry a sub-error's leading `oneOf` index selects."""
    if not schema_path:
        return None
    return _branches()[schema_path[0]] if schema_path[0] < len(_branches()) else None


@lru_cache(maxsize=None)
def _branches() -> tuple[dict, ...]:
    """The check branches of the v2 manifest, in `oneOf` order.

    Order matters because a sub-error identifies its branch by index, so this
    reads the array the schema actually declares rather than sorting a dict of
    definitions, which would put the branches in a different order than the one
    the indices refer to.
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
    """Decode a check artifact from the process that wrote it.

    Separate from validation because the two failures mean different things to a
    reader: undecodable bytes are a malformed artifact, a schema mismatch is
    also a malformed artifact but a different diagnosis.
    """
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaValidationError("check artifact", f"not valid UTF-8 JSON: {exc}") from exc
