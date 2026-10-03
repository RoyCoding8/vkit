"""The TLC adapter, from the bytes TLC writes, and from a real TLC where one runs.

**The reader is exercised on every host; the checker only where one exists.** Every
refusal this file asserts is a decision made from a report document and a check
declaration, so all of them run on a host with no JRE and no jar. The cases that
need TLC itself are marked and skipped with a stated reason where it is absent.
That split matters: this host has no Java, so if the reader's refusals were only
reachable through a real run they would be untested here and a broken reader would
read green.

**Every string the reader matches was measured against TLC's own source**, not
guessed from an older receipt. `tlc2/output/MP.java` is the authority for the
completion banner, the summary line, the depth line and the simulation banner, and
`tlc2/output/EC.java`'s `ExitStatus` class is the authority for the exit statuses.
The summary line is anchored on its trailing phrase so a `Progress(n)` snapshot,
which carries the same three counts mid-sentence, cannot satisfy it. That anchoring
is `formal/run_tlc.py`'s finding, generalized: its `_final_totals` took the last
match of a looser pattern, and this reader takes the last match of a pattern that
only the final summary can match.

**`formal/run_tlc.py` had one assumption that is corrected rather than carried.**
Its `_read_domain` parsed cardinality out of the `.cfg` text and silently produced
an empty domain for a constant it did not recognise, which reads as "bounded by
nothing" and is a claim nobody checked. `_bounds` here refuses an unresolved or
contradicted bound instead, and a bare integer counts as 1 rather than 0.

**Each negative case is red by construction.** A simulation banner is what TLC
prints under `-simulate`, which the runner never passes; a queue left non-empty is
what a killed run leaves; a property absent from the config is what a config that
omits it produces; and the Java-override case is a classpath entry, which the
`classpath.txt` guard refuses by construction. The real TLC run on CI is what
confirms the shapes, and its transcript is in `formal/results/`.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

import subproc

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from vkit.outcome import Blocked, BlockedReason  # noqa: E402
from vkit.verifiers import tlc_adapter  # noqa: E402
from vkit.verifiers.dispatch import outcome_from_reading  # noqa: E402
from vkit.verifiers.spec import (  # noqa: E402
    CheckKind,
    DomainBounds,
    FingerprintSpec,
    ModuleRef,
    PropertyObligation,
    SubjectRef,
    TlcCheck,
    ToolchainRef,
)

#: A config in the shape TLC reads, with one constant per pinned bound.
CONFIG = """\
CONSTANTS
    Owners    = {"w1", "w2"}
    Resources = {"a", "b"}

SPECIFICATION Spec

INVARIANTS
    TypeOK
    NoDoubleCheckout

CHECK_DEADLOCK FALSE
"""

#: A model whose `Claim` action guards both preconditions, so no reachable state
#: holds the same owner twice. Both facts below were measured by running TLC
#: 2.19 against this file on a real JRE, not assumed:
#:
#:   * `Next` has to be a named relation, not an existential written inline in
#:     `Spec`. TLC refuses the conjunct: "TLC cannot handle this conjunct of the
#:     spec". The first draft here had it inline and exited 150.
#:   * Every operator has to be declared before it is used. `Nobody` after
#:     `TypeOK` is `Unknown operator: 'Nobody'` and also exits 150.
#:
#: It explores 9 states over 7 distinct ones and completes.
MODEL = r"""\
---- MODULE Model ----
EXTENDS Naturals

CONSTANTS Owners, Resources

VARIABLES held

Nobody == "none"

Init == held = [r \in Resources |-> Nobody]

\* Claiming requires the resource to be free AND the claimant to hold nothing.
Claim(r, o) ==
  /\ held[r] = Nobody
  /\ \A s \in Resources : held[s] # o
  /\ held' = [held EXCEPT ![r] = o]

Next == \E r \in Resources, o \in Owners : Claim(r, o)

Spec == Init /\ [][Next]_held

TypeOK == held \in [Resources -> (Owners \cup {Nobody})]

NoDoubleCheckout ==
  \A r, s \in Resources :
    r # s => held[r] = Nobody \/ held[s] = Nobody \/ held[r] # held[s]

====
"""

#: The same model with the second guard removed, so one owner can hold two
#: resources and `NoDoubleCheckout` is violated. Measured against TLC 2.19 on a
#: real JRE: it exits 12, which is `VIOLATION_SAFETY`, prints the three states
#: leading to the violation, and leaves 3 states on the queue because it stops at
#: the first violation. That last fact is why the reader asks about a violation
#: BEFORE it asks whether the exploration was exhaustive.
VIOLATING = MODEL.replace("  /\\ \\A s \\in Resources : held[s] # o\n", "")

BOUNDS = {"Owners": 2, "Resources": 2}


def tlc_available() -> bool:
    """Whether a JRE and a pinned jar are both present.

    Both, not either: TLC needs each, and a host with one of them cannot run the
    adapter's argv at all. `VKIT_TLA2TOOLS_JAR` is the same name
    `formal/run_tlc.py` reads, so an operator who has installed the toolchain for
    that script has it for this one.
    """
    jar = os.environ.get("VKIT_TLA2TOOLS_JAR", "")
    return shutil.which("java") is not None and bool(jar) and Path(jar).is_file()


needs_tlc = pytest.mark.skipif(
    not tlc_available(), reason="no JRE and pinned tla2tools.jar; see the CI job"
)

#: A real TLC 2.19 completion, transcribed from `formal/results/OwnershipAcceptance-tlc.log`
#: committed in this repository. Every number and string is one that run printed, so
#: the reader is tested against output TLC really produced rather than against a
#: shape I expected.
REAL_COMPLETION = """\
TLC2 Version 2.19 of 08 August 2024 (rev: 5a47802)
Running breadth-first search Model-Checking with fp 34 and seed -4547772520934598563 with 1 worker on 8 cores with 1820MB heap and 64MB offheap memory [pid: 39588] (Windows 11 10.0 amd64, Eclipse Adoptium 17.0.20.1 x86_64, MSBDiskFPSet, DiskStateQueue).
Parsing file /home/runner/work/OwnershipAcceptance.tla
Starting... (2026-09-30 02:36:41)
Computing initial states...
Finished computing initial states: 2 distinct states generated at 2026-09-30 02:36:41.
Progress(11) at 2026-09-30 02:36:44: 749,310 states generated (749,310 s/min), 89,759 distinct states found (89,759 ds/min), 30,289 states left on queue.
Model checking completed. No error has been found.
  Estimates of the probability that TLC did not check all reachable states
  because two distinct states had the same fingerprint:
  calculated (optimistic):  val = 1.9E-8
  based on the actual fingerprints:  val = 7.5E-10
1935362 states generated, 207360 distinct states found, 0 states left on queue.
The depth of the complete state graph search is 19.
The average outdegree of the complete state graph is 1 (minimum is 0, the maximum 14 and the 95th percentile is 3).
Finished in 08s at (2026-09-30 02:36:49)
"""

#: A real violation, transcribed from the run above. The wording, the state
#: sequence and the exit status are TLC 2.19's own. Note the 3 states left on the
#: queue: that is what a violation run looks like, because TLC stops at the first
#: violating state, and it is why the reader asks about a violation before it asks
#: whether the exploration was exhaustive.
REAL_VIOLATION = """\
TLC2 Version 2.19 of 08 August 2024 (rev: 5a47802)
Running breadth-first search Model-Checking with fp 105 and seed -7011686989538208830 with 1 worker on 8 cores with 3994MB heap and 64MB offheap memory [pid: 28312] (Windows 11 10.0 amd64, Eclipse Adoptium 17.0.20.1 x86_64, MSBDiskFPSet, DiskStateQueue).
Error: Invariant NoDoubleCheckout is violated.
Error: The behavior up to this point is:
State 1: <Initial predicate>
held = [a |-> "none", b |-> "none"]

State 2: <Claim line 13, col 3 to line 14, col 35 of module Model>
held = [a |-> "w1", b |-> "none"]

State 3: <Claim line 13, col 3 to line 14, col 35 of module Model>
held = [a |-> "w1", b |-> "w1"]

6 states generated, 6 distinct states found, 3 states left on queue.
The depth of the complete state graph search is 3.
The average outdegree of the complete state graph is 4 (minimum is 4, the maximum 4 and the 95th percentile is 4).
Finished in 00s at (2026-10-03 09:27:09)
"""

#: What `-simulate` prints instead. `EC.TLC_STATS_SIMU`, from `tlc2/output/MP.java`.
REAL_SIMULATION = """\
TLC2 Version 2.19 of 08 August 2024 (rev: 5a47802)
Starting... (2026-10-03 02:36:41)
The number of states generated: 5000
Simulation using seed -12345 and aril 1234567
"""

#: A real jar digest, so a report built here names a jar that exists somewhere.
JAR_SHA = "0" * 64


def a_project(tmp_path: Path, config: str = CONFIG) -> Path:
    project = tmp_path / "proj"
    project.mkdir(parents=True, exist_ok=True)
    (project / "Model.tla").write_text(MODEL, encoding="utf-8")
    (project / "Model.cfg").write_text(config, encoding="utf-8")
    return project


def a_check(project: Path, **overrides) -> TlcCheck:
    """A TLC check over `project/Model.tla`, with the given fields replaced."""
    base = dict(
        id="model-check",
        kind=CheckKind.TLC,
        subject=SubjectRef(),
        claim_id="claim",
        cwd=project,
        timeout_seconds=900.0,
        artifact_name="tlc-report.json",
        model=ModuleRef("Model", "Model.tla"),
        config="Model.cfg",
        properties=(
            PropertyObligation("TypeOK", (("Owners", 2), ("Resources", 2))),
            PropertyObligation("NoDoubleCheckout", (("Owners", 2), ("Resources", 2))),
        ),
        bounds=DomainBounds((("Owners", 2), ("Resources", 2))),
        fingerprint=FingerprintSpec(True, False, 1),
        toolchain=ToolchainRef("tlc", jar_sha256=JAR_SHA),
    )
    base.update(overrides)
    return TlcCheck(**base)


def _report(stdout: str = "", **overrides) -> bytes:
    """A runner report carrying `stdout`, with the given fields replaced."""
    document = {
        "version": tlc_adapter.REPORT_VERSION,
        "complete": True,
        "tool": "tlc",
        "tool_version": "TLC2 Version 2.19 of 08 August 2024 (rev: 5a47802)",
        "model": "Model",
        "config": "Model.cfg",
        "jar_sha256": JAR_SHA,
        "exit_code": tlc_adapter.SUCCESS,
        "stdout": stdout,
        "stderr": "",
    }
    document.update(overrides)
    return json.dumps(document).encode("utf-8")


# -------------------------------------------------------- the positive case


def test_a_real_completion_satisfies_every_required_property(tmp_path: Path) -> None:
    """The reader accepts a run TLC really completed, and records its counts.

    Fed the committed transcript of a real TLC 2.19 run, so the strings this
    matches are ones the tool printed rather than ones I expected. The satisfied
    entries carry the state counts because `receipt.v2.json` requires them, and a
    property result without the exploration that produced it is the overstatement
    `claimkind.py` exists to prevent.
    """
    project = a_project(tmp_path)
    result = tlc_adapter.interpret(_report(REAL_COMPLETION), a_check(project))
    assert isinstance(result, tlc_adapter.AdapterResult), result
    assert result.is_pass
    assert result.exploration.distinct == 207360
    assert result.exploration.generated == 1935362
    assert result.exploration.depth == 19
    assert result.checked_properties == ("TypeOK", "NoDoubleCheckout")

    satisfied, counterexamples = result.obligation_results()
    assert counterexamples == []
    assert [entry["obligation"]["obligation"] for entry in satisfied] == [
        "TypeOK", "NoDoubleCheckout",
    ]
    for entry in satisfied:
        assert entry["kind"] == "property_satisfied"
        assert entry["states"]["distinct"] == 207360
        assert entry["states"]["depth"] == 19
    outcome = outcome_from_reading(result)
    assert outcome.to_json()["result"] == "PASS"


def test_the_progress_line_is_not_mistaken_for_the_final_summary(tmp_path: Path) -> None:
    """The counts come from the LAST summary, not the first match.

    `formal/run_tlc.py` measured this: TLC prints a `Progress(n)` line every
    minute with the counts so far, and its first version took the first regex
    match and recorded a snapshot as the result. Here the transcript carries a
    progress line of 89,759 distinct states against a final 207,360, and the
    receipt must carry the final one.
    """
    project = a_project(tmp_path)
    result = tlc_adapter.interpret(_report(REAL_COMPLETION), a_check(project))
    assert result.exploration.distinct == 207360
    assert result.exploration.distinct != 89759


def test_the_receipt_says_a_model_says_nothing_about_an_implementation(
    tmp_path: Path,
) -> None:
    """`claimkind.py`'s prose, which the receipt must not overstate.

    Asserted as a literal substring so a change to `claimkind.py` that loosened
    the sentence fails here rather than quietly changing what a receipt claims.
    """
    project = a_project(tmp_path)
    result = tlc_adapter.interpret(_report(REAL_COMPLETION), a_check(project))
    text = " ".join(result.assumptions())
    assert "any implementation in any language" in text
    assert "correspondence obligation" in text
    assert "not an exhaustive" not in text


# --------------------------------------------------------- the refusals


def test_a_simulation_is_refused(tmp_path: Path) -> None:
    """A sample of states is not an exhaustive exploration.

    TLC prints `EC.TLC_STATS_SIMU` under `-simulate` and exits, which is why the
    reader refuses the output shape rather than trusting its own argv: a config or
    a wrapper can still select simulation, and a sample of any size discharges
    nothing.
    """
    project = a_project(tmp_path)
    result = tlc_adapter.interpret(_report(REAL_SIMULATION), a_check(project))
    assert isinstance(result, Blocked)
    assert result.detail.startswith("simulation_only"), result.detail
    assert result.reason is BlockedReason.ARTIFACT_EMPTY


def test_a_run_that_printed_no_summary_is_refused(tmp_path: Path) -> None:
    """A completion banner with no counts is not a result either.

    TLC prints `EC.TLC_SUCCESS` before the counts, so a run cut off between the
    two leaves a banner and nothing to record. Reading the banner alone would
    report a result with no exploration behind it.
    """
    project = a_project(tmp_path)
    result = tlc_adapter.interpret(
        _report("Model checking completed. No error has been found.\n"),
        a_check(project),
    )
    assert isinstance(result, Blocked)
    assert result.detail.startswith("results_absent"), result.detail


def test_a_run_with_states_left_on_the_queue_is_refused(tmp_path: Path) -> None:
    """A green exit code with work still queued is a cut-short run.

    Both halves are asserted because the failure is precisely that they disagree:
    `formal/run_tlc.py` recorded that TLC exits 0 on a run it stopped early, and
    the queue count is the only thing that tells the two apart.
    """
    project = a_project(tmp_path)
    truncated = REAL_COMPLETION.replace("0 states left on queue", "30,289 states left on queue")
    result = tlc_adapter.interpret(_report(truncated, exit_code=0), a_check(project))
    assert isinstance(result, Blocked)
    assert result.detail.startswith("incomplete_exploration"), result.detail
    # The count is normalized before it reaches a refusal, because a receipt
    # should not carry a locale-dependent separator into durable evidence.
    assert "30289" in result.detail


def test_a_run_with_a_green_exit_and_no_banner_is_refused(tmp_path: Path) -> None:
    """Complete queue, no banner: the run cannot be confirmed to have finished."""
    project = a_project(tmp_path)
    counts_only = (
        "1935362 states generated, 207360 distinct states found, "
        "0 states left on queue.\n"
    )
    result = tlc_adapter.interpret(
        _report(counts_only, exit_code=0), a_check(project)
    )
    assert isinstance(result, Blocked)
    assert result.detail.startswith("incomplete_exploration"), result.detail


@pytest.mark.parametrize("exit_code,token", [
    (151, "incomplete_exploration"),   # ERROR_SPEC_PARSE
    (152, "incomplete_exploration"),   # ERROR_CONFIG_PARSE
    (153, "incomplete_exploration"),   # ERROR_STATESPACE_TOO_LARGE
    (154, "incomplete_exploration"),   # ERROR_SYSTEM
    (255, "incomplete_exploration"),   # ERROR_UNMAPPED
    (77, "incomplete_exploration"),    # FAILURE_LIVENESS_EVAL
])
def test_an_infrastructure_status_is_blocked_not_a_violation(
    tmp_path: Path, exit_code: int, token: str,
) -> None:
    """A run that could not answer is not a run that answered no.

    The statuses are TLC's own, from `EC.ExitStatus`. Each is paired with the
    completion banner present, because that is the case that matters: a run that
    printed the banner and then failed must not be read as a pass, and must not be
    read as a property violation either.
    """
    project = a_project(tmp_path)
    result = tlc_adapter.interpret(
        _report(REAL_COMPLETION, exit_code=exit_code), a_check(project)
    )
    assert isinstance(result, Blocked), f"exit {exit_code} was not blocked"
    assert result.detail.startswith(token), result.detail
    assert result.reason is BlockedReason.INTERNAL_ERROR


def test_an_undocumented_exit_status_is_refused(tmp_path: Path) -> None:
    """An exit status `EC.ExitStatus` does not define is one this reader cannot read.

    TLC maps everything unmapped onto 255, so a 3 here means a wrapper changed it
    or the run was killed by something other than TLC. Guessing a reading for it
    would be a verdict no tool produced.
    """
    project = a_project(tmp_path)
    result = tlc_adapter.interpret(
        _report(REAL_COMPLETION, exit_code=3), a_check(project)
    )
    assert isinstance(result, Blocked)
    assert result.detail.startswith("incomplete_exploration"), result.detail
    assert "not one of the statuses" in result.detail


def test_a_violation_is_a_fail_with_a_counterexample(tmp_path: Path) -> None:
    """The negative case the plan names, and the exit status that carries it.

    `EC.TLC_INVARIANT_VIOLATED_BEHAVIOR` maps to 12. The reading is a FAIL, not a
    BLOCKED, because TLC answered: it found a reachable state that broke a checked
    property. Every required property appears as a counterexample, because TLC
    stops at the first violation and the ones it had not reached were neither
    satisfied nor refuted.
    """
    project = a_project(tmp_path)
    result = tlc_adapter.interpret(
        _report(REAL_VIOLATION, exit_code=tlc_adapter.VIOLATION_SAFETY),
        a_check(project),
    )
    assert isinstance(result, tlc_adapter.AdapterResult), result
    assert not result.is_pass
    outcome = outcome_from_reading(result)
    assert outcome.to_json()["result"] == "FAIL"

    satisfied, counterexamples = result.obligation_results()
    assert satisfied == []
    assert len(counterexamples) == 2
    for entry in counterexamples:
        # `receipt.v2.json`'s counterexample branch carries the obligation and
        # the trace and no `kind`; the kind lives on the satisfied branch only.
        assert set(entry) == {"obligation", "trace"}
        assert entry["obligation"]["kind"] == "property"
    trace = counterexamples[0]["trace"]
    assert "Invariant NoDoubleCheckout is violated" in trace
    assert 'held = [a |-> "w1", b |-> "w1"]' in trace, (
        "the counterexample must name the violating state"
    )


def test_a_violation_reported_in_the_output_is_a_fail_whatever_the_exit(
    tmp_path: Path,
) -> None:
    """A violation marker is read from the output, not inferred from the status.

    `formal/run_tlc.py` measured that TLC prints `EC.TLC_SUCCESS` after the
    safety phase and again after the liveness checks, so a run that violated a
    property prints the banner once, after the violation. The banner alone is
    therefore not a pass signal and a zero status is not either.
    """
    project = a_project(tmp_path)
    result = tlc_adapter.interpret(
        _report(REAL_VIOLATION, exit_code=0), a_check(project)
    )
    assert isinstance(result, tlc_adapter.AdapterResult), result
    assert not result.is_pass


def test_a_required_property_the_config_does_not_check_is_refused(
    tmp_path: Path,
) -> None:
    """A property the run never checked cannot be discharged by it.

    The config names `TypeOK` only, so the required `NoDoubleCheckout` was never
    checked even though the run completed. This is the plan's "run stops before
    completion" case's quieter cousin: everything TLC did do was correct, and the
    obligation is still unmet.
    """
    config = "CONSTANTS\n    Owners = {\"w1\", \"w2\"}\n\nSPECIFICATION Spec\n\nINVARIANTS\n    TypeOK\n"
    project = a_project(tmp_path, config)
    check = a_check(project, bounds=DomainBounds((("Owners", 2),)))
    result = tlc_adapter.interpret(_report(REAL_COMPLETION), check)
    assert isinstance(result, Blocked)
    assert result.detail.startswith("properties_absent"), result.detail
    assert "NoDoubleCheckout" in result.detail


def test_an_unpinned_jar_is_refused(tmp_path: Path) -> None:
    """TLC loads its operators from the jar, so an unpinned jar is an unchecked one.

    TLC resolves a Java override class from the classpath
    (`FilenameToStream.getModuleOverride` looks for `<Module>.class`), so a
    candidate that could supply the jar could supply the model checking. The
    refusal is placed after the jar exists but before it is trusted, and it is a
    policy refusal rather than a tool failure: the jar may be perfectly good and
    this build still cannot say which one it was.
    """
    project = a_project(tmp_path)
    check = a_check(project, toolchain=ToolchainRef("tlc", jar_sha256=None))
    result = tlc_adapter.interpret(_report(REAL_COMPLETION), check)
    assert isinstance(result, Blocked)
    assert result.detail.startswith("jar_unpinned"), result.detail
    assert result.reason is BlockedReason.TOOL_MISSING


def test_a_bound_the_config_does_not_set_is_refused(tmp_path: Path) -> None:
    """A bound nobody configured is a claim about a run that did not happen.

    `formal/run_tlc.py`'s `_read_domain` produced an empty domain for a constant
    it did not recognise, which reads as "bounded by nothing". This refuses
    instead, which is the difference between a missing measurement and a
    measurement of nothing.
    """
    project = a_project(tmp_path)
    check = a_check(project, bounds=DomainBounds((("Owners", 2), ("Resources", 2),
                                                  ("Generations", 2))))
    result = tlc_adapter.interpret(_report(REAL_COMPLETION), check)
    assert isinstance(result, Blocked)
    assert result.detail.startswith("bounds_unresolved"), result.detail
    assert "Generations" in result.detail


def test_a_bound_the_config_contradicts_is_refused(tmp_path: Path) -> None:
    """A run at different bounds is a different claim.

    The config sets `Owners` to two values and the check pinned four. Reporting
    the run under the declared bound would describe a model that was never
    explored, and this is the specific overstatement the receipt's own
    `bounds` field exists to prevent.
    """
    project = a_project(tmp_path)
    check = a_check(
        project,
        bounds=DomainBounds((("Owners", 4), ("Resources", 2))),
        properties=(PropertyObligation("TypeOK", (("Owners", 4), ("Resources", 2))),
                    PropertyObligation("NoDoubleCheckout", (("Owners", 4), ("Resources", 2)))),
    )
    result = tlc_adapter.interpret(_report(REAL_COMPLETION), check)
    assert isinstance(result, Blocked)
    assert result.detail.startswith("bounds_unresolved"), result.detail
    assert "Owners" in result.detail


def test_an_absent_config_is_refused(tmp_path: Path) -> None:
    """There is no recorded configuration, so there is no recorded claim."""
    project = tmp_path / "proj"
    project.mkdir()
    result = tlc_adapter.interpret(_report(REAL_COMPLETION), a_check(project))
    assert isinstance(result, Blocked)
    assert result.detail.startswith("bounds_unresolved"), result.detail


@pytest.mark.parametrize("raw,token", [
    (b"not json", "report_malformed"),
    (b"[]", "report_malformed"),
])
def test_bytes_that_are_not_a_report_are_refused(tmp_path: Path, raw, token) -> None:
    project = a_project(tmp_path)
    result = tlc_adapter.interpret(raw, a_check(project))
    assert isinstance(result, Blocked)
    assert result.detail.startswith(token), result.detail


def test_a_report_of_another_version_is_refused(tmp_path: Path) -> None:
    project = a_project(tmp_path)
    result = tlc_adapter.interpret(_report(REAL_COMPLETION, version=99), a_check(project))
    assert isinstance(result, Blocked)
    assert result.detail.startswith("report_version"), result.detail


def test_a_truncated_report_is_refused(tmp_path: Path) -> None:
    """A runner that never launched reported nothing about the model."""
    project = a_project(tmp_path)
    result = tlc_adapter.interpret(
        _report(complete=False, failure="the pinned tla2tools jar does not exist"),
        a_check(project),
    )
    assert isinstance(result, Blocked)
    assert result.detail.startswith("report_truncated"), result.detail


def test_a_report_with_no_jar_digest_is_refused(tmp_path: Path) -> None:
    """A runner that measured no jar cannot say which checker ran."""
    project = a_project(tmp_path)
    result = tlc_adapter.interpret(_report(REAL_COMPLETION, jar_sha256=""), a_check(project))
    assert isinstance(result, Blocked)
    assert result.detail.startswith("tool_missing"), result.detail


# ------------------------------------------------------ the java-override gate


def test_a_candidate_java_override_is_refused_before_the_run(tmp_path: Path) -> None:
    """A compiled override beside the model is a refusal, not a run.

    TLC resolves a Java override by looking for `<Module>.class` next to the
    `.tla` (`FilenameToStream.getModuleOverride`), and loads it from the
    classpath. So a candidate that can drop a `.class` beside its model can
    replace a trusted operator's implementation with one that returns whatever the
    property needs. The gate refuses the run rather than trusting the classpath,
    and it refuses before a `java` process exists.

    This is the one refusal here that is structural rather than diagnostic: it
    does not depend on reading TLC's output at all, so a run that printed a
    perfect completion cannot get past it.
    """
    from vkit.verifiers import tlc_runner

    project = a_project(tmp_path)
    (project / "Model.class").write_bytes(b"\xca\xfe\xba\xbe not really a class")
    problem = tlc_runner.refuse_java_overrides(project, ("Model.tla",))
    assert problem is not None
    assert "Model.class" in problem
    assert "override" in problem.lower()


def test_a_clean_model_directory_passes_the_override_gate(tmp_path: Path) -> None:
    """The gate refuses only what it names, so a correct run is not blocked."""
    from vkit.verifiers import tlc_runner

    project = a_project(tmp_path)
    assert tlc_runner.refuse_java_overrides(project, ("Model.tla",)) is None


def test_an_override_beside_an_imported_module_is_also_refused(tmp_path: Path) -> None:
    """An override of a module the model imports is the same attack.

    The declared `model.path` is the only file the check names, so a gate that
    looked only there would miss an override dropped beside an imported module.
    Everything in the model's directory is scanned, because that directory is the
    resolver's search path and every `.class` in it is a candidate operator.
    """
    from vkit.verifiers import tlc_runner

    project = a_project(tmp_path)
    (project / "Helpers.tla").write_text("---- MODULE Helpers ----\n====\n", encoding="utf-8")
    (project / "Helpers.class").write_bytes(b"\xca\xfe\xba\xbe")
    problem = tlc_runner.refuse_java_overrides(project, ("Model.tla",))
    assert problem is not None
    assert "Helpers.class" in problem


# ------------------------------------------------------------------ the argv


def test_the_argv_names_the_pinned_jar_and_its_digest(tmp_path: Path) -> None:
    """The jar and its expected digest both travel into the runner.

    Asserted because the digest check is the whole reason the jar is safe to run:
    if the argv stopped carrying it, the check would still pass while the jar
    behind it went unaudited.
    """
    project = a_project(tmp_path)
    argv = tlc_adapter.argv_for(a_check(project), tmp_path / "run", sys.executable)
    assert argv[0] == sys.executable
    assert argv[1].endswith("tlc_runner.py")
    assert JAR_SHA in argv, argv
    assert "--model Model" in " ".join(argv)
    assert "--workers" not in argv, (
        "the worker count belongs to the runner, not the manifest, because "
        "-workers auto makes the summary lines interleave"
    )


def test_dispatch_reaches_the_tlc_adapter(repo, tmp_path: Path) -> None:
    """One dispatch, and `tlc` reaches its adapter through it."""
    from vkit.verifiers.dispatch import ADAPTERS, argv_for, interpret

    assert ADAPTERS[CheckKind.TLC].kind is CheckKind.TLC
    assert set(ADAPTERS) == set(CheckKind)

    project = repo({"Model.tla": MODEL, "Model.cfg": CONFIG})
    definition = {
        "schema_version": 2,
        "checks": [{
            "id": "model-check",
            "kind": "tlc",
            "cwd": ".",
            "timeout_seconds": 900,
            "artifact": "tlc-report.json",
            "subject": {"paths": ["Model.tla"], "digest": None},
            "claim_id": "claim",
            "model": {"module": "Model", "path": "Model.tla"},
            "config": "Model.cfg",
            "properties": ["TypeOK"],
            "bounds": {"Owners": 2, "Resources": 2},
            "fingerprint": {"constants_from_config": True,
                            "checksum_states": False, "workers": 1},
            "toolchain": {"tool": "tlc", "jar_sha256": JAR_SHA},
        }],
    }
    from vkit import manifest as manifest_module

    parsed = manifest_module.parse_manifest_bytes(
        json.dumps(definition).encode("utf-8"),
        project=project, run_dir=project.runs_root, origin="<test>",
    ).checks["model-check"]
    assert parsed.kind is CheckKind.TLC
    assert parsed.evidence_kind().value == "finite_model_checking"
    assert argv_for(parsed, tmp_path / "run", sys.executable)[1].endswith("tlc_runner.py")
    reading = interpret(parsed, b"not json")
    assert isinstance(reading, Blocked)
    assert reading.detail.startswith("report_malformed")


# --------------------------------------------------- the real checker, if any


def _run_real(tmp_path: Path, source: str) -> tuple[bytes, TlcCheck]:
    """Run the real TLC over `source` through the adapter's own runner.

    The jar digest is measured from the file rather than declared, because the
    adapter compares the two and a test that declared a digest the CI jar does not
    have would fail on the digest rather than on anything about the model.
    """
    project = a_project(tmp_path)
    (project / "Model.tla").write_text(source, encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    jar = Path(os.environ["VKIT_TLA2TOOLS_JAR"])
    check = a_check(project, toolchain=ToolchainRef(
        "tlc", jar_sha256=hashlib.sha256(jar.read_bytes()).hexdigest(),
    ))
    done = subproc.run(
        tlc_adapter.argv_for(check, run_dir, sys.executable),
        cwd=str(project), capture_output=True, encoding="utf-8", errors="replace",
        timeout=1800, check=False,
    )
    report = run_dir / check.artifact_name
    assert report.is_file(), f"no report; exit {done.returncode}\n{done.stdout}\n{done.stderr}"
    return report.read_bytes(), check


def test_the_tool_version_is_read_out_of_the_help_text_tlc_prints() -> None:
    """`-help` carries the version, and its escapes are in the way of reading it.

    Measured on TLC 2.19: the NAME section's description line is bolded with an
    ANSI escape before `TLC` and a tab before the text, and the version phrase is
    the second half of that same line. A reader that matched a line STARTING with
    the banner a real run prints found nothing here and recorded `unreported` on
    every report, which is a receipt carrying a tool identity that had in fact
    been measured and thrown away.
    """
    from vkit.verifiers import tlc_runner

    help_text = (
        "\x1b[1mNAME\x1b[0m\r\n\r\n"
        "\tTLC - provides model checking and simulation of TLA+ specifications"
        " - Version 2.19 of 08 August 2024\r\n\r\n"
        "\x1b[1mSYNOPSIS\x1b[0m\r\n"
    )
    assert tlc_runner._version_of({"stdout": help_text, "stderr": ""}) == (
        "TLC 2.19 of 08 August 2024"
    )
    # A run banner is the other spelling, and both are real output.
    banner = "TLC2 Version 2.19 of 08 August 2024 (rev: 5a47802)\n"
    assert tlc_runner._version_of({"stdout": banner, "stderr": ""}) == (
        "TLC2 Version 2.19 of 08 August 2024 (rev: 5a47802)"
    )
    # Neither spelling present is a fact about the answer, not a default.
    assert tlc_runner._version_of({"stdout": "", "stderr": ""}) == "unreported"


@needs_tlc
def test_a_real_model_satisfying_its_properties_passes(tmp_path: Path) -> None:
    """The positive case, run by a real TLC on a real JRE.

    Both facts asserted below were measured against TLC 2.19 rather than assumed:
    the model explores 9 states over 7 distinct ones, and the completion line is
    `0 states left on queue`. The receipt must carry the run's own numbers, not
    the ones this test expects, and the second assertion is what says the reader
    read them rather than echoing the fixture.
    """
    raw, check = _run_real(tmp_path, MODEL)
    result = tlc_adapter.interpret(raw, check)
    assert isinstance(result, tlc_adapter.AdapterResult), result
    assert result.is_pass, result.obligation_results()
    assert result.exploration.left_on_queue == 0
    assert result.exploration.distinct == 7
    assert result.exploration.generated == 9
    assert outcome_from_reading(result).to_json()["result"] == "PASS"


@needs_tlc
def test_a_real_violating_model_fails(tmp_path: Path) -> None:
    """The negative case, and why it cannot pass: TLC finds the state.

    The same model with the "claimant holds nothing" guard removed, so one owner
    can hold two resources and TLC exits 12. This is the case that makes the
    positive case above meaningful: an adapter that accepted every run would pass
    the first and fail this one.

    The states-left-on-queue assertion is here deliberately. TLC stops at the
    first violation, so a violation run leaves work queued, and a reader that
    asked about exhaustion before it asked about a violation would report this
    run as BLOCKED. Measured: 3 states left on queue, exit 12.
    """
    raw, check = _run_real(tmp_path, VIOLATING)
    result = tlc_adapter.interpret(raw, check)
    assert isinstance(result, tlc_adapter.AdapterResult), result
    assert not result.is_pass, "a violated invariant was reported as a pass"
    assert result.exploration.left_on_queue > 0, (
        "TLC stops at the first violation, so this run left states queued; if it "
        "did not, the model changed and this test is no longer measuring that"
    )
    assert outcome_from_reading(result).to_json()["result"] == "FAIL"
    _, counterexamples = result.obligation_results()
    assert len(counterexamples) == 2
    assert "NoDoubleCheckout" in counterexamples[0]["trace"]
