"""Runs one Lean module twice, audits axioms, and writes the JSON report `lean_adapter` reads; it
decides nothing. It is launched by file path (`python lean_runner.py`) and puts the source root
on `sys.path` for `vkit.nowindow`.
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
    """One checker invocation, recorded whole. Decoded as UTF-8 with replacement because Lean error
    messages quote source verbatim.
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
    """Every JSON line `lean --json` wrote; an unparseable line is kept with severity `unparsed` so
    a changed output format is visible.
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
    """The axiom audit module, written from the check's theorem list.

    Each `#print axioms` sits in `namespace <module>` so short theorem names resolve there.
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
    # Absolute, `os.pathsep`-separated: a relative entry resolves against Lean's own library dir.
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
    """Write the report atomically, including when the run stopped early."""
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_suffix(target.suffix + ".partial")
    staged.write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    staged.replace(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
