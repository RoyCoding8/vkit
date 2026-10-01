import sqlite3
import tempfile
from pathlib import Path

"""Does the runs rebuild need `PRAGMA foreign_keys` off?

Re-checked against the schema vkit ACTUALLY has, because an earlier probe of
mine tested against the shape the R2 design doc described instead. The design
named `claim_holders` / `claim_members` as tables referencing `runs`. The real
storage.py has no REFERENCES clause anywhere, so that premise was never
verified, and the conclusion drawn from it ("_migrate needs a foreign-keys-off
flag") was an artifact of the probe rather than a property of the code.

This prints what the real DDL declares, then runs the rebuild exactly the way
`_migrate` drives it: one bare executescript, version INSERT appended, FK
pragma left untouched.
"""

REAL_RUNS = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    check_id    TEXT NOT NULL,
    task_id     TEXT,
    attempt     INTEGER,
    lifecycle   TEXT NOT NULL CHECK (lifecycle IN ('preparing','running','terminal')),
    result      TEXT,
    reason      TEXT,
    registered_at TEXT NOT NULL,
    ended_at    TEXT,
    source_json TEXT,
    configuration_digest TEXT,
    fixture_digest TEXT,
    process_json TEXT
);
CREATE INDEX IF NOT EXISTS runs_by_task ON runs(task_id);
INSERT INTO runs VALUES ('r1','c1','t1',1,'preparing',NULL,NULL,'now',NULL,'{}','d1',NULL,NULL);
INSERT INTO runs VALUES ('r2','c1','t1',1,'terminal','PASS',NULL,'now','now','{}','d1',NULL,'{}');
INSERT INTO schema_version (version) VALUES (4);
"""

REBUILD = """
CREATE TABLE IF NOT EXISTS runs_new (
    run_id      TEXT PRIMARY KEY,
    check_id    TEXT NOT NULL,
    task_id     TEXT,
    attempt     INTEGER,
    lifecycle   TEXT NOT NULL CHECK (lifecycle IN
                   ('preparing','launching','running','cancelling','terminal')),
    result      TEXT,
    reason      TEXT,
    registered_at TEXT NOT NULL,
    ended_at    TEXT,
    source_json TEXT,
    configuration_digest TEXT,
    fixture_digest TEXT,
    process_json TEXT
);
INSERT OR REPLACE INTO runs_new
  SELECT run_id, check_id, task_id, attempt, lifecycle, result, reason,
         registered_at, ended_at, source_json, configuration_digest,
         fixture_digest, process_json
  FROM runs;
DROP TABLE runs;
ALTER TABLE runs_new RENAME TO runs;
"""

# 13 columns, in the order the CREATE TABLE declares them.
def row(lifecycle):
    return ("c", "t", 1, lifecycle, None, None, "now", None, "{}", "d", None, None)


INSERT = "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"


def migrate(conn, version, sql):
    """`_migrate` verbatim: version check, then one bare executescript."""
    seen = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    if seen and seen[0] is not None and version <= seen[0]:
        return
    conn.executescript(
        sql.rstrip().rstrip(";") + f";\nINSERT INTO schema_version (version) VALUES ({int(version)});"
    )


def indexes(conn):
    return [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")
    ]


db = Path(tempfile.mkdtemp()) / "t.db"
conn = sqlite3.connect(db, isolation_level=None)
conn.executescript("CREATE TABLE schema_version (version INTEGER NOT NULL);" + REAL_RUNS)
conn.execute("PRAGMA foreign_keys=ON")

print("=== what the real schema actually declares ===")
tables = [
    r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
]
print(f"  tables : {tables}")
print(f"  indexes: {indexes(conn)}")
print(f"  foreign_key_list(runs): {conn.execute('PRAGMA foreign_key_list(runs)').fetchall()}")
print("  -> runs_by_task is an INDEX; no table references runs")

print()
print("=== rebuild, foreign_keys=ON, pragma untouched by the script ===")
try:
    migrate(conn, 5, REBUILD)
    print("  DROP TABLE runs + rename: SUCCEEDED")
    print(f"  schema_version: {conn.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]}")
    print(f"  rows carried over: {conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]}")
    for lifecycle in ("launching", "cancelling"):
        conn.execute(INSERT, ("x-" + lifecycle,) + row(lifecycle)[3:])
        print(f"  {lifecycle} -> ACCEPTED")
    try:
        conn.execute(INSERT, ("x-typo",) + row("prepring")[3:])
        print("  typo 'prepring' -> ACCEPTED  (the CHECK would be gone)")
    except sqlite3.IntegrityError as exc:
        print(f"  typo 'prepring' -> REJECTED: {exc}")
except Exception as exc:
    print(f"  FAILED: {type(exc).__name__}: {exc}")

print()
print("=== what the rebuild silently lost ===")
print(f"  indexes now: {indexes(conn)}")
print("  -> runs_by_task is gone. It was not in the rebuild script.")
print("     _live_holder reads runs by task_id on every claim release.")
plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM runs WHERE task_id = 't1'").fetchall()
print(f"  query plan for the task_id lookup: {plan}")
print("  -> 'USING INDEX' means indexed; a bare SCAN means the index was lost.")

print()
print("=== re-issuing the index after the rename ===")
conn.executescript("CREATE INDEX IF NOT EXISTS runs_by_task ON runs(task_id);")
print(f"  indexes now: {indexes(conn)}")
plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM runs WHERE task_id = 't1'").fetchall()
print(f"  query plan now: {plan}")
print("  -> migration 5 must re-issue this index, and a test should assert it.")