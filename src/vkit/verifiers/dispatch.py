"""One place a check kind is turned into an argv and read back as evidence.

There is a single adapter per kind and `for_kind` is the only way to reach one.
A second dispatch is a second authority about what a verdict means, and the two
would disagree the first time one of them was extended.

**What an adapter owes the core.** Two things: the argument array that runs the
check, and a reading of the bytes the check's own report produced. It owes the
core nothing else. It does not decide the outcome, it does not build the
receipt, and it does not read the runner's console output, because a line of
prose on a terminal is not evidence about a test.

**Why the report travels through a file and not through a pipe.** The runner's
structured output is written to `check.artifact_name` inside the run directory,
and the runner's console output goes to `stdout.log` beside it. They are
separate files, they are separately referenced in the report, and a reader that
wants to know what a run said has to name which of the two it is reading. A
design where both shared one stream would make "the report says the test passed"
and "the report said something about the word passed" the same kind of evidence.

**Why every kind has a real adapter and none is a stub.** `lean` and `tlc` land
here at checkpoint 3, and they land the way the plan requires rather than as a
helper that reports success. Each one launches a real checker through a stdlib-only
runner, reads that checker's own output, and refuses each way that output can fail
to answer. There is no fallback adapter that returns a verdict no checker
produced, because such a thing is worse than an absent one: it converts a missing
capability into a passing milestone.

**The receipt is built here, once.** It is the projection of an `AdapterResult`
onto `schemas/receipt.v2.json`, and every field in it is either measured here or
read from something that already measured it. Nothing is filled with a plausible
default: the check's category comes from `evidence_kind(spec)` rather than from
the adapter, which is what makes a report claiming a stronger category inert.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..claimkind import ClaimCategory
from ..identity import SourceIdentity
from ..outcome import Blocked, Failed, Outcome, Passed, ScenarioResult
from . import lean_adapter, node_adapter, property_adapter, pytest_adapter, tlc_adapter
from .spec import (CheckKind, ScenarioCheck, SubjectRef)
from .obligation import CaseObligation, obligation_to_json

VERIFIER_MODULE = "vkit.verifiers.dispatch"

HASH_LIMIT = (
    "These digests bind the evidence to bytes. They do not establish that the "
    "oracle reading those bytes is correct, and a before/after comparison cannot "
    "see a transient edit that was restored between the two measurements."
)

NORMALIZATION = (
    "File digests are sha256 over the file's bytes with CRLF folded to LF, which "
    "is the form git stores and the form a POSIX checkout reads. A line-ending "
    "change alone does not alter the digest."
)

ORACLE_REVIEW = (
    "The tests this check ran are the ones the approved manifest's `subject` "
    "and `inputs` name, and their source is covered by the recorded source "
    "inventory. That the tests assert what the claim needs is a claim about a "
    "human review, which vkit records rather than verifies."
)




@dataclass(frozen=True)
class Adapter:
    """How one kind is run and read.

    `argv_for(check, run_dir, python)` builds the command and
    `read(check, raw)` interprets what it wrote. Both are required, because an
    adapter that could run something but not read its output would have nothing
    to contribute and a reader that could not be run would have nothing to run.

    The check comes first in both, deliberately. `pytest_adapter.interpret` takes
    its arguments the other way round, because it is the module that does the
    reading and the thing being read is its subject; this wrapper's job is to be
    uniform, and a uniform order is what stops the two being confused at a call
    site. Measured: they were transposed, and the error surfaced deep inside the
    adapter as `'PytestCheck' object has no attribute 'decode'`, which reads like
    a report bug and is not one.
    """

    kind: CheckKind
    argv_for: Callable[[Any, Path, "str | None"], tuple[str, ...]]
    read: Callable[[Any, bytes], Any]

    def identity(self) -> str:
        return f"{VERIFIER_MODULE}:{self.kind.value}"


def _scenario_argv(check: Any, run_dir: Path, python: str | None) -> tuple[str, ...]:
    """A driver check's own command, with the two documented placeholders.

    Read off the parsed check rather than the variant, because a v1 check has no
    variant at all and this is the branch both readings share. A v2 scenario
    check's argv is the same command, carried on the variant and copied onto the
    parsed check by `manifest._wrap`.
    """
    return _substituted(check.argv, run_dir, python or _this_interpreter())


@dataclass(frozen=True)
class ScenarioReading:
    """A driver artifact's scenarios and the refusal if it could not be read.

    Two members rather than one because the v1 parser has always answered this
    way, and it answers in this order: the artifact is decoded and validated
    before anything is said about which scenarios it reported. Collapsing the two
    would mean picking which question to answer first.
    """

    scenarios: tuple[ScenarioResult, ...]
    problem: Blocked | None = None

    @property
    def failing(self) -> tuple[ScenarioResult, ...]:
        return tuple(s for s in self.scenarios if not s.passed)

    def obligation_results(self) -> tuple[list[dict], list[dict]]:
        if self.problem is not None:
            return [], []
        satisfied = [
            {"kind": "case_satisfied", "obligation": obligation_to_json(CaseObligation(s.scenario_id)),
             "observation": s.observation}
            for s in self.scenarios if s.passed
        ]
        counterexamples = [
            {"obligation": obligation_to_json(CaseObligation(s.scenario_id)), "trace": s.observation}
            for s in self.scenarios if not s.passed
        ]
        return satisfied, counterexamples


def _scenario_read(check: Any, raw: bytes) -> ScenarioReading:
    """The v1 check-artifact reading, unchanged since Plan 01.

    Imported inside the function rather than at module scope because
    `execution` imports this module. The alternative is a package-level cycle
    resolved by import order, which is invisible until the order changes; this
    one is a deferred lookup, the same device `execution._revalidate` uses.
    """
    from ..execution import _scenarios_from_artifact

    scenarios, problem = _scenarios_from_artifact(raw, tuple(check.required_scenarios))
    return ScenarioReading(scenarios, problem)


def _pytest_argv(check: Any, run_dir: Path, python: str | None) -> tuple[str, ...]:
    """The pinned runner, told where to write its report and which tests to run.

    Built from the variant, because a pytest check declares no command: its argv
    is the runner's, the runner's own base arguments, vkit's two report flags and
    the required test ids. `manifest._wrap` leaves the parsed check's own `argv`
    empty for every kind but `scenario`, which is the statement that vkit
    constructs this rather than being handed a command.

    The report path is vkit's decision rather than the manifest's, because it has
    to resolve inside this run's directory and only vkit knows which directory
    that is. Everything else about the command is the manifest's, which is why
    the two documented placeholders are the only substitution applied.
    """
    variant = _variant(check)
    interpreter = python or _this_interpreter()
    report = run_dir / check.artifact_name
    return (
        *_substituted((variant.runner.executable,), run_dir, interpreter),
        *_substituted(variant.runner.base_argv, run_dir, interpreter),
        "-p", pytest_adapter.PLUGIN_NAME,
        pytest_adapter.REPORT_FLAG, str(report),
        *variant.required_tests,
    )


def _node_argv(check: Any, run_dir: Path, python: str | None) -> tuple[str, ...]:
    """The pinned Node runner, told where to write its report and which files to run.

    Built exactly as `_pytest_argv` is, and for the same reasons: the check
    declares no command, the runner's own base arguments and the required ids
    come from the variant, and the report path is vkit's decision because it has
    to resolve inside this run's directory.

    The two flags that are not the pytest ones are all measured requirements of
    this runner rather than preferences. `--test-reporter` names vkit's own
    reporter, because stock Node TAP carries a file only on a failing entry and
    required-case accounting needs the file on every one. `--test-isolation=none`
    runs every file in one process, because with the default per-file isolation
    the runner reports each file as a test of its own and a required case would
    be satisfied by a file that ran no case at all; which SPELLING of that flag
    this runner understands is the executable's own answer, because Node 22
    spells it `--experimental-test-isolation` and rejects the unprefixed form
    with `bad option` and exit 9, having written no report at all. And the
    required ids are split into the files to run and the names to select, because
    `node --test` reads its file arguments as filenames. All three are explained
    at length in `node_adapter`, which owns the format.
    """
    variant = _variant(check)
    report = run_dir / check.artifact_name
    files, pattern = node_adapter.argv_selectors(variant.required_tests)
    executable = _substituted((variant.runner.executable,), run_dir, python or _this_interpreter())[0]
    return (
        executable,
        *_substituted(variant.runner.base_argv, run_dir, python or _this_interpreter()),
        node_adapter.isolation_flag(executable),
        "--test-reporter", node_adapter.reporter_argv_token(),
        "--test-reporter-destination", str(report),
        *(("--test-name-pattern", pattern) if pattern else ()),
        *files,
    )


def _property_argv(check: Any, run_dir: Path, python: str | None) -> tuple[str, ...]:
    """The pinned Hypothesis run: the pytest argv, plus the pinned profile.

    Every part of `_pytest_argv` is here, with one flag added, because a
    property check is a pytest run whose required tests happen to be Hypothesis
    tests. The added flag carries the pinned generator settings into the child.

    They travel as an argument rather than being read back out of the run's own
    manifest, and the reason is measured rather than preferred: the check runs
    inside `vkit.integration.launcher`, which refuses `import vkit` in the
    process hosting the check, so a plugin in that process cannot read the
    manifest either. A version that tried read the library's default instead of
    the approved settings and reported 100 examples for a check that pinned 25.
    """
    variant = _variant(check)
    interpreter = python or _this_interpreter()
    report = run_dir / check.artifact_name
    return (
        *_substituted((variant.runner.executable,), run_dir, interpreter),
        *_substituted(variant.runner.base_argv, run_dir, interpreter),
        "-p", pytest_adapter.PLUGIN_NAME,
        "-p", PROPERTY_PLUGIN,
        pytest_adapter.REPORT_FLAG, str(report),
        property_adapter.SETTINGS_FLAG, property_adapter.settings_token(variant),
        *variant.required_tests,
    )


PROPERTY_PLUGIN = "vkit.verifiers.property_adapter"


def _substituted(parts: tuple, run_dir: Path, interpreter: str) -> tuple[str, ...]:
    """Apply the only two placeholders docs/verification.md defines."""
    return tuple(
        str(part).replace("{{run_dir}}", str(run_dir)).replace("{{python}}", interpreter)
        for part in parts
    )


def _this_interpreter() -> str:
    import sys

    return sys.executable


ADAPTERS: dict[CheckKind, Adapter] = {
    CheckKind.SCENARIO: Adapter(
        kind=CheckKind.SCENARIO, argv_for=_scenario_argv, read=_scenario_read,
    ),
    CheckKind.PYTEST: Adapter(
        kind=CheckKind.PYTEST, argv_for=_pytest_argv,
        read=lambda check, raw: pytest_adapter.interpret(raw, _variant(check)),
    ),
    CheckKind.NODE_TEST: Adapter(
        kind=CheckKind.NODE_TEST, argv_for=_node_argv,
        read=lambda check, raw: node_adapter.interpret(raw, _variant(check)),
    ),
    CheckKind.PROPERTY: Adapter(
        kind=CheckKind.PROPERTY, argv_for=_property_argv,
        read=lambda check, raw: property_adapter.interpret(raw, _variant(check)),
    ),
    CheckKind.LEAN: Adapter(
        kind=CheckKind.LEAN, argv_for=lean_adapter.argv_for,
        read=lambda check, raw: lean_adapter.interpret(raw, _variant(check)),
    ),
    CheckKind.TLC: Adapter(
        kind=CheckKind.TLC, argv_for=tlc_adapter.argv_for,
        read=lambda check, raw: tlc_adapter.interpret(raw, _variant(check)),
    ),
}


def for_kind(kind: CheckKind) -> Adapter:
    """The one adapter for a kind. Raises for a kind the enum does not have."""
    try:
        return ADAPTERS[kind]
    except KeyError:
        raise KeyError(
            f"{kind!r} has no adapter, and no default exists: an unrecognised kind "
            f"is a manifest bug upstream, not a check to run"
        ) from None


def _variant(check: Any) -> Any:
    """The adapter's own argument: the parsed check, or the variant behind it.

    The scenario and pytest adapters are written against one shape each, and
    which shape they are handed is a property of the caller rather than of the
    kind. A `manifest.CheckSpec` carries its obligations in its own fields and
    needs no variant; a caller holding a bare `PytestCheck` has no wrapper at
    all. Passing the variant when there is one covers both without asking every
    adapter to know which wrapper it was given.
    """
    return getattr(check, "variant", None) or check


def _kind_of(check: Any) -> CheckKind:
    """The kind of a parsed check, whichever shape a caller is holding.

    `manifest.CheckSpec` and the six variants are both legal arguments here. A
    v1 check has no variant beyond its scenario reading, so it is a `scenario`
    check by the only reading it has.
    """
    variant = getattr(check, "variant", None)
    if variant is None:
        if isinstance(check, ScenarioCheck):
            return CheckKind.SCENARIO
        raise TypeError(
            f"{type(check).__name__} is not a check this dispatch can select an "
            "adapter for; a caller must hand it a parsed check or one of the six "
            "variants"
        )
    return variant.kind


def argv_for(check: Any, run_dir: Path, python: str | None) -> tuple[str, ...]:
    """The exact argument list this check is executed with.

    The parsed check is handed through unchanged. Unwrapping to the variant here
    was wrong for `scenario`, whose variant carries no argv at all, and it was
    applied to every kind because the shape was decided in one place rather than
    by the adapter that needs it.
    """
    return for_kind(_kind_of(check)).argv_for(check, run_dir, python)


def interpret(check: Any, raw: bytes) -> Any:
    """The adapter's reading of what the check wrote.

    Handed the parsed check, for the same reason `argv_for` does. The pytest
    adapter unwraps the variant itself, because only it knows it needs the
    runner and the required test ids rather than the wrapper's fields.
    """
    return for_kind(_kind_of(check)).read(check, raw)




def outcome_from_reading(reading: Any) -> Outcome:
    """The one Outcome an adapter's reading means.

    Every refusal is already a `Blocked` carrying its own reason, and the
    detail's leading token names which of the seven it is, so it passes through
    unchanged. Re-deriving one here would discard exactly the detail the refusal
    exists to give.
    """
    if isinstance(reading, Blocked):
        return reading
    if isinstance(reading, ScenarioReading):
        if reading.problem is not None:
            return reading.problem
        return Failed(reading.scenarios) if reading.failing else Passed(reading.scenarios)
    if hasattr(reading, "scenarios") and hasattr(reading, "counterexamples"):
        scenarios = reading.scenarios()
        return Failed(scenarios) if reading.counterexamples else Passed(scenarios)
    raise TypeError(
        f"an adapter returned {type(reading).__name__}, which is neither a reading "
        "nor a Blocked; every adapter owes the core one of the two"
    )




@dataclass(frozen=True)
class ReceiptInputs:
    """Everything a receipt records that is not the adapter's reading.

    Named and frozen because a receipt built from a partly-filled set of these
    is a receipt with plausible values in fields nothing measured, and that is
    the failure the schema's required members exist to prevent.
    """

    check_id: str
    claim_id: str
    run_id: str
    task_id: str | None
    generation: int | None
    category: ClaimCategory
    subject: SubjectRef
    specification_digest: str | None
    policy_digest: str
    source: SourceIdentity
    fixture_digest: str | None
    tool_versions: dict[str, str]
    runtime: dict[str, str]
    timeout_seconds: float
    report_path: Path
    project_root: Path
    outcome: Outcome
    reading: Any


def build_receipt(inputs: ReceiptInputs) -> dict[str, Any]:
    """The typed receipt for one run, as the document vkit persists.

    Every value is derived here or read from something that measured it. The
    two that are computed from the check's declaration rather than from the run
    are named as such in `assumptions`, because a reader needs to know which
    fields describe what happened and which describe what the policy asked for.

    `status` comes from the outcome and the obligations from the reading, and
    those are two things rather than one because a BLOCKED reading has no
    obligations to record: it established nothing, and a receipt listing
    satisfied cases for a run that was refused would be the lie this whole
    contract exists to prevent.
    """
    status = _status_of(inputs.outcome)
    satisfied, counterexamples = _obligation_results(inputs.reading)
    return {
        "schema_version": 2,
        "check_id": inputs.check_id,
        "claim_id": inputs.claim_id,
        "task_id": inputs.task_id,
        "generation": inputs.generation,
        "run_id": inputs.run_id,
        "status": status,
        "evidence_kind": inputs.category.value,
        "specification_digest": inputs.specification_digest,
        "subject": {
            "paths": list(inputs.subject.paths),
            "digest": _measure(inputs.project_root, inputs.subject),
        },
        "policy_digest": inputs.policy_digest,
        "source": {
            "inventory_digest": inputs.source.inventory_digest,
            "files": [
                {"path": p, "kind": "modified"} for p in inputs.source.dirty_paths
            ],
        },
        "fixture_digest": inputs.fixture_digest,
        "verifier": {
            "module": VERIFIER_MODULE,
            "version": __version__,
            "source_digest": _verifier_digest(),
        },
        "tool_versions": dict(inputs.tool_versions),
        "dependencies": [],
        "runtime": inputs.runtime,
        "satisfied": satisfied,
        "assumptions": _assumptions(inputs),
        "limits": {
            "timeout_seconds": inputs.timeout_seconds,
            "workers": None,
            "seed": None,
        },
        "counterexamples": counterexamples,
        "artifacts": {"report": inputs.report_path.name},
        "trust_boundary": {
            "establishes": inputs.category.establishes,
            "does_not_establish": inputs.category.does_not_establish,
            "oracle_review": ORACLE_REVIEW,
            "hash_limit": HASH_LIMIT,
            "normalization": NORMALIZATION,
        },
    }


def _status_of(outcome: Outcome) -> str:
    if isinstance(outcome, Passed):
        return "PASS"
    if isinstance(outcome, Failed):
        return "FAIL"
    return "BLOCKED"


def _obligation_results(reading: Any) -> tuple[tuple[dict, ...], tuple[dict, ...]]:
    """The satisfied obligations and the counterexamples, in the schema's shapes.

    Every adapter enumerates the obligations its observations establish.
    """
    if hasattr(reading, "obligation_results"):
        return reading.obligation_results()
    return [], []


def _assumptions(inputs: ReceiptInputs) -> list[str]:
    """What a reader has to believe for this receipt to mean what it says.

    The adapter's own assumptions come first and lead, because they are the ones
    only it can state: a property result's generator settings and tested scope
    are facts about what was sampled, and a receipt that recorded the verdict
    without them would be the overstatement `claimkind.py` exists to prevent.
    """
    assumptions: list[str] = []
    if hasattr(inputs.reading, "assumptions"):
        assumptions.extend(inputs.reading.assumptions())
    assumptions.append(
        f"evidence_kind {inputs.category.value!r} is derived from the check's "
        f"declared variant by evidence_kind(spec); no field in the manifest, the "
        f"report or this receipt sets it"
    )
    if inputs.subject.digest is not None:
        assumptions.append(
            f"the owner declared subject digest {inputs.subject.digest}; this "
            f"receipt records vkit's own measurement of the same paths, and a "
            f"reader comparing the two is comparing a declaration to an observation"
        )
    if inputs.fixture_digest is None:
        assumptions.append(
            "no fixture digest was recorded for this run, so what inputs the "
            "evidence was produced against is unresolved"
        )
    return assumptions


def _measure(project_root: Path, subject: SubjectRef) -> str | None:
    """vkit's measurement of the declared subject, or None when it measured nothing.

    Measured rather than copied from the declaration, and only over paths that
    exist. A path that does not exist is not reported as a digest of nothing: the
    measurement is unresolved, which is a different fact from a digest.
    """
    root = project_root
    digests = []
    for relative in subject.paths:
        candidate = (root / relative).resolve()
        if not candidate.is_file():
            return None
        digests.append(
            hashlib.sha256(candidate.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        )
    if not digests:
        return None
    running = hashlib.sha256()
    for digest in digests:
        running.update(digest.encode("ascii"))
    return running.hexdigest()


def _verifier_digest() -> str:
    """This implementation's own digest, so a receipt names bytes and not a name.

    Read from the source of the module that built the receipt rather than from a
    version string, because two builds of one version number can interpret a
    report differently and the receipt is where a reader would look to tell.
    """
    source = Path(__file__).resolve().read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(source).hexdigest()


def persist(receipt: dict[str, Any], report_path: Path) -> None:
    """Write the receipt beside the run's other artifacts, atomically.

    Written to a temporary name and moved, because a reader in another process
    must never see half a document. A half-written receipt would be refused as
    malformed, and the refusal would name a corruption rather than the verdict.
    """
    validate_receipt(receipt)
    staged = report_path.with_suffix(report_path.suffix + ".partial")
    staged.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    staged.replace(report_path)


def validate_receipt(receipt: dict[str, Any]) -> None:
    """Hold the receipt to the boundary schema before anything is persisted."""
    from ..schemas import RECEIPT, SchemaValidationError, validate

    try:
        validate("receipt", RECEIPT, receipt)
    except SchemaValidationError as exc:
        raise SchemaValidationError(
            exc.subject, f"{exc.reason}. A receipt that violates its own schema is "
            "worse than no receipt, so it is not written"
        ) from None


__all__ = [
    "ADAPTERS",
    "HASH_LIMIT",
    "NORMALIZATION",
    "ORACLE_REVIEW",
    "VERIFIER_MODULE",
    "Adapter",
    "ReceiptInputs",
    "argv_for",
    "build_receipt",
    "for_kind",
    "interpret",
    "outcome_from_reading",
    "persist",
    "validate_receipt",
]
