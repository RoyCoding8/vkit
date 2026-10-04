"""Scenario boundaries and discovery truncation behavior."""
from __future__ import annotations

import json

from vkit import discover, schemas
from vkit.outcome import BlockedReason


def test_artifact_schema_accepts_an_empty_scenario_list() -> None:
    raw = json.dumps({"schema_version": 1, "scenarios": []}).encode("utf-8")

    document = schemas.parse_artifact(raw)
    schemas.validate("check artifact", schemas.CHECK_ARTIFACT, document)

    assert document["scenarios"] == [], "the boundary must still accept an empty list"


def test_execution_turns_an_empty_scenario_list_into_artifact_empty() -> None:
    """The reason `outcome.py` now points at instead of the parse boundary."""
    from vkit.execution import _scenarios_from_artifact

    raw = json.dumps({"schema_version": 1, "scenarios": []}).encode("utf-8")

    scenarios, problem = _scenarios_from_artifact(raw, required=("checkout",))

    assert scenarios == ()
    assert problem is not None
    assert problem.reason is BlockedReason.ARTIFACT_EMPTY


def _command(i: int, kind: str) -> discover.DiscoveredCommand:
    return discover.DiscoveredCommand(
        id=f"{kind}:{i}",
        kind=kind,
        argv=("node", "--test"),
        summary="a test",
        provenance=discover.Provenance(file="package.json", line=i + 1),
        ambiguity=(),
        prerequisites=(),
        runner="npm",
    )


def _bounded(kinds: dict[str, int]) -> discover.Inspection:
    commands = [
        _command(i, kind) for kind, count in kinds.items() for i in range(count)
    ]
    inspection = discover.Inspection(project=".", ecosystem=["npm"])
    inspection.commands = commands
    discover._bound(inspection)
    return inspection


def test_bound_does_not_say_test_commands_are_never_dropped_when_it_drops_one(
    tmp_path,
) -> None:
    """The bound's gap string once claimed the opposite of what it does.

    Sixty test commands exceed `MAX_PER_CATEGORY`, so the bound drops twenty of
    them. The report has to say so rather than print the reassuring sentence.
    """
    inspection = _bounded({"test": 60})

    assert len(inspection.commands) == discover.MAX_PER_CATEGORY
    assert len(inspection.commands) < 60
    gaps = " ".join(inspection.gaps)
    assert "never dropped by this bound" not in gaps
    assert "no room for them" in gaps


def test_bound_keeps_the_claim_when_nothing_important_was_dropped() -> None:
    """The same sentence is honest in the case the existing test covers.

    Two hundred build helpers overflow the bound, but the test script and the
    postinstall hook both survive, so every kind a reader came for is present.
    """
    inspection = _bounded({"build": 200, "test": 1, "install": 1})

    kinds = {c.kind for c in inspection.commands}
    assert "test" in kinds
    assert "install" in kinds
    gaps = " ".join(inspection.gaps)
    assert "never dropped by this bound" in gaps
