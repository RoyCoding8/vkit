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

from .claimkind import ClaimCategory
from .paths import Project
from .schemas import (
    MANIFEST,
    MANIFEST_V2,
    SCHEMA_VERSIONS,
    SchemaValidationError,
    validate,
)
from .verifiers import (
    CaseObligation,
    CheckKind,
    DomainBounds,
    FingerprintSpec,
    HypothesisSettings,
    LeanCheck,
    LeanProfile,
    ModuleRef,
    NativeCheckSpec,
    NodeTestCheck,
    Obligation,
    PinnedRunner,
    PropertyCheck,
    PropertyObligation,
    PytestCheck,
    ReplaySettings,
    ScenarioCheck,
    SubjectRef,
    TheoremObligation,
    TlcCheck,
    ToolchainRef,
    evidence_kind,
)

#: A run must never be allowed to hang forever, and a day is long enough for any
#: real check while still being a bound.
MAX_TIMEOUT_SECONDS = 86400.0

#: The schema each manifest version is read under, and the variant each `kind`
#: builds. Both are tables so the parser has one place that knows the mapping and
#: nothing else has to spell it out.
_SCHEMA_FOR_VERSION = {1: MANIFEST, 2: MANIFEST_V2}

#: Every kind the v2 schema declares has an adapter as of Plan 10 checkpoint 3, so
#: there is no longer a kind this build accepts in a manifest and cannot run. The
#: refusal that used to live here named checkpoint 3 as the thing that would
#: supply the runner, and it is gone: `lean` and `tlc` parse into their variants
#: and are BLOCKED later, by the adapter, when the toolchain or the isolation the
#: check declared is genuinely missing. That is the better place for the refusal,
#: because a capability missing on one host is not a manifest bug, and a reader
#: told a manifest was malformed would go looking for a typo that is not there.


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

    Deprecated in favour of the `verifiers.spec` union and kept only as the name
    every existing caller imports. A v2 check is one of the six variants in
    `VARIANTS`, and a v1 check is a `ScenarioCheck`, so there is one type behind
    both readings rather than a v1 shape and a v2 shape that disagree.

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
    expectations: tuple[str, ...] | None = None
    #: The variant this check was parsed into. A v1 check is a `ScenarioCheck`
    #: with a v1-shaped command, so a v1 manifest is a `scenario` check by the
    #: only reading it has rather than a second shape with no category.
    variant: NativeCheckSpec | None = None
    #: The manifest version this check was declared under. It is a field rather
    #: than something derived from the variant, because a v1 check and a v2
    #: `scenario` check carry the same fields and the two must still produce
    #: different digests: the version is what tells a reader which contract the
    #: evidence was produced under.
    declared_version: int = 1
    subject: SubjectRef | None = None
    claim_id: str | None = None

    @property
    def kind(self) -> CheckKind:
        return CheckKind.SCENARIO if self.variant is None else self.variant.kind

    def evidence_kind(self) -> ClaimCategory:
        """The category a PASS from this check licenses.

        Derived from the variant, never declared. A v1 check has no variant
        beyond its scenario reading, which is what makes a legacy driver unable
        to claim `theorem_checking` no matter what its file says.
        """
        if self.variant is None:
            return ClaimCategory.SCENARIO
        return evidence_kind(self.variant)

    def obligations(self) -> tuple[Obligation, ...]:
        """The obligations a receipt must discharge for this check.

        A v1 check's `required_scenarios` are case obligations, which is what
        they always were; `CaseObligation` is the reading, not a translation
        layer over a different fact.
        """
        if self.variant is not None:
            return self.variant.obligations
        return tuple(CaseObligation(s) for s in self.required_scenarios)

    def resolved_argv_for(self, run_dir: Path, python: str | None) -> tuple[str, ...]:
        """The exact list to execute. Only two placeholders exist, both documented
        in CONTRACT.md: the run artifact directory and the resolved interpreter.
        Nothing is ever passed through a shell or evaluated.

        `python` is None for the interpreter running vkit, which is the right
        answer for every development run. A caller that must run the check
        against something other than itself passes that interpreter explicitly;
        Plan 07's trusted integration path does, so the checks of a candidate
        checkout are executed by the approved verifier revision rather than by
        whatever happens to be imported."""
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
            "schema_version": max(
                (c.declared_version for c in self.checks.values()), default=1
            ),
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
                    # The kind and the obligations are inside the digest because
                    # a manifest that re-declares the same commands as a
                    # different kind is a different policy, and one that drops a
                    # theorem is a different policy again. Leaving them out would
                    # make a renamed check produce the same digest as the one it
                    # replaced, which is the exact change acceptance exists to
                    # notice.
                    "kind": check.kind.value,
                    "subject": {
                        "paths": list(check.subject.paths) if check.subject else [],
                        "digest": check.subject.digest if check.subject else None,
                    },
                    "claim_id": check.claim_id or "",
                    **({"expectations": list(check.expectations)}
                       if check.expectations is not None else {}),
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
    schema_name = _SCHEMA_FOR_VERSION.get(version) if isinstance(version, int) else None
    if schema_name is None:
        supported = ", ".join(str(v) for v in sorted(_SCHEMA_FOR_VERSION))
        raise ManifestError(
            f"unsupported manifest schema_version {version!r}; this build reads {supported}"
        )

    try:
        validate(origin, schema_name, raw)
    except SchemaValidationError as exc:
        raise ManifestError(f"{origin} rejected: {exc.reason}") from exc

    checks: dict[str, CheckSpec] = {}
    for entry in raw["checks"]:
        check_id = entry["id"]
        if check_id in checks:
            raise ManifestError(f"duplicate check id {check_id!r} in {origin}")
        checks[check_id] = _build_check(entry, project=project, run_dir=run_dir,
                                       version=version)

    return Manifest(
        project=project,
        description=raw.get("description", ""),
        checks=checks,
    )


def _build_check(
    entry: dict, *, project: Project, run_dir: Path, version: int
) -> CheckSpec:
    """One parsed check, as the variant its `kind` selects.

    A v1 entry has no `kind` and is a scenario driver by the only reading it has,
    so it becomes a `ScenarioCheck` with a `ScenarioCheck` variant attached. That
    is what lets one code path serve both versions: `evidence_kind` answers
    SCENARIO for both, and a v1 driver therefore cannot reach any other category.
    """
    check_id = entry["id"]
    timeout = float(entry["timeout_seconds"])
    if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise ManifestError(
            f"check {check_id!r}: timeout_seconds must be positive and at most "
            f"{MAX_TIMEOUT_SECONDS:g}, got {timeout:g}"
        )

    inputs = tuple(entry.get("inputs", ()))
    expectations = entry.get("expectations")
    if expectations is not None:
        undeclared = sorted(set(expectations) - set(inputs))
        if undeclared:
            raise ManifestError(
                f"check {check_id!r}: expectations must also be declared in inputs: "
                f"{', '.join(undeclared)}"
            )

    common = dict(
        id=check_id,
        description=entry.get("description", ""),
        cwd=_resolve_cwd(project.root, entry.get("cwd"), check_id),
        timeout_seconds=timeout,
        artifact_name=_resolve_artifact(run_dir, entry["artifact"], check_id),
        prerequisites=tuple(
            Prerequisite(p["name"], p["executable"], tuple(p.get("args", ())))
            for p in entry.get("prerequisites", ())
        ),
        inputs=inputs,
        expectations=None if expectations is None else tuple(expectations),
        subject=_subject(entry.get("subject")),
        claim_id=entry.get("claim_id", check_id),
    )

    if version == 1:
        required = tuple(entry["required_scenarios"])
        _require_nonempty(check_id, "scenario", required)
        _require_distinct(check_id, "scenario", required)
        command = tuple(entry["command"])
        _require_argv(check_id, command)
        variant = ScenarioCheck(**common, command=command,
                                required_scenarios=tuple(CaseObligation(s) for s in required))
        return _wrap(common, required, command, variant, 1)

    kind = CheckKind(entry["kind"])
    variant = _variant_for(kind, entry, common, check_id)
    return _wrap(common, (), variant.argv, variant, 2)


def _wrap(common: dict, required_scenarios: tuple[str, ...],
          command: tuple[str, ...], variant, version: int) -> CheckSpec:
    return CheckSpec(
        **common,
        argv=command,
        required_scenarios=_scenario_names(variant, required_scenarios),
        variant=variant,
        declared_version=version,
    )


def _scenario_names(variant, fallback: tuple[str, ...]) -> tuple[str, ...]:
    """The scenario ids a check reports, read off its obligations.

    `required_scenarios` stays populated for a v2 scenario check because the
    console view, the MCP check view, `verify.py` and `execution` all read it, and
    the obligation a scenario check carries is exactly its scenario id. Leaving it
    empty made those four callers report a check with no required cases, which is
    a false statement about the policy rather than a stale field.

    For every other kind it stays empty, because a `lean` or `tlc` check has no
    scenario to name and inventing one would be the claim this contract exists to
    prevent. A caller that needs the general set asks `obligations()`.
    """
    if not isinstance(variant, ScenarioCheck):
        return ()
    return tuple(obligation.test_id for obligation in variant.required_scenarios)


def _subject(raw: dict | None) -> SubjectRef:
    """The declared subject, defaulting to an empty one.

    A v1 manifest has no `subject` key, and an absent subject is not the same as
    a claim about nothing: it is a manifest written before the field existed. The
    empty ref records that honestly, and the receipt carries whatever vkit
    measured.
    """
    if not raw:
        return SubjectRef()
    return SubjectRef(tuple(raw.get("paths", ())), raw.get("digest"))


def _require_nonempty(check_id: str, what: str, values: tuple) -> None:
    if not values:
        raise ManifestError(
            f"check {check_id!r}: at least one required {what} is mandatory. A "
            f"check that exercises nothing and passes is the failure "
            f"CONTRACT.md forbids."
        )


def _require_distinct(check_id: str, what: str, values: tuple) -> None:
    duplicates = sorted({v for v in values if list(values).count(v) > 1})
    if duplicates:
        raise ManifestError(
            f"check {check_id!r}: duplicate required {what} {duplicates}"
        )


def _require_argv(check_id: str, command: tuple[str, ...]) -> None:
    if any(not isinstance(part, str) or not part for part in command):
        raise ManifestError(
            f"check {check_id!r}: command entries must be nonempty strings"
        )


def _variant_for(kind: CheckKind, entry: dict, common: dict, check_id: str):
    """The variant for one `kind`, built from the fields that kind has.

    Every read here is a direct index on a field the branch's schema already
    required, so a missing one is a schema bug rather than a shape a candidate
    can reach.
    """
    if kind is CheckKind.SCENARIO:
        command = tuple(entry["command"])
        _require_argv(check_id, command)
        names = tuple(entry["required_scenarios"])
        _require_nonempty(check_id, "scenario", names)
        _require_distinct(check_id, "scenario", names)
        return ScenarioCheck(**common, command=command,
                             required_scenarios=tuple(CaseObligation(s) for s in names))

    if kind in (CheckKind.PYTEST, CheckKind.PROPERTY):
        runner = PinnedRunner(entry["runner"]["executable"],
                              tuple(entry["runner"]["base_argv"]))
        tests = tuple(entry["required_tests"])
        _require_nonempty(check_id, "test", tests)
        _require_distinct(check_id, "test", tests)
        shared = dict(
            **common,
            required_tests=tests,
            runner=runner,
            report_format=entry["report_format"],
            expect_report_version=int(entry["expect_report_version"]),
        )
        if kind is CheckKind.PYTEST:
            return PytestCheck(**shared)
        generator = entry["generator"]
        return PropertyCheck(
            **shared,
            generator=HypothesisSettings(
                int(generator["max_examples"]),
                int(generator["stateful_step_count"]),
                generator["deadline"],
                tuple(generator["suppress_health_check"]),
            ),
            replay=ReplaySettings(
                entry["replay"]["database"],
                entry["replay"]["seed"],
            ) if entry.get("replay") else None,
        )

    if kind is CheckKind.NODE_TEST:
        runner = PinnedRunner(entry["runner"]["executable"],
                              tuple(entry["runner"]["base_argv"]))
        tests = tuple(entry["required_tests"])
        _require_nonempty(check_id, "test", tests)
        _require_distinct(check_id, "test", tests)
        return NodeTestCheck(
            **common, required_tests=tests, runner=runner,
            report_format=entry["report_format"],
            expect_report_version=int(entry["expect_report_version"]),
        )

    if kind is CheckKind.LEAN:
        module = entry["challenge"]["module"]
        theorems = tuple(TheoremObligation(name, module) for name in entry["theorems"])
        _require_nonempty(check_id, "theorem", theorems)
        return LeanCheck(
            **common,
            challenge=ModuleRef(module, entry["challenge"]["path"]),
            theorems=theorems,
            profile=entry["profile"],
            permitted_axioms=tuple(entry["permitted_axioms"]),
            toolchain=_toolchain(entry["toolchain"]),
        )

    declared = tuple(entry["bounds"].items())
    properties = tuple(PropertyObligation(name, declared) for name in entry["properties"])
    _require_nonempty(check_id, "model property", properties)
    fingerprint = entry["fingerprint"]
    return TlcCheck(
        **common,
        model=ModuleRef(entry["model"]["module"], entry["model"]["path"]),
        config=entry["config"],
        properties=properties,
        bounds=DomainBounds(declared),
        fingerprint=FingerprintSpec(
            bool(fingerprint["constants_from_config"]),
            bool(fingerprint["checksum_states"]),
            int(fingerprint["workers"]),
        ),
        toolchain=_toolchain(entry["toolchain"]),
    )


def _toolchain(raw: dict) -> ToolchainRef:
    return ToolchainRef(
        raw["tool"],
        raw.get("version"),
        raw.get("comparator"),
        raw.get("jar_sha256"),
    )
