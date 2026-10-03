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

import os
import sys
from dataclasses import dataclass
from pathlib import Path
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
from ..manifest import Manifest, ManifestError, parse_manifest
from ..paths import Project, ProjectError, open_project
from ..recover import Report as RecoveryReport
from ..recover import inspect as inspect_recovery
from ..storage import Store, StoreError, probe_state, read_receipt
from ..supervisor import SupervisorError, cancel_run, start_run
from ..verifiers import describe_obligation
from .plan import (
    DEFAULT_RUN_LIMIT,
    LOOPBACK_HOST,
    MAX_LOG_BYTES,
    MAX_RUN_LIMIT,
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
    """Cleanup. Mode, rules, protected paths, and what is still owed.

    **The panels this build cannot fill are named, rather than left blank.** The
    cleanup package writes a preservation receipt into the `Applied` it returns to
    its caller and persists only the original bytes, so there is nothing on disk to
    list; the same is true of applied patches and of proposals, which are frozen
    values a preview returns and nothing stores. Inventing a plausible empty list
    would be indistinguishable from a project that has never cleaned anything,
    which is a different and false claim.

    What is real, and comes from the core: the policy as `cleanup.load_policy`
    reads it, whether that policy is usable, the rules it enables, the paths it
    excludes, and the outstanding work `cleanup.freshness` derives from the
    working tree. `freshness` is read-only by construction: it forces the policy
    to preview mode on a copy, so calling it here cannot apply a change.

    The policy file itself lives under `verification/`, which is a protected
    path. Editing it is checkpoint 12.3 and is not on the writable surface here,
    so no action is offered on this section.

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
            "unavailable": _CLEANUP_WITHOUT_BACKEND,
        }

    outstanding: list[dict[str, Any]] = []
    freshness_error: str | None = None
    try:
        for owed in cleanup_hooks.freshness(context.project, policy=policy):
            path, _, rule = owed.partition(":")
            outstanding.append({"path": path, "rule": rule})
    except Exception as exc:  # noqa: BLE001 - report rather than fail the read
        freshness_error = str(exc)

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
        "error": freshness_error,
        "unavailable": _CLEANUP_WITHOUT_BACKEND,
        "note": (
            "Editing the cleanup policy is checkpoint 12.3 and is not on the "
            "writable surface in this build. Nothing here writes."
        ),
    }


#: The cleanup panels this build cannot fill, named so the page says which they
#: are rather than rendering a silent gap. Each names the backend that is absent,
#: because "not shown" without a reason is indistinguishable from "none exist".
_CLEANUP_WITHOUT_BACKEND: tuple[dict[str, str], ...] = (
    {
        "panel": "applied patches",
        "missing": (
            "cleanup.apply returns an Applied record to its caller and persists "
            "only the original bytes; nothing stores the patch list"
        ),
    },
    {
        "panel": "preservation receipts",
        "missing": (
            "the receipt travels on the Applied value and is never written to "
            "storage, so there is no receipt to read back"
        ),
    },
    {
        "panel": "proposals",
        "missing": (
            "a proposal is a frozen value a preview returns; no collection of "
            "them is stored"
        ),
    },
    {
        "panel": "past refusal reasons",
        "missing": (
            "refusals are returned to the caller that asked; recovery is by "
            "digest comparison against the file, not by a log"
        ),
    },
)


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
OPERATIONS = {
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

    **Read-only by checkpoint, and this is the honest reason.** Configuration
    editing is checkpoint 12.3, which replaces the blanket prohibition on writes
    under `verification/` with a validated project-policy operation. Until that
    exists, no action here changes a setting, so the section shows what is
    present and offers only the actions already on the writable surface.

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
        if op.name not in ("install", "repair", "remove", "enroll"):
            continue
        blocked = _host_cli_block(op.name) if host_cli is None else None
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
        "editable": False,
        "package": package,
        "marketplace_registered": _marketplace_registered(),
        "note": (
            "Configuration editing is checkpoint 12.3 and is not on the writable "
            "surface in this build. Everything here is read-only."
        ),
    }


def _host_cli_block(name: str) -> str | None:
    """Why this operation cannot run without the host CLI, or None if it can."""
    return (
        f"the 'claude' CLI is not on PATH, and {name} runs it; installing Claude "
        f"Code is the prerequisite. This is the core's own refusal, not this "
        f"page's."
    )


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
