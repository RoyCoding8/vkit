"""Interruption, resumption, and teardown, against the integration operation.

Three of the plan's rows live here, and each is a claim about what survives.

**A coordinator that stops and resumes.** The operation records its work under
the Git common directory, not in the working tree, and every recorded fact is
re-readable after the process that wrote it is gone. The test verifies a
candidate, kills nothing, starts a second verification from a fresh process, and
shows the first one's acceptance and its runs are still there with the same
identities -- no completed work is replayed and no run is re-executed.

**A checkout with uncommitted work is not silently destroyed.** The integration
checkout is created and retired by this operation, so the row is about the
teardown path: a checkout that still holds uncommitted work is refused for
retirement, the refusal names the preserved paths, and the tree is still there
afterwards. Losing a developer's work to a cleanup step is not an acceptable
failure direction.

**A live process holds its resource until it is reconciled.** The verification
scope is a claim, and a run that is still executing keeps it. The test holds a
claim from a live process, shows a second integration verification is refused
for the same repository, and shows the claim survives the holder exiting without
an explicit release.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import fixtures as fx  # noqa: E402
from test_integration import (  # noqa: E402
    EXIT_BLOCKED, EXIT_OK, passing_manifest_only_candidate, verify,
)

HOLD_SOURCE = '''\
"""A client that holds a claim until it is told to let go."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, {src!r})

from vkit.claims import ResourceSpec, acquire
from vkit.storage import Store

db_path, key, kind, seconds = sys.argv[1:5]
store = Store(Path(db_path))
if kind == "exclusive":
    spec = ResourceSpec(key, "exclusive")
else:
    spec = ResourceSpec(key, "capacity", capacity=1)
acquire(store, "holder", 1, [spec])
print(json.dumps({{"status": "holding"}}), flush=True)
time.sleep(float(seconds))
# Deliberately no release: the point is that the claim outlives the process
# unless something reconciles it.
'''


def test_a_coordinator_that_stops_and_resumes_finds_its_work_and_replays_none(
    tmp_path: Path,
) -> None:
    """The plan's ninth row.

    One verification runs and records. A second, separate process runs the same
    command for a second candidate. The first one's acceptance, its run reports
    and its source identities are all still readable, unchanged, and the second
    run did not touch the first run's evidence.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)
    first_candidate = passing_manifest_only_candidate(repo)

    done, first = verify(repo, first_candidate, target, "@main")
    assert done.returncode == EXIT_OK, first
    first_run = first["checks"][0]["run_id"]

    # A second candidate, in a second process, after the first has finished.
    second_candidate = passing_manifest_only_candidate(repo)
    second_done, second = verify(repo, second_candidate, target, "@main")
    assert second_done.returncode == EXIT_OK, second
    assert second["checks"][0]["run_id"] != first_run

    from vkit.paths import open_project
    from vkit.storage import Store

    store = Store(open_project(repo).db_path)

    # The first acceptance is still there, exactly as it was recorded. Nothing
    # replayed it and nothing overwrote it.
    recorded = store.load_acceptance(first["acceptance_id"])
    assert recorded is not None
    assert recorded["decision"] == "ACCEPTED"
    assert recorded["candidate"] == first_candidate
    assert recorded["checks"][0]["run_id"] == first_run

    # Its run report is still there, with the same source head, and it is still
    # terminal: a resume reads the decision rather than re-running the check.
    report = store.load(first_run)
    assert report["source"]["head"] == first_candidate
    assert report["lifecycle"] == "terminal"
    assert report["outcome"]["result"] == "PASS"

    # Both decisions are in the ledger, and the newer one is listed first.
    assert len(store.list_acceptances()) == 2
    assert store.list_acceptances()[0]["acceptance_id"] == second["acceptance_id"]

    # The evidence lives under the Git common directory, so it is not in the
    # working tree and a `git clean` cannot reach it.
    runs = open_project(repo).runs_root
    assert (runs / first_run / "report.json").is_file()
    assert not (repo / ".verification-kit").exists()


def test_a_checkout_with_uncommitted_work_is_not_destroyed(tmp_path: Path) -> None:
    """The plan's eleventh row.

    Teardown must never be the thing that loses work. A candidate checkout that
    still holds uncommitted changes is refused for retirement, the refusal names
    what is in it, and the directory is still on disk afterwards.
    """
    from vkit.integration import checkout as checkouts
    from vkit.paths import open_project
    from vkit.integration.checkout import CheckoutError

    repo = fx.build_repository(tmp_path)
    project = open_project(repo)
    target = fx.baseline_revision(repo)

    candidate_checkout = checkouts.create(project, target, "preserve-test")
    assert candidate_checkout.path.is_dir()

    # Something a developer would not want thrown away.
    work = candidate_checkout.path / "notes.md"
    work.write_text("half-finished thinking\n", encoding="utf-8")
    assert checkouts.dirty_paths(candidate_checkout) == ["notes.md"]

    with pytest.raises(CheckoutError) as refusal:
        checkouts.retire(candidate_checkout)
    assert "notes.md" in str(refusal.value)
    assert "uncommitted work" in str(refusal.value)

    # The work is still there. This is the whole property.
    assert work.is_file()
    assert work.read_text(encoding="utf-8") == "half-finished thinking\n"
    assert candidate_checkout.path.is_dir()

    # Once the work is committed, or the checkout is clean, retirement works and
    # the Git administrative record is cleaned with it.
    fx.git(candidate_checkout.path, "add", "-A")
    fx.git(candidate_checkout.path, "commit", "-q", "-m", "keep the notes")
    checkouts.retire(candidate_checkout)
    assert not candidate_checkout.path.exists()
    listed = fx.git(project.root, "worktree", "list", "--porcelain")
    assert str(candidate_checkout.path).replace("\\", "/") not in listed.replace("\\", "/")


def test_a_second_verification_is_refused_while_one_holds_the_repository(
    tmp_path: Path,
) -> None:
    """One integration verification per repository at a time.

    The verification scope is an exclusive claim taken before any process
    starts. A second verification of the same repository is refused with the
    holder named, rather than both running against the same candidate checkout.
    """
    from vkit.claims import ResourceSpec, acquire
    from vkit.integration import concurrency
    from vkit.paths import open_project
    from vkit.storage import Store

    repo = fx.build_repository(tmp_path)
    project = open_project(repo)
    target = fx.baseline_revision(repo)
    candidate = passing_manifest_only_candidate(repo)

    store = Store(project.db_path)
    key = concurrency.scope_spec("integration-verify").key
    acquire(store, "another-integration", 1, [ResourceSpec(key, concurrency.EXCLUSIVE)])

    done, record = verify(repo, candidate, target, "@main")
    assert done.returncode == EXIT_BLOCKED, record
    assert record["decision"] == "BLOCKED"
    assert record["checks"] == []
    assert any("another-integration" in gap for gap in record["gaps"]), record["gaps"]


def test_a_claim_outlives_the_process_that_took_it_until_it_is_reconciled(
    tmp_path: Path,
) -> None:
    """The plan's tenth row.

    A live process takes the verification scope. The process exits WITHOUT
    releasing it. The claim is still there, so a resource whose owner may still
    be doing something is not handed to someone else on the strength of a pid
    no longer existing.
    """
    from vkit.integration import concurrency
    from vkit.paths import open_project
    from vkit.storage import Store

    repo = fx.build_repository(tmp_path)
    project = open_project(repo)
    db_path = project.db_path
    store = Store(db_path)

    holder_script = tmp_path / "holder.py"
    holder_script.write_text(HOLD_SOURCE.format(src=str(SRC)), encoding="utf-8")

    key = concurrency.scope_spec("integration-verify").key
    proc = subprocess.Popen(
        [sys.executable, str(holder_script), str(db_path), key, "exclusive", "1.5"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    # Wait for the holder to report that it actually holds the claim.
    line = proc.stdout.readline()
    assert json.loads(line)["status"] == "holding"

    from vkit.claims import holder as current_holder

    held = current_holder(store, key)
    assert held is not None
    assert held.task_id == "holder"

    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err

    # The process is gone and the claim is still held. Time alone does not
    # justify reassigning it; `vkit recover` is the explicit reconciliation.
    after = current_holder(store, key)
    assert after is not None, "the claim was dropped when its process exited"
    assert after.task_id == "holder"
