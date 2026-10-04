"""Read one Lean run and decide what it established, or why it established nothing.

An adapter here owes the core two things: the argv that runs the check, and a
reading of the bytes the check wrote. It does not decide the outcome, build the
receipt, or read a terminal. This module is the second half for `kind: "lean"`,
and the half that decides nothing about a verdict: every refusal below is a
`Blocked` carrying its own reason, and every acceptance is a set of theorem
obligations with the axioms each one leaned on.

**Two profiles with different trust assumptions.** `docs/verification.md`
allows reviewed proof sources to be checked by Lean's kernel with a transitive
axiom audit and a fresh recheck. The unreviewed profile also needs a pinned
challenge/solution contract, isolated candidate build, and external comparator.
This adapter has no such contract or comparator, so it refuses every unreviewed
run. Linux alone is not evidence of isolation. The reviewed profile remains
available only for sources the owner has reviewed and admitted.

**The refusals, each with one reason.** The leading token is the whole contract of
that refusal and is covered by a test.

  report_version          the document is not a version this code reads
  report_truncated        the runner did not finish what it set out to do
  report_malformed        the document is not the shape this code reads
  tool_missing            the pinned Lean toolchain is not installed here
  challenge_absent        the approved challenge module is not where policy says
  challenge_altered       the challenge's bytes differ from the approved digest
  challenge_unknown       the check does not say which challenge it compares to
  comparator_unavailable  the unreviewed profile has no supported comparator path
  tool_version_mismatch   the report did not use the declared Lean version
  module_mismatch         the report does not name the declared module and theorems
  elaborator_failed       the module did not elaborate, so nothing was proved
  recheck_diverged        the second elaboration did not match the first
  theorem_absent          a required theorem was not audited
  theorem_holed           the audited theorem depends on `sorryAx`
  axiom_unapproved        the audited theorem leans on an axiom outside policy
  incomplete_proof        the module elaborated with an unsolved goal, an error,
                          or a `hasSorry` message
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..outcome import Blocked, BlockedReason
from .obligation import TheoremObligation, obligation_to_json
from .spec import LeanCheck, LeanProfile

REPORT_VERSION = 1

DEFAULT_PROFILE = LeanProfile.UNREVIEWED

SORRY_AXIOM = "sorryAx"

UNSOLVED_KINDS = frozenset({"hasSorry", "hasSorry'"})

STANDARD_AXIOMS = frozenset({"propext", "Quot.sound", "Classical.choice"})

AUDIT_FLAG = "--theorems"

RUNNER_MODULE = "vkit.verifiers.lean_runner"

RUNNER_PATH = Path(__file__).resolve().parent / "lean_runner.py"


def _refuse(token: str, reason: BlockedReason, detail: str) -> Blocked:
    """A BLOCKED whose detail leads with a stable, matchable token."""
    return Blocked(reason, f"{token}: {detail}")




@dataclass(frozen=True)
class AuditRecord:
    """One `#print axioms` result, as the checker printed it.

    `axioms` is the set, and `reported` says whether the audit actually printed
    anything for this declaration. They are separate because `'x' does not depend
    on any axioms` is a result and a silent absence is not: a runner that stopped
    before the third `#print axioms` must not read as a theorem that leans on
    nothing.
    """

    theorem: str
    axioms: frozenset[str]
    reported: bool


@dataclass(frozen=True)
class LeanRun:
    """A report whose shape this code has already accepted.

    Constructing one is the only way to hold a Lean run, so no caller can hold
    bytes that failed the refusals above.
    """

    version: int
    module: str
    source: str
    profile: str
    tool_version: str
    challenge_sha256: str
    theorems: tuple[str, ...]
    steps: tuple[dict, ...]


@dataclass(frozen=True)
class AdapterResult:
    """What one Lean run established.

    `observations` and `counterexamples` are separate for the reason
    `pytest_adapter` keeps them separate: a run where one theorem holds and
    another leans on an unapproved axiom is a FAIL whose receipt must still name
    what did hold.
    """

    observations: tuple[tuple[TheoremObligation, tuple[str, ...]], ...]
    counterexamples: tuple[tuple[TheoremObligation, str], ...]
    module: str
    profile: str
    tool_version: str
    challenge_sha256: str

    @property
    def is_pass(self) -> bool:
        return not self.counterexamples

    def scenarios(self) -> tuple:
        """The reading in the shape every existing outcome consumer already reads."""
        from ..outcome import ScenarioResult

        return tuple(
            ScenarioResult(f"{o.module}.{o.theorem}", True, _axiom_prose(axioms))
            for o, axioms in self.observations
        ) + tuple(
            ScenarioResult(f"{o.module}.{o.theorem}", False, trace)
            for o, trace in self.counterexamples
        )

    def obligation_results(self) -> tuple[list[dict], list[dict]]:
        """The satisfied obligations and counterexamples, in the receipt's shapes.

        A satisfied theorem carries the axioms the audit reported. That field is
        required by `receipt.v2.json` and has no default precisely because a
        satisfied theorem with no axiom audit is not a theorem result.
        """
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

    def assumptions(self) -> tuple[str, ...]:
        """What a reader has to believe for this to mean what it says.

        `claimkind.py` is the authority for the sentence about the Python core, and
        it is quoted rather than restated so a receipt and the module that defines
        what THEOREM means cannot drift apart.
        """
        from ..claimkind import ClaimCategory

        return (
            f"the theorems hold for the Lean model in module "
            f"{self.module!r} and establish "
            f"{ClaimCategory.THEOREM.establishes}. They do not establish "
            f"{ClaimCategory.THEOREM.does_not_establish}",
            f"this run used the {self.profile!r} profile and assumes the owner "
            "reviewed the proof source and trusts its imported definitions. The "
            "receipt has no recursive import inventory, so this adapter does not "
            "establish that every transitive source dependency was pinned. It adds "
            "a second elaboration by the same Lean kernel; that recheck is not an "
            "independent kernel or an isolation boundary. It also assumes the run "
            "report came from vkit's trusted runner; the adapter does not "
            "authenticate report provenance.",
            f"the challenge module measured {self.challenge_sha256}, and the "
            "comparison is against that byte sequence. A digest binds evidence to "
            "bytes; it does not establish that the challenge states the obligation "
            "the owner intended.",
            "an axiom audit records which axioms a declaration's proof actually "
            "depended on. It cannot see an assumption introduced outside the "
            "audited declaration, and a Python or JavaScript implementation of the "
            "same rules needs its own correspondence obligation, which this run did "
            "not discharge.",
        )


def _axiom_prose(axioms: tuple[str, ...]) -> str:
    if not axioms:
        return "the kernel checked it and `#print axioms` reported no axioms"
    return f"the kernel checked it against {', '.join(axioms)}"




def argv_for(check: LeanCheck, run_dir: Path, python: str | None) -> tuple[str, ...]:
    """The exact argument list a Lean check is executed with.

    vkit's own interpreter launches a stdlib-only runner, which launches Lean. Two
    hops rather than one because `execution.launch` owns process ownership,
    timeouts and descendant lifetime, and a runner that called Lean itself would
    be a second process owner. The runner is named by the path beside this module
    so a candidate cannot redirect the reader by setting an environment variable.
    """
    interpreter = python or _this_interpreter()
    variant = getattr(check, "variant", None) or check
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
    """A repository-relative declared path, resolved against the check's `cwd`.

    Resolved rather than assumed, because the runner has to `cd` somewhere and a
    path resolved against the process's working directory would be a different
    module on a different host.
    """
    root = Path(check.cwd)
    return str((root / relative).resolve())


def _this_interpreter() -> str:
    import sys

    return sys.executable




def interpret(raw: bytes, check: LeanCheck) -> AdapterResult | Blocked:
    """What one Lean run established, or the reason it established nothing.

    Unsupported profiles are refused before the runner report is read, so an
    unreviewed source never reaches Lean. Other checks then require a readable
    report, the declared tool version, the module and challenge, and the theorem.
    """
    variant = getattr(check, "variant", None) or check
    profile = variant.profile or DEFAULT_PROFILE
    if profile == LeanProfile.UNREVIEWED:
        return _refuse(
            "comparator_unavailable", BlockedReason.PREREQUISITE_MISSING,
            "the unreviewed profile needs an approved challenge/solution contract, "
            "an isolated candidate build, and the pinned external comparator. This "
            "adapter implements none of those, so the report cannot discharge the "
            "obligation. A Linux platform check or a second ordinary Lean build is "
            "not isolation or independent comparison.",
        )
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
            "statement than the one on disk now, so the altered-challenge meaning "
            "the plan forbids is not a result.",
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
        record = records.get(obligation.theorem)
        if record is None or not record.reported:
            counterexamples.append((
                obligation,
                f"`#print axioms` said nothing about {obligation.theorem!r}, so the "
                "axioms its proof depends on are unknown",
            ))
            continue
        if SORRY_AXIOM in record.axioms:
            counterexamples.append((
                obligation,
                f"{obligation.module}.{obligation.theorem} depends on {SORRY_AXIOM}, "
                "which is what an unproved goal elaborates to. The declaration "
                "compiled; nothing was proved.",
            ))
            continue
        unapproved = sorted(record.axioms - permitted)
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
        observations.append((obligation, tuple(sorted(record.axioms))))

    return AdapterResult(
        tuple(observations), tuple(counterexamples),
        variant.challenge.module, profile, report.tool_version, measured,
    )


def _lean_version(text: str) -> str | None:
    """Read Lean's declared numeric version, independent of host/commit text."""
    match = re.search(r"\bversion\s+([0-9]+\.[0-9]+\.[0-9]+)\b", text)
    return match.group(1) if match else None


def _digest(path: Path) -> str:
    """sha256 over the file's bytes with CRLF folded to LF.

    The same normalization `dispatch._measure` uses, so a receipt comparing the
    two is comparing like with like, and so a line-ending change alone does not
    read as an altered challenge.
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
    """Which required declarations the audit named but the kernel could not resolve.

    Read off the audit's own error text, because Lean's message for an unknown
    constant names the constant (`Unknown constant 'X'`, measured on 4.34.1) and
    that is the one fact a reader needs to act on.
    """
    blob = " ".join(m["data"] for m in audit.get("messages", ()))
    return tuple(
        theorem for theorem in (t.theorem for t in check.theorems)
        if theorem not in blob
    )


def _audit_records(audit: dict, module: str) -> dict[str, AuditRecord]:
    """Every `#print axioms` result in the audit, keyed by the short theorem name.

    Two spellings and both are real, measured on Lean 4.34.1:

        '<Name>' depends on axioms: [a, b]
        '<Name>' does not depend on any axioms

    Both quote the FULLY QUALIFIED declaration name, so the key is that name with
    the module prefix stripped. The theorem obligation carries the short name, and
    matching on the qualified form rather than the short one would mean every
    theorem reads as absent.

    The axiom names need the same treatment and for a related measured reason.
    The audit file opens `namespace <module>` and prints a short theorem name
    inside it, and Lean then reports axioms relative to that namespace: the same
    declaration audited as `Case.wanted` yields `[Case.cheat]` when the print sits
    at module top level and `[cheat]` when it sits inside the namespace. Left
    unqualified, `Case.cheat` would not match a policy entry naming it
    qualified, and a check that permitted the axiom would refuse its own proof.
    """
    prefix = f"{module}."
    found: dict[str, AuditRecord] = {}
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
            found[key] = AuditRecord(key, axioms, True)
    return found




def _read(raw: bytes) -> LeanRun | Blocked:
    """Decode and shape-check the runner's report, refusing each unusable way."""
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
        version,
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
