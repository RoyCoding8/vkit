#!/usr/bin/env python3
"""One entry point for a POSIX verification run of this repository.

Eighteen shell scripts ran one suite. Ten of them exited 0 on a fully failed
suite, and the reason was the same in every case: a verdict computed from
output rather than from a status. The scripts decided from `F` and `E` marks
left by a progress reporter, so a file that died before writing a mark read as
clean; one decided from the output of a pipeline, so pytest's status was thrown
away by a `tee`; one carried no exit statement at all and read as safe for that
reason. Counting exit statements finds a missing exit and misses an exit code a
pipeline threw away.

So the one rule this file is built around: **a verdict is an exit code, and the
exit code is the child's.** Nothing here parses pytest's output to decide
whether a run passed. The counts are printed for a reader, and the number that
decides is the number the process returned.

Three things make that possible.

    `subprocess` returns the child's status. There is no pipeline to lose it
    in, and no shell whose last stage becomes the verdict.

    Every file runs alone, in its own session, under a timeout. A run that
    cannot finish is a bounded fact rather than a hang, and a file that dies
    leaves no report, which counts as a failure here. An absent report is the
    case the old scripts got backwards: they read "no marks" as "no failures".

    Counts come from the junitxml report, which is a file and survives both a
    redirect and a killed run. The progress dots do not, and a redirected
    `pytest -q` writes its summary to a terminal writer that is not stdout, so a
    log of one holds only dots. The report is the record; the console is not.

## The variants, and why there are any

Most of the old entry points were the same run with a different flag. The
distinct jobs are:

    suite       every test file, one process each, one total. The default.
    per-file    one named file. A red file is a thing to read, not a total.
    why         one file, with the assertion line and the named failures under
                it. A dot line says how many failed and not why.
    acceptance  an acceptance script, with the two streams captured so a
                silent death is visible as an absence of bytes.
    rows        each acceptance02 row in its own process, one verdict each. The
                full run dies with no output on this host, and per-row
                isolation turns that silence into a per-row fact.
    doctor      a per-file run watched for memory pressure, then a verdict on
                why. It exists to diagnose a death, so it does real work.
    totals      total the junit reports of a completed run. The receipts
                outlive the run, so a total is a fact about a run that happened
                rather than a thing being observed now.
    single      the whole suite in one pytest process, watched while it runs.
                Measures whether the suite completes at all, which the per-file
                loop cannot answer.
    run         one command against the venv with the environment set up.

The venv and its PATH are `scripts/posix_setup.py`, which every subcommand
below uses, so no entry point has a copy of the directory list to drift from.

## Running it

    python3 scripts/posix_harness.py                       # the whole suite
    python3 scripts/posix_harness.py per-file test_claims  # one file
    python3 scripts/posix_harness.py why test_claims       # one file, and why
    python3 scripts/posix_harness.py rows 3 7 9            # three acceptance rows
    python3 scripts/posix_harness.py totals                # total a past run
    python3 scripts/posix_harness.py run scripts/acceptance02.py
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# scripts/ is not on sys.path, so a sibling import needs it. scripts/ holds
# pytest files and plain scripts side by side, so this is the one line that
# makes `from posix_setup import` resolve at all.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from posix_setup import (  # noqa: E402
    REPO_ROOT,
    posix_python,
    reports_dir,
    require_venv,
    run_env,
)

# One test file, bounded, in its own session. Every number here is inherited
# from the shell scripts, which is the same reason to keep them as to tune
# them: nothing measured a different one.
FILE_TIMEOUT = 900
# acceptance02 rows drive real child interpreters over a 300-second budget, so
# one row gets a tighter bound than a test file.
ROW_TIMEOUT = 420

# pytest's own exit code for "no tests collected". A fact about the file rather
# than a failure, so it is named here and the doctor treats it as one.
NO_TESTS_COLLECTED = 5


@dataclass
class Outcome:
    """What one child process did, and what its report says about it.

    `status` is the verdict. The counts are for a reader. Holding both in one
    object is what stops the printed summary and the returned code from being
    able to disagree.
    """

    name: str
    status: int
    #: (tests, failures, errors, skipped) from the junit report, or None when
    #: there is no usable report. A file with no report is a file that died, so
    #: this is never quietly read as zero.
    counts: tuple[int, int, int, int] | None = None
    note: str = ""
    log: Path | None = None

    @property
    def failed(self) -> bool:
        """True when this child did not pass.

        A death counts. The status alone is the verdict, and the report is a
        second independent reason; either one is enough. A report that parses
        and counts no tests describes no run, so that is a death too rather
        than a clean file.
        """
        if self.status != 0 or self.counts is None:
            return True
        tests, failures, errors, _skipped = self.counts
        return bool(failures or errors) or tests == 0


@dataclass
class Run:
    """Every file of one run, and the counts that decide it.

    There is no path through this object that turns a run in which a file
    produced nothing into a pass.
    """

    label: str
    outcomes: list[Outcome] = field(default_factory=list)

    def add(self, outcome: Outcome) -> None:
        self.outcomes.append(outcome)

    @property
    def status(self) -> int:
        # A run of no files is not a pass. It is a run that collected nothing,
        # which is a fact about the target rather than a clean bill of health,
        # and reading it as PASS is the false green this whole file exists to
        # end. pytest's own code for it is 5, so this returns the same thing.
        if not self.outcomes:
            return NO_TESTS_COLLECTED
        return 1 if any(outcome.failed for outcome in self.outcomes) else 0

    def totals(self) -> tuple[int, int, int, int, int]:
        """(tests, failures, errors, skipped, no report) summed over the run.

        The no-report count travels alongside the tests rather than being left
        out. A file that died contributes zero tests, so a total on its own
        cannot say whether it was missing or clean, and a reader who sees
        `643 tests` has no way to know a file died.
        """
        tests = failures = errors = skipped = silent = 0
        for outcome in self.outcomes:
            if outcome.counts is None:
                silent += 1
                continue
            t, f, e, s = outcome.counts
            tests += t
            failures += f
            errors += e
            skipped += s
        return tests, failures, errors, skipped, silent

    def failures(self) -> list[Outcome]:
        return [outcome for outcome in self.outcomes if outcome.failed]


# -------------------------------------------------------------------- reports


# The list of files a run attempted, written next to its reports.
#
# A junit report is written by a process that finished, so a file that died
# leaves no report at all. Reading the reports alone therefore cannot tell a
# file that was never run from a file that was run and died: measured on a tree
# where one file called `os._exit(9)`, `totals` summed the two surviving reports
# and reported PASS while the run that wrote them had a third file that produced
# nothing. That is the same false green the shell scripts had, one layer up.
#
# So the run records what it set out to run, and the reader compares. A name in
# the manifest with no report beside it is a death, and a death is a failure.
MANIFEST = "manifest.txt"


def read_manifest(directory: Path) -> list[str] | None:
    """The file stems a past run attempted, or None if it recorded none.

    None rather than an empty list, because "no manifest" and "a manifest
    naming no files" are different states and only the first is a run whose
    attempt list is unknown.
    """
    path = directory / MANIFEST
    if not path.is_file():
        return None
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


def write_manifest(directory: Path, names: list[str]) -> None:
    """Record what this run is about to attempt, before it starts.

    Written before the first child, because a run that dies part-way is exactly
    the run whose manifest is the only account of what it never got to.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MANIFEST).write_text("\n".join(names) + "\n", encoding="utf-8")


def read_report(path: Path) -> tuple[int, int, int, int] | None:
    """(tests, failures, errors, skipped) from a junit report, or None.

    None means there is no usable report, which is a death rather than a clean
    file and is never read as zero. The `testsuites` wrapper is unwrapped here
    rather than at each call site, because every reader wants the suite element
    and a caller that forgets returns no suite, which this function would then
    report as a file that ran nothing.
    """
    if not path.is_file():
        return None
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None
    suite = root.find("testsuite") if root.tag == "testsuites" else root
    if suite is None:
        return None
    return (
        int(suite.get("tests", 0)),
        int(suite.get("failures", 0)),
        int(suite.get("errors", 0)),
        int(suite.get("skipped", 0)),
    )


# --------------------------------------------------------------------- running


def _have(program: str, env: dict[str, str]) -> bool:
    from shutil import which

    return which(program, path=env.get("PATH")) is not None


def _which(program: str, env: dict[str, str]) -> str:
    from shutil import which

    return which(program, path=env.get("PATH")) or "NONE"


def _which_or(program: str, env: dict[str, str], fallback: list[str]) -> list[str]:
    """`fallback` when the program is on this host's PATH, else nothing.

    A host without `setsid` or without `timeout` still runs; it just gets less
    isolation and no bound. Skipping the run is the worse answer, so the
    isolation is a bound and not a requirement.
    """
    return [program] if _have(program, env) else []


def run_isolated(
    argv: list[str],
    log: Path,
    *,
    timeout: int,
    junitxml: Path | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """Run one child, capture its output, return its exit status.

    The status comes back from `subprocess`, which is the only place it exists.
    Nothing is inferred from the log, and there is no pipeline whose last stage
    could become the verdict.

    A new session is what makes a group signal the child itself sends a fact
    this run reports rather than a fact the run disappears into: row 7 of
    acceptance02 raises OSError by design in the POSIX branch of
    terminate_owned_tree, and without the session that death reaches the loop
    driving it. A child the timeout kills reports 128 + SIGKILL, which is
    returned as-is, because a bounded run is a fact worth carrying.
    """
    environment = env or run_env()
    log.parent.mkdir(parents=True, exist_ok=True)
    if junitxml is not None:
        junitxml.parent.mkdir(parents=True, exist_ok=True)
    command = [
        *_which_or("setsid", environment, []),
        *_which_or("timeout", environment, []),
        *([f"--signal=KILL", str(timeout)] if _have("timeout", environment) else []),
        *argv,
    ]
    with log.open("wb") as handle:
        try:
            done = subprocess.run(
                command, stdout=handle, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, cwd=str(REPO_ROOT), env=environment,
                timeout=timeout + 120,
            )
            return done.returncode
        except subprocess.TimeoutExpired:
            return 128 + signal.SIGKILL


def pytest_argv(targets: list[str], junitxml: Path) -> list[str]:
    """The pytest invocation for a set of targets.

    `-q` is deliberately not here. pytest writes its count summary to a terminal
    writer that is not stdout when stdout is a file, and `-q` is what suppresses
    it, so a log of a `-q` run holds progress dots and nothing a reader can
    quote. Without it each log ends in a summary line. The report is the record
    either way; this only means the log is readable too.

    `--tb=line` so a log holds the assertion line rather than a traceback per
    test, and `no:cacheprovider` so a run does not write into a tree it is only
    measuring.
    """
    return [
        "-m", "pytest", *targets,
        "-p", "no:cacheprovider", "--tb=line", "--color=no",
        f"--junitxml={junitxml}",
    ]


def test_files(stems: list[str]) -> list[Path]:
    """The `tests/test_<stem>.py` files named by stems, or every test file.

    A stem is a file's name without `test_` and without `.py`, which is the
    shape a reader sees in a result table. Both halves are spelled out rather
    than assumed, because a caller that passed `test_claims` got
    `tests/test_test_claims.py` and was told a file was missing when the
    caller's spelling was what was wrong.
    """
    if not stems:
        return sorted((REPO_ROOT / "tests").glob("test_*.py"))
    missing = [
        stem for stem in stems
        if not (REPO_ROOT / "tests" / f"test_{stem}.py").is_file()
    ]
    if missing:
        raise SystemExit(
            "no such test file: " + ", ".join(f"tests/test_{s}.py" for s in missing)
        )
    return [REPO_ROOT / "tests" / f"test_{stem}.py" for stem in stems]


def run_test_file(path: Path, outdir: Path) -> Outcome:
    """One test file, in its own process, with its own report."""
    junit = outdir / f"{path.stem}.xml"
    log = outdir / f"{path.stem}.txt"
    relative = str(path.relative_to(REPO_ROOT))
    status = run_isolated([str(posix_python()), *pytest_argv([relative], junit)],
                          log, timeout=FILE_TIMEOUT, junitxml=junit)
    return Outcome(path.stem, status, read_report(junit), log=log)


# ------------------------------------------------------------------ reporting


def print_environment() -> None:
    """Print the environment the run is about to use, and where output goes.

    The PATH is the receipt for the eight console tests, so it is printed rather
    than assumed: a run whose PATH lost `~/.local/bin` skips eight tests and
    says nothing about it in the summary, which is the hole this closes.
    """
    env = run_env()
    print("== environment ==")
    print(f"  repo:    {REPO_ROOT}")
    print(f"  venv:    {posix_python()}")
    print(f"  reports: {reports_dir()}")
    print(f"  python:  {_which('python', env)}")
    print(f"  claude:  {_which('claude', env)}")
    print(f"  node:    {_which('node', env)}")
    print(f"  GIT_DIR: {env.get('GIT_DIR', '<unset>')}")
    print()


def print_run(run: Run) -> int:
    """Print the run and return its status.

    The counts and the status are printed together so a reader who sees
    `0 failed` and a reader who sees `exit 1` are looking at the same numbers.
    `VERDICT` is last and explicit, because the failures this file exists to
    prevent are all cases where a reader stopped at the counts.

    pytest statuses are listed because the status is the verdict, and a reader
    who has to infer it from a count is doing the inference this file exists to
    do for them. A file the timeout killed shows as 137, which is a bounded
    fact rather than a test failure, and the two are worth telling apart.
    """
    tests, failures, errors, skipped, silent = run.totals()
    status = run.status

    print("== per file ==")
    for outcome in run.outcomes:
        if outcome.counts is None:
            detail = "NO REPORT, so this file did not finish"
        else:
            t, f, e, s = outcome.counts
            detail = f"tests={t:<4} fail={f:<3} err={e:<3} skip={s}"
        print(f"  {outcome.name:<34} exit={outcome.status:<4} {detail}")
        if outcome.note:
            print(f"  {'':<34} {outcome.note}")

    print()
    print("=" * 68)
    if not run.outcomes:
        print(f"  {run.label}: no test files matched, so nothing ran")
    else:
        print(f"  {run.label}: {len(run.outcomes)} files")
    if silent:
        print(f"  files that produced no report: {silent}")
    print(f"  TOTAL  {tests} tests: {failures} failed, {errors} errors, "
          f"{skipped} skipped")
    print(f"  PASSED {tests - failures - errors - skipped}")
    print(f"  pytest statuses: {_statuses(run.outcomes)}")
    if failing := run.failures():
        print(f"  NOT PASSING: {', '.join(o.name for o in failing)}")
    print(f"  VERDICT: {'PASS' if status == 0 else 'FAIL'}  (exit {status})")
    print("=" * 68)
    return status


def _statuses(outcomes: list[Outcome]) -> str:
    seen: dict[int, int] = {}
    for outcome in outcomes:
        seen[outcome.status] = seen.get(outcome.status, 0) + 1
    return ", ".join(f"{code} x{count}" for code, count in sorted(seen.items()))


def tail(path: Path, lines: int) -> str:
    """The last `lines` lines of a file, or a note that there are none.

    An empty file is the death this file is about, so an empty log says so
    rather than printing nothing the reader has to interpret.
    """
    if not path.is_file():
        return f"(no such file: {path})"
    if path.stat().st_size == 0:
        return f"({path.name} is empty, so the run produced no output at all)"
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    )


# ---------------------------------------------------------------- subcommands


def cmd_suite(args: argparse.Namespace) -> int:
    """Every test file, one process each, one total.

    Per-file rather than one process because a whole-suite run in a single pytest
    process was killed repeatedly on this host with no traceback and no summary.
    Per-file is the shape that completes, and `single` is where the question of
    whether the suite can run in one process at all gets measured rather than
    assumed either way.
    """
    print_environment()
    require_venv()
    outdir = reports_dir()
    outdir.mkdir(parents=True, exist_ok=True)
    files = test_files(args.stems)
    write_manifest(outdir, [path.stem for path in files])

    run = Run("suite")
    for path in files:
        outcome = run_test_file(path, outdir)
        run.add(outcome)
        print(f"  {outcome.name:<34} exit={outcome.status}", flush=True)
    return print_run(run)


def cmd_per_file(args: argparse.Namespace) -> int:
    """One named file, run in its own process.

    Distinct from `suite` with a stem because a red file is a thing to read
    rather than a total to add up, so this prints the log with it.
    """
    print_environment()
    require_venv()
    outdir = reports_dir()
    files = test_files(args.stems)
    write_manifest(outdir, [path.stem for path in files])
    run = Run(f"per-file {', '.join(args.stems)}")
    for path in files:
        run.add(run_test_file(path, outdir))
    status = print_run(run)
    for outcome in run.failures():
        if outcome.log:
            print(f"\n== last 40 lines of {outcome.log} ==")
            print(tail(outcome.log, 40))
    return status


def cmd_why(args: argparse.Namespace) -> int:
    """One file, with where each failure came from.

    A dot line says how many tests failed and not why, and the reason is the
    part worth reading. The assertion line and the named failures are enough to
    classify without dumping a traceback per test.

    pytest's own status is propagated rather than decided separately. 0 is the
    one answer that means nothing failed, and everything else is the gate going
    red, which includes 5 for a file that collected nothing.
    """
    print_environment()
    require_venv()
    outdir = reports_dir()
    path = test_files([args.stem])[0]
    junit = outdir / f"{path.stem}.xml"
    log = outdir / f"why-{path.stem}.txt"
    argv = [*pytest_argv([str(path.relative_to(REPO_ROOT))], junit), "-rf"]
    status = run_isolated([str(posix_python()), *argv], log,
                          timeout=FILE_TIMEOUT, junitxml=junit)
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""

    print(f"=== tests/{path.name}  exit={status} ===")
    print("\n--- where each failure came from ---")
    for line in re.findall(r"^/\S+:\d+: .*$", text, re.MULTILINE)[:20]:
        print(f"  {line}")
    print("\n--- the named failures ---")
    named = re.findall(r"^(?:FAILED|ERROR) .*$", text, re.MULTILINE)
    for line in named[:20]:
        print(f"  {line.split(' - ')[0]}")
    if not named:
        print("  (none)")

    run = Run(f"why {args.stem}")
    run.add(Outcome(path.stem, status, read_report(junit)))
    print()
    return print_run(run)


def cmd_acceptance(args: argparse.Namespace) -> int:
    """An acceptance script, with the two streams captured separately.

    acceptance02.py drives real child interpreters over a 300-second budget on
    some rows, and a run that dies produces no output at all on this host.
    Writing the streams to files and reporting the byte counts makes a silent
    death a fact rather than an absence.
    """
    print_environment()
    python = require_venv()
    script = REPO_ROOT / "scripts" / args.script
    if not script.is_file():
        raise SystemExit(f"no such acceptance script: scripts/{args.script}")
    outdir = REPO_ROOT / "tmp"
    outdir.mkdir(parents=True, exist_ok=True)
    out, err = outdir / f"{script.stem}.out", outdir / f"{script.stem}.err"

    print(f"== running scripts/{args.script} {' '.join(args.rest)} ==")
    with out.open("wb") as o, err.open("wb") as e:
        try:
            status = subprocess.run(
                [str(python), "-u", str(script.relative_to(REPO_ROOT)), *args.rest],
                stdout=o, stderr=e, stdin=subprocess.DEVNULL,
                cwd=str(REPO_ROOT), env=run_env(),
            ).returncode
        except OSError as exc:
            print(f"could not start the acceptance script: {exc}", file=sys.stderr)
            return 1

    print(f"exit status: {status}")
    print(f"\n== stdout (last 60 lines of {out}) ==")
    print(tail(out, 60))
    print(f"\n== stderr (last 40 lines of {err}) ==")
    print(tail(err, 40))
    print(f"\nstdout bytes: {out.stat().st_size}   "
          f"stderr bytes: {err.stat().st_size}")
    print(f"VERDICT: {'PASS' if status == 0 else 'FAIL'}  (exit {status})")
    return status


def cmd_rows(args: argparse.Namespace) -> int:
    """Each acceptance02 row in its own process, one verdict each.

    The full run on this host dies with no output at all, which is a fact about
    the harness rather than about any one row. Running each row in isolation
    turns that silence into a per-row verdict, and a row that kills its own
    process is then visible instead of taking the rest of the table with it.

    A row is judged by its exit status. acceptance02.py exits 0 only when no row
    failed and none was left unreached, and it prints its own `[ PASS ]`,
    `[ FAIL ]` and `[ SKIP ]` lines, so the status and the printed verdict are
    two readings of one fact rather than one being parsed out of the other. A
    row that printed no verdict line is a row that did not finish, and the
    status would say so too.
    """
    print_environment()
    python = require_venv()
    outdir = REPO_ROOT / "tmp"
    outdir.mkdir(parents=True, exist_ok=True)

    run = Run("acceptance02 rows")
    for number in (args.rows or list(range(1, 15))):
        out = outdir / f"acc02-row{number}.out"
        err = outdir / f"acc02-row{number}.err"
        status = run_isolated(
            [str(python), "-u", "scripts/acceptance02.py", "--only", str(number)],
            out, timeout=ROW_TIMEOUT,
        )
        text = out.read_text(encoding="utf-8", errors="replace") if out.is_file() else ""
        verdicts = re.findall(r"\[\s*(PASS|FAIL|SKIP)\s*\] row \d+", text)
        summary = next(
            (line.strip() for line in reversed(text.splitlines())
             if re.match(r"^\d+/\d+ acceptance rows pass", line.strip())),
            "",
        )
        # acceptance02.py writes no junit report, so a row's counts are
        # constructed rather than read: one test, the row itself, which either
        # passed or did not. Giving a failed row a real (1, 1, 0, 0) rather
        # than None is what keeps it from being described as a file that "did
        # not finish" -- a row that ran and failed is not a death, and the two
        # need different words. A row with no verdict line produced no run at
        # all, so that one gets None and reads as the death it is.
        if not verdicts:
            counts = None
        elif status == 0:
            counts = (1, 0, 0, 0)
        else:
            counts = (1, 1, 0, 0)
        run.add(Outcome(
            f"row {number}", status, counts,
            note=verdicts[0] if verdicts else "NO VERDICT LINE, so this row did not finish",
        ))
        print(f"row {number:<3} exit={status:<4} "
              f"{verdicts[0] if verdicts else 'NO VERDICT LINE'}", flush=True)
        if summary:
            print(f"        {summary}")
        if err.is_file() and err.stat().st_size:
            head = " ".join(err.read_text(errors="replace").splitlines()[:3])
            print(f"        stderr: {head[:200]}")
    return print_run(run)


def cmd_doctor(args: argparse.Namespace) -> int:
    """A per-file run watched for memory pressure, then a verdict on why.

    A whole-suite run of this suite dies part-way with no traceback, and three
    explanations fit that shape with different fixes: the OOM killer, the WSL VM
    being reclaimed under memory pressure, or something in the tests signalling
    its own process group. The first two leave a record and the third does not,
    so this looks for the record rather than guessing.

    Both answers are bad news. A run that died is a broken run, and a run that
    completed with failures in it is not a pass either, so a clean bill of
    health is the one thing this cannot report.
    """
    print_environment()
    require_venv()
    outdir = REPO_ROOT / "tmp"
    outdir.mkdir(parents=True, exist_ok=True)

    print("== host facts ==")
    for label, value in host_facts().items():
        print(f"  {label:<14} {value}")
    print()

    watcher = MemoryWatcher()
    watcher.start()
    run = Run("doctor")
    died = ""
    files = test_files(args.stems)
    write_manifest(outdir, [path.stem for path in files])
    try:
        for path in files:
            outcome = run_test_file(path, outdir)
            run.add(outcome)
            if outcome.counts is None and outcome.status != NO_TESTS_COLLECTED:
                died = f"{outcome.name} (exit {outcome.status}, no output at all)"
                print(f"DIED on {outcome.name} with exit {outcome.status} and no output")
                break
            # A file reporting a failure is what this run was started to
            # diagnose, and the files after it still have something to say, so
            # the loop does not stop for one.
            marker = "FAILED" if outcome.failed else "clean"
            print(f"  {outcome.name:<34} exit={outcome.status} {marker}", flush=True)
    finally:
        peak = watcher.stop()

    print("\n== what the watcher saw ==")
    print(f"  MemAvailable  {peak.available_mib} MiB was the lowest seen")
    print(f"  process RSS   {peak.rss_mib} MiB was the highest single process")
    for label, value in host_facts().items():
        print(f"  {label:<14} {value}")

    print("\n== did the OOM killer fire? ==")
    records = oom_records()
    if records:
        for line in records:
            print(f"  {line}")
    else:
        print("  No OOM record in dmesg, in the cgroup's memory.events, or in")
        print("  /proc/meminfo. All three were read, because a negative answer")
        print("  is only worth anything if the places that would say yes were read.")

    print("\n== verdict ==")
    if died:
        print(f"  The run died at: {died}")
    else:
        print("  The run completed every file.")
    if failing := run.failures():
        print(f"  Files reporting a failure or an error: "
              f"{', '.join(o.name for o in failing)}")
    if records:
        print("  The OOM killer fired, so memory pressure is the cause.")
    else:
        print("  Memory was not the cause, so the remaining explanations are that")
        print("  something signalled this process group or the WSL VM was reclaimed")
        print("  without a kernel record. Not established either way.")
    # The exit code is the verdict and it has to agree with the words above.
    # print_run's code is that verdict already, and restating it would be a
    # second authority for one fact.
    return print_run(run)


def cmd_totals(args: argparse.Namespace) -> int:
    """Total the junit reports of a completed run.

    The reports are the record rather than the console output: a redirected
    `pytest -q` keeps only the progress dots, because pytest writes its summary
    to a terminal writer that is not stdout. junitxml is a file, so it survives
    both the redirect and a killed run, and summing it is the only way to get a
    total a reader can trust.

    The run's own manifest is read too, and that is the part that closes the
    last false green here. A report is written by a process that finished, so a
    file that died leaves no report, and the reports alone cannot tell a file
    that was never run from a file that was run and died. Measured before the
    manifest existed: a tree where one file called `os._exit(9)` produced two
    reports, and this command summed them and reported PASS. A name in the
    manifest with no report beside it is now a death, and a death is a failure.

    A report that could not be parsed, and a directory with no reports at all,
    are failures for the same reason: each describes a run that did not finish
    being described, and neither is a clean bill of health.
    """
    directory = Path(args.directory) if args.directory else reports_dir()
    print(f"reading {directory}\n")
    reports = sorted(directory.glob("*.xml"))
    if not reports:
        print(f"no junit reports in {directory}, so no run is recorded there.")
        print("VERDICT: FAIL  (exit 1)")
        return 1

    run = Run(f"totals from {directory}")
    present: set[str] = set()
    print(f"{'file':<34}{'tests':>7}{'fail+err':>10}{'skip':>7}  note")
    for path in reports:
        present.add(path.stem)
        counts = read_report(path)
        if counts is None:
            run.add(Outcome(path.stem, 1, None, "report could not be read"))
            print(f"{path.stem:<34}{'-':>7}{'-':>10}{'-':>7}  UNREADABLE")
            continue
        # 0 because the report itself is the verdict: its own failures decide.
        # The child's status is not in a report read long after it exited, and
        # inventing one would be a verdict computed from output, which is the
        # failure this whole tree of scripts existed to end.
        run.add(Outcome(path.stem, 0, counts))
        tests, failures, errors, skipped = counts
        print(f"{path.stem:<34}{tests:>7}{failures + errors:>10}{skipped:>7}")

    attempted = read_manifest(directory)
    if attempted is None:
        print("\nno manifest beside the reports, so this cannot tell a file that "
              "died\nfrom a file that was never run. Run `suite` or `per-file` to "
              "record one.")
        run.add(Outcome("(attempt list unknown)", 1, None,
                        "the run recorded no manifest, so a missing report is "
                        "indistinguishable from a file that never ran"))
    else:
        for name in attempted:
            if name in present:
                continue
            # A named file with no report is a death. It contributed no tests,
            # so a total on its own cannot see it, which is the whole reason
            # the manifest exists. The table row already says it, so the note
            # here would be the same sentence twice.
            run.add(Outcome(name, 1, None))
            print(f"{name:<34}{'-':>7}{'-':>10}{'-':>7}  NO REPORT, so it died")
        for name in sorted(present - set(attempted)):
            run.add(Outcome(name, 1, None,
                            "has a report but is not in the run's manifest"))
    print()
    return print_run(run)


def cmd_single(_args: argparse.Namespace) -> int:
    """The whole suite in one pytest process, watched while it runs.

    This measures whether the suite completes at all, which the per-file loop
    cannot answer. Per-file runs complete and a single-process run does not, and
    the cause was never established, so this is a measurement of the failure
    rather than a workaround.

    The watcher samples progress so a death is visible as the series stopping
    rather than as an absence, and the junit report is the only thing that says
    the run finished writing.
    """
    print_environment()
    python = require_venv()
    outdir = Path.home() / "vkit-posix-reports" / "single-process"
    if outdir.is_dir():
        for stale in outdir.iterdir():
            stale.unlink()
    outdir.mkdir(parents=True, exist_ok=True)
    junit, log = outdir / "all.xml", outdir / "all.txt"

    print("== before ==")
    print(f"  processes:    {proc_count()}")
    print(f"  MemAvailable: {mem_available_mib()} MiB\n")

    started = time.monotonic()
    with log.open("wb") as handle:
        child = subprocess.Popen(
            [str(python), "-u", "-m", "pytest", "tests/",
             "-p", "no:cacheprovider", "--tb=no", "--color=no", "-rf",
             f"--junitxml={junit}"],
            stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            cwd=str(REPO_ROOT), env=run_env(), start_new_session=True,
        )
    print(f"started the whole suite as one process: pid {child.pid}\n")

    farthest = 0
    while child.poll() is None:
        size = log.stat().st_size if log.is_file() else 0
        farthest = max(farthest, progress_marks(log))
        print(f"  t+{int(time.monotonic() - started):>4}s  log={size:<8} bytes  "
              f"progress={farthest:<5} marks  procs={proc_count()}", flush=True)
        time.sleep(20)

    print(f"\n== what happened ==")
    print(f"  exit status:   {child.returncode}")
    print(f"  elapsed:       {int(time.monotonic() - started)}s")
    print(f"  marks written: {farthest}")
    print(f"  log bytes:     {log.stat().st_size if log.is_file() else 0}")
    print(f"  junit report:  {'present' if junit.is_file() else 'ABSENT'}")
    print("\n== did it leave anything that explains its own death? ==")
    print(f"  survivors named in the log: {survivors()}")
    print(f"  cores: {core_count()}")
    print(f"  oom-kill lines in dmesg: {len(oom_records())}")
    print(f"  MemAvailable now: {mem_available_mib()} MiB")
    print("\n== the last 400 bytes it wrote ==")
    print(tail(log, 400))

    if not junit.is_file():
        print("\n== verdict ==")
        print("  It did NOT complete: no junit report, so pytest never finished")
        print("  writing. The cause is not established. A report is the only thing")
        print("  that says the run finished, so this exits nonzero even though it")
        print("  got as far as printing a verdict.")
        print("  VERDICT: FAIL  (exit 1)")
        return 1

    run = Run("single process")
    run.add(Outcome("all", child.returncode or 0, read_report(junit)))
    status = print_run(run)
    if status == 0:
        print("\n  The total in that report covers a single-process run and is no\n"
              "  longer unverified.")
    else:
        print("\n  It COMPLETED but did not pass. See the counts above.")
    return status


def cmd_run(args: argparse.Namespace) -> int:
    """One command against the venv, with the environment this tree needs.

    Without this a run falls back to the system python3, which has no pytest and
    no mcp. The venv's bin goes on PATH as well, because the example manifests
    and several acceptance rows name the interpreter `python` and this host
    ships only `python3`.

    A leading `-m pytest` is added unless the caller named a runnable script.
    Without it, `run tests/test_recover.py` is not a pytest invocation at all:
    Python tries to import the test file as a module, pytest never runs, and the
    command exits 0 having written nothing, which reads as a passing suite.
    scripts/ holds pytest files AND plain scripts, so the target decides, and a
    bare word with no path and no slash is a script under scripts/.
    """
    print_environment()
    python = require_venv()
    rest = _resolve_target(list(args.rest))
    if not rest:
        return 0
    # Inherited, not captured: this subcommand is a window onto another command,
    # and a reader watching this one wants the other's output as it happens.
    return subprocess.run([str(python), *rest], cwd=str(REPO_ROOT),
                          env=run_env()).returncode


def _resolve_target(rest: list[str]) -> list[str]:
    """The command to run, with `-m pytest` or a `scripts/` path supplied when
    the caller named a target and not a mode.

    Three cases, and each is one fact about the first token.

        a leading `-`      the caller is describing a pytest invocation and
                          knows what they are doing. Pass it through.
        a bare word, or a
        path under tests/  a pytest target. `tests/` is the suite and
                          `tests/test_x.py` is a file in it, so both get
                          `-m pytest`. A plain script elsewhere is a script.
        any other path     a script to run. `scripts/` holds pytest files AND
                          plain scripts, and a plain script handed to pytest
                          reports "no tests ran" and exits 5, which is a worse
                          answer than running it. A bare word is spelled out as
                          a path, because `python -m acceptance02` does not
                          resolve (scripts/ is not on sys.path) and the
                          alternative is an error that reads like a missing
                          dependency.
    """
    if not rest:
        return ["-m", "pytest", "tests/", "-q"]
    first = rest[0]
    if first.startswith("-"):
        return rest
    is_pytest_target = first.startswith("tests/") or first.startswith("test_")
    if is_pytest_target:
        return ["-m", "pytest", *rest]
    if "/" not in first:
        name = first if first.endswith(".py") else f"{first}.py"
        if (REPO_ROOT / "scripts" / name).is_file():
            rest[0] = f"scripts/{name}"
    return rest


# ---------------------------------------------------------------- measurement


class MemoryWatcher:
    """Samples memory while a run happens, so the numbers exist if it dies.

    A thread rather than a child process, because the watcher has to keep
    sampling through the death of the thing it is watching, and a child of the
    dying process would go with it.
    """

    def __init__(self, interval: float = 2.0) -> None:
        self.interval = interval
        self.available_mib = 0
        self.rss_mib = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        def sample() -> None:
            while not self._stop.is_set():
                self.available_mib = max(self.available_mib, mem_available_mib())
                self.rss_mib = max(self.rss_mib, peak_rss_mib())
                self._stop.wait(self.interval)

        self._thread = threading.Thread(target=sample, daemon=True)
        self._thread.start()

    def stop(self) -> "MemoryWatcher":
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval * 2)
        return self


def read_int(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def mem_available_mib() -> int:
    """MemAvailable in MiB, or 0 on a host whose /proc does not say."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def peak_rss_mib() -> int:
    """The largest single-process RSS on the host, in MiB."""
    try:
        done = subprocess.run(["ps", "-eo", "rss="], capture_output=True,
                              text=True, timeout=10)
        values = [int(line) for line in done.stdout.split() if line.strip()]
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0
    return max(values, default=0) // 1024


def proc_count() -> int:
    try:
        return sum(1 for entry in Path("/proc").iterdir() if entry.name.isdigit())
    except OSError:
        return 0


def cgroup(name: str) -> str | None:
    try:
        value = Path("/sys/fs/cgroup", name).read_text().strip()
    except OSError:
        return None
    return "unlimited" if value == "max" else value


def host_facts() -> dict[str, str]:
    """The host numbers the doctor reports, read fresh on each call."""
    facts = {
        "cgroup max": cgroup("memory.max") or "unknown",
        "cgroup peak": cgroup("memory.peak") or "unknown",
        "pid_max": str(read_int("/proc/sys/kernel/pid_max") or "unknown"),
        "threads-max": str(read_int("/proc/sys/kernel/threads-max") or "unknown"),
        "nproc": str(os.cpu_count() or "unknown"),
    }
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("Swap:"):
                total, used = (int(field) for field in line.split()[1:3])
                facts["swap"] = f"{total // 1024} MiB total, {used // 1024} used"
                break
    except (OSError, ValueError, IndexError):
        pass
    return facts


def oom_records() -> list[str]:
    """The kernel's own record of an OOM kill, from any place it lands.

    dmesg on most hosts, memory.events under a cgroup, and MemOomKill in
    /proc/meminfo on a newer kernel. All three are read, because the doctor is
    asking whether memory was the cause and a negative answer is worth nothing
    unless the places that would say yes were actually read.
    """
    found: list[str] = []
    try:
        done = subprocess.run(["dmesg"], capture_output=True, text=True, timeout=20)
        for line in done.stdout.splitlines():
            if re.search(r"out of memory|oom-kill|killed process", line, re.I):
                found.append(line.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    for source, pattern in (
        ("/sys/fs/cgroup/memory.events", r"^(?:[a-z_]+_)?oom(?:_kill)?\s+([1-9]\d*)$"),
        ("/proc/meminfo", r"^MemOomKill:\s+([1-9]\d*)$"),
    ):
        try:
            for line in Path(source).read_text().splitlines():
                if re.match(pattern, line):
                    found.append(f"{source}: {line.strip()}")
        except (OSError, ValueError):
            pass
    return found[-5:]


def progress_marks(log: Path) -> int:
    """How many progress marks pytest has written so far.

    A count and not a verdict. The per-file verdicts come from the reports; this
    is here so a death is visible as the series stopping rather than as an
    absence.
    """
    if not log.is_file():
        return 0
    try:
        text = log.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return 0
    return sum(text.count(mark) for mark in ".sFExX")


def survivors() -> int:
    try:
        done = subprocess.run(["pgrep", "-fc", "pytest tests/"], capture_output=True,
                              text=True, timeout=10)
        return int(done.stdout.strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def core_count() -> int:
    try:
        return sum(1 for _ in Path("/var/lib/systemd/coredump").iterdir())
    except OSError:
        return 0


# ---------------------------------------------------------------------- wiring


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="posix_harness.py",
        description="One entry point for a POSIX verification run. Every "
                    "subcommand returns the exit status of the process it ran; "
                    "no verdict is parsed out of output.",
    )
    sub = parser.add_subparsers(dest="command")

    suite = sub.add_parser("suite", help="every test file, one process each")
    suite.add_argument("stems", nargs="*", help="file stems; every test file if none")
    suite.set_defaults(run=cmd_suite)

    one = sub.add_parser("per-file", help="one or more named test files, logs and all")
    one.add_argument("stems", nargs="+", help="file stems, without test_ or .py")
    one.set_defaults(run=cmd_per_file)

    why = sub.add_parser("why", help="one file, with where each failure came from")
    why.add_argument("stem", help="file stem, without test_ or .py")
    why.set_defaults(run=cmd_why)

    acceptance = sub.add_parser("acceptance", help="an acceptance script, streams captured")
    acceptance.add_argument("script", help="a name under scripts/, e.g. acceptance02.py")
    acceptance.add_argument("rest", nargs=argparse.REMAINDER, help="arguments for it")
    acceptance.set_defaults(run=cmd_acceptance)

    rows = sub.add_parser("rows", help="each acceptance02 row in its own process")
    rows.add_argument("rows", nargs="*", type=int, help="row numbers; rows 1-14 if none")
    rows.set_defaults(run=cmd_rows)

    doctor = sub.add_parser("doctor", help="a per-file run watched for memory pressure")
    doctor.add_argument("stems", nargs="*", help="file stems; every test file if none")
    doctor.set_defaults(run=cmd_doctor)

    totals = sub.add_parser("totals", help="total the junit reports of a past run")
    totals.add_argument("directory", nargs="?", help="a report directory; the default if absent")
    totals.set_defaults(run=cmd_totals)

    single = sub.add_parser("single", help="the whole suite in one pytest process")
    single.set_defaults(run=cmd_single)

    run = sub.add_parser(
        "run",
        help="one command against the venv, environment set up",
        description="Everything after `run` is the command, passed to the venv "
                    "interpreter unchanged. A leading `-m pytest` is added "
                    "unless the caller named a runnable script, so this parses "
                    "everything itself rather than handing it to argparse: a "
                    "leading `-m` is the common case and argparse would reject "
                    "it as an unknown option.",
        add_help=False,
    )
    run.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    run.set_defaults(run=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # `run` swallows everything after its own name, so a leading `-m` reaches
    # the command rather than argparse. Every other subcommand parses normally.
    if arguments and arguments[0] == "run":
        return cmd_run(argparse.Namespace(rest=arguments[1:]))
    parser = build_parser()
    args = parser.parse_args(arguments)
    if args.command is None:
        # A bare invocation is the whole suite, which is what a reader would
        # assume and what a gate wants.
        args = parser.parse_args(["suite"])
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
