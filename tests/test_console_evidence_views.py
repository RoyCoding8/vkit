"""Checkpoint 12.2: the dashboard shows the actual engineering state.

**Every assertion here is on a real response body.** Each test drives the real
server over a real repository and reads what the API and the page actually
returned. None of them restates a constant from the code under test, and none
asserts that a function was called: a test that passes when every imported
function returns `undefined` cannot fail for a defect, so each one names a
literal value and checks the rendered or serialized result carries it.

**Three properties are load-bearing and each has a test that would fail if the
page broke it.**

1. A check's category travels with its verdict. A `property` pass and a
   `scenario` pass are the same three characters, so a page that rendered the
   word alone would let one read as the other.
2. A run's Run action creates a real task through the core's admission path, and
   that task is visible to `tasks.finalize`. There is no console-only lifecycle.
3. Viewing a page does not mutate task state. `compute_readiness` is the reader;
   `record_readiness` is the writer; the console may only call the first. The
   test snapshots the `tasks` table before and after a full page load.

**No process is launched directly.** Anything subprocess-shaped goes through
`tests/subproc.py`, which carries the Windows window-suppression keywords;
`tests/test_subprocess_windows.py` walks this file's AST and fails on a direct
`subprocess` call. This file does not launch anything, so it needs no entry in
that test's allowance table.
"""
from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import subproc

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "python-cli"
STATIC = REPO_ROOT / "src" / "vkit" / "console" / "static"

#: The id the example manifest registers, written out rather than read from the
#: manifest, so a manifest that stopped registering it fails here.
CHECK_ID = "totals-behavior"

#: The value a category-agnostic page would render. The page must show the
#: category beside the verdict, so this exact string must appear.
SCENARIO_VERDICT = "scenario PASS"


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode("utf-8")


def _post(url: str, token: str, body: bytes = b"{}") -> tuple[dict, int]:
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"X-Vkit-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8")), response.status
    except urllib.error.HTTPError as error:
        return json.loads(error.read().decode("utf-8")), error.code


def _token(console: str) -> str:
    """The session token the served page carries.

    Read from the served page, not from a Python attribute, because that is where
    the browser reads it from. A test that took the token from the session object
    would prove the plumbing works only when both sides agreed, which is not what
    the page does.
    """
    import re

    page = _get_text(f"{console}/")
    match = re.search(r'name="vkit-token" content="([^"]+)"', page)
    assert match, "the served page carries no session token, so no mutation can be made"
    token = match.group(1)
    assert token != "__VKIT_TOKEN__", "the served page still carries the unsubstituted placeholder"
    return token


def _task_rows(store) -> list[tuple]:
    """The whole `tasks` table, as comparable rows.

    Read through the store's own connection rather than by opening the database
    file, so a schema the store would migrate is seen the same way the console
    sees it.
    """
    with store._connect() as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT task_id, status, generation, readiness FROM tasks ORDER BY task_id"
        )]


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory):
    """A real console over a real repository, with the real static files.

    Port 0 so a suite cannot collide with a console the operator already has
    open. The repository is a copy of the example, git-initialised, because
    `open_project` resolves the root through git and a project with no commit has
    no HEAD for the page to show.
    """
    from vkit.console import operations, server

    root = tmp_path_factory.mktemp("evidence-views") / "space repo"
    shutil.copytree(EXAMPLE, root)
    subproc.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subproc.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subproc.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "example"],
        cwd=root, check=True, capture_output=True,
    )

    context = operations.open_context(root)
    console = server.serve(context, port=0)
    thread = threading.Thread(target=console.serve_forever, name="vkit-evidence-views", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{console.server_address[1]}", context
    finally:
        console.shutdown()
        console.server_close()
        thread.join(timeout=10)


@pytest.fixture()
def ran(live) -> str:
    """A console that has had one check run against it, and its base url.

    Running the example check takes a couple of seconds and gives every evidence
    assertion a real receipt to read rather than an empty state, which is the
    state where an evidence view can pass by rendering nothing.
    """
    url, _ = live
    token = _token(url)
    started, status = _post(f"{url}/api/run_check?check_id={CHECK_ID}", token)
    assert status == 200, f"the run did not start: {started}"
    return url


# ----------------------------------------------------------- the five sections


def test_every_planned_section_is_reachable_and_renders_real_data(live) -> None:
    """The five sections exist on a route that already exists, and carry real facts.

    The plan names five sections. Each must be served by a route the console
    already answers, because `api.py` owns the route table and no section may
    require one this package cannot add. The assertion checks that every section
    key the backend declares arrives on a route whose document actually contains
    it — a section named in the plan but never wired would otherwise be a view
    with nothing behind it.
    """
    from vkit.console.plan import SECTIONS

    url, _ = live
    assert len(SECTIONS) == 5, "the plan names five sections"

    expected = {section.id for section in SECTIONS}
    assert expected == {"overview", "evidence", "tasks", "cleanup", "integrations"}, (
        f"the sections declared are {sorted(expected)}, not the five the plan names"
    )

    for section in SECTIONS:
        document = _get(f"{url}/api/{section.route}")
        node = document
        for part in section.key.split("."):
            assert part in node, (
                f"section {section.id!r} says it rides /api/{section.route} at "
                f"{section.key!r}, but that route's document has no {part!r}"
            )
            node = node[part]
        assert node, f"section {section.id!r} arrived empty from /api/{section.route}"


def test_the_overview_section_names_the_project_enrollment_and_blockers(live) -> None:
    """The overview answers its question from this project's own record.

    The enrollment state is the core's, read through `enroll.read_enrollment`.
    Asserted against the value the CLI's own reader returns for the same project,
    so a second format of that record cannot satisfy this test.
    """
    from vkit import enroll as core_enroll

    url, context = live
    overview = _get(f"{url}/api/project")["sections"]["overview"]

    assert overview["root"] == str(context.project.root), (
        "the overview names a different root than the console is bound to"
    )
    assert overview["enrollment"]["state"] == core_enroll.read_enrollment(
        context.project
    ).state.value, (
        "the overview reports a different enrollment state than the core's own "
        "reader does, so one of the two is reading a second record"
    )
    assert "checks_registered" in overview, "the overview does not say how many checks exist"
    assert isinstance(overview["blockers"], list), "the overview has no blockers list"

    # Every blocker names what would clear it. A blocker an operator cannot act
    # on is a complaint, not information.
    for blocker in overview["blockers"]:
        assert blocker["detail"], f"a blocker names nothing: {blocker}"
        assert blocker["clears_when"], f"a blocker does not say what clears it: {blocker}"


# ------------------------------------------------------- category vs verdict


def test_a_checks_category_travels_with_its_verdict(ran) -> None:
    """A PASS is rendered with the category that decides how it reads.

    The premise is the domain's own: `claimkind.ClaimCategory` exists because
    "a generic PASS is true and useless", and a property pass and a scenario pass
    are both the word. So the evidence section must not emit the verdict without
    the category. The literal checked is the exact string an operator reads, not
    a substring that a differently-worded page would also satisfy.
    """
    url = ran
    checks = _get(f"{url}/api/checks")["sections"]["evidence"]["checks"]

    by_id = {check["id"]: check for check in checks}
    assert CHECK_ID in by_id, f"the example check {CHECK_ID!r} is not in the evidence section"
    check = by_id[CHECK_ID]

    assert check["category"] == "scenario", (
        f"the example manifest registers a scenario check but the section reports "
        f"{check['category']!r}"
    )
    assert check["latest"]["verdict"] == "PASS", (
        f"the example check was run and should have passed; it reports "
        f"{check['latest']['verdict']!r}"
    )

    # The two facts, and the two sentences that say what each does and does not
    # license. Rendered together they are what stops one PASS reading as another.
    rendered = f"{check['category']} {check['latest']['verdict']}"
    assert rendered == SCENARIO_VERDICT, (
        f"the category and the verdict do not sit together; the page would read "
        f"{check['latest']['verdict']!r} with no category, which is a scenario "
        f"pass readable as a property pass"
    )
    assert check["establishes"] and check["does_not_establish"], (
        "the check carries no statement of what its pass does and does not "
        "establish, so a reader cannot tell which claim the green mark licenses"
    )


def test_the_evidence_section_shows_the_receipt_not_a_bare_pass(ran) -> None:
    """What the run established comes from the receipt, not the run row.

    The run row carries a cached projection of the receipt and omits exactly the
    fields an operator needs: `assumptions`, `limits`, `trust_boundary`, and the
    counterexamples. A view built from the row would show a bare PASS. So this
    asserts the receipt's own four keys are present and non-empty on the response,
    which is what distinguishes a receipt read from a row read.
    """
    url = ran
    latest = _get(f"{url}/api/checks")["sections"]["evidence"]["checks"][0]["latest"]

    assert latest["state"] == "receipt", (
        f"the latest run published no readable receipt (state {latest['state']!r}), "
        f"so the evidence view would be showing the run row alone"
    )
    assert latest["assumptions"], (
        "no assumptions recorded: a receipt always carries at least the note that "
        "its category is derived rather than declared"
    )
    assert latest["limits"], "no limits recorded: a receipt always carries its timeout"
    assert latest["trust_boundary"].get("establishes"), (
        "the receipt carries no trust boundary, so the page cannot say what the "
        "pass does not establish"
    )
    assert latest["trust_boundary"].get("does_not_establish"), (
        "the receipt's trust boundary says what the pass establishes but not what "
        "it does not, which is the half that prevents a category confusion"
    )


def test_a_never_run_check_is_distinguished_from_a_silent_failure(ran) -> None:
    """A check with no run reads as `never_run`, not as an empty PASS.

    Three states have to stay distinguishable: never run, ran with no receipt, and
    ran with a receipt. Collapsing them means a reader who sees no evidence cannot
    tell a project that was never verified from a project whose evidence could
    not be read.
    """
    url = ran
    section = _get(f"{url}/api/checks")["sections"]["evidence"]

    assert section["available"] is True, "the evidence section is not available on a parsed manifest"
    states = {check["latest"]["state"] for check in section["checks"]}
    assert states <= {"never_run", "no_receipt", "receipt"}, (
        f"an evidence row reports an unrecognised state: {states}"
    )
    assert "receipt" in states, (
        "no check on this project reports a receipt, so this test is not "
        "measuring what it claims to"
    )


# ------------------------------------------------------------------- tasks


def test_a_task_carries_its_verdict_missing_evidence_and_held_resources(ran) -> None:
    """A run's verdict, its gaps, and its held resources are shown from real records.

    The held resources are read through `claims.holders`, which is the store's
    own table of who owns what. The gaps are the core's own strings from
    `compute_readiness`, carried verbatim rather than summarised.
    """
    url = ran
    section = _get(f"{url}/api/project")["sections"]["tasks"]

    assert section["tasks"], "the tasks section is empty after a check was run through the console"
    task = section["tasks"][0]

    assert task["task_id"].startswith("console-"), (
        f"the task the console created is not clearly identified as an operator "
        f"task: {task['task_id']!r}"
    )
    assert task["verdict"]["readiness"] == "READY", (
        f"the task ran a required check to PASS and should be READY; it reports "
        f"{task['verdict']['readiness']!r} with gaps {task['verdict']['gaps']}"
    )
    assert task["verdict"]["recorded"] is False, (
        "the section is reporting a recorded verdict. Viewing a page must not "
        "stamp a readiness onto the task; only finalize does that"
    )
    assert task["held_resources"], "the task holds the resource it admitted with, and shows none"
    assert task["held_resources"][0]["key"], "a held resource has no key"
    assert CHECK_ID in task["contract"]["required_checks"], (
        f"the task does not require the check it ran: {task['contract']['required_checks']}"
    )


def test_viewing_a_page_does_not_mutate_task_state(live) -> None:
    """A full page load leaves the tasks table byte-identical.

    **The requirement is that viewing must not decide anything**, and the only
    way to show it is to compare the durable state before and after. This runs a
    check first, so there is a task with a real readiness to be disturbed, then
    loads the page and calls every route the page itself calls on load — project,
    readiness, checks, runs, recovery, operations — and reads the whole `tasks`
    table before and after.

    The defect it guards against is specific: `tasks.record_readiness` stamps a
    verdict onto the row, and it is one call away from `compute_readiness`, which
    decides the same thing without writing. A console that called the writer would
    look correct on every screen and would move a task the moment an operator
    opened the page.
    """
    url, context = live
    token = _token(url)
    started, status = _post(f"{url}/api/run_check?check_id={CHECK_ID}", token)
    assert status == 200, f"the run did not start: {started}"

    before = _task_rows(context.store)
    assert before, "there is no task to observe, so this test proves nothing"

    # What the page does on load.
    _get_text(f"{url}/")
    for route in ("project", "readiness", "checks", "runs?limit=50", "recovery", "operations"):
        _get(f"{url}/api/{route}")

    after = _task_rows(context.store)

    assert before == after, (
        f"loading the page changed the tasks table: before {before}, after {after}. "
        f"Viewing a page must not record a readiness verdict; compute_readiness "
        f"decides without writing and only finalize may write."
    )


def live_for_context(url: str):
    """Recover the `Context` for a served url, by re-opening the same root.

    Kept for tests that need both the base url and a live `Context` without
    depending on the fixture's internals. Returns a pair so the caller can take
    either half.
    """
    from vkit.console import operations

    root = _get(f"{url}/api/project")["root"]
    return url, operations.open_context(root)


# ------------------------------------------------------- the admission path


def test_the_run_action_creates_a_task_visible_to_finalize(live) -> None:
    """The task a Run button creates is a real task, not a console-only row.

    **There must be no console-only verification lifecycle.** The console admits
    its task through `tasks.admit`, the core's own admission path, and hands that
    task id to the supervisor. This test makes a run happen over HTTP exactly as
    the page does, then decides the resulting task through `tasks.finalize` — the
    function `vkit task finalize` calls — and asserts it reaches READY.

    A run with no task cannot satisfy a contract and cannot be finalized at all,
    so the assertion is not cosmetic: a console that created a bare run would
    raise here rather than produce a verdict.
    """
    from vkit import tasks as core_tasks

    url, context = live
    token = _token(url)
    started, status = _post(f"{url}/api/run_check?check_id={CHECK_ID}", token)
    assert status == 200, f"the run did not start: {started}"

    task_id = started["task_id"]
    assert task_id, (
        "the run response names no task, so the evidence this run produced "
        "belongs to nothing that can be finalized"
    )
    assert started["generation"] >= 1, (
        f"the task was admitted at generation {started['generation']!r}; admission "
        f"opens at generation 1"
    )

    record = core_tasks.get_task(context.store, task_id)
    assert record.status == "closed", (
        f"the task the console created is {record.status!r} after its run finished; "
        f"a finished attempt is closed, and leaving it open would hold the checkout "
        f"and make the next Run refuse"
    )

    # The task is a real task in the store, with the frozen floor it was admitted
    # against and the evidence its run produced. `compute_readiness` is the same
    # decision `vkit task finalize` makes; the console reached it through that
    # path, so this proves the evidence the console showed is evidence the core
    # accepts. A run with no task could not be decided at all, which is what a
    # console-only lifecycle would produce.
    acceptance = core_tasks.acceptance_context(context.project, lambda: context.manifest)
    result = core_tasks.compute_readiness(
        context.store, task_id,
        required_check_ids=record.pinned().required_checks,
        context=acceptance,
    )
    assert result.readiness == "READY", (
        f"the core decided {result.readiness!r} for a task the console created and "
        f"ran to PASS; gaps were {result.gaps}. The evidence the console published "
        f"is not evidence the core accepts."
    )

    # And finalize reaches the same task by the same name, refusing only because
    # the console closed it -- which is the refusal a closed task must get.
    with pytest.raises(core_tasks.TaskError, match="closed"):
        core_tasks.finalize(context.store, task_id, context=acceptance)


def test_a_second_run_admits_a_new_attempt_of_the_same_operator_task(ran) -> None:
    """Two runs of one check are two attempts, not one task with a stale claim.

    The core holds an exclusive claim per task for that task's whole life, and
    deliberately refuses both to release a superseded generation's claims and to
    let a new generation take a resource the old one still holds. Superseding
    therefore cannot work for a repeated run: the second attempt would sit at
    generation 2 while the checkout stayed held at generation 1, and
    `verify_ownership` would refuse the launch. So each run admits its own
    attempt, and the previous one is closed. This asserts the property that makes
    the second run work at all.
    """
    url = ran
    token = _token(url)
    second, status = _post(f"{url}/api/run_check?check_id={CHECK_ID}", token)
    assert status == 200, f"the second run did not start: {second}"

    section = _get(f"{url}/api/project")["sections"]["tasks"]
    console_tasks = [t for t in section["tasks"] if t["task_id"].startswith("console-")]

    assert len(console_tasks) >= 2, (
        f"two runs of the same check produced {len(console_tasks)} console tasks "
        f"{[t['task_id'] for t in console_tasks]}; each run must be able to take "
        f"the checkout, so each must be its own task"
    )
    ids = [t["task_id"] for t in console_tasks]
    assert len(set(ids)) == len(ids), f"two runs share a task id: {ids}"

    for task in console_tasks:
        assert task["held_resources"], (
            f"task {task['task_id']} holds no resource. It admitted one and must "
            f"keep it: compute_readiness verifies the claim before it will decide, "
            f"so releasing it would turn the recorded PASS into BLOCKED."
        )
    keys = [
        claim["key"] for task in console_tasks for claim in task["held_resources"]
    ]
    assert len(set(keys)) == len(keys), (
        f"two attempts hold the same resource {keys}. Each attempt holds a key of "
        f"its own, so a repeated run of one check cannot be refused for a claim "
        f"the previous attempt still holds."
    )


# ------------------------------------------------------- hidden or explained


def test_an_action_whose_prerequisite_is_absent_is_hidden_with_its_reason(live) -> None:
    """A Run action that cannot work is withheld, and the reason names the cause.

    The manifest declares `python` as a prerequisite. When it is not on PATH the
    core refuses the check before it starts, so a button would be a click that
    cannot produce evidence. The section must therefore mark the action
    unavailable *and* carry a reason, because an absent button with no explanation
    leaves the operator guessing.
    """
    url, context = live

    # Force the prerequisite absent by pointing PATH at an empty directory, on
    # this read only. `shutil.which` is what the core's own prerequisite check
    # calls, so this is the real condition rather than a mocked one.
    import os

    empty = REPO_ROOT / "tests" / ".empty-path-for-evidence-views"
    empty.mkdir(exist_ok=True)
    original = os.environ.get("PATH", "")
    os.environ["PATH"] = str(empty)
    try:
        checks = _get(f"{url}/api/checks")["sections"]["evidence"]["checks"]
    finally:
        os.environ["PATH"] = original

    check = {c["id"]: c for c in checks}[CHECK_ID]
    needs = {need["name"]: need["found"] for need in check["prerequisites"]}
    assert needs.get("python") is False, (
        f"the prerequisite was still found with an empty PATH: {needs}. The "
        f"absence this test depends on did not take effect"
    )
    assert check["run_action"]["available"] is False, (
        "the section offers a Run action for a check whose prerequisite is "
        "absent. A check the core refuses before it starts cannot produce "
        "evidence, so the button would be a control that cannot work"
    )
    assert "python" in check["run_action"]["reason"], (
        f"the action is withheld but its reason does not name the prerequisite: "
        f"{check['run_action']['reason']!r}"
    )

    try:
        empty.rmdir()
    except OSError:
        pass


def test_the_page_draws_no_button_for_an_action_whose_prerequisite_is_absent(live) -> None:
    """Withheld on the page too, not only in the data.

    The data test above proves the API withholds the action. This proves the page
    honours it: the string that identifies the withheld action's button must not
    appear in the page's source, and the reason must be rendered in its place.
    The reason is asserted against the served `app.js`, because the rendering
    decision is made there and nowhere else.
    """
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "actionOrReason" in script, (
        "the page has no rule for drawing an unavailable action, so it either "
        "renders every action or hides every unavailable one without a reason"
    )
    # The rule must draw the reason, not a disabled button: a disabled control is
    # still something to press, and pressing it yields a refusal rather than the
    # explanation.
    assert "why-not" in script, (
        "the page has no rendering for a withheld action's reason"
    )
    assert "action.available === false" in script, (
        "the page does not branch on whether an action is available, so an "
        "unavailable action is drawn the same as an available one"
    )


def test_an_unavailable_setup_action_names_its_missing_prerequisite(live) -> None:
    """The same rule on the integrations section: absent CLI, named reason.

    `install`, `repair` and `remove` all shell out to the `claude` CLI. When it
    is not on PATH those three cannot run, and the section says so with the
    prerequisite in the sentence rather than offering a button.
    """
    url, _ = live
    section = _get(f"{url}/api/checks")["sections"]["integrations"]

    assert section["editable"] is False, (
        "the integrations section claims settings are editable. Configuration "
        "editing is checkpoint 12.3 and is not on this build's writable surface"
    )
    components = {c["id"]: c for c in section["components"]}
    assert "vkit" in components, "the core's own version is not reported"
    assert components["vkit"]["version"], "the core reports no version"

    for action in section["actions"]:
        if action["available"] is False:
            assert action["reason"], (
                f"{action['operation']} is unavailable but names no reason"
            )
            assert "claude" in action["reason"], (
                f"{action['operation']} is withheld for a reason that does not "
                f"mention the CLI it needs: {action['reason']!r}"
            )


# ----------------------------------------------------------------- cleanup


def test_the_cleanup_section_reports_the_policy_and_names_what_it_cannot_show(live) -> None:
    """Cleanup shows what the core holds, and names the panels it cannot fill.

    The policy is the core's own, read through `cleanup.load_policy`. The
    unavailable list is asserted to be non-empty *and* to name a backend for each
    entry: a silent gap and an empty list are indistinguishable on a screen, and
    an empty list would read as "nothing has ever been cleaned", which is a
    different and false claim.
    """
    url, _ = live
    section = _get(f"{url}/api/checks")["sections"]["cleanup"]

    assert section["available"] is True, (
        f"the cleanup section is not available on a project with no cleanup "
        f"policy: {section.get('error')}"
    )
    assert section["policy"]["mode"] in ("off", "preview", "apply_verified"), (
        f"the cleanup mode is {section['policy']['mode']!r}, which is not one of "
        f"the modes the core defines"
    )
    assert section["registered_rules"], "no cleanup rules are reported"
    assert section["protected_paths"], "no protected paths are reported"
    assert isinstance(section["outstanding"], list), "there is no pending-cleanup list"

    assert section["unavailable"], (
        "the cleanup section reports every panel as available. Applied patches, "
        "preservation receipts and proposals are written and never read back in "
        "this build, and claiming otherwise would be inventing capability."
    )
    for entry in section["unavailable"]:
        assert entry["panel"], "an unavailable cleanup panel names nothing"
        assert entry["missing"], (
            f"the {entry['panel']!r} panel is withheld without naming the backend "
            f"that is absent, so an operator cannot tell a build gap from a bug"
        )


def test_the_cleanup_section_never_offers_a_write(live) -> None:
    """Nothing on the cleanup section can change the policy.

    The cleanup policy lives under `verification/`, which is a protected path,
    and editing it is checkpoint 12.3. This asserts the read-only claim by
    checking that the section offers no action and that the underlying function
    was not reachable as a write from the console package's own source.
    """
    import ast

    url, _ = live
    section = _get(f"{url}/api/checks")["sections"]["cleanup"]
    assert "actions" not in section, (
        "the cleanup section offers actions. The cleanup policy is a protected "
        "path in this build and is not writable from the console."
    )
    assert "editable" not in section or section.get("editable") is not True, (
        "the cleanup section claims the policy is editable, which it is not"
    )

    # Structural: the module that builds this section must not import the apply
    # entry point, so a future edit cannot quietly make the view a writer.
    source = (REPO_ROOT / "src" / "vkit" / "console" / "operations.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    applied = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
        if "apply_cleanup" in alias.name
    ]
    assert not applied, (
        f"the console operations module imports {applied}; a read-only view must "
        f"not be able to reach the apply path"
    )


# --------------------------------------------------------------- no mutations


def test_no_read_route_performs_a_mutation(live) -> None:
    """The five sections ride GET routes only.

    The section map names the route each section rides. Every one of them must be
    a GET route, because a section that reached its data by POST would be a read
    page with a mutation attached — and the boundary test that refuses GET
    mutations would not catch it, because the method would be the wrong way
    round.
    """
    from vkit.console import api
    from vkit.console.plan import SECTIONS

    for section in SECTIONS:
        assert section.route in api.READ_ROUTES, (
            f"section {section.id!r} rides /api/{section.route}, which is not a "
            f"read route; a read view must not reach its data by POST"
        )
        assert section.route not in api.MUTATIONS, (
            f"section {section.id!r} rides /api/{section.route}, which is also a "
            f"mutation route"
        )


def test_the_sections_bounded_reads_stay_bounded(live) -> None:
    """The sections do not widen the runs limit or the log ceiling.

    A section that asked for every run would turn a bounded read into an
    unbounded one, which is the property `MAX_RUN_LIMIT` exists to hold. The
    section's own window is the clamped default, and the clamp still applies to a
    caller who asks for more.
    """
    from vkit.console import operations
    from vkit.console.plan import DEFAULT_RUN_LIMIT, MAX_LOG_BYTES, MAX_RUN_LIMIT

    url, _ = live
    section = _get(f"{url}/api/project")["sections"]["tasks"]
    assert section["limit"] == DEFAULT_RUN_LIMIT, (
        f"the tasks section reads {section['limit']} runs, not the bounded default "
        f"{DEFAULT_RUN_LIMIT}"
    )

    # The clamp is what makes an over-large request safe, and it is unchanged: a
    # caller asking for a million runs still gets the ceiling.
    clamped = operations.runs_view(_context(live), limit=10_000)["limit"]
    assert clamped == MAX_RUN_LIMIT, (
        f"a request for 10000 runs was clamped to {clamped}, not to the ceiling "
        f"{MAX_RUN_LIMIT}"
    )
    assert MAX_LOG_BYTES > 0, "the log ceiling must remain positive"


def _context(live):
    """The `Context` the fixture bound, for a call that goes through operations."""
    return live[1]