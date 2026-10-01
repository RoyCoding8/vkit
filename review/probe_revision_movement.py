"""Which kind of "the project moved to a new revision" is visible to acceptance.

`formal/reference.py` carried two comments about the same behaviour, and they
said opposite things. `_latest_by_check` called it a live disagreement with the
TLA+ model's Property4; `acceptable` called it an agreement, with the divergence
"narrower than it was". Both describe `_latest_by_check` not filtering by
revision. A comment asserting a fact is the defect class R5 exists to remove, so
this probe measures the fact instead of choosing between the two sentences.

The phrase "moves to a new revision" is ambiguous because git admits at least
three different movements, and the product treats them differently:

  1. HEAD only. An empty commit moves `SourceIdentity.head` and changes no file.
  2. Content. Editing a tracked file and committing moves `inventory_digest`.
  3. Policy. Rewriting `verification/manifest.json` and re-running under it moves
     the policy digest, and the digest the decision compares against is the one
     pinned at ADMISSION.

The first is the one `COMPARED_IDENTITIES` cannot see: it compares
`source_inventory_digest`, and `source_unchanged` says in so many words that "the
digest decides, and HEAD deliberately does not". So an empty commit is invisible
to acceptance, a content change is visible, and a policy change is visible only
once evidence answers it, and then only against the pinned digest.

Rewriting the manifest WITHOUT re-running is a fourth thing, and it is the one
that is easy to misreport as the third. The manifest is a tracked file, so
rewriting it moves the SOURCE digest, and the recorded run still carries the
digest it was measured under, which is the policy this attempt was admitted
under. The gap therefore names source and no policy gap exists, because the
recorded run genuinely does answer the contract the attempt was admitted under.
Re-running under the new policy repairs the source gap and raises the policy one
instead, which is the pair of rows 3 and 4. This probe measures both rather than
assuming either.

Every row is a real `finalize` over a real `Store` on a real git checkout, run
through the production path. Each case opens by reaching READY on the same
evidence and then changes exactly one thing, so a verdict of BLOCKED is a
statement about that movement and not about a store where nothing can pass.

    python review/probe_revision_movement.py

Writes `review/revision-movement.json` and prints the table. Exits 0 when the
measured verdicts are the ones this docstring claims, and 1 when they are not,
so the docstring cannot go stale without the probe going red.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import fixtures  # noqa: E402

from vkit import tasks  # noqa: E402
from vkit.claims import holder  # noqa: E402
from vkit.execution import run_check  # noqa: E402
from vkit.identity import compute_source_identity  # noqa: E402
from vkit.manifest import parse_manifest  # noqa: E402
from vkit.paths import open_project  # noqa: E402
from vkit.storage import Store  # noqa: E402

EXAMPLE = ROOT / "examples" / "python-cli"
MANIFEST_RELATIVE = "verification/manifest.json"
CHECK_ID = "totals-behavior"
TASK = "t"


def _checks() -> list[dict]:
    return json.loads((EXAMPLE / MANIFEST_RELATIVE).read_text(encoding="utf-8"))["checks"]


def _project(root: Path):
    """A committed checkout of the shipped example carrying the shipped policy."""
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(EXAMPLE, root, dirs_exist_ok=True)
    fixtures.git(root, "init", "-q", "-b", "main")
    fixtures.git(root, "config", "user.email", "t@example.invalid")
    fixtures.git(root, "config", "user.name", "Revision Probe")
    fixtures.commit_all(root, "the policy under test")
    return open_project(root)


def _context(project):
    """Read through the product's own builder, so the identities are measured."""
    return tasks.acceptance_context(
        project, lambda: parse_manifest(project, project.runs_root)
    )


def _run(store, project) -> None:
    run_check(
        parse_manifest(project, project.runs_root), CHECK_ID, store=store,
        source=compute_source_identity(project), task_id=TASK, attempt=1,
    )


def _edit_a_declared_input(project) -> None:
    target = project.root / "src" / "totals.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n# an edit after the recorded run\n",
        encoding="utf-8",
    )
    fixtures.commit_all(project.root, "an edit to a declared input")


def _add_a_check(project) -> None:
    """The strongest form of a policy change: evidence re-gathered under it."""
    first = _checks()[0]
    changed = [*_checks(), {**first, "id": "second-behavior"}]
    fixtures.write_json(
        project.root / MANIFEST_RELATIVE, {"schema_version": 1, "checks": changed}
    )
    fixtures.commit_all(project.root, "a second check in the policy")


def _ready(tmp: Path, name: str) -> tuple[Store, object, object]:
    """Admit, run, and reach READY. The control every case below departs from."""
    project = _project(tmp / name)
    store = Store(project.db_path)
    context = _context(project)
    assert context.usable, context.refusal
    tasks.admit(
        store, TASK, context=context, required_checks=[CHECK_ID],
        resources=[{"key": "repository", "kind": "exclusive"}],
    )
    _run(store, project)
    verdict = tasks.finalize(store, TASK, context=context)
    assert verdict.readiness == "READY", f"control was {verdict.readiness}: {list(verdict.gaps)}"
    return store, project, context


def _row(case: str, movement: str, store, project, before, admitted) -> dict:
    """What the identities did, then what the decision said about it.

    `before` is the source identity as it stood when the control run was
    recorded, so the two halves of the row are the same measurement taken on
    either side of the movement.
    """
    after = compute_source_identity(project)
    context = _context(project)
    assert context.usable, context.refusal
    verdict = tasks.finalize(store, TASK, context=context)
    pinned = tasks.get_task(store, TASK).pinned().policy_digest
    return {
        "case": case,
        "movement": movement,
        "head_before": before.head,
        "head_after": after.head,
        "head_moved": before.head != after.head,
        "inventory_before": before.inventory_digest,
        "inventory_after": after.inventory_digest,
        "inventory_moved": before.inventory_digest != after.inventory_digest,
        "policy_pinned_at_admission": admitted,
        "policy_in_force": context.policy_digest,
        "policy_moved": admitted != context.policy_digest,
        "pinned_follows_the_manifest": pinned == context.policy_digest,
        "readiness": verdict.readiness,
        "gaps": list(verdict.gaps),
        "readiness_recorded_on_task": tasks.get_task(store, TASK).readiness,
    }


def main() -> int:
    rows: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="vkit-revision-") as raw:
        tmp = Path(raw)

        store, project, context = _ready(tmp, "control")
        rows.append(_row(
            "0 no movement (control)", "none", store, project,
            compute_source_identity(project), context.policy_digest,
        ))

        store, project, context = _ready(tmp, "head-only")
        before = compute_source_identity(project)
        fixtures.git(project.root, "commit", "-q", "--allow-empty", "-m", "empty commit")
        rows.append(_row(
            "1 empty commit (HEAD moves, no file changes)", "head-only",
            store, project, before, context.policy_digest,
        ))

        store, project, context = _ready(tmp, "content")
        before = compute_source_identity(project)
        _edit_a_declared_input(project)
        rows.append(_row(
            "2 edit a declared input, then commit", "content",
            store, project, before, context.policy_digest,
        ))

        store, project, context = _ready(tmp, "policy-untouched-evidence")
        before = compute_source_identity(project)
        _add_a_check(project)
        rows.append(_row(
            "3 rewrite the policy, evidence NOT re-run", "policy-without-rerun",
            store, project, before, context.policy_digest,
        ))

        store, project, context = _ready(tmp, "policy-rerun")
        before = compute_source_identity(project)
        _add_a_check(project)
        _run(store, project)
        rows.append(_row(
            "4 rewrite the policy, evidence re-run under it", "policy-with-rerun",
            store, project, before, context.policy_digest,
        ))

        # What a context-free caller sees over the evidence of case 2. This is
        # the decision `tests/test_formal_correspondence.py` compares against
        # the reference model, and it is why that model needs no identity.
        store, project, _ = _ready(tmp, "context-free")
        _edit_a_declared_input(project)
        without = tasks.compute_readiness(store, TASK, required_check_ids=[CHECK_ID])
        context_free = {
            "case": "5 the case-2 content change, no AcceptanceContext",
            "readiness": without.readiness,
            "gaps": list(without.gaps),
            "identities_compared": without.context["identities"],
        }
        claim = holder(store, "repository")
        ownership = None if claim is None else {
            "task_id": claim.task_id, "generation": claim.generation, "held": claim.held,
        }

    # The measured verdicts this docstring commits to, and which identity each
    # case's gap is required to name. A product change that moves any of them
    # turns this probe red, which is what keeps the two comments in
    # `formal/reference.py` from drifting apart again.
    expected = {
        "0 no movement (control)": ("READY", ()),
        "1 empty commit (HEAD moves, no file changes)": ("READY", ()),
        "2 edit a declared input, then commit": ("BLOCKED", ("source",)),
        "3 rewrite the policy, evidence NOT re-run": ("BLOCKED", ("source",)),
        "4 rewrite the policy, evidence re-run under it": ("BLOCKED", ("policy",)),
    }
    broken = []
    for row in rows:
        verdict, names = expected[row["case"]]
        named = tuple(
            "source" if "different source" in gap else "policy" for gap in row["gaps"]
        )
        if row["readiness"] != verdict or named != names:
            broken.append(row["case"])
    report = {"rows": rows, "context_free": context_free, "ownership": ownership}
    (ROOT / "review" / "revision-movement.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )

    print("which revision movement is visible to acceptance")
    print("=" * 100)
    for row in rows:
        print(f"\n{row['case']}")
        print(f"  HEAD moved       {row['head_moved']}   "
              f"{row['head_before'][:12]} -> {row['head_after'][:12]}")
        print(f"  inventory moved  {row['inventory_moved']}   "
              f"{row['inventory_before'][:12]} -> {row['inventory_after'][:12]}")
        print(f"  policy moved     {row['policy_moved']}   "
              f"pinned at admission still differs from the manifest: "
              f"{not row['pinned_follows_the_manifest']}")
        print(f"  finalize         {row['readiness']}")
        for gap in row["gaps"]:
            which = "source" if "different source" in gap else (
                "policy" if "different policy" in gap else "other")
            print(f"                   gap names {which}")
    print(f"\n{context_free['case']}")
    print(f"  finalize         {context_free['readiness']}")
    print(f"  identities       {context_free['identities_compared']}")
    print(f"\nclaim still held: {ownership}")
    print("=" * 100)
    print("written: review/revision-movement.json")
    if broken:
        print(f"\nFAILED: measured verdict or gap names differ from the claim for {broken}")
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
