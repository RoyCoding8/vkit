from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from vkit.operations import dependency_resolution
from vkit.operations.dependency_resolution import resolve_dependencies


def _environment(**changes: str) -> dict[str, str]:
    environment = {
        "implementation_name": "cpython",
        "implementation_version": "3.12.4",
        "os_name": "nt",
        "platform_machine": "AMD64",
        "platform_release": "10",
        "platform_system": "Windows",
        "platform_version": "10.0.22631",
        "platform_python_implementation": "CPython",
        "python_full_version": "3.12.4",
        "python_version": "3.12",
        "sys_platform": "win32",
    }
    environment.update(changes)
    return environment


def _bundle(*, roots: list[str], candidates: list[dict[str, Any]],
            environment: dict[str, str] | None = None) -> dict[str, Any]:
    return {"version": 1, "roots": roots, "candidates": candidates,
            "environment": environment or _environment()}


def _resolve(tmp_path: Path, bundle: dict[str, Any], *, timeout_seconds: int = 20) -> dict[str, Any]:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    return resolve_dependencies(tmp_path, path.name, timeout_seconds=timeout_seconds)


def test_resolves_transitive_requirements_and_returns_independent_edges(tmp_path: Path) -> None:
    result = _resolve(tmp_path, _bundle(
        roots=["App >= 1"],
        candidates=[
            {"name": "app", "version": "1", "requires_dist": ["helper >= 2"]},
            {"name": "helper", "version": "2", "requires_dist": []},
        ],
    ))

    assert result["status"] == "RESOLVED", result
    assert result["resolution"] == [
        {"name": "app", "version": "1", "requires_python": None},
        {"name": "helper", "version": "2", "requires_python": None},
    ]
    assert result["edges"] == [
        {"from": None, "requirement": "App>=1"},
        {"from": "app==1", "requirement": "helper>=2"},
    ]
    assert result["backend_version"] == "1.2.1"
    assert result["packaging_version"] == "26.3"
    assert result["target_environment"]["sys_platform"] == "win32"
    assert result["prerelease_policy"] == "explicit_in_every_active_requirement"


def test_backtracks_to_a_candidate_that_satisfies_all_roots(tmp_path: Path) -> None:
    result = _resolve(tmp_path, _bundle(
        roots=["app >= 1", "shared < 2"],
        candidates=[
            {"name": "app", "version": "2", "requires_dist": ["shared >= 2"]},
            {"name": "app", "version": "1", "requires_dist": ["shared < 2"]},
            {"name": "shared", "version": "2", "requires_dist": []},
            {"name": "shared", "version": "1", "requires_dist": []},
        ],
    ))

    assert result["status"] == "RESOLVED", result
    assert [(item["name"], item["version"]) for item in result["resolution"]] == [
        ("app", "1"), ("shared", "1")]


@pytest.mark.parametrize(
    ("sys_platform", "expected"),
    [("win32", ["app", "win-helper"]), ("linux", ["app"])],
)
def test_evaluates_markers_only_against_the_captured_target_environment(
    tmp_path: Path, sys_platform: str, expected: list[str],
) -> None:
    result = _resolve(tmp_path, _bundle(
        roots=["app"],
        candidates=[
            {"name": "app", "version": "1", "requires_dist": [
                "win-helper >= 1; sys_platform == 'win32'"]},
            {"name": "win-helper", "version": "1", "requires_dist": []},
        ],
        environment=_environment(sys_platform=sys_platform),
    ))

    assert result["status"] == "RESOLVED", result
    assert [item["name"] for item in result["resolution"]] == expected


def test_requires_python_is_checked_against_the_captured_target(tmp_path: Path) -> None:
    result = _resolve(tmp_path, _bundle(
        roots=["lib"],
        candidates=[
            {"name": "lib", "version": "2", "requires_dist": [], "requires_python": ">=3.13"},
            {"name": "lib", "version": "1", "requires_dist": [], "requires_python": ">=3.11,<3.13"},
        ],
    ))

    assert result["status"] == "RESOLVED", result
    assert result["resolution"] == [
        {"name": "lib", "version": "1", "requires_python": "<3.13,>=3.11"}]


def test_prerelease_requires_every_active_requirement_to_opt_in(tmp_path: Path) -> None:
    result = _resolve(tmp_path, _bundle(
        roots=["lib >= 1a1", "lib < 2"],
        candidates=[{"name": "lib", "version": "1.5a1", "requires_dist": []}],
    ))

    assert result["status"] == "CONFLICT", result
    assert "finite candidate universe" in result["reason"]


def test_prerelease_is_eligible_when_every_active_requirement_opts_in(tmp_path: Path) -> None:
    result = _resolve(tmp_path, _bundle(
        roots=["lib >= 1a1, < 2a1"],
        candidates=[{"name": "lib", "version": "1.5a1", "requires_dist": []}],
    ))

    assert result["status"] == "RESOLVED", result
    assert result["resolution"][0]["version"] == "1.5a1"


def test_null_requires_python_is_not_treated_as_an_omitted_field(tmp_path: Path) -> None:
    result = _resolve(tmp_path, _bundle(
        roots=["app"],
        candidates=[{"name": "app", "version": "1", "requires_dist": [],
                     "requires_python": None}],
        environment=_environment(platform_system="extra"),
    ))

    assert result["status"] == "UNSUPPORTED", result


def test_marker_string_literal_extra_value_is_supported(tmp_path: Path) -> None:
    result = _resolve(tmp_path, _bundle(
        roots=["app"],
        candidates=[{"name": "app", "version": "1", "requires_dist": [
            "other; platform_system == 'extra'"]},
            {"name": "other", "version": "1", "requires_dist": []}],
        environment=_environment(platform_system="extra"),
    ))

    assert result["status"] == "RESOLVED", result
    assert [item["name"] for item in result["resolution"]] == ["app", "other"]


@pytest.mark.parametrize("root", ["lib @ https://example.test/lib.whl", "lib[fast]"])
def test_direct_urls_and_extras_are_outside_profile(tmp_path: Path, root: str) -> None:
    result = _resolve(tmp_path, _bundle(roots=[root], candidates=[]))

    assert result["status"] == "UNSUPPORTED"


def test_candidate_extras_and_direct_urls_are_outside_profile(tmp_path: Path) -> None:
    for requirement in ("other[fast]", "other @ https://example.test/other.whl"):
        result = _resolve(tmp_path, _bundle(
            roots=["app"],
            candidates=[{"name": "app", "version": "1", "requires_dist": [requirement]}],
        ))
        assert result["status"] == "UNSUPPORTED"

    marker = _resolve(tmp_path, _bundle(
        roots=["app"],
        candidates=[{"name": "app", "version": "1", "requires_dist": [
            "other; extra == 'fast'"]}],
    ))
    assert marker["status"] == "UNSUPPORTED"


def test_normalized_duplicate_candidate_identity_is_rejected(tmp_path: Path) -> None:
    result = _resolve(tmp_path, _bundle(
        roots=["some-lib"],
        candidates=[
            {"name": "Some_Lib", "version": "1.0", "requires_dist": []},
            {"name": "some-lib", "version": "1.0.0", "requires_dist": []},
        ],
    ))

    assert result["status"] == "UNSUPPORTED"
    assert "unique after name/version normalization" in result["reason"]


def test_missing_candidate_is_conflict_only_within_the_snapshot(tmp_path: Path) -> None:
    result = _resolve(tmp_path, _bundle(roots=["not-captured >= 4"], candidates=[]))

    assert result["status"] == "CONFLICT"
    assert result["resolution"] == []
    assert result["causes"] == [{"requirement": "not-captured>=4", "parent": None}]
    assert "finite candidate universe" in result["reason"]


def test_worker_deadline_returns_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def timed_out(*args: object, **kwargs: object) -> None:
        raise dependency_resolution.subprocess.TimeoutExpired("resolver", 1)

    monkeypatch.setattr(dependency_resolution.subprocess, "run", timed_out)
    result = _resolve(tmp_path, _bundle(roots=["app"], candidates=[]), timeout_seconds=1)

    assert result["status"] == "UNKNOWN"
    assert "exceeded 1 seconds" in result["reason"]


def test_round_limit_returns_unknown_instead_of_claiming_conflict(tmp_path: Path) -> None:
    candidates = [
        {"name": f"pkg-{index}", "version": "1", "requires_dist":
         ([f"pkg-{index + 1}"] if index < 205 else [])}
        for index in range(206)
    ]
    result = _resolve(tmp_path, _bundle(roots=["pkg-0"], candidates=candidates))

    assert result["status"] == "UNKNOWN", result
    assert "200-round limit" in result["reason"]


def test_oversized_candidate_version_is_rejected_before_output_amplification(tmp_path: Path) -> None:
    result = _resolve(tmp_path, _bundle(roots=["app"], candidates=[
        {"name": "app", "version": "1+" + "a" * 10_000, "requires_dist": ["leaf"] * 1_000},
        {"name": "leaf", "version": "1", "requires_dist": []}]))

    assert result["status"] == "UNSUPPORTED"
    assert "at most 256 UTF-8 bytes" in result["reason"]


def test_repeated_dependency_edges_cannot_amplify_worker_output(tmp_path: Path) -> None:
    app, leaf = "a" * 256, "b" * 100
    result = _resolve(tmp_path, _bundle(roots=[app], candidates=[
        {"name": app, "version": "1+" + "c" * 250, "requires_dist": [leaf] * 3_998},
        {"name": leaf, "version": "1", "requires_dist": []}]))

    assert result["status"] == "UNKNOWN"
    assert result["reason"] == "isolated resolver output exceeded its byte limit"
    assert len(json.dumps(result).encode("utf-8")) < 2_000_000
