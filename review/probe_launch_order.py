import sqlite3
import tempfile
from pathlib import Path

"""Does `launches` have to be inserted after the `runs` row it references?

R2-DESIGN §2 declares `launches.run_id TEXT PRIMARY KEY REFERENCES
runs(run_id)`, and §3 step 2 inserts `launches` *before* step 3 calls
register_run. If foreign key enforcement is on, step 2 raises on the
first run of the new code.

This uses the same connection setup `Store._connect` uses
(storage.py:296: `PRAGMA foreign_keys = ON`) so the answer reflects what
the application actually does, and prints the constraint list first so
the premise is visible rather than assumed.
"""

db = Path(tempfile.mkdtemp()) / "t.db"
conn = sqlite3.connect(db, isolation_level=None, timeout=5.0)
conn.execute("PRAGMA foreign_keys = ON")

conn.executescript("""
CREATE TABLE schema_version (version INTEGER NOT NULL);

CREATE TABLE runs (
    run_id    TEXT PRIMARY KEY,
    check_id  TEXT NOT NULL,
    task_id   TEXT,
    attempt   INTEGER,
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('preparing','running','terminal'))
);

CREATE TABLE launches (
    run_id      TEXT PRIMARY KEY REFERENCES runs(run_id),
    check_id    TEXT NOT NULL,
    supervisor_pid INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('detached','inline'))
);
""")

print("=== the premise, printed rather than assumed ===")
print(f"  foreign_keys pragma : {conn.execute('PRAGMA foreign_keys').fetchone()[0]}")
print(f"  foreign_key_list(launches) = "
      f"{conn.execute('PRAGMA foreign_key_list(launches)').fetchall()}")

LAUNCH = "INSERT INTO launches (run_id, check_id, supervisor_pid, kind) VALUES (?,?,?,?)"

print()
print("=== A. the design's order: launches (step 2) BEFORE register_run (step 3) ===")
try:
    conn.execute(LAUNCH, ("r1", "totals-behavior", 4242, "detached"))
    print("  launches inserted -> SUCCEEDED")
except sqlite3.IntegrityError as exc:
    print(f"  launches inserted -> REJECTED: {exc}")
    print("  -> step 2 as written raises on the FIRST run of the new code.")

print()
print("=== B. swapped: register_run (step 3) BEFORE launches (step 2) ===")
try:
    conn.execute(
        "INSERT INTO runs (run_id, check_id, lifecycle) VALUES (?,?,?)",
        ("r2", "totals-behavior", "preparing"),
    )
    print("  runs row inserted -> ok")
    conn.execute(LAUNCH, ("r2", "totals-behavior", 4242, "detached"))
    print("  launches inserted -> SUCCEEDED")
    print("  -> the swapped order is executable.")
except sqlite3.IntegrityError as exc:
    print(f"  REJECTED: {exc}")

print()
print("=== C. is the failure immediate or deferred? ===")
try:
    conn.execute(LAUNCH, ("r3", "totals-behavior", 4242, "detached"))
    print("  an orphan launches row was ACCEPTED — the FK is not enforced")
except sqlite3.IntegrityError as exc:
    print(f"  orphan launches row -> REJECTED immediately: {exc}")
    print("  -> not deferred: there is no window in which to insert the parent later.")