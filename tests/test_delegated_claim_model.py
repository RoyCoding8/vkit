from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import NO_WINDOW

ROOT = Path(__file__).resolve().parents[1]


def test_delegated_claim_model_and_omission_mutants(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_delegated_claim_authority.py"),
         "--output", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
        **NO_WINDOW,
    )
    unavailable = (
        "Java is unavailable; set JAVA_HOME or pass --java",
        "TLC jar is unavailable; set VKIT_TLA2TOOLS_JAR or pass --jar",
    )
    if done.returncode != 0 and any(message in done.stderr for message in unavailable):
        pytest.skip("the Java or pinned TLC jar is not configured")
    assert done.returncode == 0, done.stdout + done.stderr
    report = json.loads(done.stdout)
    cases = {item["case"]: item for item in report["cases"]}
    assert cases["good"]["result"] == "invariants_hold"
    assert len(cases) == 12
    assert all(
        cases[name]["expected_counterexample"] in cases[name]["observed_counterexamples"]
        for name in cases if name != "good"
    )
    expected_hash = (ROOT / "examples" / "formal-ci" / "tla2tools.sha256").read_text(
        encoding="ascii"
    ).strip()
    assert report["jar_sha256"] == expected_hash
    assert "not a Python refinement proof" in report["limit"]
