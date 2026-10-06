"""End-to-end gate: drive each example through the real `vkit` CLI and assert literal outcomes.

Run with `uv run python scripts/e2e.py`. Exit 0 means every row matched.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
MISSING_TOOL = "vkit-e2e-missing-tool"
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


@dataclass(frozen=True)
class Example:
    name: str
    checks: tuple[str, ...]
    bug_file: str
    bug_from: str
    bug_to: str
    failing_check: str


EXAMPLE_TABLE = (
    Example("python-cli", ("totals-behavior",), "src/totals.py",
            "running += amount", "running += abs(amount)", "totals-behavior"),
    Example("node-cli", ("split-bill-behavior", "split-bill-units"), "src/split-bill.js",
            "handed < leftover;", "handed < leftover - 1;", "split-bill-behavior"),
    Example("node-http", ("items-api", "items-api-sockets"), "src/items-server.js",
            "items.push({ name: parsed.name });", "items.push({ name: parsed.name + '!' });", "items-api"),
)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=e2e@vkit", "-c", "user.name=e2e", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True, **NO_WINDOW)


def fresh_copy(example: Example, scratch: Path) -> Path:
    target = scratch / example.name
    shutil.copytree(EXAMPLES / example.name, target)
    git(target, "init", "-q")
    git(target, "add", ".")
    git(target, "commit", "-qm", "init")
    code, body = vkit(target, "accept", "--yes")
    if code != 0:
        raise SystemExit(f"e2e setup: accept failed in {target}: {body}")
    return target


def vkit(project: Path, *args: str) -> tuple[int, dict]:
    done = subprocess.run([sys.executable, "-m", "vkit.cli", *args, "--project", str(project), "--json"],
                          capture_output=True, text=True, timeout=600, **NO_WINDOW)
    try:
        body = json.loads(done.stdout)
    except json.JSONDecodeError:
        body = {"unparsed_stdout": done.stdout[-500:], "stderr": done.stderr[-500:]}
    return done.returncode, body


def check_run(project: Path, check_id: str) -> tuple[int, dict]:
    code, body = vkit(project, "check", "run", "--check", check_id)
    runs = body.get("runs") or [{}]
    return code, {**body, "outcome": runs[0].get("outcome") or {}}


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"e2e setup: expected exactly one {old!r} in {path}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def rows(example: Example, scratch: Path) -> list[tuple[str, bool, str]]:
    out = []
    clean = fresh_copy(example, scratch / "clean")
    for check_id in example.checks:
        code, body = check_run(clean, check_id)
        result = body.get("outcome", {}).get("result")
        out.append((f"{example.name}/{check_id} passes on clean source", code == 0 and result == "PASS",
                    f"exit={code} result={result}"))

    code, body = vkit(clean, "gate")
    out.append((f"{example.name} gate is READY after every check passed", code == 0 and body.get("verdict") == "READY",
                f"exit={code} verdict={body.get('verdict')}"))
    with open(clean / example.bug_file, "a", encoding="utf-8") as handle:
        handle.write("\n")
    code, body = vkit(clean, "status", "--path", example.bug_file)
    states = {c["id"]: c["state"] for c in body.get("checks", [])}
    out.append((f"{example.name} an edit to {example.bug_file} makes {example.failing_check} stale",
                states.get(example.failing_check) == "stale", f"states={states}"))
    code, body = vkit(clean, "gate")
    out.append((f"{example.name} gate is BLOCKED while a check is stale", code == 3 and body.get("verdict") == "BLOCKED",
                f"exit={code} verdict={body.get('verdict')}"))
    code, body = vkit(clean, "check", "run", "--needed")
    ran = sorted(r["check_id"] for r in body.get("runs", []))
    out.append((f"{example.name} check run --needed reruns exactly the stale checks",
                code == 0 and ran == sorted(states), f"exit={code} ran={ran} stale={sorted(states)}"))
    code, body = vkit(clean, "gate")
    out.append((f"{example.name} gate is READY again", code == 0 and body.get("verdict") == "READY",
                f"exit={code} verdict={body.get('verdict')}"))

    buggy = fresh_copy(example, scratch / "buggy")
    replace_once(buggy / example.bug_file, example.bug_from, example.bug_to)
    code, body = check_run(buggy, example.failing_check)
    outcome = body.get("outcome", {})
    failed = [s for s in outcome.get("scenarios", []) if s.get("result") == "FAIL" and s.get("observation")]
    out.append((f"{example.name}/{example.failing_check} fails on seeded bug with an observation",
                code == 1 and outcome.get("result") == "FAIL" and bool(failed),
                f"exit={code} result={outcome.get('result')} failed_scenarios={len(failed)}"))

    blocked = fresh_copy(example, scratch / "blocked")
    manifest = blocked / "verification" / "manifest.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    for check in document["checks"]:
        for prerequisite in check.get("prerequisites", []):
            prerequisite["executable"] = MISSING_TOOL
    manifest.write_text(json.dumps(document, indent=2), encoding="utf-8")
    code, body = check_run(blocked, example.checks[0])
    outcome = body.get("outcome", {})
    out.append((f"{example.name}/{example.checks[0]} is BLOCKED after its definition changed without acceptance",
                code == 3 and outcome.get("result") == "BLOCKED" and outcome.get("reason") == "not_approved",
                f"exit={code} result={outcome.get('result')} reason={outcome.get('reason')}"))
    vkit(blocked, "accept", "--yes")
    code, body = check_run(blocked, example.checks[0])
    outcome = body.get("outcome", {})
    out.append((f"{example.name}/{example.checks[0]} is BLOCKED when its tool is missing",
                code == 3 and outcome.get("result") == "BLOCKED" and outcome.get("reason") == "prerequisite_missing",
                f"exit={code} result={outcome.get('result')} reason={outcome.get('reason')}"))
    return out


def main() -> int:
    only = set(sys.argv[1:])
    results = []
    with tempfile.TemporaryDirectory(prefix="vkit-e2e-") as raw:
        scratch = Path(raw)
        for example in EXAMPLE_TABLE:
            if only and example.name not in only:
                continue
            results.extend(rows(example, scratch))
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({detail})")
    failures = sum(1 for _, ok, _ in results if not ok)
    print(f"{len(results) - failures}/{len(results)} rows matched")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
