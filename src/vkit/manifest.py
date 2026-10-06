"""Parse `verification/manifest.json` into checks. The manifest is the only source of commands."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .budgets import Budget
from .inputs import canonical_digest
from .paths import Project
from .schemas import MANIFEST_V2, SchemaValidationError, validate
from .verifiers.obligation import CaseObligation, PropertyObligation, TheoremObligation
from .verifiers.spec import (CheckKind, CheckSpec, DomainBounds, FingerprintSpec, HypothesisSettings, LeanCheck,
                             LeanProfile, ModuleRef, NodeTestCheck, PinnedRunner, Prerequisite, PropertyCheck,
                             PytestCheck, ReplaySettings, ScenarioCheck, StaticCheck, SubjectRef, TlcCheck, ToolchainRef)

MAX_TIMEOUT_SECONDS = 24 * 60 * 60


class ManifestError(Exception):
    """The manifest is unusable, or names something that cannot be run safely."""


@dataclass(frozen=True)
class Manifest:
    project: Project
    description: str
    checks: dict[str, CheckSpec]
    entries: dict[str, dict[str, Any]]

    def require(self, check_id: str) -> CheckSpec:
        try:
            return self.checks[check_id]
        except KeyError:
            known = ", ".join(sorted(self.checks)) or "<none>"
            raise ManifestError(f"unknown check {check_id!r}; manifest defines: {known}") from None

    def digest(self, check_id: str) -> str:
        return canonical_digest(self.entries[check_id])


def parse_manifest(project: Project, path: Path | None = None) -> Manifest:
    path = path or project.manifest_path
    if not path.is_file():
        raise ManifestError(f"no manifest at {path}")
    try:
        raw = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{path} is not readable UTF-8 JSON: {exc}") from exc
    return parse_manifest_data(raw, project=project, origin=str(path))


def parse_manifest_data(raw: Any, *, project: Project, origin: str) -> Manifest:
    if not isinstance(raw, dict) or raw.get("schema_version") != 2:
        raise ManifestError(f"{origin}: expected a JSON object with schema_version 2")
    try:
        validate(origin, MANIFEST_V2, raw)
    except SchemaValidationError as exc:
        raise ManifestError(f"{origin} rejected: {exc.reason}") from exc
    checks: dict[str, CheckSpec] = {}
    entries: dict[str, dict[str, Any]] = {}
    for entry in raw["checks"]:
        if entry["id"] in checks:
            raise ManifestError(f"duplicate check id {entry['id']!r} in {origin}")
        checks[entry["id"]] = _build_check(entry, project)
        entries[entry["id"]] = entry
    return Manifest(project, raw.get("description", ""), checks, entries)


def _inside(root: Path, raw: str, what: str, check_id: str) -> Path:
    candidate = (root / raw).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ManifestError(f"check {check_id!r}: {what} {raw!r} escapes the repository root")
    return candidate


def _artifact_name(raw: str, check_id: str) -> str:
    parts = PurePosixPath(raw.replace("\\", "/")).parts
    if not parts or Path(raw).is_absolute() or ".." in parts:
        raise ManifestError(f"check {check_id!r}: artifact {raw!r} must be a relative name inside the run directory")
    return raw


def _distinct_nonempty(check_id: str, what: str, values: tuple) -> tuple:
    if not values:
        raise ManifestError(f"check {check_id!r}: at least one required {what} is mandatory")
    duplicates = sorted({str(v) for v in values if values.count(v) > 1})
    if duplicates:
        raise ManifestError(f"check {check_id!r}: duplicate required {what} {duplicates}")
    return values


def _command(check_id: str, raw: list) -> tuple[str, ...]:
    command = tuple(raw)
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ManifestError(f"check {check_id!r}: command entries must be nonempty strings")
    return command


def _build_check(entry: dict, project: Project) -> CheckSpec:
    check_id = entry["id"]
    timeout = float(entry["timeout_seconds"])
    if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise ManifestError(f"check {check_id!r}: timeout_seconds must be in (0, {MAX_TIMEOUT_SECONDS}]")
    cwd = _inside(project.root, entry.get("cwd") or ".", "cwd", check_id)
    inputs = list(entry.get("inputs", ()))
    for path in inputs:
        _inside(project.root, path, "input", check_id)
    kind = CheckKind(entry["kind"])
    native = {CheckKind.LEAN: lambda: (entry["challenge"]["path"],
                                       *((entry["solution"]["path"],) if "solution" in entry else ())),
              CheckKind.TLC: lambda: (entry["model"]["path"], entry["config"])}.get(kind, lambda: ())()
    for path in native:
        relative = _inside(project.root, str(cwd / path), "native input", check_id).relative_to(
            project.root.resolve()).as_posix()
        if inputs and relative not in inputs:
            inputs.append(relative)
    expectations = entry.get("expectations")
    if expectations:
        undeclared = sorted(set(expectations) - set(inputs))
        if undeclared:
            raise ManifestError(f"check {check_id!r}: expectations must also be inputs: {', '.join(undeclared)}")
    subject = entry.get("subject") or {}
    common: dict[str, Any] = dict(
        id=check_id, cwd=cwd, timeout_seconds=timeout,
        artifact_name=_artifact_name(entry["artifact"], check_id),
        subject=SubjectRef(tuple(subject.get("paths", ())), subject.get("digest")),
        claim_id=entry.get("claim_id", check_id), description=entry.get("description", ""),
        prerequisites=tuple(Prerequisite(p["name"], p["executable"], tuple(p.get("args", ())))
                            for p in entry.get("prerequisites", ())),
        inputs=tuple(inputs), expectations=None if expectations is None else tuple(expectations),
    )
    if kind is CheckKind.SCENARIO:
        names = _distinct_nonempty(check_id, "scenario", tuple(entry["required_scenarios"]))
        budgets = tuple(Budget(b["name"], b.get("limit"), b.get("max_regression_pct")) for b in entry.get("budgets", ()))
        return ScenarioCheck(**common, command=_command(check_id, entry["command"]),
                             required_scenarios=tuple(CaseObligation(n) for n in names), budgets=budgets)
    if kind is CheckKind.STATIC:
        return StaticCheck(**common, command=_command(check_id, entry["command"]))
    if kind in (CheckKind.PYTEST, CheckKind.PROPERTY, CheckKind.NODE_TEST):
        runner = dict(
            required_tests=_distinct_nonempty(check_id, "test", tuple(entry["required_tests"])),
            runner=PinnedRunner(entry["runner"]["executable"], tuple(entry["runner"]["base_argv"])),
            report_format=entry["report_format"], expect_report_version=int(entry["expect_report_version"]),
        )
        if kind is CheckKind.PYTEST:
            return PytestCheck(**common, **runner)
        if kind is CheckKind.NODE_TEST:
            return NodeTestCheck(**common, **runner)
        generator = entry["generator"]
        replay = entry.get("replay")
        return PropertyCheck(
            **common, **runner,
            generator=HypothesisSettings(int(generator["max_examples"]), int(generator["stateful_step_count"]),
                                         generator["deadline"], tuple(generator["suppress_health_check"])),
            replay=ReplaySettings(replay["database"], replay["seed"]) if replay else None,
        )
    if kind is CheckKind.LEAN:
        module = entry["challenge"]["module"]
        profile = LeanProfile(entry["profile"])
        solution = entry.get("solution")
        if profile is LeanProfile.UNREVIEWED and (solution is None or "challenge_sha256" not in entry):
            raise ManifestError(f"check {check_id!r}: the unreviewed_agent profile needs a solution module and "
                                "the challenge_sha256 that freezes the statements")
        return LeanCheck(
            solution=None if solution is None else ModuleRef(solution["module"], solution["path"]),
            challenge_sha256=entry.get("challenge_sha256"),
            **common, challenge=ModuleRef(module, entry["challenge"]["path"]),
            theorems=_distinct_nonempty(check_id, "theorem",
                                        tuple(TheoremObligation(n, module) for n in entry["theorems"])),
            profile=profile, permitted_axioms=tuple(entry["permitted_axioms"]),
            toolchain=_toolchain(entry["toolchain"]),
        )
    bounds = tuple(entry["bounds"].items())
    fingerprint = entry["fingerprint"]
    return TlcCheck(
        **common, model=ModuleRef(entry["model"]["module"], entry["model"]["path"]), config=entry["config"],
        properties=_distinct_nonempty(check_id, "model property",
                                      tuple(PropertyObligation(n, bounds) for n in entry["properties"])),
        bounds=DomainBounds(bounds),
        fingerprint=FingerprintSpec(bool(fingerprint["constants_from_config"]),
                                    bool(fingerprint["checksum_states"]), int(fingerprint["workers"])),
        toolchain=_toolchain(entry["toolchain"]),
    )


def _toolchain(raw: dict) -> ToolchainRef:
    return ToolchainRef(raw["tool"], raw.get("version"), raw.get("comparator"), raw.get("jar_sha256"))
