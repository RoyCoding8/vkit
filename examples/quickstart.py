from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
EXAMPLE = Path(__file__).resolve().parent / "python-cli"


def prepare_demo() -> tuple[Path, dict[str, str]]:
    run_root = REPOSITORY / "tmp" / f"vkit-demo-{uuid.uuid4().hex[:10]}"
    project = run_root / "project"
    shutil.copytree(EXAMPLE, project)

    config_dir = run_root / "config"
    config_dir.mkdir()
    initialized = subprocess.run(
        ["git", "init", "--quiet"],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    if initialized.returncode:
        raise RuntimeError(initialized.stderr.strip() or "git init failed")

    environment = os.environ.copy()
    environment["VKIT_CONFIG"] = str(config_dir / "vkit.json")
    scripts = str(Path(sys.executable).parent)
    environment["PATH"] = os.pathsep.join((scripts, environment.get("PATH", "")))
    return project, environment


def run(command: list[str], project: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    print(f"\n$ {shlex.join(command)}", flush=True)
    result = subprocess.run(
        command,
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result


def main() -> int:
    try:
        project, environment = prepare_demo()
    except (OSError, RuntimeError, shutil.Error) as exc:
        print(f"error: could not prepare the demo: {exc}", file=sys.stderr)
        return 1

    relative_project = project.relative_to(REPOSITORY).as_posix()
    print(f"Demo project: {relative_project}")
    print("This demo accepts only the bundled `totals-behavior` definition from the copied example.")

    accepted = run(
        ["vkit", "accept", "--project", ".", "--check", "totals-behavior", "--yes"],
        project,
        environment,
    )
    if accepted.returncode:
        return accepted.returncode

    checked = run(
        ["vkit", "check", "run", "--project", ".", "--check", "totals-behavior"],
        project,
        environment,
    )
    if checked.returncode:
        return checked.returncode
    if "totals-behavior: PASS" not in checked.stdout:
        print("error: totals-behavior did not report PASS", file=sys.stderr)
        return 1

    gate = run(["vkit", "gate", "--project", "."], project, environment)
    if gate.returncode:
        return gate.returncode
    if not gate.stdout.startswith("READY:"):
        print("error: the demo gate did not report READY", file=sys.stderr)
        return 1

    print("\nFrom the checkout, continue with:")
    print(f"  uv run vkit check run --project {relative_project} --check totals-behavior")
    print(f"  uv run vkit gate --project {relative_project}")
    print(f"  uv run vkit console --project {relative_project}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
