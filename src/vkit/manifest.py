"""Parse `verification/manifest.json` into records, rejecting anything that would
be unsafe or ambiguous to execute.

Every rejection here happens before a process is launched. That is the point of
putting them in one module: Plan 01 step 1 requires bad schema versions,
duplicate check ids, unknown selections, path escapes, malformed argument arrays
and unbounded timeouts to be refused up front, and they are all the same kind of
decision, so they belong in one place rather than scattered through the CLI.

The parsed records are frozen and already-validated. Downstream code can execute
them without re-checking.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import Project
from .schemas import MANIFEST, SCHEMA_VERSION, SchemaValidationError, validate

# A run must never be allowed to hang forever, and a day is long enough for any
# real check while still being a bound.
MAX_TIMEOUT_SECONDS = 86400.0


class ManifestError(Exception):
    """The manifest is unusable, or names something that cannot be run safely."""


@dataclass(frozen=True)
class Prerequisite:
    name: str
    executable: str
    args: tuple[str, ...]


@dataclass(frozen=True)
class CheckSpec:
    """One executable, fully resolved against the repository root.

    `argv` still holds the placeholders unexpanded. They are substituted once the
    run directory exists, because that path is only known at run time and a
    check's own arguments are the only thing that legitimately needs it.
    """

    id: str
    description: str
    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: float
    required_scenarios: tuple[str, ...]
    artifact_name: str
    prerequisites: tuple[Prerequisite, ...]
    inputs: tuple[str, ...]

    def resolved_argv(self, run_dir: Path, python: str) -> tuple[str, ...]:
        """The exact list to execute. Only two placeholders exist, both documented
        in CONTRACT.md: the run artifact directory and the resolved interpreter.
        Nothing is ever passed through a shell or evaluated."""
        return tuple(
            part.replace("{{run_dir}}", str(run_dir)).replace("{{python}}", python)
            for part in self.argv
        )


@dataclass(frozen=True)
class Manifest:
    project: Project
    description: str
    checks: dict[str, CheckSpec]

    def require(self, check_id: str) -> CheckSpec:
        try:
            return self.checks[check_id]
        except KeyError:
            known = ", ".join(sorted(self.checks)) or "<none>"
            raise ManifestError(
                f"unknown check {check_id!r}; manifest defines: {known}"
            ) from None

    def digest(self) -> str:
        """Identity of the configuration, not of the file's bytes. Two manifests
        that select the same command with different whitespace are the same
        policy, and should not invalidate each other's evidence."""
        import hashlib

        parts = sorted(
            f"{c.id}|{' '.join(c.argv)}|{c.cwd}|{c.timeout_seconds}"
            f"|{','.join(c.required_scenarios)}|{c.artifact_name}"
            for c in self.checks.values()
        )
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _resolve_cwd(root: Path, raw: str | None, check_id: str) -> Path:
    """A working directory is repository-relative and must stay inside the root.

    Resolving and then checking containment is the only form that actually works:
    string prefix checks lose to `..`, absolute paths, and Windows drive-relative
    paths, all of which are real ways out of a repository.
    """
    candidate = (root / (raw or ".")).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ManifestError(
            f"check {check_id!r}: cwd {raw!r} escapes the repository root"
        )
    return candidate


def _resolve_artifact(run_dir: Path, raw: str, check_id: str) -> str:
    """Artifact names are written by the check, so they are the least trusted
    string in the system. A name that resolves outside the run directory is
    rejected here rather than discovered when a report points at it."""
    if Path(raw).is_absolute():
        raise ManifestError(
            f"check {check_id!r}: artifact {raw!r} must be a relative name inside the run directory"
        )
    resolved = (run_dir / raw).resolve()
    if run_dir.resolve() not in resolved.parents and resolved != run_dir.resolve():
        raise ManifestError(
            f"check {check_id!r}: artifact {raw!r} escapes the run directory"
        )
    return raw


def parse_manifest(project: Project, run_dir: Path, path: Path | None = None) -> Manifest:
    """Read, validate and resolve the project's manifest.

    `path` defaults to the project's own manifest path, which is what every
    execution caller wants. Plan 06's enrollment needs to parse a *proposal*
    that has deliberately not been promoted to the policy path, so it passes the
    proposal's location explicitly. Parsing is the same work either way; only
    the file differs.
    """
    path = path or project.manifest_path
    if not path.is_file():
        raise ManifestError(f"no manifest at {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ManifestError(f"{path} must contain a JSON object")
    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ManifestError(
            f"unsupported manifest schema_version {version!r}; this build supports {SCHEMA_VERSION}"
        )

    try:
        validate(str(path), MANIFEST, raw)
    except SchemaValidationError as exc:
        raise ManifestError(f"{path} rejected: {exc.reason}") from exc

    checks: dict[str, CheckSpec] = {}
    for entry in raw["checks"]:
        check_id = entry["id"]
        if check_id in checks:
            raise ManifestError(f"duplicate check id {check_id!r} in {path}")

        argv = tuple(entry["command"])
        if any(not isinstance(part, str) or not part for part in argv):
            raise ManifestError(f"check {check_id!r}: command entries must be nonempty strings")

        timeout = float(entry["timeout_seconds"])
        if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            raise ManifestError(
                f"check {check_id!r}: timeout_seconds must be positive and at most "
                f"{MAX_TIMEOUT_SECONDS:g}, got {timeout:g}"
            )

        required = tuple(entry["required_scenarios"])
        if not required:
            raise ManifestError(f"check {check_id!r}: at least one required scenario is mandatory")
        duplicates = {s for s in required if required.count(s) > 1}
        if duplicates:
            raise ManifestError(
                f"check {check_id!r}: duplicate required scenarios {sorted(duplicates)}"
            )

        checks[check_id] = CheckSpec(
            id=check_id,
            description=entry.get("description", ""),
            argv=argv,
            cwd=_resolve_cwd(project.root, entry.get("cwd"), check_id),
            timeout_seconds=timeout,
            required_scenarios=required,
            artifact_name=_resolve_artifact(run_dir, entry["artifact"], check_id),
            prerequisites=tuple(
                Prerequisite(p["name"], p["executable"], tuple(p.get("args", ())))
                for p in entry.get("prerequisites", ())
            ),
            inputs=tuple(entry.get("inputs", ())),
        )

    return Manifest(
        project=project,
        description=raw.get("description", ""),
        checks=checks,
    )
