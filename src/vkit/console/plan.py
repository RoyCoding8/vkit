"""The writable surface, in one list, plus the change set shown before applying it.

This is the file to read when asking "what can this thing change?". It is one
list, and nothing outside it writes. The console is a view over a core that
already owns every mutation here, so a name that is not in `WRITABLE` has no
code path that performs it.

**The manifest is deliberately absent.** `verification/manifest.json` is
executable repository policy: committed, reviewed in a diff, and its digest is
stamped into every run report. A console that could rewrite it would let an
operator weaken the contract the evidence is measured against with nothing to
review. Changing the manifest means making a commit. `PROTECTED_PATHS` names the
paths that are policy rather than state, and a test asserts no handler in this
package writes anything under them.

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


# The permitted surface. Six names, and nothing outside this tuple writes. The
# manifest is not among them and must never be added: see the module docstring.
WRITABLE: tuple[Operation, ...] = (
    Operation(
        name="enroll",
        effect="Register a repository as verified",
        implemented=False,
        writes=("the repository's vkit registration",),
        note="No core operation exists; the console performs nothing.",
    ),
    Operation(
        name="install",
        effect="Install the host plugin, skills, and hooks at a chosen scope",
        implemented=False,
        writes=("plugin directory", "skills directory", "hook configuration"),
        note="No core operation exists; the console performs nothing.",
    ),
    Operation(
        name="repair",
        effect="Re-apply a drifted installation",
        implemented=False,
        writes=("plugin directory", "skills directory", "hook configuration"),
        note="No core operation exists; the console performs nothing.",
    ),
    Operation(
        name="remove",
        effect="Undo an installation, preserving recoverable prior values",
        implemented=False,
        writes=("plugin directory", "skills directory", "hook configuration"),
        note="No core operation exists; the console performs nothing.",
    ),
    Operation(
        name="run_check",
        effect="Start a registered check",
        implemented=True,
        writes=("the run's own record in the state database", "the run's artifact directory"),
    ),
    Operation(
        name="cancel_run",
        effect="Stop a running check, verifying process identity first",
        implemented=True,
        writes=("the run's terminal report",),
    ),
)

WRITABLE_NAMES: tuple[str, ...] = tuple(operation.name for operation in WRITABLE)

#: Repository paths that are policy, not state. Nothing in this package writes
#: them. `verification/manifest.json` is committed policy whose digest every run
#: report carries, so rewriting it from a browser would let an operator weaken
#: the contract with no diff to review. The schemas are the same.
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
