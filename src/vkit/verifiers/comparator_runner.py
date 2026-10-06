"""Run leanprover/comparator on an agent's solution against a challenge whose digest a human froze.

Writes a JSON report; the exit status of this script only says whether the report was written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
TAIL = 4000


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def sandbox() -> str:
    landrun = shutil.which("landrun")
    if landrun is None:
        return "missing"
    try:
        done = subprocess.run([landrun, "--version"], capture_output=True, text=True, timeout=30, **NO_WINDOW)
    except OSError:
        return "missing"
    return "none (landrun passthrough)" if "passthrough" in done.stdout + done.stderr else "landrun"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    for name in ("comparator", "project-dir", "challenge-path", "challenge-sha256", "challenge-module",
                 "solution-module", "theorems", "axioms", "run-dir", "report"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    report_path = Path(args.report)
    project = Path(args.project_dir)
    report: dict = {"version": 1, "sandbox": sandbox()}
    measured = digest(project / args.challenge_path)
    report["challenge_sha256"] = measured
    if measured != args.challenge_sha256:
        report["status"] = "challenge_changed"
    else:
        config = Path(args.run_dir) / "comparator.json"
        config.write_text(json.dumps({
            "challenge_module": args.challenge_module, "solution_module": args.solution_module,
            "theorem_names": [t for t in args.theorems.split(",") if t],
            "permitted_axioms": [a for a in args.axioms.split(",") if a],
        }), encoding="utf-8")
        done = subprocess.run(["lake", "env", args.comparator, str(config)], cwd=project, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", **NO_WINDOW)
        output = (done.stdout + done.stderr).strip()
        report.update({"status": "accepted" if done.returncode == 0 else "rejected",
                       "exit_code": done.returncode, "output": output[-TAIL:]})
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
