from __future__ import annotations

import hashlib
import json
import math
import platform
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import paired
from .inputs import CapturedSource
from .intent import BoundChangeRequest
from .kani_toolchain import KaniToolchain
from .paired_profiles import FilePin, parse_binding

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_U32 = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_MAX_INPUTS = 4096
_MAX_U32 = 2**32 - 1
_UNAVAILABLE_REASONS = frozenset(
    {
        "delegation_requires_rust_kani",
        "rust_kani_requires_rust_binding",
        "rust_kani_requires_native_linux_x86_64",
        "rust_kani_requires_host_toolchain_pin",
    }
)


@dataclass(frozen=True, slots=True)
class MethodCatalogEntry:
    kind: Literal["finite_paired", "rust_kani"]
    languages: tuple[str, ...]
    evidence: str
    cost_tier: int
    execution_profile: str
    scope: str
    limitations: str

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "languages": list(self.languages),
            "evidence": self.evidence,
            "cost_tier": self.cost_tier,
            "execution_profile": self.execution_profile,
            "scope": self.scope,
            "limitations": self.limitations,
        }


METHOD_CATALOG = (
    MethodCatalogEntry(
        "finite_paired",
        ("rust", "python", "typescript"),
        "empirical_sampled_finite_observations",
        1,
        "declared_u32_function_and_observation_profile",
        "selected finite inputs and repetitions",
        "Does not establish universal behavior, native-compiler correspondence, a source proof, or approval.",
    ),
    MethodCatalogEntry(
        "rust_kani",
        ("rust",),
        "trusted_engine_model",
        2,
        "kani_0_68_rust_u32_native_linux_x86_64_host_pin",
        "qualified finite-u32 relational harness under the retained engine model",
        "Does not establish arbitrary Rust semantics, native-compiler correspondence, or a source proof; it has no approval authority and dependency closure is incomplete.",
    ),
)
_CATALOG_BY_KIND = {entry.kind: entry for entry in METHOD_CATALOG}
_CATALOG_BYTES = json.dumps(
    [entry.to_json() for entry in METHOD_CATALOG],
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
CATALOG_SHA256 = hashlib.sha256(_CATALOG_BYTES).hexdigest()


def _validate_tokens(tokens: tuple[str, ...] | None) -> None:
    if tokens is None:
        return
    if not isinstance(tokens, tuple) or not 1 <= len(tokens) <= _MAX_INPUTS:
        raise ValueError(
            "input_tokens must be a non-empty tuple of at most 4096 tokens"
        )
    for token in tokens:
        if (
            not isinstance(token, str)
            or len(token) > 10
            or _U32.fullmatch(token) is None
        ):
            raise ValueError("input_tokens must contain canonical decimal u32 strings")
        if int(token) > _MAX_U32:
            raise ValueError("input_tokens values must fit in u32")
    if len(set(tokens)) != len(tokens):
        raise ValueError("input_tokens must be unique")


@dataclass(frozen=True, slots=True)
class FiniteMethod:
    input_tokens: tuple[str, ...] | None = None
    repetitions: int = 3
    timeout_seconds: float = 10
    max_shrink_trials: int = 20
    kind: Literal["finite_paired"] = field(default="finite_paired", init=False)

    def __post_init__(self) -> None:
        paired._validate_limits(
            self.repetitions, self.timeout_seconds, self.max_shrink_trials
        )
        _validate_tokens(self.input_tokens)

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "input_tokens": None
            if self.input_tokens is None
            else list(self.input_tokens),
            "repetitions": self.repetitions,
            "timeout_seconds": self.timeout_seconds,
            "max_shrink_trials": self.max_shrink_trials,
        }


def parse_kani_toolchain(raw: object) -> KaniToolchain:
    keys = {
        "kind",
        "profile",
        "target",
        "kani_root",
        "rustup_home",
        "cargo_home",
        "rust_root",
        "driver",
        "kani_manifest_sha256",
        "rust_manifest_sha256",
        "rustup_sha256",
        "environment",
        "host_libraries",
        "host_closure_complete",
    }
    if not isinstance(raw, dict) or set(raw) != keys:
        raise ValueError("toolchain must contain exactly the pinned Kani fields")
    roots = []
    for key in ("kani_root", "rustup_home", "cargo_home"):
        value = raw[key]
        if not isinstance(value, str) or "\0" in value:
            raise ValueError(f"toolchain.{key} must be a path string")
        path = Path(value)
        if not path.is_absolute():
            raise ValueError(f"toolchain.{key} must be absolute")
        roots.append(path)
    digests = []
    for key in ("kani_manifest_sha256", "rust_manifest_sha256", "rustup_sha256"):
        value = raw[key]
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise ValueError(f"toolchain.{key} must be a lowercase SHA-256")
        digests.append(value)
    raw_libraries = raw["host_libraries"]
    if not isinstance(raw_libraries, list):
        raise ValueError("toolchain.host_libraries must be an array")
    libraries = []
    for item in raw_libraries:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValueError("each host library pin must contain path and sha256")
        path_value = item["path"]
        digest = item["sha256"]
        if not isinstance(path_value, str) or "\0" in path_value:
            raise ValueError("host library path must be a path string")
        path = Path(path_value)
        if not path.is_absolute():
            raise ValueError("host library path must be absolute")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ValueError("host library sha256 must be a lowercase SHA-256")
        libraries.append(FilePin(path_value, digest))
    toolchain = KaniToolchain(
        kani_root=roots[0],
        rustup_home=roots[1],
        cargo_home=roots[2],
        kani_manifest_sha256=digests[0],
        rust_manifest_sha256=digests[1],
        rustup_sha256=digests[2],
        host_libraries=tuple(libraries),
    )
    if toolchain.to_json() != raw:
        raise ValueError(
            "toolchain contains inconsistent derived profile or environment fields"
        )
    return toolchain


@dataclass(frozen=True, slots=True)
class KaniMethod:
    toolchain: KaniToolchain
    timeout_seconds: float = 90
    kind: Literal["rust_kani"] = field(default="rust_kani", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.toolchain, KaniToolchain):
            raise TypeError("toolchain must be a KaniToolchain")
        timeout = self.timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or (isinstance(timeout, float) and not math.isfinite(timeout))
            or not 0 < timeout <= 300
        ):
            raise ValueError("Kani timeout_seconds must be in (0, 300]")

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "toolchain": self.toolchain.to_json(),
            "timeout_seconds": self.timeout_seconds,
        }


def _step_from_json(raw: object) -> FiniteMethod | KaniMethod:
    if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str):
        raise ValueError("each method step must be an object with a kind")
    kind = raw["kind"]
    if kind == "finite_paired":
        if set(raw) != {
            "kind",
            "input_tokens",
            "repetitions",
            "timeout_seconds",
            "max_shrink_trials",
        }:
            raise ValueError("finite_paired step has unexpected or missing fields")
        tokens = raw["input_tokens"]
        if tokens is not None:
            if not isinstance(tokens, list) or any(
                not isinstance(t, str) for t in tokens
            ):
                raise ValueError("input_tokens must be null or an array of strings")
            tokens = tuple(tokens)
        return FiniteMethod(
            input_tokens=tokens,
            repetitions=raw["repetitions"],
            timeout_seconds=raw["timeout_seconds"],
            max_shrink_trials=raw["max_shrink_trials"],
        )
    if kind == "rust_kani":
        if set(raw) != {"kind", "toolchain", "timeout_seconds"}:
            raise ValueError("rust_kani step has unexpected or missing fields")
        return KaniMethod(
            parse_kani_toolchain(raw["toolchain"]), raw["timeout_seconds"]
        )
    raise ValueError(f"unknown change method kind: {kind}")


@dataclass(frozen=True, slots=True)
class ChangeMethodPlan:
    steps: tuple[FiniteMethod | KaniMethod, ...]
    unavailable: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.steps, tuple) or len(self.steps) > len(METHOD_CATALOG):
            raise ValueError("method plan must contain at most two steps")
        if any(not isinstance(step, (FiniteMethod, KaniMethod)) for step in self.steps):
            raise TypeError("method plan steps must use a registered method kind")
        kinds = tuple(step.kind for step in self.steps)
        if len(kinds) != len(set(kinds)):
            raise ValueError("method plan cannot repeat a method kind")
        if not isinstance(self.unavailable, tuple) or any(
            not isinstance(reason, str) or reason not in _UNAVAILABLE_REASONS
            for reason in self.unavailable
        ):
            raise ValueError("method plan contains an unknown unavailable reason")
        if len(set(self.unavailable)) != len(self.unavailable):
            raise ValueError("method plan unavailable reasons must be unique")
        if not self.steps and not self.unavailable:
            raise ValueError(
                "method plan must select a method or explain unavailability"
            )
        if "rust_kani" in kinds and self.unavailable:
            raise ValueError("an available Kani step cannot have an unavailable reason")

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "catalog_sha256": CATALOG_SHA256,
            "steps": [step.to_json() for step in self.steps],
            "unavailable": list(self.unavailable),
        }

    @classmethod
    def from_json(cls, raw: object) -> ChangeMethodPlan:
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version",
            "catalog_sha256",
            "steps",
            "unavailable",
        }:
            raise ValueError("method plan must contain exactly the schema fields")
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            raise ValueError("method plan schema_version must be 1")
        if raw["catalog_sha256"] != CATALOG_SHA256:
            raise ValueError("method catalog identity changed")
        if not isinstance(raw["steps"], list) or len(raw["steps"]) > 2:
            raise ValueError(
                "method plan steps must be an array of at most two entries"
            )
        unavailable = raw["unavailable"]
        if not isinstance(unavailable, list) or any(
            not isinstance(reason, str) for reason in unavailable
        ):
            raise ValueError("method plan unavailable must be an array of strings")
        return cls(
            tuple(_step_from_json(step) for step in raw["steps"]),
            tuple(unavailable),
        )


def _kani_unavailable_reason(
    language: str, kani_toolchain: KaniToolchain | None
) -> str | None:
    if language not in _CATALOG_BY_KIND["rust_kani"].languages:
        return "rust_kani_requires_rust_binding"
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        return "rust_kani_requires_native_linux_x86_64"
    if kani_toolchain is None:
        return "rust_kani_requires_host_toolchain_pin"
    if not isinstance(kani_toolchain, KaniToolchain):
        raise TypeError("kani_toolchain must be a KaniToolchain or None")
    return None


def select_change_methods(
    bound: BoundChangeRequest,
    base: CapturedSource,
    candidate: CapturedSource,
    *,
    requested: str = "auto",
    kani_toolchain: KaniToolchain | None = None,
) -> ChangeMethodPlan:
    if not isinstance(requested, str) or requested not in {
        "auto",
        "finite_paired",
        "rust_kani",
    }:
        raise ValueError("requested must be auto, finite_paired, or rust_kani")
    if kani_toolchain is not None and not isinstance(kani_toolchain, KaniToolchain):
        raise TypeError("kani_toolchain must be a KaniToolchain or None")
    semantic = paired._semantic_files(bound, base, candidate)
    binding = parse_binding(
        paired._json_value(semantic["binding"], "binding semantic artifact")
    )
    if bound.delegation is not None:
        if requested == "finite_paired":
            return ChangeMethodPlan((), ("delegation_requires_rust_kani",))
        unavailable = _kani_unavailable_reason(binding.language, kani_toolchain)
        if unavailable is not None:
            return ChangeMethodPlan((), (unavailable,))
        return ChangeMethodPlan((KaniMethod(kani_toolchain),))
    finite = FiniteMethod()
    if requested == "finite_paired":
        return ChangeMethodPlan((finite,))
    unavailable = _kani_unavailable_reason(binding.language, kani_toolchain)
    if requested == "rust_kani":
        if unavailable is not None:
            return ChangeMethodPlan((), (unavailable,))
        return ChangeMethodPlan((KaniMethod(kani_toolchain),))
    if unavailable is not None:
        return ChangeMethodPlan((finite,), (unavailable,))
    return ChangeMethodPlan((finite, KaniMethod(kani_toolchain)))
