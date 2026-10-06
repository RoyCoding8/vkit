"""Builds the argv for a Lean check and reads the runner's report into theorem obligations with the
axioms each used. The unreviewed profile is answered only by the comparator report; the reviewed
profile is kernel-checked twice and axiom-audited.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..outcome import Blocked, BlockedReason, ScenarioResult
from .obligation import TheoremObligation, obligation_to_json
from .spec import LeanCheck, LeanProfile

REPORT_VERSION = 1

DEFAULT_PROFILE = LeanProfile.UNREVIEWED

SORRY_AXIOM = "sorryAx"

UNSOLVED_KINDS = frozenset({"hasSorry", "hasSorry'"})

AUDIT_FLAG = "--theorems"

RUNNER_PATH = Path(__file__).resolve().parent / "lean_runner.py"
COMPARATOR_RUNNER_PATH = Path(__file__).resolve().parent / "comparator_runner.py"


def _refuse(token: str, reason: BlockedReason, detail: str) -> Blocked:
    """A BLOCKED whose detail leads with a stable token that callers and tests match on."""
    return Blocked(reason, f"{token}: {detail}")


@dataclass(frozen=True)
class LeanRun:
    """A report that passed every shape check in `_read`."""

    module: str
    source: str
    profile: str
    tool_version: str
    challenge_sha256: str
    theorems: tuple[str, ...]
    steps: tuple[dict, ...]


@dataclass(frozen=True)
class AdapterResult:
    """What one Lean run established; a run can hold both satisfied theorems and counterexamples."""

    observations: tuple[tuple[TheoremObligation, tuple[str, ...]], ...]
    counterexamples: tuple[tuple[TheoremObligation, str], ...]
    prose: str = ""

    def scenarios(self) -> tuple:
        """The reading as outcome `ScenarioResult`s."""
        return tuple(
            ScenarioResult(f"{o.module}.{o.theorem}", True, self.prose or _axiom_prose(axioms))
            for o, axioms in self.observations
        ) + tuple(
            ScenarioResult(f"{o.module}.{o.theorem}", False, trace)
            for o, trace in self.counterexamples
        )

    def obligation_results(self) -> tuple[list[dict], list[dict]]:
        """Satisfied theorems (with their axioms) and counterexamples as run-record dicts."""
        satisfied = [
            {
                "kind": "theorem_satisfied",
                "obligation": obligation_to_json(obligation),
                "axioms": sorted(axioms),
            }
            for obligation, axioms in self.observations
        ]
        counterexamples = [
            {"obligation": obligation_to_json(obligation), "trace": trace}
            for obligation, trace in self.counterexamples
        ]
        return satisfied, counterexamples


def _comparator_result(raw: bytes, variant: LeanCheck) -> AdapterResult | Blocked:
    try:
        report = json.loads(raw.decode("utf-8"))
        status = report["status"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        return _refuse("report_malformed", BlockedReason.ARTIFACT_MALFORMED, f"comparator report unreadable: {exc}")
    if status == "challenge_changed":
        return _refuse("challenge_changed", BlockedReason.NOT_APPROVED,
                       f"the challenge now digests to {report.get('challenge_sha256')}, not the accepted "
                       f"{variant.challenge_sha256}; a human must accept the new statements")
    if status == "accepted":
        prose = (f"comparator: same statement as the frozen challenge, axioms within "
                 f"{', '.join(variant.permitted_axioms) or 'none'}, kernel accepted; build sandbox: "
                 f"{report.get('sandbox')}")
        return AdapterResult(tuple((t, ()) for t in variant.theorems), (), prose)
    if status == "rejected":
        lines = [line for line in str(report.get("output", "")).splitlines() if line.strip()]
        trace = lines[-1] if lines else f"comparator exited {report.get('exit_code')}"
        return AdapterResult((), tuple((t, trace) for t in variant.theorems))
    return _refuse("report_malformed", BlockedReason.ARTIFACT_MALFORMED, f"unknown comparator status {status!r}")


def _axiom_prose(axioms: tuple[str, ...]) -> str:
    if not axioms:
        return "the kernel checked it and `#print axioms` reported no axioms"
    return f"the kernel checked it against {', '.join(axioms)}"


def argv_for(check: LeanCheck, run_dir: Path, python: str | None) -> tuple[str, ...]:
    """The argument list a Lean check runs with.

    The runner is the script beside this module, never a path from the environment.
    """
    interpreter = python or sys.executable
    variant = getattr(check, "variant", None) or check
    if variant.profile == LeanProfile.UNREVIEWED:
        return (
            interpreter, str(COMPARATOR_RUNNER_PATH),
            "--comparator", variant.toolchain.comparator or "comparator",
            "--project-dir", str(Path(check.cwd)),
            "--challenge-path", variant.challenge.path, "--challenge-sha256", variant.challenge_sha256,
            "--challenge-module", variant.challenge.module, "--solution-module", variant.solution.module,
            "--theorems", ",".join(t.theorem for t in variant.theorems),
            "--axioms", ",".join(variant.permitted_axioms),
            "--run-dir", str(run_dir), "--report", str(run_dir / check.artifact_name),
        )
    return (
        interpreter, str(RUNNER_PATH),
        "--lean", variant.toolchain.tool if _is_path(variant.toolchain.tool) else "lean",
        "--source", _absolute(variant.challenge.path, check),
        "--module", variant.challenge.module,
        AUDIT_FLAG, ",".join(t.theorem for t in variant.theorems),
        "--profile", variant.profile or DEFAULT_PROFILE,
        "--run-dir", str(run_dir),
        "--report", str(run_dir / check.artifact_name),
    )


def _is_path(value: str) -> bool:
    return "/" in value or "\\" in value or value.endswith(".exe")


def _absolute(relative: str, check: Any) -> str:
    """A declared repository-relative path resolved against the check's `cwd`."""
    root = Path(check.cwd)
    return str((root / relative).resolve())


def interpret(raw: bytes, check: LeanCheck) -> AdapterResult | Blocked:
    """What one Lean run established, or why it established nothing."""
    variant = getattr(check, "variant", None) or check
    profile = variant.profile or DEFAULT_PROFILE
    if profile == LeanProfile.UNREVIEWED:
        return _comparator_result(raw, variant)
    if profile != LeanProfile.REVIEWED:
        return _refuse(
            "profile_unsupported", BlockedReason.ARTIFACT_MALFORMED,
            f"the check selected unsupported Lean profile {profile!r}",
        )
    if any(item.module != variant.challenge.module for item in variant.theorems):
        return _refuse(
            "module_mismatch", BlockedReason.ARTIFACT_MALFORMED,
            "every required theorem must name the same module as the declared "
            f"challenge {variant.challenge.module!r}",
        )

    report = _read(raw)
    if isinstance(report, Blocked):
        return report

    expected_version = variant.toolchain.version
    if not isinstance(expected_version, str) or not expected_version.strip():
        return _refuse(
            "toolchain_unpinned", BlockedReason.TOOL_MISSING,
            "the check declares no Lean version, so the kernel that checked the "
            "theorem is unknown",
        )
    actual_version = _lean_version(report.tool_version)
    if actual_version is None:
        return _refuse(
            "tool_version_unreported", BlockedReason.ARTIFACT_MALFORMED,
            f"the report's Lean version {report.tool_version!r} has no readable "
            "numeric version",
        )
    if actual_version != expected_version:
        return _refuse(
            "tool_version_mismatch", BlockedReason.PREREQUISITE_MISSING,
            f"the check pins Lean {expected_version}, but the report names "
            f"Lean {actual_version}",
        )
    expected_theorems = tuple(item.theorem for item in variant.theorems)
    expected_source = Path(_absolute(variant.challenge.path, check)).resolve()
    reported_source = Path(report.source)
    if not reported_source.is_absolute():
        reported_source = Path(check.cwd) / reported_source
    if (report.module != variant.challenge.module
            or report.profile != profile
            or reported_source.resolve() != expected_source
            or report.theorems != expected_theorems):
        return _refuse(
            "module_mismatch", BlockedReason.ARTIFACT_MALFORMED,
            f"the check names module {variant.challenge.module!r}, source "
            f"{str(expected_source)!r}, profile {profile!r}, and theorem list "
            f"{expected_theorems!r}, but the report names module {report.module!r}, "
            f"source {report.source!r}, profile {report.profile!r}, and theorem "
            f"list {report.theorems!r}",
        )

    challenge = Path(_absolute(variant.challenge.path, check))
    if not challenge.is_file():
        return _refuse(
            "challenge_absent", BlockedReason.ARTIFACT_MISSING,
            f"the approved challenge module {challenge} does not exist, so the run "
            "compared nothing against anything",
        )
    measured = _digest(challenge)
    if not report.challenge_sha256:
        return _refuse(
            "challenge_unknown", BlockedReason.ARTIFACT_MALFORMED,
            f"the run recorded no digest for the challenge module it compiled, so "
            f"there is nothing to compare {measured} against. A challenge "
            "comparison that cannot name the bytes it compared proves nothing "
            "about whether the challenge was altered.",
        )
    if report.challenge_sha256 != measured:
        return _refuse(
            "challenge_altered", BlockedReason.SOURCE_CHANGED,
            f"the challenge module measured {measured} and the run read "
            f"{report.challenge_sha256}. The proof was checked against a different "
            "statement than the one on disk now, so it is not a result.",
        )

    elaborations = [
        step for step in report.steps
        if step.get("name", "").startswith(("build:", "recheck:"))
    ]
    initial_builds = [step for step in elaborations if step.get("name") == "build:1"]
    rechecks = [step for step in elaborations if step.get("name") == "recheck:2"]
    if not initial_builds:
        return _refuse(
            "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
            "the report records no elaboration, so there is no kernel check to read",
        )

    errored = [
        step for step in elaborations
        if step.get("exit_code") != 0
        or any(m["severity"] == "error" for m in step.get("messages", ()))
    ]
    if errored:
        detail = _first_error(errored)
        return _refuse(
            "incomplete_proof", BlockedReason.INTERNAL_ERROR,
            f"the module did not elaborate, so nothing was proved: {detail}. A "
            "failed elaboration is a check that could not answer, not a check that "
            "answered no.",
        )
    for step in elaborations:
        sorry = [m for m in step.get("messages", ()) if m.get("kind") in UNSOLVED_KINDS]
        if sorry:
            return _refuse(
                "incomplete_proof", BlockedReason.INTERNAL_ERROR,
                f"{len(sorry)} declaration(s) in the module were left unproved and "
                f"Lean reported {sorry[0]['kind']!r}. A module with an unsolved goal "
                "elaborates and then says nothing about the statement.",
            )

    if profile == LeanProfile.REVIEWED:
        if (len(initial_builds) != 1 or len(rechecks) != 1
                or not initial_builds[0].get("olean_digest")
                or initial_builds[0].get("olean_digest") != rechecks[0].get("olean_digest")):
            return _refuse(
                "recheck_diverged", BlockedReason.INTERNAL_ERROR,
                "the reviewed profile requires a fresh second elaboration of the "
                "same bytes, and the two did not produce the same module. A second "
                "check that cannot reproduce the first is not a recheck.",
            )

    audit = _audit_step(report)
    if audit is None:
        return _refuse(
            "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
            "the report records no axiom audit, so nothing here is a theorem "
            "result rather than a compiled module",
        )
    audit_errors = [m for m in audit.get("messages", ()) if m["severity"] == "error"]
    if audit_errors:
        missing = sorted(_unresolved(audit, variant))
        return _refuse(
            "theorem_absent", BlockedReason.SCENARIO_UNKNOWN,
            f"the axiom audit did not resolve "
            f"{len(missing) or 'a'} required declaration(s): "
            f"{', '.join(missing) if missing else audit_errors[0]['data']}. A "
            "theorem the kernel could not name is not a theorem that holds.",
        )

    records = _audit_records(audit, variant.challenge.module)
    permitted = frozenset(variant.permitted_axioms)
    observations: list[tuple[TheoremObligation, tuple[str, ...]]] = []
    counterexamples: list[tuple[TheoremObligation, str]] = []
    for obligation in variant.theorems:
        axioms = records.get(obligation.theorem)
        if axioms is None:
            counterexamples.append((
                obligation,
                f"`#print axioms` said nothing about {obligation.theorem!r}, so the "
                "axioms its proof depends on are unknown",
            ))
            continue
        if SORRY_AXIOM in axioms:
            counterexamples.append((
                obligation,
                f"{obligation.module}.{obligation.theorem} depends on {SORRY_AXIOM}, "
                "which is what an unproved goal elaborates to. The declaration "
                "compiled; nothing was proved.",
            ))
            continue
        unapproved = sorted(axioms - permitted)
        if unapproved:
            counterexamples.append((
                obligation,
                f"{obligation.module}.{obligation.theorem} depends on "
                f"{', '.join(unapproved)}, which the approved axiom set "
                f"({', '.join(sorted(permitted)) or 'empty'}) does not permit. An "
                "assumption added to make a goal close is not a proof of the "
                "statement.",
            ))
            continue
        observations.append((obligation, tuple(sorted(axioms))))

    return AdapterResult(tuple(observations), tuple(counterexamples))


def _lean_version(text: str) -> str | None:
    """Lean's numeric version from its `--version` text."""
    match = re.search(r"\bversion\s+([0-9]+\.[0-9]+\.[0-9]+)\b", text)
    return match.group(1) if match else None


def _digest(path: Path) -> str:
    """sha256 of the file with CRLF folded to LF, so a line-ending change does not read as an
    altered challenge.
    """
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _first_error(steps: list[dict]) -> str:
    for step in steps:
        for message in step.get("messages", ()):
            if message["severity"] == "error":
                return message["data"].strip().splitlines()[0]
    return f"exit {steps[0].get('exit_code')}"


def _audit_step(report: LeanRun) -> dict | None:
    return next((s for s in report.steps if s.get("name") == "audit"), None)


def _unresolved(audit: dict, check: LeanCheck) -> tuple[str, ...]:
    """Required declarations the audit's error text names as unresolved."""
    blob = " ".join(m["data"] for m in audit.get("messages", ()))
    return tuple(
        theorem for theorem in (t.theorem for t in check.theorems)
        if theorem not in blob
    )


def _audit_records(audit: dict, module: str) -> dict[str, frozenset[str]]:
    """Each `#print axioms` result keyed by short theorem name.

    Lean quotes the fully qualified declaration name, and reports axioms relative to the audit's
    `namespace <module>`, so both are stripped of the module prefix to match the obligation and
    the policy.
    """
    prefix = f"{module}."
    found: dict[str, frozenset[str]] = {}
    for message in audit.get("messages", ()):
        data = message["data"].strip()
        if "depends on axioms: [" in data:
            name, _, tail = data.partition("depends on axioms: [")
            axioms = frozenset(
                part.strip().removeprefix(prefix)
                for part in tail.rstrip("]").split(",") if part.strip()
            )
        elif "does not depend on any axioms" in data:
            name = data.split("does not depend on any axioms", 1)[0]
            axioms = frozenset()
        else:
            continue
        key = name.strip().strip("'\"").removeprefix(prefix)
        if key:
            found[key] = axioms
    return found


def _read(raw: bytes) -> LeanRun | Blocked:
    """Decode and shape-check the runner's report, refusing each unusable form."""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _refuse(
            "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
            f"the report is not UTF-8 JSON: {exc}",
        )
    if not isinstance(document, dict):
        return _refuse(
            "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
            f"the report is a {type(document).__name__}, not an object",
        )
    if document.get("tool") != "lean":
        return _refuse(
            "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
            f"the report names tool {document.get('tool')!r}, not 'lean'",
        )
    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        return _refuse(
            "report_version", BlockedReason.ARTIFACT_MALFORMED,
            f"the report declares version {version!r}, which is not a version number",
        )
    if version != REPORT_VERSION:
        return _refuse(
            "report_version", BlockedReason.ARTIFACT_MALFORMED,
            f"the report is version {version}; this vkit reads version "
            f"{REPORT_VERSION} only",
        )
    if document.get("complete") is not True:
        return _refuse(
            "report_truncated", BlockedReason.ARTIFACT_MALFORMED,
            f"the runner did not finish: {document.get('failure', 'no reason given')}",
        )
    steps = document.get("steps")
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        return _refuse(
            "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
            "the report's steps member is not a list of objects",
        )
    for step in steps:
        name = step.get("name")
        code = step.get("exit_code")
        messages = step.get("messages")
        if (not isinstance(name, str) or not name
                or not isinstance(code, int) or isinstance(code, bool)
                or not isinstance(messages, list)
                or not all(
                    isinstance(message, dict)
                    and isinstance(message.get("severity"), str)
                    and isinstance(message.get("kind"), str)
                    and isinstance(message.get("data"), str)
                    for message in messages
                )):
            return _refuse(
                "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
                "a step is missing its name, integer exit code, or readable messages",
            )
    step_names = [step["name"] for step in steps]
    if "build:1" not in step_names or step_names.count("audit") != 1:
        return _refuse(
            "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
            "the report must contain its initial elaboration and exactly one axiom audit",
        )
    theorems = document.get("theorems")
    if (not isinstance(theorems, list)
            or not all(isinstance(name, str) and name for name in theorems)):
        return _refuse(
            "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
            "the report's theorems member is not a list of names",
        )
    source = document.get("source")
    profile = document.get("profile")
    if not isinstance(source, str) or not source or not isinstance(profile, str):
        return _refuse(
            "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
            "the report's source path or profile is missing",
        )
    return LeanRun(
        str(document.get("module", "")),
        source,
        profile,
        str(document.get("tool_version", "")),
        str(document.get("source_sha256", "")),
        tuple(theorems),
        tuple(steps),
    )


__all__ = [
    "DEFAULT_PROFILE",
    "REPORT_VERSION",
    "RUNNER_PATH",
    "SORRY_AXIOM",
    "AdapterResult",
    "argv_for",
    "interpret",
]
