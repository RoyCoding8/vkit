"""Plan 11 checkpoint 4: what cleanup did is durable, bounded, and listable.

Every test here drives the real path: a real repository, a real preview, a real
apply, and then a read of what was persisted. None of them restates a constant
the code under test contains, and none asserts that a function was called.

The load-bearing properties:

1. An applied cleanup is listed with its path, its rule, both digests, its
   preservation receipt and its outcome. The digests are asserted against the
   bytes on disk, not against the record's own fields.
2. A refusal is listed WITH its reason. `not_task_owned` is a refusal with a
   real cause, produced here by a file another task owns.
3. Records survive process exit. One process writes, a second reads.
4. The listing is bounded on read and the bound is enforced on write.
5. No file CONTENTS are persisted. The record is digests, locations and
   receipts, never source bytes.
6. An absent record reads as an empty list, not a synthesised row.

**No process is launched directly.** The cross-process test goes through
`tests/subproc.py`, which carries the Windows window-suppression keywords, so
`tests/test_subprocess_windows.py` finds no direct launch in this module.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import subproc  # noqa: E402

from vkit.cleanup import (  # noqa: E402
    CLEANUP_RULE_IDS,
    TRAILING_COMMENT_RULE,
    CleanupMode,
    CleanupPolicy,
    Proposal,
    apply_comment_cleanup as _apply_comment_cleanup,
    preview_comment_cleanup,
)
from vkit.cleanup.records import (  # noqa: E402
    MAX_RECORDS,
    cleanup_records,
    cleanup_summary,
)
from vkit.paths import open_project  # noqa: E402
from vkit.storage import Store  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "python-cli"

APPLY_POLICY = CleanupPolicy(
    mode=CleanupMode.APPLY_VERIFIED, enabled_rules=CLEANUP_RULE_IDS
)


def _test_ownership(_request) -> None:
    """Record tests explicitly stand in for a task that still owns the file."""


def apply_comment_cleanup(*args, ownership_check=_test_ownership, **kwargs):
    return _apply_comment_cleanup(*args, ownership_check=ownership_check, **kwargs)

#: The source line the comment rule is allowed to touch, written out so the
#: digest assertions below are against a value this test chose rather than one
#: the code produced.
SOURCE = b"def bump(total):\n    total = total + 1  # increment the counter\n    return total\n"
CLEANED = b"def bump(total):\n    total = total + 1\n    return total\n"


def run_git(*args: str, cwd: Path) -> None:
    subproc.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def project_with(tmp_path: Path, relative: str, content: bytes):
    """A committed one-file repository, resolved the way the product resolves it."""
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    run_git("init", "-q", cwd=root)
    run_git("add", "-A", cwd=root)
    run_git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "fixture", cwd=root)
    return open_project(root)


def example_project(tmp_path: Path, name: str = "repo") -> Path:
    """A committed copy of the example repository, which is what `open_project` needs."""
    root = tmp_path / name
    shutil.copytree(EXAMPLE, root)
    run_git("init", "-q", cwd=root)
    run_git("add", "-A", cwd=root)
    run_git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "example", cwd=root)
    return root




def test_an_applied_cleanup_is_listed_with_its_path_rule_digests_and_receipt(
    tmp_path: Path,
) -> None:
    """The first thing an operator asks: what did cleanup change, and on what authority.

    The digests are recomputed from the bytes on disk rather than read off the
    record, so the assertion is that the record describes the real file and not
    that it describes itself.
    """
    project = project_with(tmp_path, "sample.py", SOURCE)
    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    assert isinstance(proposal, Proposal), proposal

    applied = apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY,
    )

    listed = cleanup_records(project)
    assert len(listed) == 1, f"expected one record, got {listed}"

    record = listed[0]
    assert record.relative_path == "sample.py"
    assert record.rule_id == TRAILING_COMMENT_RULE
    assert record.outcome == "applied"
    assert record.reason is None, "an applied cleanup has no refusal reason to name"

    assert record.after_digest == hashlib.sha256(CLEANED).hexdigest(), (
        f"the record's after digest does not describe the file on disk, which now "
        f"holds {CLEANED!r}"
    )
    assert record.before_digest == hashlib.sha256(SOURCE).hexdigest(), (
        "the record's before digest does not describe the original bytes"
    )

    assert record.receipt["result"] == "PASS"
    assert record.receipt["beforeDigest"] == record.before_digest
    assert record.receipt["afterDigest"] == record.after_digest

    assert record.policy_digest == APPLY_POLICY.digest, (
        "the record does not name the policy in force, so an operator cannot tell "
        "which approved rules authorized this"
    )
    assert record.generation == 1
    assert record.proposal_id == proposal.proposal_id
    assert record.artifact_path == applied.artifact_path, (
        "the record names no preserved original, so the write is not reversible"
    )

    assert record.sites, "an applied cleanup recorded no site to point at"
    assert record.sites[0]["line"] == 2


def test_a_refusal_is_listed_with_the_reason_that_caused_it(tmp_path: Path) -> None:
    """A file another task owns is left alone, and the dashboard says why.

    `not_task_owned` is a refusal with a real cause. The assertion is on the
    reason string an operator would read, not on the fact that a refusal
    happened: a list saying "refused" with nothing after it is the empty-list
    problem wearing a different hat.
    """
    project = project_with(tmp_path, "sample.py", SOURCE)
    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    assert isinstance(proposal, Proposal), proposal

    apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY,
        ownership_check=lambda request: (
            f"task {request.generation} does not own sample.py; "
            "another task holds the claim"
        ),
    )

    assert (project.root / "sample.py").read_bytes() == SOURCE, (
        "a refused cleanup changed the file anyway"
    )

    listed = cleanup_records(project)
    assert len(listed) == 1, f"expected one record, got {listed}"

    record = listed[0]
    assert record.outcome == "refused"
    assert record.reason == "not_task_owned"
    assert "another task holds the claim" in record.detail, (
        f"the refusal names no cause an operator can act on: {record.detail!r}"
    )
    assert record.after_digest is None, (
        "a refusal recorded an after digest, which claims a write that did not happen"
    )
    assert record.receipt == {}, "a refusal recorded a preservation receipt it never earned"


def test_a_tampered_receipt_is_refused_and_the_tampering_is_named(tmp_path: Path) -> None:
    """A receipt that no longer reproduces from the bytes is the plan's refusal row.

    The record must carry that reason, because the operator's question is "why
    was my file left alone" and "something was refused" does not answer it.
    """
    from vkit.cleanup import apply_cleanup as _apply_cleanup, ApplyRequest

    project = project_with(tmp_path, "sample.py", SOURCE)
    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    assert isinstance(proposal, Proposal), proposal

    tampered = replace(proposal, after_bytes=proposal.after_bytes.replace(
        b"total = total + 1", b"total = total + 2"
    ))
    _apply_cleanup(
        project,
        ApplyRequest(
            request_id="req-tampered", proposal=tampered, generation=1, policy=APPLY_POLICY,
        ),
        ownership_check=_test_ownership,
    )

    record = cleanup_records(project)[0]
    assert record.outcome == "refused"
    assert record.reason in ("preservation_failed", "bytes_outside_spans_changed"), (
        f"the refusal does not name the preservation failure that caused it: {record.reason!r}"
    )
    assert record.detail, "the refusal records no detail an operator can act on"
    assert (project.root / "sample.py").read_bytes() == SOURCE


def test_a_changed_file_is_refused_and_the_record_names_the_digests_that_differed(
    tmp_path: Path,
) -> None:
    """The plan's row: before bytes changed, no overwrite.

    The digests are the point. A refusal record that carried only the proposal's
    EXPECTED digest, rendered as "this file was X", would state the opposite of
    the truth on the one refusal an operator most needs to read. The recorded
    digest must be the one the file actually held.
    """
    project = project_with(tmp_path, "sample.py", SOURCE)
    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    assert isinstance(proposal, Proposal), proposal

    edited = b"def bump(total):\n    total = total + 100  # someone else\n    return total\n"
    (project.root / "sample.py").write_bytes(edited)

    result = apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY,
    )
    assert result.reason == "before_bytes_changed"

    record = cleanup_records(project)[0]
    assert record.outcome == "refused"
    assert record.reason == "before_bytes_changed"
    assert record.before_digest == hashlib.sha256(edited).hexdigest(), (
        "the refusal recorded the digest the proposal EXPECTED rather than the "
        "bytes the file actually held, so the console would describe a file that "
        "does not exist"
    )
    assert record.before_digest != proposal.before_digest
    assert record.after_digest is None
    assert (project.root / "sample.py").read_bytes() == edited, (
        "a refused cleanup overwrote an edit it did not make"
    )




def test_records_survive_a_new_process(tmp_path: Path) -> None:
    """One process applies, a second reads. The record is not in-process state.

    The reading process opens the store itself from `paths.db_path`, which is
    what the console does, so this is the same read the dashboard performs.
    """
    project = project_with(tmp_path, "sample.py", SOURCE)
    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    assert isinstance(proposal, Proposal), proposal
    apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY,
    )

    reader = (
        "import json,sys;"
        "sys.path.insert(0, %r);"
        "from pathlib import Path;"
        "from vkit.cleanup.records import cleanup_records;"
        "from vkit.paths import open_project;"
        "print(json.dumps([{'path': r.relative_path, 'outcome': r.outcome,"
        " 'after': r.after_digest, 'rule': r.rule_id,"
        " 'receipt': r.receipt.get('result')}"
        " for r in cleanup_records(open_project(%r))]))"
        % (str(REPO_ROOT / "src"), str(project.root))
    )
    done = subproc.run(
        [sys.executable, "-c", reader],
        capture_output=True, encoding="utf-8", errors="replace", check=True,
    )

    read = json.loads(done.stdout)
    assert len(read) == 1, f"the second process read {len(read)} records"
    assert read[0]["path"] == "sample.py"
    assert read[0]["outcome"] == "applied"
    assert read[0]["rule"] == TRAILING_COMMENT_RULE
    assert read[0]["receipt"] == "PASS"
    assert read[0]["after"] == hashlib.sha256(CLEANED).hexdigest(), (
        "the digest did not survive the process that wrote it"
    )




def test_the_bound_is_enforced_on_write_and_the_listing_is_bounded_on_read(
    tmp_path: Path,
) -> None:
    """The table cannot grow without limit, and the read cannot return all of it.

    `MAX_RECORDS` rows are written, then more, and the count stays at the bound
    with the NEWEST surviving: an unbounded log in a state directory is the
    thing that fills a disk at 3am.
    """
    project = project_with(tmp_path, "sample.py", SOURCE)
    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    assert isinstance(proposal, Proposal), proposal

    for index in range(MAX_RECORDS + 5):
        result = apply_comment_cleanup(
            project, proposal,
            request_id=f"req-{index}", generation=1, policy=APPLY_POLICY,
        )
        assert result is not None

    store = Store(project.db_path)
    with store._connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM cleanup_records").fetchone()[0]
    assert total == MAX_RECORDS, (
        f"the table holds {total} rows after {MAX_RECORDS + 5} outcomes; the write "
        f"bound is not being enforced"
    )

    listed = cleanup_records(project, limit=MAX_RECORDS)
    assert len(listed) == MAX_RECORDS
    expected = [f"req-{index}" for index in range(MAX_RECORDS + 4, 4, -1)]
    assert [record.request_id for record in listed] == expected, (
        "the listing is not newest-first in insertion order"
    )

    assert len(cleanup_records(project)) < MAX_RECORDS
    assert len(cleanup_records(project, limit=3)) == 3, (
        "the listing returned every row despite being asked for three"
    )


def test_the_bound_is_a_number_a_reader_can_check(tmp_path: Path) -> None:
    """`MAX_RECORDS` is the ceiling, and the listing reports when it truncated.

    A bounded read that silently returns fewer rows leaves the reader unable to
    tell "that is everything" from "there is more", which is the same honest-gap
    failure the console was fixed for.
    """
    project = project_with(tmp_path, "sample.py", SOURCE)
    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    assert isinstance(proposal, Proposal), proposal

    for index in range(4):
        apply_comment_cleanup(
            project, proposal, request_id=f"req-{index}", generation=1, policy=APPLY_POLICY,
        )

    view = cleanup_records(project, limit=2)
    assert len(view) == 2
    assert view.total == 4, "the view does not report how many records exist in all"




def test_no_source_bytes_are_persisted(tmp_path: Path) -> None:
    """The record holds digests and locations. It never holds the file.

    Checkpoint 11.2 preserves the original bytes as an artifact, so a copy of
    them inside the record would be a second copy of a thing that already has
    one, in a table nobody prunes.
    """
    project = project_with(tmp_path, "sample.py", SOURCE)
    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    assert isinstance(proposal, Proposal), proposal
    apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY,
    )

    db = project.db_path.read_bytes()
    text = db.decode("utf-8", errors="replace")

    for fragment in (
        "def bump",
        "increment the counter",
        "total = total + 1",
        SOURCE.decode("utf-8").strip(),
    ):
        assert fragment not in text, (
            f"the state database holds the source text {fragment!r}; a cleanup record "
            "stores digests and receipts, not file contents"
        )

    record = cleanup_records(project)[0]
    assert record.before_digest == hashlib.sha256(SOURCE).hexdigest()

    assert record.sites[0]["line"] == 2
    assert "text" not in record.sites[0], (
        "a recorded site carries the comment text, which is file content"
    )


def test_an_absent_record_reads_as_an_empty_list_not_a_placeholder(tmp_path: Path) -> None:
    """A project that has never cleaned anything reports nothing, and says so.

    Not a synthesised row. A placeholder is indistinguishable from a real
    outcome, which is the failure checkpoint 12.2 was written to prevent.
    """
    project = project_with(tmp_path, "sample.py", SOURCE)

    view = cleanup_records(project)
    assert list(view) == [], "a project with no cleanup returned a record"
    assert view.total == 0
    assert view.truncated is False


def test_a_repeated_request_is_recorded_without_claiming_a_second_application(
    tmp_path: Path,
) -> None:
    """An `AlreadyApplied` is a retry of one request, not a second cleanup.

    It gets a record, because an operator asking what happened to a file should
    see that the host asked twice. It does not go in the applied list, because
    two applied rows for one edit is a claim that something cleaned the file
    twice, and the count under the panel would then be the false one.
    """
    project = project_with(tmp_path, "sample.py", SOURCE)
    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    assert isinstance(proposal, Proposal), proposal

    apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY,
    )
    second = apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY,
    )
    assert type(second).__name__ == "AlreadyApplied", (
        f"the repeated request reported {type(second).__name__}, so the retry did "
        "not converge"
    )
    summary = cleanup_summary(project)
    outcomes = [record.outcome for record in cleanup_records(project)]
    assert outcomes == ["already_applied", "applied"], (
        f"the two outcomes recorded were {outcomes}; a retry must be recorded as "
        "its own outcome rather than as another application"
    )
    assert len(summary["applied"]) == 1, (
        "a repeated request was counted as a second application, so the panel "
        "claims one edit was applied twice"
    )
    assert summary["applied_total"] == 1
    assert summary["already_applied_total"] == 1
    assert summary["total"] == 2, "both outcomes should be recorded"
    assert summary["refusal_total"] == 0


def test_a_policy_that_will_not_parse_still_serves_the_record_panels(tmp_path) -> None:
    """An unreadable policy must not blank the panels the record fills.

    A broken `verification/cleanup.json` is reported through `problem`, not by
    failing the read, so the section takes its healthy path with a policy of
    `OFF_POLICY`. It must still carry every history key: the two returns of
    `cleanup_section` having different shapes is how a page ends up rendering a
    history panel as `undefined` on the one configuration an operator is most
    likely to be staring at while fixing it.
    """
    import json

    from vkit.console import operations

    root = example_project(tmp_path / "case", "broken")
    (root / "verification" / "cleanup.json").write_text(
        json.dumps({"mode": "apply_everything", "enabled_rules": ["NOPE"]}) + "\n",
        encoding="utf-8",
    )
    context = operations.open_context(root)

    section = operations.cleanup_section(context)

    assert section["problem"], "an unusable policy names no problem"
    assert "cleanup.json" in section["problem"]
    assert section["policy"]["mode"] == "off", (
        "an unusable policy must read as off; absence of a usable policy is not "
        "permission"
    )
    for panel in (
        "applied", "refusals", "applied_total", "refusal_total",
        "already_applied_total", "total", "truncated", "limit",
    ):
        assert panel in section, (
            f"the section omits {panel!r} when the policy is unusable, so the page "
            "renders a history panel as undefined"
        )
    assert "unavailable" not in section, (
        "the section re-advertises absent panels on the unhealthy path, which is "
        "where an operator is most likely to be reading them"
    )


def test_the_record_table_migrates_onto_a_database_an_older_build_wrote(tmp_path) -> None:
    """Migration 7 is additive, and every earlier migration still applies.

    A database at version 6 was written by a build with no cleanup records. Opening
    it must reach version 7 without touching a row an existing table holds, and
    the tables from migrations 1 to 6 must all still be there afterwards. This is
    the plan 01 property in miniature: evidence a shipped build produced is still
    readable after a later migration.
    """
    from vkit.storage import MIGRATIONS, Store

    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    for version, sql in MIGRATIONS:
        if version > 6:
            break
        connection.executescript(
            sql.rstrip().rstrip(";") + f"; INSERT INTO schema_version (version) VALUES ({version});"
        )
    connection.execute(
        "INSERT INTO runs (run_id, check_id, lifecycle, source_json,"
        " configuration_digest, registered_at)"
        " VALUES ('r-old', 'totals-behavior', 'terminal', '{}', 'digest', 'then')"
    )
    connection.close()

    store = Store(path)

    assert store.version() == 7, (
        f"a database at version 6 opened at version {store.version()}; the "
        "cleanup table did not migrate"
    )
    assert [run["run_id"] for run in store.list_runs(limit=5)] == ["r-old"], (
        "the migration dropped a run an earlier build recorded"
    )
    tables = {
        row[0] for row in sqlite3.connect(path).execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    for table in ("runs", "tasks", "claim_holders", "claim_members", "launches",
                  "run_intents", "cancel_intents", "request_keys", "acceptances",
                  "cleanup_records"):
        assert table in tables, f"the migration left {table!r} absent"


def test_an_outcome_the_schema_does_not_define_is_refused_by_the_database(tmp_path) -> None:
    """The outcome is a closed set of three, enforced where the rows live.

    A caller branches on this value, so a fourth one has to fail at the boundary
    rather than reach a branch nobody reviewed. The rejected insert must also
    leave nothing behind: the trim shares the transaction, and a partial write
    would be a record describing an outcome that does not exist.
    """
    from vkit.storage import Store

    store = Store(tmp_path / "state.sqlite3")
    row = {
        "relative_path": "a.py", "outcome": "cleaned", "rule_id": "R",
        "request_id": "q", "proposal_id": "p", "generation": 1,
        "policy_digest": "d", "before_digest": "b", "after_digest": None,
        "reason": None, "detail": None, "receipt": {}, "sites": [],
        "artifact_path": None,
    }

    with pytest.raises(sqlite3.IntegrityError):
        store.record_cleanup(row, keep=500)

    assert store.list_cleanup(limit=5)[1] == 0, (
        "the rejected insert left a row behind, so the table describes an outcome "
        "no code path can produce"
    )




@pytest.fixture(scope="module")
def live_console(tmp_path_factory: pytest.TempPathFactory):
    """A real console over a real repository, with the example's manifest."""
    from vkit.console import operations, server

    root = tmp_path_factory.mktemp("cleanup-records") / "space repo"
    example_project(root.parent, root.name)
    context = operations.open_context(root)
    console, thread = server.start_in_thread(context, port=0)
    try:
        yield f"http://127.0.0.1:{console.server_address[1]}", context
    finally:
        console.shutdown()
        console.server_close()
        thread.join(timeout=10)


def test_the_console_shows_the_records_and_names_no_absent_panel(live_console) -> None:
    """The four panels are real, so the "not available in this build" list is empty.

    Checkpoint 12.2 named these four as absent because there was nothing behind
    them. Asserting the key is GONE is the assertion that the backend landed, and
    it has to stay: a page still advertising panels it now fills would tell an
    operator to distrust data it is showing them. Asserting the list is empty
    would not do, because a section that had deleted the panels along with the
    key would pass that too.
    """
    url, context = live_console

    with urllib.request.urlopen(f"{url}/api/checks", timeout=60) as response:
        section = json.loads(response.read().decode("utf-8"))["sections"]["cleanup"]

    assert "unavailable" not in section, (
        "the cleanup section still carries an `unavailable` key, so the page "
        "advertises panels it now fills"
    )
    for panel in ("applied", "refusals", "applied_total", "refusal_total"):
        assert panel in section, (
            f"the cleanup section reports no {panel!r}; the record is persisted but "
            "the console has no panel for it"
        )
    assert section["applied"] == [], (
        "a repository nothing has cleaned reports applied work, which is a "
        "placeholder row standing in for a real record"
    )
    assert section["refusals"] == []
    assert section["applied_total"] == 0


def test_the_console_renders_a_refusal_reason_from_the_record(live_console) -> None:
    """What a refusal looked like, with the cause, on the real response body.

    A panel that listed refusals without their reasons would be the empty-list
    problem again: a reader would see that files were left alone and not why.
    """
    url, context = live_console
    root = context.project.root
    (root / "left_alone.py").write_bytes(SOURCE)
    run_git("add", "-A", cwd=root)
    run_git(
        "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "left alone",
        cwd=root,
    )

    project = open_project(root)
    proposal = preview_comment_cleanup(project, "left_alone.py", changed_lines=[2])
    assert isinstance(proposal, Proposal), proposal
    apply_comment_cleanup(
        project, proposal, request_id="req-refused", generation=1, policy=APPLY_POLICY,
        ownership_check=lambda request: "task 7 owns left_alone.py, not this one",
    )

    with urllib.request.urlopen(f"{url}/api/checks", timeout=60) as response:
        section = json.loads(response.read().decode("utf-8"))["sections"]["cleanup"]

    refusals = [
        entry for entry in section["refusals"]
        if entry["path"] == "left_alone.py"
    ]
    assert len(refusals) == 1, f"the console listed {section['refusals']}"
    refusal = refusals[0]
    assert refusal["reason"] == "not_task_owned"
    assert "task 7 owns left_alone.py" in refusal["detail"], (
        f"the console listed a refusal without its cause: {refusal!r}"
    )
    assert refusal["beforeDigest"] == hashlib.sha256(SOURCE).hexdigest(), (
        "the refusal the console shows describes bytes that are not the file's"
    )
    assert refusal["afterDigest"] is None, (
        "the console listed a refusal carrying an after digest, which claims a "
        "write that did not happen"
    )
