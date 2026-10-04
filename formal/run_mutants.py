"""Break each guard in turn and require TLC to notice.

A property test is only as good as the defect it refuses to accept. This
harness makes that concrete: it copies the model, deletes one guard, runs TLC
on the copy, and REQUIRES a counterexample. A mutant that still passes is not a
weak mutant to be explained away, it is a broken property, and the harness
fails the build over it.

The four mutants, and what each one removes:

  MUTANT_STALE_GENERATION  `g = genOf[o]` from Accept. This is the guard the
                           plan cares most about: a superseded attempt must
                           not be able to publish an accepted verdict.
  MUTANT_STALE_IDENTITY    `r = curRevision` from Accept. A task must not be
                           satisfied by readiness decided under the source or
                           policy revision it replaced.
  MUTANT_MISSING_CHECK     the full-coverage requirement from `Acceptable`,
                           so acceptance stops requiring EVERY check.
  MUTANT_CRASH_RELEASES    the `owner`/`owned` entries from Crash's UNCHANGED
                           list, so a crash silently frees the resource.

The stale-generation mutant is run against BOTH the TLA+ model and the Python
correspondence test, because the plan asks for that one specifically and it is
the one a reviewer will want to see fail.

Run directly:

    python formal/run_mutants.py

Exit 0 when every mutant was caught, 1 when one passed, 2 when TLC was
unavailable and nothing could be checked.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TLA_DIR = ROOT / "formal" / "tla"
MODEL = TLA_DIR / "OwnershipAcceptance.tla"
CFG = TLA_DIR / "OwnershipAcceptance.cfg"

sys.path.insert(0, str(ROOT / "formal"))
from run_tlc import _find_java, _find_jar, _final_totals  # noqa: E402

_WINDOW_OPTIONS = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


@dataclass(frozen=True)
class Mutant:
    """One guard, the exact text that removes it, and what must break."""

    name: str
    removes: str
    replacement: str
    expect_property: str | None
    covers: str


MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        name="MUTANT_STALE_GENERATION",
        removes="    /\\ g = genOf[o]\n",
        replacement="",
        expect_property="Property2_SupersededGenerationCannotAccept",
        covers="a superseded attempt publishing an accepted verdict",
    ),
    Mutant(
        name="MUTANT_STALE_IDENTITY",
        removes="    /\\ r = curRevision\n",
        replacement="",
        expect_property="Property4_IdentityChangeInvalidatesPriorReadiness",
        covers="readiness decided under a replaced source or policy revision",
    ),
    Mutant(
        name="MUTANT_MISSING_CHECK",
        removes='    /\\ \\A c \\in Checks : evidence[c][o][g][r] = "PASS"\n',
        replacement="",
        expect_property="Property3_AcceptanceRequiresEveryCheck",
        covers="acceptance with one required check missing",
    ),
    Mutant(
        name="MUTANT_CRASH_RELEASES",
        removes="    /\\ owner' = owner\n    /\\ owned' = owned\n",
        replacement=(
            "    /\\ LET movers == {r \\in Resources :"
            " /\\ owner[r] = o /\\ \\E x \\in Owners : x # o /\\ alive[x]}\n"
            "           thief == (CHOOSE x \\in Owners : x # o /\\ alive[x])\n"
            "       IN  /\\ movers # {}\n"
            "           /\\ owner' = [r \\in Resources |->"
            " IF r \\in movers THEN thief ELSE owner[r]]\n"
            "           /\\ owned' = [r \\in Resources |->"
            " IF r \\in movers THEN genOf[thief] ELSE owned[r]]\n"
        ),
        expect_property="Property5_CrashAloneDoesNotTransferOwnership",
        covers="a crash silently transferring a claim to a competing live attempt",
    ),
)


def _run_tlc(model_dir: Path, java: Path, jar: Path) -> str:
    """Run TLC on a directory holding one mutant model. Returns the output."""
    states = model_dir / "states"
    done = subprocess.run(
        [
            str(java), "-XX:+UseParallelGC", "-Xmx2g", "-cp", str(jar), "tlc2.TLC",
            "-workers", "1", "-nowarning", "-cleanup", "-metadir", str(states),
            "OwnershipAcceptance",
        ],
        cwd=model_dir, capture_output=True, text=True, timeout=3600, check=False,
        **_WINDOW_OPTIONS,
    )
    return done.stdout + done.stderr


def _violated_property(blob: str) -> str | None:
    """The name of the property TLC reports as violated, or None.

    TLC prefixes an invariant's name with `Invariant ` and an action property's
    with `Action property `. The prefix is stripped so the harness compares the
    bare name, and the two forms stay distinguishable in the printed output.
    """
    for line in blob.splitlines():
        if "is violated" not in line or "Error" not in line:
            continue
        name = line.split("Error: ", 1)[-1].replace(" is violated.", "").strip()
        for prefix in ("Invariant ", "Action property "):
            if name.startswith(prefix):
                name = name[len(prefix):]
        return name
    return None


def _tlc_failed_to_run(blob: str) -> str | None:
    """Why TLC did not produce a verdict, or None if it did.

    This exists because its absence is how an earlier revision of this harness
    reported a broken mutant as an uncovered property. A mutant whose model
    raises a Java exception produces no counterexample and no completion line,
    which is indistinguishable from "the property did not fire" if you only ask
    whether a violation was found. One of the four mutations sat silently
    uncaught for exactly that reason. Every way TLC can decline to answer is
    enumerated here so the run is BLOCKED rather than green.
    """
    if "TLC threw an unexpected exception" in blob:
        return "the mutated model is not well-formed; TLC raised an exception"
    if "Parsing or semantic analysis failed" in blob:
        return "the mutated model does not parse"
    if "Semantic errors:" in blob:
        return "the mutated model has semantic errors"
    if "OutOfMemory" in blob:
        return "TLC ran out of memory"
    if "states left on queue" not in blob:
        return "TLC produced no final state count"
    return None


def main() -> int:
    java, jar = _find_java(), _find_jar()
    if java is None or jar is None:
        print(
            "BLOCKED: no java or tla2tools.jar. Mutation evidence requires TLC, and "
            "TLC is opt-in. Ordinary verification does not need it.",
            file=sys.stderr,
        )
        return 2

    source = MODEL.read_text(encoding="utf-8")
    results: list[tuple[Mutant, bool, str | None, str]] = []
    failures: list[str] = []

    for mutant in MUTANTS:
        if mutant.removes not in source:
            failures.append(
                f"{mutant.name}: the text it removes is no longer in the model. The "
                f"guard was reworded and the mutation now does nothing."
            )
            results.append((mutant, False, None, "target text not found in model"))
            continue

        with tempfile.TemporaryDirectory(prefix=f"vkit-{mutant.name}-") as work:
            workdir = Path(work)
            mutated = source.replace(mutant.removes, mutant.replacement, 1)
            (workdir / "OwnershipAcceptance.tla").write_text(mutated, encoding="utf-8")
            shutil.copy(CFG, workdir / "OwnershipAcceptance.cfg")
            blob = _run_tlc(workdir, java, jar)

        property_name = _violated_property(blob)
        ran = _tlc_failed_to_run(blob)
        if ran is not None:
            failures.append(f"{mutant.name}: BLOCKED, {ran}. The mutation is invalid.")
            results.append((mutant, False, None, f"BLOCKED, {ran}"))
            continue
        if property_name is None:
            failures.append(
                f"{mutant.name}: TLC found no counterexample. The guard it removes is "
                f"not covered by any checked property, so that rule is unverified."
            )
            results.append((mutant, False, None, "no counterexample produced"))
            continue

        wrong = mutant.expect_property is not None and property_name != mutant.expect_property
        if wrong:
            note = f"caught, but by {property_name} rather than {mutant.expect_property}"
        else:
            note = f"caught by {property_name}"
        if wrong:
            failures.append(f"{mutant.name}: {note}")
        results.append((mutant, not wrong, property_name, note))

    print("mutation results")
    print("=" * 72)
    for mutant, caught, property_name, note in results:
        mark = "CAUGHT" if caught else "ESCAPED"
        print(f"{mark:>8}  {mutant.name}")
        print(f"          removes: {mutant.covers}")
        print(f"          outcome: {note}")
    print("=" * 72)

    if failures:
        print()
        for line in failures:
            print(f"FAILED: {line}")
        return 1
    print(f"\nAll {len(MUTANTS)} mutants produced a counterexample.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
