"""A repository the integration tests can actually merge branches into.

The product under test is a verification product, so the fixture has to be a
real repository with real code and a real driver, not a stub. What it is not is
vkit's own repository: an integration test that ran against vkit's example would
be testing the example's arithmetic, and the plan's rows are about
orchestration, not about totals.

**The app, and why it is split across two files.** A discount on a price is
bounded by a maximum percentage, and the two rules are separated on purpose:

    pricing/rules.py    MAX_DISCOUNT_PERCENT, and the ceiling the order allows
    pricing/quote.py    subtotal(), and a floor that depends on the ceiling

The split is what lets the plan's central row be real. Two engineers edit
different files, Git merges them without a conflict, and the result is wrong:
one of them raises the ceiling, and the other guards a hard-coded 100% that no
longer holds. Neither edit is wrong on its own and each passes the same check
in its own checkout. That is a semantic conflict, which is the only kind this
product can catch and the only kind a text merge cannot.

**The driver.** `verify_price.py` holds the literal expected outputs and
compares them against what the real process printed. It takes no pass or fail
argument, so nothing but the code under test can produce agreement. `{{python}}`
is used deliberately, so a check that runs here exercises the interpreter
substitution Plan 07's trusted path depends on. It always exits 0: a run that
both writes an artifact and fails is BLOCKED as untrustworthy, so the outcome
belongs in the artifact and the exit code is reserved for the driver crashing.

**The scenarios.** `discount-above-ceiling` and `never-negative` are the two that
fail on a merge. The rest pass either way, and their presence is what makes the
failure evidence specific rather than a blanket "something broke": a defect that
fails everything is not a driver that discriminates.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

RULES_NAME = "pricing/rules.py"
QUOTE_NAME = "pricing/quote.py"
# `-m` names a module, not a path, so the driver receives the dotted form. The
# two are kept adjacent because getting this wrong produces a ModuleNotFoundError
# about a path that visibly exists, which reads as a fixture bug rather than an
# argument mistake.
QUOTE_MODULE = "pricing.quote"
DRIVER_NAME = "verify_price.py"
MANIFEST_RELATIVE = "verification/manifest.json"
POLICY_NAME = "policy.json"

APP_NAMES = (RULES_NAME, QUOTE_NAME)
CHECK_ID = "quote-behavior"
CHECK_COMMAND = [
    "python", DRIVER_NAME, "--app", QUOTE_MODULE, "--out", "{{run_dir}}/result.json",
]
SCENARIOS = (
    "no-discount",
    "ordinary-discount",
    "discount-at-ceiling",
    "free-order-is-zero",
    "discount-above-ceiling",
    "subtotal-is-additive",
)

#: How every child process in this file is launched on Windows.
#:
#: A fixture that spawns git and Python dozens of times otherwise throws a console
#: window per child over whatever the developer is using, and a test run becomes
#: unusable on a machine someone is looking at. Both flags are no-ops off
#: Windows, so this is one call site rather than a platform branch at each spawn.
#: `CREATE_NO_WINDOW` does the work; `STARTF_USESHOWWINDOW` with an explicit
#: hidden window is set as well because a spawn that goes through a batch wrapper
#: can still ask for a visible window, and one leaked window is the complaint
#: this removes.
def _hidden() -> dict:
    if not sys.platform.startswith("win"):
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startupinfo}

RULES_SOURCE = '''\
"""The pricing rules, in one place, so a policy change is one reviewable diff."""

# The most a promotion may take off, as a percentage. A merchant may
# legitimately let a promotion exceed a hundred percent; what a customer is
# shown is a separate question, answered in the quoting module.
MAX_DISCOUNT_PERCENT = 100


class DiscountRule:
    """How much a discount may take off.

    The order-level guarantee that a customer is never charged less than zero is
    deliberately NOT restated here. One rule in one place is what makes a policy
    change reviewable; a second copy of the same number in a caller is what makes
    a policy change dangerous.
    """

    def __init__(self, max_discount_percent: int = MAX_DISCOUNT_PERCENT) -> None:
        if max_discount_percent < 0:
            raise ValueError("a discount ceiling cannot be negative")
        self.max_discount_percent = max_discount_percent

    def is_allowed(self, percent: int) -> bool:
        return 0 <= percent <= self.max_discount_percent
'''

QUOTE_SOURCE = '''\
"""Quoting an order: a subtotal, then a discount, then the guarantee.

The guarantee is that a customer is never charged less than zero. Whether the
discount may exceed 100% is a separate question, answered by the rules, and the
two answers belong together: a merchant who lets a promotion take more than the
whole order still must not hand the customer a credit.
"""
import argparse

from .rules import DiscountRule

# A promotion above a hundred percent is a legitimate policy, so the ceiling is
# the rules' business. How far below zero a total may fall is a guarantee about
# what the customer is shown, so it is quoted here. Both numbers are needed and
# neither implies the other: the ceiling bounds the DISCOUNT, and this bounds
# the RESULT.
FLOOR_CENTS = 0


def subtotal(prices_cents, quantities) -> int:
    """The order total before any discount."""
    total = 0
    for price, quantity in zip(prices_cents, quantities):
        total += price * quantity
    return total


def quote(prices_cents, quantities, rule: DiscountRule, percent: int = 0) -> dict:
    """Quote an order. `percent` is a request; the rule decides what happens."""
    before = subtotal(prices_cents, quantities)
    if rule.is_allowed(percent):
        allowed = percent
        reason = "allowed"
    else:
        allowed = rule.max_discount_percent
        reason = "clamped to the maximum permitted discount"
    after = before - (before * allowed // 100)
    if after < FLOOR_CENTS:
        after = FLOOR_CENTS
    return {
        "subtotal": before,
        "requested_percent": percent,
        "applied_percent": allowed,
        "reason": reason,
        "total": after,
    }


def main(argv=None) -> int:
    """Print the total for one order. The driver compares what this printed."""
    parser = argparse.ArgumentParser(description="Quote one order with a percentage discount.")
    parser.add_argument("--price", required=True, type=int, help="unit price in cents")
    parser.add_argument("--quantity", required=True, type=int, help="units ordered")
    parser.add_argument("--percent", default="0", type=int, help="discount requested")
    args = parser.parse_args(argv)
    result = quote([args.price], [args.quantity], DiscountRule(), args.percent)
    print(result["total"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

# (scenario id, request, expected total printed by the real CLI)
#
# Every case runs at the ceiling the driver is given, on purpose. A ceiling
# above 100 means the rule allows a discount that takes more than the whole
# order, which is a legitimate promotional policy and not a bug. What is never
# legitimate is the total coming out below zero, and `never-negative` is the case
# that says so. The semantic conflict the integration row needs is therefore
# between the ceiling in the rules and the floor in the guard, not between the
# ceiling and a fixed constant, because the ceiling is a value a candidate may
# legitimately change.
CASES: list[tuple[str, list[str], str]] = [
    ("no-discount", ["--price", "250", "--quantity", "2", "--percent", "0"], "500"),
    ("ordinary-discount", ["--price", "1000", "--quantity", "1", "--percent", "10"], "900"),
    ("discount-at-ceiling", ["--price", "1000", "--quantity", "1", "--percent", "100"], "0"),
    # A free order. It carries no discount at all, so the only thing that can
    # decide its total is the floor -- which is exactly what makes it the case
    # that catches a guard derived from the amount being discounted.
    ("free-order-is-zero", ["--price", "1000", "--quantity", "1", "--percent", "100"], "0"),
    ("discount-above-ceiling", ["--price", "1000", "--quantity", "1", "--percent", "250"], "0"),
    ("subtotal-is-additive", ["--price", "300", "--quantity", "2", "--percent", "50"], "300"),
]

# A candidate's version of the expectations, with the floor weakened. This is a
# change to what the check DEMANDS, not to what the app does, and the integration
# tests use it to show that a passing check and an accepted candidate are not the
# same fact: the candidate's own evidence records a total nobody should be
# willing to accept.
WEAKENED_CASES: list[tuple[str, list[str], str]] = [
    (scenario, tail, "0" if expected == "0" else expected)
    for scenario, tail, expected in CASES
]

DRIVER_SOURCE = '''\
"""Runs the real CLI and records what actually happened.

The expected totals are literals in this file. No argument requests a pass, so
agreement can only come from the code under test behaving as it should. Every
case is run as a fresh process, so one import cannot warm a module for the next
case and hide a change in module-level state.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

CASES = __CASES__


def _hidden():
    if not sys.platform.startswith("win"):
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startupinfo}


def run_case(python, app, case):
    scenario_id, tail, expected = case
    done = subprocess.run(
        [python, "-m", app, *tail],
        capture_output=True, text=True, timeout=60, check=False,
        **_hidden(),
    )
    printed = done.stdout.strip()
    passed = done.returncode == 0 and printed == expected
    if done.returncode != 0:
        observation = f"exited {done.returncode}: {done.stderr.strip()[:200]}"
    elif passed:
        observation = f"printed {printed}"
    else:
        observation = f"expected {expected}, printed {printed or '<nothing>'}"
    return {"id": scenario_id, "result": "PASS" if passed else "FAIL", "observation": observation}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    scenarios = [run_case(sys.executable, args.app, case) for case in CASES]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Written atomically, and the process always exits 0.
    #
    # Exiting non-zero on a failing scenario would collide with execution.
    # failure: a run that both writes an artifact and fails is BLOCKED as
    # untrustworthy rather than reported as the FAIL the artifact describes, so a
    # driver that did that would turn every real failure into BLOCKED. The
    # outcome belongs in the artifact; the exit code is reserved for "the driver
    # itself could not do its job", which is what a crash is.
    temp = out.with_suffix(out.suffix + ".tmp")
    temp.write_text(
        json.dumps({"schema_version": 1, "scenarios": scenarios}, indent=2) + "\\n",
        encoding="utf-8",
    )
    temp.replace(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

def driver_source(cases: list[tuple[str, list[str], str]] | None = None) -> str:
    """The driver, with the given expectations written into it.

    The cases are the fixture's by default, and a test that wants a defect can
    pass its own. That is the only way the expectations can differ between the
    approved baseline and a candidate: they are the verification semantics, and
    a candidate that redefines them is making a policy change, not a code
    change. Every test that needs a defect in the EXPECTATIONS says so in its
    own name.
    """
    return DRIVER_SOURCE.replace("__CASES__", repr(cases if cases is not None else CASES))


def manifest_document(
    *,
    check_id: str = CHECK_ID,
    command: list[str] | None = None,
    scenarios: tuple[str, ...] = SCENARIOS,
    timeout_seconds: int = 120,
    artifact: str = "result.json",
) -> dict:
    return {
        "schema_version": 1,
        "description": "The real quote CLI, verified by running it and comparing printed totals.",
        "checks": [
            {
                "id": check_id,
                "description": "Runs the quote CLI and compares printed totals with literal expectations.",
                "command": list(command or CHECK_COMMAND),
                "cwd": ".",
                "timeout_seconds": timeout_seconds,
                "required_scenarios": list(scenarios),
                "artifact": artifact,
                "inputs": [*APP_NAMES, DRIVER_NAME],
                "expectations": [],
            }
        ],
    }


def policy_document(
    *,
    check_id: str = CHECK_ID,
    scenarios: tuple[str, ...] = SCENARIOS,
    manifest_revision: str | None = None,
    context: str | None = None,
    evidence_kind: str | None = None,
) -> dict:
    document: dict = {
        "schema_version": 1,
        "description": "The bar every integration candidate is measured against.",
        "required_checks": [{
            "id": check_id,
            "obligations": [{"kind": "case", "obligation": s} for s in scenarios],
            "evidence_kind": evidence_kind or "scenario",
        }],
    }
    if manifest_revision:
        document["manifest_revision"] = manifest_revision
    if context:
        document["context"] = context
    return document


# ------------------------------------------------------------------ git


#: Environment variables that let git be aimed somewhere other than the directory
#: it is run in. `GIT_DIR` is the dangerous one: it is inherited by every child
#: process, so an export anywhere in a test run silently redirects EVERY
#: `git init`, `git config` and `git commit` below into whatever repository it
#: names. That happened, and it wrote `core.worktree` into the real repository
#: and killed git on the developer's own machine.
#:
#: Stripped rather than overridden. `GIT_CONFIG_GLOBAL` and friends would be a
#: second way to be wrong, and the point of the fixture is that `git` in a
#: throwaway directory means that directory.
_GIT_STEERING = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE")


def git(repo: Path, *args: str) -> str:
    # Stripped, not overridden. Setting GIT_DIR to `repo/.git` looks like a
    # stronger guarantee and is not: git then refuses operations that need a
    # work tree ("fatal: this operation must be run in a work tree") because the
    # variable says "this is a bare-ish git dir" rather than "this is a
    # repository". Removing the variables lets git do its own discovery from cwd,
    # which is the behaviour the fixture is written against.
    env = {k: v for k, v in os.environ.items() if k not in _GIT_STEERING}
    done = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, encoding="utf-8",
        errors="replace", timeout=180, check=False, env=env, **_hidden(),
    )
    if done.returncode != 0:
        detail = done.stderr.strip().splitlines()
        raise AssertionError(
            f"git {' '.join(args)} failed in {repo}: "
            f"{detail[-1] if detail else done.stdout.strip()}"
        )
    return done.stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


GITIGNORE = """\
# Python bytecode. Running the app inside a checkout leaves it behind, and a
# checkout that a check just ran in is not dirty by accident -- it is dirty
# because the check ran. Declaring it here is what tells `git status` that.
__pycache__/
*.pyc
"""


def build_repository(root: Path) -> Path:
    """A committed, passing repository, and the commit its policy pins.

    The pinned baseline is named in every test that uses it, so a reader knows
    which definition of the check "the approved one" refers to rather than
    trusting whatever the fixture contains today.
    """
    repo = root / "shop"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (repo / RULES_NAME).parent.mkdir(parents=True, exist_ok=True)
    (repo / RULES_NAME).write_text(RULES_SOURCE, encoding="utf-8")
    (repo / QUOTE_NAME).write_text(QUOTE_SOURCE, encoding="utf-8")
    (repo / DRIVER_NAME).write_text(driver_source(), encoding="utf-8")
    write_json(repo / MANIFEST_RELATIVE, manifest_document())
    write_json(repo / POLICY_NAME, policy_document())

    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "Integration Test")
    # Two fixture settings that would otherwise make this host decide the test's
    # outcome. `merge.tool` is pinned because this host has an interactive
    # mergetool that fails outright, and `rerere` is disabled because a recorded
    # resolution for a previous test's conflict would be replayed into this one
    # and hide the conflict the row is about. A fixture that inherits the
    # developer's global configuration is a fixture whose failures mean nothing.
    git(repo, "config", "merge.tool", "true")
    git(repo, "config", "rerere.enabled", "false")
    baseline = commit_all(repo, "the passing baseline")
    # The baseline is the revision the policy pins, so its own policy names it.
    write_json(repo / POLICY_NAME, policy_document(manifest_revision=baseline))
    commit_all(repo, "pin the approved manifest revision")
    # Main stays checked out. A linked worktree cannot check out a commit the
    # main checkout already has, so leaving main on the tip would make every
    # integration verification of the tip fail to create its candidate checkout
    # -- and it would fail for a reason that has nothing to do with the
    # decision the command is supposed to be making. A work in progress belongs
    # on a branch, which is what every test creates.
    return repo


def main_revision(repo: Path) -> str:
    """Where main points, without changing what is checked out."""
    return git(repo, "rev-parse", "main")


def baseline_revision(repo: Path) -> str:
    """The commit the repository's own policy pins.

    Read from the policy at `main` rather than from the working tree, because a
    worker branch is usually the current checkout and carries its own copy of
    the policy file. The commit the policy approves is the same on every branch
    that did not change it, and reading it from `main` says exactly that.
    """
    return json.loads(git(repo, "show", f"main:{POLICY_NAME}"))["manifest_revision"]


def checkout_branch(repo: Path, name: str, start: str | None = None) -> str:
    """A new branch, and the commit it starts at."""
    if start:
        git(repo, "checkout", "-q", "-B", name, start)
    else:
        git(repo, "checkout", "-q", "-B", name)
    return git(repo, "rev-parse", "HEAD")


def replace_in(repo: Path, filename: str, old: str, new: str, message: str) -> str:
    """One textual edit, committed. The only edit mechanism the tests use.

    It refuses when `old` is absent rather than silently doing nothing, because
    a test that intended to inject a defect and injected nothing would go on to
    report a pass that means nothing.
    """
    path = repo / filename
    source = path.read_text(encoding="utf-8")
    if old not in source:
        raise AssertionError(f"{filename} does not contain the text {old!r}")
    if source.count(old) != 1:
        raise AssertionError(f"{old!r} appears {source.count(old)} times in {filename}")
    path.write_text(source.replace(old, new), encoding="utf-8")
    return commit_all(repo, message)


# The two edits the plan's central row is built on. They are in different files,
# and neither one's correctness depends on the other, so Git merges them without
# a conflict and the combination is wrong. Either one alone still passes every
# scenario, which is what makes the merge the variable under test.
#
# One raises the ceiling, which is a legitimate promotion policy: a merchant
# may let a discount take more than the whole order. The other narrows the floor
# to apply only to a discount of a hundred percent or less, which is a
# reasonable reading of "a discount cannot exceed the price" while the ceiling
# cannot go past a hundred, and is wrong the moment it can. Neither decision
# refers to the other. Their combination is a total below zero.
RAISE_THE_CEILING_OLD = "MAX_DISCOUNT_PERCENT = 100"
RAISE_THE_CEILING_NEW = "MAX_DISCOUNT_PERCENT = 200"
HARD_CODE_THE_GUARD_OLD = "    if after < FLOOR_CENTS:\n        after = FLOOR_CENTS"
HARD_CODE_THE_GUARD_NEW = "    if allowed <= 100 and after < FLOOR_CENTS:\n        after = FLOOR_CENTS"
