"""Replay the formal correspondence sequence deterministically, and assert agreement.

`tests/test_formal_correspondence.py::test_the_core_and_the_reference_model_agree`
proves correspondence over the sequences Hypothesis happened to generate. It is
the real evidence, and it is slow: with hypothesis installed it runs 200
examples of up to 25 steps, which takes a couple of minutes. That is a bad
latency for the question this script exists to answer -- "did something just
break the core/reference agreement, and on which step?" -- where you want an
answer in under a second and you want to know the step.

So this replays one fixed sequence, the two-step one this check was originally
written to diagnose:

    [('record', 'w1', 'unit', 'rev1', 'FAIL'), ('crash', 'w1')]

and compares the core and the reference model after every step. It uses the
same `Core` adapter the test uses, so the replay cannot differ from the test by
reimplementing anything, and it prints what each side decided.

`compute_readiness` filters evidence by attempt generation, so a recorded FAIL
stops counting as evidence once its attempt has been superseded. That filter is
why this sequence AGREES. It used not to, and the disagreement was real:

    step 1: crash w1
      generations: core=2 reference=2
         w1: core=BLOCKED  reference=REJECTED

The core dropped the superseded FAIL and answered BLOCKED (the required checks
are no longer satisfied at the new attempt); the reference model carried the
stale FAIL across the generation change and answered REJECTED. The model was
encoding the bug as the specification, and `reference.acceptable` now filters
by generation for the same reason `compute_readiness` does. So the divergence
this script was written to demonstrate is fixed, and the script's job is now to
say so loudly rather than to assert it.

Which means the interesting question is whether this check can still DETECT a
divergence, or whether it has degenerated into a script that prints "agreed"
forever. `--self-test` answers that by reintroducing the exact historical bug
in a scratch copy of `formal/reference.py` under a temporary directory -- never
in the working tree -- and requiring the check to fire on it. The check is only
worth running if it can still fail.

Exit status: 0 when the core and the reference model agreed on every step, 1
when they diverged (and, under --self-test, when the mutated model was NOT
detected, which is a failure of this script rather than of the core).

Run:  python scripts/diagnose_formal_divergence.py
      python scripts/diagnose_formal_divergence.py --self-test
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "formal"))
sys.path.insert(0, str(ROOT / "tests"))

import reference  # noqa: E402
from test_formal_correspondence import Core, _reference_init  # noqa: E402
from vkit.storage import Store  # noqa: E402
from vkit.tasks import compute_readiness  # noqa: E402

SEQUENCE = [
    ("record", "w1", "unit", "rev1", "FAIL"),
    ("crash", "w1"),
]

#: The guard in `reference.Model._latest_by_check` that filters evidence down to
#: the owner's current attempt. Disabling it is what this script's divergence
#: used to be. Matched on content, not on indentation, so reformatting
#: `reference.py` cannot silently turn the self-test into a no-op -- the guard
#: below fails loudly instead.
GENERATION_FILTER = "if generation is not None and record.generation != generation:"


def replay(ideal: reference.Model) -> tuple[list[str], list[str]]:
    """Drive both models through SEQUENCE, comparing after every step.

    Returns the transcript lines and the divergences found. A divergence is one
    owner whose verdict the two models disagree about at one step, or one step
    on which the generations themselves disagree -- the test asserts both, so
    both are checked here.
    """
    store = Store(Path(tempfile.mkdtemp()) / "state.sqlite3")
    real = Core(store)
    lines: list[str] = []
    diverged: list[str] = []

    for step, op in enumerate(SEQUENCE):
        kind = op[0]
        before = f"after {step - 1} steps, before {op}"
        if kind == "record":
            _, owner, check, revision, outcome = op
            lines.append(f"step {step}: record {owner} {check} @ {revision} -> {outcome}")
            real.record(owner, check, revision, outcome)
            ideal = ideal.record(owner, check, revision, outcome)
        elif kind == "crash":
            _, owner = op
            lines.append(f"step {step}: crash {owner}")
            real.crash(owner)
            # The core's supersede stops the attempt AND advances its
            # generation, so the reference takes both steps in that order. This
            # is the test's mapping, not a choice made here.
            ideal = ideal.crash(owner)
            ideal = ideal.reconcile(owner)

        for who in reference.OWNERS:
            core_gen = real.generations(who)
            if core_gen != ideal.generations[who]:
                diverged.append(
                    f"generation of {who} diverged {before}: "
                    f"core={core_gen} reference={ideal.generations[who]}"
                )
                lines.append(
                    f"  !! generation of {who}: core={core_gen} "
                    f"reference={ideal.generations[who]}"
                )

        lines.append(
            f"  generations: core={real.generations('w1')} "
            f"reference={ideal.generations['w1']}"
        )
        for who in reference.OWNERS:
            core = real.decide(who, ideal.revision)
            ref = ideal.decide(who, ideal.generations[who], ideal.revision)
            mark = "  " if core == ref else "!!"
            lines.append(f"  {mark} {who}: core={core:8} reference={ref:8}")
            if core != ref:
                diverged.append(
                    f"readiness for {who} diverged {before}: "
                    f"core={core} reference={ref} "
                    f"(core generation {real.generations(who)}, reference generation "
                    f"{ideal.generations[who]})"
                )
                runs = [
                    (r["check_id"], r["result"])
                    for r in store.list_runs(limit=50)
                    if r.get("task_id") == who
                ]
                lines.append(f"     core runs recorded for {who}: {runs}")
                detail = compute_readiness(
                    store, who, required_check_ids=list(reference.REQUIRED_CHECKS)
                )
                lines.append(f"     core gaps: {detail.gaps}")
                lines.append(
                    f"     core keeps evidence at generations "
                    f"{sorted(getattr(ideal, 'generations', {}))}: "
                    f"the reference still holds the recorded FAIL at generation "
                    f"{ideal.generations[who]}"
                )

    return lines, diverged


def report(lines: list[str], diverged: list[str], heading: str) -> None:
    print(f"== {heading} ==")
    for line in lines:
        print(line)
    print()
    if diverged:
        print(f"DIVERGED on {len(diverged)} observation(s):")
        for item in diverged:
            print(f"  {item}")
    else:
        print("AGREED on every step: generations and verdicts matched throughout.")
    print()


def mutated_model() -> reference.Model:
    """Build a reference model whose generation filter is switched off.

    The patch is applied to a COPY of `formal/reference.py` in a temporary
    directory and imported from there. The working tree is not modified, and
    the copy differs from the real model by exactly one line -- the generation
    filter -- so what the self-test detects is that specific regression and not
    an artefact of a different model.

    `_reference_init` from the test is not reusable here because it closes over
    the real `reference` module; this is the same three setup calls against the
    mutated module.
    """
    scratch = Path(tempfile.mkdtemp())
    source = (ROOT / "formal" / "reference.py").read_text(encoding="utf-8")
    if source.count(GENERATION_FILTER) != 1:
        raise SystemExit(
            "self-test cannot run: expected exactly one occurrence of the "
            f"generation filter ({GENERATION_FILTER!r}) in formal/reference.py, "
            f"found {source.count(GENERATION_FILTER)}. This script can no longer "
            "reconstruct the divergence it checks for, so it will not pretend to."
        )
    # `if <cond>: continue` becomes `if False: continue`, which is a no-op
    # branch: the same shape, with the generation comparison gone.
    target = scratch / "reference_stale_generation.py"
    target.write_text(
        source.replace(GENERATION_FILTER, "if False:  # self-test: generation ignored"),
        encoding="utf-8",
    )

    spec = importlib.util.spec_from_file_location("reference_stale_generation", target)
    if spec is None or spec.loader is None:
        raise SystemExit(f"self-test cannot import the mutated model from {target}")
    module = importlib.util.module_from_spec(spec)
    # `@dataclass` resolves types through `sys.modules[cls.__module__]`, so the
    # module has to be registered before it is executed or the decorator raises
    # on a module that is not yet findable.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    model = module.Model(revision=module.REVISIONS[0])
    for owner in module.OWNERS:
        model = model.add_owner(owner)
    for resource in module.RESOURCES:
        model = model.add_resource(resource)
    return model


def main(argv: list[str]) -> int:
    lines, diverged = replay(_reference_init())
    report(lines, diverged, "replaying the sequence")
    if diverged:
        print("The core and the reference model disagree. The model was written")
        print("from the documented rules, so a disagreement is a real finding:")
        print("read which side is wrong before changing either.")
        return 1

    print("== what this is ==")
    print("  A correspondence check over a fixed sequence, not a claim about a")
    print("  known divergence. This sequence used to diverge on step 1: the core")
    print("  answered BLOCKED and the reference model REJECTED, because the core")
    print("  filters evidence by attempt generation and the model did not. The")
    print("  model filters by generation now, the two agree, and the divergence")
    print("  this script was written to demonstrate is fixed.")
    print()
    print("  'Agreed on this sequence' is a weaker sentence than 'they agree'.")
    print("  The full evidence is tests/test_formal_correspondence.py, which is")
    print("  slow; this is the fast answer to 'did the agreement just break, and")
    print("  on which step'. Run it with --self-test to confirm it can still fail.")
    print()

    if "--self-test" not in argv:
        return 0

    stale_lines, stale_diverged = replay(mutated_model())
    report(stale_lines, stale_diverged, "self-test: generation filter disabled")
    if not stale_diverged:
        print("SELF-TEST FAILED: the check did not detect a reference model that")
        print("carries a superseded FAIL across a generation change. A check that")
        print("cannot fail is not a check, so the agreement reported above means")
        print("nothing until this fires.")
        return 1
    print("Self-test passed: the check detects the divergence it was written for,")
    print("so the agreement reported above is a real observation and not a check")
    print("that has stopped looking.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
