"""Read one TLC run and decide what it established, or why it established nothing.

The half of `kind: "tlc"` that decides nothing about a verdict: every refusal is a
`Blocked` carrying its own reason, and every acceptance is a set of model-property
obligations at the bounds the check pinned, with the state counts TLC reported.

**Why the exit code alone decides nothing.** `plans/10-native-verifiers.md:91`
requires successful exhaustive exploration, and TLC's own source is why that has to
be read from the output rather than the status. `tlc2/TLC.java` ends with
`System.exit(EC.ExitStatus.errorConstantToExitStatus(errorCode))`, and
`tlc2/output/EC.java` maps that constant onto a small vocabulary: 0 success, 11
safety violation, 12 liveness violation, 13 deadlock, 151 spec parse error, 152
config parse error, 255 anything unmapped. So a violation, a config typo and an
unmapped crash are three different exit statuses and a caller that read only
"non-zero" could not tell a model that failed from a run that never started. This
reader reads BOTH: the mapped exit status says which class of answer arrived, and
the completion markers say whether the exploration was exhaustive. Both are
required, and neither substitutes for the other.

**The completion markers, measured rather than remembered.** `tlc2/output/MP.java`
is the authority for every string below:

    EC.TLC_STATS         "%1% states generated, %2% distinct states found,
                          %3% states left on queue."
    EC.TLC_SUCCESS       "Model checking completed. No error has been found.\\n..."
    EC.TLC_SEARCH_DEPTH  "The depth of the complete state graph search is %1%."
    EC.TLC_STATS_SIMU    "The number of states generated: %1%\\nSimulation using
                          seed %2% and aril %3%"

`formal/run_tlc.py` already reasoned about the first three against a real TLC 2.19
run. Two of its findings are carried here and one is corrected. It takes the LAST
match of the summary pattern, because TLC prints a `Progress(n)` line every minute
whose counts are a snapshot rather than a result; the pattern below is anchored on
the trailing "states left on queue." so a progress line cannot satisfy it. And it
treats "0 states left on queue" as the completion signal rather than the absence of
the word "error". Its `_read_domain` parses cardinality out of the `.cfg` text,
which is right in direction and wrong in one respect that is corrected here: it
silently produced an empty domain for a constant spelled differently, and this
reader refuses rather than reporting a model as bounded by nothing.

**The refusals, each with one reason.** The leading token is the whole contract of
that refusal and is covered by a test.

  report_version          the document is not a version this code reads
  report_truncated        the runner did not finish what it set out to do
  report_malformed        the document is not the shape this code reads
  tool_missing            the pinned jar or the JRE is not installed here
  jar_unpinned            no jar digest was pinned, so the operators are unaudited
  jar_mismatched          the jar measures a different digest than the check pinned
  simulation_only         TLC sampled states rather than exploring them
  results_absent          TLC printed no completion summary at all
  incomplete_exploration  the run stopped with states still on the queue
  properties_absent       the config TLC read checked fewer properties than required
  bounds_unresolved       a pinned bound was not a constant the config assigns
  property_violated       a reachable state or execution violated a checked property
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..outcome import Blocked, BlockedReason
from .obligation import PropertyObligation, obligation_to_json
from .spec import TlcCheck

#: The one report version this code reads.
REPORT_VERSION = 1

#: TLC's own exit statuses, from `tlc2/output/EC.java`'s `ExitStatus` class. The
#: class exists because Linux and macOS only carry eight bits, so the many EC
#: constants are folded onto this vocabulary on the way out of the process. This
#: table is that fold, transcribed.
SUCCESS = 0
VIOLATION_ASSUMPTION = 10
VIOLATION_DEADLOCK = 11
VIOLATION_SAFETY = 12
VIOLATION_LIVENESS = 13
VIOLATION_ASSERT = 14
FAILURE_SPEC_EVAL = 75
FAILURE_SAFETY_EVAL = 76
FAILURE_LIVENESS_EVAL = 77
ERROR_SPEC_PARSE = 151
ERROR_CONFIG_PARSE = 152
ERROR_STATESPACE_TOO_LARGE = 153
ERROR_SYSTEM = 154
ERROR_UNMAPPED = 255

#: The exit statuses that mean a checked property did not hold. The plan's FAIL.
VIOLATION_STATUSES = frozenset({
    VIOLATION_ASSUMPTION, VIOLATION_DEADLOCK, VIOLATION_SAFETY,
    VIOLATION_LIVENESS, VIOLATION_ASSERT,
})

#: The exit statuses that mean the run could not answer, as distinct from a run
#: that answered no. These are BLOCKED even though TLC also printed its summary.
INFRASTRUCTURE_STATUSES = frozenset({
    FAILURE_SPEC_EVAL, FAILURE_SAFETY_EVAL, FAILURE_LIVENESS_EVAL,
    ERROR_SPEC_PARSE, ERROR_CONFIG_PARSE, ERROR_STATESPACE_TOO_LARGE,
    ERROR_SYSTEM, ERROR_UNMAPPED,
})

#: The final summary line. Anchored on the trailing phrase so a `Progress(n)`
#: snapshot, which carries the same three counts mid-word, cannot satisfy it.
_SUMMARY = re.compile(
    r"(?P<generated>[\d,]+) states generated, "
    r"(?P<distinct>[\d,]+) distinct states found, "
    r"(?P<left>[\d,]+) states left on queue\."
)

#: The completion banner. `EC.TLC_SUCCESS`, printed by `reportSuccess` on the path
#: that reached the end of the state queue.
_COMPLETED = "Model checking completed. No error has been found."

#: The sampling banner. `EC.TLC_STATS_SIMU`, which `-simulate` produces and which
#: this runner never passes, but a config or a wrapper can still select, so the
#: reader refuses the output shape rather than trusting its own argv.
_SIMULATED = "The number of states generated:"

#: `EC.TLC_SEARCH_DEPTH`.
_DEPTH = re.compile(r"depth of the complete state graph search is ([\d,]+)")

#: How much of a violation report travels into a counterexample.
TRACE_LIMIT = 6000


def _refuse(token: str, reason: BlockedReason, detail: str) -> Blocked:
    """A BLOCKED whose detail leads with a stable, matchable token."""
    return Blocked(reason, f"{token}: {detail}")


# --------------------------------------------------------------- the reading


@dataclass(frozen=True)
class TlcRun:
    """A report whose shape this code has already accepted."""

    version: int
    model: str
    tool_version: str
    jar_sha256: str
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class Exploration:
    """What the run's own output says about how far it got.

    `exhaustive` is a single derived fact rather than three booleans kept in step,
    because the three conditions are not independent and a caller that could set
    them separately could read a partial exploration as a complete one.
    """

    generated: int
    distinct: int
    left_on_queue: int
    depth: int | None
    completed_banner: bool
    violation_reported: bool

    @property
    def exhaustive(self) -> bool:
        """Whether every reachable state was explored and no property broke.

        All three conditions, and the reason is measured: TLC prints
        `EC.TLC_SUCCESS` after the safety phase and again after the liveness
        checks, so a run that violated a property prints it once, after the
        violation. The banner alone is therefore not a pass signal, and neither is
        a zero queue count on a run that already failed.
        """
        return (
            self.left_on_queue == 0
            and self.completed_banner
            and not self.violation_reported
        )


@dataclass(frozen=True)
class AdapterResult:
    """What one TLC run established.

    `observations` and `counterexamples` are separate for the reason
    `pytest_adapter` keeps them separate. On a violation, TLC stops at the first
    failing state, so the properties it had not yet reached are unknown rather
    than satisfied; they appear as counterexamples saying so rather than being
    dropped or reported as passing.
    """

    observations: tuple[tuple[PropertyObligation, dict], ...]
    counterexamples: tuple[tuple[PropertyObligation, str], ...]
    model: str
    tool_version: str
    jar_sha256: str
    exploration: Exploration
    checked_properties: tuple[str, ...]
    config_digest: str

    @property
    def is_pass(self) -> bool:
        return not self.counterexamples

    def scenarios(self) -> tuple:
        """The reading in the shape every existing outcome consumer already reads."""
        from ..outcome import ScenarioResult

        observation = (
            f"TLC explored all {self.exploration.distinct} distinct states of "
            f"{self.model} exhaustively and {self.tool_version} reported no "
            "violation"
        )
        return tuple(
            ScenarioResult(f"{o.property_name}", True, observation)
            for o, _ in self.observations
        ) + tuple(
            ScenarioResult(f"{o.property_name}", False, trace)
            for o, trace in self.counterexamples
        )

    def obligation_results(self) -> tuple[list[dict], list[dict]]:
        """The satisfied obligations and counterexamples, in the receipt's shapes.

        A satisfied property carries the state counts, which `receipt.v2.json`
        requires. A receipt that recorded a property as satisfied without the
        exploration that satisfied it would be the overstatement
        `claimkind.py` exists to prevent.
        """
        satisfied = [
            {
                "kind": "property_satisfied",
                "obligation": obligation_to_json(obligation),
                "states": {
                    "distinct": self.exploration.distinct,
                    "depth": self.exploration.depth or 0,
                    "generated": self.exploration.generated,
                },
            }
            for obligation, _ in self.observations
        ]
        counterexamples = [
            {"obligation": obligation_to_json(obligation), "trace": trace}
            for obligation, trace in self.counterexamples
        ]
        return satisfied, counterexamples

    def assumptions(self) -> tuple[str, ...]:
        """What a reader has to believe for this to mean what it says.

        `claimkind.py` owns the sentence about what a finite-model result does and
        does not establish, and it is quoted rather than restated so the receipt
        and the module that defines FINITE_MODEL cannot drift apart.
        """
        from ..claimkind import ClaimCategory

        return (
            f"the checked properties hold in every reachable state of one finite "
            f"model ({self.model}) at one recorded configuration, which establishes "
            f"{ClaimCategory.FINITE_MODEL.establishes}. It does not establish "
            f"{ClaimCategory.FINITE_MODEL.does_not_establish}",
            f"the properties checked were exactly {', '.join(self.checked_properties) or 'none'}, "
            "read out of the config file TLC itself read rather than out of a "
            "manifest declaration",
            f"the run explored {self.exploration.distinct} distinct states "
            f"({self.exploration.generated} generated, "
            f"{self.exploration.left_on_queue} left on the queue) to depth "
            f"{self.exploration.depth}. A different number of workers, revisions or "
            "resources is a different claim and needs its own run.",
            f"TLC {self.tool_version} was run from a jar measuring {self.jar_sha256}. "
            "TLC loads its operators from that jar, so the digest is what pins the "
            "checker; it does not establish that the model faithfully represents any "
            "implementation.",
            "a model checking result is a statement about the TLA+ specification. An "
            "implementation of the same rules needs its own correspondence "
            "obligation, which this run did not discharge.",
        )


# ------------------------------------------------------------------- argv


def argv_for(check: TlcCheck, run_dir: Path, python: str | None) -> tuple[str, ...]:
    """The exact argument list a TLC check is executed with.

    vkit's interpreter launches the stdlib-only runner, which launches `java`. Two
    hops for the reason `lean_adapter.argv_for` gives: `execution.launch` owns
    process ownership and descendant lifetime, and a runner that forked TLC itself
    would be a second process owner.

    The jar path and its digest both come from the check's `toolchain`, and the
    runner verifies the digest before a `java` process exists. Nothing here is
    derived from a manifest-supplied shell string.
    """
    interpreter = python or _this_interpreter()
    variant = getattr(check, "variant", None) or check
    root = Path(check.cwd)
    tool = variant.toolchain.tool
    java, jar = _tool_paths(tool)
    config = str((root / variant.config).resolve())
    return (
        interpreter, str(_runner_path()),
        "--java", java,
        "--jar", jar,
        "--jar-sha256", variant.toolchain.jar_sha256 or "",
        "--model", variant.model.module,
        "--config", config,
        "--dir", str(root),
        "--metadir", str(run_dir / "tlc-states"),
        "--report", str(run_dir / check.artifact_name),
    )


def _tool_paths(tool: str) -> tuple[str, str]:
    """The JRE and the jar, from the toolchain declaration and the environment.

    `toolchain.tool` names the TOOL, and `manifest.v2.json` constrains it to
    `"lean"` or `"tlc"`. So a bare `"tlc"` is a name rather than a path, and the
    jar has to come from somewhere else: `VKIT_TLA2TOOLS_JAR`, the same variable
    `formal/run_tlc.py` reads, so an operator who installed the toolchain for that
    script has it for this one.

    The first version treated a non-path `tool` as though it were the jar path and
    handed the runner `--jar tlc`, which it refused as a file that does not exist.
    Measured: with a real JDK and a real jar on PATH, the run failed on the tool
    NAME rather than on anything about the model. A tool name is never a file, so
    the two cases are told apart by looking at what the value names.
    """
    looks_like_a_path = (
        tool.endswith(".jar") or "/" in tool or "\\" in tool
    )
    if looks_like_a_path:
        return "java", tool
    return "java", os.environ.get("VKIT_TLA2TOOLS_JAR", tool)


def _runner_path() -> Path:
    """The runner beside this module.

    Resolved from `__file__` rather than from an environment variable, because an
    environment variable is a thing a candidate can set and a path beside the
    shipped adapter is not.
    """
    return Path(__file__).resolve().parent / "tlc_runner.py"


def _this_interpreter() -> str:
    import sys

    return sys.executable


# ---------------------------------------------------------------- the reader


def interpret(raw: bytes, check: TlcCheck) -> AdapterResult | Blocked:
    """What one TLC run established, or the reason it established nothing.

    Ordered so each refusal is about a fact that has to hold before the next
    question is meaningful: a readable report, then the pinned jar, then whether
    the run explored rather than sampled, then the configuration it actually ran,
    and only then the properties.
    """
    variant = getattr(check, "variant", None) or check
    report = _read(raw)
    if isinstance(report, Blocked):
        return report

    if report.exit_code in INFRASTRUCTURE_STATUSES:
        return _refuse(
            "incomplete_exploration", BlockedReason.INTERNAL_ERROR,
            f"TLC exited {report.exit_code}, which tlc2/output/EC.ExitStatus maps "
            "onto an evaluation or tool error rather than a property violation. The "
            "run could not answer, so its counts are not a result: "
            f"{_tail(report.stdout or report.stderr)}",
        )
    if report.exit_code not in (SUCCESS, *VIOLATION_STATUSES):
        return _refuse(
            "incomplete_exploration", BlockedReason.INTERNAL_ERROR,
            f"TLC exited {report.exit_code}, which is not one of the statuses "
            "tlc2/output/EC.ExitStatus documents. An undocumented status is a run "
            "this reader has no reading for.",
        )
    if not variant.toolchain.jar_sha256:
        return _refuse(
            "jar_unpinned", BlockedReason.TOOL_MISSING,
            f"the check pinned no jar_sha256 for its TLC toolchain. TLC loads its "
            "operators from tla2tools.jar, so an unpinned jar is an unaudited "
            "checker, and a candidate that could supply the jar could supply the "
            "model checking.",
        )
    if _SIMULATED in report.stdout:
        return _refuse(
            "simulation_only", BlockedReason.ARTIFACT_EMPTY,
            "TLC printed its simulation banner rather than a completion summary. A "
            "sample of states is not an exhaustive exploration, and no number of "
            "sampled states discharges a finite-model obligation.",
        )

    exploration = _exploration(report)
    if exploration is None:
        return _refuse(
            "results_absent", BlockedReason.ARTIFACT_EMPTY,
            "TLC printed no completion summary, so there is no state count to "
            "record. A run that reported nothing established nothing about the "
            f"model: {_tail(report.stdout or report.stderr)}",
        )
    # A violation is answered before the exhaustion questions, because it makes
    # them the wrong question. TLC stops at the first violating state, so a
    # violation run has states on the queue BY CONSTRUCTION and exits 0 on the
    # paths that report a violation from the output rather than the status. The
    # first version of this reader asked about exhaustion first, so a real
    # violation transcript came back BLOCKED as "cut short" and the FAIL it
    # actually was never reached. Measured: the committed transcript of the
    # counterexample in `formal/results/` has a non-zero queue and a banner.
    violated = exploration.violation_reported or report.exit_code in VIOLATION_STATUSES

    if not violated and report.exit_code == SUCCESS and not exploration.exhaustive:
        return _refuse(
            "incomplete_exploration", BlockedReason.INTERNAL_ERROR,
            f"TLC exited 0 but left {exploration.left_on_queue} states on the queue, "
            "so the exploration was cut short. 'Explored everything' and 'stopped "
            "early' both end with a green exit code, and only the queue count "
            "tells them apart.",
        )
    if not violated and report.exit_code == SUCCESS and not exploration.completed_banner:
        return _refuse(
            "incomplete_exploration", BlockedReason.INTERNAL_ERROR,
            "TLC exited 0 with a complete state queue but never printed its "
            "completion banner, so this reader cannot confirm the run finished "
            "rather than being cut off between the two.",
        )

    config = _config_text(check, variant)
    if isinstance(config, Blocked):
        return config
    text, digest = config

    checked = _checked_properties(text)
    required = {obligation.property_name for obligation in variant.properties}
    unchecked = sorted(required - set(checked))
    if unchecked:
        return _refuse(
            "properties_absent", BlockedReason.SCENARIO_UNKNOWN,
            f"the config TLC read checks {', '.join(sorted(checked)) or 'nothing'}, "
            f"which does not include required propert(y/ies) {', '.join(unchecked)}. "
            "A property the run never checked cannot be discharged by it.",
        )

    bounds = _bounds(text, variant)
    if isinstance(bounds, Blocked):
        return bounds

    if violated:
        return _fail_all(variant, _violation_trace(report.stdout), report, exploration,
                         checked, digest)

    observations = tuple(
        (obligation, {"distinct": exploration.distinct})
        for obligation in variant.properties
    )
    return AdapterResult(
        observations, (),
        variant.model.module, report.tool_version, report.jar_sha256,
        exploration, tuple(checked), digest,
    )


def _fail_all(variant: TlcCheck, trace: str, report: TlcRun,
              exploration: Exploration, checked: tuple[str, ...], digest: str
              ) -> AdapterResult:
    """The FAIL reading, with every required property a counterexample.

    TLC stops at the first violation, so the properties it had not reached were
    neither satisfied nor refuted. Recording them as satisfied would be a claim
    the run did not make, so every required obligation appears as a
    counterexample and the trace says which state the run stopped at.

    The exploration counts are carried through rather than reset: a violation
    happens partway, and the number of states reached before the violation is a
    fact about how far the search got. `_fail_all` records the observed
    exploration, so a reader can see it was partial.
    """
    return AdapterResult(
        (),
        tuple((obligation, trace) for obligation in variant.properties),
        variant.model.module, report.tool_version, report.jar_sha256,
        exploration, checked, digest,
    )


def _exploration(report: TlcRun) -> Exploration | None:
    """The run's own totals, from its LAST summary line.

    `formal/run_tlc.py` measured why the LAST one and not the first: TLC prints a
    `Progress(n)` line every minute carrying the counts so far, and taking the
    first match recorded a snapshot as though it were the result.
    """
    found = list(_SUMMARY.finditer(report.stdout))
    if not found:
        return None
    last = found[-1]
    depth = _DEPTH.search(report.stdout)
    return Exploration(
        generated=int(last.group("generated").replace(",", "")),
        distinct=int(last.group("distinct").replace(",", "")),
        left_on_queue=int(last.group("left").replace(",", "")),
        depth=int(depth.group(1).replace(",", "")) if depth else None,
        completed_banner=_COMPLETED in report.stdout,
        violation_reported=_VIOLATION.search(report.stdout) is not None,
    )


#: The markers TLC prints when a checked property does not hold. `EC.TLC_SUCCESS`
#: is deliberately absent: it is printed after a violation too, on the single-run
#: path, which is why the violation markers are what carry the FAIL.
_VIOLATION = re.compile(
    r"Invariant is violated|Temporal properties were violated|"
    r"Deadlock reached|Action property .* is violated|"
    r"Error: Invariant|Error: Action property|Error: Temporal property|"
    r"The behavior up to this state is:",
)


def _violation_trace(stdout: str) -> str:
    """The state sequence TLC printed, which is the counterexample.

    Taken from the first line TLC marks as a state so the trace starts at the
    violation rather than at the run's banner, and bounded because an
    unbounded one would carry a whole state graph into durable evidence.
    """
    lines = stdout.splitlines()
    start = next(
        (index for index, line in enumerate(lines)
         if line.lstrip().startswith("State ") or _VIOLATION.search(line)),
        None,
    )
    if start is None:
        return _tail(stdout)
    return "\n".join(lines[start:start + 60])[:TRACE_LIMIT]


def _tail(blob: str) -> str:
    """The last few lines of TLC's output, for a refusal that needs to show why."""
    return "\n".join([line for line in blob.splitlines() if line.strip()][-15:])[:2000]


def _config_text(check: Any, variant: TlcCheck) -> tuple[str, str] | Blocked:
    """The config TLC was pointed at, and its digest.

    Read from the file rather than from the check's declaration, because the
    declaration says what the owner intended and the file says what TLC read, and
    a receipt reporting the first as though it were the second would be the
    overstatement `formal/run_tlc.py` refuses when its parsed domain disagrees
    with its recorded one.
    """
    import hashlib

    path = Path(check.cwd) / variant.config
    if not path.is_file():
        return _refuse(
            "bounds_unresolved", BlockedReason.ARTIFACT_MISSING,
            f"the config {path} does not exist, so there is no recorded "
            "configuration for this model result",
        )
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    return raw.decode("utf-8", "replace"), hashlib.sha256(raw).hexdigest()


def _checked_properties(config_text: str) -> tuple[str, ...]:
    """The property names the config's INVARIANTS and PROPERTIES sections name.

    Both sections, because a property checked as an action or temporal property
    is as checked as one checked as an invariant. Read from the config text with
    the same shape `formal/run_tlc.py` used, which is the shape TLC itself reads.
    """
    names: list[str] = []
    for section in ("INVARIANTS", "PROPERTIES"):
        block = re.search(rf"^{section}\n((?:[ \t]+\S+[ \t]*\n)+)", config_text, re.MULTILINE)
        if block:
            names.extend(
                line.strip() for line in block.group(1).splitlines() if line.strip()
            )
    return tuple(names)


def _bounds(config_text: str, variant: TlcCheck) -> dict | Blocked:
    """Each pinned bound, measured against the config that set it.

    A run at different bounds is a different claim, so a bound the config does not
    assign is refused rather than reported from the manifest. This is
    `formal/run_tlc.py`'s `_read_domain` with the silent-empty case turned into a
    refusal: that version produced `{}` for a config whose constants were spelled
    differently, and an empty domain reads as "bounded by nothing", which is a
    claim nobody checked.
    """
    assigned = _constants(config_text)
    unresolved = sorted(name for name, _ in variant.bounds.as_pairs if name not in assigned)
    if unresolved:
        return _refuse(
            "bounds_unresolved", BlockedReason.ARTIFACT_MALFORMED,
            f"the check pinned bound(s) {', '.join(unresolved)}, which the config "
            f"does not assign. The config assigns "
            f"{', '.join(sorted(assigned)) or 'no constants'}. A bound the config "
            "does not set is a claim about a run that did not happen.",
        )
    mismatched = sorted(
        f"{name}={value} (config has {assigned[name]})"
        for name, value in variant.bounds.as_pairs
        if value != assigned[name]
    )
    if mismatched:
        return _refuse(
            "bounds_unresolved", BlockedReason.ARTIFACT_MALFORMED,
            f"the check pinned a bound the config contradicts: "
            f"{'; '.join(mismatched)}. A run at different bounds is a different "
            "claim, and reporting it under the declared one is the lie this "
            "reader's existence prevents.",
        )
    return dict(variant.bounds.as_pairs)


def _constants(config_text: str) -> dict[str, int]:
    """Every constant the config assigns, with its cardinality.

    The cardinality rules are TLC's own, taken from the shapes a TLA+ config
    permits: a `..` range is inclusive at both ends, a set literal is its own
    size, and a bare integer is 1. `formal/run_tlc.py` implemented the first two
    and left a bare integer at 0, which understated a model rather than refusing
    it.
    """
    block = re.search(r"^CONSTANTS\n((?:[ \t]+\S+.*\n)+)", config_text, re.MULTILINE)
    if not block:
        return {}
    found: dict[str, int] = {}
    for line in block.group(1).splitlines():
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$", line)
        if match:
            found[match.group(1)] = _cardinality(match.group(2))
    return found


def _cardinality(value: str) -> int:
    """How many values TLC will take for one constant assignment."""
    text = value.strip()
    if ".." in text:
        low, _, high = text.partition("..")
        try:
            return int(high.strip()) - int(low.strip()) + 1
        except ValueError:
            return -1
    if re.fullmatch(r"-?\d+", text):
        return 1
    inner = text.strip().strip("{}").strip()
    if not inner:
        return 0
    return len([part for part in inner.split(",") if part.strip()])


# ------------------------------------------------------------ the refusals


def _read(raw: bytes) -> TlcRun | Blocked:
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
    exit_code = document.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return _refuse(
            "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
            f"the report carries exit_code {exit_code!r}, which is not an integer",
        )
    if not document.get("jar_sha256"):
        return _refuse(
            "tool_missing", BlockedReason.TOOL_MISSING,
            str(document.get("failure", "the runner reported no jar digest at all")),
        )
    return TlcRun(
        version,
        str(document.get("model", "")),
        str(document.get("tool_version", "")),
        str(document["jar_sha256"]),
        exit_code,
        str(document.get("stdout", "")),
        str(document.get("stderr", "")),
    )


__all__ = [
    "ERROR_UNMAPPED",
    "REPORT_VERSION",
    "SUCCESS",
    "VIOLATION_STATUSES",
    "AdapterResult",
    "Exploration",
    "TlcRun",
    "argv_for",
    "interpret",
]