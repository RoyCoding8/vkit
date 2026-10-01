"""Can `_migrate` apply a migration that drops and renames a table?

R2 must widen the `runs.lifecycle` CHECK, and SQLite cannot widen a CHECK
in place, so migration 5 has to do the documented 12-step rebuild. Two
things have to hold for that to be safe:

  1. The version INSERT still lands inside the same transaction as the
     rebuild, so a crash cannot leave the schema advanced without the
     rebuild, or the rebuild done without the version recorded.
  2. Foreign keys that point at `runs` survive the drop and rename.

This builds the real migration-runner loop from `storage._migrate` and runs
a rebuild migration through it, then checks both.
"""
import sqlite3
import tempfile
from pathlib import Path

REBUILD = """
CREATE TABLE IF NOT EXISTS runs_new (
  run_id TEXT PRIMARY KEY,
  task_id TEXT,
  lifecycle TEXT NOT NULL CHECK (lifecycle IN
    ('preparing','launching','running','cancelling','terminal')),
  payload TEXT
);
INSERT OR REPLACE INTO runs_new (run_id, task_id, lifecycle, payload)
  SELECT run_id, task_id, lifecycle, payload FROM runs;
DROP TABLE runs;
ALTER TABLE runs_new RENAME TO runs;
CREATE INDEX IF NOT EXISTS runs_by_task ON runs(task_id);
"""


def migrate(conn, migrations):
    """`_migrate` as written today: executescript is the transaction."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    for version, sql in migrations:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        if row and row[0] is not None and version <= row[0]:
            continue
        conn.executescript(
            sql.rstrip().rstrip(";") + f";\nINSERT INTO schema_version (version) VALUES ({int(version)});"
        )


def build(with_referencing_fk: bool):
    db = Path(tempfile.mkdtemp()) / "t.db"
    conn = sqlite3.connect(db, isolation_level=None)
    base = """
    CREATE TABLE schema_version (version INTEGER NOT NULL);
    CREATE TABLE runs (
      run_id TEXT PRIMARY KEY,
      task_id TEXT,
      lifecycle TEXT NOT NULL CHECK (lifecycle IN ('preparing','running','terminal')),
      payload TEXT
    );
    CREATE INDEX IF NOT EXISTS runs_by_task ON runs(task_id);
    INSERT INTO runs VALUES ('r1','t1','preparing','{}');
    INSERT INTO runs VALUES ('r2','t1','terminal','{}');
    INSERT INTO schema_version (version) VALUES (1);
    """
    if with_referencing_fk:
        base += """
        CREATE TABLE claim_members (
          run_id TEXT REFERENCES runs(run_id),
          note TEXT
        );
        INSERT INTO claim_members VALUES ('r1','held');
        """
    conn.executescript(base)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def report(label, conn):
    print(f"--- {label} ---")
    print(f"  schema_version = {conn.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]}")
    print(f"  runs rows      = {conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]}")
    try:
        conn.execute("INSERT INTO runs VALUES ('r9','t1','launching','{}')")
        print("  'launching'    = ACCEPTED")
    except sqlite3.IntegrityError as exc:
        print(f"  'launching'    = REJECTED ({exc})")


print("=== 1. rebuild with foreign_keys ON, a table referencing runs ===")
conn = build(with_referencing_fk=True)
try:
    migrate(conn, [(2, REBUILD)])
    report("fk ON, referencing table present", conn)
    print(f"  claim_members rows after rebuild = "
          f"{conn.execute('SELECT COUNT(*) FROM claim_members').fetchone()[0]}")
    print(f"  claim_members still valid         = "
          f"{conn.execute('PRAGMA foreign_key_check').fetchall() or 'no violations'}")
except Exception as exc:
    print(f"  FAILED: {type(exc).__name__}: {exc}")

print()
print("=== 2. same rebuild, but foreign_keys OFF for the migration ===")
conn = build(with_referencing_fk=True)
conn.execute("PRAGMA foreign_keys=OFF")
try:
    migrate(conn, [(2, REBUILD)])
    report("fk OFF", conn)
    conn.execute("PRAGMA foreign_keys=ON")
    print(f"  claim_members still valid         = "
          f"{conn.execute('PRAGMA foreign_key_check').fetchall() or 'no violations'}")
except Exception as exc:
    print(f"  FAILED: {type(exc).__name__}: {exc}")

print()
print("=== 3. can the version INSERT be made to survive a crash mid-rebuild? ===")
print("  The runner concatenates the version INSERT onto the script, so both")
print("  the DROP and the INSERT commit together — but only if executescript")
print("  did not COMMIT before the script began. Probing that directly:")
conn = build(with_referencing_fk=False)
print(f"  isolation_level default = {sqlite3.connect(':memory:').isolation_level!r}")
conn2 = sqlite3.connect(":memory:")
conn2.execute("CREATE TABLE t(x)")
conn2.execute("BEGIN")
try:
    conn2.execute("PRAGMA foreign_keys=OFF")
    print("  PRAGMA foreign_keys inside a transaction: accepted (silently a no-op)")
except sqlite3.Error as exc:
    print(f"  PRAGMA foreign_keys inside a transaction: {exc}")