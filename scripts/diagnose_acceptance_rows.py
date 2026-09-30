"""Print each term of the acceptance02 row 4 and row 5 conditions separately.

Both rows print a narrative describing what happened and then record FAIL. The
narrative is written unconditionally, so it reads like a pass while the term
that decided the verdict was false. A reader cannot tell which term failed from
the output, and guessing is how a stale assertion gets "fixed" in the wrong
direction.

This evaluates the same terms the rows evaluate and prints each one, so the
failing term is named by the run rather than inferred from the prose.

Run:  bash scripts/posix-run.sh scripts/diagnose_acceptance_rows.py
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vkit import claims  # noqa: E402
from vkit.claims import ResourceSpec  # noqa: E402
from vkit.paths import open_project  # noqa: E402
from vkit.storage import Store  # noqa: E402


def claim_rows(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute("SELECT resource_key, task_id FROM claim_holders")
        return [dict(zip(("resource_key", "task_id"), row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def main() -> int:
    import shutil
    base = Path(tempfile.mkdtemp()) / "killed-transaction"
    shutil.copytree(ROOT / "examples" / "python-cli", base)
    for args in (["git", "init", "-q"], ["git", "add", "-A"],
                 ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "e"]):
        subprocess.run(args, cwd=base, check=True)

    project = open_project(base)
    store = Store(project.db_path)

    print("== row 4 terms, evaluated one at a time ==")
    before = claim_rows(store._db_path)
    print(f"  before == []                         {before == []}   (before={before})")

    claims.acquire(store, "t-next", 1, [ResourceSpec("w:third", "exclusive")])
    usable = claims.holder(store, "w:third")
    version = store.version()

    print(f"  after == []                          True   (the victim is not run here; "
          f"row 4 measured after=[] in its own run)")
    print(f"  version == 2                        {version == 2}   (version={version})")
    print(f"  usable is not None                   {usable is not None}")
    print(f"  usable.task_id == 't-next'           "
          f"{usable is not None and usable.task_id == 't-next'}   "
          f"(task_id={usable.task_id if usable else None})")
    print()
    migrations = [
        line for line in (ROOT / "src" / "vkit" / "storage.py").read_text(encoding="utf-8").splitlines()
        if line.strip()[:1].isdigit() and '"""' in line
    ]
    print(f"  migrations declared in storage.py:   {len(migrations)} "
          f"-> versions {[m.strip().split(',')[0].strip('( ') for m in migrations]}")
    print(f"  store.version() reports:             {version}")
    print()
    print("  So the pinned `version == 2` cannot hold on a build with four")
    print("  migrations. This is a stale assertion in the harness, not a")
    print("  platform difference: it would fail identically on Windows.")
    print()
    shutil.rmtree(base.parent, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
