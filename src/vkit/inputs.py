"""Content digests of the files a check depends on."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .nowindow import hidden_window
from .paths import Project, ProjectError

_SECRET_NAMES = frozenset(
    {
        ".htpasswd",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)
_SECRET_SUFFIXES = frozenset({".key", ".keystore", ".p12", ".pem", ".pfx"})


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
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
            cwd=project.root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
            **hidden_window(),
        )
    except FileNotFoundError as exc:
        raise ProjectError("git is not installed or not on PATH") from exc
    if done.returncode != 0:
        raise ProjectError(
            done.stderr.decode("utf-8", "replace").strip() or "git ls-files failed"
        )
    names = {
        raw.decode("utf-8", "surrogateescape")
        for raw in done.stdout.split(b"\0")
        if raw
    }
    return tuple(
        sorted(n for n in names if not is_secret(n) and (project.root / n).is_file())
    )


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def matches(relative: str, pattern: str) -> bool:
    pattern = pattern.rstrip("/")
    return (
        relative == pattern
        or relative.startswith(pattern + "/")
        or fnmatch.fnmatchcase(relative, pattern)
    )


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
            bool(self.declared) and any(matches(relative, p) for p in self.declared)
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "scope": self.scope,
            "declared": list(self.declared),
            "files": [{"path": p, "sha256": d} for p, d in self.files],
            "unmatched": list(self.unmatched),
        }


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
            chosen = tuple(
                f for f in self.files if any(matches(f, p) for p in patterns)
            )
            unmatched = tuple(
                p for p in patterns if not any(matches(f, p) for f in self.files)
            )
        return InputSet(
            patterns, tuple((f, self.digest_of(f)) for f in chosen), unmatched
        )


@dataclass(frozen=True)
class CapturedFile:
    """One immutable source file, with both Git and raw-byte identities."""

    path: str
    mode: str
    blob_oid: str
    raw_sha256: str
    content: bytes

    def to_json(self) -> dict[str, str]:
        return {
            "path": self.path,
            "mode": self.mode,
            "blob_oid": self.blob_oid,
            "raw_sha256": self.raw_sha256,
        }


@dataclass(frozen=True)
class CapturedSource:
    """A sealed Git commit or provisional working-tree source snapshot."""

    kind: Literal["git_commit", "working_tree"]
    object_format: Literal["sha1", "sha256"]
    commit: str | None
    tree: str | None
    parents: tuple[str, ...]
    files: tuple[CapturedFile, ...]

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_json())

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "object_format": self.object_format,
            "commit": self.commit,
            "tree": self.tree,
            "parents": list(self.parents),
            "files": [file.to_json() for file in self.files],
        }

    def materialize(self, captured_root: str | os.PathLike[str]) -> tuple[Path, ...]:
        """Write captured bytes under an existing, plain directory and verify every identity."""
        root = Path(captured_root).absolute()
        _check_plain_directory(root)
        written: list[Path] = []
        for file in self.files:
            _validate_relative_path(file.path)
            if file.mode not in {"100644", "100755"}:
                raise ProjectError(f"unsupported captured file mode: {file.path}")
            _validate_oid(file.blob_oid, self.object_format)
            if re.fullmatch(r"[0-9a-f]{64}", file.raw_sha256) is None:
                raise ProjectError(f"invalid raw-byte digest: {file.path}")
            if _git_blob_oid(file.content, self.object_format) != file.blob_oid:
                raise ProjectError(f"captured Git blob identity changed: {file.path}")
            if hashlib.sha256(file.content).hexdigest() != file.raw_sha256:
                raise ProjectError(f"captured raw-byte identity changed: {file.path}")
            target = root.joinpath(*file.path.split("/"))
            _make_plain_parents(root, target.parent)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(
                    target, flags, 0o755 if file.mode == "100755" else 0o644
                )
            except OSError as exc:
                raise ProjectError(
                    f"could not safely materialize {file.path}: {exc}"
                ) from exc
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(file.content)
            written.append(target)
        return tuple(written)


def capture_git_commit(
    root: str | os.PathLike[str] | Project, oid: str
) -> CapturedSource:
    """Capture every regular blob in a full Git commit using one batched blob read."""
    project_root = _root(root)
    _check_plain_directory(project_root)
    object_format = _object_format(project_root)
    commit, tree, parents = _commit_metadata(project_root, oid, object_format)
    result = _git(project_root, ["ls-tree", "-rz", "--full-tree", "-r", commit])
    entries = _parse_tree(result.stdout, object_format)
    secrets = [path for path, _, _ in entries if is_secret(path)]
    if secrets:
        raise ProjectError(
            f"formal source capture contains secret-matched paths: {', '.join(secrets)}"
        )
    blobs = _batch_blobs(
        project_root, tuple(entry[2] for entry in entries), object_format
    )
    files = tuple(
        _captured_file(path, mode, blobs[blob_oid], object_format)
        for path, mode, blob_oid in entries
    )
    return CapturedSource("git_commit", object_format, commit, tree, parents, files)


def capture_working_tree(root: str | os.PathLike[str] | Project) -> CapturedSource:
    """Capture tracked and unignored untracked regular files, preserving their exact bytes."""
    project_root = _root(root)
    _check_plain_directory(project_root)
    object_format = _object_format(project_root)
    commit_info = _head_metadata(project_root, object_format)
    result = _git(
        project_root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    )
    names = sorted({_decode_path(raw) for raw in result.stdout.split(b"\0") if raw})
    _validate_unique_paths(names)
    files: list[CapturedFile] = []
    for name in names:
        if is_secret(name):
            raise ProjectError(
                f"formal source capture contains a secret-matched path: {name}"
            )
        path = project_root.joinpath(*name.split("/"))
        _check_plain_path(project_root, path)
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or _is_reparse(info):
                raise ProjectError(f"unsupported non-regular source path: {name}")
            flags = (
                os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            with os.fdopen(os.open(path, flags), "rb") as stream:
                opened = os.fstat(stream.fileno())
                before = (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
                during = (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                )
                if (
                    not opened.st_ino
                    or not stat.S_ISREG(opened.st_mode)
                    or _is_reparse(opened)
                    or before != during
                ):
                    raise ProjectError(f"source path changed during capture: {name}")
                content = stream.read()
                finished = os.fstat(stream.fileno())
                current = path.lstat()
                after = (
                    finished.st_dev,
                    finished.st_ino,
                    finished.st_size,
                    finished.st_mtime_ns,
                    finished.st_ctime_ns,
                )
                path_after = (
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                    current.st_ctime_ns,
                )
                if (
                    after != during
                    or path_after != after
                    or not stat.S_ISREG(current.st_mode)
                    or _is_reparse(current)
                    or len(content) != finished.st_size
                ):
                    raise ProjectError(f"source path changed during capture: {name}")
        except FileNotFoundError as exc:
            raise ProjectError(f"tracked source path is missing: {name}") from exc
        except OSError as exc:
            raise ProjectError(
                f"could not safely read source path: {name}: {exc}"
            ) from exc
        mode = "100755" if info.st_mode & 0o111 else "100644"
        files.append(_captured_file(name, mode, content, object_format))
    return CapturedSource("working_tree", object_format, *commit_info, tuple(files))


def _root(root: str | os.PathLike[str] | Project) -> Path:
    value = root.root if isinstance(root, Project) else Path(root)
    return value.absolute()


def _git(
    root: Path, args: list[str], *, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
            input=input_bytes,
            stdin=subprocess.DEVNULL if input_bytes is None else None,
            capture_output=True,
            timeout=60,
            check=False,
            **hidden_window(),
        )
    except FileNotFoundError as exc:
        raise ProjectError("git is not installed or not on PATH") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ProjectError(detail or f"git {' '.join(args)} failed")
    return result


def _object_format(root: Path) -> Literal["sha1", "sha256"]:
    result = _git(root, ["rev-parse", "--show-object-format"])
    value = result.stdout.decode("ascii", "strict").strip()
    if value not in {"sha1", "sha256"}:
        raise ProjectError(f"unsupported Git object format: {value}")
    return value  # type: ignore[return-value]


def _commit_metadata(
    root: Path, oid: str, object_format: Literal["sha1", "sha256"]
) -> tuple[str, str, tuple[str, ...]]:
    _validate_oid(oid, object_format)
    result = _git(root, ["cat-file", "commit", oid])
    raw = result.stdout
    if _git_object_oid(b"commit", raw, object_format) != oid:
        raise ProjectError("commit object identity does not match requested OID")
    header = raw.split(b"\n\n", 1)[0].splitlines()
    trees = [line[5:].decode("ascii") for line in header if line.startswith(b"tree ")]
    parents = tuple(
        line[7:].decode("ascii") for line in header if line.startswith(b"parent ")
    )
    if len(trees) != 1:
        raise ProjectError("commit object has invalid tree metadata")
    _validate_oid(trees[0], object_format)
    for parent in parents:
        _validate_oid(parent, object_format)
    return oid, trees[0], parents


def _head_metadata(
    root: Path, object_format: Literal["sha1", "sha256"]
) -> tuple[str | None, str | None, tuple[str, ...]]:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=root,
        env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
        **hidden_window(),
    )
    if result.returncode:
        return None, None, ()
    oid = result.stdout.decode("ascii", "strict").strip()
    return _commit_metadata(root, oid, object_format)


def _parse_tree(
    raw: bytes, object_format: Literal["sha1", "sha256"]
) -> tuple[tuple[str, str, str], ...]:
    entries: list[tuple[str, str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode_bytes, kind_bytes, oid_bytes = metadata.split(b" ")
            path = _decode_path(path_bytes)
            mode = mode_bytes.decode("ascii")
            kind = kind_bytes.decode("ascii")
            oid = oid_bytes.decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProjectError("Git returned an invalid tree entry") from exc
        _validate_oid(oid, object_format)
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ProjectError(f"unsupported Git source entry {mode} {kind}: {path}")
        entries.append((path, mode, oid))
    entries.sort(key=lambda entry: entry[0])
    _validate_unique_paths([entry[0] for entry in entries])
    return tuple(entries)


def _batch_blobs(
    root: Path, oids: tuple[str, ...], object_format: Literal["sha1", "sha256"]
) -> dict[str, bytes]:
    if not oids:
        return {}
    result = _git(
        root,
        ["cat-file", "--batch"],
        input_bytes=("\n".join(oids) + "\n").encode("ascii"),
    )
    data = result.stdout
    offset = 0
    blobs: dict[str, bytes] = {}
    for expected_oid in oids:
        end = data.find(b"\n", offset)
        if end < 0:
            raise ProjectError("Git returned a truncated batch header")
        header = data[offset:end].split()
        if (
            len(header) != 3
            or header[0].decode("ascii", "replace") != expected_oid
            or header[1] != b"blob"
        ):
            raise ProjectError(
                f"Git returned an unexpected batch object for {expected_oid}"
            )
        try:
            size = int(header[2])
        except ValueError as exc:
            raise ProjectError("Git returned an invalid batch object size") from exc
        start = end + 1
        stop = start + size
        if stop >= len(data) or data[stop : stop + 1] != b"\n":
            raise ProjectError("Git returned a truncated batch object")
        content = data[start:stop]
        if _git_blob_oid(content, object_format) != expected_oid:
            raise ProjectError(f"Git blob identity mismatch: {expected_oid}")
        blobs[expected_oid] = content
        offset = stop + 1
    if offset != len(data):
        raise ProjectError("Git returned unexpected trailing batch data")
    return blobs


def _captured_file(
    path: str, mode: str, content: bytes, object_format: Literal["sha1", "sha256"]
) -> CapturedFile:
    return CapturedFile(
        path,
        mode,
        _git_blob_oid(content, object_format),
        hashlib.sha256(content).hexdigest(),
        content,
    )


def _git_blob_oid(content: bytes, object_format: Literal["sha1", "sha256"]) -> str:
    return _git_object_oid(b"blob", content, object_format)


def _git_object_oid(
    kind: bytes, content: bytes, object_format: Literal["sha1", "sha256"]
) -> str:
    digest = hashlib.sha1 if object_format == "sha1" else hashlib.sha256
    return digest(
        kind + b" " + str(len(content)).encode("ascii") + b"\0" + content
    ).hexdigest()


def _validate_oid(oid: str, object_format: Literal["sha1", "sha256"]) -> None:
    length = 40 if object_format == "sha1" else 64
    if len(oid) != length or re.fullmatch(r"[0-9a-f]+", oid) is None:
        raise ProjectError(f"expected a full {object_format} Git object ID")


def _decode_path(value: bytes) -> str:
    try:
        path = value.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ProjectError("source paths must be valid UTF-8") from exc
    _validate_relative_path(path)
    return path


def _validate_relative_path(path: str) -> None:
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or ":" in path
        or any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in parts)
        or re.match(r"^[A-Za-z]:", path)
    ):
        raise ProjectError(f"unsafe source path: {path!r}")
    if any(
        part.split(".", 1)[0].upper()
        in {
            "CON",
            "CONIN$",
            "CONOUT$",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
        for part in parts
    ):
        raise ProjectError(f"unsafe source path: {path!r}")


def _validate_unique_paths(paths: list[str]) -> None:
    seen: dict[str, str] = {}
    for path in paths:
        key = path.casefold()
        if key in seen:
            raise ProjectError(
                f"source paths collide on case-insensitive filesystems: {seen[key]}, {path}"
            )
        seen[key] = path


def _check_plain_directory(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ProjectError(
                f"source/capture root is not accessible: {current}"
            ) from exc
        if _is_reparse(info) or stat.S_ISLNK(info.st_mode):
            raise ProjectError(
                f"source/capture root contains a symlink or reparse point: {current}"
            )
    if not stat.S_ISDIR(absolute.lstat().st_mode):
        raise ProjectError(f"source/capture root is not a directory: {absolute}")


def _check_plain_path(root: Path, path: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ProjectError(f"source path escapes repository root: {path}") from exc
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ProjectError(f"source path is inaccessible: {current}") from exc
        if _is_reparse(info) or stat.S_ISLNK(info.st_mode):
            raise ProjectError(
                f"source path contains a symlink or reparse point: {current}"
            )


def _make_plain_parents(root: Path, parent: Path) -> None:
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise ProjectError(f"capture destination escapes its root: {parent}") from exc
    current = root
    for part in parent.relative_to(root).parts:
        current /= part
        try:
            current.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise ProjectError(
                f"could not create capture directory {current}: {exc}"
            ) from exc
        try:
            info = current.lstat()
        except OSError as exc:
            raise ProjectError(f"capture directory is inaccessible: {current}") from exc
        if (
            _is_reparse(info)
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
        ):
            raise ProjectError(
                f"capture path contains a symlink or reparse point: {current}"
            )


def _is_reparse(info: os.stat_result) -> bool:
    return bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )
