"""Minimum-cardinality covering configurations for bounded finite factors."""
from __future__ import annotations

import itertools
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from ._data import _object_without_duplicates, _reject_constant, encode_json, read_json
from .cover import (
    BACKEND,
    BACKEND_VERSION,
    MAX_CANDIDATES,
    MAX_COVERAGE_PAIRS,
    MAX_REQUIRED,
    MAX_TIMEOUT_SECONDS,
    _worker as _cover_worker,
)

MAX_MODEL_BYTES = 512_000
MAX_OUTPUT_BYTES = 2_000_000
MAX_FACTORS = 12
MAX_ROWS = MAX_CANDIDATES
MAX_FORBIDDEN = 4_000
MODEL_SCOPE = (
    "Minimum-cardinality selection from the legal full configurations of the supplied "
    "version-1 finite-factor model, covering every strength-t projection that occurs "
    "in at least one legal configuration. It makes no claim about unlisted levels or factors."
)


def _bounded_label(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty) or "\0" in value:
        raise ValueError(f"{label} must be a string without NUL")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must contain Unicode scalar values") from exc
    if size > 256:
        raise ValueError(f"{label} must be at most 256 UTF-8 bytes")
    return value


def _parse_model(value: object) -> tuple[list[str], list[list[str]], int, list[tuple[tuple[int, int], ...]]]:
    if not isinstance(value, dict) or set(value) - {"version", "factors", "strength", "forbidden"}:
        raise ValueError("model must contain only version, factors, strength and forbidden")
    if type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("model version must be the integer 1")
    raw_factors = value.get("factors")
    if not isinstance(raw_factors, dict) or not 2 <= len(raw_factors) <= MAX_FACTORS:
        raise ValueError(f"factors must be an object with 2 to {MAX_FACTORS} factors")
    names: list[str] = []
    levels: list[list[str]] = []
    level_indexes: list[dict[str, int]] = []
    for raw_name, raw_levels in raw_factors.items():
        name = _bounded_label(raw_name, "factor name")
        if not isinstance(raw_levels, list) or not raw_levels:
            raise ValueError(f"factor {name!r} must have a non-empty level array")
        valid_levels = [_bounded_label(item, f"factor {name!r} level", allow_empty=True)
                        for item in raw_levels]
        if len(set(valid_levels)) != len(valid_levels):
            raise ValueError(f"factor {name!r} levels must be unique")
        names.append(name)
        levels.append(valid_levels)
        level_indexes.append({item: index for index, item in enumerate(valid_levels)})

    strength = value.get("strength", 2)
    if type(strength) is not int or not 1 <= strength <= min(3, len(names)):
        raise ValueError(f"strength must be an integer from 1 to {min(3, len(names))}")

    raw_forbidden = value.get("forbidden", [])
    if not isinstance(raw_forbidden, list) or len(raw_forbidden) > MAX_FORBIDDEN:
        raise ValueError(f"forbidden must be an array with at most {MAX_FORBIDDEN} assignments")
    name_indexes = {name: index for index, name in enumerate(names)}
    forbidden: list[tuple[tuple[int, int], ...]] = []
    seen_forbidden: set[tuple[tuple[int, int], ...]] = set()
    for assignment in raw_forbidden:
        if not isinstance(assignment, dict):
            raise ValueError("each forbidden assignment must be an object")
        pairs: list[tuple[int, int]] = []
        for raw_name, raw_level in assignment.items():
            name = _bounded_label(raw_name, "forbidden factor name")
            if name not in name_indexes:
                raise ValueError(f"forbidden assignment names unknown factor {name!r}")
            factor_index = name_indexes[name]
            level = _bounded_label(raw_level, f"forbidden level for {name!r}", allow_empty=True)
            if level not in level_indexes[factor_index]:
                raise ValueError(f"forbidden assignment uses unknown level {level!r} for {name!r}")
            pairs.append((factor_index, level_indexes[factor_index][level]))
        canonical = tuple(sorted(pairs))
        if canonical in seen_forbidden:
            raise ValueError("forbidden assignments must be unique")
        seen_forbidden.add(canonical)
        forbidden.append(canonical)
    return names, levels, strength, forbidden


def _generate(value: object, timeout_seconds: int) -> dict[str, Any]:
    names, levels, strength, forbidden = _parse_model(value)
    row_count = 1
    for factor_levels in levels:
        row_count *= len(factor_levels)
        if row_count > MAX_ROWS:
            return {"status": "UNKNOWN", "configurations": None, "count": None,
                    "backend_version": None,
                    "reason": f"Cartesian model exceeds the {MAX_ROWS}-row enumeration limit"}

    legal_rows: list[tuple[int, ...]] = []
    forbidden_by_factor = [
        (tuple(index for index, _ in assignment), tuple(level for _, level in assignment))
        for assignment in forbidden
    ]
    for row in itertools.product(*(range(len(factor_levels)) for factor_levels in levels)):
        if any(all(row[factor] == level for factor, level in zip(factors, values))
               for factors, values in forbidden_by_factor):
            continue
        legal_rows.append(row)
    if not legal_rows:
        return {"status": "INFEASIBLE", "configurations": None, "count": None,
                "backend_version": None, "reason": "no legal full configuration exists"}

    subsets = list(itertools.combinations(range(len(names)), strength))
    pair_count = len(legal_rows) * len(subsets)
    if pair_count > MAX_COVERAGE_PAIRS:
        return {"status": "UNKNOWN", "configurations": None, "count": None,
                "backend_version": None,
                "reason": f"coverage matrix exceeds the {MAX_COVERAGE_PAIRS}-pair limit"}

    row_projections: list[list[tuple[int, ...]]] = []
    required_projection_set: set[tuple[int, tuple[int, ...]]] = set()
    for row in legal_rows:
        projections = []
        for subset_index, subset in enumerate(subsets):
            projection = tuple(row[index] for index in subset)
            projections.append(projection)
            required_projection_set.add((subset_index, projection))
            if len(required_projection_set) > MAX_REQUIRED:
                return {"status": "UNKNOWN", "configurations": None, "count": None,
                        "backend_version": None,
                        "reason": f"feasible interactions exceed the {MAX_REQUIRED}-interaction limit"}
        row_projections.append(projections)

    ordered_required = sorted(required_projection_set)
    interaction_ids = {projection: f"i{index}" for index, projection in enumerate(ordered_required)}
    candidates = [
        {"id": f"r{index}", "cost": 1,
         "covers": [interaction_ids[(subset_index, projection)]
                    for subset_index, projection in enumerate(projections)]}
        for index, projections in enumerate(row_projections)
    ]
    cover_result = _cover_worker({
        "required": list(interaction_ids.values()),
        "candidates": candidates,
        "timeout_seconds": timeout_seconds,
    })
    status = cover_result.get("status")
    backend_version = cover_result.get("backend_version")
    if status == "UNAVAILABLE":
        return {"status": "UNAVAILABLE", "configurations": None, "count": None,
                "backend_version": backend_version, "reason": cover_result.get("reason")}
    if status == "UNKNOWN":
        return {"status": "UNKNOWN", "configurations": None, "count": None,
                "backend_version": backend_version, "reason": "CP-SAT did not prove an optimum"}
    if status not in ("OPTIMAL", "FEASIBLE") or backend_version != BACKEND_VERSION:
        return {"status": "UNAVAILABLE", "configurations": None, "count": None,
                "backend_version": backend_version,
                "reason": "CP-SAT worker returned an invalid status or backend version"}

    selected = cover_result.get("selected")
    reported_cost = cover_result.get("cost")
    candidate_indexes = {candidate["id"]: index for index, candidate in enumerate(candidates)}
    if (not isinstance(selected, list) or any(not isinstance(item, str) for item in selected)
            or len(set(selected)) != len(selected)
            or any(item not in candidate_indexes for item in selected)
            or type(reported_cost) is not int or reported_cost != len(selected)):
        return {"status": "UNAVAILABLE", "configurations": None, "count": None,
                "backend_version": backend_version,
                "reason": "CP-SAT worker returned an invalid selection or cardinality"}
    selected_rows = [legal_rows[candidate_indexes[item]] for item in selected]
    covered = {
        (subset_index, tuple(row[index] for index in subsets[subset_index]))
        for row in selected_rows
        for subset_index in range(len(subsets))
    }
    if covered != required_projection_set:
        return {"status": "UNAVAILABLE", "configurations": None, "count": None,
                "backend_version": backend_version,
                "reason": "CP-SAT worker selection did not cover every feasible interaction"}
    configurations = [
        {name: levels[index][row[index]] for index, name in enumerate(names)}
        for row in selected_rows
    ]
    return {"status": status, "configurations": configurations, "count": len(configurations),
            "backend_version": backend_version, "reason": None}


def _worker_main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict) or set(request) != {"model", "timeout_seconds"}:
            return 1
        timeout_seconds = request["timeout_seconds"]
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
            return 1
        result = _generate(request["model"], timeout_seconds)
        try:
            output = encode_json(result, max_bytes=MAX_OUTPUT_BYTES)
        except ValueError:
            output = encode_json({"status": "UNKNOWN", "configurations": None, "count": None,
                                  "backend_version": result["backend_version"],
                                  "reason": "configuration output exceeded its byte limit"},
                                 max_bytes=MAX_OUTPUT_BYTES)
        print(output)
        return 0
    except (IndexError, KeyError, TypeError, ValueError, RecursionError):
        return 1


def _result(path: str, digest: str, *, status: str, configurations: Any,
            count: Any, backend_version: Any, reason: Any) -> dict[str, Any]:
    return {
        "operation": "generate_configurations", "status": status, "path": path,
        "source_sha256": digest, "strength": None, "configurations": configurations,
        "count": count, "backend": BACKEND, "backend_version": backend_version,
        "reason": reason, "model_scope": MODEL_SCOPE,
    }


def generate_configurations(root: Path, path: str, timeout_seconds: int = 10) -> dict[str, Any]:
    """Generate a minimum-cardinality set covering all feasible strength-t interactions."""
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer from 1 to {MAX_TIMEOUT_SECONDS}")
    try:
        model, digest = read_json(root, path, max_bytes=MAX_MODEL_BYTES, reject_symlinks=True)
    except ValueError as exc:
        return _result(path, "", status="UNSUPPORTED", configurations=None, count=None,
                       backend_version=None, reason=str(exc))
    try:
        parsed_names, parsed_levels, strength, parsed_forbidden = _parse_model(model)
    except ValueError as exc:
        return _result(path, digest, status="UNSUPPORTED", configurations=None, count=None,
                       backend_version=None, reason=str(exc))
    try:
        request = json.dumps({"model": model, "timeout_seconds": timeout_seconds},
                             ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return _result(path, digest, status="UNSUPPORTED", configurations=None, count=None,
                       backend_version=None, reason="captured model cannot be encoded for the worker")
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-m", "vkit.operations.configurations", "--worker"],
            input=request, capture_output=True, encoding="utf-8", timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _result(path, digest, status="UNKNOWN", configurations=None, count=None,
                       backend_version=None,
                       reason=f"isolated enumeration and CP-SAT exceeded {timeout_seconds} seconds")
    except OSError as exc:
        return _result(path, digest, status="UNAVAILABLE", configurations=None, count=None,
                       backend_version=None, reason=f"cannot start isolated worker: {exc}")
    if completed.returncode != 0:
        return _result(path, digest, status="UNAVAILABLE", configurations=None, count=None,
                       backend_version=None, reason="isolated worker exited without a result")
    try:
        result = json.loads(completed.stdout, object_pairs_hook=_object_without_duplicates,
                            parse_constant=_reject_constant)
    except (ValueError, json.JSONDecodeError):
        return _result(path, digest, status="UNAVAILABLE", configurations=None, count=None,
                       backend_version=None, reason="isolated worker returned invalid JSON")
    if not isinstance(result, dict):
        return _result(path, digest, status="UNAVAILABLE", configurations=None, count=None,
                       backend_version=None, reason="isolated worker returned an invalid response")
    if (set(result) != {"status", "configurations", "count", "backend_version", "reason"}
            or (result["backend_version"] is not None
                and not isinstance(result["backend_version"], str))
            or (result["reason"] is not None and not isinstance(result["reason"], str))):
        return _result(path, digest, status="UNAVAILABLE", configurations=None, count=None,
                       backend_version=None, reason="isolated worker returned an invalid response shape")
    status = result.get("status")
    backend_version = result.get("backend_version")
    if status == "UNAVAILABLE":
        return _result(path, digest, status=status, configurations=None, count=None,
                       backend_version=backend_version, reason=result.get("reason"))
    if status not in ("OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN"):
        return _result(path, digest, status="UNAVAILABLE", configurations=None, count=None,
                       backend_version=backend_version, reason="isolated worker returned an invalid status")
    if status in ("OPTIMAL", "FEASIBLE"):
        configurations = result.get("configurations")
        count = result.get("count")
        if (backend_version != BACKEND_VERSION or not isinstance(configurations, list)
                or type(count) is not int or count != len(configurations)
                or result.get("reason") is not None):
            return _result(path, digest, status="UNAVAILABLE", configurations=None, count=None,
                           backend_version=backend_version,
                           reason="isolated worker returned an invalid configuration result")
    else:
        configurations = None
        count = None
        if result.get("configurations") is not None or result.get("count") is not None:
            return _result(path, digest, status="UNAVAILABLE", configurations=None, count=None,
                           backend_version=backend_version,
                           reason="isolated worker returned configurations for a non-solution status")
        if status == "INFEASIBLE" and backend_version is not None:
            return _result(path, digest, status="UNAVAILABLE", configurations=None, count=None,
                           backend_version=backend_version,
                           reason="isolated worker returned an unexpected backend version for infeasibility")
        if status == "UNKNOWN" and backend_version not in (None, BACKEND_VERSION):
            return _result(path, digest, status="UNAVAILABLE", configurations=None, count=None,
                           backend_version=backend_version,
                           reason="isolated worker returned an unexpected backend version")
    if status == "INFEASIBLE":
        row_count = 1
        for domain in parsed_levels:
            row_count *= len(domain)
        has_legal_row = row_count <= MAX_ROWS and any(
            not any(all(row[factor] == level for factor, level in assignment)
                    for assignment in parsed_forbidden)
            for row in itertools.product(*(range(len(domain)) for domain in parsed_levels))
        )
        if (has_legal_row or row_count > MAX_ROWS
                or result.get("reason") != "no legal full configuration exists"):
            return _result(path, digest, status="UNAVAILABLE", configurations=None, count=None,
                           backend_version=backend_version,
                           reason="isolated worker returned infeasibility for a model with a legal row")
    output = _result(path, digest, status=status, configurations=configurations, count=count,
                     backend_version=backend_version, reason=result.get("reason"))
    output["strength"] = strength
    if configurations is not None:
        if any(not isinstance(row, dict) or set(row) != set(parsed_names)
               or any(not isinstance(row[name], str) or row[name] not in parsed_levels[index]
                      for index, name in enumerate(parsed_names))
               for row in configurations):
            return _result(path, digest, status="UNAVAILABLE", configurations=None, count=None,
                           backend_version=backend_version,
                           reason="isolated worker returned a configuration outside the input domains")
        normalized_rows = [tuple(row[name] for name in parsed_names) for row in configurations]
        if len(set(normalized_rows)) != len(normalized_rows):
            return _result(path, digest, status="UNAVAILABLE", configurations=None, count=None,
                           backend_version=backend_version,
                           reason="isolated worker returned duplicate configurations")
        level_indexes = [{level: index for index, level in enumerate(domain)}
                         for domain in parsed_levels]
        if any(any(all(level_indexes[factor][row[parsed_names[factor]]] == level
                       for factor, level in assignment)
                   for assignment in parsed_forbidden)
               for row in configurations):
            return _result(path, digest, status="UNAVAILABLE", configurations=None, count=None,
                           backend_version=backend_version,
                           reason="isolated worker returned a forbidden configuration")
    return output


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    raise SystemExit(_worker_main())
