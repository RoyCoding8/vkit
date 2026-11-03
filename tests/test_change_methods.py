from __future__ import annotations

import math
from pathlib import Path

import pytest

from test_paired import _repository
from vkit import change_methods
from vkit.change_methods import (
    CATALOG_SHA256,
    ChangeMethodPlan,
    FiniteMethod,
    KaniMethod,
    select_change_methods,
)
from vkit.intent import _prepare_change_request
from vkit.kani_toolchain import KaniToolchain
from vkit.paired_profiles import FilePin
from vkit.paths import open_project


def _captured_pair(tmp_path: Path, language: str):
    root, target_oid, request = _repository(
        tmp_path,
        language=language,
        candidate_delta=0,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
    )
    bound, base, candidate = _prepare_change_request(
        open_project(root), request, trusted_target_oid=target_oid
    )
    return bound, base, candidate


def _toolchain(tmp_path: Path) -> KaniToolchain:
    return KaniToolchain(
        kani_root=tmp_path / "kani",
        rustup_home=tmp_path / "rustup",
        cargo_home=tmp_path / "cargo",
        kani_manifest_sha256="a" * 64,
        rust_manifest_sha256="b" * 64,
        rustup_sha256="c" * 64,
        host_libraries=(FilePin(str(tmp_path / "libc.so"), "d" * 64),),
    )


def test_auto_selects_finite_for_captured_python_binding(tmp_path: Path) -> None:
    bound, base, candidate = _captured_pair(tmp_path, "python")

    plan = select_change_methods(bound, base, candidate)

    assert [step.kind for step in plan.steps] == ["finite_paired"]
    assert plan.steps[0] == FiniteMethod()
    assert plan.unavailable == ("rust_kani_requires_rust_binding",)
    assert plan.to_json()["steps"] == [
        {
            "kind": "finite_paired",
            "input_tokens": None,
            "repetitions": 3,
            "timeout_seconds": 10,
            "max_shrink_trials": 20,
        }
    ]


def test_auto_adds_kani_after_finite_for_captured_rust_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bound, base, candidate = _captured_pair(tmp_path, "rust")
    monkeypatch.setattr(change_methods.platform, "system", lambda: "Linux")
    monkeypatch.setattr(change_methods.platform, "machine", lambda: "x86_64")
    pin = _toolchain(tmp_path)
    assert not pin.kani_root.exists()

    plan = select_change_methods(bound, base, candidate, kani_toolchain=pin)

    assert [step.kind for step in plan.steps] == ["finite_paired", "rust_kani"]
    assert plan.steps[1] == KaniMethod(pin)
    assert plan.unavailable == ()


@pytest.mark.parametrize(
    ("system", "machine", "toolchain", "reason"),
    [
        ("Windows", "AMD64", None, "rust_kani_requires_native_linux_x86_64"),
        ("Linux", "x86_64", None, "rust_kani_requires_host_toolchain_pin"),
    ],
)
def test_auto_records_why_kani_is_unavailable_for_captured_rust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    toolchain: KaniToolchain | None,
    reason: str,
) -> None:
    bound, base, candidate = _captured_pair(tmp_path, "rust")
    monkeypatch.setattr(change_methods.platform, "system", lambda: system)
    monkeypatch.setattr(change_methods.platform, "machine", lambda: machine)

    plan = select_change_methods(bound, base, candidate, kani_toolchain=toolchain)

    assert [step.kind for step in plan.steps] == ["finite_paired"]
    assert plan.unavailable == (reason,)


def test_explicit_kani_does_not_fall_back_for_unsupported_binding(
    tmp_path: Path,
) -> None:
    bound, base, candidate = _captured_pair(tmp_path, "python")

    plan = select_change_methods(
        bound,
        base,
        candidate,
        requested="rust_kani",
        kani_toolchain=_toolchain(tmp_path),
    )

    assert plan.steps == ()
    assert plan.unavailable == ("rust_kani_requires_rust_binding",)


def test_explicit_finite_selects_only_finite_method(tmp_path: Path) -> None:
    bound, base, candidate = _captured_pair(tmp_path, "rust")

    plan = select_change_methods(
        bound,
        base,
        candidate,
        requested="finite_paired",
        kani_toolchain=_toolchain(tmp_path),
    )

    assert [step.kind for step in plan.steps] == ["finite_paired"]
    assert plan.unavailable == ()


def test_plan_round_trip_preserves_typed_steps_and_catalog_identity(
    tmp_path: Path,
) -> None:
    plan = ChangeMethodPlan(
        (
            FiniteMethod(
                input_tokens=("0", "3"),
                repetitions=2,
                timeout_seconds=2.5,
                max_shrink_trials=0,
            ),
            KaniMethod(_toolchain(tmp_path), timeout_seconds=120),
        )
    )

    raw = plan.to_json()
    restored = ChangeMethodPlan.from_json(raw)

    assert restored == plan
    assert raw["catalog_sha256"] == CATALOG_SHA256
    assert isinstance(restored.steps[0].input_tokens, tuple)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.update(extra=True),
        lambda raw: raw.update(schema_version=True),
        lambda raw: raw.update(catalog_sha256="0" * 64),
        lambda raw: raw.update(steps=[raw["steps"][0], raw["steps"][0]]),
        lambda raw: raw["steps"][0].update(extra=True),
        lambda raw: raw.update(unavailable=["not-a-registered-reason"]),
    ],
)
def test_plan_parser_rejects_changed_or_open_shapes(tmp_path: Path, mutate) -> None:
    raw = ChangeMethodPlan((FiniteMethod(),)).to_json()
    mutate(raw)

    with pytest.raises((TypeError, ValueError)):
        ChangeMethodPlan.from_json(raw)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda toolchain: toolchain["environment"].update(PATH="candidate-bin"),
        lambda toolchain: toolchain.update(driver="/candidate/driver"),
        lambda toolchain: toolchain.update(host_libraries=[{"path": "/lib/libc.so"}]),
        lambda toolchain: toolchain.update(
            host_libraries=[{"path": "relative.so", "sha256": "d" * 64}]
        ),
        lambda toolchain: toolchain.update(
            host_libraries=[{"path": "/lib/libc.so", "sha256": "D" * 64}]
        ),
    ],
)
def test_kani_step_rejects_forged_derived_environment_or_host_pin(
    tmp_path: Path, mutate
) -> None:
    raw = ChangeMethodPlan((KaniMethod(_toolchain(tmp_path)),)).to_json()
    mutate(raw["steps"][0]["toolchain"])

    with pytest.raises(ValueError):
        ChangeMethodPlan.from_json(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repetitions", True),
        ("repetitions", 0),
        ("timeout_seconds", math.nan),
        ("timeout_seconds", True),
        ("max_shrink_trials", 257),
        ("input_tokens", []),
        ("input_tokens", ["01"]),
        ("input_tokens", [str(2**32)]),
    ],
)
def test_finite_step_parser_rejects_bad_bounds_and_tokens(
    field: str, value: object
) -> None:
    raw = ChangeMethodPlan((FiniteMethod(),)).to_json()
    raw["steps"][0][field] = value

    with pytest.raises((TypeError, ValueError)):
        ChangeMethodPlan.from_json(raw)


@pytest.mark.parametrize("timeout", [0, 301, True, math.nan, math.inf])
def test_kani_step_rejects_invalid_deadline(tmp_path: Path, timeout: object) -> None:
    raw = ChangeMethodPlan((KaniMethod(_toolchain(tmp_path)),)).to_json()
    raw["steps"][0]["timeout_seconds"] = timeout

    with pytest.raises((TypeError, ValueError)):
        ChangeMethodPlan.from_json(raw)
