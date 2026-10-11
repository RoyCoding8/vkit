from __future__ import annotations

import pytest

from vkit.operations.cache_keys import find_cache_key_collision


pytest.importorskip("cvc5", reason="optional pinned cvc5 backend is not installed")


def _run(tmp_path, source: str, function: str = "value", key_function: str = "key", **kwargs):
    (tmp_path / "cache.py").write_text(source, encoding="utf-8")
    return find_cache_key_collision(tmp_path, "cache.py", function, key_function, **kwargs)


def test_cache_key_collision_returns_cvc5_pair_that_replays(tmp_path) -> None:
    source = """
def value(x: int) -> int:
    return x

def key(x: int) -> int:
    return x * x
"""

    result = _run(tmp_path, source)

    assert result["status"] == "COLLISION"
    assert result["backend"] == "cvc5"
    assert result["backend_version"] == "1.4.2"
    left, right = result["collision"]["left"], result["collision"]["right"]
    assert left["arguments"] != right["arguments"]
    assert left["key"] == right["key"]
    assert left["value"] != right["value"]
    assert result["source_sha256"]


def test_cache_key_collision_can_prove_no_pair_within_model(tmp_path) -> None:
    source = """
def value(x: int) -> int:
    return x * x

def key(x: int) -> int:
    return x
"""

    result = _run(tmp_path, source)

    assert result["status"] == "NO_COLLISION_WITHIN_MODEL"
    assert result["collision"] is None
    assert "Mathematical integers" in result["model_scope"]


def test_cache_key_collision_refuses_effects_without_executing_module(tmp_path) -> None:
    marker = tmp_path / "executed"
    source = f"""
from pathlib import Path
Path({str(marker)!r}).write_text('imported')

def value(x: int) -> int:
    return helper(x)

def key(x: int) -> int:
    return x
"""

    result = _run(tmp_path, source)

    assert result["status"] == "UNSUPPORTED"
    assert "Call" in result["reason"]
    assert not marker.exists()


def test_cache_key_collision_requires_same_argument_sorts_and_result_sort(tmp_path) -> None:
    source = """
def value(x: int) -> int:
    return x

def key(x: bool) -> int:
    return 1 if x else 0
"""

    result = _run(tmp_path, source)

    assert result["status"] == "UNSUPPORTED"
    assert "same positional parameter sorts" in result["reason"]


def test_cache_key_collision_models_boolean_inputs(tmp_path) -> None:
    source = """
def value(enabled: bool) -> int:
    return 1 if enabled else 0

def key(enabled: bool) -> int:
    return 7
"""

    result = _run(tmp_path, source)

    assert result["status"] == "COLLISION"
    left, right = result["collision"]["left"], result["collision"]["right"]
    assert type(left["arguments"]["enabled"]) is bool
    assert type(right["arguments"]["enabled"]) is bool
    assert left["key"] == right["key"] == 7
    assert left["value"] != right["value"]


def test_cache_key_collision_requires_matching_key_return_sort(tmp_path) -> None:
    source = """
def value(x: int) -> int:
    return x

def key(x: int) -> bool:
    return x == 0
"""

    result = _run(tmp_path, source)

    assert result["status"] == "UNSUPPORTED"
    assert "same positional parameter sorts and return sort" in result["reason"]
