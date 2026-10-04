"""A verification driver: it runs the real CLI and records what actually happened.

The driver has no way to be told the answer. It contains literal expected totals
and compares them against the real process output. A defect in totals.py changes
what the process prints, and the comparison notices. Nothing here accepts a
requested pass or fail flag, so the driver cannot manufacture agreement.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

CASES: list[tuple[str, list[str], int]] = [
    ("empty-cart", [], 0),
    ("single-positive", ["42"], 42),
    ("several-positives", ["10", "20", "12"], 42),
    ("mixed-sign", ["50", "-20", "12"], 42),
    ("negatives-only", ["-5", "-7"], -12),
    ("cancels-to-zero", ["9", "-9"], 0),
]


def run_case(python: str, app: Path, case: tuple[str, list[str], int]) -> dict:
    scenario_id, tail, expected = case
    options = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    done = subprocess.run(
        [python, str(app), *tail],
        capture_output=True, text=True, timeout=60, check=False,
        **options,
    )
    printed = done.stdout.strip()
    passed = done.returncode == 0 and printed == str(expected)
    if done.returncode != 0:
        observation = f"exited {done.returncode}: {done.stderr.strip()[:200]}"
    elif passed:
        observation = f"printed {printed}"
    else:
        observation = f"expected {expected}, printed {printed or '<nothing>'}"
    return {"id": scenario_id, "result": "PASS" if passed else "FAIL", "observation": observation}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the totals CLI by running it.")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    scenarios = [run_case(args.python, args.app, case) for case in CASES]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temp = args.out.with_suffix(args.out.suffix + ".tmp")
    temp.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "description": "totals CLI behavior observed by running the real command",
                "scenarios": scenarios,
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    temp.replace(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
