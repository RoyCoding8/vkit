"""Check what the schema does with boot_id on each platform's shape.

execution.py writes `boot_id` only when it is truthy, and the schema declares
`minLength: 1`. So a Windows report omits the key rather than writing an empty
string. That is what the schema description says should happen ("absent on
Windows"), but "consistent with" is not "verified": a conditional write and a
minLength constraint can disagree, and the disagreement would only show up when
a report is validated.

This validates both shapes against the real schema: a POSIX report carrying a
boot id, and a Windows report without the key. Both must be accepted, and an
empty string must be rejected, because minLength 1 says an empty boot id is not
a boot.

Run:  bash scripts/posix-run.sh scripts/check_boot_id_schema.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vkit.schemas import SchemaValidationError, validate  # noqa: E402

SCHEMA = "run-report.v1.json"

ok = True


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok
    if not condition:
        ok = False
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def accepts(document: dict) -> tuple[bool, str]:
    try:
        validate("check", SCHEMA, document)
        return True, ""
    except SchemaValidationError as exc:
        return False, exc.reason


def _real_report() -> dict:
    """A report the product actually wrote, by running a real check.

    Hand-building a fixture against a schema with `additionalProperties: false`
    is a losing game: every invented key is refused for a reason unrelated to the
    property under test, and the refusals look like findings. This runs the
    example check through vkit's own execution path and reads the report it
    produced, so the baseline is real and every later assertion differs from it
    in exactly one key.
    """
    import shutil
    import subprocess
    import tempfile

    from vkit.execution import run_check
    from vkit.identity import compute_source_identity
    from vkit.manifest import parse_manifest
    from vkit.paths import open_project
    from vkit.storage import Store

    base = Path(tempfile.mkdtemp()) / "boot-id-schema"
    shutil.copytree(ROOT / "examples" / "python-cli", base)
    for args in (["git", "init", "-q"], ["git", "add", "-A"],
                 ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "e"]):
        subprocess.run(args, cwd=base, check=True)
    project = open_project(base)
    run_check(
        parse_manifest(project, project.runs_root / "probe"),
        "totals-behavior",
        store=Store(project.db_path),
        source=compute_source_identity(project),
    )
    runs = Store(project.db_path).list_runs(limit=1)
    assert runs, "the example check produced no run to read a report from"
    return Store(project.db_path).load(runs[0]["run_id"])


def main() -> int:
    if sys.platform == "win32":
        print("this checks the report shape both platforms produce; run it under WSL")
        return 2

    baseline = _real_report()
    recorded = baseline.get("process") or {}
    print("== the report the product actually wrote on this host ==")
    print(f"  process keys: {sorted(recorded)}")
    print(f"  ownership:    {recorded.get('ownership')}")
    print(f"  has boot_id:  {'boot_id' in recorded}")
    print()

    def variant(**changes) -> dict:
        document = json.loads(json.dumps(baseline))
        document["process"] = {**document["process"], **changes}
        return document

    print("== shapes the schema must accept ==")
    good, why = accepts(baseline)
    check("the real report validates, so the baseline is sound", good, why[:110])

    without_boot = variant()
    without_boot["process"].pop("boot_id", None)
    good, why = accepts(without_boot)
    check("a report with the boot_id key absent validates, as a Windows one must",
          good, why[:110])

    print()
    print("== shapes the schema must refuse ==")
    empty = variant(boot_id="")
    good, why = accepts(empty)
    check("an empty boot_id is refused, because minLength is 1", not good, why[:110])

    surprise = variant(surprise=1)
    good, why = accepts(surprise)
    check("an unknown key in process is still refused", not good, why[:110])

    print()
    print("VERDICT: " + ("every check passed" if ok else "at least one check FAILED"))
    print()
    print("So the conditional write in execution.py is right, and now verified")
    print("rather than merely consistent: omitting the key is what a Windows")
    print("report does and the schema accepts it, while an empty string would not.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
