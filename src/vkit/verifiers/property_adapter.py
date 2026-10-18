"""Reads an approved Hypothesis run through the pytest report and records the sampled family and
its bound, never that the property holds. The evidence category comes from the manifest's
`property` kind (`spec.evidence_kind`), not from Hypothesis being importable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..outcome import Blocked, BlockedReason
from . import pytest_adapter
from .obligation import CaseObligation, case_results, scenario_results
from .spec import HypothesisSettings, PropertyCheck

def _measured(document: dict) -> HypothesisSettings:
    """The generator settings a run reported; read from the report, not the manifest, because a
    decorator or profile can move them.
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
    """What was sampled. `family` is the ceiling on examples (Hypothesis may stop early), and None
    when no generator settings were in force.
    """

    family: int | None
    stateful_step_count: int = 0

    def sentence(self) -> str:
        """The scope sentence shown with a PASS; it states that the family is sampled, not
        exhausted.
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
    """What one Hypothesis run established: the wrapped pytest reading and the scope it covers."""

    reading: pytest_adapter.AdapterResult
    scope: TestedScope

    @property
    def counterexamples(self) -> tuple:
        """The cases that disagreed, lifted from the wrapped reading."""
        return self.reading.counterexamples

    @property
    def observations(self) -> tuple[tuple[CaseObligation, str], ...]:
        """Each satisfied case with the scope sentence attached, so a property PASS is never read
        as a bare scenario PASS.
        """
        scope = self.scope.sentence()
        return tuple(
            (obligation, f"{observation}. Tested scope: {scope}")
            for obligation, observation in self.reading.observations
        )

    def scenarios(self) -> tuple:
        """The reading as outcome `ScenarioResult`s."""
        return scenario_results(self.observations, self.counterexamples)

    def obligation_results(self) -> tuple[list[dict], list[dict]]:
        """Satisfied cases (with scope) and counterexamples as run-record dicts."""
        return case_results(self.observations, self.counterexamples)


def interpret(raw: bytes, check: PropertyCheck) -> PropertyAdapterResult | Blocked:
    """What the run established, or why it could not.

    Refusals are the pytest adapter's, plus `generator_absent` when a passing run reported no
    generator settings: without them the sampled family is unknown. A failure needs no family.
    """
    if check.generator is None:
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
    measured = min(
        measured_settings, key=lambda settings: settings.max_examples,
    ) if measured_settings else None

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
        scope=TestedScope(
            None if measured is None else measured.max_examples,
            0 if measured is None else measured.stateful_step_count,
        ),
    )


VKIT_PROFILE = "vkit-pinned"

SETTINGS_FLAG = "--vkit-hypothesis-settings"


def settings_token(check: PropertyCheck) -> str:
    """The `--vkit-hypothesis-settings` JSON for one check, projected from the approved
    declaration.
    """
    generator = check.generator
    document: dict = {
        "max_examples": generator.max_examples,
        "deadline": generator.deadline,
        "suppress_health_check": list(generator.suppress_health_check),
    }
    if generator.stateful_step_count:
        document["stateful_step_count"] = generator.stateful_step_count
    if check.replay is not None:
        document["replay_database"] = check.replay.database
        document["replay_seed"] = check.replay.seed
    return json.dumps(document, sort_keys=True)


class SettingsRefused(Exception):
    """The pinned generator settings could not be applied. Raised, never swallowed: falling back to
    library defaults would report a family nobody approved.
    """


def pytest_addoption(parser: Any) -> None:  # noqa: D103 - a pytest plugin hook
    """Register the settings flag."""
    group = parser.getgroup("vkit", "vkit evidence contract")
    group.addoption(
        SETTINGS_FLAG, action="store", default=None, metavar="JSON",
        help="the pinned Hypothesis generator settings for this run",
    )


def pytest_configure(config: Any) -> None:  # noqa: D103 - a pytest plugin hook
    """Activate the pinned generator profile before collection, since `@given` captures settings at
    decoration time.
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