from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@vkit", "-c", "user.name=t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True, **NO_WINDOW)


def vkit_argv(project: Path, *args: str) -> list[str]:
    return [sys.executable, "-m", "vkit.cli", *args, "--project", str(project), "--json"]


def vkit(project: Path, *args: str, timeout: float = 300) -> tuple[int, Any]:
    done = subprocess.run(vkit_argv(project, *args), capture_output=True, text=True, timeout=timeout, **NO_WINDOW)
    try:
        return done.returncode, json.loads(done.stdout)
    except json.JSONDecodeError:
        raise AssertionError(f"exit {done.returncode}, not JSON:\n{done.stdout}\n{done.stderr}") from None


def scenario_check(check_id: str, script: str, *, scenarios: list[str], inputs: list[str],
                   timeout: float = 60) -> dict[str, Any]:
    return {
        "id": check_id, "kind": "scenario", "timeout_seconds": timeout, "artifact": "result.json",
        "command": ["{{python}}", script, "{{run_dir}}/result.json"], "required_scenarios": scenarios,
        "inputs": inputs, "subject": {"paths": inputs, "digest": None}, "claim_id": check_id,
    }


def pytest_kind_check(check_id: str, tests: list[str], inputs: list[str]) -> dict[str, Any]:
    return {
        "id": check_id, "kind": "pytest", "timeout_seconds": 120, "artifact": "pytest-report.json",
        "required_tests": tests, "runner": {"executable": "{{python}}", "base_argv": ["-m", "pytest", "-q", "-p", "no:cacheprovider"]},
        "report_format": "pytest_json_report", "expect_report_version": 1,
        "inputs": inputs, "subject": {"paths": inputs, "digest": None}, "claim_id": check_id,
    }


DRIVER = textwrap.dedent('''
    import json, sys
    def report(results):
        with open(sys.argv[1], "w", encoding="utf-8") as handle:
            json.dump({"schema_version": 1, "scenarios": [
                {"id": i, "result": "PASS" if ok else "FAIL", "observation": o} for i, ok, o in results]}, handle)
''')
