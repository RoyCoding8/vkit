"""The feature map: what a user can do, how they reach it, and which checks cover it.

`verification/features.json`, schema_version 2. It holds no expected results; those live in the checks.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

FEATURES_RELATIVE = Path("verification") / "features.json"
SCHEMA_VERSION = 2
_FIELDS = {"id", "behavior", "how_to_reach", "entry_points", "covered_by", "gaps"}


class FeatureError(Exception):
    """The feature map cannot be read."""


@dataclass(frozen=True)
class Feature:
    id: str
    behavior: str
    how_to_reach: tuple[str, ...]
    entry_points: tuple[str, ...]
    covered_by: tuple[str, ...]
    gaps: tuple[str, ...]

    def audit(self, root: Path, registered: Iterable[str]) -> list[str]:
        known = set(registered)
        problems = [f"entry point {p!r} does not exist" for p in self.entry_points if not (root / p).exists()]
        problems += [f"check {c!r} is not registered" for c in self.covered_by if c not in known]
        if not self.covered_by:
            problems.append("no check covers this feature")
        return problems

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "behavior": self.behavior, "how_to_reach": list(self.how_to_reach),
                "entry_points": list(self.entry_points), "covered_by": list(self.covered_by), "gaps": list(self.gaps)}


def _strings(entry: dict, key: str, path: Path) -> tuple[str, ...]:
    value = entry.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise FeatureError(f"{path}: feature {entry.get('id')!r} field {key!r} must be a list of nonempty strings")
    return tuple(value)


def load_features(root: Path) -> list[Feature]:
    path = root / FEATURES_RELATIVE
    if not path.is_file():
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FeatureError(f"{path} is not readable JSON: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise FeatureError(f"{path} must be an object with schema_version {SCHEMA_VERSION}")
    features, seen = [], set()
    for entry in document.get("features", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
            raise FeatureError(f"{path}: every feature needs a nonempty string id")
        unknown = sorted(set(entry) - _FIELDS)
        if unknown:
            raise FeatureError(f"{path}: feature {entry['id']!r} has unknown field(s) {', '.join(unknown)}")
        if entry["id"] in seen:
            raise FeatureError(f"{path}: duplicate feature id {entry['id']!r}")
        seen.add(entry["id"])
        if not isinstance(entry.get("behavior"), str) or not entry["behavior"]:
            raise FeatureError(f"{path}: feature {entry['id']!r} needs a behavior")
        features.append(Feature(entry["id"], entry["behavior"], _strings(entry, "how_to_reach", path),
                                _strings(entry, "entry_points", path), _strings(entry, "covered_by", path),
                                _strings(entry, "gaps", path)))
    return features
