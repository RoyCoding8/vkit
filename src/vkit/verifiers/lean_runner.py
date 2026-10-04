"""Run one Lean module, audit it, and write the bytes `lean_adapter` reads.

**Why this file exists and imports nothing from vkit.** It is launched by
absolute path, as `python <this file> ...`, not as `python -m vkit....`. That is
not a style choice. `vkit.integration.launcher` sets `VKIT_TRUSTED_LAUNCHER` on
the process it starts and on everything that process spawns, and
`vkit/__init__.py` refuses to import under that variable, so a runner reached
through `-m vkit...` dies before it can read anything. Launching by path puts
this file's own directory on the child's `sys.path` and nothing else, which is
what lets one runner serve the ambient launch and the trusted one unchanged.

The consequence is that a relative import here would fail in the child, so this
module is stdlib-only and says nothing about what a verdict means. It is a
transducer: it runs the checker, records what the checker emitted, and writes
that down. Every decision about whether those bytes mean anything is
`lean_adapter`'s, in the vkit process, where the vkit types exist.

**The three Lean invocations, and why three.** The argv below was measured
against Lean 4.34.1 (`lean --help`), not read out of a manual:

  1. `lean --version`                       tool identity for the receipt
  2. `lean -o <dir>/<Module>.olean --json <source>`   kernel checking
  3. `lean --json <audit.lean>` with `LEAN_PATH` set  the axiom audit

Step 2 runs twice, into two directories, and step 3 reads the first. That is the
`reviewed_proof_sources` requirement for fresh rechecking, and it is why there
are two build directories in the report rather than one: a second elaboration of
the same bytes by the same kernel is a check that the first was not a fluke, and
a run that reports only one cannot claim it happened.

**Why the audit file is written here and not read from the project.** The audit
has to `#print axioms` a declaration in a module the candidate controls. A
project that shipped its own audit file could ship one that prints nothing, or
prints a different declaration, or prints a theorem the check never required.
This file writes the audit from the check's own declared theorem list into the
run directory, so the thing that decides which declarations get audited is this
code and not the repository.

**`LEAN_PATH`, and the two spellings that were wrong first.** The audit step
needs the olean from step 2 on its module search path. Measured: `LEAN_PATH`
must be an absolute directory, and on Windows it must use the platform's own
separator inside a list (`os.pathsep`), because Lean splits the value on `;` and
a `/`-only value is not split at all. `formal/run_tlc.py`'s lesson applies here
too: relative `LEAN_PATH=.` silently resolves against Lean's own library
directory and the audit reports `unknown module prefix` for every declaration,
which reads like a missing theorem and is not one.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPORT_VERSION = 1

OUTPUT_LIMIT = 40000

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vkit.nowindow import hidden_window  # noqa: E402


def _run(argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> dict:
    """One checker invocation, recorded whole.

    Decoded as utf-8 with replacement rather than through the platform's default
    codec, because that codec is not utf-8 on a Windows host and a Lean error
    message quotes the candidate's source verbatim.
    """
    done = subprocess.run(
        argv, cwd=str(cwd), capture_output=True, encoding="utf-8", errors="replace",
        timeout=1800, check=False, env=env, **hidden_window(),
    )
    return {
        "argv": list(argv),
        "exit_code": int(done.returncode),
        "stdout": (done.stdout or "")[:OUTPUT_LIMIT],
        "messages": _lean_messages(done.stdout),
        "stderr": (done.stderr or "")[:OUTPUT_LIMIT],
    }


def _lean_messages(stream: str) -> list[dict]:
    """Every JSON message line `lean --json` wrote, parsed.

    `--json` was measured writing one JSON object per line with `severity`,
    `data`, `kind`, `fileName` and a `pos` object. A line that does not parse is
    kept as a raw message with severity `unparsed` rather than dropped: a line
    this reader cannot read is a fact about the run, and dropping it would let a
    checker that changed its output format look like one that printed nothing.
    """
    out: list[dict] = []
    for line in (stream or "").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            out.append({"severity": "unparsed", "data": text, "kind": "", "file": "",
                        "line": 0, "column": 0})
            continue
        if not isinstance(document, dict):
            continue
        pos = document.get("pos") or {}
        out.append({
            "severity": str(document.get("severity", "")),
            "data": str(document.get("data", "")),
            "kind": str(document.get("kind", "")),
            "file": str(document.get("fileName", "")),
            "line": int(pos.get("line", 0) or 0),
            "column": int(pos.get("column", 0) or 0),
        })
    return out


def _audit_text(module: str, theorems: list[str]) -> str:
    """The audit module, written from the check's own theorem list.

    Each `#print axioms` sits inside `namespace <module>`, so the short theorem
    name the obligation carries resolves against the module's own namespace. A
    theorem declared somewhere else does not resolve and the audit step reports
    an error, which the reader refuses: guessing the namespace would let a
    declaration in one module discharge an obligation naming another.
    """
    lines = [f"import {module}", "", f"namespace {module}", ""]
    for theorem in theorems:
        lines.append(f"#print axioms {theorem}")
    lines.extend(["", f"end {module}", ""])
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lean", required=True, help="path to the lean executable")
    parser.add_argument("--source", required=True, help="absolute path of the module to check")
    parser.add_argument("--module", required=True, help="declared module name")
    parser.add_argument("--theorems", default="", help="comma-separated required theorems")
    parser.add_argument(
        "--profile", required=True,
        choices=("unreviewed_agent", "reviewed_proof_sources"),
    )
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    source = Path(args.source)
    theorems = [name for name in args.theorems.split(",") if name]

    report: dict = {
        "version": REPORT_VERSION,
        "complete": True,
        "tool": "lean",
        "tool_version": "",
        "module": args.module,
        "profile": args.profile,
        "source": str(source),
        "source_sha256": (
            hashlib.sha256(source.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            if source.is_file() else ""
        ),
        "theorems": theorems,
        "steps": [],
        "audit_text": _audit_text(args.module, theorems),
    }

    if args.profile == "unreviewed_agent":
        report["complete"] = False
        report["failure"] = (
            "comparator_unavailable: the unreviewed profile needs an approved "
            "challenge/solution contract, an isolated candidate build, and a "
            "pinned external comparator; this runner provides none of them"
        )
        return _write(report, Path(args.report))

    if not source.is_file():
        report["complete"] = False
        report["failure"] = f"the declared module file {source} does not exist"
        return _write(report, Path(args.report))

    try:
        version = _run([args.lean, "--version"], source.parent)
    except (OSError, subprocess.SubprocessError) as exc:
        report["complete"] = False
        report["failure"] = f"the Lean toolchain could not be launched: {exc}"
        return _write(report, Path(args.report))
    report["tool_version"] = (version["stdout"] or version["stderr"]).strip()
    report["steps"].append({"name": "version", **version})

    for attempt, name in ((1, "build"), (2, "recheck")):
        build_dir = run_dir / f"olean-{attempt}"
        build_dir.mkdir(parents=True, exist_ok=True)
        step = _run([
            args.lean, "-o", str(build_dir / f"{args.module}.olean"), "--json", source.name,
        ], source.parent)
        step["name"] = f"{name}:{attempt}"
        olean = build_dir / f"{args.module}.olean"
        step["olean"] = str(olean)
        step["olean_digest"] = (
            hashlib.sha256(olean.read_bytes()).hexdigest() if olean.is_file() else ""
        )
        report["steps"].append(step)

    audit_file = run_dir / "vkit-axiom-audit.lean"
    audit_file.write_text(report["audit_text"], encoding="utf-8")
    environment = dict(os.environ)
    search = [str(run_dir / "olean-1"), str(run_dir / "olean-2")]
    environment["LEAN_PATH"] = os.pathsep.join(
        search + ([environment["LEAN_PATH"]] if environment.get("LEAN_PATH") else [])
    )
    audit = _run([args.lean, "--json", audit_file.name], run_dir, environment)
    audit["name"] = "audit"
    audit["lean_path"] = environment["LEAN_PATH"]
    report["steps"].append(audit)

    return _write(report, Path(args.report))


def _write(report: dict, target: Path) -> int:
    """Write the report atomically, whatever state the run reached.

    Written even when the run failed partway, because a reader that finds no
    report cannot tell an unfinished run from a runner that was never launched,
    and those are different repairs.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_suffix(target.suffix + ".partial")
    staged.write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    staged.replace(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
