from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .paired_profiles import FilePin

_PROFILE = "kani-0.68.0-x86_64-unknown-linux-gnu-v1"
_TARGET = "x86_64-unknown-linux-gnu"
_RUST_TOOLCHAIN = "nightly-2026-08-21-x86_64-unknown-linux-gnu"
_KANI_RUST_TOOLCHAIN = _RUST_TOOLCHAIN.encode("ascii")
_RUSTC_VERSION = b"rustc 1.100.0-nightly (8925ea358 2026-08-20)"
_RUSTC_DRIVER = "lib/librustc_driver-59857489213b2a36.so"
_LLVM = "lib/libLLVM.so.23.1-rust-1.100.0-nightly"
_RUSTUP_LINKS = frozenset(
    {
        "cargo",
        "cargo-clippy",
        "cargo-fmt",
        "cargo-miri",
        "clippy-driver",
        "rls",
        "rust-analyzer",
        "rust-gdb",
        "rust-gdbgui",
        "rust-lldb",
        "rustc",
        "rustdoc",
        "rustfmt",
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CHUNK_SIZE = 1024 * 1024
_MAX_TREE_ENTRIES = 400


class KaniToolchainError(ValueError):
    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class KaniToolchain:
    kani_root: Path
    rustup_home: Path
    cargo_home: Path
    kani_manifest_sha256: str
    rust_manifest_sha256: str
    rustup_sha256: str
    host_libraries: tuple[FilePin, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.host_libraries, tuple) or any(
            not isinstance(pin, FilePin) for pin in self.host_libraries
        ):
            raise TypeError("host_libraries must be a tuple of FilePin values")
        object.__setattr__(
            self,
            "host_libraries",
            tuple(sorted(self.host_libraries, key=lambda pin: pin.path)),
        )

    @property
    def rust_root(self) -> Path:
        return self.rustup_home / "toolchains" / _RUST_TOOLCHAIN

    @property
    def driver(self) -> Path:
        return self.kani_root / "bin" / "kani-driver"

    def environment(self) -> dict[str, str]:
        return {
            "CARGO_HOME": str(self.cargo_home),
            "LANG": "C.UTF-8",
            "LD_LIBRARY_PATH": str(self.rust_root / "lib"),
            "PATH": f"{self.kani_root / 'bin'}:{self.cargo_home / 'bin'}:/usr/bin:/bin",
            "RUSTUP_HOME": str(self.rustup_home),
            "RUSTUP_TOOLCHAIN": _RUST_TOOLCHAIN,
            "TZ": "UTC",
        }

    def to_json(self) -> dict[str, object]:
        return {
            "kind": "kani_toolchain",
            "profile": _PROFILE,
            "target": _TARGET,
            "kani_root": str(self.kani_root),
            "rustup_home": str(self.rustup_home),
            "cargo_home": str(self.cargo_home),
            "rust_root": str(self.rust_root),
            "driver": str(self.driver),
            "kani_manifest_sha256": self.kani_manifest_sha256,
            "rust_manifest_sha256": self.rust_manifest_sha256,
            "rustup_sha256": self.rustup_sha256,
            "environment": self.environment(),
            "host_libraries": [
                {"path": pin.path, "sha256": pin.sha256} for pin in self.host_libraries
            ],
            "host_closure_complete": False,
        }


def _error(reason: str, message: str) -> KaniToolchainError:
    return KaniToolchainError(reason, message)


def _check_cancelled(should_cancel: Callable[[], bool]) -> None:
    if should_cancel():
        raise _error("cancelled", "Kani toolchain validation was cancelled")


def _require_linux() -> None:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise _error(
            "unsupported_platform",
            "Kani 0.68.0 is qualified only for x86_64 Linux",
        )


def _digest_pin(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise _error("unsupported_installation", f"{label} must be a lowercase SHA-256")


def _resolved_directory(path: Path, label: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise _error(
            "unsupported_installation", f"{label} must be an absolute Linux path"
        )
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _error("unavailable", f"{label} is unavailable: {exc}") from exc
    if resolved != path or path.is_symlink():
        raise _error(
            "unsupported_installation", f"{label} must not contain a symlink path"
        )
    if not resolved.is_dir():
        raise _error("unavailable", f"{label} is not a directory")
    return resolved


def _read_marker(path: Path, label: str, expected: bytes) -> bytes:
    try:
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode):
            raise _error("unsupported_installation", f"{label} must not be a symlink")
        if not stat.S_ISREG(st.st_mode):
            raise _error("unsupported_installation", f"{label} must be a regular file")
        if st.st_size > len(expected):
            raise _error("unsupported_installation", f"{label} has unexpected content")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            data = os.read(descriptor, len(expected) + 1)
        finally:
            os.close(descriptor)
    except KaniToolchainError:
        raise
    except OSError as exc:
        raise _error("unavailable", f"{label} is unavailable: {exc}") from exc
    return data


def _require_regular(path: Path, label: str, *, executable: bool = False) -> None:
    try:
        st = path.lstat()
    except OSError as exc:
        raise _error("unavailable", f"{label} is unavailable: {exc}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise _error("unsupported_installation", f"{label} must be a regular file")
    if executable and not st.st_mode & 0o111:
        raise _error("unsupported_installation", f"{label} is not executable")


def _check_installation(kani_root: Path, rust_root: Path) -> None:
    kani_version = _read_marker(
        kani_root / "rust-toolchain-version",
        "Kani Rust toolchain marker",
        _KANI_RUST_TOOLCHAIN,
    )
    rustc_version = _read_marker(
        kani_root / "rustc-version", "Kani rustc marker", _RUSTC_VERSION + b"\n"
    )
    if kani_version != _KANI_RUST_TOOLCHAIN or rustc_version != _RUSTC_VERSION + b"\n":
        raise _error(
            "unsupported_installation", "Kani embedded Rust version is unsupported"
        )

    for path, label in (
        (kani_root / "bin" / "kani-driver", "Kani driver"),
        (kani_root / "bin" / "kani-compiler", "Kani compiler"),
        (rust_root / "bin" / "rustc", "selected rustc"),
        (rust_root / _RUSTC_DRIVER, "selected rustc driver library"),
        (rust_root / _LLVM, "selected LLVM library"),
    ):
        _require_regular(
            path,
            label,
            executable=label in {"Kani driver", "Kani compiler", "selected rustc"},
        )


def _file_sha256(
    path: Path, should_cancel: Callable[[], bool]
) -> tuple[int, str, os.stat_result]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _error(
            "unavailable", f"cannot open toolchain file {path}: {exc}"
        ) from exc

    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise _error(
                "unsupported_installation", f"non-regular toolchain file: {path}"
            )
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(descriptor, "rb", buffering=0, closefd=False) as stream:
            while True:
                _check_cancelled(should_cancel)
                chunk = stream.read(_CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or size != after.st_size:
            raise _error(
                "pin_mismatch", f"toolchain file changed while hashing: {path}"
            )
        return size, digest.hexdigest(), after
    finally:
        os.close(descriptor)


def _manifest_bytes(entries: list[dict[str, object]]) -> bytes:
    entries.sort(key=lambda entry: str(entry["path"]))
    return "".join(
        json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"
        for entry in entries
    ).encode("utf-8")


def _tree_manifest(root: Path, should_cancel: Callable[[], bool]) -> bytes:
    entries: list[dict[str, object]] = []

    def walk_error(error: OSError) -> None:
        raise _error(
            "unavailable", f"cannot inspect toolchain tree: {error}"
        ) from error

    for current, directories, files in os.walk(
        root, topdown=True, followlinks=False, onerror=walk_error
    ):
        _check_cancelled(should_cancel)
        for name in sorted((*directories, *files)):
            _check_cancelled(should_cancel)
            path = Path(current) / name
            relative_path = path.relative_to(root).as_posix()
            try:
                st = path.lstat()
            except OSError as exc:
                raise _error("unavailable", f"cannot inspect {path}: {exc}") from exc
            mode = stat.S_IMODE(st.st_mode)
            record: dict[str, object] = {
                "path": relative_path,
                "mode": mode,
            }
            if stat.S_ISDIR(st.st_mode):
                record["kind"] = "dir"
                entries.append(record)
            elif stat.S_ISREG(st.st_mode):
                size, digest, after = _file_sha256(path, should_cancel)
                if mode != stat.S_IMODE(after.st_mode):
                    raise _error("pin_mismatch", f"toolchain file mode changed: {path}")
                record.update({"kind": "file", "size": size, "sha256": digest})
                entries.append(record)
            elif stat.S_ISLNK(st.st_mode):
                raise _error(
                    "unsupported_installation", f"symlink in toolchain: {path}"
                )
            else:
                raise _error(
                    "unsupported_installation", f"special file in toolchain: {path}"
                )
            if len(entries) > _MAX_TREE_ENTRIES:
                raise _error("unsupported_installation", "toolchain tree is too large")
    return _manifest_bytes(entries)


def _cargo_manifest(
    cargo_home: Path, expected_rustup_sha256: str, should_cancel: Callable[[], bool]
) -> bytes:
    directory = cargo_home / "bin"
    try:
        st = directory.lstat()
    except OSError as exc:
        raise _error("unavailable", f"CARGO_HOME/bin is unavailable: {exc}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise _error(
            "unsupported_installation", "CARGO_HOME/bin must be a real directory"
        )
    try:
        with os.scandir(directory) as scan:
            children = sorted(scan, key=lambda entry: entry.name)
    except OSError as exc:
        raise _error("unavailable", f"cannot inspect CARGO_HOME/bin: {exc}") from exc

    names = {entry.name for entry in children}
    expected_names = _RUSTUP_LINKS | {"rustup"}
    if names != expected_names:
        raise _error(
            "unsupported_installation",
            "CARGO_HOME/bin has missing or unexpected entries",
        )

    rustup_path = directory / "rustup"
    rustup_size, rustup_digest, rustup_stat = _file_sha256(rustup_path, should_cancel)
    if rustup_digest != expected_rustup_sha256:
        raise _error(
            "pin_mismatch", "CARGO_HOME/bin/rustup digest differs from its pin"
        )
    if not (rustup_stat.st_mode & 0o111):
        raise _error(
            "unsupported_installation", "CARGO_HOME/bin/rustup is not executable"
        )

    entries: list[dict[str, object]] = [
        {
            "path": "rustup",
            "kind": "file",
            "mode": stat.S_IMODE(rustup_stat.st_mode),
            "size": rustup_size,
            "sha256": rustup_digest,
        }
    ]
    for entry in children:
        if entry.name == "rustup":
            continue
        _check_cancelled(should_cancel)
        path = directory / entry.name
        try:
            st = entry.stat(follow_symlinks=False)
            target = os.readlink(path)
        except OSError as exc:
            raise _error(
                "unavailable", f"cannot inspect rustup proxy {path}: {exc}"
            ) from exc
        if not stat.S_ISLNK(st.st_mode) or target != "rustup":
            raise _error(
                "unsupported_installation", f"unexpected rustup proxy: {entry.name}"
            )
        entries.append(
            {
                "path": entry.name,
                "kind": "symlink",
                "mode": stat.S_IMODE(st.st_mode),
                "target": target,
                "resolved": str(path.resolve(strict=True)),
            }
        )

    return _manifest_bytes(entries)


def _host_libraries_manifest(
    pins: tuple[FilePin, ...], should_cancel: Callable[[], bool]
) -> bytes:
    entries: list[dict[str, object]] = []
    for pin in pins:
        _check_cancelled(should_cancel)
        if not pin.path or "\0" in pin.path or not Path(pin.path).is_absolute():
            raise _error(
                "unsupported_installation", "host library paths must be absolute"
            )
        _digest_pin(pin.sha256, f"host library {pin.path}")
        path = Path(pin.path)
        try:
            resolved = path.resolve(strict=True)
            if resolved != path or path.is_symlink():
                raise _error(
                    "unsupported_installation", "host library path must be resolved"
                )
            st = path.lstat()
        except KaniToolchainError:
            raise
        except OSError as exc:
            raise _error(
                "unavailable", f"host library is unavailable: {path}: {exc}"
            ) from exc
        if not stat.S_ISREG(st.st_mode):
            raise _error(
                "unsupported_installation",
                f"host library is not a regular file: {path}",
            )
        size, digest, after = _file_sha256(path, should_cancel)
        if digest != pin.sha256:
            raise _error(
                "pin_mismatch", f"host library digest differs from its pin: {path}"
            )
        entries.append(
            {
                "path": str(path),
                "kind": "file",
                "mode": stat.S_IMODE(after.st_mode),
                "size": size,
                "sha256": digest,
            }
        )
    return _manifest_bytes(entries)


def validate_kani_toolchain(
    pin: KaniToolchain,
    *,
    should_cancel: Callable[[], bool] = lambda: False,
) -> tuple[tuple[str, bytes], ...]:
    if not isinstance(pin, KaniToolchain):
        raise TypeError("pin must be a KaniToolchain")
    _require_linux()
    _check_cancelled(should_cancel)
    _digest_pin(pin.kani_manifest_sha256, "Kani manifest pin")
    _digest_pin(pin.rust_manifest_sha256, "Rust manifest pin")
    _digest_pin(pin.rustup_sha256, "rustup pin")

    kani_root = _resolved_directory(pin.kani_root, "Kani root")
    _resolved_directory(pin.rustup_home, "RUSTUP_HOME")
    cargo_home = _resolved_directory(pin.cargo_home, "CARGO_HOME")
    rust_root = _resolved_directory(pin.rust_root, "selected Rust toolchain")
    _check_installation(kani_root, rust_root)

    kani_manifest = _tree_manifest(kani_root, should_cancel)
    if hashlib.sha256(kani_manifest).hexdigest() != pin.kani_manifest_sha256:
        raise _error("pin_mismatch", "Kani installation manifest differs from its pin")
    rust_manifest = _tree_manifest(rust_root, should_cancel)
    if hashlib.sha256(rust_manifest).hexdigest() != pin.rust_manifest_sha256:
        raise _error(
            "pin_mismatch", "selected Rust toolchain manifest differs from its pin"
        )
    cargo_manifest = _cargo_manifest(cargo_home, pin.rustup_sha256, should_cancel)
    host_manifest = _host_libraries_manifest(pin.host_libraries, should_cancel)
    return (
        ("kani.jsonl", kani_manifest),
        ("rust.jsonl", rust_manifest),
        ("cargo-bin.jsonl", cargo_manifest),
        ("host-libraries.jsonl", host_manifest),
    )
