r"""Reproduce candidate-controlled checker false acceptance in an isolated Git fixture.

Run from the repository root with .venv\Scripts\python.exe review\probe_integration.py.
The temporary repository is deleted automatically; the workspace is not changed.
"""
from __future__ import annotations
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import fixtures as fx  # noqa: E402
from test_integration import verify  # noqa: E402

def main() -> int:
    with tempfile.TemporaryDirectory(prefix="vkit-integration-audit-") as temp:
        repo = fx.build_repository(Path(temp) / "repo")
        target = fx.main_revision(repo)
        approved_manifest_revision = fx.baseline_revision(repo)
        fx.checkout_branch(repo, 'audit-candidate')
        (repo / fx.QUOTE_NAME).write_text('print("compromised")\n', encoding="utf-8")
        fx.git(repo, 'add', '.')
        fx.git(repo, 'commit', '-m', 'break app with original driver retained')
        broken_candidate = fx.git(repo, 'rev-parse', 'HEAD')
        original_completed, original_record = verify(repo, broken_candidate, target, '@main')
        passed = {
            "schema_version": 1,
            "scenarios": [
                {"id": scenario_id, "result": "PASS", "observation": "forged"}
                for scenario_id, _args, _expected in fx.CASES
            ],
        }
        driver = "\n".join([
            "import argparse",
            "from pathlib import Path",
            "p=argparse.ArgumentParser(); p.add_argument('--app'); p.add_argument('--out'); a=p.parse_args()",
            "out=Path(a.out); out.parent.mkdir(parents=True,exist_ok=True)",
            f"out.write_text({json.dumps(passed)!r},encoding='utf-8')",
            "",
        ])
        (repo / fx.DRIVER_NAME).write_text(driver, encoding="utf-8")
        fx.git(repo, "add", ".")
        fx.git(repo, "commit", "-m", "break app and forge checker result")
        candidate = fx.git(repo, "rev-parse", "HEAD")
        completed, record = verify(repo, candidate, target, "@main")
        result = {
            "exit_code": completed.returncode,
            "decision": record.get("decision"),
            "candidate": candidate,
            "target": target,
            "approved_manifest_revision": approved_manifest_revision,
            "approved_main_stayed_fixed": fx.main_revision(repo) == target,
            "broken_app_with_original_driver": {
                "candidate": broken_candidate,
                "exit_code": original_completed.returncode,
                "decision": original_record.get('decision'),
                "check_results": [c['result'] for c in original_record.get('checks', [])],
            },
            "check_results": [check["result"] for check in record.get("checks", [])],
            "check_details": [check.get("detail") for check in record.get("checks", [])],
            "gaps": record.get("gaps", []),
            "stderr": completed.stderr,
        }
        Path(__file__).with_name("probe-integration-results.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(result, indent=2))
        return 0 if (completed.returncode == 0 and record.get("decision") == "ACCEPTED"
                     and original_record.get('decision') != 'ACCEPTED'
                     and fx.main_revision(repo) == target) else 1

if __name__ == "__main__":
    raise SystemExit(main())
