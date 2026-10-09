from __future__ import annotations

import hashlib
import importlib.metadata
import itertools
import json
import random
import subprocess
from pathlib import Path
from typing import Any

import pytest

from vkit.operations import cover
from vkit.operations.cover import minimize_cover


def _write_matrix(root: Path, matrix: dict[str, Any], *, raw: bytes | None = None) -> str:
    path = root / "coverage.json"
    path.write_bytes(raw if raw is not None else json.dumps(matrix).encode("utf-8"))
    return "coverage.json"


def _brute_force(matrix: dict[str, Any]) -> int | None:
    best: int | None = None
    for bits in itertools.product((False, True), repeat=len(matrix["candidates"])):
        chosen = [candidate for candidate, bit in zip(matrix["candidates"], bits) if bit]
        covered = {element for candidate in chosen for element in candidate["covers"]}
        if set(matrix["required"]).issubset(covered):
            cost = sum(candidate["cost"] for candidate in chosen)
            if best is None or cost < best:
                best = cost
    return best


def test_returns_minimum_cost_selection_from_the_supplied_matrix(tmp_path: Path) -> None:
    matrix = {
        "version": 1,
        "required": ["a", "b", "c"],
        "candidates": [
            {"id": "ab", "cost": 4, "covers": ["a", "b"]},
            {"id": "c", "cost": 4, "covers": ["c"]},
            {"id": "a", "cost": 3, "covers": ["a"]},
            {"id": "bc", "cost": 3, "covers": ["b", "c"]},
            {"id": "all", "cost": 8, "covers": ["a", "b", "c"]},
        ],
    }
    path = _write_matrix(tmp_path, matrix)

    result = minimize_cover(tmp_path, path)

    assert result["status"] == "OPTIMAL", result
    assert result["selected"] == ["a", "bc"]
    assert result["total_cost"] == 6
    assert result["backend"] == "ortools"
    assert result["backend_version"] == "9.15.6755"
    assert result["source_sha256"] == hashlib.sha256((tmp_path / path).read_bytes()).hexdigest()
    assert "does not guarantee future coverage" in result["model_scope"]


def test_random_small_instances_match_exhaustive_integer_minima(tmp_path: Path) -> None:
    rng = random.Random(7189)
    for case in range(4):
        required = [f"e{index}" for index in range(4)]
        candidates: list[dict[str, Any]] = []
        for element in required:
            candidates.append({"id": f"required-{element}", "cost": rng.randint(1, 9),
                              "covers": [element]})
        for index in range(4):
            candidates.append({
                "id": f"random-{index}",
                "cost": rng.randint(0, 12),
                "covers": [element for element in (*required, "observed-extra") if rng.randrange(2)],
            })
        matrix = {"version": 1, "required": required, "candidates": candidates}
        path = _write_matrix(tmp_path, matrix)

        result = minimize_cover(tmp_path, path)

        assert result["status"] == "OPTIMAL"
        assert result["total_cost"] == _brute_force(matrix)
        selected = set(result["selected"])
        selected_candidates = [candidate for candidate in candidates if candidate["id"] in selected]
        assert set(required).issubset({element for item in selected_candidates for element in item["covers"]})
        assert result["total_cost"] == sum(item["cost"] for item in selected_candidates)


def test_reports_infeasible_when_a_required_id_has_no_candidate(tmp_path: Path) -> None:
    matrix = {"version": 1, "required": ["uncovered"],
              "candidates": [{"id": "a", "cost": 0, "covers": ["other"]}]}
    path = _write_matrix(tmp_path, matrix)

    result = minimize_cover(tmp_path, path)

    assert result["status"] == "INFEASIBLE"
    assert result["selected"] is None
    assert result["total_cost"] is None


def test_empty_required_set_has_empty_zero_cost_optimum(tmp_path: Path) -> None:
    matrix = {"version": 1, "required": [],
              "candidates": [{"id": "free", "cost": 0, "covers": ["extra"]}]}
    path = _write_matrix(tmp_path, matrix)

    result = minimize_cover(tmp_path, path)

    assert result["status"] == "OPTIMAL"
    assert result["selected"] == []
    assert result["total_cost"] == 0


def test_preserves_exact_objective_at_the_float_safe_cost_limit(tmp_path: Path) -> None:
    matrix = {"version": 1, "required": ["large"], "candidates": [
        {"id": "large", "cost": cover.MAX_TOTAL_COST, "covers": ["large"]},
        {"id": "unused", "cost": 0, "covers": []},
    ]}
    path = _write_matrix(tmp_path, matrix)

    result = minimize_cover(tmp_path, path)

    assert result["status"] == "OPTIMAL"
    assert result["selected"] == ["large"]
    assert result["total_cost"] == (1 << 53) - 1


def test_rejects_duplicate_keys_nonfinite_values_and_invalid_costs(tmp_path: Path) -> None:
    empty = {"version": 1, "required": [], "candidates": []}
    for raw in (
        b'{"version":1,"required":[],"required":[],"candidates":[]}',
        b'{"version":1,"required":[],"candidates":[],"extra":NaN}',
    ):
        path = _write_matrix(tmp_path, empty, raw=raw)
        with pytest.raises(ValueError):
            minimize_cover(tmp_path, path)

    for cost in (True, 1.0, -1):
        matrix = {"version": 1, "required": [],
                  "candidates": [{"id": "a", "cost": cost, "covers": []}]}
        path = _write_matrix(tmp_path, matrix)
        with pytest.raises(ValueError, match="non-negative integer"):
            minimize_cover(tmp_path, path)

    too_large = {"version": 1, "required": [], "candidates": [
        {"id": "max", "cost": cover.MAX_TOTAL_COST, "covers": []},
        {"id": "one-more", "cost": 1, "covers": []},
    ]}
    path = _write_matrix(tmp_path, too_large)
    with pytest.raises(ValueError, match="sum of candidate costs"):
        minimize_cover(tmp_path, path)


@pytest.mark.parametrize("path", ("../outside.json", "..\\outside.json", "", "C:/matrix.json"))
def test_rejects_paths_that_are_not_repository_relative_posix_paths(tmp_path: Path, path: str) -> None:
    with pytest.raises(ValueError, match="path"):
        minimize_cover(tmp_path, path)


def test_rejects_symlink_that_resolves_outside_the_repository(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-cover.json"
    outside.write_text('{"version":1,"required":[],"candidates":[]}', encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ValueError, match="inside the repository root"):
        minimize_cover(tmp_path, "link.json")


@pytest.mark.parametrize("timeout", (True, 0, 61, 1.5))
def test_rejects_invalid_deadline(timeout: object, tmp_path: Path) -> None:
    path = _write_matrix(tmp_path, {"version": 1, "required": [], "candidates": []})
    with pytest.raises(ValueError, match="timeout_seconds"):
        minimize_cover(tmp_path, path, timeout_seconds=timeout)  # type: ignore[arg-type]


def test_hard_worker_deadline_returns_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_matrix(tmp_path, {"version": 1, "required": [], "candidates": []})

    def timed_out(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("worker", 1)

    monkeypatch.setattr(cover.subprocess, "run", timed_out)
    result = minimize_cover(tmp_path, path, timeout_seconds=1)

    assert result["status"] == "UNKNOWN"
    assert result["selected"] is None
    assert "exceeded 1 seconds" in result["reason"]


def test_missing_or_wrong_backend_version_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(package: str) -> str:
        raise importlib.metadata.PackageNotFoundError(package)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    cp_model, installed, reason = cover._load_backend()
    assert cp_model is None
    assert installed is None
    assert reason == "ortools 9.15.6755 is not installed"

    monkeypatch.setattr(importlib.metadata, "version", lambda package: "9.15.0")
    cp_model, installed, reason = cover._load_backend()
    assert cp_model is None
    assert installed == "9.15.0"
    assert reason == "requires ortools 9.15.6755; found 9.15.0"


def test_wrong_backend_version_is_returned_as_unavailable_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_matrix(tmp_path, {"version": 1, "required": [], "candidates": []})
    response = {"status": "UNAVAILABLE", "selected": None, "cost": None,
                "backend_version": "9.15.0", "reason": "requires ortools 9.15.6755; found 9.15.0"}

    def wrong_version(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args="worker", returncode=0,
                                           stdout=json.dumps(response), stderr="")

    monkeypatch.setattr(cover.subprocess, "run", wrong_version)
    result = minimize_cover(tmp_path, path)

    assert result["status"] == "UNAVAILABLE"
    assert result["backend_version"] == "9.15.0"
    assert result["reason"] == "requires ortools 9.15.6755; found 9.15.0"


def test_worker_start_failure_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_matrix(tmp_path, {"version": 1, "required": [], "candidates": []})

    def cannot_start(*args: object, **kwargs: object) -> None:
        raise OSError("python is unavailable")

    monkeypatch.setattr(cover.subprocess, "run", cannot_start)
    result = minimize_cover(tmp_path, path)

    assert result["status"] == "UNAVAILABLE"
    assert "cannot start isolated CP-SAT worker" in result["reason"]
