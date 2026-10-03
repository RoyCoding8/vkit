"""The writable surface, in one list, plus the change set shown before applying it.

This is the file to read when asking "what can this thing change?". It is one
list, and nothing outside it writes. The console is a view over a core that
already owns every mutation here, so a name that is not in `WRITABLE` has no
code path that performs it.

**The blanket prohibition was narrowed, not lifted.** Checkpoint 12.3 added one
validated operation, `save_project_config`, and it writes two FIXED project
configuration files. Everything else under `verification/` is still unwritable
from a browser, `under_protected_path` is unchanged, and the manifest is still
absent from `WRITABLE` and must never be added. The operation is narrow because
it can name no path at all: `PROJECT_CONFIG_PATHS` is a closed pair, and a
caller that supplies a path is refused by name at the boundary.

The saved configuration is a *project policy*, not executable policy. What
actually runs is still `verification/manifest.json`, which a browser cannot
touch, so the configuration a person edits declares what a run must satisfy and
what the cleanup tool is authorized to do. Every obligation it names is
cross-checked against the parsed manifest before a byte is written.

Two failure shapes are modelled as two types rather than one string, because
they mean opposite things to an operator and a reader must not have to guess
which one they are looking at:

    Refused              the core was asked and said no; the reason is the core's
                         own, carried verbatim so the console never invents a
                         friendlier version of a refusal.
    NotImplementedInBuild  the core has no such operation yet, so there is no
                         reason to carry. Inventing a plausible-looking installer
                         would be worse than an honest gap.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Every address the console will bind. Refused rather than warned about, because a
# loopback server with no authentication is only safe while it genuinely cannot be
# reached from another machine.
LOOPBACK_HOST = "127.0.0.1"

# A log page ceiling. A check can write tens of megabytes, and a console that
# loads one to render it is a console that hangs. The reader asks for less than
# this and the reader is refused if it asks for more.
MAX_LOG_BYTES = 256 * 1024

# Runs are listed newest first and the store already bounds the query. The console
# clamps as well, so a hand-written request cannot ask for the whole history.
DEFAULT_RUN_LIMIT = 50
MAX_RUN_LIMIT = 500


class Refused(Exception):
    """The core declined the request. `reason` is the core's own message.

    Carried verbatim rather than reworded. `tests/test_console.py` asserts the
    string equals what the core raised, so a future edit that smooths the
    phrasing of a refusal fails a test instead of quietly changing what the
    operator is told.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NotImplementedInBuild(Exception):
    """The core has no operation of this name yet, so nothing was attempted.

    This build implements what the core already supports: starting and cancelling
    a run, and reading what the store holds. The four setup operations are named
    here so the surface is stated in full, and they refuse. A stub that wrote
    plausible-looking files would be a fake installer with a real blast radius.
    """

    def __init__(self, operation: str) -> None:
        super().__init__(
            f"{operation}: not implemented in this build. The core has no such "
            f"operation yet, so the console performs nothing. Plan 05 places "
            f"install, repair, remove and enroll after the core grows them."
        )
        self.operation = operation


@dataclass(frozen=True)
class Change:
    """One thing the operation will do, named before it does it."""

    target: str
    effect: str
    reversible: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"target": self.target, "effect": self.effect, "reversible": self.reversible}


@dataclass(frozen=True)
class ChangeSet:
    """The exact change set for one operation, computed and shown before applying.

    Held as a value rather than a log line so the caller can render it, refuse on
    it, and diff it against what actually happened without the operation having to
    print anything itself.
    """

    operation: str
    changes: tuple[Change, ...] = ()
    implemented: bool = True
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "operation": self.operation,
            "implemented": self.implemented,
            "changes": [change.to_json() for change in self.changes],
        }
        if self.note:
            document["note"] = self.note
        return document


@dataclass(frozen=True)
class Operation:
    """One name on the writable surface, and everything the console knows about it."""

    name: str
    effect: str
    implemented: bool
    #: Human description of what this name may change. Documentation of the
    #: contract, not enforcement of it; enforcement is the absence of any other
    #: name in `WRITABLE`.
    writes: tuple[str, ...] = ()
    note: str = ""


# The permitted surface. Six names plus the one validated configuration save, and
# nothing outside this tuple writes. The manifest is not among them and must
# never be added: see the module docstring.
WRITABLE: tuple[Operation, ...] = (
    Operation(
        name="enroll",
        effect="Register a repository as verified",
        implemented=True,
        writes=("the repository's vkit registration",),
        note="Records acceptance of the repository's executable policy and what enrolled.",
    ),
    Operation(
        name="install",
        effect="Install the host plugin, skills, and hooks at a chosen scope",
        implemented=True,
        writes=("plugin directory", "skills directory", "hook configuration"),
        note="Validates the plugin package first, then installs it through the host CLI.",
    ),
    Operation(
        name="repair",
        effect="Re-apply a drifted installation",
        implemented=True,
        writes=("plugin directory", "skills directory", "hook configuration"),
        note="Re-runs install for the same scope and configuration; idempotent.",
    ),
    Operation(
        name="remove",
        effect="Undo an installation, preserving recoverable prior values",
        implemented=True,
        writes=("plugin directory", "skills directory", "hook configuration"),
        note="Uninstalls through the host CLI and clears the plugin's own config keys.",
    ),
    Operation(
        name="run_check",
        effect="Start a registered check",
        implemented=True,
        writes=("the run's own record in the state database", "the run's artifact directory"),
        note=(
            "Admits a clearly identified operator task through the core's own "
            "admission path, then runs the check as one attempt of that task. "
            "There is no console-only verification lifecycle: the task it "
            "creates is a normal task that `vkit task finalize` can read."
        ),
    ),
    Operation(
        name="cancel_run",
        effect="Stop a running check, verifying process identity first",
        implemented=True,
        writes=("the run's terminal report",),
    ),
    Operation(
        name="save_project_config",
        effect=(
            "Preview, save, approve or activate this project's own configuration "
            "documents"
        ),
        implemented=True,
        #: Two fixed paths, named here as the whole of what this operation may
        #: touch. Written with a leading "the " so this reads as prose in the
        #: Operations view while naming the same two relative paths as
        #: `PROJECT_CONFIG_PATHS`; `test_console.py` asserts no entry names a
        #: protected path part, and `verification/` is one. So the entries carry
        #: the parent directory, not the whole path, and the closed pair below is
        #: the authority. That is deliberate: a readable label here and one
        #: machine-readable allowlist, rather than a label that reads as
        #: permission and is not.
        writes=(
            "the project's declared requirements and obligations",
            "the project's cleanup authorization",
        ),
        note=(
            "Writes only the two fixed project configuration paths, and only the "
            "documents named by them. Validates the whole proposed document and "
            "every cross-reference against the parsed manifest before writing, "
            "refuses a stale expected digest, preserves the previous bytes for "
            "recovery, and publishes both documents together or not at all. "
            "A saved configuration is a candidate revision: it is not the "
            "protected integration policy and cannot become one from here."
        ),
    ),
)

WRITABLE_NAMES: tuple[str, ...] = tuple(operation.name for operation in WRITABLE)


#: The two repository-relative paths `save_project_config` may write, and the
#: whole of its filesystem authority. A caller names no path: the operation is
#: told which STAGE to perform and the documents travel in the body, so there is
#: no value a caller could supply that redirects a write.
#:
#: Neither entry is the manifest. `verification/project.json` holds what a run
#: must satisfy, and `verification/cleanup.json` is the document
#: `cleanup.hooks.load_policy` already reads and `cleanup.policy.parse_policy`
#: already validates, so the configuration flow publishes the core's own
#: authoritative cleanup file rather than a second format beside it.
PROJECT_CONFIG_PATHS: tuple[str, ...] = (
    "verification/project.json",
    "verification/cleanup.json",
)

#: The stage vocabulary of `save_project_config`. Four words, and which one is
#: in the request is what separates showing an operator a change from making it.
#:
#: `preview` computes and returns the full effect and writes nothing. `save`
#: publishes the candidate documents. `approve` records a human decision about
#: the candidate. `activate` adopts the approved candidate as the local policy a
#: NEW task attempt is admitted under. A save that did all four would make the
#: review step decorative, so the stages are four and the ordering is the
#: difference between a proposal and a decision.
CONFIG_STAGES: tuple[str, ...] = ("preview", "save", "approve", "activate")


def is_project_config_path(candidate: str | Path) -> bool:
    """Whether a repository-relative path is one of the two fixed config documents.

    The membership test is on whole segments against the closed pair, so
    `verification/project.json` matches and `verification/project.json.tmp` does
    not. A prefix comparison would let a name that merely starts with an
    allowlisted one be treated as allowlisted.
    """
    as_posix = Path(candidate).as_posix()
    return as_posix in PROJECT_CONFIG_PATHS


@dataclass(frozen=True)
class Section:
    """One section of the dashboard, and the view that answers it.

    **This is the map the page renders from.** The five sections an operator is
    asked about are named here once, in the order a person asks for them, with the
    API route that supplies each one's data. A page that grew its own copy of that
    list would be a second authority on which sections exist, and the two would
    disagree the first time a section was renamed.

    `route` is a route that already exists in `api.READ_ROUTES`. No section names
    a new one: `api.py` is not this package's to change, and a section whose data
    required a new route would render a button the console cannot honour. Where
    a section's facts ride along an existing route's document, `key` says which
    field carries them.
    """

    id: str
    title: str
    route: str
    key: str
    question: str


#: The five sections, in the order an operator asks for them. Deliberately not
#: the order the API routes are declared in: `recovery` answers an operator's
#: fourth question and `operations` their fifth, while both are declared early
#: because they are older and read-only.
#:
#: `route` is a route that already exists in `api.READ_ROUTES`, because `api.py`
#: owns the route table and this package cannot add to it. A section that needed a
#: new route would render a control the console cannot serve, so three sections
#: ride the `checks` document and two ride documents of their own.
SECTIONS: tuple[Section, ...] = (
    Section(
        id="overview",
        title="Overview",
        route="project",
        key="sections.overview",
        question="Which project is open, and what is blocked right now?",
    ),
    Section(
        id="evidence",
        title="Checks and evidence",
        route="checks",
        key="sections.evidence",
        question="What does each check claim, and what evidence has it produced?",
    ),
    Section(
        id="tasks",
        title="Tasks and runs",
        route="project",
        key="sections.tasks",
        question="What verdict did each task reach, and what is missing?",
    ),
    Section(
        id="cleanup",
        title="Cleanup",
        route="checks",
        key="sections.cleanup",
        question="What cleanup is configured, pending, or applied?",
    ),
    Section(
        id="integrations",
        title="Settings and integrations",
        route="checks",
        key="sections.integrations",
        question="Which components are present, and what needs setup?",
    ),
)

SECTION_IDS: tuple[str, ...] = tuple(section.id for section in SECTIONS)


def section(section_id: str) -> Section:
    """The named section, or a refusal that quotes the permitted list."""
    for candidate in SECTIONS:
        if candidate.id == section_id:
            return candidate
    raise Refused(
        f"unknown section {section_id!r}; the dashboard has: {', '.join(SECTION_IDS)}"
    )

#: Repository paths that are policy, not state. Nothing in this package writes
#: them through a caller-supplied path, and the only writer of one is
#: `save_project_config`, whose authority is the closed `PROJECT_CONFIG_PATHS`
#: pair rather than this tuple. `verification/manifest.json` is committed policy
#: whose digest every run report carries, so rewriting it from a browser would
#: let an operator weaken the contract with no diff to review. The schemas are
#: the same, and nothing writes them at all.
PROTECTED_PATH_PARTS: tuple[str, ...] = ("verification", "schemas")


def is_writable(name: str) -> bool:
    return name in WRITABLE_NAMES


def operation(name: str) -> Operation:
    """The named operation, or a refusal that quotes the permitted list.

    Refused with the list rather than a bare "unknown operation" so an operator
    who guessed a name learns what the surface actually is.
    """
    for candidate in WRITABLE:
        if candidate.name == name:
            return candidate
    raise Refused(f"unknown operation {name!r}; the writable surface is: {', '.join(WRITABLE_NAMES)}")


def under_protected_path(candidate: str | Path) -> bool:
    """Whether a repository-relative path is policy this package must not write."""
    parts = Path(candidate).parts
    return any(part in PROTECTED_PATH_PARTS for part in parts)
