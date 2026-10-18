"""Built-in computations over repository source and recorded failures."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..paths import open_project

MAX_SOURCE_BYTES = 65_536


def _source(root: Path, path: str) -> str:
    relative = Path(path)
    if relative.is_absolute():
        raise ValueError("path must be repository-relative")
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()) or target.suffix != ".py":
        raise ValueError("path must identify a Python file inside the repository")
    try:
        with target.open("rb") as handle:
            data = handle.read(MAX_SOURCE_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"cannot read {path!r}: {exc}") from exc
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError(f"source must be at most {MAX_SOURCE_BYTES} bytes")
    return data.decode("utf-8")


def check_rewrite(root: Path, path: str, function: str, replacement: str,
                  timeout_ms: int = 2_000) -> dict[str, Any]:
    from .expressions import ResourceLimits
    from .rewrites import check_function_rewrite

    limits = ResourceLimits(timeout_ms=timeout_ms, max_source_bytes=MAX_SOURCE_BYTES, max_ast_nodes=1_000)
    result = check_function_rewrite(_source(root, path), function, replacement, limits).to_json()
    return {"operation": "check_rewrite", "path": path, "function": function, **result}


def simplify_function(root: Path, path: str, function: str,
                      timeout_ms: int = 2_000) -> dict[str, Any]:
    from .expressions import ResourceLimits
    from .simplify import simplify_function as simplify

    limits = ResourceLimits(timeout_ms=timeout_ms, max_source_bytes=MAX_SOURCE_BYTES, max_ast_nodes=1_000)
    result = simplify(_source(root, path), function, limits=limits).to_json()
    return {"operation": "simplify_function", "path": path, "function": function, **result}


def reduce_failure(root: Path, run_id: str, path: str,
                   timeout_seconds: int = 60) -> dict[str, Any]:
    from .reduction import reduce_failure as reduce

    result = reduce(open_project(root), run_id, path, timeout_seconds=timeout_seconds).to_json()
    return {"operation": "reduce_failure", "run_id": run_id, "path": path, **result}
