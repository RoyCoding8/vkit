"""Feature documents, and the conservative choice of what a change must re-run.

## What a feature document is

A feature document states what a user can observe, where they observe it, how
the fixture is prepared, which checks exercise it, and what is *not* covered.
It does not contain assertions. An assertion in a feature document is a second
place for the expected result to live, and it is the place a behaviour
regression would edit first. The expected outcome belongs in the check
implementation, where running the application produces it.

## Why selection is conservative

The rule Plan 06 states is that an unmapped change must select a broader suite
or report a gap, never an empty passing selection. That is not a heuristic here,
it is a shape. `Selection` cannot represent "nothing to run" as a success: a
selection with no checks is only constructible through `broader`, which attaches
the reason and the gap. A caller that changes nothing and has no mapping gets a
refusal, not a green tick.

The reason is that an empty selection passes trivially. Any pipeline that
selects zero checks and reports success has reported the absence of evidence as
the presence of it, which is the specific failure this whole product exists to
prevent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

FEATURES_RELATIVE = Path("verification") / "features.json"
SCHEMA_VERSION = 1

EXACT = "exact"
BROADER = "broader"
FORCED = "forced"


class FeatureError(Exception):
    """A feature document, or a selection request, cannot be honoured as asked."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()



@dataclass(frozen=True)
class Feature:
    """One user-visible behaviour, and what is and is not known about it.

    `covered_by` names check ids, never commands. A feature that named a
    command would be a second way to execute something, and docs/verification.md is
    explicit that agent-facing APIs accept registered ids.
    """

    id: str
    behavior: str
    entry_point: str
    setup: tuple[str, ...]
    covered_by: tuple[str, ...]
    coverage_gaps: tuple[str, ...] = ()
    expected_outcome: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "behavior": self.behavior,
            "entry_point": self.entry_point,
            "setup": list(self.setup),
            "covered_by": list(self.covered_by),
            "expected_outcome": self.expected_outcome,
            "coverage_gaps": list(self.coverage_gaps),
        }


@dataclass
class FeatureMap:
    """Every feature this repository claims, and how much of it is verified.

    `verified` is computed, never declared. A feature is verified only when
    every check it names exists in the manifest, and a feature with a coverage
    gap is never verified even then. That is the rule that stops a plan from
    counting a feature as covered because someone listed its id.
    """

    project_root: Path
    features: list[Feature] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def by_id(self, feature_id: str) -> Feature:
        for feature in self.features:
            if feature.id == feature_id:
                return feature
        known = ", ".join(f.id for f in self.features) or "<none>"
        raise FeatureError(f"unknown feature {feature_id!r}; the map declares: {known}")

    def verified_features(self, known_checks: Iterable[str]) -> list[str]:
        """Features whose every check exists and which declare no gap.

        The empty intersection is what makes this honest: a feature naming a
        check the manifest does not define is uncovered, not pending.
        """
        known = set(known_checks)
        return [
            f.id for f in self.features
            if f.covered_by and not f.coverage_gaps and set(f.covered_by) <= known
        ]

    def uncovered(self, known_checks: Iterable[str]) -> list[dict[str, str]]:
        """Every feature that is not verified, and why.

        Returned rather than counted, because a count tells a reader that
        something is missing and this tells them what.
        """
        known = set(known_checks)
        rows = []
        for feature in self.features:
            missing = [c for c in feature.covered_by if c not in known]
            reasons = list(feature.coverage_gaps)
            if not feature.covered_by:
                reasons.append("no check is registered for this feature")
            for check in missing:
                reasons.append(f"check {check!r} is named but not registered in the manifest")
            if reasons:
                rows.append({"feature": feature.id, "reason": "; ".join(reasons)})
        return rows

    def to_json(self, known_checks: Iterable[str] = ()) -> dict[str, Any]:
        known = list(known_checks)
        return {
            "schema_version": SCHEMA_VERSION,
            "command": "features",
            "project": str(self.project_root),
            "features": [f.to_json() for f in self.features],
            "verified": self.verified_features(known) if known else [],
            "uncovered": self.uncovered(known),
            "notes": list(self.notes),
            "path": str(self.project_root / FEATURES_RELATIVE),
        }

    def render(self, known_checks: Iterable[str] = ()) -> str:
        known = list(known_checks)
        verified = set(self.verified_features(known)) if known else set()
        lines = [f"project : {self.project_root}", f"features : {len(self.features)}"]
        for feature in self.features:
            mark = "verified  " if feature.id in verified else "UNCOVERED"
            lines.append(f"  [{mark}] {feature.id}")
            lines.append(f"    behavior : {feature.behavior}")
            lines.append(f"    entry    : {feature.entry_point}")
            lines.append(f"    checks   : {', '.join(feature.covered_by) or '<none>'}")
            for gap in feature.coverage_gaps:
                lines.append(f"    gap      : {gap}")
        if known:
            lines.append("")
            lines.append(f"verified: {len(verified)} of {len(self.features)}")
        return "\n".join(lines)


def features_path(project_root: Path) -> Path:
    return project_root / FEATURES_RELATIVE


def load_features(project_root: Path) -> FeatureMap:
    """Read the feature map, or return an empty one with a note.

    An absent map is a real state, not an error: a repository that has been
    enrolled but whose behaviours nobody has written down has no features, and
    saying so is the honest answer. A malformed map is refused, because a
    half-read feature list is worse than none.
    """
    path = features_path(project_root)
    if not path.is_file():
        return FeatureMap(
            project_root=project_root,
            notes=[
                f"no feature map at {path}; nothing is claimed to be covered. "
                "Write one when you know what the application is supposed to do"
            ],
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FeatureError(f"{path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise FeatureError(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise FeatureError(
            f"{path} must be an object with schema_version {SCHEMA_VERSION}"
        )
    raw_features = document.get("features")
    if not isinstance(raw_features, list):
        raise FeatureError(f"{path} must contain a 'features' list")

    features: list[Feature] = []
    seen: set[str] = set()
    for entry in raw_features:
        if not isinstance(entry, dict):
            raise FeatureError(f"{path}: every feature must be an object")
        missing = [k for k in ("id", "behavior", "entry_point") if not entry.get(k)]
        if missing:
            raise FeatureError(
                f"{path}: a feature is missing {', '.join(missing)}; a feature with no "
                "entry point cannot be verified by anything"
            )
        identifier = str(entry["id"])
        if identifier in seen:
            raise FeatureError(f"{path}: duplicate feature id {identifier!r}")
        seen.add(identifier)
        features.append(Feature(
            id=identifier,
            behavior=str(entry["behavior"]),
            entry_point=str(entry["entry_point"]),
            setup=tuple(entry.get("setup", ())),
            covered_by=tuple(entry.get("covered_by", ())),
            coverage_gaps=tuple(entry.get("coverage_gaps", ())),
            expected_outcome=str(entry.get("expected_outcome", "")),
        ))
    return FeatureMap(
        project_root=project_root,
        features=features,
        notes=list(document.get("notes", [])),
    )


def write_features(feature_map: FeatureMap) -> Path:
    path = features_path(feature_map.project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": SCHEMA_VERSION,
        "notes": list(feature_map.notes),
        "features": [f.to_json() for f in feature_map.features],
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path



@dataclass(frozen=True)
class Mapping:
    """A declared relationship between changed files and a check.

    Explicit and narrow on purpose. A mapping is a claim that changing exactly
    these paths can only affect this check, and it is the only thing that is
    allowed to narrow a run. Anything a person cannot state as confidently as
    that belongs in a glob, and a glob selects more, not less.
    """

    check_id: str
    files: tuple[str, ...]


def _matches(changed: str, pattern: str) -> bool:
    """Whether one changed path matches one pattern.

    `fnmatch` semantics, with the cases spelled out rather than left to the
    reader: a pattern naming a directory matches it and everything under it, a
    bare filename matches at any depth, and everything else is an exact-or-glob
    match. The bias in every case is toward matching, because a pattern that
    fails to match turns into a broader selection later, and a broader selection
    that turns into *nothing* is the failure this module exists to prevent.
    """
    import fnmatch

    changed = changed.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    if pattern.endswith("/"):
        pattern = pattern + "**"
    if fnmatch.fnmatch(changed, pattern):
        return True
    if fnmatch.fnmatch(changed, pattern.rstrip("/") + "/**"):
        return True
    if "/" not in pattern:
        return fnmatch.fnmatch(changed.rsplit("/", 1)[-1], pattern)
    return False


@dataclass(frozen=True)
class Selection:
    """The checks a change requires, and the reason each was chosen.

    `gaps` is the important field. A selection that had to widen because
    something was unmapped says so, and a caller that ignores `gaps` is still
    holding the record of what was not known.
    """

    checks: tuple[str, ...]
    reasons: dict[str, str]
    broader: tuple[str, ...]
    gaps: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.checks

    def to_json(self) -> dict[str, Any]:
        return {
            "checks": list(self.checks),
            "reasons": dict(self.reasons),
            "broader": list(self.broader),
            "gaps": list(self.gaps),
            "complete": not self.empty,
        }

    def render(self) -> str:
        lines = [f"selected : {len(self.checks)} check(s)"]
        for check in self.checks:
            reason = self.reasons.get(check, FORCED)
            lines.append(f"  {check:28} {reason}")
        for gap in self.gaps:
            lines.append(f"  gap: {gap}")
        if self.empty:
            lines.append(
                "NOTHING was selected. An empty selection is a gap, never a pass."
            )
        return "\n".join(lines)


FORCED_BY_CHANGE = (
    "verification/manifest.json",
    "verification/features.json",
    "pyproject.toml",
    "package.json",
    "Makefile",
)


def select_checks(
    changed: Sequence[str],
    *,
    available: Iterable[str],
    mappings: Sequence[Mapping],
    drivers: Sequence[str] = (),
    features: FeatureMap | None = None,
) -> Selection:
    """Choose what to re-run for a set of changed paths, and say why.

    Three rules, in order.

    **A shared driver or policy file reruns everything.** The manifest defines
    what the checks are, and a driver defines what a check observes. Editing
    either can change the meaning of any result, so a change to one of them
    selects every registered check. The alternative is trusting a file list to
    tell us that a change to the thing that *defines the checks* affected none.

    **An explicit mapping may narrow.** This is the only path that reduces a
    run, and it has to be a person saying so.

    **Anything unmapped broadens.** An unmapped change does not select nothing.
    It selects every check that shares a directory with the changed file, and
    records a gap naming exactly what was not covered. If nothing shares a
    directory, the selection is everything, because a change that maps to no
    known surface is exactly the case where guessing narrowly is most wrong.
    """
    known = list(dict.fromkeys(available))
    if not known:
        return Selection((), {}, (), (
            "the manifest registers no checks, so no change can be covered; "
            "this is a gap and not a passing selection"
        ))

    reasons: dict[str, str] = {}
    chosen: list[str] = []
    gaps: list[str] = []
    broader: list[str] = []

    def select(check_id: str, reason: str) -> None:
        if check_id not in known:
            gaps.append(f"check {check_id!r} is mapped but not registered in the manifest")
            return
        if check_id not in reasons:
            reasons[check_id] = reason
            chosen.append(check_id)

    forced = [p for p in changed if _is_forced(p, drivers)]
    if forced:
        for check_id in known:
            select(check_id, f"{FORCED}: {', '.join(forced)} defines the checks")
        return Selection(tuple(chosen), reasons, tuple(known), tuple(gaps))

    matched: set[str] = set()
    unmapped: list[str] = []
    for path in changed:
        hit = False
        for mapping in mappings:
            if any(_matches(path, pattern) for pattern in mapping.files):
                select(mapping.check_id, f"{EXACT}: {path} matches this check's declared files")
                matched.add(path)
                hit = True
        if not hit:
            unmapped.append(path)

    for path in unmapped:
        siblings = _sibling_checks(path, known, features)
        if siblings:
            for check_id in siblings:
                select(check_id, f"{BROADER}: {path} is unmapped, so its directory's checks rerun")
                broader.append(check_id)
            gaps.append(
                f"{path} is not mapped to any check; {len(siblings)} check(s) sharing its "
                "directory were selected instead, and any behaviour reachable only "
                "through this file is unverified"
            )
        else:
            for check_id in known:
                select(check_id, f"{BROADER}: {path} is unmapped and shares a directory with nothing")
                broader.append(check_id)
            gaps.append(
                f"{path} is not mapped to any check and shares a directory with none; "
                f"all {len(known)} registered checks were selected because narrowing "
                "here would be a guess"
            )

    if not chosen:
        gaps.append(
            "the change selected no checks; this is reported as a gap and must not be "
            "read as a passing selection"
        )
    return Selection(tuple(chosen), reasons, tuple(dict.fromkeys(broader)), tuple(gaps))


def _is_forced(path: str, drivers: Sequence[str]) -> bool:
    for driver in drivers:
        if _matches(path, driver):
            return True
    normalised = path.replace("\\", "/")
    return any(normalised == f or normalised.endswith("/" + f) for f in FORCED_BY_CHANGE)


def _sibling_checks(
    path: str, known: Sequence[str], features: FeatureMap | None
) -> list[str]:
    """Checks related to a changed file by directory, or by feature.

    Feature mapping is the useful half: a file under `src/` and a feature whose
    entry point is in `src/` are related even when no explicit mapping exists.
    When neither relation holds, the answer is empty, and the caller widens to
    everything rather than to nothing.
    """
    directory = path.replace("\\", "/").rsplit("/", 1)[0] if "/" in path.replace("\\", "/") else ""
    related = [c for c in known if directory and directory in c]
    if related or features is None:
        return related
    for feature in features.features:
        entry = feature.entry_point.replace("\\", "/")
        if directory and directory in entry:
            related.extend(c for c in feature.covered_by if c in known)
    return list(dict.fromkeys(related))
