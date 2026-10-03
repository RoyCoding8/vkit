"""Read one Lean run and decide what it established, or why it established nothing.

An adapter here owes the core two things: the argv that runs the check, and a
reading of the bytes the check wrote. It does not decide the outcome, build the
receipt, or read a terminal. This module is the second half for `kind: "lean"`,
and the half that decides nothing about a verdict: every refusal below is a
`Blocked` carrying its own reason, and every acceptance is a set of theorem
obligations with the axioms each one leaned on.

**Two profiles, and why the weaker one is the default.** `plans/10-native-verifiers.md`
requires agent-generated proofs to be enrolled in the unreviewed profile by
default, with the owner explicitly selecting the reviewed one, and forbids a
silent downgrade. So the default is a function of the declaration, not of what
the host happens to be able to do: `LeanCheck.profile` carries it, and
`DEFAULT_PROFILE` here is what `manifest` puts on a check that did not say. The
reviewed profile additionally requires a fresh second elaboration, which
`lean_runner` performs unconditionally, so the two profiles differ in what the
adapter REFUSES rather than in what it runs. That is deliberate. One spelling of
the run, two sets of refusals over the same bytes, and the stronger set cannot
quietly be skipped by a host that lacks the capability for the weaker one.

**Where the isolation requirement bites.** The unreviewed profile is only honest
on a host that can isolate a proof build. `plans/10-native-verifiers.md:85` says
the stronger profile can require Linux and that Windows process ownership is not
proof-build isolation. So `isolation_required` is a fact about the platform and
`requires_isolation` says so; a review-sourced enrollment on a host without it is
a `isolation_unavailable` BLOCKED naming the missing capability, which is an
environment blocker and not a passed milestone. The unreviewed profile does not
demand a sandbox from the host, because the threat it defends against is a
candidate weakening the CHALLENGE rather than a candidate reaching the machine,
and that one is answered by comparing the challenge digest instead.

**The refusals, each with one reason.** The leading token is the whole contract of
that refusal and is covered by a test.

  report_version          the document is not a version this code reads
  report_truncated        the runner did not finish what it set out to do
  report_malformed        the document is not the shape this code reads
  tool_missing            the pinned Lean toolchain is not installed here
  challenge_absent        the approved challenge module is not where policy says
  challenge_altered       the challenge's bytes differ from the approved digest
  challenge_unknown       the check does not say which challenge it compares to
  isolation_unavailable   the profile needs a capability this host does not have
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..outcome import Blocked, BlockedReason
from .obligation import TheoremObligation, obligation_to_json
from .spec import LeanCheck, LeanProfile

#: The one report version this code reads.
REPORT_VERSION = 1

#: What a check that did not declare a profile gets. Unreviewed, because the plan
#: makes it the default for agent-generated proofs and says the owner must
#: explicitly select the other. Defaulting to reviewed would let a check acquire
#: a stronger trust assumption by omitting a field, which is the inversion the
#: plan forbids.
DEFAULT_PROFILE = LeanProfile.UNREVIEWED

#: The axiom `sorry` introduces. A theorem depending on it is not a theorem, and
#: Lean's own `#print axioms` names it, so the refusal reads the checker's own
#: answer rather than scanning source text for a spelling.
SORRY_AXIOM = "sorryAx"

#: The message kinds that mean a proof-shaped thing is not a proof. Measured on
#: Lean 4.34.1: `sorry` in a declaration reports severity `warning` with kind
#: `hasSorry`, while an unsolved goal reports severity `error`. Both refuse.
UNSOLVED_KINDS = frozenset({"hasSorry", "hasSorry'"})

#: Lean's own three. A check that declares fewer permits fewer; this is only the
#: universe a permitted-axiom name is checked against for shape.
STANDARD_AXIOMS = frozenset({"propext", "Quot.sound", "Classical.choice"})

#: A theorem obligation's theorem is a short name resolved inside its module's
#: namespace. This is the audit file `lean_runner` writes.
AUDIT_FLAG = "--theorems"

#: The runner this adapter launches. A module path rather than a package path,
#: because `VKIT_TRUSTED_LAUNCHER` refuses `import vkit` and `-m` would import the
#: package before the module runs. Measured against the launcher's own contract.
RUNNER_MODULE = "vkit.verifiers.lean_runner"

#: `lean_runner.py` beside this file. Resolved from `__file__` rather than from an
#: environment variable, because an environment variable is a thing a candidate
#: can set and a path beside the shipped adapter is not.
RUNNER_PATH = Path(__file__).resolve().parent / "lean_runner.py"


def isolation_required(profile: str) -> bool:
    """Whether this profile needs a host capability the reviewed path assumes.

    A function of the profile alone, with no branch that consults the platform.
    Reading the host in here would let the answer change with the machine, and
    the machine is not evidence about a proof.
    """
    return profile == LeanProfile.REVIEWED


def requires_isolation() -> bool:
    """Whether this host can isolate a proof build at all.

    Linux, by measurement rather than by name. The reviewed profile's requirement
    is a separate process tree and userland namespaces, which `plans/10` asks CI to
    exercise on Ubuntu. A host that cannot do that reports the capability missing
    rather than pretending a subprocess was an isolation boundary.
    """
    import sys

    return sys.platform.startswith("linux")


def _refuse(token: str, reason: BlockedReason, detail: str) -> Blocked:
    """A BLOCKED whose detail leads with a stable, matchable token."""
    return Blocked(reason, f"{token}: {detail}")


# --------------------------------------------------------------- the reading


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
    tool_version: str
    challenge_sha256: str
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
            f"this run used the {self.profile!r} profile. "
            + (
                "That profile compares the challenge the check pinned against the "
                "candidate's own build and reads the axioms back off the built "
                "module; it does not review the proof source. The owner's selection "
                "of it is the trust assumption."
                if self.profile == LeanProfile.UNREVIEWED
                else "That profile adds a second, independent elaboration of the same "
                "bytes by the same kernel, on a host that isolates the build."
            ),
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


# ------------------------------------------------------------------- argv


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


# ---------------------------------------------------------------- the reader


def interpret(raw: bytes, check: LeanCheck) -> AdapterResult | Blocked:
    """What one Lean run established, or the reason it established nothing.

    The order of the refusals is the order of what has to be true before the next
    question is meaningful: a report nobody can read, then a profile the host
    cannot honour, then a challenge that is not the approved one, then the
    elaboration, then each theorem. A refusal about a theorem on a run whose
    challenge was altered would name the wrong repair.
    """
    variant = getattr(check, "variant", None) or check
    report = _read(raw)
    if isinstance(report, Blocked):
        return report

    profile = variant.profile or DEFAULT_PROFILE
    if isolation_required(profile) and not requires_isolation():
        return _refuse(
            "isolation_unavailable", BlockedReason.PREREQUISITE_MISSING,
            f"this check declared the {profile!r} profile, which requires a host "
            "that isolates a proof build, and this host does not provide one. The "
            "profile is not silently downgraded to "
            f"{LeanProfile.UNREVIEWED!r}: the owner selected the stronger one and "
            "the environment cannot deliver it. The missing capability is "
            "proof-build isolation (a separate, unprivileged process tree); "
            "`plans/10-native-verifiers.md:85` puts this capability on an Ubuntu "
            "CI runner, and this is an environment blocker rather than a result.",
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
        # A missing digest is refused rather than treated as agreement. The first
        # version compared `if report.challenge_sha256 and ...`, which meant a
        # report that recorded no digest at all skipped the comparison entirely
        # and the check passed. An unrecorded digest is the absence of the one
        # measurement that makes "the challenge was not altered" say anything,
        # and its absence must not read as a match.
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

    builds = [step for step in report.steps if step.get("name", "").startswith("build:")]
    if not builds:
        return _refuse(
            "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
            "the report records no elaboration, so there is no kernel check to read",
        )

    errored = [
        step for step in builds
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
    for step in builds:
        sorry = [m for m in step.get("messages", ()) if m.get("kind") in UNSOLVED_KINDS]
        if sorry:
            return _refuse(
                "incomplete_proof", BlockedReason.INTERNAL_ERROR,
                f"{len(sorry)} declaration(s) in the module were left unproved and "
                f"Lean reported {sorry[0]['kind']!r}. A module with an unsolved goal "
                "elaborates and then says nothing about the statement.",
            )

    if isolation_required(profile):
        recheck = builds[1] if len(builds) > 1 else None
        if recheck is None or _digests(builds) != _digests([recheck]):
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


def _digest(path: Path) -> str:
    """sha256 over the file's bytes with CRLF folded to LF.

    The same normalization `dispatch._measure` uses, so a receipt comparing the
    two is comparing like with like, and so a line-ending change alone does not
    read as an altered challenge.
    """
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _digests(steps: tuple[dict, ...]) -> tuple[str, ...]:
    return tuple(step.get("olean_digest", "") for step in steps)


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
            # Sliced off the phrase rather than partitioned on it. Measured on
            # 4.34.1: the two spellings split differently -- the first carries
            # "depends on axioms: [" and partitions on it, the second carries no
            # colon at all and so partitioned to the whole line, leaving a key of
            # "wanted' does not depend on any axioms" and making every dependency
            # free theorem read as unaudited.
            name = data.split("does not depend on any axioms", 1)[0]
            axioms = frozenset()
        else:
            continue
        key = name.strip().strip("'\"").removeprefix(prefix)
        if key:
            found[key] = AuditRecord(key, axioms, True)
    return found


# ------------------------------------------------------------ the refusals


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
    return LeanRun(
        version,
        str(document.get("module", "")),
        str(document.get("tool_version", "")),
        str(document.get("source_sha256", "")),
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
    "isolation_required",
    "requires_isolation",
]