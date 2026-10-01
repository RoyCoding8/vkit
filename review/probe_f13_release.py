"""F13, end to end: does `recover --apply release_claim` free a checkout
that a live process is still writing?

The crash-window analysis claims a CRITICAL consequence for
`recover.py:801-802`: a run sitting in `preparing` with no pid is skipped by
`_live_holder`, so the release guard finds no blocker, deletes the claim, and a
second task is admitted to the same checkout while the first is still running.

This reproduces the whole sequence with a REAL process, not a mocked one:

  1. task A admitted at generation 1 holding exclusive `scope:checkout`
  2. a run registered `preparing` with NO pid, exactly as a supervisor crash
     between `register_run` (execution.py:277) and `mark_running` leaves it,
     while its child is genuinely alive and writing
  3. A superseded to generation 2 (claims deliberately kept, tasks.py:267-270)
  4. `recover.inspect` -> CLAIM_STALE_GENERATION with action RELEASE_CLAIM
  5. `recover.apply_action(RELEASE_CLAIM, ...)`
  6. task B admitted for the same checkout
  7. is A's process still writing?

If step 5 refuses, F13's release half is already closed and the finding is
about the record only. If it succeeds while the child lives, it is critical.
"""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "src")

from vkit import claims, recover, storage, tasks  # noqa: E402
from vkit.identity import compute_source_identity  # noqa: E402
from vkit.paths import open_project  # noqa: E402

ROOT = Path(tempfile.mkdtemp())
PROJECT = ROOT / "repo"
PROJECT.mkdir()
# A real git repository, because open_project requires one.
subprocess.run(["git", "init", "-q", str(PROJECT)], check=True)
subprocess.run(["git", "-C", str(PROJECT), "config", "user.email", "a@b.c"], check=True)
subprocess.run(["git", "-C", str(PROJECT), "config", "user.name", "t"], check=True)
(PROJECT / "app.py").write_text("x = 1\n", encoding="utf-8")
subprocess.run(["git", "-C", str(PROJECT), "add", "-A"], check=True)
subprocess.run(["git", "-C", str(PROJECT), "commit", "-qm", "init"], check=True)

# Admission refuses a context with no policy, which is R1 working as intended,
# so the probe has to write one. One check, never actually executed here.
VDIR = PROJECT / "verification"
VDIR.mkdir(exist_ok=True)
(VDIR / "manifest.json").write_text(json.dumps({
    "schema_version": 1,
    "description": "F13 probe",
    "checks": [{
        "id": "some-check",
        "description": "a check this probe never runs",
        "command": ["python", "-c", "pass"],
        "timeout_seconds": 30.0,
        "required_scenarios": ["it runs"],
        "artifact": "result.json",
    }],
}), encoding="utf-8")
subprocess.run(["git", "-C", str(PROJECT), "add", "-A"], check=True)
subprocess.run(["git", "-C", str(PROJECT), "commit", "-qm", "manifest"], check=True)

project = open_project(PROJECT)
store = storage.Store(project.db_path)
source = compute_source_identity(project)
context = tasks.acceptance_context(project, lambda: __import__(
    "vkit.manifest", fromlist=["parse_manifest"]
).parse_manifest(project, project.runs_root / "probe"))

KEY = "scope:checkout"

print("=== 1. task A admitted at generation 1, holding the checkout ===")
a = tasks.admit(store, "A", context=context, resources=[{"key": KEY, "kind": "exclusive"}])
print(f"  A admitted at generation {a.generation}; holder = {claims.holder(store, KEY)}")

print()
print("=== 2. a live child writing into its run directory, run row has NO pid ===")
# This is the state a supervisor crash between register_run and mark_running
# leaves behind: lifecycle='preparing', process_json NULL, child alive.
marker = PROJECT / "still-writing.txt"
child = subprocess.Popen(
    [sys.executable, "-c",
     f"import time,pathlib;p=pathlib.Path({str(marker)!r});\n"
     f"for i in range(400): p.write_text(str(i)); time.sleep(0.05)"],
)
store.register_run(
    "run-A", "some-check", task_id="A", attempt=1,
    source=source.to_json(), configuration_digest="d", fixture_digest=None,
)
time.sleep(0.5)
with store._connect() as conn:
    row = conn.execute(
        "SELECT lifecycle, process_json FROM runs WHERE run_id='run-A'"
    ).fetchone()
print(f"  run row: lifecycle={row[0]!r} process_json={row[1]!r}")
print(f"  child pid {child.pid} alive = {child.poll() is None}")
print(f"  marker exists (child is writing) = {marker.exists()}")

print()
print("=== 3. supersede A to generation 2; claims are deliberately kept ===")
tasks.supersede_task(store, "A")
print(f"  holder after supersede = {claims.holder(store, KEY)}")

print()
print("=== 4. inspect -> is the claim offered for release? ===")
report = recover.inspect(store)
for finding in report.findings:
    if finding.kind is recover.FindingKind.CLAIM_STALE_GENERATION:
        print(f"  {finding.kind.name}: target={finding.target} action={finding.action}")

print()
print("=== 5. apply RELEASE_CLAIM ===")
try:
    recover.apply_action(store, recover.Action.RELEASE_CLAIM, target=KEY,
                         evidence="probe: supervisor crashed before publishing identity")
    print("  RELEASE SUCCEEDED")
    released = True
except recover.RecoveryRefused as exc:
    print(f"  REFUSED: {exc}")
    released = False

print()
print("=== 6. can a second task take the same checkout? ===")
b_admitted = False
try:
    b = tasks.admit(store, "B", context=context,
                    resources=[{"key": KEY, "kind": "exclusive"}])
    print(f"  B ADMITTED at generation {b.generation} -> holder = {claims.holder(store, KEY)}")
    b_admitted = True
except Exception as exc:
    print(f"  B refused: {type(exc).__name__}: {exc}")

print()
print("=== 7. is A's process still running and still writing? ===")
time.sleep(0.4)
alive = child.poll() is None
grew = marker.exists() and marker.read_text(encoding="utf-8") != "0"
print(f"  A's child alive = {alive}")
print(f"  A's child still advancing its output = {grew}")

child.kill()
child.wait()

print()
print("=" * 72)
if released and b_admitted and alive:
    print("CRITICAL CONFIRMED: the claim was released and re-admitted to B")
    print("while A's process was still alive and still writing to its run directory.")
elif released and b_admitted:
    print("PARTIAL: released and re-admitted, but the child was no longer alive.")
elif released:
    print("PARTIAL: released, but a second task could not take the key.")
else:
    print("NOT REPRODUCED: the release was refused.")
print("=" * 72)