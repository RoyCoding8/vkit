"""The process that hosts a candidate's own check and captures what it produced.

`python -m vkit.integration.launcher <argv...>` runs `<argv...>` as a child
process, waits for it, then copies the artifacts the child left in the run
directory to verifier-named paths. Its own exit status is the child's, so a
timeout or a crash at the launcher's level cannot be reported as a passing
check.

**Why a child process and not an in-process call.** Two reasons, and the second
is the important one. The first is that a check may run for minutes or leave
grandchildren behind, and the verifier has to survive either. The second is that
the child's code, when it does try to import `vkit`, is refused by
`vkit/__init__.py` because the launcher sets the flag; an in-process call would
put the candidate's code in the same interpreter as the code that reads the
result, and there would be no import boundary left to refuse anything at.

**What it copies, and when.** After the child exits, every artifact named in
`VKIT_CAPTURE_ARTIFACTS` is read and written to `<name>.vkit-captured` beside
the original. The copy is taken after the last process to write the original has
exited, so it is the bytes that survived. The verifier compares the two, and a
mismatch is a BLOCKED reason rather than a pass.

**The capture list is passed in, not read from the manifest.** The manifest is
the candidate's to edit, and this is the list of things the verifier will hold
its own copy of. `execution.run_check` names the one artifact the check is
contracted to write.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ..nowindow import hidden_window
from . import sandbox

ENV_FLAG = "VKIT_TRUSTED_LAUNCHER"
ENV_ARTIFACTS = "VKIT_CAPTURE_ARTIFACTS"
ENV_RUN_DIR = "VKIT_RUN_DIR"


def capture(run_dir: Path, names: list[str]) -> list[str]:
    """Copy each named artifact to its verifier-owned copy. Returns what moved."""
    captured: list[str] = []
    for name in names:
        source = run_dir / name
        if not source.is_file():
            continue
        sandbox.captured_path(run_dir, name).write_bytes(source.read_bytes())
        captured.append(name)
    return captured


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: python -m vkit.integration.launcher <argv...>",
            file=sys.stderr,
        )
        return 2

    os.environ[ENV_FLAG] = "1"

    command = argv[1:]
    try:
        completed = subprocess.run(
            command, stdin=subprocess.DEVNULL, check=False, **hidden_window()
        )
    except FileNotFoundError as exc:
        print(f"launcher: {command[0]!r} is not executable: {exc}", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"launcher: could not run {command[0]!r}: {exc}", file=sys.stderr)
        return 126

    run_dir = os.environ.get(ENV_RUN_DIR)
    if run_dir:
        names = [n for n in os.environ.get(ENV_ARTIFACTS, "").split("\x00") if n]
        capture(Path(run_dir), names)

    return completed.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
