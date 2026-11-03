from __future__ import annotations

import hashlib
import json
import stat
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from vkit import kani_toolchain
from vkit.kani_toolchain import (
    KaniToolchain,
    KaniToolchainError,
    validate_kani_toolchain,
)
from vkit.paired_profiles import FilePin

_RUST_TOOLCHAIN = "nightly-2026-08-21-x86_64-unknown-linux-gnu"
_RUSTUP_LINKS = (
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
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_bytes(root: Path) -> bytes:
    entries: list[dict[str, object]] = []
    directories = [Path()]
    while directories:
        relative_directory = directories.pop()
        directory = root / relative_directory
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = relative_directory / path.name
            mode = stat.S_IMODE(path.lstat().st_mode)
            record: dict[str, object] = {"path": relative.as_posix(), "mode": mode}
            if path.is_dir() and not path.is_symlink():
                record["kind"] = "dir"
                entries.append(record)
                directories.append(relative)
            else:
                content = path.read_bytes()
                record.update(
                    {
                        "kind": "file",
                        "size": len(content),
                        "sha256": _sha256(content),
                    }
                )
                entries.append(record)
    entries.sort(key=lambda entry: str(entry["path"]))
    return "".join(
        json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"
        for entry in entries
    ).encode("utf-8")


def _write(path: Path, content: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(mode)


@pytest.fixture
def installed(tmp_path: Path) -> tuple[KaniToolchain, Path]:
    if sys.platform != "linux":
        pytest.skip("toolchain filesystem validation requires Linux")
    kani_root = tmp_path / "cache" / "kani" / "kani-0.68.0"
    rustup_home = tmp_path / "cache" / "rustup"
    rust_root = rustup_home / "toolchains" / _RUST_TOOLCHAIN
    cargo_home = tmp_path / "cache" / "cargo"

    _write(
        kani_root / "rust-toolchain-version",
        b"nightly-2026-08-21-x86_64-unknown-linux-gnu",
    )
    _write(
        kani_root / "rustc-version",
        b"rustc 1.100.0-nightly (8925ea358 2026-08-20)\n",
    )
    _write(kani_root / "bin" / "kani-driver", b"driver", 0o755)
    _write(kani_root / "bin" / "kani-compiler", b"compiler", 0o755)
    _write(rust_root / "bin" / "rustc", b"rustc", 0o755)
    _write(rust_root / "lib" / "librustc_driver-59857489213b2a36.so", b"rustc-driver")
    _write(rust_root / "lib" / "libLLVM.so.23.1-rust-1.100.0-nightly", b"llvm")

    rustup_path = cargo_home / "bin" / "rustup"
    _write(rustup_path, b"rustup", 0o755)
    for name in _RUSTUP_LINKS:
        (cargo_home / "bin" / name).symlink_to("rustup")

    host_library = tmp_path / "host" / "libc.so"
    _write(host_library, b"host-library")
    host_pin = FilePin(str(host_library), _sha256(host_library.read_bytes()))
    pin = KaniToolchain(
        kani_root=kani_root,
        rustup_home=rustup_home,
        cargo_home=cargo_home,
        kani_manifest_sha256=_sha256(_manifest_bytes(kani_root)),
        rust_manifest_sha256=_sha256(_manifest_bytes(rust_root)),
        rustup_sha256=_sha256(rustup_path.read_bytes()),
        host_libraries=(host_pin,),
    )
    return pin, host_library


def test_validates_fixed_profile_and_returns_actual_manifest_bytes(installed):
    pin, _ = installed

    manifests = validate_kani_toolchain(pin)

    assert tuple(name for name, _ in manifests) == (
        "kani.jsonl",
        "rust.jsonl",
        "cargo-bin.jsonl",
        "host-libraries.jsonl",
    )
    assert manifests[0][1] == _manifest_bytes(pin.kani_root)
    assert manifests[1][1] == _manifest_bytes(pin.rust_root)
    assert hashlib.sha256(manifests[0][1]).hexdigest() == pin.kani_manifest_sha256
    assert hashlib.sha256(manifests[1][1]).hexdigest() == pin.rust_manifest_sha256

    cargo_records = [json.loads(line) for line in manifests[2][1].splitlines()]
    assert len(cargo_records) == 14
    assert {row["target"] for row in cargo_records if row["kind"] == "symlink"} == {
        "rustup"
    }
    assert manifests[3][1] == _manifest_bytes_for_host(pin.host_libraries)


def _manifest_bytes_for_host(pins: tuple[FilePin, ...]) -> bytes:
    rows = []
    for pin in sorted(pins, key=lambda item: item.path):
        path = Path(pin.path)
        st = path.stat()
        rows.append(
            {
                "path": str(path),
                "kind": "file",
                "mode": stat.S_IMODE(st.st_mode),
                "size": st.st_size,
                "sha256": _sha256(path.read_bytes()),
            }
        )
    return "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    ).encode("utf-8")


def test_identity_and_environment_are_fixed_and_do_not_inherit_host_values(installed):
    pin, _ = installed

    environment = pin.environment()
    identity = pin.to_json()

    assert set(environment) == {
        "CARGO_HOME",
        "LANG",
        "LD_LIBRARY_PATH",
        "PATH",
        "RUSTUP_HOME",
        "RUSTUP_TOOLCHAIN",
        "TZ",
    }
    assert environment == {
        "CARGO_HOME": str(pin.cargo_home),
        "LANG": "C.UTF-8",
        "LD_LIBRARY_PATH": str(pin.rust_root / "lib"),
        "PATH": f"{pin.kani_root / 'bin'}:{pin.cargo_home / 'bin'}:/usr/bin:/bin",
        "RUSTUP_HOME": str(pin.rustup_home),
        "RUSTUP_TOOLCHAIN": _RUST_TOOLCHAIN,
        "TZ": "UTC",
    }
    assert identity["kind"] == "kani_toolchain"
    assert identity["target"] == "x86_64-unknown-linux-gnu"
    assert identity["host_closure_complete"] is False
    identity["environment"]["PATH"] = "forged"
    assert pin.to_json()["environment"] == environment
    with pytest.raises(FrozenInstanceError):
        pin.rustup_sha256 = "0" * 64


@pytest.mark.parametrize(
    ("field", "content"),
    [
        ("kani_manifest_sha256", "changed"),
        ("rust_manifest_sha256", "changed"),
        ("rustup_sha256", "changed"),
    ],
)
def test_rejects_invalid_pin_digest_syntax(installed, field, content):
    pin, _ = installed
    changed = replace(pin, **{field: content})

    with pytest.raises(KaniToolchainError) as error:
        validate_kani_toolchain(changed)

    assert error.value.reason == "unsupported_installation"


@pytest.mark.parametrize("field", ["kani_manifest_sha256", "rust_manifest_sha256"])
def test_rejects_changed_tree_bytes_or_modes(installed, field):
    pin, _ = installed
    target = (
        pin.kani_root / "bin" / "kani-compiler"
        if field.startswith("kani")
        else pin.rust_root / "bin" / "rustc"
    )
    original = target.read_bytes()
    target.write_bytes(original + b"changed")
    if field == "rust_manifest_sha256":
        target.chmod(0o700)

    with pytest.raises(KaniToolchainError) as error:
        validate_kani_toolchain(pin)

    assert error.value.reason == "pin_mismatch"


@pytest.mark.parametrize("change", ["add", "delete"])
def test_rejects_added_or_missing_tree_entries(installed, change):
    pin, _ = installed
    extra = pin.kani_root / "unexpected.bin"
    if change == "add":
        extra.write_bytes(b"unexpected")
    else:
        (pin.kani_root / "bin" / "kani-compiler").unlink()

    with pytest.raises(KaniToolchainError) as error:
        validate_kani_toolchain(pin)

    assert error.value.reason in {"pin_mismatch", "unavailable"}


def test_rejects_internal_symlinks_and_symlinked_install_roots(installed, tmp_path):
    pin, _ = installed
    (pin.kani_root / "linked.bin").symlink_to(pin.kani_root / "rustc-version")
    with pytest.raises(KaniToolchainError) as internal_error:
        validate_kani_toolchain(pin)
    assert internal_error.value.reason == "unsupported_installation"

    link_root = tmp_path / "kani-link"
    link_root.symlink_to(pin.kani_root, target_is_directory=True)
    linked_pin = KaniToolchain(
        link_root,
        pin.rustup_home,
        pin.cargo_home,
        pin.kani_manifest_sha256,
        pin.rust_manifest_sha256,
        pin.rustup_sha256,
        pin.host_libraries,
    )
    with pytest.raises(KaniToolchainError) as root_error:
        validate_kani_toolchain(linked_pin)
    assert root_error.value.reason == "unsupported_installation"


def test_rejects_wrong_cargo_proxy_layout_or_rustup_digest(installed):
    pin, _ = installed
    proxy = pin.cargo_home / "bin" / "cargo"
    proxy.unlink()
    proxy.symlink_to("./rustup")
    with pytest.raises(KaniToolchainError) as proxy_error:
        validate_kani_toolchain(pin)
    assert proxy_error.value.reason == "unsupported_installation"

    proxy.unlink()
    proxy.symlink_to("rustup")
    rustup = pin.cargo_home / "bin" / "rustup"
    rustup.write_bytes(b"mutated")
    with pytest.raises(KaniToolchainError) as rustup_error:
        validate_kani_toolchain(pin)
    assert rustup_error.value.reason == "pin_mismatch"


def test_rejects_unpinned_host_library_changes(installed):
    pin, host_library = installed
    host_library.write_bytes(b"mutated host library")

    with pytest.raises(KaniToolchainError) as error:
        validate_kani_toolchain(pin)

    assert error.value.reason == "pin_mismatch"


def test_cancellation_is_checked_during_large_file_hashing(installed):
    pin, _ = installed
    (pin.kani_root / "0-large.bin").write_bytes(b"x" * (4 * 1024 * 1024))
    pin = KaniToolchain(
        pin.kani_root,
        pin.rustup_home,
        pin.cargo_home,
        _sha256(_manifest_bytes(pin.kani_root)),
        pin.rust_manifest_sha256,
        pin.rustup_sha256,
        pin.host_libraries,
    )
    calls = 0

    def should_cancel() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 7

    with pytest.raises(KaniToolchainError) as error:
        validate_kani_toolchain(pin, should_cancel=should_cancel)

    assert error.value.reason == "cancelled"
    assert calls == 7


def test_rejects_missing_roots_and_unsupported_embedded_toolchain(installed, tmp_path):
    pin, _ = installed
    missing = KaniToolchain(
        tmp_path / "missing",
        pin.rustup_home,
        pin.cargo_home,
        pin.kani_manifest_sha256,
        pin.rust_manifest_sha256,
        pin.rustup_sha256,
        pin.host_libraries,
    )
    with pytest.raises(KaniToolchainError) as missing_error:
        validate_kani_toolchain(missing)
    assert missing_error.value.reason == "unavailable"

    marker = pin.kani_root / "rust-toolchain-version"
    marker.write_bytes(b"nightly-unsupported")
    with pytest.raises(KaniToolchainError) as version_error:
        validate_kani_toolchain(pin)
    assert version_error.value.reason == "unsupported_installation"


def test_rejects_non_linux_and_wrong_architecture_before_touching_paths(
    monkeypatch,
):
    pin = KaniToolchain(
        Path("relative-kani"),
        Path("relative-rustup"),
        Path("relative-cargo"),
        "0" * 64,
        "0" * 64,
        "0" * 64,
    )
    monkeypatch.setattr(kani_toolchain.platform, "system", lambda: "Windows")
    with pytest.raises(KaniToolchainError) as os_error:
        validate_kani_toolchain(pin)
    assert os_error.value.reason == "unsupported_platform"

    monkeypatch.setattr(kani_toolchain.platform, "system", lambda: "Linux")
    monkeypatch.setattr(kani_toolchain.platform, "machine", lambda: "aarch64")
    with pytest.raises(KaniToolchainError) as arch_error:
        validate_kani_toolchain(pin)
    assert arch_error.value.reason == "unsupported_platform"
