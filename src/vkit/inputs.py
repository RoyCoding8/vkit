"""Content digests of the files a check depends on."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .nowindow import hidden_window
from .paths import Project, ProjectError

_SECRET_NAMES = frozenset({".htpasswd", ".netrc", ".npmrc", ".pypirc", "credentials",
                           "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"})
_SECRET_SUFFIXES = frozenset({".key", ".keystore", ".p12", ".pem", ".pfx"})


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def is_secret(relative: str) -> bool:
    for part in relative.split("/"):
        if part == ".env" or part.startswith(".env."):
            return True
        if part in _SECRET_NAMES or Path(part).suffix.lower() in _SECRET_SUFFIXES:
            return True
    return False


def source_files(project: Project) -> tuple[str, ...]:
    """Tracked plus untracked-but-not-ignored files, minus secrets, sorted."""
    try:
        done = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=project.root, capture_output=True, timeout=30, check=False, **hidden_window(),
        )
    except FileNotFoundError as exc:
        raise ProjectError("git is not installed or not on PATH") from exc
    if done.returncode != 0:
        raise ProjectError(done.stderr.decode("utf-8", "replace").strip() or "git ls-files failed")
    names = {raw.decode("utf-8", "surrogateescape") for raw in done.stdout.split(b"\0") if raw}
    return tuple(sorted(n for n in names if not is_secret(n) and (project.root / n).is_file()))


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def matches(relative: str, pattern: str) -> bool:
    pattern = pattern.rstrip("/")
    return relative == pattern or relative.startswith(pattern + "/") or fnmatch.fnmatchcase(relative, pattern)


@dataclass(frozen=True)
class InputSet:
    """The files one check reads, with their digests, at one moment."""

    declared: tuple[str, ...]
    files: tuple[tuple[str, str], ...]
    unmatched: tuple[str, ...]

    @property
    def digest(self) -> str:
        return canonical_digest([list(f) for f in self.files])

    @property
    def scope(self) -> str:
        if not self.declared:
            return f"whole tree ({len(self.files)} files)"
        return f"declared inputs ({len(self.files)} files)"

    def covers(self, relative: str) -> bool:
        return any(name == relative for name, _ in self.files) or (
            bool(self.declared) and any(matches(relative, p) for p in self.declared))

    def to_json(self) -> dict[str, Any]:
        return {"digest": self.digest, "scope": self.scope, "declared": list(self.declared),
                "files": [{"path": p, "sha256": d} for p, d in self.files],
                "unmatched": list(self.unmatched)}


class Snapshot:
    """One listing of the tree, hashed lazily and at most once per file."""

    def __init__(self, project: Project, files: Iterable[str] | None = None) -> None:
        self.project = project
        self.files = tuple(files) if files is not None else source_files(project)
        self._digests: dict[str, str] = {}

    def digest_of(self, relative: str) -> str:
        if relative not in self._digests:
            self._digests[relative] = file_digest(self.project.root / relative)
        return self._digests[relative]

    def inputs(self, declared: Iterable[str]) -> InputSet:
        patterns = tuple(declared)
        if not patterns:
            chosen = self.files
            unmatched: tuple[str, ...] = ()
        else:
            chosen = tuple(f for f in self.files if any(matches(f, p) for p in patterns))
            unmatched = tuple(p for p in patterns if not any(matches(f, p) for f in self.files))
        return InputSet(patterns, tuple((f, self.digest_of(f)) for f in chosen), unmatched)
