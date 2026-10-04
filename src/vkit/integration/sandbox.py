"""A candidate's own verifier code runs, and the bytes it produces are evidence
that the candidate cannot touch.

The problem this solves is specific. A check is an executable the repository
defines, so the candidate supplies the verifier code that runs its own checks.
That code is attacker-controlled by definition, and the question is not whether
to run it -- docs/verification.md requires the checks to run -- but what it is allowed
to *say*.

**Three things make its output untrustworthy on its own.**

1. It could import `vkit` and use the authoritative package. `vkit/__init__.py`
   refuses the import when `VKIT_TRUSTED_LAUNCHER` is set, so inside the
   trusted process the candidate's `import vkit` is refused at import time. The
   authoritative package is imported under its own name *before* the candidate's
   code is loaded, so the refusal is about the name the candidate typed and
   nothing else is taken away from it.
2. It could write the artifact and then rewrite it. The trusted launcher runs
   the candidate's command and copies the artifact out of the shared run
   directory *after that process exits*, so the bytes that get validated are the
   bytes the last writer left. A second copy at a verifier-named path is kept
   for the comparison in `execution._revalidate`.
3. It could write a well-formed artifact claiming PASS for a check that fails.
   Schema validity is the floor, not the answer. The process's real exit status
   and the real filesystem effect are the answer, and the artifact is a claim
   about them.

**What this does not claim.** This is not a sandbox against hostile code. The
candidate's process runs with the job's own privileges and can read the
checkout, run commands and take a long time. What it cannot do is change the
verdict without leaving a disagreement the verifier sees, and that is the
property Plan 07 asks for.

**Where the executable comes from.** The launcher is the authoritative package
re-executed as `python -m vkit.integration.launcher`, with the check's `argv`
passed as data. The check's own command is unchanged: a check naming `python`
still gets `python`, and `{{python}}` still names the interpreter that will
re-validate the result.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from ..execution import RunEnvironment
from ..manifest import CheckSpec, Manifest
from ..outcome import Blocked, BlockedReason
from ..procs import ExecutionResult, run_command

CAPTURED_SUFFIX = ".vkit-captured"

LAUNCHER_MODULE = "vkit.integration.launcher"

_CONTRACT_VARS = (
    "VKIT_CAPTURE_ARTIFACTS",
    "VKIT_RUN_DIR",
    "PYTHONPATH",
)


def run_in_plugin_subprocess(
    argv: list[str],
    *,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: float,
    plugin_root: Path,
    capture_artifacts: tuple[str, ...] = (),
) -> ExecutionResult:
    """Run the check's own command under the trusted launcher.

    The check's argv is handed over unchanged, so the check is exactly the
    approved one; only the interpreter hosting it is the launcher's, and that
    interpreter is the one that will re-validate the result.

    `capture_artifacts` names what the launcher copies out after the check
    exits. It is passed in rather than read from the manifest because the
    manifest is the candidate's to edit, and this is the list the verifier will
    hold its own copy of.

    The three variables are set on this process and restored afterwards, rather
    than passed to the launch. The reason is that `procs.run_command` has no
    environment parameter on either platform: the POSIX path would need a
    signature change, and the Windows path hands `CreateProcess` a `None`
    environment, so the child inherits whatever this process holds. A single
    mechanism that works the same on both platforms is worth more than an
    environment parameter that is only honoured on one. Nothing else in this
    process reads them, and the values are removed even when the launch fails.
    """
    previous = {name: os.environ.get(name) for name in _CONTRACT_VARS}
    os.environ["VKIT_CAPTURE_ARTIFACTS"] = "\x00".join(capture_artifacts)
    os.environ["VKIT_RUN_DIR"] = str(Path(stdout_path).parent)
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [str(_package_root(plugin_root)), *([previous["PYTHONPATH"]]
                                            if previous.get("PYTHONPATH") else [])]
    )
    try:
        return run_command(
            [sys.executable, "-m", LAUNCHER_MODULE, *argv],
            cwd=cwd, stdout_path=stdout_path, stderr_path=stderr_path,
            timeout_seconds=timeout_seconds,
        )
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _package_root(plugin_root: Path) -> Path:
    """The source tree holding the authoritative vkit.

    The tree this process was imported from is by construction the tree that
    will read the result, so it wins. A pinned plugin root is an assertion about
    which tree that is, and a root that does not actually hold the package is
    refused rather than silently overridden, because a policy that named the
    wrong tree should stop the run instead of quietly running another one.
    """
    from .. import __file__ as self_init

    imported_root = Path(self_init).resolve().parent.parent
    if imported_root == plugin_root.resolve():
        return imported_root
    if (plugin_root / "vkit" / "__init__.py").is_file():
        return plugin_root
    raise ImportError(
        f"no authoritative vkit package at {plugin_root}; the trusted launcher "
        "cannot be started without the tree that will read its result"
    )


def captured_path(run_dir: Path, artifact_name: str) -> Path:
    """Where the trusted launcher writes the authoritative copy."""
    return run_dir / f"{artifact_name}{CAPTURED_SUFFIX}"


def read_artifacts(run_dir: Path, artifact_name: str) -> tuple[bytes, bytes | None]:
    """(bytes the trusted launcher captured, bytes the candidate's file holds).

    The captured copy is authoritative. When they differ, the candidate's copy
    was written after the process that produced it exited, and the caller turns
    that into a BLOCKED reason. `None` for the second means the candidate
    deleted its own artifact, which is also a disagreement.
    """
    captured = captured_path(run_dir, artifact_name)
    if not captured.is_file():
        raise FileNotFoundError(
            f"the trusted launcher captured no {artifact_name}; the check did not "
            "run under it"
        )
    submitted = run_dir / artifact_name
    return captured.read_bytes(), (submitted.read_bytes() if submitted.is_file() else None)


def validate_artifact_bytes(
    raw: bytes, *, check: CheckSpec, manifest: Manifest, env: RunEnvironment
) -> Blocked | None:
    """Validate the captured bytes against the authoritative schema.

    The schema is the one shipped with the package doing the validating, never
    one found in the candidate's checkout. A candidate that ships a loosened
    schema changes nothing here. The required scenarios come from the check
    record of the manifest this run is executing, which for a protected run is
    the approved manifest, so a candidate cannot drop a scenario by editing the
    manifest it ships.
    """
    from ..execution import _scenarios_from_artifact

    _scenarios, problem = _scenarios_from_artifact(raw, check.required_scenarios)
    return problem


def disagreement(captured: bytes, submitted: bytes | None) -> Blocked | None:
    """The reason a candidate's artifact cannot be accepted, or None.

    Kept apart from the schema check so the two failures stay distinguishable:
    bytes that do not parse are a malformed artifact, and bytes that parse but
    differ from what the trusted launcher captured are an edit after the fact.
    """
    if submitted is None:
        return Blocked(
            BlockedReason.ARTIFACT_MISSING,
            "the check's artifact was removed after the process that produced it "
            "exited; the copy the trusted launcher captured is the only one left",
        )
    if captured != submitted:
        return Blocked(
            BlockedReason.ARTIFACT_MALFORMED,
            "the artifact the check left differs from the bytes its trusted launcher "
            "captured at exit; a check's evidence cannot be edited in place after "
            "the process that produced it wrote it",
        )
    return None
