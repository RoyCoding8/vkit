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

import hashlib
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


def _digest_of(value: Any) -> str:
    """One sha256 over a canonical JSON encoding of `value`.

    `sort_keys` fixes field order and the compact separators mean no two
    distinct values can share an encoding, so the digest identifies the
    structure and not a serialization of it. A digest built by joining fields
    with a delimiter is the same arithmetic with the property removed: two
    different policies whose text happens to join the same are one digest.
    """
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ManifestError(Exception):
    """The manifest is unusable, or names something that cannot be run safely."""


@dataclass(frozen=True)
class Prerequisite:
    name: str
    executable: str
    args: tuple[str, ...]


@dataclass(frozen=True)
class FixtureIdentity:
    """What the declared inputs were when the evidence was produced.

    `digest` is the value stored on the run and compared at acceptance.
    `inputs` is the measured list it came from, kept so a reader who sees a
    fixture digest change can see which file changed rather than being told
    only that something did.
    """

    digest: str
    inputs: tuple[dict[str, str], ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"digest": self.digest, "inputs": [dict(i) for i in self.inputs]}


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
        Nothing is ever passed through a shell or evaluated.

        `python` defaults to the interpreter running vkit, which is the right
        answer for every development run. A caller that must run the check
        against something other than itself passes that interpreter explicitly;
        Plan 07's trusted integration path does, so the checks of a candidate
        checkout are executed by the approved verifier revision rather than by
        whatever happens to be imported."""
        return self.resolved_argv_for(run_dir, python)

    def resolved_argv_for(self, run_dir: Path, python: str | None) -> tuple[str, ...]:
        interpreter = python if python is not None else sys.executable
        return tuple(
            part.replace("{{run_dir}}", str(run_dir)).replace("{{python}}", interpreter)
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

    def canonical_form(self) -> dict[str, Any]:
        """The whole policy as plain data, with no absolute path in it.

        This is the single description of what the policy *is*, and both the
        digest and the fixture identity are computed from it. Two callers
        deriving facts from it therefore cannot disagree about what a policy
        contains, which is the failure a hand-maintained second copy has.

        Every field that can change what executes is present: argv, the
        repository-relative cwd, the timeout, required scenarios, the artifact
        name, prerequisites and declared inputs. Nothing is omitted as
        "derived", because a derived value is only as trustworthy as the
        derivation and the omission is invisible at the call site.
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "description": self.description,
            "checks": [
                {
                    "id": check.id,
                    "description": check.description,
                    # A list, never a joined string. `['a b', 'c']` and
                    # `['a', 'b c']` are different argument vectors and
                    # produce different argument vectors in the child process;
                    # joining them with a space made the two policies
                    # indistinguishable to the fingerprint that decides whether
                    # old evidence is still valid.
                    "argv": list(check.argv),
                    "cwd": _relative(check.cwd, self.project.root),
                    "timeout_seconds": check.timeout_seconds,
                    "required_scenarios": list(check.required_scenarios),
                    "artifact": check.artifact_name,
                    "prerequisites": [
                        {"name": p.name, "executable": p.executable, "args": list(p.args)}
                        for p in check.prerequisites
                    ],
                    "inputs": list(check.inputs),
                }
                for check in sorted(self.checks.values(), key=lambda c: c.id)
            ],
        }

    def digest(self) -> str:
        """Identity of the configuration, not of the file's bytes.

        Two manifests that declare the same policy in different whitespace are
        the same policy and must not invalidate each other's evidence, so this
        hashes the parsed structure rather than the file. It is a hash over
        `canonical_form`, whose canonical JSON is a serialization of the data
        and not a concatenation of it: field order and separators cannot make
        two different policies collide, and no value can be smuggled across a
        delimiter.

        The repository root is absent from that structure by construction,
        because the cwd is recorded as the repository-relative path the manifest
        declared. Policy is the same whichever checkout is being tested, and a
        digest that varied by absolute path would make two candidates' approved
        policies compare unequal for a reason no reviewer could see.
        """
        return _digest_of(self.canonical_form())

    def fixture_identity(self) -> FixtureIdentity | None:
        """What the declared inputs actually are, measured now.

        A check declares `inputs`; the report's `fixture_digest` is what the
        evidence was produced against. Writing null there implied the fixture
        had been measured when nothing had been read, and a report that claims
        an identity it does not have is worse than one that admits it has none.

        Only declared inputs are hashed, and only for checks this policy runs.
        A check with no declared inputs contributes the empty list rather than
        a missing one: no declared fixture and an unreadable fixture are
        different facts, and only the second one can block.

        Returns None when a declared input cannot be read or escapes the
        repository. That is not an empty identity: it is an unresolved
        measurement, and the caller blocks rather than proceeding without one.
        """
        measured: list[dict[str, str]] = []
        root = self.project.root.resolve()
        for check in sorted(self.checks.values(), key=lambda c: c.id):
            for raw in check.inputs:
                try:
                    relative, digest = _measure_input(root, raw)
                except ManifestError:
                    return None
                measured.append({"check": check.id, "input": relative, "sha256": digest})
        return FixtureIdentity(_digest_of(measured), tuple(measured))


def _measure_input(root: Path, raw: str) -> tuple[str, str]:
    """One declared input's repository-relative path and content hash.

    Containment is checked after resolution, for the reason every other path in
    this codebase takes one: a prefix test loses to `..` and to an absolute
    path, and a manifest naming a path outside the repository is naming a file
    the repository's own identity cannot describe.
    """
    if Path(raw).is_absolute():
        raise ManifestError(f"declared input {raw!r} must be a repository-relative path")
    resolved = (root / raw).resolve()
    if resolved != root and root not in resolved.parents:
        raise ManifestError(f"declared input {raw!r} escapes the repository root")
    if not resolved.is_file():
        raise ManifestError(f"declared input {raw!r} does not exist")
    return resolved.relative_to(root).as_posix(), hashlib.sha256(
        resolved.read_bytes()
    ).hexdigest()


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


def _relative(cwd: Path, root: Path) -> str:
    """Express a resolved working directory relative to the project that owns it.

    `parse_manifest` resolved the raw repository-relative string once; this is
    how that resolution is reversed for a different root without keeping the raw
    string around. A directory outside the root cannot be re-expressed, and
    `_resolve_cwd` rejects the relative form for exactly the same reason, so the
    refusal is the same one a manifest author already sees."""
    resolved_root = root.resolve()
    resolved = cwd.resolve()
    if resolved == resolved_root:
        return "."
    if resolved_root in resolved.parents:
        return resolved.relative_to(resolved_root).as_posix()
    return str(resolved)


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
        blob = path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc
    return parse_manifest_bytes(blob, project=project, run_dir=run_dir, origin=str(path))


def parse_manifest_bytes(
    blob: bytes, *, project: Project, run_dir: Path, origin: str
) -> Manifest:
    """Validate and resolve manifest bytes read from somewhere other than the tree.

    Plan 07's trusted integration path reads the approved manifest out of a
    commit and must execute it against the candidate checkout. Re-rooting those
    bytes requires this parser rather than a second implementation of the
    manifest rules, because a second one is free to disagree about exactly the
    paths and timeouts this product refuses. `origin` names where the bytes came
    from so a rejection quotes the approved revision instead of a local path
    that was never written.
    """
    try:
        raw = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{origin} is not valid UTF-8 JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ManifestError(f"{origin} must contain a JSON object")
    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ManifestError(
            f"unsupported manifest schema_version {version!r}; this build supports {SCHEMA_VERSION}"
        )

    try:
        validate(origin, MANIFEST, raw)
    except SchemaValidationError as exc:
        raise ManifestError(f"{origin} rejected: {exc.reason}") from exc

    checks: dict[str, CheckSpec] = {}
    for entry in raw["checks"]:
        check_id = entry["id"]
        if check_id in checks:
            raise ManifestError(f"duplicate check id {check_id!r} in {origin}")

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
