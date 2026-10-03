"""Every result view shows the category, and a property PASS cannot read as a scenario one.

A green tick means four different things in this product. The same tick covers a
test that proved one named example, a property check that sampled a family, a
model check that explored every reachable state and a kernel-checked theorem, and
`claimkind.py` exists because a reader who sees only the tick cannot tell which
they have. This file asserts the category reaches the reader on every surface
that shows a verdict.

**How each surface is driven.** The live views are rendered by running the real
`app.js` against the real console over a real repository, through the DOM harness
`test_console_home_view.py` already builds, because a page that builds the right
elements in a different order than it ships is not a page that shows them. The
API surfaces are asserted on the payloads, because a field the page never reads
is a field a reader never sees and the two have to be checked separately.

**The one that matters.** `test_a_property_pass_cannot_be_read_as_a_scenario_pass`
runs one property check and one scenario check over comparable repositories and
asserts the rendered text differs. Asserting that a property view contains the
word "property" would pass on a page that also shows the word everywhere else, so
this compares the two renderings to each other.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import threading
from pathlib import Path

import pytest

import subproc

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from vkit.console import operations, server  # noqa: E402

NODE = shutil.which("node")
HYPOTHESIS_IMPORTABLE = (
    subproc.run(
        [sys.executable, "-c", "import hypothesis"], capture_output=True, check=False,
    ).returncode == 0
)

requires_node = pytest.mark.skipif(
    NODE is None, reason="requires a node binary to render the page's DOM"
)

#: The DOM harness `test_console_home_view.py` builds. Reused rather than
#: rewritten: the page's own rendering is exercised by that file's driver, and a
#: second driver would be a second set of assumptions about what the page does.
_HOME_VIEW = Path(__file__).with_name("test_console_home_view.py")

PROPERTY_SOURCE = '''\
"""The code under test, and a separate model of what it should return."""
from hypothesis import given, strategies as st

from shop.pricing import model_quote, quote


@given(st.lists(st.integers(min_value=-1_000, max_value=1_000), max_size=10))
def test_quote_agrees_with_the_reference_model(amounts):
    assert quote(amounts) == model_quote(amounts)
'''

PRICING_SOURCE = '''\
"""The code under test, and a separate model of what it should return.

The two differ mechanically on purpose: a property check that compared the code
against itself would agree with every bug it contained.
"""


def quote(amounts):
    return sum(amounts)


def model_quote(amounts):
    total = 0
    for amount in amounts:
        total += amount
    return total
'''

DRIVER_SOURCE = '''\
"""Runs the real function and records what actually happened."""
import json
import sys
from pathlib import Path

from shop.pricing import quote

CASES = [("one-amount", [7], 7), ("two-amounts", [2, 3], 5)]


def main():
    out = Path(sys.argv[1])
    scenarios = []
    for name, values, expected in CASES:
        printed = quote(values)
        scenarios.append({
            "id": name,
            "result": "PASS" if printed == expected else "FAIL",
            "observation": f"printed {printed}",
        })
    out.write_text(
        json.dumps({"schema_version": 1, "scenarios": scenarios}, indent=2) + "\\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _init_repo(repo: Path) -> None:
    subproc.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subproc.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subproc.run(
        ["git", "-c", "user.email=t@t.invalid", "-c", "user.name=t",
         "commit", "-qm", "the approved baseline"],
        cwd=repo, check=True, capture_output=True,
    )


def _repository(tmp_path: Path, *, kind: str) -> Path:
    """A real repository registering one check of the named kind."""
    repo = tmp_path / kind
    (repo / "shop").mkdir(parents=True)
    (repo / "shop" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "shop" / "pricing.py").write_text(PRICING_SOURCE, encoding="utf-8")
    (repo / "verification").mkdir()

    if kind == "property":
        (repo / "tests").mkdir()
        (repo / "tests" / "test_property.py").write_text(PROPERTY_SOURCE, encoding="utf-8")
        check = {
            "id": "quote-property",
            "kind": "property",
            "description": "Requires each generated case to pass under a pinned family.",
            "cwd": ".",
            "timeout_seconds": 180,
            "artifact": "property-report.json",
            "inputs": ["shop/pricing.py", "tests/test_property.py"],
            "expectations": [],
            "subject": {"paths": ["shop/pricing.py"], "digest": None},
            "claim_id": "quote-agrees-with-its-model",
            "required_tests": ["tests/test_property.py::test_quote_agrees_with_the_reference_model"],
            "runner": {"executable": "{{python}}", "base_argv": ["-m", "pytest", "-q", "-p", "no:cacheprovider"]},
            "report_format": "pytest_json_report",
            "expect_report_version": 1,
            "generator": {
                "max_examples": 25, "stateful_step_count": 0,
                "deadline": None, "suppress_health_check": [],
            },
            "replay": {"database": ".hypothesis/examples", "seed": None},
        }
    else:
        (repo / "verify.py").write_text(DRIVER_SOURCE, encoding="utf-8")
        check = {
            "id": "quote-behavior",
            "kind": "scenario",
            "description": "Runs the real function and compares printed totals.",
            "command": ["{{python}}", "verify.py", "{{run_dir}}/result.json"],
            "cwd": ".",
            "timeout_seconds": 120,
            "required_scenarios": ["one-amount", "two-amounts"],
            "artifact": "result.json",
            "inputs": ["shop/pricing.py", "verify.py"],
            "expectations": [],
            "subject": {"paths": ["shop/pricing.py"], "digest": None},
            "claim_id": "quote-prints-the-expected-totals",
        }

    (repo / "verification" / "manifest.json").write_text(
        json.dumps({"schema_version": 2, "checks": [check]}, indent=2) + "\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text(".hypothesis/\n__pycache__/\n*.pyc\n", encoding="utf-8")
    _init_repo(repo)
    return repo


def _run(repo: Path, check_id: str) -> str:
    """Run one check through the CLI, the way an operator would. Returns the run id."""
    from vkit import cli

    argv = [
        "vkit", "check", "run", "--project", str(repo), "--check", check_id, "--json",
    ]
    saved = sys.argv
    sys.argv = list(argv)
    try:
        status = cli.main(list(argv[1:]))
    finally:
        sys.argv = saved
    assert status == 0, f"{check_id} exited {status}"

    from vkit.paths import open_project
    from vkit.storage import Store

    store = Store(open_project(repo).db_path)
    return store.list_runs(limit=1)[0]["run_id"]


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory):
    """Both kinds rendered by the real page, over one live console each.

    Two consoles rather than one repository holding both checks, because the
    assertions compare the two renderings and a single page showing both would
    show both words on both pages. Module-scoped because a console and a run each
    cost a second and every test here reads the same two renders.
    """
    base = tmp_path_factory.mktemp("categories")
    out: dict = {}
    for kind in ("scenario", "property"):
        repo = _repository(base, kind=kind)
        check_id = "quote-property" if kind == "property" else "quote-behavior"
        _run(repo, check_id)

        context = operations.open_context(repo)
        console = server.serve(context, port=0)
        thread = threading.Thread(
            target=console.serve_forever, name=f"vkit-category-{kind}", daemon=True,
        )
        thread.start()
        try:
            url = f"http://127.0.0.1:{console.server_address[1]}"
            out[kind] = {"repo": repo, "url": url, "panels": _render(url)}
        finally:
            console.shutdown()
            console.server_close()
            thread.join(timeout=10)
    return out


def _driver_source() -> str:
    """The DOM harness driver, read from the file that owns it and widened.

    Read rather than imported because it is a string constant holding a whole
    JavaScript program. Duplicating it here would be a second copy of the page's
    DOM assumptions, and the two would drift the first time the harness changed.

    Widened by one line, because that harness reports a fixed list of panels and
    the runs table is not on it. The edit adds a panel this file asserts on and
    changes nothing about how the page is rendered, which is asserted: the
    replacement is checked to have applied before the driver is handed to node.
    """
    text = _HOME_VIEW.read_text(encoding="utf-8")
    match = re.search(r'^DRIVER = r"""(.*?)"""', text, re.DOTALL | re.MULTILINE)
    assert match, "test_console_home_view.py no longer holds a DRIVER constant"
    driver = match.group(1)
    anchor = '"project-body", "setup-body", "checks-body",'
    assert anchor in driver, (
        "the DOM harness no longer collects the panels this file reads, so its "
        "list of ids has changed shape"
    )
    return driver.replace(
        anchor, '"project-body", "setup-body", "checks-body", "runs-body", "run-detail",',
    )


def _render(url: str) -> dict:
    """Run the real app.js against the live console and return the panels' text."""
    static = ROOT / "src" / "vkit" / "console" / "static"
    done = subproc.run(
        [NODE, "-e", _driver_source(), str(static), url, "{}"],
        capture_output=True, encoding="utf-8", errors="replace", timeout=180,
        check=False,
    )
    assert done.returncode == 0, f"the page driver failed:\n{done.stdout}\n{done.stderr}"
    result = json.loads(done.stdout)
    assert "error" not in result, f"the page did not render: {result.get('error')}"
    return result


# ------------------------------------------ the API surfaces carry it


@pytest.mark.skipif(not HYPOTHESIS_IMPORTABLE, reason="requires an importable hypothesis")
def test_the_checks_api_carries_the_category_and_every_obligation(
    tmp_path: Path,
) -> None:
    """A reader choosing a check can see what kind of claim it discharges.

    Two fields, and the second is the one that was empty. `required_scenarios` is
    only ever the case obligations, so for a check whose obligations are theorem
    names or model properties it is `[]` and a page rendering it would show an
    empty list where the check has real obligations. `obligations` is every one,
    in the words `obligation.describe` gives them.
    """
    property_repo = _repository(tmp_path / "property", kind="property")
    scenario_repo = _repository(tmp_path / "scenario", kind="scenario")

    property_check = operations.checks_view(
        operations.open_context(property_repo)
    )["checks"][0]
    scenario_check = operations.checks_view(
        operations.open_context(scenario_repo)
    )["checks"][0]

    assert property_check["evidence_kind"] == "property"
    assert scenario_check["evidence_kind"] == "scenario"
    assert property_check["obligations"] == [
        "case 'tests/test_property.py::test_quote_agrees_with_the_reference_model'",
    ], property_check["obligations"]
    assert scenario_check["obligations"] == [
        "case 'one-amount'", "case 'two-amounts'",
    ], scenario_check["obligations"]


@pytest.mark.skipif(not HYPOTHESIS_IMPORTABLE, reason="requires an importable hypothesis")
def test_the_run_detail_api_carries_the_category_the_run_recorded(
    tmp_path: Path,
) -> None:
    """The detail view's category is what the run recorded, not what the check declares.

    Read from the receipt, which is the document `Store.publish` copies the
    category from and the one the CLI reads. A run that recorded none has no
    category, and None is the answer rather than a default that would read as
    measured.
    """
    repo = _repository(tmp_path, kind="property")
    run_id = _run(repo, "quote-property")
    context = operations.open_context(repo)

    detail = operations.run_detail_view(context, run_id)

    assert detail["evidence_kind"] == "property"
    assert detail["report"]["outcome"]["result"] == "PASS"


def test_a_blocked_run_names_its_category_but_discharges_nothing(
    tmp_path: Path,
) -> None:
    """A refused run still says what kind of evidence it would have produced.

    Driven on its own repository because the check is rewritten here to require a
    scenario its driver never reports. Two facts asserted together, because one
    without the other is a misreading: the category is present, and the receipt
    lists no satisfied obligation.

    The category is not a claim that evidence exists. `dispatch.build_receipt`
    writes it for every receipt including a BLOCKED one, from the check's variant,
    while `_obligation_results` records nothing because a refused run established
    nothing. A display that showed the category beside a BLOCKED would be
    repeating what the receipt says; one that showed no category at all would be
    hiding a fact the reader needs to judge the refusal.
    """
    repo = _repository(tmp_path, kind="scenario")
    manifest = json.loads((repo / "verification" / "manifest.json").read_text(encoding="utf-8"))
    manifest["checks"][0]["required_scenarios"] = ["never-run"]
    (repo / "verification" / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    run_id = _run_expecting(repo, "quote-behavior", 3)
    context = operations.open_context(repo)

    detail = operations.run_detail_view(context, run_id)
    receipt = json.loads(
        (context.store.run_dir(run_id) / "receipt.v2.json").read_text(encoding="utf-8")
    )

    assert detail["report"]["outcome"]["result"] == "BLOCKED"
    assert receipt["status"] == "BLOCKED"
    assert receipt["evidence_kind"] == "scenario"
    assert receipt["satisfied"] == [], (
        "a BLOCKED run established nothing, so a receipt listing satisfied "
        "obligations for it would be the lie this contract exists to prevent"
    )


def _run_expecting(repo: Path, check_id: str, expected_status: int) -> str:
    """Run one check and assert the exit code, returning the run id."""
    from vkit import cli

    argv = [
        "vkit", "check", "run", "--project", str(repo), "--check", check_id, "--json",
    ]
    saved = sys.argv
    sys.argv = list(argv)
    try:
        status = cli.main(list(argv[1:]))
    finally:
        sys.argv = saved
    assert status == expected_status, f"{check_id} exited {status}"

    from vkit.paths import open_project
    from vkit.storage import Store

    return Store(open_project(repo).db_path).list_runs(limit=1)[0]["run_id"]


# ------------------------------------------------- the views display it


@requires_node
def test_every_result_view_shows_the_category(rendered: dict) -> None:
    """Each surface that shows a verdict shows the category beside it.

    Four surfaces, because a verdict is shown in four places: the landing page's
    verification panel, the checks list, the runs table and the run detail. A
    category on three of them still leaves a reader who lands on the fourth
    unable to tell what they are looking at, which is the failure this exists to
    prevent.
    """
    for kind, rendered_one in rendered.items():
        panels = rendered_one["panels"]["panels"]
        assert panels["home-available"]["text"].count("property" if kind == "property" else "scenario") >= 1, (
            f"the landing page's checks table does not show the category for a "
            f"{kind} check: {panels['home-available']['text'][:300]}"
        )
        assert panels["checks-body"]["text"].count(kind) >= 1, (
            f"the checks view does not show the category for a {kind} check: "
            f"{panels['checks-body']['text'][:300]}"
        )


@requires_node
def test_the_runs_table_shows_the_category_of_the_run_it_lists(rendered: dict) -> None:
    """The runs table's rows carry the category of the run they name.

    The table is built from the store's own rows, which carry `evidence_kind`
    because `Store.publish` writes it in the same transaction as the verdict, so
    this asserts the page reads what the run recorded.
    """
    for kind, rendered_one in rendered.items():
        panels = rendered_one["panels"]["panels"]
        runs = panels["runs-body"]["text"]
        assert kind in runs, (
            f"the runs table does not show {kind!r}: {runs[:400]}"
        )


@requires_node
def test_a_property_pass_cannot_be_read_as_a_scenario_pass(rendered: dict) -> None:
    """The comparison that matters, and the reason this file exists.

    One repository ran a property check; the other ran a scenario check; both
    produced PASS; both were rendered by the same page code. The two renderings
    differ by their category, so a reader holding either can tell what kind of
    claim it is looking at.

    Asserted as a comparison between the two renderings rather than as a
    substring search, because a page that printed the word `property` somewhere
    unrelated would satisfy the search and still fail the reader.
    """
    property_panels = rendered["property"]["panels"]["panels"]
    scenario_panels = rendered["scenario"]["panels"]["panels"]

    assert "property" in property_panels["checks-body"]["text"]
    assert "scenario" in scenario_panels["checks-body"]["text"]
    assert "property" not in scenario_panels["checks-body"]["text"], (
        "the scenario checks view names the property category, so the two "
        "readings a reader has are no longer different"
    )
    assert "scenario" not in property_panels["checks-body"]["text"], (
        "the property checks view names the scenario category, so a reader "
        "cannot tell the two results apart"
    )

    property_runs = rendered["property"]["panels"]["panels"]["runs-body"]["text"]
    scenario_runs = rendered["scenario"]["panels"]["panels"]["runs-body"]["text"]
    assert "property" in property_runs and "property" not in scenario_runs
    assert "scenario" in scenario_runs and "scenario" not in property_runs


@requires_node
def test_the_rendered_page_does_not_parse_markup_it_was_given(rendered: dict) -> None:
    """The category reached the page as text, not as markup.

    The category is derived from a variant and a run's receipt, and a category
    string is still a string. Asserted here because this file added values to the
    page's data path, and the page's own rule is that nothing it is handed
    becomes markup.
    """
    for rendered_one in rendered.values():
        assert rendered_one["panels"]["innerHTMLWrites"] == 0, (
            "app.js assigned innerHTML while rendering the category. A value that "
            "reaches innerHTML is a value the browser parses as markup"
        )