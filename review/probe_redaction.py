"""What the matrix row "Secret appears in command output" is actually implemented.

`tmp/research/KIT_ACCEPTANCE.md` advertises two things in one row: "Apply the
project's redaction rules before sharing evidence; do not dump full environment
variables". R5d reported that the second half has real coverage and the first has
none, and that a grep for `redact|mask|secret|password` across `src/vkit/` turns up
only console session-token code and a docstring in `identity.py`. This probe
checks that by running the product, because a grep over a codebase cannot
distinguish a routine that is never called from one that is.

The row has three separable claims, and they have three different answers:

  A. The report's `environment` block does not carry the process environment.
     VERIFIED, and covered by `tests/test_storage.py::test_a_reported_environment_
     carries_no_credential`. Asserted here again from the outside so the claim
     does not rest on one test file.
  B. A secret the CHECK ITSELF prints is kept out of `stdout.log` / `stderr.log`.
     NOT IMPLEMENTED. The child's stdout file descriptor IS the log file, so
     there is no code between the check's write and the disk, and this probe
     reads a real secret back off the disk to show it.
  C. A secret is redacted before evidence is shared or exported. NOT IMPLEMENTED,
     and not even reachable: there is no export, share or bundle surface in
     `src/`, so "before sharing" names a step that does not exist. Asserted by
     the absence of such a surface rather than by a failure.
  D. The reader every surface shares returns filtered text. NOT IMPLEMENTED.
     `_read_page` bounds a window by byte offset and decodes it, and does nothing
     else to the bytes, so a secret that reached the log is served to the caller.

    python review/probe_redaction.py

Writes `review/redaction.json`. Exits 0 when the measured answers are the ones
this docstring claims and 1 when they are not, so the claim in
`formal/RESULTS.md` cannot drift from the product silently.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import fixtures  # noqa: E402

from vkit import tasks  # noqa: E402
from vkit.execution import run_check  # noqa: E402
from vkit.identity import compute_source_identity  # noqa: E402
from vkit.manifest import parse_manifest  # noqa: E402
from vkit.mcp import _tools as mcp_tools  # noqa: E402
from vkit.paths import open_project  # noqa: E402
from vkit.storage import Store  # noqa: E402

EXAMPLE = ROOT / "examples" / "python-cli"
SECRET = "sk-live-must-never-reach-a-shared-artifact-0f3a91"
MANIFEST_RELATIVE = "verification/manifest.json"
CHECK_ID = "noisy"
TASK = "t"

#: The half of the row that holds, and the assertion that decides it.
A_ANSWER = "VERIFIED"
B_ANSWER = "NOT IMPLEMENTED"
C_ANSWER = "NOT IMPLEMENTED"


def _project(root: Path):
    """The shipped example under a policy whose check prints the secret itself."""
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(EXAMPLE, root, dirs_exist_ok=True)
    printer = root / "print_secret.py"
    printer.write_text(
        "import os, sys\n"
        "print('token=' + os.environ['VKIT_PROBE_SECRET'])\n"
        "sys.stderr.write('password=' + os.environ['VKIT_PROBE_SECRET'] + '\\n')\n",
        encoding="utf-8",
    )
    fixtures.write_json(root / MANIFEST_RELATIVE, {"schema_version": 1, "checks": [{
        "id": CHECK_ID,
        "command": ["python", "print_secret.py", "--out", "{{run_dir}}/result.json"],
        "cwd": ".",
        "timeout_seconds": 60,
        "required_scenarios": ["only"],
        "artifact": "result.json",
    }]})
    fixtures.git(root, "init", "-q", "-b", "main")
    fixtures.git(root, "config", "user.email", "t@example.invalid")
    fixtures.git(root, "config", "user.name", "Redaction Probe")
    fixtures.commit_all(root, "a policy whose check prints a secret")
    return open_project(root)


def _claim_a(store, report: dict) -> dict:
    """Does the report's environment block carry the process environment?"""
    environment = report.get("environment", {})
    return {
        "claim": "A. the report's environment block omits the process environment",
        "answer": A_ANSWER if SECRET not in json.dumps(report) else "NOT VERIFIED",
        "environment_block_keys": sorted(environment),
        "secret_in_report": SECRET in json.dumps(report),
        "provenance_keys": sorted(report.get("provenance", {})),
    }


def _claim_b(run_dir: Path) -> dict:
    """Does a secret the check printed survive on disk and in the log reader?"""
    on_disk = {
        name: (run_dir / name).read_bytes() for name in ("stdout.log", "stderr.log")
    }
    return {
        "claim": "B. a secret the check itself prints is kept out of the logs",
        "answer": B_ANSWER if any(SECRET.encode() in blob for blob in on_disk.values())
        else "IMPLEMENTED",
        "secret_bytes_in_stdout_log": SECRET.encode() in on_disk["stdout.log"],
        "secret_bytes_in_stderr_log": SECRET.encode() in on_disk["stderr.log"],
        "stdout_log_contents": on_disk["stdout.log"].decode("utf-8", "replace").strip(),
        "stderr_log_contents": on_disk["stderr.log"].decode("utf-8", "replace").strip(),
    }


def _reader_is_passthrough(store: Store, run_id: str) -> dict:
    """What `run_get` hands a caller for the same log bytes."""
    page = mcp_tools._read_page(store, run_id, "stdout.log", 0, 4096)
    return {
        "claim": "D. the log reader every surface shares filters what it returns",
        "answer": "NOT IMPLEMENTED" if SECRET in page.get("text", "") else "IMPLEMENTED",
        "surface": "vkit.mcp._tools._read_page",
        "text_contains_secret": SECRET in page.get("text", ""),
        "text": page.get("text", "").strip(),
    }


def _claim_c() -> dict:
    """Is there any surface on which evidence could be shared, to be redacted?"""
    surfaces = []
    for path in sorted((ROOT / "src" / "vkit").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                word in node.name.lower() for word in ("export", "share", "bundle", "redact")
            ):
                surfaces.append(f"{path.relative_to(ROOT).as_posix()}:{node.name}")
    return {
        "claim": "C. evidence is redacted before it is shared or exported",
        "answer": C_ANSWER if not surfaces else "IMPLEMENTED",
        "export_share_or_redact_definitions": surfaces,
        "note": (
            "no export, share or redact surface exists in src/vkit, so the row's "
            "'before sharing evidence' names a step the product does not have"
        ),
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="vkit-redact-") as raw:
        root = Path(raw) / "repo"
        os.environ["VKIT_PROBE_SECRET"] = SECRET
        try:
            project = _project(root)
            store = Store(project.db_path)
            manifest = parse_manifest(project, project.runs_root)
            outcome = run_check(
                manifest, CHECK_ID, store=store,
                source=compute_source_identity(project), task_id=TASK, attempt=1,
            )
            report = outcome.report
            run_dir = store.run_dir(outcome.report["run_id"])

            claims = [
                _claim_a(store, report),
                _claim_b(run_dir),
                _reader_is_passthrough(store, outcome.report["run_id"]),
                _claim_c(),
            ]
            log_refs = report.get("logs", {})
        finally:
            os.environ.pop("VKIT_PROBE_SECRET", None)

    report = {
        "secret_probe_value_prefix": SECRET[:7] + "...",
        "log_references_in_report": log_refs,
        "claims": claims,
    }
    (ROOT / "review" / "redaction.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )

    print("matrix row: Secret appears in command output")
    print("=" * 96)
    for claim in claims:
        print(f"\n{claim['claim']}")
        print(f"  {claim['answer']}")
        for key, value in claim.items():
            if key in ("claim", "answer", "note"):
                continue
            print(f"  {key:<34} {value}")
        if claim.get("note"):
            print(f"  {claim['note']}")
    print("=" * 96)
    print("written: review/redaction.json")

    answers = {c["claim"][0]: c["answer"] for c in claims}
    expected = {"A": A_ANSWER, "B": B_ANSWER, "C": C_ANSWER, "D": "NOT IMPLEMENTED"}
    wrong = {k: (v, expected[k]) for k, v in answers.items() if v != expected[k]}
    if wrong:
        print(f"\nFAILED: measured answer differs from the claim: {wrong}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
