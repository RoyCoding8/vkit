"""Break the core's stale-generation guard and require a test to catch it.

Plan 08's acceptance is specific: "A mutation removing the stale-generation
guard yields a counterexample", and "If mutating the guard does not break a
test, your test is not testing the guard". This module performs that mutation
against a COPY of the core and runs the covering test against the copy, so the
guard is genuinely removed and the failure is genuinely observed rather than
argued about.

The mutation is applied to a temporary file tree, not to the checkout, so a
failing mutation run leaves the working tree untouched. The guard is restored
by throwing the tree away.

Two mutations are applied, one per guard the plan names, and each must make a
named test go red:

  MUTANT_STALE_GENERATION   removes the generation comparison in
                           tasks.record_readiness_in, so a superseded attempt can
                           publish an accepted verdict.
  MUTANT_MISSING_CHECK      stops the readiness loop reporting a required check
                           that has no completed run, so acceptance stops
                           requiring every check.

Run directly:

    python formal/run_python_mutants.py

Exit 0 when each mutation made its test fail, 1 when a mutation passed (the
test is not covering the guard), 2 when pytest is unavailable, 3 when a test
could not be run at all.

Exit 3 exists because a nonzero exit is not a verdict. pytest uses four distinct
codes and only one of them means a test ran and failed: 4 is a usage error, which
is what a node id naming a file that is not there produces, and 2 is a collection
error. Reading either as "the test failed against the mutated core" reports a
guard as covered on the strength of a test that never executed. This harness
once did exactly that and reported both mutants caught when neither test ran.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
OUT = ROOT / "formal" / "results"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from digest import NORMALIZATION, canonical_sha256  # noqa: E402

_WINDOW_OPTIONS = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

_PASSED, FAILED, ERROR, USAGE, NOTHING_COLLECTED = 0, 1, 2, 4, 5


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
            '    decided_at = result.context.get("generation")\n'
            '    if decided_at != generation:\n'
            '        raise ConflictError(\n'
            '            f"task {task_id} was reassigned from generation {decided_at} to "\n'
            '            f"{generation}; refusing to record readiness for a superseded attempt"\n'
            '        )\n'
        ),
        replacement="",
        test="tests/test_stale_attempt_traces.py::test_a_superseded_attempt_cannot_publish_ready",
        covers="a superseded attempt publishing an accepted verdict",
    ),
    PythonMutant(
        name="MUTANT_MISSING_CHECK",
        relative="vkit/tasks.py",
        removes=(
            '            gaps.append(f"no completed run for required check '
            '{check_id!r}")\n'
        ),
        replacement="",
        test="tests/test_stale_attempt_traces.py::test_one_missing_required_check_is_never_ready",
        covers="acceptance with one required check absent",
    ),
)


@dataclass(frozen=True)
class Outcome:
    """What one mutated run actually established."""

    status: str
    note: str
    counterexample: str | None
    returncode: int


def _run_one(mutant: PythonMutant) -> Outcome:
    """Apply the mutation to a copy of src and run the named test on it."""
    source_file = SRC / mutant.relative
    original = source_file.read_text(encoding="utf-8")
    if mutant.removes not in original:
        return Outcome(
            "BLOCKED",
            f"the text it removes is no longer in {mutant.relative}. The guard was "
            f"reworded and the mutation now does nothing.",
            None, -1,
        )
    if original.count(mutant.removes) != 1:
        return Outcome(
            "BLOCKED",
            f"the text it removes appears {original.count(mutant.removes)} times in "
            f"{mutant.relative}. A mutation that cannot say which guard it removed "
            f"is not a measurement.",
            None, -1,
        )
    mutated = original.replace(mutant.removes, mutant.replacement, 1)

    with tempfile.TemporaryDirectory(prefix=f"vkit-py-{mutant.name}-") as work:
        workdir = Path(work)
        shutil.copytree(SRC, workdir / "src")
        (workdir / "src" / mutant.relative).write_text(mutated, encoding="utf-8")

        tests_dir = workdir / "tests"
        shutil.copytree(ROOT / "tests", tests_dir,
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "formal", workdir / "formal",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (tests_dir / "conftest.py").write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(workdir / 'src')!r})\n",
            encoding="utf-8",
        )
        (workdir / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\naddopts = '-q'\n", encoding="utf-8"
        )

        done = subprocess.run(
            [sys.executable, "-m", "pytest", mutant.test,
             "-p", "no:cacheprovider", "--no-header", "-x"],
            cwd=workdir, capture_output=True, text=True, timeout=1800, check=False,
            env={**os.environ, "PYTHONPATH": str(workdir / "src")},
            **_WINDOW_OPTIONS,
        )
        output = done.stdout + done.stderr
        return _classify(mutant, done.returncode, output)


def _classify(mutant: PythonMutant, returncode: int, output: str) -> Outcome:
    """Turn pytest's exit code into a verdict, never into a bare inequality.

    A mutant is caught when the named test RAN and FAILED against the mutated
    core. Every other nonzero code means the test did not reach a verdict, and
    calling that a catch credits a guard with coverage it did not get.
    """
    if returncode == FAILED:
        for line in output.splitlines():
            if line.strip().startswith("E "):
                return Outcome(
                    "CAUGHT",
                    "the test failed against the mutated core, as it must",
                    line.strip()[:110], returncode,
                )
        return Outcome(
            "CAUGHT",
            "the test failed against the mutated core, as it must",
            "the test failed, and its output named no single assertion line",
            returncode,
        )

    if returncode == _PASSED:
        return Outcome(
            "ESCAPED",
            "the test still passed against the mutated core. The guard is not "
            "covered, so the test is not testing what it claims.",
            None, returncode,
        )

    meaning = {
        ERROR: "pytest hit a collection error, so the test never ran",
        USAGE: "pytest rejected the node id as a usage error, so the test never ran",
        NOTHING_COLLECTED: "pytest collected nothing, so the test never ran",
    }.get(returncode, f"pytest exited {returncode}, which is not a verdict")
    return Outcome(
        "BLOCKED",
        f"{meaning}. A test that did not run is not a counterexample.",
        None, returncode,
    )


def main() -> int:
    if shutil.which("pytest") is None and not (ROOT / "src").exists():
        print("BLOCKED: pytest is not available.", file=sys.stderr)
        return 2

    print("python-core mutation results")
    print("=" * 72)
    failures: list[str] = []
    blocked: list[str] = []
    results: list[tuple[PythonMutant, Outcome]] = []
    for mutant in MUTANTS:
        outcome = _run_one(mutant)
        results.append((mutant, outcome))
        print(f"{outcome.status:>8}  {mutant.name}")
        print(f"          removes: {mutant.covers}")
        print(f"          outcome: {outcome.note}")
        if outcome.counterexample:
            print(f"          counterexample: {outcome.counterexample}")
        if outcome.status == "ESCAPED":
            failures.append(
                f"{mutant.name}: {outcome.note}"
            )
        elif outcome.status == "BLOCKED":
            blocked.append(f"{mutant.name}: {outcome.note}")

    print("=" * 72)
    if failures or blocked:
        if failures:
            print()
            for line in failures:
                print(f"FAILED: {line}")
        if blocked:
            print()
            for line in blocked:
                print(f"BLOCKED: {line}")
            print(
                "\nA mutation that could not be performed or could not be observed "
                "established nothing. Silence about a guard is not coverage of it."
            )
        _write_receipt(results, "FAIL" if failures else "BLOCKED")
        return 1 if failures else 3
    print(f"\nAll {len(MUTANTS)} Python mutations were caught by their tests.")
    _write_receipt(results, "PASS")
    return 0


def _write_receipt(results: list[tuple[PythonMutant, Outcome]], status: str) -> None:
    """Record what each mutation established, and the core it was measured against.

    A mutant result is a statement about one guard in one file at one digest, so
    the digest belongs in the receipt: without it the result outlives the text it
    was measured on and a later reader cannot tell whether it still applies.
    """
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "python-core-mutants-receipt.json").write_text(
        json.dumps({
            "status": status,
            "tool": "pytest",
            "target": "src/vkit/tasks.py",
            "target_sha256": canonical_sha256(SRC / "vkit" / "tasks.py"),
            "mutants": [
                {
                    "name": mutant.name,
                    "status": outcome.status,
                    "removes": mutant.covers,
                    "test": mutant.test,
                    "pytest_exit": outcome.returncode,
                    "counterexample": outcome.counterexample,
                    "note": outcome.note,
                }
                for mutant, outcome in results
            ],
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "digest_normalization": NORMALIZATION,
            "scope": (
                "A CAUGHT result means one named guard in tasks.py was removed and "
                "one named test failed against the mutated copy, on this host, at "
                "the digest above. It does not establish that the guard is the only "
                "way to break the rule, and it establishes nothing about any other "
                "module."
            ),
        }, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
