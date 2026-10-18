"""Install the Linux x86_64 Kani 0.68.0 runtime in a caller-selected directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

VERSION = "0.68.0"
CBMC = "6.11.0"
TOOLCHAIN = "nightly-2026-08-21"
RUSTC = "rustc 1.100.0-nightly (8925ea358 2026-08-20)"
BUNDLE_NAME = f"kani-{VERSION}-x86_64-unknown-linux-gnu.tar.gz"
BUNDLE_URL = f"https://github.com/model-checking/kani/releases/download/kani-{VERSION}/{BUNDLE_NAME}"
BUNDLE_SHA256 = "32e2b484d73ede0bbf64a2cf0879c4259422497d8aec4ee67d448de9ae7843d3"
MAX_BUNDLE_BYTES = 512 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download_bundle(target: Path) -> None:
    temporary = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(BUNDLE_URL, timeout=30) as response, temporary.open("wb") as output:
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > MAX_BUNDLE_BYTES:
                raise RuntimeError("Kani release bundle exceeds the download limit")
            total = 0
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > MAX_BUNDLE_BYTES:
                    raise RuntimeError("Kani release bundle exceeds the download limit")
                output.write(block)
        if _sha256(temporary) != BUNDLE_SHA256:
            raise RuntimeError("downloaded Kani release bundle failed its pinned SHA-256 check")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _run(command: list[str], env: dict[str, str], cwd: Path) -> None:
    result = subprocess.run(command, cwd=cwd, env=env, check=False)
    if result.returncode:
        raise RuntimeError(f"installation command failed with exit code {result.returncode}: {command[0]}")


def install(root: Path) -> dict[str, str]:
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("the pinned Kani installer supports Linux x86_64 only")
    cargo = shutil.which("cargo")
    rustup = shutil.which("rustup")
    if cargo is None or rustup is None:
        raise RuntimeError("install Rust and rustup first; cargo and rustup must be on PATH")

    root.mkdir(parents=True, exist_ok=True)
    kani_home = root / "kani-home"
    cargo_home = root / "cargo-home"
    rustup_home = root / "rustup-home"
    cargo_root = root / "cargo"
    for directory in (kani_home, cargo_home, rustup_home, cargo_root):
        directory.mkdir(parents=True, exist_ok=True)
    bundle = kani_home / BUNDLE_NAME
    if bundle.exists():
        if _sha256(bundle) != BUNDLE_SHA256:
            raise RuntimeError(f"existing release bundle has the wrong SHA-256: {bundle}")
    else:
        _download_bundle(bundle)
    bundle_cache_dir = root / "bundle-cache"
    bundle_cache_dir.mkdir(exist_ok=True)
    bundle_cache = bundle_cache_dir / BUNDLE_NAME
    if bundle_cache.exists():
        if _sha256(bundle_cache) != BUNDLE_SHA256:
            raise RuntimeError(f"retained release bundle has the wrong SHA-256: {bundle_cache}")
    else:
        shutil.copy2(bundle, bundle_cache)
        if _sha256(bundle_cache) != BUNDLE_SHA256:
            raise RuntimeError("the retained Kani release bundle failed its pinned SHA-256 check")

    bootstrap_env = os.environ.copy()
    bootstrap_env.update({
        "CARGO_HOME": str(cargo_home),
        "CARGO_TARGET_DIR": str(root / "cargo-target"),
    })
    bootstrap_env["PATH"] = os.pathsep.join((str(cargo_root / "bin"), str(cargo_home / "bin"),
                                               bootstrap_env.get("PATH", "")))
    _run([cargo, "install", "--locked", "kani-verifier", "--version", VERSION,
          "--root", str(cargo_root), "--force"], bootstrap_env, root)
    rustup_target = cargo_root / "bin" / "rustup"
    if Path(rustup).resolve() != rustup_target.resolve():
        shutil.copy2(rustup, rustup_target)
    isolated_rustup = cargo_home / "bin" / "rustup"
    isolated_rustup.parent.mkdir(parents=True, exist_ok=True)
    if Path(rustup).resolve() != isolated_rustup.resolve():
        shutil.copy2(rustup, isolated_rustup)
    env = bootstrap_env.copy()
    env.update({"KANI_HOME": str(kani_home), "RUSTUP_HOME": str(rustup_home)})
    # Invoke the installed cargo subcommand directly. Sending `cargo kani`
    # through the host rustup shim after switching RUSTUP_HOME would make it
    # seek a default compiler in the still-empty isolated Rustup home.
    cargo_kani = cargo_root / "bin" / "cargo-kani"
    if not cargo_kani.is_file():
        raise RuntimeError("cargo install did not produce the pinned cargo-kani setup executable")
    _run([str(cargo_kani), "setup"], env, root)

    binary = cargo_root / "bin" / "kani"
    expected_version = f"Kani Rust Verifier {VERSION} (standalone)\nCBMC {CBMC}\n"
    version = subprocess.run([str(binary), "--version"], cwd=root, env=env,
                             check=False, capture_output=True, text=True, timeout=30)
    if version.returncode or version.stdout.splitlines()[-2:] != expected_version.splitlines():
        raise RuntimeError("installed Kani executable did not report the pinned version")
    if _sha256(bundle_cache) != BUNDLE_SHA256:
        raise RuntimeError("the retained Kani release bundle failed its pinned SHA-256 check")
    if not (kani_home / f"kani-{VERSION}" / "bin" / "cbmc").is_file():
        raise RuntimeError("Kani setup did not install the release bundle in the expected layout")
    toolchain = rustup_home / "toolchains" / f"{TOOLCHAIN}-x86_64-unknown-linux-gnu" / "bin" / "rustc"
    rustc = subprocess.run([str(toolchain), "--version"], cwd=root, env=env,
                           check=False, capture_output=True, text=True, timeout=30)
    if rustc.returncode or rustc.stdout.strip() != RUSTC or rustc.stderr:
        raise RuntimeError("Kani setup did not install the pinned Rust toolchain")

    configuration = {
        "VKIT_KANI_BIN": str(binary.resolve()),
        "VKIT_KANI_SHA256": _sha256(binary),
        "VKIT_KANI_HOME": str(kani_home.resolve()),
        "VKIT_RUSTUP_HOME": str(rustup_home.resolve()),
    }
    output = root / "vkit-kani-runtime.json"
    output.write_text(json.dumps(configuration, indent=2) + "\n", encoding="utf-8")
    return {**configuration, "configuration_file": str(output.resolve())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path,
                        help="isolated installation root; no system tool directories are modified")
    arguments = parser.parse_args()
    try:
        result = install(arguments.root.expanduser().resolve())
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"install_rust_engine: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
