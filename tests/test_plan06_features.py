"""Feature documents and conservative affected-check selection.

The invariant here is negative, so it is tested negatively. A test that asserts
a selection "is not empty" is weaker than a test that asserts a specific
unmapped change selects the whole suite *and* records a gap, because the first
passes for a selection that is accidentally non-empty for the wrong reason.

The cases that matter:

* An unmapped file broadens. It never produces an empty passing selection, which
  is the failure mode Plan 06 calls out by name.
* A driver or policy change forces every check to rerun. Editing the manifest
  changes what the checks *are*, so a file-list argument that it affected
  nothing would be exactly backwards.
* A feature naming a check that does not exist is uncovered, and a feature
  that declares a coverage gap is never counted as verified even when all its
  checks pass. A plan that lists a check id has not thereby verified anything.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

from vkit.features import (
    BROADER,
    EXACT,
    FORCED,
    Feature,
    FeatureError,
    FeatureMap,
    Mapping,
    load_features,
    select_checks,
    write_features,
)

ALL_CHECKS = ["totals", "split-bill", "items-api"]


def _mapping(check_id: str, *files: str) -> Mapping:
    return Mapping(check_id=check_id, files=files)


def test_an_explicit_mapping_may_narrow_the_selection() -> None:
    """The only path that reduces a run is a person saying so."""
    selection = select_checks(
        ["src/totals.py"],
        available=ALL_CHECKS,
        mappings=[_mapping("totals", "src/totals.py")],
    )

    assert selection.checks == ("totals",)
    assert selection.reasons["totals"].startswith(EXACT)
    assert not selection.gaps, f"a mapped change reported gaps: {selection.gaps}"


def test_an_unmapped_file_never_produces_an_empty_passing_selection() -> None:
    """The rule Plan 06 states by name, asserted on the output.

    The file is in a directory nothing is mapped to, which is the case where
    narrowing by directory would return nothing. The answer is the whole suite
    plus a gap, and the gap says the change was unmapped.
    """
    selection = select_checks(
        ["docs/notes.md"],
        available=ALL_CHECKS,
        mappings=[_mapping("totals", "src/totals.py")],
    )

    assert selection.empty is False
    assert set(selection.checks) == set(ALL_CHECKS), (
        f"an unmapped file selected {selection.checks} rather than broadening"
    )
    assert selection.gaps, "broadening happened without recording why"
    assert any("docs/notes.md" in gap for gap in selection.gaps)
    assert all(reason.startswith(BROADER) for reason in selection.reasons.values())


def test_a_file_outside_every_glob_but_sharing_a_directory_selects_those_checks() -> None:
    """The directory is the last honest narrowing, and it still records a gap.

    `src/notes.md` matches no glob, so it is unmapped. Its directory is `src`,
    which the mapping covers, so the checks reachable from `src` rerun. The
    narrower answer is right here and the gap still says the specific file was
    never mapped, because a glob covering a directory is not a claim about
    every file in it.
    """
    selection = select_checks(
        ["src/notes.md"],
        available=["totals"],
        mappings=[_mapping("totals", "src/*.py")],
    )

    assert selection.checks == ("totals",)
    assert selection.reasons["totals"].startswith(BROADER)
    assert any("notes.md" in gap and "not mapped" in gap for gap in selection.gaps)


def test_a_driver_change_forces_every_check_to_rerun() -> None:
    """Editing the manifest changes what the checks are.

    A change to the file that defines the checks cannot be argued away by a file
    list. The selection here is every registered check, and the reason says so,
    because a reader needs to know the run was forced rather than chosen.
    """
    selection = select_checks(
        ["verification/manifest.json"],
        available=ALL_CHECKS,
        mappings=[_mapping("totals", "src/totals.py")],
    )

    assert set(selection.checks) == set(ALL_CHECKS)
    assert all(reason.startswith(FORCED) for reason in selection.reasons.values())
    assert "verification/manifest.json" in " ".join(selection.reasons.values())


def test_a_driver_file_change_forces_every_check_to_rerun() -> None:
    """The same rule for a driver the caller declares, not just the manifest.

    A driver defines what a check observes, so editing it invalidates the
    evidence of every check that uses it. This is Plan 06's "a driver change
    forces live re-exercise of its path".
    """
    selection = select_checks(
        ["verify_totals.py"],
        available=ALL_CHECKS,
        mappings=[_mapping("totals", "src/totals.py")],
        drivers=["verify_*.py"],
    )

    assert set(selection.checks) == set(ALL_CHECKS)
    assert all(reason.startswith(FORCED) for reason in selection.reasons.values())


def test_a_driver_change_beats_an_explicit_mapping() -> None:
    """A mapping that would narrow a driver change is overridden.

    Someone who mapped `verify_totals.py` to `totals` did not mean "and ignore
    changes to the thing that runs it". The forced rule runs first, so the
    mapping cannot quietly exempt a file that decides what a check observes.
    """
    selection = select_checks(
        ["verify_totals.py", "src/totals.py"],
        available=["totals", "items-api"],
        mappings=[_mapping("totals", "src/totals.py", "verify_totals.py")],
        drivers=["verify_*.py"],
    )

    assert set(selection.checks) == {"totals", "items-api"}
    assert all(reason.startswith(FORCED) for reason in selection.reasons.values())


def test_no_registered_checks_is_a_gap_not_a_pass() -> None:
    """A repository with no checks reports a gap for every change.

    The empty selection here is unavoidable, and the point is that it is
    reported as a gap with `complete: False` rather than as a success.
    """
    selection = select_checks(["src/anything.py"], available=[], mappings=[])

    assert selection.empty is True
    assert selection.gaps
    assert selection.to_json()["complete"] is False
    assert "NOTHING was selected" in selection.render()


def test_a_mapping_to_an_unregistered_check_is_reported_as_a_gap() -> None:
    """A stale mapping is a gap in the manifest, not a silent skip."""
    selection = select_checks(
        ["src/gone.py"],
        available=["totals"],
        mappings=[_mapping("deleted-check", "src/gone.py")],
    )

    assert any("deleted-check" in gap and "not registered" in gap for gap in selection.gaps)
    assert selection.empty is True, "a stale mapping selected something it cannot run"


def test_a_feature_is_uncovered_when_its_check_is_not_registered() -> None:
    """Listing a check id is not the same as having the check.

    A feature map is a plan. This is the case where a plan and a manifest
    disagree, and the disagreement has to be visible as uncovered.
    """
    features = FeatureMap(
        project_root=Path("/repo"),
        features=[
            Feature(id="totals", behavior="sums amounts", entry_point="src/totals.py",
                    setup=(), covered_by=("totals",)),
            Feature(id="ghost", behavior="a feature nobody registered a check for",
                    entry_point="src/ghost.py", setup=(), covered_by=("ghost-check",)),
        ],
    )

    verified = features.verified_features(["totals"])
    uncovered = {row["feature"]: row["reason"] for row in features.uncovered(["totals"])}

    assert verified == ["totals"]
    assert "ghost" in uncovered
    assert "not registered" in uncovered["ghost"]


def test_a_feature_with_a_declared_gap_is_never_counted_verified() -> None:
    """A known gap disqualifies the feature even when its checks all exist.

    This is the rule that keeps a plan honest: naming the checks is easy, and
    the feature is only verified when nothing is known to be missing.
    """
    features = FeatureMap(
        project_root=Path("/repo"),
        features=[
            Feature(id="partial", behavior="does most of it", entry_point="src/x.py",
                    setup=(), covered_by=("totals",), coverage_gaps=("error paths are not exercised",)),
        ],
    )

    assert features.verified_features(["totals"]) == []
    rows = features.uncovered(["totals"])
    assert rows and "error paths" in rows[0]["reason"]


def test_a_feature_document_round_trips_through_disk(tmp_path: Path) -> None:
    """What a person writes is what the map reads back.

    The assertion is on the values, not on a successful round trip, because a
    round trip that silently dropped `coverage_gaps` would parse fine and lose
    the only thing that keeps a feature from counting as verified.
    """
    original = FeatureMap(
        project_root=tmp_path,
        features=[
            Feature(id="items", behavior="creates an item and returns the updated list",
                    entry_point="src/server.js", setup=("node src/server.js",),
                    covered_by=("items-api",), coverage_gaps=("no test for a duplicate name",)),
        ],
        notes=["written by hand"],
    )
    write_features(original)

    reloaded = load_features(tmp_path)

    assert [f.id for f in reloaded.features] == ["items"]
    feature = reloaded.by_id("items")
    assert feature.setup == ("node src/server.js",)
    assert feature.covered_by == ("items-api",)
    assert feature.coverage_gaps == ("no test for a duplicate name",)
    assert reloaded.verified_features(["items-api"]) == []
    assert reloaded.notes == ["written by hand"]


def test_a_malformed_feature_map_is_refused_rather_than_half_read(tmp_path: Path) -> None:
    """A feature with no entry point cannot be verified by anything.

    Half a feature is worse than none: it would appear in the map and be
    reported as uncovered for a reason that has nothing to do with coverage.
    """
    (tmp_path / "verification").mkdir()
    (tmp_path / "verification" / "features.json").write_text(
        json.dumps({"schema_version": 1, "features": [{"id": "x", "behavior": "does a thing"}]}),
        encoding="utf-8",
    )

    with pytest.raises(FeatureError, match="entry_point"):
        load_features(tmp_path)


def test_an_absent_feature_map_claims_nothing() -> None:
    """No map is a real state, and it reports zero verified rather than guessing."""
    features = load_features(Path("/nonexistent"))

    assert features.features == []
    assert features.verified_features(["totals"]) == []
    assert features.uncovered(["totals"]) == []
    assert "nothing is claimed to be covered" in features.notes[0]


def test_duplicate_feature_ids_are_refused(tmp_path: Path) -> None:
    """Two features with one id would make coverage ambiguous."""
    (tmp_path / "verification").mkdir()
    (tmp_path / "verification" / "features.json").write_text(
        json.dumps({"schema_version": 1, "features": [
            {"id": "a", "behavior": "b", "entry_point": "src/a.py"},
            {"id": "a", "behavior": "c", "entry_point": "src/b.py"},
        ]}),
        encoding="utf-8",
    )

    with pytest.raises(FeatureError, match="duplicate"):
        load_features(tmp_path)
