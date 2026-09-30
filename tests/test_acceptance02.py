"""The acceptance harness is itself under test, and these tests are the only
reason anyone can trust a green run of it.

An acceptance script that cannot fail is a document. This file establishes three
things a reviewer would otherwise have to take on faith:

  * every entry of the plan's acceptance table has a row, and every row carries
    the plan's own words, so a table row cannot be quietly dropped;
  * a row forced to fail, a row that raises, and a row that never reaches a
    verdict each make the harness exit non-zero, and a row that observes its
    condition exits zero, so the exit code means something;
  * a verdict is a function of the observed condition and nothing else, and a
    verdict with no evidence attached is refused.

The failure tests run the whole script in a subprocess with one row function
replaced, rather than monkeypatching a predicate. Replacing a row leaves the
harness's own dispatch, counting, and reporting in place, so what is exercised
is the script a reviewer runs.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "acceptance02.py"
PLAN = REPO_ROOT / "plans" / "02-ownership-and-runs.md"

sys.path.insert(0, str(SCRIPT.parent))

import acceptance02  # noqa: E402

#: The row the forced-behaviour tests swap. Row 2 is the cheapest row in the
#: table, so a test that does not actually replace anything stays fast.
FORCED_ROW = 2


def plan_table() -> list[str]:
    """The plan's acceptance table, read from the plan rather than from a copy.

    Reading it here is what stops the harness and the plan from drifting apart
    unnoticed: a row added to the plan and not to the harness fails the count.
    """
    exercises = []
    for line in PLAN.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| "):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] == "Exercise":
            continue
        if all(cell and set(cell) <= {"-", " ", ":"} for cell in cells):
            continue  # the header rule, which is not an exercise
        exercises.append(cells[0])
    return exercises


def run_forced(substitution: str) -> subprocess.CompletedProcess[str]:
    """Run the harness in a subprocess with one row's behaviour replaced.

    The path is absolute because the child runs with `cwd` at the repository
    root, and a relative one would resolve against a different directory than
    the one this module was imported from.
    """
    program = (
        "import sys\n"
        f"sys.path.insert(0, {str(SCRIPT.parent)!r})\n"
        "import acceptance02\n"
        f"{substitution}\n"
        # argparse wants strings; an int here fails in the child and the test then
        # asserts on an empty stdout, which fails for the wrong reason.
        f"sys.exit(acceptance02.main(['--only', '{FORCED_ROW}']))\n"
    )
    return subprocess.run([sys.executable, "-c", program], capture_output=True,
                          text=True, timeout=300, cwd=REPO_ROOT)


def replace_row(body: str) -> str:
    return f"acceptance02.ROW_FUNCTIONS[{FORCED_ROW}] = {body}"


# --- the table and the harness agree ----------------------------------------


def test_the_plan_table_reads_as_fourteen_exercises() -> None:
    """If this fails the table is not being read, and the tests below prove nothing."""
    exercises = plan_table()
    assert len(exercises) == 14, f"expected 14 acceptance rows, read {exercises}"
    assert exercises[0].startswith("100 competing claim attempts")
    assert exercises[-1] == "Stateful operation sequences"


def test_every_acceptance_table_row_has_a_harness_row() -> None:
    """One row per table entry. A missing one is a row nobody can fail."""
    assert len(plan_table()) == len(acceptance02.ROWS) == 14
    assert sorted(acceptance02.ROWS) == list(range(1, 15)), (
        "the harness must number its rows 1 to 14 with no gaps"
    )
    assert sorted(acceptance02.ROW_FUNCTIONS) == sorted(acceptance02.ROWS), (
        "every numbered row must name a row function, or a table entry is unmeasured"
    )
    assert set(acceptance02.ROW_TITLES) == set(acceptance02.ROWS), (
        "every row must carry a title, or a reader cannot tell what it measured"
    )


def test_the_plan_text_is_carried_verbatim_into_every_title() -> None:
    """The titles are the plan's exercise column, not a paraphrase of it."""
    for number, exercise in enumerate(plan_table(), start=1):
        assert acceptance02.ROW_TITLES[number] == exercise, (
            f"row {number} is titled {acceptance02.ROW_TITLES[number]!r} but the plan "
            f"says {exercise!r}"
        )


def test_every_row_function_takes_no_arguments() -> None:
    """A row drives the whole exercise itself, so it takes nothing from the driver."""
    for number, function in acceptance02.ROW_FUNCTIONS.items():
        assert callable(function), f"row {number} is not callable"
        assert function.__code__.co_argcount == 0, (
            f"row {number} takes arguments, so its outcome depends on its caller"
        )


# --- the harness can fail, and can pass -------------------------------------


def test_a_forced_failure_makes_the_harness_exit_non_zero() -> None:
    """The gate: a row made to fail must take the exit code with it."""
    done = run_forced(replace_row(
        "lambda: acceptance02.observed(acceptance02._row(%d), False, "
        "'forced to fail by the test')" % FORCED_ROW
    ))
    assert done.returncode == 1, (
        f"the harness exited {done.returncode} with a row forced to fail; "
        f"an acceptance script that cannot fail is a document"
    )
    assert "[ FAIL ] row 2" in done.stdout
    assert "forced to fail by the test" in done.stdout
    assert "1/1 acceptance rows pass" not in done.stdout


def test_a_forced_pass_makes_the_harness_exit_zero() -> None:
    """The other direction, without which the test above is satisfied by a
    harness that always exits non-zero and therefore proves nothing."""
    done = run_forced(replace_row(
        "lambda: acceptance02.observed(acceptance02._row(%d), True, "
        "'forced to pass by the test')" % FORCED_ROW
    ))
    assert done.returncode == 0, done.stdout
    assert "[ PASS ] row 2" in done.stdout
    assert "1/1 acceptance rows pass (0 skipped, 0 failed, 0 never ran)" in done.stdout


def test_a_row_that_never_reaches_a_verdict_makes_the_harness_exit_non_zero() -> None:
    """A row that returns without deciding is a failure, not a quiet pass."""
    done = run_forced(replace_row("lambda: None"))
    assert done.returncode == 1
    assert "[UNREACHED] row 2" in done.stdout
    assert "1 never ran" in done.stdout


def test_a_row_that_raises_is_reported_as_a_failure_and_not_a_crash() -> None:
    """A broken row must not stop the run, and must not read as a pass."""
    done = run_forced("def explode():\n    raise RuntimeError('the row blew up')\n"
                      + replace_row("explode"))
    assert done.returncode == 1
    assert "[ FAIL ] row 2" in done.stdout
    assert "RuntimeError: the row blew up" in done.stdout
    assert "Traceback" not in done.stderr, "the harness must absorb a broken row, not crash on it"


def test_a_skipped_row_is_reported_and_counts_as_a_skip() -> None:
    """SKIP is not a soft pass, and the summary has to say how many there were."""
    done = run_forced(replace_row(
        "lambda: acceptance02.unestablished(acceptance02._row(%d), "
        "'not established, for the test')" % FORCED_ROW
    ))
    assert "[ SKIP ] row 2" in done.stdout
    assert "0/1 acceptance rows pass (1 skipped" in done.stdout
    assert "not established, for the test" in done.stdout


def test_the_count_in_the_summary_matches_the_rows_that_were_printed() -> None:
    """A count that disagrees with the printed rows is a count a reader cannot use."""
    done = run_forced(replace_row(
        "lambda: acceptance02.observed(acceptance02._row(%d), False, 'forced')" % FORCED_ROW
    ))
    printed = [line for line in done.stdout.splitlines() if line.startswith("[")]
    assert len(printed) == 1
    assert "0/1 acceptance rows pass" in done.stdout
    assert "FAILED:" in done.stdout
    assert done.stdout.count("forced") >= 1


# --- the verdicts come from observed values ---------------------------------


def test_observed_passes_only_when_the_condition_is_true() -> None:
    """A verdict is a function of the condition, and of nothing else."""
    row = acceptance02.Row(99)
    acceptance02.observed(row, True, "held")
    assert (row.verdict, row.note) == (acceptance02.PASS, "held")
    acceptance02.observed(row, False, "not held")
    assert (row.verdict, row.note) == (acceptance02.FAIL, "not held")


def test_observed_refuses_a_verdict_with_no_note() -> None:
    """A note is the evidence. A verdict without one says nothing happened."""
    row = acceptance02.Row(99)
    for condition in (True, False):
        try:
            acceptance02.observed(row, condition, "")
        except ValueError as exc:
            assert "no note" in str(exc)
        else:
            raise AssertionError(f"a verdict with no note was accepted for {condition}")


def test_unestablished_is_a_skip_and_never_a_pass() -> None:
    row = acceptance02.Row(99)
    acceptance02.unestablished(row, "the build cannot do this")
    assert row.verdict == acceptance02.SKIP
    assert row.verdict != acceptance02.PASS
    assert "cannot do this" in row.note


def test_a_fresh_row_is_unreached_rather_than_passed() -> None:
    """The default verdict is the one that fails, so a lost row is loud."""
    assert acceptance02.Row(99).verdict == acceptance02.UNREACHED
    assert acceptance02.Row(99).verdict != acceptance02.PASS


def test_a_row_is_opened_once_per_number() -> None:
    """The driver and the row function both open the row; two objects would leave
    the verdict in one and the empty default in the reader."""
    acceptance02.RESULTS.clear()
    try:
        first = acceptance02._row(7)
        second = acceptance02._row(7)
        assert first is second
        assert len(acceptance02.RESULTS) == 1
    finally:
        acceptance02.RESULTS.clear()


# --- the harness cannot be pointed at another checkout ----------------------


def test_the_harness_puts_this_checkout_first_on_sys_path() -> None:
    """An editable install would otherwise let a row pass against other code."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'SRC = ROOT / "src"' in source
    assert "sys.path.insert(0, str(SRC))" in source, (
        "the harness must exercise its own src tree, not whichever vkit an "
        "editable install points at"
    )


def test_the_harness_names_this_checkout_as_its_source_root() -> None:
    """The path the harness inserts has to be the one next to the script."""
    assert acceptance02.SRC == REPO_ROOT / "src"
    assert acceptance02.EXAMPLE == REPO_ROOT / "examples" / "python-cli"
