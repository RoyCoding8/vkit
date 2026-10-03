"""Interpret one approved Hypothesis run, and record what was sampled rather than that it holds.

The evidence kind a run produces is decided before this module reads a byte. A
`property` check says so in the manifest, by declaring the `property` variant
with a `generator` block; a `pytest` check over the same file does not, and is
SCENARIO whatever Hypothesis happens to be importable in the child.
`plans/10-native-verifiers.md:44` is explicit that the category comes from the
declared obligation and never from a package being installed, and this module
takes no part in deciding it: `spec.evidence_kind` does, before `interpret` is
called. Requirement 2 is therefore a structural property of the types rather than
a rule this reader enforces at runtime.

**What is read, and what is not.** This reuses `pytest_adapter`'s report
verbatim. Hypothesis is a library a test calls; it does not replace the runner
or change what the runner reports, so there is one report format here rather
than two readings that would have to agree. The generator settings are measured
off the test object during the run and travel beside the report in each entry.

**Why the generator settings are measured, not copied from the manifest.** The
manifest declares what the owner pinned. What ran is what the test object says
afterwards, and a decorator or a `conftest.py` profile can move it. Recording the
declaration would make the receipt describe the request rather than the run,
which is the same overstatement as quoting an example count out of the docs.
Measured on hypothesis 6.168.3: `test._hypothesis_internal_use_settings` carries
the effective `max_examples`, `stateful_step_count`, `deadline`, `database` and
`suppress_health_check` at the end of the call phase.

**What the receipt does not claim.** Hypothesis samples a bounded family of
inputs and reports no disagreement. It does not exhaust that family, so no
count here is a proof and `TestedScope.sentence` says so in the words the
receipt carries. `claimkind.py` draws the same distinction in prose, and the two
must agree: a receipt whose observation said "holds for all inputs" would
contradict the category it is filed under.

**The example count is not recorded, because nothing supported reports it.**
Measured on 6.168.3: `hypothesis.statistics.collector` is a `DynamicVariable`,
`get_statistics_for` does not exist on it, and `_hypothesis_internal_use_statistics`
is `None` after a plain pass. The engine's own statistics live on a private
attribute of a private class. So the tested scope reported here is the pinned
family and its bound, which is what the evidence actually establishes, rather
than a number no supported API yields.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..outcome import Blocked, BlockedReason
from . import pytest_adapter
from .obligation import CaseObligation, obligation_to_json
from .spec import HypothesisSettings, PropertyCheck, ReplaySettings

#: What the generator settings and replay information are recorded under in a
#: receipt's `assumptions`. Assumptions rather than a new receipt field, because
#: `schemas/receipt.v2.json` has `additionalProperties: false` and a pinned field
#: set that belongs to whoever owns the schema. An assumption is where a reader
#: is told what a run did and what it did not cover.
GENERATOR_TOKEN = "hypothesis generator settings in force"


def _assumption(settings: HypothesisSettings) -> str:
    """One line naming the generator settings this run actually used."""
    return (
        f"{GENERATOR_TOKEN}: max_examples={settings.max_examples}, "
        f"stateful_step_count={settings.stateful_step_count}, "
        f"deadline={'none' if settings.deadline is None else settings.deadline}, "
        f"suppress_health_check="
        f"{','.join(settings.suppress_health_check) if settings.suppress_health_check else 'none'}"
    )


def _measured(document: dict) -> HypothesisSettings:
    """The generator settings a run reported, as vkit's own value.

    Read out of the report rather than out of the manifest on purpose, for the
    reason the module docstring gives.
    """
    deadline = document["deadline"]
    return HypothesisSettings(
        max_examples=int(document["max_examples"]),
        stateful_step_count=int(document["stateful_step_count"]),
        deadline=None if deadline is None else float(deadline),
        suppress_health_check=tuple(
            str(name) for name in document["suppress_health_check"]
        ),
    )


@dataclass(frozen=True)
class TestedScope:
    """What was sampled, in the words the receipt and the display both read.

    `family` is the bound, not a count. It is the number of examples the
    generator was permitted to try, which is a ceiling on the evidence and never
    a count of what was tried: Hypothesis stops early when it has seen enough,
    and the two numbers are different facts about a run.

    `family` is None when no generator settings were in force, which is a real
    state rather than a missing value: a required test that is not a Hypothesis
    test sampled no family at all, and saying so is the honest sentence. A zero
    would read as a family of no examples that had been tried.
    """

    family: int | None
    stateful_step_count: int = 0

    def sentence(self) -> str:
        """The scope a reader is shown, and the limit stated in the same breath.

        The second clause is not decoration. `claimkind.py` says the sequences
        are sampled rather than exhausted, and a scope shown without that reads
        as "checked everything", which is the sentence this whole category
        exists to stop anyone writing.
        """
        if self.family is None:
            return (
                "no generator settings were in force for this case, so no family "
                "was sampled. The verdict below stands on the disagreement the "
                "runner reported, and not on any claim about a bounded search."
            )
        return (
            f"no disagreement over a sampled family of up to {self.family} "
            f"generated case(s)"
            + (
                f", with up to {self.stateful_step_count} step(s) per stateful run"
                if self.stateful_step_count
                else ""
            )
            + ". Hypothesis samples a bounded family rather than exhausting it, so "
            "this establishes correspondence over the cases actually generated and "
            "not over every possible input."
        )


@dataclass(frozen=True)
class PropertyAdapterResult:
    """What one Hypothesis run established, with the scope it establishes it over.

    Wraps `pytest_adapter.AdapterResult` rather than reimplementing it. The run
    was a pytest run; what this adds is the two things that make it property
    evidence rather than scenario evidence, and neither of them changes which
    cases passed.
    """

    reading: pytest_adapter.AdapterResult
    generator: HypothesisSettings
    replay: ReplaySettings | None
    scope: TestedScope

    @property
    def is_pass(self) -> bool:
        return self.reading.is_pass

    @property
    def counterexamples(self) -> tuple:
        """The cases that disagreed, lifted from the reading this wraps.

        Exposed rather than left behind the `reading` member because it is one
        of the two things `dispatch.outcome_from_reading` asks every adapter for.
        A caller that had to reach through `reading` to learn whether a run had
        failed would be coupling itself to this adapter's composition rather than
        to the contract every adapter owes the core.
        """
        return self.reading.counterexamples

    @property
    def observations(self) -> tuple[tuple[CaseObligation, str], ...]:
        """Each satisfied case, with the scope sentence attached to it.

        The scope rides on every observation rather than sitting beside them,
        because an observation read alone is the sentence a reader skims, and
        "the runner reported passed in 4ms" says nothing about what was sampled.
        One property PASS therefore cannot be read as a bare scenario PASS.
        """
        scope = self.scope.sentence()
        return tuple(
            (obligation, f"{observation}. Tested scope: {scope}")
            for obligation, observation in self.reading.observations
        )

    def scenarios(self) -> tuple:
        """The reading in the shape every existing outcome consumer already reads."""
        from ..outcome import ScenarioResult

        scope = self.scope.sentence()
        return tuple(
            ScenarioResult(
                obligation.test_id, True, f"{observation}. Tested scope: {scope}",
            )
            for obligation, observation in self.reading.observations
        ) + tuple(
            ScenarioResult(obligation.test_id, False, trace)
            for obligation, trace in self.reading.counterexamples
        )

    def obligation_results(self) -> tuple[list[dict], list[dict]]:
        """The satisfied obligations and counterexamples, in the receipt's shapes.

        A counterexample carries the shrunk falsifying input Hypothesis found,
        which is the thing an engineer has to reproduce, so a counterexample
        keeps its trace untouched while a satisfied case gains the scope.
        """
        satisfied = [
            {
                "kind": "case_satisfied",
                "obligation": obligation_to_json(obligation),
                "observation": observation,
            }
            for obligation, observation in self.observations
        ]
        counterexamples = [
            {
                "obligation": obligation_to_json(obligation),
                "trace": trace,
            }
            for obligation, trace in self.reading.counterexamples
        ]
        return satisfied, counterexamples

    def assumptions(self) -> list[str]:
        """The named facts a reader has to believe for this receipt to mean what it says.

        Three lines, and each one is a fact about what was sampled that the
        verdict alone does not carry: the settings in force, where a failing
        sequence is retained, and the scope the evidence covers.
        """
        lines = [_assumption(self.generator)]
        if self.replay is None:
            lines.append(
                "no replay block was declared, so a failing sequence is not "
                "retained anywhere this receipt names and reproducing it means "
                "re-running the generator"
            )
        else:
            seed = (
                "chosen by the generator and not pinned"
                if self.replay.seed is None
                else f"seeded at {self.replay.seed}"
            )
            lines.append(
                f"replay: failing sequences are retained in {self.replay.database}, "
                f"{seed}"
            )
        lines.append(self.scope.sentence())
        return lines


def interpret(raw: bytes, check: PropertyCheck) -> PropertyAdapterResult | Blocked:
    """What the run established, or why it could not be established.

    The refusals are `pytest_adapter`'s, unchanged and for the same reasons: the
    report this reads is a pytest report, so a missing document, a truncated
    session, an empty collection and a required test that never ran mean exactly
    what they mean there, and a second vocabulary for them would be a second
    dialect of one language.

    One refusal is added, and it is the one the category depends on. A `property`
    check whose required tests all passed and reported no generator settings ran
    no generator this build can read, so the family it sampled is unknown, and a
    green result under this category says "no disagreement was found over a
    sampled family". Without the family that sentence has nothing behind it.

    It applies to a pass and not to a failure. A counterexample is a concrete
    input the runner produced, so a FAIL states its own disagreement without a
    family, and `TestedScope` says in words that none was in force. Measured on
    this path: an earlier version refused both, and the refusal named
    `generator_absent` for a test that had just failed an assertion outright,
    which replaced a counterexample a reader could reproduce with a BLOCKED they
    could not.
    """
    if check.generator is None:
        # Unreachable through the manifest, whose `property` branch requires the
        # block. Guarded here because the dataclass permits it, and a property
        # result whose settings were not pinned says nothing about what was
        # sampled, which is the whole difference between this category and the
        # one below it.
        return Blocked(
            BlockedReason.ARTIFACT_MALFORMED,
            "generator_absent: a property check declares no generator settings, so "
            "this run cannot say what family it sampled and cannot be property "
            "evidence",
        )
    reading = pytest_adapter.interpret(raw, check)
    if isinstance(reading, Blocked):
        return reading

    reported = reading.generator_settings or {}
    measured_settings = [
        _measured(reported[test_id]) for test_id in check.required_tests
        if test_id in reported
    ]
    # The bound recorded is the smallest family any required test ran under. A
    # receipt that quoted the largest would describe the least-supported case in
    # the run, and the family is a ceiling on the evidence: the tightest ceiling
    # is the one the evidence actually has.
    measured = min(
        measured_settings, key=lambda settings: settings.max_examples,
    ) if measured_settings else None

    # A PASS with no generator settings is refused, and a disagreement is not.
    # The settings are what license a claim about a sampled family, so their
    # absence makes a PASS unstateable: a green result under a `property` check
    # would otherwise read as "no disagreement was found over a family" with no
    # family behind it. A FAIL is different, because its counterexample is a
    # concrete input the runner produced and shrinking, and the scope sentence
    # beside it says plainly that none was in force. Measured on this path: an
    # earlier version refused both, and the refusal reported
    # `generator_absent` for a test that had just failed an assertion outright,
    # which turns a FAIL a reader could act on into a BLOCKED they could not.
    if reading.is_pass and len(measured_settings) != len(check.required_tests):
        unmeasured = [
            test_id for test_id in check.required_tests if test_id not in reported
        ]
        return Blocked(
            BlockedReason.SCENARIO_UNKNOWN,
            "generator_absent: required case(s) reported no generator settings: "
            f"{', '.join(unmeasured)}. The check declared a pinned generator family "
            "and the run reported none, so a sampled family cannot be claimed for a "
            "run that passed",
        )
    return PropertyAdapterResult(
        reading=reading,
        generator=measured or check.generator,
        replay=check.replay,
        scope=TestedScope(
            None if measured is None else measured.max_examples,
            0 if measured is None else measured.stateful_step_count,
        ),
    )


# -------------------------------------------------- the pytest plugin side
#
# Everything below runs inside the pinned runner's process, loaded by the argv
# `dispatch` builds. It activates the generator settings a property check pinned
# and decides nothing about any result.
#
# **The settings travel on the command line, not through the manifest.** An
# earlier version read them back out of the run's own manifest, and that cannot
# work. The check runs inside `vkit.integration.launcher`, which sets
# `VKIT_TRUSTED_LAUNCHER` so that the code under test's `import vkit` is refused
# at import time. The plugin lives in that same process, so its own import of
# `vkit.manifest` is refused too, the pinned profile never loaded, and the run
# silently used Hypothesis's own default of 100 examples while the receipt
# described the family the manifest had pinned. Measured: a `property` check
# pinning `max_examples: 25` reported 100 through the launcher, and the same
# check run outside it reported 25. The two numbers came from the same test.
#
# So the settings cross as one JSON argument vkit itself built, and the plugin's
# only job is to apply what it is handed. That is a narrower channel than
# re-reading the manifest and it is the one channel that survives the launcher's
# import boundary.

#: The profile name the pinned settings are registered under. A name of its own
#: rather than "default" so a test that loads the default profile afterwards is
#: visibly doing something this module did not, rather than silently overwriting
#: what the check pinned.
VKIT_PROFILE = "vkit-pinned"

#: The flag carrying the pinned settings into the child. A flag rather than an
#: environment variable because the launcher restores its environment afterwards,
#: and a flag rather than a positional because pytest has no position left to
#: give a check's argv.
SETTINGS_FLAG = "--vkit-hypothesis-settings"


def settings_token(check: PropertyCheck) -> str:
    """The `--vkit-hypothesis-settings` value for one check, as JSON.

    Built from the declared variant, so the document is vkit's projection of an
    approved declaration and never something the child wrote. Read back with the
    refusals below rather than trusted, because a value that crossed a process
    boundary is untrusted whatever produced it.
    """
    generator = check.generator
    document: dict = {
        "max_examples": generator.max_examples,
        "deadline": generator.deadline,
        "suppress_health_check": list(generator.suppress_health_check),
    }
    if generator.stateful_step_count:
        # Omitted when zero, because Hypothesis rejects a stateful step count
        # below one. Measured on 6.168.3: `register_profile(name,
        # stateful_step_count=0)` raises `InvalidArgument`, and the raise lands in
        # `pytest_configure`, so pytest reports INTERNAL_ERROR and the whole run
        # is refused. The manifest schema permits 0 because "this check declares
        # no stateful testing" is the honest thing to write for a check whose
        # tests are not state machines, so zero means "leave the default alone".
        document["stateful_step_count"] = generator.stateful_step_count
    if check.replay is not None:
        document["replay_database"] = check.replay.database
        document["replay_seed"] = check.replay.seed
    return json.dumps(document, sort_keys=True)


class SettingsRefused(Exception):
    """The pinned generator settings could not be applied, and why.

    A raise rather than a refusal value because `pytest_configure` has no return
    channel and that process does not decide anything. Raised rather than
    swallowed because a property run that quietly fell back to the library
    default is exactly the overstatement this category forbids: it would report a
    sampled family the owner never approved. Measured: the raise becomes pytest's
    INTERNAL_ERROR and exit 3, which `execution` already treats as a run the
    runner could not finish rather than as a verdict.
    """


def pytest_addoption(parser: Any) -> None:  # noqa: D103 - a pytest plugin hook
    """Teach the runner the one flag vkit needs it to accept."""
    group = parser.getgroup("vkit", "vkit evidence contract")
    group.addoption(
        SETTINGS_FLAG, action="store", default=None, metavar="JSON",
        help="the pinned Hypothesis generator settings for this run",
    )


def pytest_configure(config: Any) -> None:  # noqa: D103 - a pytest plugin hook
    """Activate the generator settings the run was launched with.

    Applied here rather than at import because the settings arrive on the command
    line and the profile has to be in force before any test module is collected.
    Measured: `@given` captures the settings in force at decoration time, which
    happens during collection, so a profile loaded after collection would never
    reach the test.
    """
    from hypothesis import HealthCheck, settings

    raw = config.getoption(SETTINGS_FLAG, None)
    if not raw:
        return
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SettingsRefused(
            f"the pinned generator settings are not JSON: {exc}"
        ) from None
    if not isinstance(document, dict) or not isinstance(
        document.get("max_examples"), int
    ):
        raise SettingsRefused(
            "the pinned generator settings do not name a whole number of "
            f"examples, which is the one field a property run cannot do without. "
            f"Got {document!r}"
        )
    deadline = document.get("deadline")
    pinned: dict = {
        "max_examples": document["max_examples"],
        "deadline": None if deadline is None else timedelta(seconds=float(deadline)),
        # A name that is not a health check yields nothing rather than raising,
        # because the manifest schema checks that each is a string and does not
        # check that Hypothesis recognises it. A typo must not turn a property run
        # into a crash, and the settings that do apply are still recorded.
        "suppress_health_check": tuple(
            getattr(HealthCheck, name)
            for name in document.get("suppress_health_check", ())
            if hasattr(HealthCheck, name)
        ),
    }
    steps = document.get("stateful_step_count")
    if steps:
        pinned["stateful_step_count"] = int(steps)
    settings.register_profile(VKIT_PROFILE, **pinned)
    settings.load_profile(VKIT_PROFILE)