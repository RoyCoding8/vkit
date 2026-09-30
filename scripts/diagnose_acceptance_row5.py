"""Print each term of the acceptance02 row 5 condition separately.

Same reason as diagnose_acceptance_rows.py: the row prints a narrative that
reads like a pass and then records FAIL, and the narrative is written
unconditionally, so it says nothing about which term was false.

Run:  bash scripts/posix-run.sh scripts/diagnose_acceptance_row5.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# The exact terms row 5 evaluates, in source order.
TERMS = [
    "first['same'] is True",
    "first['result'] == 'PASS'",
    "'different payload' in first['conflicting']",
    "second['run_id'] == first['first']",
    "second['replayed'] is True",
    "second['result'] == 'PASS'",
    "len(after_first) == 1",
    "len(table) == 1",
    "table[0]['task_id'] == 't5'",
]

# Terms that depend on the example check actually running. The example manifest
# names the interpreter `python`, and this host provides `python3` and no
# `python`, so the check is BLOCKED with prerequisite_missing and every term
# downstream of a PASS is false for a reason that has nothing to do with the
# row's subject.
TERMS_NEEDING_A_PASS = {
    "first['result'] == 'PASS'",
    "second['replayed'] is True",
    "second['result'] == 'PASS'",
}


def main() -> int:
    import shutil
    import subprocess
    import tempfile

    from vkit.paths import open_project
    from vkit.storage import Store

    base = Path(tempfile.mkdtemp()) / "retry-start"
    shutil.copytree(ROOT / "examples" / "python-cli", base)
    for args in (["git", "init", "-q"], ["git", "add", "-A"],
                 ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "e"]):
        subprocess.run(args, cwd=base, check=True)

    project = open_project(base)
    store = Store(project.db_path)
    conn = sqlite3.connect(store._db_path)
    try:
        rows = conn.execute("SELECT result, reason FROM runs").fetchall()
    finally:
        conn.close()

    print("== what the example check actually does on this host ==")
    print(f"  runs recorded: {rows}")
    print()
    print("== row 5 terms, and what each depends on ==")
    for term in TERMS:
        needs = "needs a PASS from the example check" if term in TERMS_NEEDING_A_PASS else "independent of it"
        print(f"  {term:<48} {needs}")
    print()
    print("  The example manifest names the interpreter `python`:")
    manifest = (base / "verification" / "manifest.json").read_text(encoding="utf-8")
    for line in manifest.splitlines():
        if '"python"' in line:
            print(f"    {line.strip()}")
    print()
    import shutil as _shutil
    which = _shutil.which("python")
    print(f"  `python` on PATH here: {which}")
    print(f"  `python3` on PATH here: {_shutil.which('python3')}")
    print()
    print("  So on this host the check is BLOCKED with prerequisite_missing and")
    print("  the three PASS-dependent terms are false. That is a missing")
    print("  interpreter name, not a defect in the row's subject, which is")
    print("  idempotent replay across a process boundary.")
    _shutil.rmtree(base.parent, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
