"""Builds the argv for a TLC check and reads the runner's report into property obligations with the
state counts TLC reported. A pass needs both the mapped exit status and TLC's completion markers
in its output; neither alone decides.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..outcome import Blocked, BlockedReason, ScenarioResult
from .obligation import PropertyObligation, obligation_to_json
from .spec import TlcCheck

REPORT_VERSION = 2

# The only fingerprint settings the runner supports.
SUPPORTED_FINGERPRINT = (True, False, 1)

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

VIOLATION_STATUSES = frozenset({
    VIOLATION_ASSUMPTION, VIOLATION_DEADLOCK, VIOLATION_SAFETY,
    VIOLATION_LIVENESS, VIOLATION_ASSERT,
})

INFRASTRUCTURE_STATUSES = frozenset({
    FAILURE_SPEC_EVAL, FAILURE_SAFETY_EVAL, FAILURE_LIVENESS_EVAL,
    ERROR_SPEC_PARSE, ERROR_CONFIG_PARSE, ERROR_STATESPACE_TOO_LARGE,
    ERROR_SYSTEM, ERROR_UNMAPPED,
})

_SUMMARY = re.compile(
    r"(?P<generated>[\d,]+) states generated, "
    r"(?P<distinct>[\d,]+) distinct states found, "
    r"(?P<left>[\d,]+) states left on queue\."
)

_COMPLETED = "Model checking completed. No error has been found."

_SIMULATED = "The number of states generated:"

_DEPTH = re.compile(r"depth of the complete state graph search is ([\d,]+)")

TRACE_LIMIT = 6000


def _refuse(token: str, reason: BlockedReason, detail: str) -> Blocked:
    """A BLOCKED whose detail leads with a stable token that callers and tests match on."""
    return Blocked(reason, f"{token}: {detail}")


@dataclass(frozen=True)
class TlcRun:
    """A report that passed every shape check in `_read`."""

    model: str
    model_path: str
    config: str
    tool_version: str
    jar_sha256: str
    fingerprint: tuple[bool, bool, int]
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class Exploration:
    """How far the run got, per its own output. `exhaustive` is derived so partial exploration
    cannot be set as complete.
    """

    generated: int
    distinct: int
    left_on_queue: int
    depth: int | None
    completed_banner: bool
    violation_reported: bool

    @property
    def exhaustive(self) -> bool:
        """Every reachable state explored and no property broken.

        TLC prints its success banner even after a violation, so the banner alone is not a pass.
        """
        return (
            self.left_on_queue == 0
            and self.completed_banner
            and not self.violation_reported
        )


@dataclass(frozen=True)
class AdapterResult:
    """What one TLC run established. TLC stops at the first failing state, so only the property it
    names is a counterexample.
    """

    observations: tuple[PropertyObligation, ...]
    counterexamples: tuple[tuple[PropertyObligation, str], ...]
    model: str
    tool_version: str
    exploration: Exploration

    def scenarios(self) -> tuple:
        """The reading as outcome `ScenarioResult`s."""
        observation = (
            f"TLC explored all {self.exploration.distinct} distinct states of "
            f"{self.model} exhaustively and {self.tool_version} reported no "
            "violation"
        )
        return tuple(
            ScenarioResult(o.property_name, True, observation)
            for o in self.observations
        ) + tuple(
            ScenarioResult(o.property_name, False, trace)
            for o, trace in self.counterexamples
        )

    def obligation_results(self) -> tuple[list[dict], list[dict]]:
        """Satisfied properties (with state counts) and counterexamples as run-record dicts."""
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
            for obligation in self.observations
        ]
        counterexamples = [
            {"obligation": obligation_to_json(obligation), "trace": trace}
            for obligation, trace in self.counterexamples
        ]
        return satisfied, counterexamples


def argv_for(check: TlcCheck, run_dir: Path, python: str | None) -> tuple[str, ...]:
    """The argument list a TLC check runs with.

    The runner is the script beside this module, and it verifies the jar digest before any
    `java` process exists.
    """
    interpreter = python or sys.executable
    variant = getattr(check, "variant", None) or check
    root = Path(check.cwd)
    tool = variant.toolchain.tool
    java, jar = _tool_paths(tool)
    fingerprint = variant.fingerprint
    return (
        interpreter, str(_runner_path()),
        "--java", java,
        "--jar", jar,
        "--jar-sha256", variant.toolchain.jar_sha256 or "",
        "--expected-version", variant.toolchain.version or "",
        "--model", variant.model.module,
        "--model-path", variant.model.path,
        "--config", variant.config,
        "--root", str(root.resolve()),
        "--constants-from-config", str(fingerprint.constants_from_config).lower(),
        "--checksum-states", str(fingerprint.checksum_states).lower(),
        "--workers", str(fingerprint.workers),
        "--metadir", str(run_dir / "tlc-states"),
        "--report", str(run_dir / check.artifact_name),
    )


def _tool_paths(tool: str) -> tuple[str, str]:
    """The JRE and the jar for a toolchain declaration.

    `toolchain.tool` is a name (`"tlc"`), not a path, so the jar comes from `VKIT_TLA2TOOLS_JAR`
    unless the value already looks like a path.
    """
    looks_like_a_path = tool.endswith(".jar") or "/" in tool or "\\" in tool
    if looks_like_a_path:
        return "java", tool
    return "java", os.environ.get("VKIT_TLA2TOOLS_JAR", tool)


def _runner_path() -> Path:
    """The runner beside this module, never a path from the environment."""
    return Path(__file__).resolve().parent / "tlc_runner.py"


def interpret(raw: bytes, check: TlcCheck) -> AdapterResult | Blocked:
    """What one TLC run established, or why it established nothing."""
    variant = getattr(check, "variant", None) or check
    expected_fingerprint = (
        variant.fingerprint.constants_from_config,
        variant.fingerprint.checksum_states,
        variant.fingerprint.workers,
    )
    if expected_fingerprint != SUPPORTED_FINGERPRINT:
        return _refuse(
            "fingerprint_unsupported", BlockedReason.PREREQUISITE_MISSING,
            f"the check declares fingerprint settings {expected_fingerprint!r}; "
            f"this runner supports only {SUPPORTED_FINGERPRINT!r}",
        )
    expected_version = variant.toolchain.version
    if not isinstance(expected_version, str) or not expected_version.strip():
        return _refuse(
            "toolchain_unpinned", BlockedReason.TOOL_MISSING,
            "the check declares no TLC version, so the checker that explored the "
            "model is unknown",
        )
    if not variant.toolchain.jar_sha256:
        return _refuse(
            "jar_unpinned", BlockedReason.TOOL_MISSING,
            "the check declares no tla2tools jar digest, so the checker that "
            "explored the model is unknown",
        )
    report = _read(raw)
    if isinstance(report, Blocked):
        return report

    if report.model != variant.model.module:
        return _refuse(
            "model_mismatch", BlockedReason.ARTIFACT_MALFORMED,
            f"the check names model {variant.model.module!r}, but the report names "
            f"{report.model!r}",
        )
    if report.model_path != variant.model.path or report.config != variant.config:
        return _refuse(
            "source_path_mismatch", BlockedReason.ARTIFACT_MALFORMED,
            f"the check names model path {variant.model.path!r} and config "
            f"{variant.config!r}, but the report names model path "
            f"{report.model_path!r} and config {report.config!r}",
        )
    if report.fingerprint != expected_fingerprint:
        return _refuse(
            "fingerprint_mismatch", BlockedReason.ARTIFACT_MALFORMED,
            f"the check declares fingerprint settings {expected_fingerprint!r}, "
            f"but the report records {report.fingerprint!r}",
        )
    if report.jar_sha256 != variant.toolchain.jar_sha256:
        return _refuse(
            "jar_mismatch", BlockedReason.PREREQUISITE_MISSING,
            f"the check pins tla2tools.jar sha256 "
            f"{variant.toolchain.jar_sha256}, but the report records "
            f"{report.jar_sha256}",
        )
    actual_version = _tlc_version(report.tool_version)
    if actual_version is None:
        return _refuse(
            "tool_version_unreported", BlockedReason.ARTIFACT_MALFORMED,
            f"the report's TLC version {report.tool_version!r} has no readable "
            "numeric version",
        )
    if actual_version != expected_version:
        return _refuse(
            "tool_version_mismatch", BlockedReason.PREREQUISITE_MISSING,
            f"the check pins TLC {expected_version}, but the report names "
            f"TLC {actual_version}",
        )

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

    text = _config_text(check, variant)
    if isinstance(text, Blocked):
        return text

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
        return _failed_property(variant, _violation_trace(report.stdout), report, exploration)

    return AdapterResult(
        tuple(variant.properties), (), variant.model.module, report.tool_version, exploration,
    )


def _failed_property(variant: TlcCheck, trace: str, report: TlcRun,
                     exploration: Exploration) -> AdapterResult | Blocked:
    """Attribute a violation to the one required property TLC named, or refuse."""
    match = re.search(
        r"Error: (?:Invariant|Temporal property|Action property) "
        r"([A-Za-z_][A-Za-z0-9_]*) is violated\.", report.stdout,
    )
    if not match:
        return _refuse(
            "counterexample_unattributed", BlockedReason.SCENARIO_UNKNOWN,
            "TLC reported a violation but did not name the failed property, so the "
            "trace cannot be assigned to a required obligation",
        )
    failures = [
        obligation for obligation in variant.properties
        if obligation.property_name == match.group(1)
    ]
    if len(failures) != 1:
        return _refuse(
            "counterexample_unattributed", BlockedReason.SCENARIO_UNKNOWN,
            f"TLC named violated property {match.group(1)!r}, which does not map to "
            "exactly one required obligation",
        )
    return AdapterResult(
        (), ((failures[0], trace),), variant.model.module, report.tool_version, exploration,
    )


def _exploration(report: TlcRun) -> Exploration | None:
    """The run's totals from the LAST summary line, since periodic `Progress(n)` lines carry
    snapshots.
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


_VIOLATION = re.compile(
    r"Invariant is violated|Temporal properties were violated|"
    r"Deadlock reached|Action property .* is violated|"
    r"Error: Invariant|Error: Action property|Error: Temporal property|"
    r"The behavior up to this state is:",
)


def _violation_trace(stdout: str) -> str:
    """The state sequence TLC printed, from the first state or violation line, bounded."""
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
    """The last non-empty lines of TLC's output, for refusals that need to show why."""
    return "\n".join([line for line in blob.splitlines() if line.strip()][-15:])[:2000]


def _config_text(check: Any, variant: TlcCheck) -> str | Blocked:
    """The config file TLC was pointed at, read from disk rather than from the check's declaration.
    """
    path = Path(check.cwd) / variant.config
    if not path.is_file():
        return _refuse(
            "bounds_unresolved", BlockedReason.ARTIFACT_MISSING,
            f"the config {path} does not exist, so there is no recorded "
            "configuration for this model result",
        )
    return path.read_bytes().replace(b"\r\n", b"\n").decode("utf-8", "replace")


def _checked_properties(config_text: str) -> tuple[str, ...]:
    """Property names in the config's INVARIANTS and PROPERTIES sections."""
    names: list[str] = []
    for section in ("INVARIANTS", "PROPERTIES"):
        block = re.search(rf"^{section}\n((?:[ \t]+\S+[ \t]*\n)+)", config_text, re.MULTILINE)
        if block:
            names.extend(
                line.strip() for line in block.group(1).splitlines() if line.strip()
            )
    return tuple(names)


def _bounds(config_text: str, variant: TlcCheck) -> dict | Blocked:
    """Each pinned bound checked against the config that set it; a bound the config does not
    assign, or contradicts, is refused.
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
    """Every constant the config assigns, with its cardinality (`..` ranges are inclusive, a bare
    integer is 1).
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
    """How many values one constant assignment takes."""
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


def _read(raw: bytes) -> TlcRun | Blocked:
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
    if document.get("tool") != "tlc":
        return _refuse(
            "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
            f"the report names tool {document.get('tool')!r}, not 'tlc'",
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
        failure = str(document.get("failure", "no reason given"))
        refusal_reasons = {
            "environment_unsupported": BlockedReason.PREREQUISITE_MISSING,
            "fingerprint_unsupported": BlockedReason.PREREQUISITE_MISSING,
            "java_override_unsupported": BlockedReason.PREREQUISITE_MISSING,
            "jar_unpinned": BlockedReason.TOOL_MISSING,
            "tool_version_mismatch": BlockedReason.PREREQUISITE_MISSING,
            "toolchain_unpinned": BlockedReason.TOOL_MISSING,
        }
        token, separator, detail = failure.partition(":")
        if separator and token in refusal_reasons:
            return _refuse(
                token, refusal_reasons[token], detail.strip(),
            )
        return _refuse(
            "report_truncated", BlockedReason.ARTIFACT_MALFORMED,
            f"the runner did not finish: {failure}",
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
    fingerprint = document.get("fingerprint")
    if (not isinstance(fingerprint, dict)
            or type(fingerprint.get("constants_from_config")) is not bool
            or type(fingerprint.get("checksum_states")) is not bool
            or not isinstance(fingerprint.get("workers"), int)
            or isinstance(fingerprint.get("workers"), bool)):
        return _refuse(
            "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
            "the report does not record valid fingerprint settings",
        )
    return TlcRun(
        str(document.get("model", "")),
        str(document.get("model_path", "")),
        str(document.get("config", "")),
        str(document.get("tool_version", "")),
        str(document["jar_sha256"]),
        (fingerprint["constants_from_config"], fingerprint["checksum_states"],
         fingerprint["workers"]),
        exit_code,
        str(document.get("stdout", "")),
        str(document.get("stderr", "")),
    )


def _tlc_version(text: str) -> str | None:
    """TLC's checker version, not the release tag used to download its jar."""
    match = re.search(r"\bVersion\s+([0-9]+\.[0-9]+)\b", text, re.IGNORECASE)
    if not match:
        match = re.search(r"\bTLC\s+([0-9]+\.[0-9]+)\b", text, re.IGNORECASE)
    return match.group(1) if match else None


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
