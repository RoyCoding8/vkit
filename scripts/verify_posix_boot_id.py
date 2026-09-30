"""Drive the boot-id half of the POSIX identity through recover.liveness.

`recover._creation_time_mismatch` compared the start-time tick count on its own.
A tick count is scoped to one boot, and WSL restarts the init namespace, so a
record written before a restart can carry the same tick count as a process
running after it. That is a half-fix: boot_id was recorded in the run record and
never compared at the point where the decision is made.

This drives liveness with a matching tick count and a wrong boot, which is the
case that must not read as a match.

Run:  bash scripts/posix-run.sh scripts/verify_posix_boot_id.py
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vkit.procidentity import boot_id, read_identity  # noqa: E402
from vkit.recover import LivenessState, liveness  # noqa: E402

ok = True


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok
    if not condition:
        ok = False
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    if sys.platform == "win32":
        print("this verifies the POSIX boot-id check; run it under WSL")
        return 2

    print("== liveness compares the boot, not only the tick count ==")
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    time.sleep(0.4)
    try:
        identity = read_identity(live.pid)
        assert identity is not None
        real_boot = identity.boot_id
        real_ticks = identity.creation_time
        print(f"  pid {live.pid}: ticks={real_ticks} boot={real_boot}")

        matching = liveness(
            live.pid, creation_time=real_ticks, boot_id=real_boot
        )
        check(
            "same pid, same ticks, same boot -> ALIVE",
            matching.state is LivenessState.ALIVE,
            matching.detail,
        )

        other_boot = liveness(
            live.pid, creation_time=real_ticks, boot_id="a-different-boot"
        )
        check(
            "same pid, same ticks, DIFFERENT boot -> UNCERTAIN",
            other_boot.state is LivenessState.UNCERTAIN,
            other_boot.detail,
        )
        check(
            "the boot is named in the detail, so the reason is legible",
            "a-different-boot" in other_boot.detail and real_boot in other_boot.detail,
        )

        # A record with no boot compared against a live process that has one.
        # The current code only objects when the RECORDED boot is non-empty and
        # differs, so a legacy record matches on the tick count alone. Both
        # answers are defensible; the one that matters is that it is stated
        # rather than accidental, so this prints what it does instead of
        # asserting a direction.
        no_boot = liveness(live.pid, creation_time=real_ticks)
        print(f"  same pid, same ticks, no boot recorded -> {no_boot.state.value}")
        print(f"    {no_boot.detail}")
        print(
            "    A tick count with no boot is weaker evidence than one with a boot, so"
        )
        print(
            "    treating it as a match is the direction that keeps a claim alive but"
        )
        print(
            "    is also the direction that trusts a weaker identity. That trade is"
        )
        print(
            "    recorded here rather than asserted; it is a product decision about"
        )
        print(
            "    what a pre-boot_id record is worth, and not one this change makes."
        )

        check("the process is still running, so nothing was signalled", live.poll() is None)
    finally:
        live.kill()
        live.wait()

    print()
    print("VERDICT: " + ("every check passed" if ok else "at least one check FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
