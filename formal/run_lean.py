"""Check the optional Lean example, and check the checks that check it.

Plan 08's Deliverable B is optional and the plan says missing Lean is BLOCKED
only when this check is selected as required, and must not make ordinary users
install Lean. So this script looks for Lean, and reports BLOCKED without it
rather than failing. That is the whole opt-in contract, and it is why nothing
here downloads anything.

When Lean IS present, the script does four things and treats any of them
failing as a failure of this script:

  1. Compiles the file. A warning is a warning; an error is not.
  2. Scans the source for `sorry`, `sorryAx`, `native_decide`, a bare `axiom`
     declaration, and `opaque`. Each of those can produce something that looks
     like a proof and is not one.
  3. Reads the `#print axioms` output and rejects any axiom outside Lean's
     three standard ones. `propext`, `Classical.choice` and `Quot.sound` are
     what a sound Lean development uses; anything else is an unapproved
     assumption someone added to make a goal close.
  4. Checks the `#eval` cases. The plan requires a false acceptance to be
     REJECTED, so the expected values are recorded here and a case that flips
     is a failure rather than a curiosity.

Run directly:

    python formal/run_lean.py

Exit 0 when every check passed, 1 when one failed, 2 when Lean is absent
(BLOCKED, not a pass, and not a failure either).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEAN_DIR = ROOT / "formal" / "lean"
MODULE = "Acceptance.lean"
OUT = ROOT / "formal" / "results"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from digest import NORMALIZATION, canonical_sha256  # noqa: E402

_WINDOW_OPTIONS = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

ALLOWED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}

EXPECTED_EVALS = (
    ("both required checks pass under the current identity", "true"),
    ("one required check has no record at all", "false"),
    ("one required check passed at a stale generation", "false"),
    ("one required check passed under a superseded revision", "false"),
    ("the required set is empty", "false"),
    ("a required check has a pass and a fail under one identity", "true"),
    ("the same evidence record is listed twice", "true"),
    ("a required check is named twice in the required set", "true"),
    ("a required check has a recorded fail and no pass", "false"),
)

FORBIDDEN = (
    ("sorry", "an unproved goal"),
    ("sorryAx", "the sorry axiom"),
    ("native_decide", "evaluation by the compiler rather than by proof"),
    ("axiom ", "an unapproved custom axiom"),
    ("opaque ", "a definition hidden from reduction"),
)


def _find_lean() -> str | None:
    found = shutil.which("lean")
    if found:
        return found
    elan = Path.home() / ".elan" / "bin" / "lean.exe"
    if elan.is_file():
        return str(elan)
    toolchains = Path.home() / ".elan" / "toolchains"
    if toolchains.is_dir():
        for candidate in sorted(toolchains.glob("*/bin/lean.exe"), reverse=True):
            return str(candidate)
    return None


def _strip_comments(text: str) -> str:
    """Remove Lean line and block comments, so prose cannot trip the scan."""
    text = re.sub(r"/-.*?-/", " ", text, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", text)


def main() -> int:
    lean = _find_lean()
    module = LEAN_DIR / MODULE
    if not module.is_file():
        print(f"missing {module}", file=sys.stderr)
        return 2
    if lean is None:
        reason = (
            "BLOCKED: Lean is not installed. Deliverable B is OPTIONAL and this "
            "blocks nothing: no ordinary verification path needs Lean, and "
            "nothing here downloads it. Run `elan toolchain install leanprover/"
            "lean4` if this check is ever made required."
        )
        print(reason, file=sys.stderr)
        _receipt("BLOCKED", reason, lean=None, output="", scanned=[])
        return 2

    done = subprocess.run(
        [lean, MODULE], cwd=LEAN_DIR, capture_output=True, text=True,
        timeout=1800, check=False,
        **_WINDOW_OPTIONS,
    )
    output = done.stdout + done.stderr
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "Acceptance-lean.log").write_text(output, encoding="utf-8")
    print(output)

    failures: list[str] = []

    if done.returncode != 0:
        failures.append(f"the module did not compile (exit {done.returncode})")

    body = _strip_comments(module.read_text(encoding="utf-8"))
    scanned = [name for name, _ in FORBIDDEN if name in body]
    for name, why in FORBIDDEN:
        if name in body:
            failures.append(f"the module contains {name!r}: {why}")

    axioms = set()
    for match in re.finditer(r"depends on axioms: \[([^\]]*)\]", output):
        axioms.update(part.strip() for part in match.group(1).split(",") if part.strip())
    unapproved = axioms - ALLOWED_AXIOMS
    if unapproved:
        failures.append(f"unapproved axioms: {sorted(unapproved)}")
    if not axioms and not any("does not depend on any axioms" in line for line in output.splitlines()):
        failures.append("no axiom audit appeared in the output; the #print axioms commands did not run")

    evals = re.findall(r"^(true|false)$", output, flags=re.MULTILINE)
    if len(evals) != len(EXPECTED_EVALS):
        failures.append(
            f"expected {len(EXPECTED_EVALS)} #eval results, found {len(evals)}: {evals}"
        )
    else:
        for (label, expected), actual in zip(EXPECTED_EVALS, evals):
            if actual != expected:
                failures.append(
                    f"#eval for {label!r} returned {actual}, expected {expected}"
                )

    if failures:
        print()
        for line in failures:
            print(f"FAILED: {line}")
        _receipt("FAIL", "; ".join(failures), lean, output, scanned)
        return 1

    print()
    print(f"status: PASS ({len(EXPECTED_EVALS)} #eval cases, axioms {sorted(axioms) or 'none'})")
    print("scope: the theorems hold for the Lean model in this file. They are not a "
          "statement about the Python core, and the shared #eval cases are a "
          "countercheck of a few decisions rather than a proof of equivalence.")
    _receipt("PASS", "compiled clean, no forbidden constructs, axioms within the "
                     "allowed set, every #eval matched", lean, output, scanned)
    return 0


def _receipt(status: str, reason: str, lean, output: str, scanned: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    version = "unavailable"
    if lean:
        try:
            done = subprocess.run([lean, "--version"], capture_output=True, text=True,
                                  timeout=120, check=False, **_WINDOW_OPTIONS)
            version = done.stdout.strip() or done.stderr.strip()
        except OSError:
            version = "unreported"
    (OUT / "Acceptance-lean-receipt.json").write_text(json.dumps({
        "status": status,
        "tool": "Lean",
        "tool_version": version,
        "optional": True,
        "module": MODULE,
        "module_sha256": canonical_sha256(LEAN_DIR / MODULE),
        "allowed_axioms": sorted(ALLOWED_AXIOMS),
        "forbidden_constructs_found": scanned,
        "reason": reason,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "scope": "The theorems hold for the Lean model in this file. They say "
                 "nothing about the Python core.",
        "digest_normalization": NORMALIZATION,
    }, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
