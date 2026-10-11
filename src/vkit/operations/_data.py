"""Bounded capture of strict repository-local JSON data files."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"non-finite JSON number {value!r} is not allowed")
    return number


def encode_json(value: Any, *, max_bytes: int) -> str:
    """Encode an ASCII JSON response without constructing an oversized serialization."""
    chunks: list[str] = []
    size = 0
    encoder = json.JSONEncoder(ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    for chunk in encoder.iterencode(value):
        size += len(chunk)
        if size > max_bytes:
            raise ValueError("JSON output exceeded its byte limit")
        chunks.append(chunk)
    return "".join(chunks)


def read_json(root: Path, path: str, *, max_bytes: int,
              reject_symlinks: bool = False) -> tuple[Any, str]:
    """Read one bounded JSON file and return its value and captured-byte digest."""
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if (not isinstance(path, str) or not path or Path(path).is_absolute()
            or PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive):
        raise ValueError("path must be a non-empty repository-relative path")
    parts = path.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("path must not contain empty, '.' or '..' segments")

    try:
        resolved_root = Path(root).resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValueError("root must identify a directory")
        target = resolved_root.joinpath(*parts)
        if reject_symlinks:
            current = resolved_root
            for part in parts:
                current = current / part
                if current.is_symlink():
                    raise ValueError("path must not contain symlinks")
        target = target.resolve(strict=True)
        if not target.is_relative_to(resolved_root):
            raise ValueError("path must remain inside the repository root")
        if not target.is_file():
            raise ValueError("path must identify a regular file")
        with target.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except ValueError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot read repository-relative JSON {path!r}: {exc}") from exc

    if len(raw) > max_bytes:
        raise ValueError(f"JSON file must be at most {max_bytes} bytes")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeds parser limits") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON file must be valid UTF-8 JSON: {exc}") from exc
    return value, hashlib.sha256(raw).hexdigest()
