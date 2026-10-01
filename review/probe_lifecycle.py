import sqlite3, tempfile, pathlib

db = pathlib.Path(tempfile.mkdtemp()) / "t.db"
con = sqlite3.connect(db)
con.executescript("""
CREATE TABLE runs (run_id TEXT PRIMARY KEY,
  lifecycle TEXT NOT NULL CHECK (lifecycle IN ('preparing','running','terminal')));
INSERT INTO runs VALUES ('r0','preparing');
""")

print("baseline accepted:")
for v in ('preparing', 'running', 'terminal'):
    con.execute("INSERT INTO runs VALUES (?,?)", (v, v))
print("  preparing / running / terminal -> ok")

print()
print("the two new lifecycle values R2 needs:")
for v in ('launching', 'cancelling'):
    try:
        con.execute("INSERT INTO runs VALUES (?,?)", (v, v))
        print(f"  {v} -> ACCEPTED")
    except sqlite3.IntegrityError as e:
        print(f"  {v} -> REJECTED: {e}")

print()
print("does ADD COLUMN rescue it? (it cannot alter an existing CHECK)")
try:
    con.execute("ALTER TABLE runs ADD COLUMN launch_state TEXT")
    print("  ADD COLUMN launch_state -> ok")
    con.execute("INSERT INTO runs VALUES ('r5','launching',NULL)")
    print("  insert 'launching' -> ACCEPTED")
except sqlite3.IntegrityError as e:
    print(f"  still rejected after ADD COLUMN: {e}")

print()
print("the documented 12-step table rebuild:")
try:
    con.executescript("""
    BEGIN;
    CREATE TABLE runs_new (
      run_id TEXT PRIMARY KEY,
      lifecycle TEXT NOT NULL CHECK (lifecycle IN ('preparing','launching','running','cancelling','terminal')),
      launch_state TEXT);
    INSERT INTO runs_new (run_id, lifecycle) SELECT run_id, lifecycle FROM runs;
    DROP TABLE runs;
    ALTER TABLE runs_new RENAME TO runs;
    COMMIT;
    """)
    con.execute("INSERT INTO runs VALUES ('r6','launching',NULL)")
    print("  after rebuild, 'launching' -> ACCEPTED")
except Exception as e:
    print(f"  rebuild failed: {type(e).__name__}: {e}")