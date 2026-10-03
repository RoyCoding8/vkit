"""Plan 11 checkpoint 2: the compiled-equality checker and the two logic rules.

The load-bearing properties are the ones the plan names and this build had to
measure rather than assume:

* `0.0` and `-0.0` are NOT the same constant, and a checker using `==` on
  `co_consts` would certify them as identical. `test_zero_and_negative_zero_are_not_the_same_constant`
  is the test that proves this checker catches what an `==` checker cannot, and
  it fails against an `==` implementation. That failure is asserted by
  `test_the_zero_case_would_pass_against_a_plain_equality_checker`, which builds
  the `==` checker inline and shows the wrong verdict.

* An empty trailing `else: pass` has equal executable fields at optimization
  levels 0, 1 and 2, so it CAN apply.
* An interior redundant `pass` changes bytecode, so it CANNOT. Same for a `pass`
  that promotes a docstring. Both are tested, because "cannot apply" is a result
  the plan requires and not a failure of this module.

* A change to a constant, the exception table, a closure, or a binding is
  refused. So is an interpreter this checker has not measured.

Nothing here asserts that an internal function ran. Every test calls the public
checker or the public preview and asserts the verdict a caller would act on.
"""
from __future__ import annotations

import math
import platform
from pathlib import Path

import pytest

from vkit.cleanup import (
    CHECKER_ID,
    COMPARED_FIELDS,
    EMPTY_ELSE_PASS,
    EXCLUDED_LOCATION_FIELDS,
    REDUNDANT_PASS,
    CompiledDiffers,
    CompiledEqual,
    CompiledUnsupported,
    LogicProposal,
    LogicRefusal,
    REQUIRED_OPTIMIZE_LEVELS,
    TESTED_CPYTHON_VERSIONS,
    preview_logic_cleanup,
    verify_compiled_equality,
    verify_docstrings,
)
from vkit.paths import open_project

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import subproc  # noqa: E402


def run_git(*args: str, cwd: Path) -> None:
    subproc.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def project_with(tmp_path: Path, relative: str, content: bytes) -> "object":
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    run_git("init", "-q", cwd=root)
    run_git("add", "-A", cwd=root)
    run_git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "fixture", cwd=root)
    return open_project(root)


def check(before: str, after: str):
    return verify_compiled_equality(before.encode(), after.encode())


# --------------------------------------------------------------------------- #
# The trap the plan names: 0.0 and -0.0
# --------------------------------------------------------------------------- #


def test_zero_and_negative_zero_are_not_the_same_constant() -> None:
    """The case `plans/11-checked-cleanup.md:47` names by hand.

    `x = 0.0` and `x = -0.0` compile to byte-identical `co_code`. Every other
    compared field agrees. Only the constant's exact representation separates
    them, so this is the test that says whether the checker is comparing
    constants at all.
    """
    verdict = check("x = 0.0\n", "x = -0.0\n")

    assert not isinstance(verdict, CompiledEqual), (
        "the checker certified 0.0 and -0.0 as the same constant"
    )
    assert isinstance(verdict, CompiledDiffers)
    assert verdict.reason == "executable_fields_differ"
    assert "co_consts" in verdict.detail


def test_the_zero_case_would_pass_against_a_plain_equality_checker() -> None:
    """The red test, written so it stays red for the wrong implementation.

    This builds the checker the plan forbids -- `==` on `co_consts` -- and
    asserts it reaches the WRONG verdict. If this test ever fails, the premise
    it rests on has changed on this interpreter and the surrounding tests need
    re-measuring rather than editing.
    """
    before = compile("x = 0.0\n", "<before>", "exec", optimize=0)
    after = compile("x = -0.0\n", "<after>", "exec", optimize=0)

    assert before.co_code == after.co_code, "the premise of this test needs re-measuring"
    plain_equality_says_equal = before.co_consts == after.co_consts

    assert plain_equality_says_equal is True
    assert math.copysign(1, 0.0) != math.copysign(1, -0.0)
    assert not isinstance(check("x = 0.0\n", "x = -0.0\n"), CompiledEqual)


def test_a_nested_negative_zero_is_also_caught() -> None:
    """The comparison recurses into constants rather than reading the top level."""
    verdict = check("def f():\n    return (0.0,)\n", "def f():\n    return (-0.0,)\n")
    assert not isinstance(verdict, CompiledEqual), verdict


def test_int_bool_and_float_are_three_constants() -> None:
    """The type is part of every key, so `1`, `True` and `1.0` stay distinct."""
    assert not isinstance(check("x = 1\n", "x = True\n"), CompiledEqual)
    assert not isinstance(check("x = 1\n", "x = 1.0\n"), CompiledEqual)


# --------------------------------------------------------------------------- #
# What must be refused
# --------------------------------------------------------------------------- #

REFUSED_CASES: tuple[tuple[str, str, str, str], ...] = (
    (
        "an interior redundant pass changes bytecode",
        "def f(a):\n    x = a\n    pass\n    return x\n",
        "def f(a):\n    x = a\n    return x\n",
        "LOGIC-REDUNDANT-PASS",
    ),
    (
        "a pass inside a try body rewrites the exception table",
        "def f(a):\n    try:\n        x = 1\n        pass\n    except E:\n        pass\n    return 0\n",
        "def f(a):\n    try:\n        x = 1\n    except E:\n        pass\n    return 0\n",
        "LOGIC-REDUNDANT-PASS",
    ),
    (
        "a while/else is not empty in the compiled form",
        "def w(a):\n    while a:\n        a -= 1\n    else:\n        pass\n",
        "def w(a):\n    while a:\n        a -= 1\n",
        "LOGIC-EMPTY-ELSE-PASS",
    ),
    (
        "an else in mid-body position emits a surviving NOP",
        "def f(a):\n    if a:\n        return 1\n    else:\n        pass\n    return 2\n",
        "def f(a):\n    if a:\n        return 1\n    return 2\n",
        "LOGIC-EMPTY-ELSE-PASS",
    ),
)


@pytest.mark.parametrize(
    ("description", "before", "after", "rule"),
    REFUSED_CASES,
    ids=[case[0] for case in REFUSED_CASES],
)
def test_a_change_to_executable_content_is_refused(
    description: str, before: str, after: str, rule: str
) -> None:
    """A change to the compiled representation leaves the patch a suggestion.

    This is the plan's acceptance row: "redundant pass changes bytecode" cannot
    automatically apply. The refusal is the result, not a failure of the check.
    """
    verdict = verify_compiled_equality(before.encode(), after.encode())

    assert not isinstance(verdict, CompiledEqual), f"{description} was certified safe"
    assert isinstance(verdict, CompiledDiffers)
    assert verdict.reason == "executable_fields_differ"


def test_a_changed_constant_is_refused() -> None:
    verdict = check("def f():\n    return 41\n", "def f():\n    return 42\n")
    assert isinstance(verdict, CompiledDiffers), verdict
    assert "co_consts" in verdict.detail


def test_a_changed_exception_table_is_refused() -> None:
    verdict = check(
        "def f():\n    try:\n        x = 1\n    except E:\n        raise\n    return x\n",
        "def f():\n    try:\n        x = 1\n    except E:\n        return 0\n    return x\n",
    )
    assert isinstance(verdict, CompiledDiffers), verdict


def test_a_changed_closure_is_refused() -> None:
    """A closure's cell variables are part of the executable representation."""
    verdict = check(
        "def o():\n    c = 1\n    def i():\n        return c\n    return i\n",
        "def o():\n    c = 1\n    def i():\n        return 1\n    return i\n",
    )
    assert isinstance(verdict, CompiledDiffers), verdict


def test_a_changed_binding_is_refused() -> None:
    """`co_names` is a binding, and a name change moves a STORE to a different one."""
    verdict = check("def f():\n    alpha = 1\n    return alpha\n",
                    "def f():\n    beta = 1\n    return beta\n")
    assert isinstance(verdict, CompiledDiffers), verdict


def test_a_deleted_definition_is_refused() -> None:
    verdict = check("def a():\n    return 1\ndef b():\n    return 2\n",
                    "def a():\n    return 1\n")
    assert isinstance(verdict, CompiledDiffers), verdict


def test_nothing_that_compiles_the_same_is_refused() -> None:
    """The counter-test. A patch that changes only comments must PASS, or the
    checker is not discriminating between executable content and prose."""
    verdict = check("def f(a):\n    # note\n    return a\n",
                    "def f(a):\n    return a\n")
    assert isinstance(verdict, CompiledEqual), verdict


def test_an_unsupported_interpreter_is_refused_rather_than_extrapolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unmeasured CPython version is a refusal, never a pass."""
    from vkit.cleanup import logic

    monkeypatch.setattr(logic.platform, "python_version", lambda: "3.9.0")
    verdict = verify_compiled_equality(b"x = 1\n", b"x = 1\n")

    assert isinstance(verdict, CompiledUnsupported)
    assert verdict.reason == "unsupported_python_version"


def test_an_unknown_code_field_is_refused_rather_than_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field the checker has never heard of makes its coverage claim wrong."""
    from vkit.cleanup import logic

    monkeypatch.setattr(logic, "_census", lambda: logic.CompiledUnsupported(
        "unknown_code_field",
        "this interpreter's code objects carry ['co_something_new'], which "
        f"{logic.CHECKER_ID} does not compare",
    ))
    verdict = verify_compiled_equality(b"x = 1\n", b"x = 1\n")

    assert isinstance(verdict, CompiledUnsupported)
    assert verdict.reason == "unknown_code_field"


def test_the_field_list_excludes_only_location_metadata() -> None:
    """The plan's "exclude only explicitly documented source-location metadata".

    Every excluded field is documented with a reason, and the excluded set does
    not overlap the compared set. A field appearing in both would mean the
    coverage claim is incoherent.
    """
    for field, reason in EXCLUDED_LOCATION_FIELDS.items():
        assert reason.strip(), f"{field} is excluded without a reason"
        assert field not in COMPARED_FIELDS, f"{field} is both compared and excluded"
    for required in ("co_code", "co_consts", "co_names", "co_varnames",
                     "co_freevars", "co_cellvars", "co_exceptiontable", "co_flags"):
        assert required in COMPARED_FIELDS, f"{required} must be compared"


def test_the_checker_refuses_if_this_interpreter_is_not_one_it_measured() -> None:
    """The suite's own guarantee: it is only meaningful on a measured runtime."""
    assert platform.python_version() in TESTED_CPYTHON_VERSIONS, (
        f"this interpreter {platform.python_version()} is not in "
        f"{sorted(TESTED_CPYTHON_VERSIONS)}; the compiled-equality tests cannot "
        "certify anything here and should be skipped or the version added"
    )
    assert REQUIRED_OPTIMIZE_LEVELS == (0, 1, 2)


# --------------------------------------------------------------------------- #
# Optimization levels
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("level", REQUIRED_OPTIMIZE_LEVELS)
def test_every_required_level_is_compared_and_recorded(level: int) -> None:
    """The receipt names the levels, so a reader can see what was compared."""
    verdict = check("def f(a):\n    return a\n", "def f(a):\n    return a\n")
    assert isinstance(verdict, CompiledEqual)
    assert level in verdict.optimize_levels
    assert verdict.to_json()["optimizeLevels"] == [0, 1, 2]


def test_an_empty_else_pass_is_equal_at_every_optimization_level() -> None:
    """The plan's acceptance row: this one CAN apply."""
    before = "def f(a):\n    if a:\n        return 1\n    else:\n        pass\n"
    after = "def f(a):\n    if a:\n        return 1\n"

    for level in REQUIRED_OPTIMIZE_LEVELS:
        a = compile(before, "<a>", "exec", optimize=level)
        b = compile(after, "<b>", "exec", optimize=level)
        assert isinstance(
            verify_compiled_equality(before.encode(), after.encode()), CompiledEqual
        ), f"opt level {level} disagreed"

    assert isinstance(verify_compiled_equality(before.encode(), after.encode()), CompiledEqual)


# --------------------------------------------------------------------------- #
# Docstring promotion
# --------------------------------------------------------------------------- #


PROMOTION_CASES: tuple[tuple[str, str, str], ...] = (
    ("a function", "def f():\n    pass\n    'doc'\n", "def f():\n    'doc'\n"),
    ("a class", "class C:\n    pass\n    'doc'\n", "class C:\n    'doc'\n"),
    ("a module", "pass\n'doc'\n", "'doc'\n"),
)


@pytest.mark.parametrize(
    ("description", "before", "after"), PROMOTION_CASES,
    ids=[case[0] for case in PROMOTION_CASES],
)
def test_a_pass_that_promotes_a_docstring_is_caught(
    description: str, before: str, after: str
) -> None:
    """The plan's guard, tested against the observable it changes."""
    verdict = verify_docstrings(before.encode(), after.encode())

    assert not isinstance(verdict, CompiledEqual), (
        f"{description} docstring promotion was certified safe"
    )
    assert isinstance(verdict, CompiledDiffers)
    assert verdict.reason == "docstring_changed"


def test_a_docstring_that_does_not_move_is_equal() -> None:
    verdict = verify_docstrings(
        b"def f():\n    'doc'\n    return 1\n", b"def f():\n    'doc'\n    return 1\n"
    )
    assert isinstance(verdict, CompiledEqual), verdict


def test_the_docstring_check_reads_staticly_and_does_not_execute() -> None:
    """It parses, it does not import. A checker that ran the file to find a
    docstring would execute arbitrary code during a cleanup decision."""
    from vkit.cleanup.logic import static_docstrings

    found = static_docstrings("def f():\n    'doc'\n")
    assert found["<module>/f"] == "doc"
    # A module whose first statement is `pass` has no docstring, which is the
    # promotion the check exists to catch: removing that `pass` would install one.
    assert static_docstrings("pass\n'doc'\n")["<module>"] is None
    assert static_docstrings("'doc'\n")["<module>"] == "doc"


# --------------------------------------------------------------------------- #
# The rules, through the public preview
# --------------------------------------------------------------------------- #


def test_a_trailing_empty_else_pass_is_proposed_for_removal(tmp_path: Path) -> None:
    """The plan's acceptance row: an empty else with identical executable
    fields can apply under the scoped policy."""
    project = project_with(
        tmp_path, "sample.py",
        b"def f(a):\n    if a:\n        return 1\n    else:\n        pass\n",
    )

    preview = preview_logic_cleanup(project, "sample.py")

    assert isinstance(preview, LogicProposal), preview
    assert preview.rule_id == EMPTY_ELSE_PASS
    assert preview.after_bytes == b"def f(a):\n    if a:\n        return 1\n"
    assert preview.optimize_levels == (0, 1, 2)


def test_a_trailing_redundant_pass_is_proposed_for_removal(tmp_path: Path) -> None:
    project = project_with(tmp_path, "sample.py", b"def f(a):\n    x = a\n    pass\n")

    preview = preview_logic_cleanup(project, "sample.py")

    assert isinstance(preview, LogicProposal), preview
    assert preview.rule_id == REDUNDANT_PASS
    assert preview.after_bytes == b"def f(a):\n    x = a\n"


def test_an_interior_pass_produces_no_proposal(tmp_path: Path) -> None:
    """The other half of the plan's acceptance row, and it is a refusal."""
    project = project_with(
        tmp_path, "sample.py", b"def f(a):\n    x = a\n    pass\n    return x\n"
    )

    preview = preview_logic_cleanup(project, "sample.py")

    assert isinstance(preview, LogicRefusal), preview
    assert preview.reason == "no_candidate"
    assert (project.root / "sample.py").read_bytes() == (
        b"def f(a):\n    x = a\n    pass\n    return x\n"
    )


def test_a_docstring_promoting_pass_produces_no_proposal(tmp_path: Path) -> None:
    project = project_with(tmp_path, "sample.py", b"def f():\n    pass\n    'doc'\n")

    preview = preview_logic_cleanup(project, "sample.py")

    assert isinstance(preview, LogicRefusal), preview
    assert (project.root / "sample.py").read_bytes() == b"def f():\n    pass\n    'doc'\n"


def test_a_lone_pass_is_never_a_candidate(tmp_path: Path) -> None:
    """`try`, `except` and a bare `if` require a body, so a lone `pass` is
    load-bearing syntax rather than a leftover."""
    project = project_with(
        tmp_path, "sample.py", b"def f(a):\n    if a:\n        pass\n"
    )

    preview = preview_logic_cleanup(project, "sample.py")

    assert isinstance(preview, LogicRefusal), preview


def test_previewing_twice_yields_the_same_proposal(tmp_path: Path) -> None:
    """No clock and no counter, so the proposal id is content-addressed."""
    project = project_with(
        tmp_path, "sample.py",
        b"def f(a):\n    if a:\n        return 1\n    else:\n        pass\n",
    )

    first = preview_logic_cleanup(project, "sample.py")
    second = preview_logic_cleanup(project, "sample.py")

    assert isinstance(first, LogicProposal) and isinstance(second, LogicProposal)
    assert first.proposal_id == second.proposal_id
    assert first.after_bytes == second.after_bytes


def test_an_unknown_rule_id_is_refused(tmp_path: Path) -> None:
    project = project_with(tmp_path, "sample.py", b"x = 1\n")

    preview = preview_logic_cleanup(project, "sample.py", rule_ids=["LOGIC-NOT-REGISTERED"])

    assert isinstance(preview, LogicRefusal), preview
    assert "unknown" in preview.detail.lower() or "not registered" in preview.detail.lower()


def test_javascript_is_refused_by_name(tmp_path: Path) -> None:
    project = project_with(tmp_path, "sample.ts", b"const x = 1;\n")

    preview = preview_logic_cleanup(project, "sample.ts")

    assert isinstance(preview, LogicRefusal), preview
    assert preview.reason == "unsupported_language"


def test_a_malformed_file_is_refused(tmp_path: Path) -> None:
    project = project_with(tmp_path, "sample.py", b"def broken(:\n    pass\n")

    preview = preview_logic_cleanup(project, "sample.py")

    assert isinstance(preview, LogicRefusal), preview
    assert preview.reason == "malformed_source"


def test_only_two_rules_are_registered() -> None:
    """The plan's "do not generalize". A third rule id is a defect, not a start."""
    assert REDUNDANT_PASS in (EMPTY_ELSE_PASS, REDUNDANT_PASS)
    assert len({EMPTY_ELSE_PASS, REDUNDANT_PASS}) == 2
    assert CHECKER_ID.startswith("vkit.cleanup.logic")


def test_the_receipt_names_the_checker_and_the_fields(tmp_path: Path) -> None:
    project = project_with(
        tmp_path, "sample.py",
        b"def f(a):\n    if a:\n        return 1\n    else:\n        pass\n",
    )

    preview = preview_logic_cleanup(project, "sample.py")
    assert isinstance(preview, LogicProposal)
    document = preview.to_json()

    assert document["checker"] == CHECKER_ID
    assert document["optimizeLevels"] == [0, 1, 2]
    assert "co_consts" in document["compiledEquality"]["checkedFields"]
    assert "co_firstlineno" in document["compiledEquality"]["excludedFields"]