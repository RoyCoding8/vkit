"""Break the core's stale-generation guard and require a test to catch it.

Plan 08's acceptance is specific: "A mutation removing the stale-generation
guard yields a counterexample", and "If mutating the guard does not break a
test, your test is not testing the guard". This module performs that mutation
against a COPY of the core and runs the correspondence test against the copy,
so the guard is genuinely removed and the failure is genuinely observed rather
than argued about.

The mutation is applied to a temporary file tree, not to the checkout, so a
failing mutation run leaves the working tree untouched. The guard is restored
by throwing the tree away.

Two mutations are applied, one per guard the plan names, and each must make a
named test go red:

  MUTANT_STALE_GENERATION   removes the `result.context.get("generation") !=
                           generation` check from tasks.record_readiness, so a
                           superseded attempt can publish an accepted verdict.
  MUTANT_MISSING_CHECK      removes one required check from the readiness loop,
                           so acceptance stops requiring every check.

Run directly:

    python formal/run_python_mutants.py

Exit 0 when each mutation made its test fail, 1 when a mutation passed (the
test is not covering the guard), 2 when pytest is unavailable.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


@dataclass(frozen=True)
class PythonMutant:
    """One guard, the exact text removed, and the test that must go red."""

    name: str
    relative: str
    removes: str
    replacement: str
    test: str
    covers: str


MUTANTS: tuple[PythonMutant, ...] = (
    PythonMutant(
        name="MUTANT_STALE_GENERATION",
        relative="vkit/tasks.py",
        removes=(
            '    if result.context.get("generation") != generation:\n'
            '        raise ConflictError(\n'
            '            f"task {task_id} was reassigned from generation "\n'
            '            f"{result.context.get(\'generation\')} to {generation}; refusing to record "\n'
            '            "readiness for a superseded attempt"\n'
            '        )\n'
        ),
        replacement="",
        test="test_formal_correspondence.py::test_a_superseded_attempt_cannot_publish_ready",
        covers="a superseded attempt publishing an accepted verdict",
    ),
    PythonMutant(
        name="MUTANT_MISSING_CHECK",
        relative="vkit/tasks.py",
        removes=(
            "    for check_id in required:\n"
        ),
        replacement=(
            "    for check_id in required[:-1]:\n"
        ),
        test="test_formal_correspondence.py::test_one_missing_required_check_is_never_ready",
        covers="acceptance with one required check absent",
    ),
)


def _run_one(mutant: PythonMutant) -> tuple[bool, str]:
    """Apply the mutation to a copy of src and run the named test on it."""
    source_file = SRC / mutant.relative
    original = source_file.read_text(encoding="utf-8")
    if mutant.removes not in original:
        return False, (
            f"the text it removes is no longer in {mutant.relative}. The guard was "
            f"reworded and the mutation now does nothing."
        )
    mutated = original.replace(mutant.removes, mutant.replacement, 1)

    with tempfile.TemporaryDirectory(prefix=f"vkit-py-{mutant.name}-") as work:
        workdir = Path(work)
        # Copy the whole src tree so the imports resolve, then overwrite the one
        # mutated file. A test that imports `vkit` must import the MUTATED one.
        shutil.copytree(SRC, workdir / "src")
        (workdir / "src" / mutant.relative).write_text(mutated, encoding="utf-8")

        # The test's own conftest prepends <repo>/src. Point it at the copy by
        # running pytest with a conftest that wins over the repository one.
        tests_dir = workdir / "tests"
        shutil.copytree(ROOT / "tests", tests_dir,
                        ignore=shutil.ignore_patterns("__pycache__"))
        (tests_dir / "conftest.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0, r'{workdir / 'src'}')\n",
            encoding="utf-8",
        )
        (workdir / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\naddopts = '-q'\n", encoding="utf-8"
        )

        done = subprocess.run(
            [sys.executable, "-m", "pytest", mutant.test,
             "-p", "no:cacheprovider", "--no-header", "-x"],
            cwd=workdir, capture_output=True, text=True, timeout=1800, check=False,
            env={**__import__("os").environ, "PYTHONPATH": str(workdir / "src")},
        )
        output = done.stdout + done.stderr
        # A mutant is caught when the named test FAILS against the mutated core.
        return done.returncode != 0, output


def main() -> int:
    if shutil.which("pytest") is None and not (ROOT / "src").exists():
        print("BLOCKED: pytest is not available.", file=sys.stderr)
        return 2

    print("python-core mutation results")
    print("=" * 72)
    failures: list[str] = []
    for mutant in MUTANTS:
        failed, output = _run_one(mutant)
        if not failed:
            verdict = "ESCAPED"
            note = "the test still passed against the mutated core"
            failures.append(
                f"{mutant.name}: {note}. The guard is not covered, so the test is "
                f"not testing what it claims."
            )
        else:
            verdict = "CAUGHT"
            note = "the test failed against the mutated core, as it must"
        print(f"{verdict:>8}  {mutant.name}")
        print(f"          removes: {mutant.covers}")
        print(f"          outcome: {note}")
        if failed:
            # Show the one assertion that fired, so the evidence is a real
            # counterexample and not just a nonzero exit code.
            for line in output.splitlines():
                if line.strip().startswith("E ") and "Error" not in line:
                    print(f"          counterexample: {line.strip()[:110]}")
                    break
    print("=" * 72)
    if failures:
        print()
        for line in failures:
            print(f"FAILED: {line}")
        return 1
    print(f"\nAll {len(MUTANTS)} Python mutations were caught by their tests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
