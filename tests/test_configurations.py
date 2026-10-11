from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

import vkit.operations.configurations as configurations
from vkit.operations.configurations import generate_configurations


def _write_model(tmp_path: Path, model: object) -> str:
    path = "model.json"
    (tmp_path / path).write_text(json.dumps(model), encoding="utf-8")
    return path


def _legal_rows(model: dict[str, object]) -> tuple[list[str], list[list[str]], list[dict[str, str]]]:
    factors = model["factors"]
    assert isinstance(factors, dict)
    names = list(factors)
    domains = [factors[name] for name in names]
    assert all(isinstance(domain, list) for domain in domains)
    forbidden = model.get("forbidden", [])
    rows = []
    for values in itertools.product(*domains):
        row = dict(zip(names, values))
        if not any(all(row[name] == level for name, level in assignment.items())
                   for assignment in forbidden):
            rows.append(row)
    return names, domains, rows


def _feasible_interactions(model: dict[str, object], rows: list[dict[str, str]]) -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
    names = list(model["factors"])
    strength = model.get("strength", 2)
    return {
        (subset, tuple(row[name] for name in subset))
        for row in rows
        for subset in itertools.combinations(names, strength)
    }


def _covered(model: dict[str, object], rows: list[dict[str, str]]) -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
    return _feasible_interactions(model, rows)


def _oracle_minimum(model: dict[str, object]) -> int:
    _, _, legal = _legal_rows(model)
    required = _feasible_interactions(model, legal)
    for size in range(1, len(legal) + 1):
        if any(_covered(model, list(selection)) == required
               for selection in itertools.combinations(legal, size)):
            return size
    raise AssertionError("each legal full row should cover every feasible projection")


def test_pairwise_generation_covers_every_feasible_interaction(tmp_path: Path) -> None:
    model = {"version": 1, "factors": {
        "browser": ["chromium", "firefox"],
        "os": ["linux", "windows"],
        "database": ["sqlite", "postgres"],
    }}
    result = generate_configurations(tmp_path, _write_model(tmp_path, model))
    assert result["status"] == "OPTIMAL", result
    assert result["count"] == 4
    names, _, legal = _legal_rows(model)
    generated = result["configurations"]
    assert isinstance(generated, list)
    assert len({tuple(row[name] for name in names) for row in generated}) == len(generated)
    assert all(row in legal for row in generated)
    assert _covered(model, generated) == _feasible_interactions(model, legal)


def test_forbidden_partial_assignments_define_feasible_projection_universe(tmp_path: Path) -> None:
    model = {"version": 1, "strength": 2, "factors": {
        "a": ["0", "1"], "b": ["0", "1"], "c": ["0", "1"], "d": ["0", "1"],
    }, "forbidden": [{"a": "0", "b": "0"}, {"c": "1", "d": "1"}]}
    result = generate_configurations(tmp_path, _write_model(tmp_path, model))
    assert result["status"] == "OPTIMAL"
    names, _, legal = _legal_rows(model)
    generated = result["configurations"]
    assert isinstance(generated, list)
    assert all(row in legal for row in generated)
    assert _covered(model, generated) == _feasible_interactions(model, legal)
    assert result["count"] == _oracle_minimum(model)


def test_strength_three_covers_all_feasible_triples(tmp_path: Path) -> None:
    model = {"version": 1, "strength": 3, "factors": {
        "a": ["0", "1"], "b": ["0", "1"], "c": ["0", "1"],
    }}
    result = generate_configurations(tmp_path, _write_model(tmp_path, model))
    assert result["status"] == "OPTIMAL"
    assert result["count"] == 8
    names, _, legal = _legal_rows(model)
    generated = result["configurations"]
    assert isinstance(generated, list)
    assert _covered(model, generated) == _feasible_interactions(model, legal)


def test_small_optimum_matches_independent_brute_force_oracle(tmp_path: Path) -> None:
    model = {"version": 1, "strength": 2, "factors": {
        "a": ["0", "1", "2"], "b": ["0", "1"], "c": ["0", "1"],
    }, "forbidden": [{"a": "2", "b": "1"}]}
    result = generate_configurations(tmp_path, _write_model(tmp_path, model))
    assert result["status"] == "OPTIMAL"
    assert result["count"] == _oracle_minimum(model)


def test_empty_forbidden_assignment_leaves_no_legal_configuration(tmp_path: Path) -> None:
    model = {"version": 1, "factors": {"a": ["0", "1"], "b": ["0", "1"]},
             "forbidden": [{}]}
    result = generate_configurations(tmp_path, _write_model(tmp_path, model))
    assert result["status"] == "INFEASIBLE"
    assert result["configurations"] is None
    assert result["count"] is None
    assert result["backend_version"] is None


@pytest.mark.parametrize("model", [
    {"version": 1, "factors": {"a": ["0", "0"], "b": ["0"]}},
    {"version": 1, "factors": {"a": ["0"], "b": ["0"]}, "forbidden": [{"missing": "0"}]},
    {"version": 1, "factors": {"a": ["0"], "b": ["0"]}, "forbidden": [{"a": "missing"}]},
    {"version": 1, "strength": 4, "factors": {"a": ["0"], "b": ["0"]}},
    {"version": 1, "factors": {"a": ["0"]}},
    {"version": 1, "factors": {"a": ["0"], "b": ["0"]}, "extra": True},
])
def test_invalid_factor_models_are_unsupported(tmp_path: Path, model: object) -> None:
    result = generate_configurations(tmp_path, _write_model(tmp_path, model))
    assert result["status"] == "UNSUPPORTED"
    assert result["configurations"] is None


def test_cartesian_row_bound_returns_unknown_before_solver(tmp_path: Path) -> None:
    model = {"version": 1, "factors": {
        **{f"f{index}": [str(level) for level in range(10)] for index in range(4)},
    }}
    result = generate_configurations(tmp_path, _write_model(tmp_path, model))
    assert result["status"] == "UNKNOWN"
    assert result["configurations"] is None
    assert "enumeration limit" in result["reason"]


def test_coverage_pair_bound_returns_unknown(tmp_path: Path) -> None:
    model = {"version": 1, "factors": {
        **{f"f{index}": ["0", "1"] for index in range(11)},
    }}
    result = generate_configurations(tmp_path, _write_model(tmp_path, model))
    assert result["status"] == "UNKNOWN"
    assert result["configurations"] is None
    assert "coverage matrix" in result["reason"]


def test_repository_capture_rejects_duplicate_keys_and_symlink(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"version":1,"version":1}', encoding="utf-8")
    result = generate_configurations(tmp_path, "duplicate.json")
    assert result["status"] == "UNSUPPORTED"

    target = tmp_path / "target.json"
    target.write_text('{"version":1,"factors":{"a":["0"],"b":["0"]}}', encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")
    result = generate_configurations(tmp_path, "link.json")
    assert result["status"] == "UNSUPPORTED"


def test_invalid_timeout_is_rejected_before_capture(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        generate_configurations(tmp_path, "missing.json", timeout_seconds=0)


def test_outer_deadline_returns_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = {"version": 1, "factors": {"a": ["0"], "b": ["0"]}}

    def timeout(*args: object, **kwargs: object) -> None:
        raise configurations.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(configurations.subprocess, "run", timeout)
    result = generate_configurations(tmp_path, _write_model(tmp_path, model))
    assert result["status"] == "UNKNOWN"
    assert result["configurations"] is None
    assert "exceeded 10 seconds" in result["reason"]


def test_long_labels_cannot_amplify_configuration_output(tmp_path: Path) -> None:
    model = {"version": 1, "factors": {
        "a" * 256: [str(index).zfill(256) for index in range(40)],
        "b" * 256: [str(index).zfill(256) for index in range(40)],
        "c" * 256: ["x" * 256],
    }}
    result = generate_configurations(tmp_path, _write_model(tmp_path, model))

    assert result["status"] == "UNKNOWN"
    assert result["configurations"] is None
    assert result["reason"] == "configuration output exceeded its byte limit"
    assert len(json.dumps(result).encode("utf-8")) < 2_000_000
