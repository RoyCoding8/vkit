"""Run TLC over the ownership model and record what it actually proved.

Plan 08 is a plan about not overstating what a check established, so the
script's first job is to refuse to print a bare word like "PASS". It writes a
JSON receipt naming the tool, the model, the finite configuration, the number
of states explored, and whether exploration actually completed. A run that was
cut short, hit a resource limit, or died is recorded BLOCKED, because
"incomplete exploration" and "explored everything and found nothing" are
different claims and only one of them is a result.

Run it directly:

    python formal/run_tlc.py

Exit codes: 0 every checked property held, 1 a property failed, 2 BLOCKED
(TLC absent, or the run did not complete), 3 the script itself is wrong.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TLA_DIR = ROOT / "formal" / "tla"
MODULE = "OwnershipAcceptance"
OUT = ROOT / "formal" / "results"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from digest import NORMALIZATION, canonical_sha256  # noqa: E402

# The toolchain is opt-in and lives outside the repository. CONTRACT.md forbids
# downloading a toolchain from a hook or an ordinary MCP call, so nothing here
# fetches anything: it looks for a java and a tla2tools.jar the operator has
# already put in place, and reports BLOCKED when they are absent. An ordinary
# `vkit check run` never reaches this file.
TOOLS = ROOT / "tmp" / "formal-tools"

# The finite configuration is read out of the .cfg rather than restated here,
# so the receipt cannot disagree with the file TLC actually read. The
# expectations below are what the run is allowed to claim if it completes.
EXPECTED_DOMAIN = {
    "owners": 2,
    "resources": 2,
    "checks": 2,
    "revisions": 2,
    "generations": 2,
}


@dataclass(frozen=True)
class Result:
    """One TLC run, with the bounds it is scoped to."""

    status: str
    tool: str
    tool_version: str
    tool_jar_sha256: str
    model: str
    model_sha256: str
    config_sha256: str
    domain: dict[str, int]
    properties: list[str]
    states_generated: int | None
    distinct_states: int | None
    depth: int | None
    elapsed_s: float
    finished_at: str
    reason: str
    counterexample: str | None

    def to_json(self) -> dict:
        return asdict(self)


def _sha256(path: Path) -> str:
    """Digest a text artifact: the model and the config.

    Both are checked out through `text=auto`, so their bytes on this host depend
    on the host's line-ending setting. See formal/digest.py.
    """
    return canonical_sha256(path)


def _jar_sha256(jar: Path) -> str:
    """Digest the jar over its exact bytes.

    Deliberately not the normalizing helper: a jar is a downloaded binary, it
    never passes through git's line-ending filter, and folding a byte pair out
    of a compressed stream would be hashing something other than the file.
    """
    return hashlib.sha256(jar.read_bytes()).hexdigest()


def _tool_version(java: Path, jar: Path) -> str:
    """Ask the jar what it is, rather than recording a version we remember."""
    done = subprocess.run(
        [str(java), "-cp", str(jar), "tlc2.TLC", "-help"],
        capture_output=True, text=True, timeout=300, check=False,
    )
    blob = done.stdout + done.stderr
    match = re.search(r"Version\s+(\S+)\s+of\s+(\S+)", blob)
    if match:
        return f"TLC {match.group(1)} ({match.group(2)})"
    return "TLC version unreported"


def _find_java() -> Path | None:
    found = shutil.which("java")
    if found:
        return Path(found)
    local = sorted(TOOLS.glob("*/bin/java.exe")) + sorted(TOOLS.glob("*/bin/java"))
    return local[0] if local else None


def _find_jar() -> Path | None:
    local = TOOLS / "tla2tools.jar"
    if local.is_file():
        return local
    env = __import__("os").environ.get("VKIT_TLA2TOOLS_JAR")
    return Path(env) if env else None


def _read_domain(cfg_text: str) -> dict[str, int]:
    """Count each constant's cardinality out of the .cfg text.

    Parsed rather than hardcoded so a run receipt that says "2 resources"
    is a fact about the file TLC read. A mismatch against EXPECTED_DOMAIN is
    reported rather than corrected: silently using a different configuration
    than the receipt claims is the exact overstatement this plan exists to
    stop.
    """
    def size(value: str) -> int:
        value = value.strip()
        if ".." in value:
            lo, hi = value.split("..", 1)
            return int(hi) - int(lo) + 1
        inner = value.strip('{}"').rstrip("}").strip('"')
        if not inner:
            return 0
        return len([part for part in inner.split(",") if part.strip()])

    found: dict[str, int] = {}
    for name, key in (
        ("Owners", "owners"), ("Resources", "resources"), ("Checks", "checks"),
        ("Revisions", "revisions"), ("Generations", "generations"),
    ):
        match = re.search(rf"^\s*{name}\s*=\s*(.+)$", cfg_text, re.MULTILINE)
        if match:
            found[key] = size(match.group(1))
    return found


def _properties(cfg_text: str) -> list[str]:
    names: list[str] = []
    for section in ("INVARIANTS", "PROPERTIES"):
        match = re.search(rf"^{section}\n((?:\s+\S+\n)+)", cfg_text, re.MULTILINE)
        if match:
            names.extend(line.strip() for line in match.group(1).splitlines() if line.strip())
    return names


def main() -> int:
    started = datetime.now(timezone.utc)
    module_tla = TLA_DIR / f"{MODULE}.tla"
    module_cfg = TLA_DIR / f"{MODULE}.cfg"
    for required in (module_tla, module_cfg):
        if not required.is_file():
            print(f"missing {required}", file=sys.stderr)
            return 3

    java, jar = _find_java(), _find_jar()
    if java is None or jar is None:
        missing = "java" if java is None else "tla2tools.jar"
        reason = (
            f"BLOCKED: {missing} is not available. TLC is opt-in. Install a JRE and "
            f"tla2tools.jar under {TOOLS}, or set VKIT_TLA2TOOLS_JAR. Ordinary "
            f"verification does not need either."
        )
        print(reason, file=sys.stderr)
        _write(reason, "BLOCKED", None, None, None, started, 0.0)
        return 2

    cfg_text = module_cfg.read_text(encoding="utf-8")
    domain = _read_domain(cfg_text)
    if domain != EXPECTED_DOMAIN:
        reason = (
            f"BLOCKED: the .cfg declares {domain}, not the recorded configuration "
            f"{EXPECTED_DOMAIN}. Fix the receipt or the config deliberately; do not "
            f"let a run be reported at bounds it was not run at."
        )
        print(reason, file=sys.stderr)
        _write(reason, "BLOCKED", java, jar, cfg_text, started, 0.0)
        return 2

    states = ROOT / "tmp" / "tlc-states"
    if states.exists():
        shutil.rmtree(states, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)

    # No -dump, and one worker. TLC splits the -dump argument on whitespace, so
    # a repository path containing a space is read as a filename and the run
    # dies before the model is parsed. With -workers auto each worker prints its
    # own "N states generated" summary, and they interleave, so the first regex
    # match in the output is whichever worker finished first, not the run's
    # total. One worker costs wall-clock time and buys an unambiguous receipt.
    # The counterexample TLC prints is captured verbatim into formal/results/
    # instead, which is the same evidence without the parsing hazard.
    command = [
        str(java), "-XX:+UseParallelGC", "-Xmx2g", "-cp", str(jar), "tlc2.TLC",
        "-workers", "1", "-nowarning", "-cleanup", "-metadir", str(states),
        MODULE,
    ]
    done = subprocess.run(
        command, cwd=TLA_DIR, capture_output=True, text=True, timeout=3600, check=False,
    )
    blob = done.stdout + done.stderr
    (OUT / f"{MODULE}-tlc.log").parent.mkdir(parents=True, exist_ok=True)
    (OUT / f"{MODULE}-tlc.log").write_text(blob, encoding="utf-8")
    print(blob)

    elapsed = round(datetime.now(timezone.utc).timestamp() - started.timestamp(), 1)
    return _classify(blob, done.returncode, java, jar, cfg_text, domain, started, elapsed)


def _classify(blob, returncode, java, jar, cfg_text, domain, started, elapsed) -> int:
    """Turn TLC's output into a status, never into a bare word.

    The distinction that matters: "No error has been found" with a complete
    exploration is a result. A timeout, a killed worker, or an out-of-memory
    is not, and both print similar text. Completion is decided by looking for
    the completion line, not by the absence of the word "error".
    """
    # TLC prints "Model checking completed. No error has been found." for the
    # safety phase, then checks the action properties and prints the same
    # sentence again. A run that violated a property prints that sentence once,
    # after the violation, so the sentence alone is not a pass signal. The pass
    # condition is the safety sentence AND the final summary lines AND no
    # violation marker anywhere in the output.
    complete = (
        "Model checking completed. No error has been found." in blob
        and "states left on queue" in blob
    )
    # "0 states left on queue" is the completion signal. A run cut short leaves
    # work on the queue, and its counts are not a result even if the sentence
    # above is present.
    generated, distinct, left = _final_totals(blob)
    if left not in (0, None):
        complete = False
    violation = (
        "Invariant is violated" in blob
        or "Temporal properties were violated" in blob
        or ("Action property" in blob and "is violated" in blob)
        or "Error: Invariant" in blob
    )
    exhausted = any(
        marker in blob for marker in ("OutOfMemory", "OutOfMemoryError", "Java heap space",
                                      "workers have been aborted", "TLC has encountered a non-recoverable error")
    )
    counterexample = None
    if violation or exhausted:
        counterexample = _trace(blob)

    if complete and not violation:
        status, reason, code = "PASS", (
            "TLC explored every reachable state in the recorded configuration and the "
            "checked properties held in all of them."
        ), 0
    elif violation:
        status, reason, code = "FAIL", (
            "TLC found a reachable state or execution violating a checked property."
        ), 1
    elif exhausted:
        status, reason, code = "BLOCKED", (
            "TLC exhausted its resources before finishing the exploration. An "
            "exhausted resource is BLOCKED, not a pass."
        ), 2
    else:
        status, reason, code = "BLOCKED", (
            f"TLC did not complete the exploration (exit {returncode}). Incomplete "
            f"exploration is BLOCKED, not a pass."
        ), 2

    # "0 states left on queue" is the completion signal. A run that was cut
    # short leaves work on the queue, and its counts are not a result.
    receipt = Result(
        status=status,
        tool="TLC",
        tool_version=_tool_version(java, jar),
        tool_jar_sha256=_jar_sha256(jar),
        model=f"{MODULE}.tla",
        model_sha256=_sha256(TLA_DIR / f"{MODULE}.tla"),
        config_sha256=_sha256(TLA_DIR / f"{MODULE}.cfg"),
        domain=domain,
        properties=_properties(cfg_text),
        states_generated=generated,
        distinct_states=distinct,
        depth=_int(blob, r"depth of the complete state graph search is (\d+)"),
        elapsed_s=elapsed,
        finished_at=datetime.now(timezone.utc).isoformat(),
        reason=reason,
        counterexample=counterexample,
    )
    _emit(receipt, java, jar, cfg_text, started, elapsed, status)
    return code


def _int(blob: str, pattern: str) -> int | None:
    match = re.search(pattern, blob)
    return int(match.group(1)) if match else None


def _final_totals(blob: str) -> tuple[int | None, int | None, int | None]:
    """The run's totals, taken from TLC's LAST summary, not its progress lines.

    TLC prints a `Progress(n)` line every minute with the counts so far, and
    those lines are interleaved with the final summary. Taking the first regex
    match recorded a progress snapshot as if it were the result, which is
    exactly the sort of small lie this plan exists to prevent. The final line
    is the one that ends with "states left on queue", and on a completed run it
    reads zero.
    """
    finals = re.findall(
        r"([\d,]+) states generated, ([\d,]+) distinct states found,\s*"
        r"([\d,]+) states left on queue",
        blob,
    )
    if not finals:
        return None, None, None
    generated, distinct, left = finals[-1]
    return (
        int(generated.replace(",", "")),
        int(distinct.replace(",", "")),
        int(left.replace(",", "")),
    )


def _trace(blob: str) -> str:
    """Keep the state sequence TLC printed, which is the counterexample."""
    lines = [line for line in blob.splitlines() if line.strip()]
    start = next((i for i, line in enumerate(lines) if "State " in line), None)
    if start is None:
        return "\n".join(lines[-40:])
    return "\n".join(lines[start:start + 60])


def _emit(receipt: Result, java, jar, cfg_text, started, elapsed, status) -> None:
    payload = receipt.to_json()
    payload["scope"] = (
        "A PASS establishes these properties for this model at this finite "
        "configuration only. It does not establish them for other worker counts, "
        "other revision counts, or any Python implementation. Safety properties "
        "only; no fairness or termination assumption was modelled, so no liveness "
        "claim is made."
    )
    payload["digest_normalization"] = NORMALIZATION
    _write_receipt(payload)
    print()
    print(f"status: {status}")
    print(f"tool: {receipt.tool_version}")
    print(f"states generated: {receipt.states_generated}  distinct: {receipt.distinct_states}"
          f"  depth: {receipt.depth}  elapsed: {receipt.elapsed_s}s")
    print(f"domain: {receipt.domain}")
    print("scope: " + payload["scope"])


def _write_receipt(payload: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{MODULE}-receipt.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def _write(reason, status, java, jar, cfg_text, started, elapsed) -> None:
    """A BLOCKED receipt, so an unavailable toolchain is a recorded fact."""
    receipt = Result(
        status=status, tool="TLC", tool_version="unavailable", tool_jar_sha256="",
        model=f"{MODULE}.tla",
        model_sha256=_sha256(TLA_DIR / f"{MODULE}.tla") if (TLA_DIR / f"{MODULE}.tla").is_file() else "",
        config_sha256=_sha256(TLA_DIR / f"{MODULE}.cfg") if (TLA_DIR / f"{MODULE}.cfg").is_file() else "",
        domain=_read_domain(cfg_text) if cfg_text else {}, properties=[],
        states_generated=None, distinct_states=None, depth=None, elapsed_s=elapsed,
        finished_at=datetime.now(timezone.utc).isoformat(), reason=reason, counterexample=None,
    )
    payload = receipt.to_json()
    payload["scope"] = "No run happened, so no property was established."
    payload["digest_normalization"] = NORMALIZATION
    _write_receipt(payload)


if __name__ == "__main__":
    raise SystemExit(main())
