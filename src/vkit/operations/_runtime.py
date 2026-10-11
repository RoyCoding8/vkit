"""Resolve configured backend binaries against their SHA-256 pins."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def pinned_binary(path_env: str, sha_env: str) -> tuple[Path, str] | None:
    raw_path = os.environ.get(path_env)
    expected = os.environ.get(sha_env, "").lower()
    if not raw_path or not _SHA256.fullmatch(expected):
        return None
    try:
        binary = Path(raw_path).expanduser().resolve()
        if not binary.is_file():
            return None
        with binary.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
    except (OSError, RuntimeError, ValueError):
        return None
    return (binary, digest) if digest == expected else None
