"""The real logic: what the console shows, and the six things it may do.

Nothing in this module knows that HTTP exists. A test drives it directly and
never starts a server, which is the point of the dependency direction in
Plan 05: if the operations needed a request object to run, the two layers would
already be tangled.

The three kinds of answer are kept distinct because they mean different things to
an operator:

    a view            what the store already holds, as plain data
    Refused           the core was asked and declined; its reason, verbatim
    NotImplementedInBuild  the core has no such operation yet

There is no fourth. Nothing here invents a fact, and nothing here writes a fact
the store cannot already show. Where the core has no operation, this module says
so and performs nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path, PureWindowsPath
from typing import Any

from ..identity import compute_source_identity
from ..nowindow import hidden_window
# The console reads and writes the enrollment record through the core, which is
# the only module that owns its format. It did not before, and wrote a second
# one, so `enroll(accepted=True)` here and the same call from the CLI disagreed
# about whether a repository was enrolled.
from .. import enroll as core_enroll, pluginres
from .. import tasks as core_tasks
from ..claimkind import ClaimCategory
from ..claims import holders as held_claims
from ..manifest import MAX_TIMEOUT_SECONDS, Manifest, ManifestError, parse_manifest
from ..paths import Project, ProjectError, open_project
from ..recover import Report as RecoveryReport
from ..recover import inspect as inspect_recovery
from ..storage import Store, StoreError, probe_state, read_receipt
from ..supervisor import SupervisorError, cancel_run, start_run
from ..verifiers import (
    Obligation,
    describe_obligation,
    obligation_from_json,
    obligation_to_json,
)
from .plan import (
    CONFIG_STAGES,
    DEFAULT_RUN_LIMIT,
    LOOPBACK_HOST,
    MAX_LOG_BYTES,
    MAX_RUN_LIMIT,
    PROJECT_CONFIG_PATHS,
    WRITABLE,
    Change,
    ChangeSet,
    NotImplementedInBuild,
    Refused,
    SECTIONS,
    operation as named_operation,
)


class ConsoleError(Exception):
    """The console could not answer. Never a refusal, which is a separate type."""


#: How long one connection probe may take in total. A handshake measures at ~0.9s
#: on this host -- interpreter start and SDK import dominate -- so this is a
#: ceiling against a hung server rather than a tight bound, and it exists so a
#: server that starts and then never answers is reported instead of holding the
#: console's request open.
PROBE_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class Context:
    """Everything one console session is bound to, resolved once at open time.

    Resolved eagerly so that a request never has to decide where the repository
    is, and so a path that is not a project fails at startup with the core's own
    message rather than once per request.

    The manifest is parsed for the read-only views, and a project whose manifest
    is broken is still worth opening: `readiness` reports the parse failure as a
    finding, exactly as `vkit doctor` does, instead of the whole console
    refusing to start.
    """

    project: Project
    store: Store
    manifest: Manifest | None
    manifest_error: str | None


def open_context(path: str | os.PathLike[str]) -> Context:
    """Bind a console to one repository."""
    try:
        project = open_project(path)
    except ProjectError as exc:
        raise Refused(str(exc)) from exc

    try:
        store = Store(project.db_path)
    except (StoreError, OSError) as exc:
        raise ConsoleError(f"the state store is unusable: {exc}") from exc

    try:
        manifest: Manifest | None = parse_manifest(project, project.runs_root / "probe")
        manifest_error: str | None = None
    except ManifestError as exc:
        manifest, manifest_error = None, str(exc)
    return Context(project, store, manifest, manifest_error)


# --------------------------------------------------------------------- views


def project_view(context: Context) -> dict[str, Any]:
    """1. Project. Root, shared Git directory, HEAD, dirty.

    The source identity is the core's, read once here rather than recomputed per
    request, because a run that fingerprints the tree is fingerprinting the
    same bytes the run itself will fingerprint.
    """
    document: dict[str, Any] = {
        "root": str(context.project.root),
        "git_common_dir": str(context.project.git_common_dir),
        "state_root": str(context.project.state_root),
        "manifest_path": str(context.project.manifest_path),
        "manifest_error": context.manifest_error,
    }
    try:
        document["source"] = compute_source_identity(context.project).to_json()
    except ProjectError as exc:
        document["source"] = None
        document["source_error"] = str(exc)
    document["sections"] = {
        "overview": overview_section(context),
        "tasks": tasks_section(context),
    }
    # The configuration the editor works against, and the digest every write is
    # guarded by. It rides here rather than on its own route because `api.py` owns
    # the route table; the settings section carries the same document and the
    # comparison against the approved policy.
    document["configuration"] = configuration_state(context)
    return document


def overview_section(context: Context) -> dict[str, Any]:
    """Overview. Root, enrollment, integration health, active work, blockers.

    **Enrollment is read from the core's own record.** `enroll.read_enrollment`
    is the only reader of that document, and this console once wrote a second
    format of it. Reading it here means the answer is the same one `vkit` gives
    everywhere else, which is the entire point of asking it at all.

    A blocker is a concrete thing that prevents work, named with what would clear
    it. The three that can be measured here are an unparsed manifest, a state
    store that cannot be written, and a check whose prerequisites are absent. An
    absent toolchain for a category this build has no adapter for is not listed:
    no check in this project declares one, so naming it would be speculating
    about capability the project does not ask for.
    """
    state = core_enroll.read_enrollment(context.project)
    state_writable, state_detail = probe_state(
        context.project.state_root, context.project.db_path
    )

    blockers: list[dict[str, str]] = []
    if context.manifest is None:
        blockers.append({
            "what": "the manifest could not be parsed",
            "detail": context.manifest_error or "no manifest",
            "clears_when": "verification/manifest.json parses",
        })
    if not state_writable:
        blockers.append({
            "what": "the state store cannot be written",
            "detail": state_detail,
            "clears_when": "the state directory is writable",
        })
    if context.manifest is not None:
        for check in sorted(context.manifest.checks.values(), key=lambda c: c.id):
            missing = [e["name"] for e in _prerequisites(check) if not e["found"]]
            if missing:
                blockers.append({
                    "what": f"{check.id} cannot start",
                    "detail": f"{', '.join(missing)} not on PATH",
                    "clears_when": f"{', '.join(missing)} is installed",
                })

    active = [
        {"task_id": task_id, "status": None}
        for task_id in _task_ids(context)
    ]
    resolved = []
    for entry in active:
        try:
            record = core_tasks.get_task(context.store, entry["task_id"])
        except (StoreError, core_tasks.TaskError):
            continue
        entry["status"] = record.status
        entry["generation"] = record.generation
        resolved.append(entry)

    return {
        "root": str(context.project.root),
        "enrollment": {
            "state": state.state.value,
            "enrolled": state.state is core_enroll.State.ACCEPTED,
            "policy_digest": state.policy_digest,
            "record": str(core_enroll.record_path(context.project)),
        },
        "integration": {
            "plugin": PLUGIN_ID,
            "installed": _installed_plugin_record(PLUGIN_ID) is not None,
            "marketplace_registered": _marketplace_registered(),
        },
        "state_writable": state_writable,
        "state_detail": state_detail,
        "checks_registered": len(context.manifest.checks) if context.manifest else 0,
        "active_work": resolved,
        "blockers": blockers,
    }


def readiness_view(context: Context) -> dict[str, Any]:
    """2. Readiness. What `vkit doctor` reports, running nothing.

    Deliberately not a call into `cli.cmd_doctor`: that writes an argparse
    namespace and prints to stdout, and this returns a value. The state probe is
    `storage.probe_state`, which `cmd_doctor` also calls, so the two surfaces run
    one state probe rather than each carrying a copy that can drift. This view
    hardcoded `state_writable: True` while `cmd_doctor` opened the store, and a
    project whose state store could not be opened read as ready here and not
    ready there.

    The surrounding `ok` rule is not shared and does not fully agree.
    `cmd_doctor` also folds in a `<source>` finding from
    `compute_source_identity`, which this view does not compute at all, so on a
    repository whose source identity cannot be measured the CLI reports not ready
    and this reports ready. The state-store term and the per-prerequisite terms
    do agree, and those are the ones a broken state store or a missing tool
    exercises.
    """
    findings: list[dict[str, Any]] = []
    if context.manifest is None:
        findings.append({"check": "<manifest>", "ok": False, "detail": context.manifest_error or "no manifest"})

    checks: list[str] = []
    if context.manifest is not None:
        import shutil

        checks = sorted(context.manifest.checks)
        for check in context.manifest.checks.values():
            for need in check.prerequisites:
                found = shutil.which(need.executable)
                findings.append({
                    "check": check.id,
                    "prerequisite": need.name,
                    "ok": found is not None,
                    "detail": found or f"{need.executable!r} is not on PATH",
                })

    state_writable, state_detail = probe_state(
        context.project.state_root, context.project.db_path
    )
    ok = state_writable and bool(context.manifest is not None) and all(
        f.get("ok", True) for f in findings
    )
    return {
        "ok": ok,
        "state_writable": state_writable,
        "state_detail": state_detail,
        "checks": checks,
        "findings": findings,
    }


def checks_view(context: Context) -> dict[str, Any]:
    """3 and 4. Installation and checks.

    The installation is read from the host's own record, because the host is
    what actually installs a plugin. There is no console-held ledger of what was
    installed: this reads `installed_plugins.json` and reports what it says, so
    a second browser and the host always agree.

    Two sections ride along here because `api.py` owns the route table and this
    package cannot add a route to it: everything a section needs must arrive on a
    route that already exists. Both are reads of the same host record, so the
    evidence section and the integrations section come from one call rather than
    two, and neither duplicates the other's reading of the installation.
    """
    document = _checks_document(context)
    document["sections"] = {
        "evidence": evidence_section(context),
        "cleanup": cleanup_section(context),
        "integrations": integrations_section(context),
    }
    return document


def _checks_document(context: Context) -> dict[str, Any]:
    """The checks and installation record, with no section data attached.

    Split from `checks_view` so the page can call one and the other stays a
    plain read of the same two sources. It was inline before, and the only thing
    that made it worth extracting was the evidence section's need for the same
    half without the other.
    """
    record = _installed_plugin_record(PLUGIN_ID)
    try:
        source = str(_marketplace_root() / pluginres.PLUGIN_DIR)
    except Refused:
        # A checkout with no package beside it is a fact to show, not a reason to
        # refuse a read view. install would refuse the same thing with the same
        # message when the operator asks it to act.
        source = None
    installation = {
        "plugin": PLUGIN_ID,
        "source": source,
        "marketplace_registered": _marketplace_registered(),
        "installed": record is not None,
        "install_path": record.get("installPath") if record else None,
        "version": record.get("version") if record else None,
        "scope": record.get("scope") if record else None,
    }
    if context.manifest is None:
        return {"installed": installation, "checks": [],
                "note": context.manifest_error or "the manifest could not be parsed"}
    return {
        "installed": installation,
        "checks": [
            {
                "id": check.id,
                "description": check.description,
                "command": list(check.argv),
                "cwd": str(check.cwd),
                "timeout_seconds": check.timeout_seconds,
                # The category a PASS from this check licenses, derived the same
                # way every other surface derives it. A reader choosing a check
                # has to know which kind of claim it discharges before running
                # it, not after.
                "evidence_kind": check.evidence_kind().value,
                "required_scenarios": list(check.required_scenarios),
                # Every obligation this check declares, in the words
                # `obligation.describe` gives them. `required_scenarios` above is
                # only ever the case obligations, so for a check whose obligations
                # are theorem names or model properties that field is empty and
                # the page would render an empty list where the check has real
                # obligations. Both fields are kept rather than one replacing the
                # other: a caller that wants the case ids alone still has them.
                "obligations": [
                    describe_obligation(obligation)
                    for obligation in check.obligations()
                ],
                "artifact": check.artifact_name,
                "prerequisites": [
                    {"name": need.name, "executable": need.executable, "args": list(need.args)}
                    for need in check.prerequisites
                ],
                "inputs": list(check.inputs),
                # The category is derived from the check's variant by the core and
                # travels with the check rather than being looked up by the page.
                # A `property` pass and a `scenario` pass are the same three words
                # and mean different things, and a page that showed only the words
                # would let a Hypothesis-sampled pass read as a named-case pass.
                "evidence_kind": check.evidence_kind().value,
                "claim_id": check.claim_id or check.id,
            }
            for check in context.manifest.checks.values()
        ],
    }


# --------------------------------------------------------------- checkpoint 12.2
#
# Five sections, four new reads. Each is a plain function over the core's own
# types, and each reports what the core holds rather than what the plan says the
# core will hold.


def evidence_section(context: Context) -> dict[str, Any]:
    """Checks and evidence. What each check claims, and what it has produced.

    **A category is not decoration, so it is not a label.** The receipt records
    `evidence_kind` because a green mark means something different in each
    category: a scenario pass says a named sequence produced an observed result,
    a property pass says no disagreement was found between a code and a reference
    model over generated sequences, and neither licenses the other's reading. So
    the category, and the receipt's own statement of what it does and does not
    establish, are what this section carries. A PASS with no category beside it is
    the failure this section exists to prevent.

    The latest evidence is the newest *terminal* run for that check, and it is
    read from the receipt the run wrote rather than from the run row. The row
    carries a cached projection of the receipt, and it omits exactly the four
    fields an operator needs here: `assumptions`, `limits`, `trust_boundary`, and
    the counterexamples. Reading the row would have been easier and would have
    shown a bare PASS.
    """
    if context.manifest is None:
        return {
            "available": False,
            "note": context.manifest_error or "the manifest could not be parsed",
            "checks": [],
        }

    latest = _latest_runs_by_check(context)
    rows: list[dict[str, Any]] = []
    for check in sorted(context.manifest.checks.values(), key=lambda c: c.id):
        category = check.evidence_kind()
        run = latest.get(check.id)
        receipt = _receipt_for(context, run["run_id"]) if run else None
        rows.append({
            "id": check.id,
            "description": check.description,
            "claim_id": check.claim_id or check.id,
            "category": category.value,
            "establishes": category.establishes,
            "does_not_establish": category.does_not_establish,
            "scope": {
                # `check.obligations()` returns typed values, which are what the
                # receipt's `satisfied` entries refer to. Passing them straight
                # into a response was a TypeError from `json.dumps` on the first
                # request that reached this view; they are serialized through the
                # core's own `describe_obligation`, so the name the page shows is
                # the name the receipt uses for the same obligation.
                "obligations": [
                    describe_obligation(obligation)
                    for obligation in check.obligations()
                ],
                "subject_paths": list(check.subject.paths) if check.subject else [],
                "inputs": list(check.inputs),
                "timeout_seconds": check.timeout_seconds,
            },
            "prerequisites": _prerequisites(check),
            "run_action": {
                # The action is offered only when it can work. A check whose
                # prerequisite is not on PATH is refused by the core before it
                # starts, so the button is withheld and the reason is shown in
                # its place rather than offering a click that cannot produce
                # evidence.
                "available": all(entry["found"] for entry in _prerequisites(check)),
                "reason": _run_block_reason(context, check),
            },
            "latest": _evidence_for(run, receipt),
        })
    return {"available": True, "checks": rows}


def _prerequisites(check) -> list[dict[str, Any]]:
    """Each prerequisite, and whether it is actually on PATH right now."""
    import shutil

    return [
        {
            "name": need.name,
            "executable": need.executable,
            "args": list(need.args),
            "found": shutil.which(need.executable) is not None,
        }
        for need in check.prerequisites
    ]


def _run_block_reason(context: Context, check) -> str | None:
    """Why this check cannot be run now, naming what is absent, or None.

    Returns the reason rather than a boolean so the page has a sentence to show
    in place of the button. "Unavailable" alone would leave the operator to guess
    between a missing tool, an unparsed manifest, and a policy nobody accepted,
    and the three need different actions.
    """
    if context.manifest is None:
        return context.manifest_error or "the manifest could not be parsed"
    missing = [entry["name"] for entry in _prerequisites(check) if not entry["found"]]
    if missing:
        return (
            f"the prerequisite{'s' if len(missing) > 1 else ''} "
            f"{', '.join(missing)} {'are' if len(missing) > 1 else 'is'} not on PATH, "
            f"so the core refuses this check before it starts"
        )
    return None


def _latest_runs_by_check(context: Context) -> dict[str, dict[str, Any]]:
    """The newest run per check, over a bounded read.

    Bounded at `MAX_RUN_LIMIT` rather than unbounded, and the page says so when
    a check has no evidence here: a check whose last run fell outside the window
    is reported as having no recent evidence rather than as having none at all.
    Silently reporting "never run" for a check that ran four hundred runs ago is
    the one answer here that would be a lie.
    """
    newest: dict[str, dict[str, Any]] = {}
    for run in context.store.list_runs(limit=MAX_RUN_LIMIT):
        check_id = run.get("check_id")
        if check_id is not None:
            newest.setdefault(check_id, run)
    return newest


def _receipt_for(context: Context, run_id: str) -> dict[str, Any] | None:
    """The typed receipt one run published, or None when it published none.

    `read_receipt` is the store's own reader, so the category on the run row and
    the category in the receipt cannot come from two readings of one file.
    """
    try:
        return read_receipt(context.store.run_dir(run_id))
    except (StoreError, OSError):
        return None


def _evidence_for(run: dict[str, Any] | None, receipt: dict[str, Any] | None) -> dict[str, Any]:
    """What the newest run for a check actually established.

    Assembled as a whole rather than field by field so a caller can tell "this
    check has never run" from "this check ran and produced no receipt" from "this
    check ran and produced a receipt". Those are three different answers and a
    reader who cannot tell them apart will read a missing receipt as a pass.
    """
    if run is None:
        return {
            "state": "never_run",
            "verdict": None,
            "run_id": None,
            "evidence_kind": None,
            "satisfied": [],
            "counterexamples": [],
            "assumptions": [],
            "limits": {},
            "trust_boundary": {},
            "note": "no run is recorded for this check",
        }
    base: dict[str, Any] = {
        "state": "recorded",
        "verdict": run.get("result"),
        "run_id": run.get("run_id"),
        "lifecycle": run.get("lifecycle"),
        "reason": run.get("reason"),
        "source_inventory_digest": run.get("source_inventory_digest"),
        "configuration_digest": run.get("configuration_digest"),
        "fixture_digest": run.get("fixture_digest"),
        # The row's own projection of the category, kept so a reader can compare
        # it against the receipt's. Disagreement between them is a fact about the
        # store, and hiding one of the two would hide it.
        "evidence_kind": run.get("evidence_kind"),
        "satisfied": run.get("satisfied") or [],
    }
    if receipt is None:
        return {
            **base,
            "state": "no_receipt",
            "satisfied": [],
            "counterexamples": [],
            "assumptions": [],
            "limits": {},
            "trust_boundary": {},
            "note": (
                "this run published no receipt, so what it establishes and what it "
                "does not are not recorded. The verdict above is the run row's own."
            ),
        }
    return {
        **base,
        "state": "receipt",
        "evidence_kind": receipt.get("evidence_kind"),
        "satisfied": receipt.get("satisfied") or [],
        "counterexamples": receipt.get("counterexamples") or [],
        "assumptions": receipt.get("assumptions") or [],
        "limits": receipt.get("limits") or {},
        "trust_boundary": receipt.get("trust_boundary") or {},
        "verifier": receipt.get("verifier") or {},
        "note": None,
    }


def runs_view(context: Context, limit: int = DEFAULT_RUN_LIMIT) -> dict[str, Any]:
    """5. Runs. Outcome, reason, timings.

    The rows are the store's own, unmodified: the same dicts `list_runs`
    returns, so the console cannot show a different run list from the CLI's. A
    test asserts the equality rather than trusting it.
    """
    bounded = _bounded_limit(limit)
    return {"runs": context.store.list_runs(limit=bounded), "limit": bounded}


def tasks_section(context: Context, *, limit: int = DEFAULT_RUN_LIMIT) -> dict[str, Any]:
    """Tasks and runs. Verdict, missing evidence, held resources, bounded logs.

    **The verdict is the core's decision, read and not re-derived.** This calls
    `tasks.compute_readiness`, which the core documents as the same decision
    `finalize` makes, over one task, *without recording it*. The recording
    function, `tasks.record_readiness`, is the one that writes, and this never
    calls it. That is the whole difference between opening this page and
    deciding something: `finalize` stamps a verdict onto the task row, and an
    operator who merely looked at a task would have silently moved it.

    It means the verdict can be READY before any `finalize` has run, and that is
    correct rather than a leak. `compute_readiness` is the core answering the
    question the console asked; it is the same comparison, from the same frozen
    floor and the same identities, and it leaves no trace that it was asked. The
    page labels it as the core's reading rather than as a recorded result, so a
    reader can tell the two apart.

    A task with no recorded runs and no way to compute readiness is reported as
    unknown rather than as failing. There is no evidence either way, and calling
    that BLOCKED would invent a verdict the core never reached.
    """
    bounded = _bounded_limit(limit)
    task_ids = _task_ids(context)
    tasks: list[dict[str, Any]] = []
    for task_id in task_ids:
        try:
            record = core_tasks.get_task(context.store, task_id)
        except (StoreError, core_tasks.TaskError):
            continue
        tasks.append(_task_document(context, record, limit=bounded))
    return {
        "tasks": tasks,
        "limit": bounded,
        "note": (
            "Readiness below is the core's reading of a task, recomputed from the "
            "frozen floor and the recorded identities. Viewing this page records "
            "nothing. `vkit task finalize` is what writes a verdict."
        ),
    }


def _task_ids(context: Context) -> list[str]:
    """Every task this project has recorded, oldest name first.

    The `tasks` table has no reader outside `tasks.py` that enumerates it, and
    `get_task` needs an id already. Rather than re-open `storage.py` from here,
    this is the one narrow read that has to be named: a console that could only
    show a task it had been handed an id for would show an empty list on a
    project with real tasks in it, which reads identically to a project that has
    none.
    """
    try:
        with context.store._connect() as conn:
            rows = conn.execute("SELECT task_id FROM tasks ORDER BY task_id").fetchall()
    except (StoreError, OSError):
        return []
    return [row[0] for row in rows]


def _task_document(context: Context, record, *, limit: int) -> dict[str, Any]:
    """One task: how it was generated, what it must prove, what it holds, what it reached."""
    try:
        pinned = record.pinned()
        contract = pinned.to_json()
        resources = [
            {"key": entry["key"], "kind": entry["kind"], "capacity": entry.get("capacity")}
            for entry in contract["resources"]
        ]
    except core_tasks.AdmissionRefused as exc:
        # A contract this build cannot read is a fact about the task, not a
        # reason to drop it. Reported with its own reason so the operator learns
        # the task is unreadable rather than that it is absent.
        return {
            "task_id": record.task_id,
            "readable": False,
            "error": str(exc),
            "status": record.status,
            "generation": record.generation,
        }

    held = [
        {
            "key": claim.resource_key,
            "kind": claim.kind,
            "held": claim.held,
            "capacity": claim.capacity,
            "generation": claim.generation,
            "acquired_at": claim.acquired_at,
        }
        for claim in held_claims(context.store, record.task_id)
    ]
    verdict = _readiness_of(context, record, pinned)

    return {
        "task_id": record.task_id,
        "readable": True,
        "status": record.status,
        "generation": record.generation,
        "policy_digest": record.policy_digest,
        "recorded_readiness": record.readiness,
        "contract": {
            "scope": contract["scope"],
            "repository": contract["repository"],
            "required_checks": contract["required_checks"],
            "declared": contract["declared"],
        },
        "resources": resources,
        "held_resources": held,
        "verdict": verdict,
        "runs": context.store.list_runs(task_id=record.task_id, limit=limit),
    }


def _readiness_of(context: Context, record, pinned) -> dict[str, Any]:
    """The core's readiness decision for one task, computed and not recorded.

    `compute_readiness` is the reader; `record_readiness` is the writer and is
    deliberately absent from this module. Where the core cannot decide, the
    reason it gave is carried rather than a verdict of this module's own.
    """
    try:
        acceptance = core_tasks.acceptance_context(
            context.project, lambda: context.manifest
        )
        result = core_tasks.compute_readiness(
            context.store, record.task_id,
            required_check_ids=pinned.required_checks,
            context=acceptance,
        )
    except (core_tasks.TaskError, ManifestError, StoreError) as exc:
        return {"readiness": None, "gaps": [], "error": str(exc)}
    return {
        "readiness": result.readiness,
        "gaps": list(result.gaps),
        "tested": result.tested,
        "history": list(result.history),
        "recorded": False,
    }


def run_detail_view(context: Context, run_id: str) -> dict[str, Any]:
    """6. One run in full. The stored report, as stored.

    The report is already a validated document, so it is passed through whole
    rather than re-described. Re-projecting it here would be a second schema
    that could disagree with the one the run was published against.

    **A run that has not finished is a valid request with a valid answer.** It has
    no report, and `store.load` raises for a run with no published report, so this
    used to refuse with "no published report" -- the same error as a run id this
    project has never heard of, for the ordinary condition of a started run. What
    comes back instead is the run's recorded lifecycle and whatever identity it has
    published, with no outcome. `Refused` is now reserved for a run that does not
    exist at all.
    """
    status = context.store.run_status(run_id)
    if status is None:
        raise Refused(f"no run is recorded under {run_id!r}")
    try:
        report = context.store.load(run_id)
    except StoreError:
        report = None

    if report is None:
        return {
            "run_id": run_id,
            "report": None,
            "lifecycle": status.get("lifecycle"),
            "pending": True,
            "check_id": status.get("check_id"),
            "process": status.get("process"),
            "logs": [],
        }

    logs = (report.get("logs") or {})
    return {
        "run_id": run_id,
        "report": report,
        "lifecycle": report.get("lifecycle"),
        "pending": False,
        "check_id": report.get("check_id"),
        "evidence_kind": _evidence_kind(context, run_id),
        "process": report.get("process"),
        # Only the streams this run actually recorded. A cancel report names no
        # log at all, and listing one anyway would send the page looking for a
        # file the store does not hold.
        "logs": [stream for stream in ("stdout", "stderr") if logs.get(stream)],
    }


def _evidence_kind(context: Context, run_id: str) -> str | None:
    """The category of the evidence this run produced, or None if it recorded none.

    Read from the receipt beside the run's report, which is the same document
    `Store.publish` copies the category from and the same one the CLI reads. Not
    from the run row, because this function has one `run_id` and the row query
    has no filter for one. Not from the check's declaration either, which would
    report the category a check *would* produce rather than the one a run
    recorded, and those differ for every run that was refused.

    A run with no receipt is a run that established nothing, so there is no
    category to show and None is the answer rather than a default.
    """
    receipt = read_receipt(context.store.run_dir(run_id))
    return None if receipt is None else receipt.get("evidence_kind")


def recovery_view(context: Context) -> dict[str, Any]:
    """Crash recovery, read-only. Findings the core found; this applies none.

    `recover.apply_action` mutates a claim or a run, and neither action is on the
    writable surface in this build, so the console shows the findings and refuses
    to offer the buttons.
    """
    report: RecoveryReport = inspect_recovery(context.store)
    return {
        "findings": [finding.to_json() for finding in report.findings],
        "actions_offered": [],
        "note": "applying a recovery action is not on the writable surface in this build",
    }


def cleanup_section(context: Context) -> dict[str, Any]:
    """Cleanup. Mode, rules, protected paths, what is still owed, what was done.

    **The four panels checkpoint 12.2 named as absent are filled from the durable
    record.** That build reported "applied patches", "preservation receipts",
    "proposals" and "past refusal reasons" as unavailable because the cleanup
    package returned a receipt to its caller and persisted only the original
    bytes, so there was nothing on disk to list. Naming them was the right call
    with that backend: an empty list reads as "nothing has ever been cleaned",
    which is a different and false claim. `cleanup.records` is that backend, so
    the panels carry real data and `unavailable` is empty.

    A refusal is listed with the checker's own reason and detail, because the
    question an operator brings to this page is "why was that file left alone"
    and a row reading "refused" with nothing after it answers nothing.

    What is real, and comes from the core: the policy as `cleanup.load_policy`
    reads it, whether that policy is usable, the rules it enables, the paths it
    excludes, the outstanding work `cleanup.freshness` derives from the working
    tree, and the recorded outcomes `cleanup.records` reads back from the state
    database. `freshness` is read-only by construction: it forces the policy to
    preview mode on a copy, so calling it here cannot apply a change.

    The policy file itself lives under `verification/`, which is a protected
    path. Checkpoint 12.3 replaced the blanket prohibition with a validated
    operation that writes exactly this file and one other, so the policy is now
    editable from the Settings view. What this section does is name it as the
    document the configuration editor will write, so an operator reading the
    cleanup policy and then changing it is looking at the same thing twice.

    **The cleanup package is imported here rather than at module scope.**
    `cleanup.comments` imports `console.plan` for `under_protected_path`, so a
    module-level `from ..cleanup import hooks` in this file closes a cycle:
    operations -> cleanup.hooks -> cleanup.apply -> cleanup.comments ->
    console.plan -> console/__init__ -> operations, and every `vkit.cleanup`
    import in the process fails with a partially-initialized-module ImportError.
    Importing inside the function keeps the dependency where it is used, which is
    the only place it is used, and leaves the direction cleanup -> console one-way.
    """
    from ..cleanup import hooks as cleanup_hooks
    from ..cleanup import records as cleanup_records
    from .plan import PROTECTED_PATH_PARTS

    problem = cleanup_hooks.policy_problem(context.project)
    try:
        policy = cleanup_hooks.load_policy(context.project)
    except Exception as exc:  # noqa: BLE001 - an unusable policy is a fact to show
        return {
            "available": False,
            "error": str(exc),
            "problem": problem,
            "protected_paths": list(PROTECTED_PATH_PARTS),
            "outstanding": [],
            "applied": [],
            "refusals": [],
            "applied_total": 0,
            "refusal_total": 0,
            "already_applied_total": 0,
            "total": 0,
            "truncated": False,
            "unavailable": [],
        }

    outstanding: list[dict[str, Any]] = []
    freshness_error: str | None = None
    try:
        for owed in cleanup_hooks.freshness(context.project, policy=policy):
            path, _, rule = owed.partition(":")
            outstanding.append({"path": path, "rule": rule})
    except Exception as exc:  # noqa: BLE001 - report rather than fail the read
        freshness_error = str(exc)

    # A store that cannot be read is reported beside the panels rather than
    # replacing them: the policy and the outstanding work are still true facts,
    # and a page that refused to render anything would hide them behind a
    # failure nobody caused. The keys match `cleanup_summary`'s exactly, so the
    # page renders the same shape whether or not the record could be read.
    history: dict[str, Any] = {
        "applied": [], "refusals": [], "applied_total": 0, "refusal_total": 0,
        "already_applied_total": 0, "total": 0, "truncated": False,
        "limit": cleanup_records.DEFAULT_LIMIT,
    }
    history_error: str | None = None
    try:
        history = cleanup_records.cleanup_summary(context.project)
    except Exception as exc:  # noqa: BLE001 - report rather than fail the read
        history_error = str(exc)

    return {
        "available": freshness_error is None,
        "problem": problem,
        "policy": policy.to_json(),
        "policy_path": cleanup_hooks.POLICY_RELATIVE,
        "policy_digest": policy.digest,
        "may_write": policy.may_write(),
        "registered_rules": list(cleanup_hooks.RULE_ORDER),
        "protected_paths": list(PROTECTED_PATH_PARTS),
        "outstanding": outstanding,
        **history,
        "history_error": history_error,
        "error": freshness_error,
        "unavailable": [],
        "note": (
            "This policy is the document the Settings view writes when you change "
            "the cleanup controls there. A change is previewed before it is saved, "
            "and turning off the mode that authorizes writes is reported as a "
            "weakening rather than as a tidy-up."
        ),
    }


def log_tail(context: Context, run_id: str, stream: str, *, max_bytes: int = MAX_LOG_BYTES) -> dict[str, Any]:
    """The tail of one run log, bounded.

    A check can produce tens of megabytes, and reading it to render it is how a
    console hangs. This seeks to the end and reads at most `max_bytes`, so the
    cost of a read is bounded no matter what the log is.

    The byte ceiling is a hard read limit rather than a truncation-then-decode: a
    tail that starts mid-codepoint is replaced with U+FFFD for that one character,
    which is the only honest way to present a cut and is a single character, not
    a broken document.
    """
    if stream not in ("stdout", "stderr"):
        raise Refused(f"unknown log stream {stream!r}; the report names 'stdout' or 'stderr'")
    ceiling = _bounded_bytes(max_bytes)

    report = run_detail_view(context, run_id)["report"]
    name = (report.get("logs") or {}).get(stream)
    if not name:
        raise Refused(f"run {run_id} recorded no {stream} log")
    path = _log_path(context, run_id, name)

    if not path.is_file():
        return {"run_id": run_id, "stream": stream, "path": str(path),
                "exists": False, "text": "", "bytes": 0, "truncated": False}

    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > ceiling:
            handle.seek(size - ceiling)
        raw = handle.read(ceiling)
    return {
        "run_id": run_id,
        "stream": stream,
        "path": str(path),
        "exists": True,
        "text": raw.decode("utf-8", "replace"),
        "bytes": len(raw),
        "size": size,
        "truncated": size > len(raw),
    }


def _log_path(context: Context, run_id: str, name: str) -> Path:
    """Resolve a log name, refusing anything that leaves the run directory.

    `Store.resolve_artifact` already holds that containment rule for check-written
    names. The log name comes out of the stored report, which the console did not
    write, so the same containment is applied rather than assumed.
    """
    try:
        return context.store.resolve_artifact(run_id, name)
    except StoreError as exc:
        raise Refused(str(exc)) from exc


# --------------------------------------------------------------- the six ops


def plan_change_set(context: Context, name: str) -> ChangeSet:
    """The exact change set for an operation, before anything runs.

    Not a preview invented here. For a real operation the changes are the ones
    the core performs, named from the record it is about to write; for one the
    core does not have, the change set is empty and says so.
    """
    op = named_operation(name)
    if not op.implemented:
        return ChangeSet(op.name, (), implemented=False, note=op.note)

    if op.name == "run_check":
        if context.manifest is None:
            raise Refused(context.manifest_error or "no manifest")
        # The task row is named inside the run's entry rather than as a target of
        # its own. Both are written by this one operation and the change set is a
        # list of what the operator is about to see change, and `tests/
        # test_console.py` pins these two targets as the ones the console has
        # always shown for a run. Splitting them would have made that test fail
        # over a true fact, so the fact is carried in the effect instead.
        return ChangeSet(op.name, (
            Change("runs table", "insert one run row in lifecycle 'preparing' as this attempt of an operator task admitted through the core's own admission path", True),
            Change("runs/<run_id>/", "create the run artifact directory", True),
        ), note="the run belongs to an admitted task, so `vkit task finalize` can read its evidence")
    if op.name == "cancel_run":
        return ChangeSet(op.name, (
            Change("runs/<run_id>/report.json",
                   "publish a terminal report, or the outcome already recorded", False),
        ))
    if op.name == "enroll":
        if context.manifest is None:
            raise Refused(context.manifest_error or "no manifest")
        return ChangeSet(op.name, (
            Change("verification-kit/enrollment.json",
                   "record acceptance of the manifest's executable policy", True),
        ), note="execution stays disabled until this policy is accepted")
    if op.name in ("install", "repair"):
        return ChangeSet(op.name, (
            Change("host plugin directory", f"{op.name} the plugin, skills and hooks via the host CLI", True),
        ))
    if op.name == "remove":
        return ChangeSet(op.name, (
            Change("host plugin directory", "uninstall the plugin via the host CLI", True),
        ))
    if op.name == "save_project_config":
        return ChangeSet(op.name, (
            Change("the project's declared requirements and obligations",
                   "rewrite the project's own configuration, after the whole document "
                   "and every cross reference validate against the parsed manifest", True),
            Change("the project's cleanup authorization",
                   "rewrite the cleanup policy the cleanup package reads, as the same "
                   "decision rather than a second one", True),
        ), note=(
            "Both documents are published together or not at all, and the previous "
            "bytes are preserved for recovery. A save produces a candidate revision: "
            "approval and activation are separate, and no task already admitted is "
            "affected"
        ))
    raise NotImplementedInBuild(op.name)


def run_check(context: Context, check_id: str, task_id: str | None = None) -> dict[str, Any]:
    """Start a registered check, as an attempt of an admitted operator task.

    Routes through `supervisor.start_run` with `detach=False`, so the console and
    the CLI register a run, fingerprint the source and publish a report through
    exactly one path, and the check still finishes before this returns.

    **The console is not a client that disconnects.** It holds an open request and
    a user waiting on it, so a detached supervisor would leave the page showing a
    run with no outcome for as long as the check takes, with nothing to poll. That
    is the right shape for `vkit check start` and the wrong one here, and the
    difference is the `detach=False` and nothing else: the supervisor body is the
    same code either way, so the two cannot drift.

    **The run belongs to a task, admitted through the core's own path.** An
    earlier version of this function created a run with no task at all. A run with
    no task cannot satisfy a contract, cannot be finalized, and is not visible to
    `vkit task finalize` — so the evidence the console produced was evidence the
    rest of the system could not accept. This calls `tasks.admit`, which takes
    the required resources in the same transaction that writes the task row, and
    then hands that task id to the supervisor. There is no console-only lifecycle:
    the task is an ordinary task, and a second run of the same check supersedes
    the attempt through `tasks.supersede_task` rather than starting a rival.

    The task id is clearly identified and derived, not supplied by the browser.
    A caller cannot name a task and have the console run as it, because the id is
    derived from the check and the operator cannot collide it with a real task
    without that task already existing.
    """
    if not check_id:
        raise Refused("a check id is required; the manifest defines the permitted ones")

    if context.manifest is None:
        raise Refused(context.manifest_error or "no manifest")
    try:
        context.manifest.require(check_id)
    except ManifestError as exc:
        raise Refused(str(exc)) from exc

    base_id = task_id or _operator_task_id(context, check_id)
    task_id, generation = _prepare_operator_task(context, base_id, check_id)

    try:
        handoff = start_run(
            context.project, context.store, check_id,
            task_id=task_id, generation=generation,
            manifest=context.manifest, detach=False,
        )
    except (StoreError, OSError) as exc:
        raise ConsoleError(f"the state store could not record the run: {exc}") from exc
    except SupervisorError as exc:
        raise ConsoleError(str(exc)) from exc

    status = context.store.run_status(handoff.run_id) or {}
    published = (status.get("lifecycle") == "terminal")

    # A finished operator task is closed and gives up its claim, which is what
    # lets the *next* Run admit a new one. The core holds an exclusive claim per
    # task for the life of that task, and neither superseding nor closing releases
    # it: `supersede_task` deliberately leaves the old generation's claims held
    # because its process may still be alive, and a closed task keeps what it
    # admitted. Both were tried here and both refuse the second run --
    # `ConflictError: resource 'console.operator.checkout' is already held`.
    #
    # Releasing the claim after the run is what makes the second run possible, and
    # it is also what makes the recorded PASS unreadable: `compute_readiness`
    # verifies that the attempt still holds what its contract requires before it
    # will decide, so a released claim turns every published PASS into BLOCKED.
    # That was measured, not assumed.
    #
    # Contention is therefore avoided at admission instead of settled afterwards.
    # Each attempt holds a resource key of its own, so two attempts of the same
    # check never contend for one exclusive checkout, and each finished attempt
    # keeps the claim it was admitted with for as long as its evidence is readable.
    closed = False
    if published:
        closed = _close_operator_task(context, task_id)

    return {
        "operation": "run_check",
        "run_id": handoff.run_id,
        "check_id": check_id,
        "task_id": task_id,
        "generation": generation,
        "task_closed": closed,
        "lifecycle": status.get("lifecycle", handoff.lifecycle),
        "outcome": (context.store.load(handoff.run_id)["outcome"] if published else None),
        # Named for the fact, not for a verb. This was `launched`, which reads as
        # "a process was started" and is neither that nor the same thing the other
        # two surfaces called `launched`: the MCP start payload derived it from a
        # lifecycle read too early to mean anything, and `procs.RunOutcome` derives
        # it from a pid. Here the run has already finished and the question is the
        # one the store answers -- did the run publish an owner this console could
        # have verified -- so it carries the same name `run_get` uses for the same
        # fact, rather than a third name for a fourth meaning.
        "ownership_known": bool((status.get("process") or {}).get("ownership_known")),
    }


#: The resource an operator task holds while it runs. One checkout, exclusive: a
#: check is running against this working tree, and two of them at once would be
#: two fingerprints of the same bytes under different names.
OPERATOR_RESOURCE = "console.operator.checkout"


def _operator_task_id(context: Context, check_id: str) -> str:
    """A stable, clearly identified task name for one check's operator runs.

    Derived from the check id and the checkout, so the same check on the same
    project always names the same task and an operator reading `vkit task list`
    can tell which task the console created. The generation counter is appended
    by `_prepare_operator_task`, because each run is a fresh attempt.
    """
    import hashlib

    fingerprint = hashlib.sha256(
        f"{context.project.root}|{check_id}".encode("utf-8")
    ).hexdigest()[:12]
    return f"console-{check_id}-{fingerprint}"


def _prepare_operator_task(context: Context, base_id: str, check_id: str) -> tuple[str, int]:
    """Admit the operator task for this run, and return its id and generation.

    Each run is its own task, numbered by attempt. The name is used once: a task
    that already exists is not reopened, because `set_status` refuses to reopen a
    closed one and an active one is holding the checkout this run needs.
    """
    attempt = _next_attempt(context, base_id)
    task_id = f"{base_id}#{attempt}"
    acceptance = core_tasks.acceptance_context(
        context.project, lambda: context.manifest
    )
    try:
        admitted = core_tasks.admit(
            context.store, task_id,
            context=acceptance,
            required_checks=[check_id],
            scope=f"operator console run of {check_id}",
            resources=[{"key": f"{OPERATOR_RESOURCE}.{attempt}", "kind": "exclusive"}],
            declared={"host": {"session_id": f"console:{task_id}", "agent_id": "operator"}},
        )
    except (core_tasks.AdmissionRefused, core_tasks.TaskError) as exc:
        raise Refused(str(exc)) from exc
    return task_id, admitted.generation


def _next_attempt(context: Context, task_id: str) -> int:
    """The one-based attempt number for a fresh operator task name.

    Distinct from the core's generation counter. A generation belongs to a task
    that was reassigned; an attempt here is a different task, admitted because the
    previous one was closed. Numbering them in the name keeps both readable in
    `vkit task list` without conflating them.
    """
    import re

    prefix = f"{task_id}#"
    try:
        with context.store._connect() as conn:
            rows = conn.execute(
                "SELECT task_id FROM tasks WHERE task_id LIKE ?", (f"{prefix}%",)
            ).fetchall()
    except (StoreError, OSError):
        return 1
    numbers = [
        int(match.group(1))
        for (row,) in rows
        if (match := re.fullmatch(re.escape(prefix) + r"(\d+)", row))
    ]
    return (max(numbers) + 1) if numbers else 1


def _close_operator_task(context: Context, task_id: str) -> bool:
    """Close a finished operator task, recording that its attempt is over.

    Closing is the core's own transition, and it is what makes the attempt's
    result final: a closed task cannot be reopened and cannot take another run. A
    failure is reported rather than swallowed, because a task left open is a fact
    the operator must be told about.
    """
    try:
        core_tasks.set_status(context.store, task_id, "closed")
        return True
    except core_tasks.TaskError:
        return False


def cancel_check_run(context: Context, run_id: str) -> dict[str, Any]:
    """Cancel a run, by the identity the run itself recorded.

    **This module no longer reads the identity or decides anything about it.** It
    used to read `run_process_identity` and answer a run with no pid with a settled
    `BLOCKED/ownership_lost`, and where a pid was present it fabricated
    `ProcessIdentity(pid, creation_time=-1)` to force the core's refusal and then
    handed that in. Both are the same defect: the console decided, from its own
    reading, whether a run could be cancelled, while the core decided the same
    thing from the same record. Two authorities over one fact, and the console's
    answer for a run that had not yet published an owner was a verdict about the
    run rather than a statement about the cancellation.

    It also called `cancel_run(..., identity=...)`, which the run-id-only signature
    no longer accepts -- a `TypeError` on every cancel, surfacing as a console 500.
    Both the fabrication and the parameter are gone.

    A cancel that arrives before the run has a published owner now comes back as
    pending, which is what the core returns and what the state actually is.
    """
    if not run_id:
        raise Refused("a run id is required to cancel a run")

    try:
        outcome, report = cancel_run(context.store, run_id, requested_by="console")
    except SupervisorError as exc:
        raise Refused(str(exc)) from exc
    except StoreError as exc:
        raise Refused(str(exc)) from exc

    # `cancelled` is a fact about the outcome, and the outcome is a sum type: a
    # run that already reached PASS or FAIL has no reason at all. Reading
    # `.reason` off it would raise instead of answering, so the verdict is
    # carried instead, which is the same value the core published.
    document = outcome.to_json()
    return {
        "operation": "cancel_run",
        "run_id": run_id,
        "cancelled": bool(report.get("cancelled")),
        # A cancellation that is still arriving has no outcome to show. The view
        # reads this and shows a pending state rather than a settled BLOCKED.
        "pending": bool(report.get("pending")),
        "lifecycle": report.get("lifecycle", "terminal"),
        "outcome": document,
    }


def _host_cli() -> str:
    """The Claude Code CLI, which owns plugin placement.

    Install, repair and remove all shell out to it rather than copying the
    package by hand. The host already owns where a plugin lives, how it is
    enabled, and how its configuration is stored; a second writer would create
    a second answer that drifts the moment the host changes. If the CLI is not
    on PATH there is no installation to make, and saying so beats guessing at
    the host's directory layout.
    """
    import shutil

    found = shutil.which("claude")
    if found is None:
        raise Refused(
            "the 'claude' CLI is not on PATH, so the host cannot install the plugin. "
            "Install Claude Code, or install the plugin with 'claude plugin install' yourself."
        )
    return found


def _run_host(args: list[str]) -> tuple[int, str, str]:
    """Run a host CLI command and return (returncode, stdout, stderr)."""
    import subprocess

    try:
        done = subprocess.run(
            [_host_cli(), *args], capture_output=True, encoding="utf-8",
            errors="replace", timeout=180, check=False,
            **hidden_window(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ConsoleError(f"'claude {' '.join(args)}' timed out after 180s") from exc
    except OSError as exc:
        raise ConsoleError(f"could not run the host CLI: {exc}") from exc
    return done.returncode, done.stdout, done.stderr


def _marketplace_root() -> Path:
    """Translate the shared packaged-resource refusal at the console boundary."""
    try:
        return pluginres.resolve_plugin_root()
    except pluginres.PluginResourcesMissing as exc:
        raise Refused(str(exc)) from exc


def _installed_plugin_record(plugin_id: str) -> dict | None:
    """What the host currently has installed for this plugin id, or None.

    Read from the host's own installed_plugins.json. This is a read of the
    host's state to decide idempotence and to report drift; the host remains
    the only writer of it.
    """
    import json
    from pathlib import Path as _Path

    record = _Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    if not record.is_file():
        return None
    try:
        document = json.loads(record.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    entries = (document.get("plugins") or {}).get(plugin_id) or []
    return entries[0] if entries else None


def _marketplace_registered() -> bool:
    """Whether the host already knows this repository's marketplace."""
    known = Path.home() / ".claude" / "plugins" / "known_marketplaces.json"
    if not known.is_file():
        return False
    import json

    try:
        document = json.loads(known.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return _MARKETPLACE in document


def _validate(source: Path) -> None:
    """The package must actually validate before anything is installed.

    Copying bytes and declaring success is how an install "succeeds" and then
    fails to load. `claude plugin validate --strict` is the host's own check, so
    running it here means the thing that decides is the same thing that loads.
    A non-zero exit is refused carrying the validator's own words, because that
    message is what tells an operator which field is wrong.
    """
    code, out, err = _run_host(["plugin", "validate", "--strict", str(source)])
    if code != 0:
        detail = (err or out).strip() or "the validator reported no detail"
        raise Refused(f"the vkit package at {source} did not validate: {detail}")


def install(context: Context, scope: str = "user", **_: Any) -> dict[str, Any]:
    """Install the host plugin, skills, and hooks at a chosen scope.

    Validation first, then the host CLI. The host owns where a plugin lives, how
    it is enabled, and how its configuration is stored; this never copies the
    package into the host's directories by hand, because a second writer is a
    second answer that drifts the moment the host changes. A package that would
    not load is refused before anything is placed.
    """
    if scope != "user":
        raise Refused(
            f"scope {scope!r} is not supported; this build installs at 'user' scope only"
        )
    root = _marketplace_root()
    source = root / pluginres.PLUGIN_DIR
    _validate(root)

    # The host installs plugins from a marketplace, so the repository declares
    # one. Adding it twice is refused by the host rather than duplicated here.
    if not _marketplace_registered():
        code, out, err = _run_host(["plugin", "marketplace", "add", str(root)])
        if code != 0:
            detail = (err or out).strip() or "the host CLI reported no detail"
            raise Refused(f"the host refused to register the vkit marketplace: {detail}")

    code, out, err = _run_host(["plugin", "install", PLUGIN_ID, "--scope", scope])
    if code != 0:
        detail = (err or out).strip() or "the host CLI reported no detail"
        raise Refused(f"the host CLI refused to install {PLUGIN_ID}: {detail}")
    return {
        "operation": "install",
        "scope": scope,
        "plugin": PLUGIN_ID,
        "installed_from": str(source),
        "validated": True,
        "already_installed": "already installed" in out,
        "host_output": out.strip(),
    }


def repair(context: Context, scope: str = "user", **_: Any) -> dict[str, Any]:
    """Re-apply a drifted installation.

    Idempotent: it re-validates and re-installs through the same host call, and
    the host reports an already-correct installation as already installed rather
    than stacking a second copy. Running it twice leaves the same state as
    running it once, which is the property that makes it safe to retry.

    A drifted install is a version or enablement the host knows about but which
    no longer matches the package. There is no such installation here to repair,
    so this refuses rather than reporting a success for a no-op.
    """
    if scope != "user":
        raise Refused(
            f"scope {scope!r} is not supported; this build repairs at 'user' scope only"
        )
    _validate(_marketplace_root())
    if _installed_plugin_record(PLUGIN_ID) is None:
        raise Refused(
            f"{PLUGIN_ID} is not installed for this host, so there is no drifted "
            f"installation to repair. Use install first."
        )
    code, out, err = _run_host(["plugin", "update", PLUGIN_ID])
    if code != 0:
        detail = (err or out).strip() or "the host CLI reported no detail"
        raise Refused(f"the host CLI refused to update {PLUGIN_ID}: {detail}")
    return {
        "operation": "repair",
        "scope": scope,
        "plugin": PLUGIN_ID,
        "validated": True,
        "host_output": out.strip(),
    }


def remove(context: Context, **_: Any) -> dict[str, Any]:
    """Undo an installation, leaving no orphans.

    Uninstalling is only half of removing. The host keeps the marketplace
    registration and the cache directory after `plugin uninstall`, so both are
    taken out here and then verified: `remove` reports success only once the
    host lists no plugin under this id and no cache directory remains. That
    check is the difference between "the command succeeded" and "nothing is
    left behind".
    """
    record = _installed_plugin_record(PLUGIN_ID)
    if record is None:
        raise Refused(f"{PLUGIN_ID} is not installed for this host, so there is nothing to remove.")

    code, out, err = _run_host(["plugin", "uninstall", PLUGIN_ID])
    if code != 0:
        detail = (err or out).strip() or "the host CLI reported no detail"
        raise Refused(f"the host CLI refused to uninstall {PLUGIN_ID}: {detail}")

    # The host leaves the marketplace registered. Removing it here is what
    # "no orphans" means; otherwise a later install silently reuses a
    # registration pointing at a checkout that may be gone.
    if _marketplace_registered():
        _code, out2, err2 = _run_host(["plugin", "marketplace", "remove", _MARKETPLACE])
        if _code != 0:
            detail = (err2 or out2).strip() or "the host CLI reported no detail"
            raise Refused(
                f"the plugin was uninstalled but the {PLUGIN_ID!r} marketplace could not be "
                f"removed, so a stale registration is left behind: {detail}"
            )

    remaining = _installed_plugin_record(PLUGIN_ID)
    if remaining is not None:
        raise Refused(
            f"the host still lists the plugin at {remaining.get('installPath')!r} after "
            f"uninstall; the installation is not fully removed."
        )
    cache = Path.home() / ".claude" / "plugins" / "cache" / _MARKETPLACE / _PLUGIN_NAME
    leftover = _remove_cache(cache)
    if leftover is not None:
        # Reported as a refusal, not a warning. "No orphans" that quietly leaves
        # a directory behind is the claim this operation exists to make true, and
        # `shutil.rmtree(ignore_errors=True)` is exactly how it would: it returns
        # success while a locked file survives. The reason names the path.
        raise Refused(
            f"the plugin was uninstalled but its cache directory still holds files "
            f"under {leftover}. Close any process holding it and remove again; "
            f"nothing else is left installed."
        )
    return {
        "operation": "remove",
        "plugin": PLUGIN_ID,
        "uninstalled": True,
        # A Path here would reach the browser as a serialisation error, so every
        # path in a response is a string. The response is JSON and nothing else.
        "cache_removed": str(cache),
        "marketplace_removed": _MARKETPLACE,
        "host_output": out.strip(),
    }


def _remove_cache(cache: Path) -> str | None:
    """Delete the host's cache directory, or name what would not go.

    `shutil.rmtree(..., ignore_errors=True)` is the wrong call here: it reports
    success whatever it managed to delete, so a locked file leaves an orphan
    behind and the operation claims there is none. This deletes without the
    suppression, clearing the read-only flag Windows puts on plugin files, and
    returns a description of whatever survived so the caller can name it rather
    than guess.
    """
    if not cache.is_dir():
        return None
    import shutil
    import stat

    def clear_readonly(func, path, _exc) -> None:
        os.chmod(path, stat.S_IWRITE)
        func(path)

    # `onerror` was renamed `onexc` in 3.12 and removed later; the module must
    # work on both, because the read-only flag is a Windows fact, not a version
    # fact, and silently skipping it is what leaves the orphan.
    handler = (
        {"onexc": clear_readonly} if sys.version_info >= (3, 12) else {"onerror": clear_readonly}
    )
    try:
        shutil.rmtree(cache, **handler)
    except OSError as exc:
        return f"{exc.filename or cache} ({exc.strerror})"
    if cache.exists():
        # Returned without raising and the directory is still there, which is
        # what a partly-deleted tree looks like on Windows.
        return str(cache)
    return None


def enroll(context: Context, accepted: bool = False, **_: Any) -> dict[str, Any]:
    """Register a repository as verified, through the core enrollment record.

    This used to write its own `enrollment.json` with keys the rest of the system
    does not read — `accepted` and `configuration_digest`, where
    `enroll.read_enrollment` expects `state` and `policy_digest`. Two formats for
    one fact is how a repository came to read as enrolled here and not enrolled
    everywhere else. There is one record now, written by `vkit.enroll`, and the
    state reported here is read back from it rather than assumed from the call.

    Without `accepted` this reports the policy and changes nothing at all, so
    execution stays disabled until a person accepts. The proposal the core
    writes beside the manifest is deliberately not written from here: this
    package must leave the working tree byte-identical, and a proposal is a
    file in it.
    """
    if context.manifest is None:
        raise Refused(
            context.manifest_error
            or "the repository has no manifest to enroll against; create "
               "verification/manifest.json first"
        )
    if accepted:
        try:
            core_enroll.accept(context.project)
        except core_enroll.EnrollmentError as exc:
            raise Refused(str(exc)) from exc

    state = core_enroll.read_enrollment(context.project)
    return {
        "operation": "enroll",
        "enrolled": state.state is core_enroll.State.ACCEPTED,
        "accepted": state.state is core_enroll.State.ACCEPTED,
        "state": state.state.value,
        "policy": [
            {"id": check.id, "command": list(check.argv),
             "timeout_seconds": check.timeout_seconds}
            for check in context.manifest.checks.values()
        ],
        "policy_digest": state.policy_digest,
        "record": str(core_enroll.record_path(context.project)),
        "note": (
            "execution stays disabled until this policy is accepted; re-run with "
            "accepted=true" if state.state is not core_enroll.State.ACCEPTED else
            "this repository's policy is accepted; its registered checks may run"
        ),
    }


#: The host names this plugin by id. The marketplace is the repository's own
#: `.claude-plugin/marketplace.json`, so the id is vkit@vkit in every operation.
#: install, repair and remove all resolve it from here, so they cannot drift
#: onto two different names for one package.
_PLUGIN_NAME = "vkit"
_MARKETPLACE = "vkit"
PLUGIN_ID = f"{_PLUGIN_NAME}@{_MARKETPLACE}"



#: The writable surface as callables, by name. A router that resolves an
#: operation through this mapping cannot reach a function that is not on the
#: list, which is the property that makes the list the whole surface.
#:
#: `save_project_config` is added at the end of the module, where it is defined,
#: because a name in this table is a promise that the function exists and Python
#: evaluates the whole table at import time.
OPERATIONS: dict[str, Any] = {
    "enroll": enroll,
    "install": install,
    "repair": repair,
    "remove": remove,
    "run_check": run_check,
    "cancel_run": cancel_check_run,
}


def writable_surface() -> list[dict[str, Any]]:
    """The permitted operations, as the console shows them."""
    return [
        {
            "name": op.name,
            "effect": op.effect,
            "implemented": op.implemented,
            "writes": list(op.writes),
            "note": op.note,
        }
        for op in WRITABLE
    ]


def integrations_section(context: Context) -> dict[str, Any]:
    """Settings and integrations. Versions, connection status, setup actions.

    **Configuration editing landed at checkpoint 12.3, and it is one validated
    operation.** `proposals` carries what a person is about to edit, what was
    saved, what was approved and what was activated, as four separate facts
    because a page showing only the last thing written would let a candidate
    revision read as an accepted policy. `controls` names every control the
    editor draws together with the values it will accept, so the page cannot
    offer an option the validation refuses.

    The approved integration policy is reported beside the local configuration,
    with any mismatch, and is not writable from here at all. Protected authority
    comes from an independently approved reference, and this section's whole job
    is to make that visible rather than to imply a browser edit could grant it.

    A component's connection status is reported from whether the thing this build
    would use to reach it exists. The host CLI is a real dependency of install,
    repair and remove, so its absence is a missing prerequisite for those three
    named operations rather than a general warning.
    """
    import shutil

    from .. import __version__

    host_cli = shutil.which("claude")
    record = _installed_plugin_record(PLUGIN_ID)
    try:
        package = str(_marketplace_root() / pluginres.PLUGIN_DIR)
    except Refused:
        package = None

    components = [
        {
            "id": "vkit",
            "kind": "core",
            "version": __version__,
            "present": True,
            "status": "this console is serving from it",
            "detail": str(context.project.root),
        },
        {
            "id": PLUGIN_ID,
            "kind": "host plugin",
            "version": (record or {}).get("version"),
            "present": record is not None,
            "status": "installed" if record is not None else "not installed",
            "detail": (
                f"at {(record or {}).get('installPath')} for this host"
                if record is not None
                else "the host has no record of this plugin; install is available below"
            ),
        },
        {
            "id": "claude CLI",
            "kind": "host CLI",
            "version": None,
            "present": host_cli is not None,
            "status": "on PATH" if host_cli is not None else "not on PATH",
            "detail": (
                host_cli
                if host_cli is not None
                else (
                    "install, repair and remove all shell out to this CLI, so those "
                    "three operations cannot run until it is installed"
                )
            ),
        },
    ]
    actions = []
    for op in WRITABLE:
        if op.name not in ("install", "repair", "remove", "enroll", "save_project_config"):
            continue
        blocked = _host_cli_block(op.name) if host_cli is None and op.name in (
            "install", "repair", "remove",
        ) else None
        actions.append({
            "operation": op.name,
            "effect": op.effect,
            # The reason travels with the action, so a withheld button names the
            # prerequisite rather than simply vanishing.
            "available": blocked is None,
            "reason": blocked,
            "note": op.note,
        })
    return {
        "components": components,
        "actions": actions,
        "editable": True,
        "proposals": configuration_section(context),
        "package": package,
        "marketplace_registered": _marketplace_registered(),
        # Checkpoint 12.4's panel, riding this section because `api.py` owns the
        # route table and no route may be added for it. The section is already
        # where an operator looks for "which components are present and what
        # needs setup", and this answers exactly that for the agent connection.
        "connection": mcp_connection(context),
        "note": (
            "Project configuration is editable through one validated operation. "
            "Saving a document produces a candidate revision; approving and "
            "activating it are separate steps, and neither grants protected "
            "integration authority"
        ),
    }


def _host_cli_block(name: str) -> str | None:
    """Why this operation cannot run without the host CLI, or None if it can."""
    return (
        f"the 'claude' CLI is not on PATH, and {name} runs it; installing Claude "
        f"Code is the prerequisite. This is the core's own refusal, not this "
        f"page's."
    )


# ------------------------------------------------- checkpoint 12.4: the panel
#
# The connection panel answers "what do I paste into my agent", and it is the
# first place in this product that has to keep two facts apart that look like one.
#
# **Configured** is what a host's file says. The console did not write that
# file, so it can only read it back. **Connected** is what a real MCP handshake
# proves: a subprocess launched, an `initialize` frame answered, `tools/list`
# read off the wire. `vkit mcp serve --json` settles neither -- it returns before
# touching the transport, so it prints a full six-tool catalogue with the SDK
# absent while the same command without `--json` exits 5. A catalogue is a
# statement about the table, never about a running server, and the panel reports
# it as a third thing rather than folding it into either.
#
# **The probe crosses a process boundary, so the guards live at it.** Everything
# it accepts is checked before a byte is written to a pipe: the command must be
# an existing file, the root must be a project, and the whole thing is bounded by
# a deadline. Inside that boundary the protocol is just bytes.


@dataclass(frozen=True)
class Probe:
    """One handshake attempt, and its outcome as three distinct answers.

    Modelled as one type because these three are genuinely different facts about
    one attempt, and a reader who cannot tell them apart is the reader this
    checkpoint exists for:

        not_probed      nothing was measured
        not_connected   something was measured and it did not complete
        connected       a real `initialize` and `tools/list` came back

    `not_probed` is not `not_connected` with a default. A panel that reported
    every unprobed host as broken would train its reader to ignore the field,
    which is the failure mode this whole section is built against.
    """

    state: str
    handshake: bool
    reason: str | None = None
    tool_names: list[str] = field(default_factory=list)
    server_info: dict[str, str] = field(default_factory=dict)
    protocol_version: str | None = None
    project_root: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "handshake": self.handshake,
            "reason": self.reason,
            "tool_names": list(self.tool_names),
            "server_info": dict(self.server_info),
            "protocol_version": self.protocol_version,
            "project_root": self.project_root,
        }


def _console_script(name: str = "vkit") -> str | None:
    """The absolute path of the console script THIS installation placed on disk.

    Read out of `importlib.metadata`, not predicted from `sys.executable` and not
    taken from `shutil.which`. Both alternatives are wrong in ways this product
    has already been bitten by:

    * Predicting the layout from the interpreter's own directory assumes one.
      A POSIX `setup-python` puts the interpreter under a framework prefix and
      the script in `~/.local/bin`, so nothing need be beside it.
    * `shutil.which` answers for whatever is first on PATH, which on a host with
      two installs is the other one. Measured on the development machine:
      `which vkit` resolves to `C:\\CLI\\cx\\.venv\\Scripts\\vkit.EXE` while the
      console serving this panel runs from a different venv entirely. A snippet
      built from the PATH answer would start a server bound to that install and
      report success, which is the false receipt this checkpoint removes.

    `None` means this installation placed no such script, which is a real answer
    for a source checkout that was never installed. The panel reports it rather
    than falling back to a guess, because a fallback is what produced the wrong
    path in the first place.
    """
    from importlib.metadata import PackageNotFoundError, entry_points

    try:
        found_scripts = entry_points(group="console_scripts")
    except PackageNotFoundError:  # pragma: no cover - no distribution at all
        return None
    for entry in found_scripts:
        if entry.name != name:
            continue
        distribution = entry.dist
        for relative in distribution.files or ():
            if PureWindowsPath(str(relative)).stem.lower() != name.lower():
                continue
            resolved = Path(distribution.locate_file(relative))
            if resolved.is_file():
                return str(resolved.resolve())
    return None


def connection_snippet(context: Context) -> dict[str, Any]:
    """The host-neutral configuration an operator pastes into any MCP host.

    The shape is the one every host reading `mcpServers` understands. It is NOT
    `plugin/.mcp.json`, which is one host's configuration: that file names a
    bare `vkit` that only resolves under that host's PATH, and carries a
    `${user_config.project_root}` substitution this console has no business
    emitting.

    **`note` is load-bearing and the assertion is on it.** A generated snippet
    is a starting point. Nothing here has been started, and a panel that let a
    reader take a pasted JSON block for a working connection would be repeating
    the exact defect this section corrects.
    """
    command = _console_script()
    root = str(context.project.root)
    return {
        "mcpServers": {
            "vkit": {
                "command": command if command is not None else "vkit",
                "args": ["mcp", "serve", "--project", root],
            }
        },
        "resolved": command is not None,
        "note": (
            "This is generated configuration, not a verified connection. "
            "Nothing here has been started; paste it into your host, then read "
            "the connection state below. A host reports CONFIGURED when its own "
            "file says so, and CONNECTED only after a real MCP handshake."
        ),
    }


def host_config_document(
    context: Context, *, command: str, project_root: str, host: str = "test"
) -> dict[str, Any]:
    """One host's configuration, as a value.

    Held as a value rather than read from a file so the panel's two halves can be
    exercised independently: `mcp_connection` takes this as an argument, so a
    test can hand it a command that does not exist and read what the panel says,
    without needing a real host to have been misconfigured first. The real
    reader is `discover_host`, and this is the shape it produces.
    """
    return {"host": host, "command": command, "project_root": project_root}


def unknown_host(context: Context) -> dict[str, Any]:
    """No host configuration was found on this machine, and that is the answer.

    `None` rather than a default host with invented state. The panel publishes
    the snippet to paste and says plainly that nothing is configured, because
    answering about a host nobody named would be the panel deciding which host
    the operator uses.
    """
    return {"host": "unknown", "command": None, "project_root": None, "discovered": False}


def discover_host(context: Context) -> dict[str, Any]:
    """Read the host's own `mcpServers` record, if this machine has one.

    Claude Code's user record is at `~/.claude.json`, and its `mcpServers` key
    is read directly. It is a READ: the console does not write it, and `remove`
    stays the only operation that touches host state. A host with no record
    returns `unknown_host` rather than an empty one, so the panel can say
    "nothing configured" instead of "configured with nothing".
    """
    record = Path.home() / ".claude.json"
    if not record.is_file():
        return unknown_host(context)
    try:
        document = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return unknown_host(context)
    entry = (document.get("mcpServers") or {}).get("vkit")
    if not isinstance(entry, dict) or not entry.get("command"):
        return unknown_host(context)
    args = entry.get("args") or []
    root = None
    if "--project" in args:
        after = args[list(args).index("--project") + 1:]
        if after:
            root = after[0]
    return {
        "host": "claude-code",
        "command": entry["command"],
        "project_root": root,
        "discovered": True,
    }


def _configured_state(host: dict[str, Any], console_root: str) -> dict[str, Any]:
    """What the host's file says, with nothing inferred from whether it works.

    The `command` is reported exactly as the file spells it, which is the whole
    point: a host configured with a bare `vkit` and a host configured with an
    absolute path are both configured, and the panel does not rewrite one into
    the other. `command_exists` is a separate fact about this machine's disk,
    and it is reported next to the file's own claim rather than instead of it.
    """
    command = host.get("command")
    root = host.get("project_root")
    return {
        "host": host.get("host", "unknown"),
        "present": command is not None,
        "command": command,
        "command_exists": bool(command) and Path(command).is_file(),
        "project_root": root,
        "project_root_matches_console": root is not None and root == console_root,
        "source": "host configuration file" if host.get("discovered") else "none found",
    }


def probe_connection(
    command: str, project_root: str, *, timeout: float = PROBE_TIMEOUT_SECONDS
) -> Probe:
    """Complete a real MCP handshake against a real subprocess, or report why not.

    This is the artifact. A generated snippet is a proxy for a connection and a
    `tools/list` catalogue is a proxy for a connection; only a subprocess that
    answered `initialize` and `tools/list` is the thing itself.

    **It writes bytes on the wire, not JSON-RPC objects.** The frames are
    assembled as text and written to a pipe by hand rather than handed to the
    server's own SDK session, because a client built from the same SDK as the
    server would agree with it about a changed contract. The rule this file
    inherits from `tests/mcp_client.py`: the reader must not be able to agree
    with the server by construction.

    Every refusal before the launch is a boundary decision, and it happens here
    where the process boundary is: a command that is not an existing file, and a
    root that is not a project. Both are reported as `not_connected` with the
    reason, because an operator reading a panel needs one reading for "it does
    not work" rather than a traceback from a read-only view.

    The deadline is the outer bound on the whole exchange. A server that starts
    and then never answers is exactly the failure this panel has to survive, and
    an unbounded read would hang the console rather than report it.
    """
    resolved = Path(command)
    if not resolved.is_file():
        return Probe(
            "not_connected", False,
            reason=f"{command} is not a file on this machine, so no server could start from it",
        )
    try:
        open_project(project_root)
    except (ProjectError, OSError) as exc:
        return Probe(
            "not_connected", False,
            reason=f"{project_root} is not a project root this server could bind to: {exc}",
        )

    try:
        return _handshake(resolved, project_root, timeout)
    except OSError as exc:
        # `FileNotFoundError` and a permission refusal land here, and both are
        # answers about this machine rather than bugs in the panel.
        return Probe(
            "not_connected", False,
            reason=f"the server could not be launched: {exc}",
            project_root=project_root,
        )
    except ProbeTimeout as exc:
        return Probe("not_connected", False, reason=str(exc), project_root=project_root)


class ProbeTimeout(Exception):
    """The handshake did not complete inside its deadline."""


def _handshake(command: Path, project_root: str, timeout: float) -> Probe:
    """One `initialize`, one `tools/list`, over a real pipe pair.

    Read on a thread with a deadline rather than with a blocking read, because a
    server that answers neither frame would otherwise hold this call open until
    the console's own request timed out, which is the failure the probe exists
    to report.
    """
    lines: list[str] = []
    errors: list[str] = []
    process = _launch(command, project_root)
    try:
        _pump(process, lines, errors)
        _exchange(process, lines, errors, timeout)
    finally:
        _stop(process)
    result = _read_probe(lines, errors)
    # The root the probe ASKED for, reported on the outcome. It is the panel's
    # own record of what it launched, which is what an operator needs when the
    # handshake fails: the server's own stderr names its bound root, and this is
    # the value that was handed to it.
    return replace(result, project_root=project_root)


def _launch(command: Path, project_root: str) -> Any:
    """Start the server with three pipes and no console window.

    `vkit.nowindow.hidden_window` rather than raw keywords: on Windows a console
    application launched from the console's own process opens a window over
    whatever the operator was using, and the console is exactly the process a
    person is looking at. Binary pipes, because a child writing one protocol
    frame to a pipe nobody is reading blocks in its own line buffering.
    """
    from .. import nowindow

    environment = dict(os.environ)
    # This console's own `src` first, so the probe measures the code serving the
    # page rather than whichever install happens to lead on the path.
    here = Path(__file__).resolve().parents[2]
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(here), environment.get("PYTHONPATH", "")]
    )
    return subprocess.Popen(
        [str(command), "mcp", "serve", "--project", project_root],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        bufsize=0, env=environment, cwd=str(here),
        **nowindow.hidden_window(),
    )


def _pump(process: Any, lines: list[str], errors: list[str]) -> None:
    """Two reader threads, one per stream, and no other reader anywhere.

    A second reader racing the first for the same pipe loses frames silently,
    which shows up as a probe that hangs rather than as a failure.
    """
    def _read(stream: Any, sink: list[str]) -> None:
        try:
            for raw in stream:
                sink.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        except (OSError, ValueError):
            pass

    for stream, sink in ((process.stdout, lines), (process.stderr, errors)):
        threading.Thread(target=_read, args=(stream, sink), daemon=True).start()


def _exchange(process: Any, lines: list[str], errors: list[str], timeout: float) -> None:
    """Write the two frames a client must send, and wait for both answers.

    `initialize` first, because the protocol requires it and a `tools/list`
    before it is a `tools/list` a real client could not have sent. The
    notification is sent after the result arrives, which is where it belongs.
    """
    deadline = time.monotonic() + timeout
    _send(process, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "vkit-console-probe", "version": "1.0.0"},
        },
    })
    _await(lines, deadline, errors, "initialize")
    _send(process, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    _send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    _await(lines, deadline, errors, "tools/list")


def _send(process: Any, frame: dict[str, Any]) -> None:
    stream = process.stdin
    if stream is None:
        raise ProbeTimeout("the server's stdin was not connected")
    try:
        stream.write((json.dumps(frame) + "\n").encode("utf-8"))
        stream.flush()
    except (OSError, ValueError) as exc:
        raise ProbeTimeout(f"the server stopped reading before the exchange finished: {exc}")


def _await(
    lines: list[str], deadline: float, errors: list[str], method: str
) -> dict[str, Any]:
    """The frame answering `method`, or a failure naming what went wrong."""
    while True:
        for candidate in list(lines):
            if not candidate.strip():
                lines.remove(candidate)
                continue
            try:
                frame = json.loads(candidate)
            except json.JSONDecodeError:
                # stdout carries protocol frames only. A line that is not JSON is
                # a broken server, not a frame to skip past, and continuing here
                # is how a banner silently becomes the answer to tools/list.
                raise ProbeTimeout(
                    f"the server wrote a line that is not a protocol frame, which "
                    f"corrupts the stream for any client: {candidate[:200]!r}"
                )
            if frame.get("id") == 1 and method == "initialize":
                return frame
            if frame.get("id") == 2 and method == "tools/list":
                return frame
        if time.monotonic() >= deadline:
            raise ProbeTimeout(f"no {method} answer within {timeout:g}s: {tail(errors) or 'no stderr'}")
        time.sleep(0.05)


def tail(errors: list[str], count: int = 5) -> str:
    """The last few stderr lines, which is where a refusal goes.

    stdout is the protocol channel and a server that failed to start says why on
    stderr, so this is the only place its own explanation can be read from.
    """
    return " | ".join(errors[-count:])


def _stop(process: Any) -> None:
    """Close the pipe and wait, killing rather than leaving a server running.

    A leaked `mcp serve` holds the bound project open, so a panel the operator
    opened once would accumulate processes for as long as the console runs.
    """
    try:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - already killed
            pass


def _read_probe(lines: list[str], errors: list[str]) -> Probe:
    """The handshake's outcome, read off the recorded frames.

    `tools/list` is the frame that settles it. A server that handshakes and then
    publishes nothing is connected to nothing useful, and the six names are what
    a host will actually call.
    """
    handshake: dict[str, Any] = {}
    tools: list[dict[str, Any]] = []
    for candidate in lines:
        try:
            frame = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        result = frame.get("result") or {}
        if frame.get("id") == 1:
            handshake = result
        elif frame.get("id") == 2:
            tools = result.get("tools") or []
    if not handshake:
        return Probe("not_connected", False, reason=tail(errors) or "no handshake frame")
    if not tools:
        return Probe("not_connected", False, reason="the server published no tools")
    return Probe(
        "connected", True,
        tool_names=[str(tool.get("name")) for tool in tools],
        server_info={
            key: str(value)
            for key, value in (handshake.get("serverInfo") or {}).items()
        },
        protocol_version=handshake.get("protocolVersion"),
    )


def mcp_catalogue(context: Context) -> dict[str, Any]:
    """The six-tool catalogue, and the fact that it proves nothing about serving.

    This is what `vkit mcp serve --project ROOT --json` returns, called here
    rather than shelled out so the panel does not launch a process to learn a
    constant. `touches_transport` is False and it is not decoration: the command
    returns before the transport is touched, so it prints a full catalogue with
    the SDK absent while the same command without `--json` exits 5.
    """
    from ..mcp import tool_definitions

    return {
        "command": "mcp serve",
        "project": str(context.project.root),
        "tools": tool_definitions(),
        "touches_transport": False,
        "note": (
            "The catalogue is the published tool table, read without starting a "
            "transport. It is what the server would advertise, and is not "
            "evidence that a server can serve: `vkit mcp serve --json` returns "
            "before the transport is touched and exits 0 with the MCP SDK "
            "absent."
        ),
    }


def mcp_connection(
    context: Context, *, host: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The connection panel's whole document, in one value.

    Three things, deliberately not one status. `configured` reads the host's own
    file. `connected` is a real handshake, or `not_probed` when there was nothing
    to probe. `catalogue` is the tool table, which is evidence about neither.

    **The probe runs on its own, with no button.** Two facts made a button the
    wrong shape. `api.py` owns the route table and this package cannot add a
    route, so there was nowhere for a "probe" request to arrive; and the
    operator's question is "is this working", which a panel that answers
    `not_probed` until a button is found has not answered at all.

    So the probe is spent where it is meaningful and not otherwise: a handshake
    runs only when the host names both a command and a project root. Nothing is
    configured, nothing is launched, and the panel says `not_probed` -- which is
    the truth. Something is configured, the subprocess runs and the panel says
    what actually happened. Measured at ~0.9s per handshake on this host, which
    is the price of an answer that is never stale.
    """
    chosen = discover_host(context) if host is None else host
    command = chosen.get("command")
    root = chosen.get("project_root")
    if command and root:
        result = probe_connection(str(command), str(root))
    else:
        result = Probe("not_probed", False)
    return {
        "snippet": connection_snippet(context),
        "text": mcp_connection_text(context),
        "configured": _configured_state(chosen, str(context.project.root)),
        "connected": result.to_json(),
        "catalogue": mcp_catalogue(context),
        "capability_gaps": capability_gaps(context, chosen),
        "on_path_vkit": _on_path_vkit(),
    }


def mcp_connection_text(context: Context) -> str:
    """The snippet as an operator pastes it, and as a page renders it.

    `ensure_ascii=False`, so a project path with an accent in it survives into
    the text. The escaped form is valid JSON that names a path the filesystem
    does not have, which is worse than ugly.
    """
    return json.dumps(connection_snippet(context), indent=2, ensure_ascii=False)


def _on_path_vkit() -> str | None:
    """What PATH would run, reported as a separate fact.

    Kept beside the resolved command rather than instead of it, because on a
    host with two installs these genuinely differ and an operator deciding which
    one their host will launch needs both. On this machine they do differ.
    """
    import shutil

    return shutil.which("vkit")


#: What a host can and cannot do, as a table rather than a set of sentences.
#:
#: The whole point of this panel is that "connected" is not the whole story. A
#: standalone MCP host can call `task_begin`, `check_start`, `run_get` and
#: `task_finalize` explicitly and reach a real verdict. It cannot inherit
#: Claude's automatic completion gate, and it cannot get automatic edit cleanup,
#: because those are lifecycle hooks belonging to that host, not properties of
#: the protocol. A host without hooks is NOT less correct; it is explicit, and
#: an operator who believes otherwise will wait for a gate that will never fire.
_CAPABILITY_ROWS: tuple[dict[str, Any], ...] = (
    {
        "capability": "discover and call the six tools",
        "requires": "MCP stdio",
        "automatic": False,
        "detail": "project_inspect, task_begin, check_start, run_get, run_cancel "
                  "and task_finalize, over standard MCP discovery and calls",
    },
    {
        "capability": "explicit verification and finalization",
        "requires": "MCP stdio",
        "automatic": False,
        "detail": "a host may call check_start and task_finalize itself and reach "
                  "READY, REJECTED or BLOCKED from real evidence",
    },
    {
        "capability": "automatic completion gate on Stop",
        "requires": "host lifecycle hooks",
        "automatic": True,
        "detail": "Claude Code's Stop hook is what blocks a finish until a task "
                  "finalizes. A host without lifecycle hooks does NOT inherit "
                  "it: the gate is the host's, and the operator or the agent "
                  "calls task_finalize explicitly.",
    },
    {
        "capability": "automatic edit cleanup",
        "requires": "host lifecycle hooks",
        "automatic": True,
        "detail": "the PostToolUse hook on Edit and Write is what offers cleanup. "
                  "Without lifecycle hooks an edit is not cleaned up until a "
                  "tool call asks.",
    },
    {
        "capability": "session and agent identity",
        "requires": "nothing beyond MCP",
        "automatic": False,
        "detail": "task_begin accepts a host object carrying session_id and an "
                  "optional agent_id, so any host may bind its own identity "
                  "explicitly. Claude's SessionStart and SubagentStart hooks "
                  "supply it automatically; a standalone host passes what it has, "
                  "or binds no host at all.",
    },
    {
        "capability": "workflow skills",
        "requires": "the Claude plugin, or manual guidance",
        "automatic": False,
        "detail": "four skills offer onboarding, the work sequence, running "
                  "verification and explaining status. They are workflow help, "
                  "not a prerequisite for correctness: the six tools do not "
                  "depend on any of them.",
    },
)


def capability_gaps(
    context: Context, host: dict[str, Any] | None = None
) -> dict[str, Any]:
    """What this host has, and what it does not.

    A row is `supported` when this host satisfies its `requires`. The rows that
    need lifecycle hooks are the interesting ones, and they are the reason this
    function exists: "connected" and "as capable as Claude Code" are different
    claims, and only the second one is false for a standalone host.

    Only two host shapes are recognised. A host this build has no adapter for is
    reported as `unknown` with every hook-dependent capability withheld, which is
    the honest answer rather than an optimistic default. Building a per-host
    adapter for a host nobody has selected is the thing checkpoint 12.4 says not
    to do.
    """
    chosen = discover_host(context) if host is None else host
    name = str(chosen.get("host", "unknown"))
    if name == "claude-code":
        shape, lifecycle = "claude", True
    elif name == "test":
        shape, lifecycle = "test", False
    else:
        shape, lifecycle = "unknown", False

    rows = []
    for row in _CAPABILITY_ROWS:
        needs_hooks = row["requires"] == "host lifecycle hooks"
        supported = not needs_hooks or lifecycle
        rows.append({**row, "supported": supported, "row": shape})
    return {
        "host": name,
        "shape": shape,
        "lifecycle_hooks": lifecycle,
        "rows": rows,
        "missing": [row["capability"] for row in rows if not row["supported"]],
        "note": (
            "Supported means this host provides what the row requires. It is not "
            "a claim that the capability is automatic: an agent on a host with "
            "no lifecycle hooks calls task_finalize explicitly, and a missing "
            "gate is a missing gate."
        ),
    }


# ----------------------------------------------------------------- internals


def _bounded_limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise Refused(f"limit must be a positive integer, got {limit!r}")
    return min(limit, MAX_RUN_LIMIT)


def _bounded_bytes(max_bytes: int) -> int:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise Refused(f"max_bytes must be a positive integer, got {max_bytes!r}")
    return min(max_bytes, MAX_LOG_BYTES)


def host() -> str:
    """The one address this console will bind."""
    return LOOPBACK_HOST


# ------------------------------------------------------- checkpoint 12.3
#
# A validated, allowlisted write to two fixed project configuration files. What
# makes it safe is narrowness, and there are four separate things that have to
# hold before a byte is written. Each is stated where it is enforced rather than
# in a docstring the code could contradict.
#
#   1. The two paths are a closed pair and no caller can name one.
#   2. The whole proposed document is parsed before anything is compared, and
#      every obligation in it must be one the registered manifest declares.
#   3. The expected current digest must match, or two browser tabs cannot both
#      write.
#   4. Both documents are published together, and a failure on either leaves the
#      previous bytes in place.
#
# `cleanup.policy` is imported INSIDE the functions that use it, for the same
# reason `cleanup_section` defers `cleanup.hooks`: `cleanup.comments` imports
# `console.plan` for `under_protected_path`, so a module-level import here closes
# a cycle that a package-level import order happens to hide and a different one
# exposes. The dependency direction stays cleanup -> console.

#: The key set a project policy document may declare. A closed set rather than a
#: per-key type check, because that is what makes "no model credentials" and "no
#: unrestricted command textbox" structural: there is no field that could hold
#: either, so there is no validation of them to forget.
PROJECT_POLICY_KEYS: frozenset[str] = frozenset({
    "schema_version", "description", "required_checks", "connection", "cleanup", "checks",
})

#: The only schema version this build writes. Named rather than taken from the
#: cleanup policy's own version because these are two documents with two readers,
#: and a reader that inferred one version from the other would couple them.
PROJECT_POLICY_VERSION = 1

#: The harness connection scopes. A closed vocabulary of three, and the only one
#: that widens anything is `loopback`, which names the address the console already
#: binds. There is no scope that names a host, a token or a credential, because the
#: chosen harness owns its model and provider settings.
CONNECTION_SCOPES: tuple[str, ...] = ("none", "loopback", "loopback_and_named")

#: The two fixed paths, in the order the recovery list and the publish walk them.
#: Read from `plan` rather than repeated, so the allowlist has one declaration and
#: this module cannot drift onto a second pair.
_RELATIVE: tuple[str, str] = PROJECT_CONFIG_PATHS


class ConfigurationRefused(Exception):
    """The proposed configuration is not usable, or the operator's view is stale.

    Raised as its own type rather than as `Refused` because a caller has to tell
    "the document you sent is wrong" from "the configuration changed under you".
    The first is the operator's mistake and the second is a conflict with another
    writer, and both reach a browser as a 409 while meaning opposite things.
    """


@dataclass(frozen=True)
class _Proposal:
    """A validated proposal: the document to store, and what was compared.

    Two values rather than one because the document and the comparison answer
    different questions and a reader must be able to ask either. The document is
    what gets written and what a second preview re-parses to the same value. The
    parsed form is the typed obligation set the removal comparison is done over,
    so nothing anywhere has to re-read the stored shape to learn what a name
    means.
    """

    document: dict[str, Any]
    #: check id -> tuple of (typed obligation, rendered name, raw document)
    obligations: dict[str, tuple[tuple[Any, str, dict[str, Any]], ...]]
    cleanup: dict[str, Any]
    connection: dict[str, Any]
    checks: dict[str, Any]

    @property
    def required_ids(self) -> tuple[str, ...]:
        return tuple(entry["id"] for entry in self.document["required_checks"])


# ---------------------------------------------------------------- the document


def _canonical_bytes(value: Any) -> bytes:
    """The bytes published for a document, with a trailing newline.

    One encoder for both documents and for the recovery copies, so what is
    preserved is byte-identical to what was replaced. A writer that re-encoded on
    recovery would make the preserved copy a near-copy rather than a recovery.
    """
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _digest_of(value: Any) -> str:
    """The identity of a document's content.

    `CleanupPolicy.digest` hashes its own canonical form, so this module does not
    invent a second identity for the same document: the cleanup digest a receipt
    names and the one shown here are computed by the same arithmetic over the same
    keys.
    """
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _obligations_of(check_id: str, raw: Any) -> tuple[tuple[Any, str, dict[str, Any]], ...]:
    """Each obligation of one proposed requirement, three ways at once.

    The typed value is what the cross-reference compares, the rendered name is
    what a preview shows and a receipt uses, and the raw document is what gets
    stored. Carrying all three is what stops the stored policy and the removed-
    obligation list from being two readings of one fact: the name is always
    derived from the value, never stored in place of it.

    `obligation_from_json` parses rather than casts, so a document naming an
    obligation kind the union does not have is refused here rather than becoming a
    `CaseObligation` with a mangled field.
    """
    if not isinstance(raw, list) or not raw:
        raise ConfigurationRefused(
            f"required check {check_id!r}: obligations must be a nonempty list"
        )
    parsed: list[tuple[Any, str, dict[str, Any]]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ConfigurationRefused(
                f"required check {check_id!r}: each obligation must be an object"
            )
        try:
            value = obligation_from_json(entry)
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationRefused(
                f"required check {check_id!r}: cannot read an obligation: {exc}"
            ) from exc
        parsed.append((value, describe_obligation(value), entry))
    names = [name for _value, name, _document in parsed]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigurationRefused(
            f"required check {check_id!r}: duplicate obligation(s) "
            f"{', '.join(duplicates)}"
        )
    return tuple(parsed)


def _declared_obligations(manifest: Manifest) -> dict[str, tuple[str, ...]]:
    """What each registered check actually declares, read from the parsed manifest.

    Cross-referencing against this rather than against the raw manifest file means
    the comparison is between two parsed values, so reformatting the manifest is
    not a change and cannot smuggle one.
    """
    return {
        check.id: tuple(describe_obligation(o) for o in check.obligations())
        for check in manifest.checks.values()
    }


def _check_overrides(raw: Any, declared: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    """The per-check timeout and argument-list overrides a document may declare.

    Two fields, both optional, both checked against what the manifest already
    declares. An `argv` here is an ARGUMENT LIST and is validated as one: a
    nonempty list of nonempty strings. A joined string is refused by name,
    because a string is what a shell would take and this operation has no shell.
    """
    if not isinstance(raw, list) or not raw:
        raise ConfigurationRefused("checks must be a nonempty list of override objects")
    resolved: dict[str, Any] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise ConfigurationRefused("each checks entry must be an object")
        unknown = sorted(set(entry) - {"id", "timeout_seconds", "argv"})
        if unknown:
            raise ConfigurationRefused(
                f"checks entry {entry.get('id', '<unnamed>')!r} declares unsupported "
                f"key(s) {unknown}. A check override declares id, and optionally "
                f"timeout_seconds and argv as an argument list"
            )
        check_id = entry.get("id")
        if check_id not in declared:
            raise ConfigurationRefused(
                f"checks names {check_id!r}, which the registered manifest does not "
                f"define. The manifest defines: {', '.join(sorted(declared))}"
            )
        override: dict[str, Any] = {}
        if "timeout_seconds" in entry:
            timeout = entry["timeout_seconds"]
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise ConfigurationRefused(
                    f"check {check_id!r}: timeout_seconds must be a number, got {timeout!r}"
                )
            if not 0 < float(timeout) <= MAX_TIMEOUT_SECONDS:
                raise ConfigurationRefused(
                    f"check {check_id!r}: timeout_seconds must be positive and at most "
                    f"{MAX_TIMEOUT_SECONDS:g}, got {timeout!r}"
                )
            override["timeout_seconds"] = float(timeout)
        if "argv" in entry:
            argv = entry["argv"]
            if not isinstance(argv, list) or not argv:
                raise ConfigurationRefused(
                    f"check {check_id!r}: argv must be an argument list, a nonempty "
                    f"list of separate strings, not a command string"
                )
            if any(not isinstance(part, str) or not part for part in argv):
                raise ConfigurationRefused(
                    f"check {check_id!r}: every argv entry must be a nonempty string"
                )
            override["argv"] = list(argv)
        resolved[check_id] = override
    return resolved


def _parse_document(context: Context, raw: Any) -> _Proposal:
    """The proposed document, validated whole, or a refusal naming what is wrong.

    Every field is checked here before anything is compared against the current
    configuration, so a refusal about a malformed document is never reported as a
    conflict with what is on disk. Cross references come last and need the parsed
    manifest, because an obligation the manifest does not declare is the failure
    this whole checkpoint is about.
    """
    if not isinstance(raw, dict):
        raise ConfigurationRefused("a project policy must be a JSON object")
    unknown = sorted(set(raw) - PROJECT_POLICY_KEYS)
    if unknown:
        raise ConfigurationRefused(
            f"unsupported policy key(s) {unknown}. A project policy declares "
            f"schema_version, description, required_checks, connection and cleanup, "
            f"and nothing else: there is no field for a command string or for model "
            f"credentials, because the harness owns its model and this document owns "
            f"only what the project must prove"
        )
    if raw.get("schema_version") != PROJECT_POLICY_VERSION:
        raise ConfigurationRefused(
            f"unsupported project policy schema_version {raw.get('schema_version')!r}; "
            f"this build writes {PROJECT_POLICY_VERSION}"
        )
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ConfigurationRefused(f"description must be a string, got {description!r}")

    entries = raw.get("required_checks")
    if not isinstance(entries, list) or not entries:
        raise ConfigurationRefused(
            "required_checks must be a nonempty list of the obligations this "
            "project owes"
        )
    required: list[dict[str, Any]] = []
    parsed_by_id: dict[str, tuple[tuple[Any, str, dict[str, Any]], ...]] = {}
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigurationRefused("each required check must be an object")
        unknown_entry = sorted(set(entry) - {"id", "obligations"})
        if unknown_entry:
            raise ConfigurationRefused(
                f"required check {entry.get('id', '<unnamed>')!r} declares unsupported "
                f"key(s) {unknown_entry}. A required check declares id and obligations"
            )
        check_id = entry.get("id")
        if not isinstance(check_id, str) or not check_id:
            raise ConfigurationRefused("each required check needs a nonempty id string")
        if check_id in seen:
            raise ConfigurationRefused(f"required check {check_id!r} is listed twice")
        seen.add(check_id)
        obligations = _obligations_of(check_id, entry.get("obligations"))
        parsed_by_id[check_id] = obligations
        required.append({
            "id": check_id,
            # The raw documents go in unchanged, so a stored policy round-trips
            # through `_parse_document` to the value it was validated as.
            "obligations": [document for _value, _name, document in obligations],
        })

    if context.manifest is None:
        raise ConfigurationRefused(
            context.manifest_error
            or "the manifest could not be parsed, so no obligation in a proposed "
               "configuration can be cross-checked against what the project actually runs"
        )
    declared = _declared_obligations(context.manifest)
    for check_id, obligations in parsed_by_id.items():
        if check_id not in declared:
            raise ConfigurationRefused(
                f"the policy requires {check_id!r} and the registered manifest does "
                f"not define it. The manifest defines: {', '.join(sorted(declared))}"
            )
        names = [name for _value, name, _document in obligations]
        unknown_obligations = [name for name in names if name not in declared[check_id]]
        if unknown_obligations:
            raise ConfigurationRefused(
                f"the policy requires {check_id!r} to discharge "
                f"{', '.join(unknown_obligations)}, which that check does not declare. "
                f"It declares {', '.join(declared[check_id]) or '<none>'}. A "
                "configuration cannot require an obligation no registered check carries"
            )

    connection = raw.get("connection", {"scope": "none"})
    if not isinstance(connection, dict) or set(connection) - {"scope"}:
        raise ConfigurationRefused(
            f"connection must be an object declaring scope, one of "
            f"{list(CONNECTION_SCOPES)}"
        )
    scope = connection.get("scope", "none")
    if scope not in CONNECTION_SCOPES:
        raise ConfigurationRefused(
            f"unknown harness connection scope {scope!r}; the scopes are "
            f"{list(CONNECTION_SCOPES)}"
        )

    cleanup = raw.get("cleanup")
    if cleanup is None:
        cleanup = current_cleanup_document(context)
    else:
        # `parse_policy` is the core's own parser for this document, so the JSON
        # editor and the typed cleanup controls are validated by the same code
        # that decides whether cleanup may write. Its refusal is carried verbatim.
        from ..cleanup.policy import parse_policy as parse_cleanup_policy

        try:
            cleanup_policy = parse_cleanup_policy(cleanup)
        except Exception as exc:  # noqa: BLE001 - every parse_policy refusal means this
            raise ConfigurationRefused(str(exc)) from exc
        cleanup = cleanup_policy.to_json()

    checks = raw.get("checks")
    overrides = _check_overrides(checks, declared) if checks is not None else {}

    document = {
        "schema_version": PROJECT_POLICY_VERSION,
        "description": description,
        "required_checks": required,
        "connection": {"scope": scope},
        "cleanup": cleanup,
    }
    if overrides:
        document["checks"] = overrides
    return _Proposal(document, parsed_by_id, cleanup, {"scope": scope}, overrides)


# ------------------------------------------------------------ the two documents


def _policy_path(context: Context) -> Path:
    return context.project.root / _RELATIVE[0]


def _cleanup_path(context: Context) -> Path:
    return context.project.root / _RELATIVE[1]


#: Which of the two fixed documents each field lands in. A mapping rather than a
#: list of pairs because the diff and the publish both walk it, and two lists
#: walked in the same order is two orders to keep in step.
_FIELDS_IN: dict[tuple[str, str], str] = {
    ("description", "description"): _RELATIVE[0],
    ("connection", "scope"): _RELATIVE[0],
    ("cleanup", "mode"): _RELATIVE[1],
    ("cleanup", "enabled_rules"): _RELATIVE[1],
    ("cleanup", "excluded_paths"): _RELATIVE[1],
    ("cleanup", "python_version"): _RELATIVE[1],
    ("cleanup", "optimize_levels"): _RELATIVE[1],
}


def _read_document(path: Path) -> dict[str, Any] | None:
    """A stored document, or None when there is none or it is unreadable.

    A document that exists and will not parse is reported by
    `_configuration_problem` rather than here, so "no configuration" and "a
    configuration nobody can read" stay two answers rather than one blank.
    """
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def current_cleanup_document(context: Context) -> dict[str, Any]:
    """The cleanup document as the core reads it, in the keys it accepts.

    `CleanupPolicy.to_json` emits exactly the keys `parse_policy` takes, which is
    what makes this a document the editor can round-trip rather than a rendering
    of one. A project with no policy gets the core's own `OFF_POLICY` document, so
    the editor opens on what is actually in force rather than on a blank.
    """
    from ..cleanup import hooks as cleanup_hooks

    return cleanup_hooks.load_policy(context.project).to_json()


def current_policy_document(context: Context) -> dict[str, Any]:
    """The stored requirements and connection scope, or the defaults when absent.

    The defaults are what the core does with no document: every obligation the
    manifest itself declares, no harness connection, and cleanup off. They are
    spelled here rather than inferred from an absent key so that a first preview
    shows a real "current" column instead of blanks, and so the first preview's
    removals list is empty rather than reporting every manifest obligation as
    removed by a document nobody wrote.
    """
    stored = _read_document(_policy_path(context))
    if stored is not None:
        return stored
    return {
        "schema_version": PROJECT_POLICY_VERSION,
        "description": "",
        "required_checks": [
            {"id": check.id, "obligations": [obligation_to_json(o) for o in check.obligations()]}
            for check in sorted(
                (context.manifest.checks.values() if context.manifest else []),
                key=lambda c: c.id,
            )
        ],
        "connection": {"scope": "none"},
    }


def _configuration_problem(context: Context) -> str | None:
    """Why the stored configuration is not usable, or None when it is.

    The same split `cleanup.policy_problem` makes: absence is not a problem, and
    an operator's typo is not a project that never configured anything. The page
    shows this rather than silently offering an editor over a document the core
    would refuse.
    """
    path = _policy_path(context)
    if not path.exists():
        return None
    try:
        _parse_document(context, json.loads(path.read_text(encoding="utf-8")))
    except ConfigurationRefused as exc:
        return f"{_RELATIVE[0]} is not a usable project policy: {exc}"
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return f"{_RELATIVE[0]} could not be read: {exc}"
    return None


#: Where the candidate, approval and activation record lives. Under the shared Git
#: directory beside the store, for the reason `enroll.record_path` puts the
#: acceptance record there: it is evidence about the configuration rather than
#: part of it, and two clones must agree about what was approved. The documents
#: themselves stay in the working tree, where `git status` shows them.
_STATE_NAME = "project-config.json"


def _state_path(context: Context) -> Path:
    return context.project.state_root / _STATE_NAME


def _revision_of(document: dict[str, Any], cleanup: dict[str, Any]) -> str:
    """The identity of a candidate revision, over both documents.

    One revision over both files rather than one per file, because the two are one
    decision: a revision naming only the requirements would not distinguish two
    proposals that require the same things under different cleanup authority.
    Deriving it from the content is also what makes a repeated save converge to
    one revision rather than stacking two.
    """
    return _digest_of({"project": document, "cleanup": cleanup})[:32]


def _read_state_record(context: Context) -> dict[str, Any]:
    """The candidate, approval, activation and action log, as stored.

    An absent or unreadable record is an empty one. It is evidence beside the runs
    rather than policy, and a missing record must not stop the editor from reading
    the documents that are on disk.
    """
    empty: dict[str, Any] = {"candidate": None, "approved": None, "active": None, "actions": []}
    document = _read_document(_state_path(context))
    if not isinstance(document, dict):
        return empty
    actions = document.get("actions")
    return {
        "candidate": document.get("candidate"),
        "approved": document.get("approved"),
        "active": document.get("active"),
        "actions": actions if isinstance(actions, list) else [],
    }


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def configuration_state(context: Context) -> dict[str, Any]:
    """What is in force, what was saved, what was approved, what was activated.

    The three states are reported separately because they mean different things to
    an operator, and a page showing only the last thing written would let a saved
    proposal read as an accepted policy. The actions that got here are read from
    the record beside the runs rather than kept in the page, so a later reader
    finds them without asking this session.
    """
    current = current_policy_document(context)
    current["cleanup"] = current_cleanup_document(context)
    current.pop("checks", None)
    record = _read_state_record(context)
    return {
        "digest": _digest_of(current),
        "current": current,
        "candidate": record["candidate"],
        "approved": record["approved"],
        "active": record["active"],
        "actions": record["actions"],
        "recoverable": _recoverable(context),
    }


# ---------------------------------------------------------- atomic publication


def _publish_atomically(
    documents: list[tuple[Path, bytes]], preserved: list[Path], context: Context,
) -> None:
    """Publish several documents as one decision, or publish none of them.

    Each document is written beside its destination and renamed over it, so a
    reader never sees a half-written file. That is necessary and not sufficient:
    two renames are two operations, and the state between them is a project
    holding a new cleanup authority beside old requirements. So every replacement
    is journalled first, then applied, and any failure restores the previous
    bytes before the error leaves this function. The recovery copies are written
    before the replacements rather than after, because a copy taken after a
    failure is a copy of whatever the failure left.
    """
    backups: list[tuple[Path, Path]] = []
    try:
        for destination in preserved:
            if not destination.is_file():
                continue
            backup = _recovery_path(destination, context)
            _write_file(backup, destination.read_bytes())
            backups.append((destination, backup))
        for destination, payload in documents:
            temporary = destination.with_name(destination.name + ".vkit-tmp")
            _write_file(temporary, payload)
            temporary.replace(destination)
    except OSError as exc:
        for destination, backup in reversed(backups):
            if backup.is_file():
                # Copied rather than moved: the recovery copy is what an operator
                # reaches for after a failure, and restoring the document by
                # consuming it would leave them with nothing to inspect.
                _write_file(destination, backup.read_bytes())
        raise ConsoleError(
            f"no configuration was published: {exc}. Both documents are one "
            "decision, so a failure on either leaves the previous configuration "
            "in place and usable; the previous bytes are preserved under "
            f"{context.project.state_root / 'project-config-recovery'}"
        ) from exc


def _write_file(path: Path, payload: bytes) -> None:
    """One file, written whole, with its parent created first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _recovery_path(destination: Path, context: Context) -> Path:
    """Where the bytes being replaced are preserved.

    Under the shared state directory beside the store, not beside the document. A
    backup written into the working tree would appear in `git status` as an
    untracked policy file, and a reviewer would have to work out what it was.

    The name carries the repository-relative destination with its separators
    replaced, so a reader of the recovery list can tell which document each copy
    belongs to and two files with the same basename stay distinguishable. Flat,
    because the recovery list is read as one directory of copies rather than as a
    tree that has to be walked to answer "what can I restore".
    """
    relative = destination.relative_to(context.project.root).as_posix()
    return context.project.state_root / "project-config-recovery" / (
        f"{relative.replace('/', '__')}.previous"
    )


def _recoverable(context: Context) -> list[dict[str, Any]]:
    """The previous bytes of each configuration document that were preserved.

    Read from the directory the publish writes them to, so the list is what is
    actually on disk rather than a record of what was once offered.
    """
    directory = context.project.state_root / "project-config-recovery"
    if not directory.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.previous")):
        payload = path.read_bytes()
        found.append({
            "path": path.name,
            "location": str(path),
            "previous_digest": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        })
    return found


# ------------------------------------------------------------ the preview


def _leaf(document: dict[str, Any], section: str, field: str) -> Any:
    """One field's value, whether it is nested under a section or is the section.

    `description` is a bare string at the top of the document while `connection`
    and `cleanup` are objects, so a diff that assumed every field had a parent
    would raise on the first one it reached. Reading through here rather than
    inline is what lets `_FIELDS_IN` be a flat list of `(section, field)` pairs
    with no special case in the loop that walks it.
    """
    value = document.get(section)
    if field == section and not isinstance(value, dict):
        return value
    return value.get(field) if isinstance(value, dict) else None


def _changed_fields(
    current: dict[str, Any], proposed: dict[str, Any],
) -> list[dict[str, Any]]:
    """Every field whose value differs, named by document, field and before/after.

    A flat list of leaves rather than a nested diff, because an operator asking
    "what will change" reads a table. Each leaf names the document it lands in,
    which is what makes "one decision, two files" legible on the page.
    """
    rows: list[dict[str, Any]] = []
    for (section, field), document in _FIELDS_IN.items():
        before = _leaf(current, section, field)
        after = _leaf(proposed, section, field)
        if before != after:
            rows.append({
                "document": document,
                "field": field if field == section else f"{section}.{field}",
                "current": before,
                "changed": after,
            })
    for check_id in sorted(proposed.get("checks") or {}):
        before_entry = (current.get("checks") or {}).get(check_id, {})
        after_entry = proposed["checks"][check_id]
        for field in ("timeout_seconds", "argv"):
            before = before_entry.get(field)
            after = after_entry.get(field)
            if before != after:
                rows.append({
                    "document": _RELATIVE[0],
                    "field": f"checks.{check_id}.{field}",
                    "current": before,
                    "changed": after,
                })
    return rows


def _removed_obligations(
    current: dict[str, Any], proposal: _Proposal,
) -> list[dict[str, Any]]:
    """Every obligation the proposal drops, named with the check that carried it.

    The comparison is between parsed obligations from the current document and
    parsed obligations from the proposal, not between strings, so a rename of one
    obligation is visible as a removal and an addition rather than passing as
    "unchanged". This is the list the whole checkpoint exists to compute, and it
    is what makes a weakening legible before it is applied.
    """
    wanted: dict[str, set[str]] = {
        check_id: {name for _value, name, _document in obligations}
        for check_id, obligations in proposal.obligations.items()
    }
    held = current.get("required_checks")
    if not isinstance(held, list):
        return []
    removed: list[dict[str, Any]] = []
    for entry in held:
        if not isinstance(entry, dict):
            continue
        check_id = entry.get("id")
        if not isinstance(check_id, str):
            continue
        for obligation in entry.get("obligations") or []:
            name = describe_obligation(_obligation_of(obligation))
            if name not in wanted.get(check_id, set()):
                removed.append({
                    "check_id": check_id,
                    "obligation": name,
                    "detail": (
                        f"the configuration will no longer require {name} of "
                        f"{check_id!r}. Tasks already admitted keep it, and a run "
                        f"the policy requires has to discharge it"
                    ),
                })
    return removed


def _obligation_of(document: Any) -> Obligation:
    """One stored obligation as the core's own value.

    Parsed through `obligation_from_json` so a name rendered for a reader and a
    value compared for equality come from the same union, and a stored document
    naming a kind this build does not have fails here rather than comparing as an
    unmatched string forever.
    """
    try:
        return obligation_from_json(document)
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationRefused(
            f"the stored configuration holds an obligation this build cannot read: {exc}"
        ) from exc


def _weakens_cleanup(current: dict[str, Any], proposed: dict[str, Any]) -> bool:
    """Whether the proposal removes cleanup write authority.

    One boolean because it is one question, and it is asked over the core's own
    decision rather than over the mode name: `may_write` is what the cleanup
    package checks before it touches a file, so a policy that stops satisfying it
    is a weakening however its mode reads.
    """
    def authority(document: dict[str, Any]) -> bool:
        from ..cleanup.policy import parse_policy as parse_cleanup_policy

        try:
            return parse_cleanup_policy(document.get("cleanup") or {}).may_write()
        except Exception:  # noqa: BLE001 - an unparseable policy cannot be weaker
            return False

    return authority(current) and not authority(proposed)


def _stale_evidence(context: Context, affected: tuple[str, ...]) -> list[dict[str, Any]]:
    """The recorded evidence a change to these checks invalidates.

    A check's configuration digest is stamped into every run report, so a changed
    requirement or timeout means the recorded evidence was produced under a
    different configuration and describes something that no longer holds. This
    names those runs. It cannot release them, and it does not delete them:
    `compute_readiness` decides what a task may still claim, and the pinned floor
    is what it reads.
    """
    stale: list[dict[str, Any]] = []
    for check_id in affected:
        run = _latest_runs_by_check(context).get(check_id)
        if run is None:
            continue
        stale.append({
            "check_id": check_id,
            "run_id": run.get("run_id"),
            "verdict": run.get("result"),
            "recorded_at": run.get("created_at"),
            "detail": (
                "this run was produced under the configuration in force then. It "
                "stays readable and stays in the record, and it is not evidence for "
                "the configuration proposed here; run the check again for that"
            ),
        })
    return stale


# --------------------------------------------------------------- the stages


def _affects(current: dict[str, Any], proposal: _Proposal) -> tuple[str, ...]:
    """The checks whose configuration or obligation set moves, in a stable order.

    A check whose requirement is unchanged is not affected, so a preview does not
    tell an operator that evidence for a check it did not touch has gone stale.
    That matters when a document changes one check out of several: naming all of
    them would make the warning mean nothing.
    """
    wanted = {check_id: {n for _v, n, _d in entries}
              for check_id, entries in proposal.obligations.items()}
    held: dict[str, set[str]] = {}
    for entry in current.get("required_checks") or []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            held[entry["id"]] = {
                describe_obligation(_obligation_of(o)) for o in entry.get("obligations") or []
            }
    changed = {
        check_id for check_id in set(wanted) | set(held)
        if wanted.get(check_id, set()) != held.get(check_id, set())
    }
    changed |= set(proposal.checks) | set(current.get("checks") or {})
    return tuple(sorted(changed))


def _preview_document(
    context: Context, current: dict[str, Any], proposal: _Proposal,
) -> dict[str, Any]:
    """Everything an operator sees before anything is written.

    The five facts the plan names are separate fields rather than one summary
    line, because they answer different questions and an operator who sees only a
    summary cannot tell a routine timeout change from a dropped obligation.
    `routine` is the field a page has to check, and it is False for anything that
    removes an obligation or cleanup write authority.
    """
    removed = _removed_obligations(current, proposal)
    weakens = _weakens_cleanup(current, proposal.document)
    affected = _affects(current, proposal)
    return {
        "current": current,
        "changed": _changed_fields(current, proposal.document),
        "affected_checks": list(affected),
        "stale_evidence": _stale_evidence(context, affected),
        "removes_obligations": removed,
        "weakens_cleanup": weakens,
        "routine": not removed and not weakens,
        "candidate": {
            "revision": _revision_of(proposal.document, proposal.cleanup),
            "document": proposal.document,
            "cleanup_digest": _digest_of(proposal.cleanup),
        },
        "note": (
            "Nothing has been written. This is what saving this document would do, "
            "computed against the configuration in force now"
        ),
    }


def _require_expected(state: dict[str, Any], expected_digest: str | None, stage: str) -> None:
    """The operator must be looking at the configuration they are editing.

    Two browser tabs on one console is the ordinary case, not an attack, and
    without this the second save silently overwrites the first. The digest is over
    the content, so it moves for a real change and not for a reformat, and the
    refusal names both so an operator can tell which configuration it is.
    """
    if expected_digest is None:
        raise ConfigurationRefused(
            f"{stage} needs expected_digest, the digest of the configuration you "
            "are editing. Without it this console cannot tell whether the file "
            "changed while you had this page open"
        )
    if expected_digest != state["digest"]:
        raise ConfigurationRefused(
            f"the configuration changed since you read it: you were editing "
            f"{expected_digest[:12]} and it is now {state['digest'][:12]}. Read it "
            "again and reapply your change; nothing was written"
        )


def save_project_config(
    context: Context,
    stage: str = "preview",
    *,
    document: dict[str, Any] | None = None,
    expected_digest: str | None = None,
    revision: str | None = None,
) -> dict[str, Any]:
    """Preview, save, approve or activate this project's own configuration.

    Four stages, four questions, and the stage is what keeps them apart. `preview`
    computes the whole effect and writes nothing at all, so an operator sees
    current values, changed values, affected checks, the evidence that becomes
    stale, and any obligation the proposal removes BEFORE a decision exists.
    `save` publishes the candidate documents. `approve` records that a person
    accepted the candidate. `activate` adopts an approved candidate as the local
    policy a NEW task attempt is admitted under.

    **Nothing here releases an existing task.** A task pinned its contract and its
    policy digest at admission; this reads those rows and never writes them.
    Changing configuration cannot make old evidence current, so the only thing an
    operator can do afterwards is run a fresh attempt, which `run_check` already
    offers.

    **Nothing here approves a protected integration policy.** A save produces a
    candidate revision in this repository's local context. Protected authority
    comes from an independently approved reference and is granted by a reviewer,
    never by a browser edit.
    """
    if stage not in CONFIG_STAGES:
        raise Refused(
            f"unknown stage {stage!r}; this operation performs "
            f"{', '.join(CONFIG_STAGES)}"
        )

    state = configuration_state(context)
    problem = _configuration_problem(context)
    if problem is not None and stage in ("save", "approve", "activate"):
        raise ConfigurationRefused(
            f"{problem}. Fix or remove that file before changing it from here, "
            "because this operation validates the whole document before it writes"
        )

    if stage == "preview":
        if document is None:
            raise ConfigurationRefused(
                "preview needs the proposed document, which is what it describes. "
                "With no document there is nothing to preview"
            )
        proposal = _parse_document(context, document)
        _require_expected(state, expected_digest, "preview")
        return {
            "stage": stage,
            "wrote": False,
            "expected_digest": expected_digest,
            **_preview_document(context, state["current"], proposal),
        }

    if stage == "save":
        if document is None:
            raise ConfigurationRefused("save needs the proposed document to publish")
        proposal = _parse_document(context, document)
        _require_expected(state, expected_digest, "save")
        preview = _preview_document(context, state["current"], proposal)
        return _publish_candidate(context, state, proposal, preview)

    if stage == "approve":
        return _approve_candidate(context, state, revision)

    return _activate_candidate(context, state, revision)


def _publish_candidate(
    context: Context, state: dict[str, Any], proposal: _Proposal, preview: dict[str, Any],
) -> dict[str, Any]:
    """Write both documents as one decision, and record what was done.

    The revision is derived from the content, so saving the same document twice
    produces the same revision and the recorded action is the same entry rather
    than a second one. That is what makes a retry after a dropped connection
    converge instead of stacking.
    """
    revision = preview["candidate"]["revision"]
    policy = _policy_path(context)
    cleanup = _cleanup_path(context)

    _publish_atomically(
        [(policy, _canonical_bytes(proposal.document)),
         (cleanup, _canonical_bytes(proposal.cleanup))],
        [policy, cleanup],
        context,
    )

    entry = {
        "stage": "save",
        "revision": revision,
        "at": _now(),
        "changed": len(preview["changed"]),
        "removes_obligations": len(preview["removes_obligations"]),
        "weakens_cleanup": preview["weakens_cleanup"],
    }
    record = _read_state_record(context)
    # A repeated save of an identical document is the same decision, so the log
    # keeps one entry for it. The gate is the revision, because that is what
    # identifies the decision.
    actions = [
        item for item in record["actions"]
        if not (item.get("stage") == "save" and item.get("revision") == revision)
    ]
    record["actions"] = [entry, *actions]
    record["candidate"] = {
        "revision": revision,
        "state": "candidate",
        "saved_at": entry["at"],
        "document": proposal.document,
        "cleanup_digest": preview["candidate"]["cleanup_digest"],
    }
    _write_file(
        _state_path(context),
        _canonical_bytes({**record, "schema_version": PROJECT_POLICY_VERSION}),
    )

    after = configuration_state(context)
    return {
        "stage": "save",
        "wrote": True,
        "candidate": record["candidate"],
        "digest": after["digest"],
        "recovery": _recovery_receipt(context),
        "changed": preview["changed"],
        "removes_obligations": preview["removes_obligations"],
        "weakens_cleanup": preview["weakens_cleanup"],
        "routine": preview["routine"],
        "note": (
            "This is a candidate revision. It is not the accepted local policy "
            "until approve, and it does not affect any task already admitted: a "
            "task pinned its contract at admission and keeps it"
        ),
    }


def _recovery_receipt(context: Context) -> list[dict[str, Any]]:
    """The preserved previous documents, named for the record."""
    return [
        {"path": entry["path"], "location": entry["location"],
         "previous_digest": entry["previous_digest"]}
        for entry in _recoverable(context)
    ]


def _approve_candidate(
    context: Context, state: dict[str, Any], revision: str | None,
) -> dict[str, Any]:
    """Record that a person accepted a named candidate. Publishes nothing.

    The revision must be named, because "approve whatever is current" is a phrase
    that means a different thing a second later. Approval is about the candidate
    revision, not about the files: the documents on disk are the candidate's, and
    approval says a person has read what they contain.
    """
    if revision is None:
        raise ConfigurationRefused(
            "approve needs the revision it is approving, from the candidate the "
            "preview named. Approving whatever happens to be current is not a "
            "decision, because it means a different thing a second later"
        )
    candidate = state["candidate"]
    if candidate is None:
        raise ConfigurationRefused(
            "there is no saved candidate to approve. Save one first"
        )
    if candidate["revision"] != revision:
        raise ConfigurationRefused(
            f"the saved candidate is {candidate['revision']} and you approved "
            f"{revision}. Read it again; nothing was recorded"
        )
    approved = {
        "revision": revision,
        "state": "approved",
        "approved_at": _now(),
        "document_digest": state["digest"],
        "context": "local",
    }
    record = _read_state_record(context)
    record["approved"] = approved
    record["actions"] = [
        {"stage": "approve", "revision": revision, "at": approved["approved_at"]},
        *record["actions"],
    ]
    _write_file(
        _state_path(context),
        _canonical_bytes({**record, "schema_version": PROJECT_POLICY_VERSION}),
    )
    return {
        "stage": "approve",
        "wrote": False,
        "approved": approved,
        "note": (
            "Approved as this project's LOCAL policy. This confers no protected "
            "integration authority: protected CI uses its own independently "
            "approved policy reference, and nothing written here changes it"
        ),
    }


def _activate_candidate(
    context: Context, state: dict[str, Any], revision: str | None,
) -> dict[str, Any]:
    """Adopt an approved candidate as the local policy new attempts are admitted under.

    Activation records a policy digest rather than writing a document, so it
    cannot leave the two files disagreeing with each other. A new attempt started
    after this is admitted against the accepted local policy; a task admitted
    before it keeps the contract it pinned.
    """
    if revision is None:
        raise ConfigurationRefused("activate needs the revision it is activating")
    approved = state["approved"]
    if approved is None:
        raise ConfigurationRefused(
            f"revision {revision} has not been approved. Approve it first: saving "
            "a proposal and activating it are different decisions"
        )
    if approved["revision"] != revision:
        raise ConfigurationRefused(
            f"the approved revision is {approved['revision']} and you activated "
            f"{revision}. Nothing was recorded"
        )
    if state["digest"] != approved["document_digest"]:
        raise ConfigurationRefused(
            f"the configuration changed since it was approved: it was approved at "
            f"{approved['document_digest'][:12]} and is now {state['digest'][:12]}. "
            "Approve it again against what is on disk; nothing was activated"
        )
    active = {
        "revision": revision,
        "state": "active",
        "activated_at": _now(),
        # The manifest's own digest, because that is the policy a run is admitted
        # against. The configuration says what the run must satisfy; the manifest
        # says what executes. Naming both keeps the pair legible.
        "policy_digest": context.manifest.digest() if context.manifest else None,
        "configuration_digest": state["digest"],
        "context": "local",
    }
    record = _read_state_record(context)
    record["active"] = active
    record["actions"] = [
        {"stage": "activate", "revision": revision, "at": active["activated_at"]},
        *record["actions"],
    ]
    _write_file(
        _state_path(context),
        _canonical_bytes({**record, "schema_version": PROJECT_POLICY_VERSION}),
    )
    pinned = _pinned_tasks(context)
    return {
        "stage": "activate",
        "wrote": False,
        "active": active,
        "tasks_pinned": len(pinned),
        "next_attempt": (
            "A new attempt started now is admitted against the accepted local "
            "policy. Every task already admitted keeps the contract and policy "
            "digest it pinned, and its evidence is unchanged"
        ),
        "note": (
            "Activating a local candidate does not approve it for protected "
            "integration. Protected CI continues to use its own approved policy "
            "reference, and the two are compared on the settings page"
        ),
    }


def _pinned_tasks(context: Context) -> list[str]:
    """Every task this project holds, named, so activation can say what it did not touch."""
    return _task_ids(context)


#: The configuration operation joins the surface here rather than above, because
#: `OPERATIONS` is a table evaluated at import time and a name in it is a promise
#: that the function exists. `plan.WRITABLE` is the declaration of record and a
#: test asserts the two agree.
OPERATIONS["save_project_config"] = save_project_config


# ------------------------------------------------------------- the settings


def _cleanup_vocabulary() -> dict[str, Any]:
    """The cleanup package's own vocabulary, read from it.

    `RULE_ORDER` rather than the parser's id set because the rule order is what a
    page draws: `RULE_ORDER` is the sequence cleanup actually applies in, and a
    page listing rules in a different order than they run in is a page describing
    something else.
    """
    from ..cleanup import hooks as cleanup_hooks
    from ..cleanup.policy import CLEANUP_MODES, CLEANUP_RULE_IDS

    return {
        "modes": list(CLEANUP_MODES),
        "rule_ids": list(CLEANUP_RULE_IDS),
        "rule_order": list(cleanup_hooks.RULE_ORDER),
    }


def configuration_section(context: Context) -> dict[str, Any]:
    """The configuration an operator is about to edit, and the state around it.

    Both the local configuration and the approved integration policy are reported
    here, including any mismatch, because the checkpoint's rule is that a browser
    edit can never become protected authority and an operator has to be able to
    see that rather than be told it.
    """
    state = configuration_state(context)
    approved = _approved_integration_policy(context)
    return {
        **state,
        "editable": True,
        "problem": _configuration_problem(context),
        "paths": list(_RELATIVE),
        "vocabularies": {
            "connection_scopes": list(CONNECTION_SCOPES),
            "stages": list(CONFIG_STAGES),
            "cleanup": _cleanup_vocabulary(),
        },
        "local": _local_policy_document(context, state),
        "approved_integration": approved,
        "mismatch": _mismatch(approved, state),
        "controls": _typed_controls(context),
    }


def _local_policy_document(context: Context, state: dict[str, Any]) -> dict[str, Any]:
    """The local configuration as a candidate policy document, in context `local`.

    It is named `local` and never `protected`. `integration.from_file` refuses a
    local document that claims protected context, and this says the same thing
    rather than writing the word and letting a later reader believe it.
    """
    return {
        "context": "local",
        "policy_id": f"local-{state['digest'][:16]}",
        "digest": state["digest"],
        "required_checks": [
            {
                "id": entry["id"],
                "obligations": [
                    obligation_to_json(_obligation_of(o)) for o in entry.get("obligations") or []
                ],
            }
            for entry in state["current"].get("required_checks") or []
        ],
        "approved_for_protected": False,
        "authority": (
            "an operator edit in this console. It is local evidence, not the "
            "protected integration policy, and no action here can make it one"
        ),
    }


def _approved_integration_policy(context: Context) -> dict[str, Any]:
    """The protected integration policy this project actually has, or None.

    Read through `integration.from_commit` on the repository's own approved
    reference when there is one, and reported as absent otherwise. Inventing a
    protected policy for a project that has none would show a green bar over an
    authority that was never granted, which is the exact confusion the checkpoint
    exists to prevent.
    """
    from ..integration import policy as policies

    reference = context.project.root / ".vkit-approved-policy-ref"
    if not reference.is_file():
        return {
            "context": None,
            "policy_id": None,
            "detail": (
                "this repository declares no approved policy reference, so no "
                "protected integration policy exists here. A console edit cannot "
                "create one; protected CI reads its own approved reference"
            ),
            "required_checks": [],
        }
    try:
        policy = policies.from_commit(
            context.project, reference.read_text(encoding="utf-8").strip()
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable reference is a fact to show
        return {
            "context": None,
            "policy_id": None,
            "detail": f"the approved policy reference could not be resolved: {exc}",
            "required_checks": [],
        }
    document = policy.to_json()
    return {
        "context": document["context"],
        "policy_id": document["policy_id"],
        "digest": document["digest"],
        "manifest_revision": document["manifest_revision"],
        "required_checks": [entry["id"] for entry in document["required_checks"]],
        "detail": (
            "read from this repository's own approved reference. It is the "
            "authority for a protected run and this console cannot change it"
        ),
    }


def _mismatch(approved: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Where the local configuration and the approved policy disagree.

    Reported rather than resolved. A local configuration that requires less than
    the approved policy requires is the ordinary state for a project that has not
    finished configuring itself, and a page that rendered it as a failure would
    train operators to ignore the comparison.
    """
    local = {
        entry["id"] for entry in state["current"].get("required_checks") or []
    }
    required = set(approved.get("required_checks") or [])
    missing = sorted(required - local)
    extra = sorted(local - required)
    return {
        "approved_requires": sorted(required),
        "local_requires": sorted(local),
        "missing_locally": missing,
        "extra_locally": extra,
        "agrees": not missing and not extra,
        "detail": (
            f"the local configuration requires {sorted(local) or '<nothing>'} and "
            f"the approved policy requires {sorted(required) or '<nothing>'}. A "
            "protected run decides against the approved policy, not against this"
        ),
    }


def _typed_controls(context: Context) -> list[dict[str, Any]]:
    """The controls the editor draws, each naming the values it will accept.

    Declared here rather than in the page so the two cannot disagree about the
    vocabulary: an option the page offers that this list does not name is a
    control the validation would refuse, and a value this list names that the page
    cannot produce is a feature nobody can reach.
    """
    from ..cleanup import hooks as cleanup_hooks
    from ..cleanup.policy import APPLY_MODES as _apply_modes, CLEANUP_MODES as _modes

    controls: list[dict[str, Any]] = [
        {
            "id": "check_timeout",
            "label": "Check timeout (seconds)",
            "type": "number",
            "min_exclusive": 0,
            "max": MAX_TIMEOUT_SECONDS,
            "values": [
                {"check_id": check.id, "current": check.timeout_seconds}
                for check in sorted(
                    (context.manifest.checks.values() if context.manifest else []),
                    key=lambda c: c.id,
                )
            ],
            "note": (
                "The same bound the manifest parser enforces: positive and at most "
                f"{MAX_TIMEOUT_SECONDS:g} seconds"
            ),
        },
        {
            "id": "obligations",
            "label": "Required obligations",
            "type": "checklist",
            "values": [
                {
                    "check_id": check.id,
                    "category": check.evidence_kind().value,
                    "obligations": [
                        describe_obligation(o) for o in check.obligations()
                    ],
                }
                for check in sorted(
                    (context.manifest.checks.values() if context.manifest else []),
                    key=lambda c: c.id,
                )
            ],
            "note": (
                "Every obligation a registered check declares. Unchecking one is "
                "reported as removing an obligation and is never a routine edit"
            ),
        },
        {
            "id": "argv",
            "label": "Check command",
            "type": "argument_list",
            "values": [
                {"check_id": check.id, "current": list(check.argv)}
                for check in sorted(
                    (context.manifest.checks.values() if context.manifest else []),
                    key=lambda c: c.id,
                )
            ],
            "note": (
                "One field per argument, validated as a nonempty list of nonempty "
                "strings. A joined command string is refused: there is no shell here"
            ),
        },
        {
            "id": "cleanup_mode",
            "label": "Cleanup mode",
            "type": "choice",
            "values": [
                {"value": mode, "may_write": mode in _apply_modes}
                for mode in _modes
            ],
            "note": (
                "Only apply_verified authorizes a write, and only for the rules and "
                "paths the policy names. Turning one off is reported as a weakening"
            ),
        },
        {
            "id": "cleanup_rules",
            "label": "Cleanup rules",
            "type": "checklist",
            "values": [
                {"value": rule} for rule in cleanup_hooks.RULE_ORDER
            ],
            "note": (
                "Only rule ids this build can apply are offered. An unknown id is "
                "refused at parse rather than reading as coverage nobody has"
            ),
        },
        {
            "id": "cleanup_exclusions",
            "label": "Cleanup exclusions",
            "type": "path_list",
            "values": [
                {"value": pattern} for pattern in current_cleanup_document(context).get(
                    "excluded_paths", []
                )
            ],
            "note": (
                "Repository-relative glob prefixes. A pattern with no slash matches "
                "at any segment, so 'vendor' does not also match 'vendor_helper.py'"
            ),
        },
        {
            "id": "connection",
            "label": "Harness connection scope",
            "type": "choice",
            "values": [{"value": scope} for scope in CONNECTION_SCOPES],
            "note": (
                "The scope the console answers on. There is no setting here for a "
                "model, a provider or a credential: the chosen harness owns those"
            ),
        },
    ]
    return controls
