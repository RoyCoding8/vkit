"""Install release-pinned Cedar SymCC and cvc5 binaries for local or CI verification."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO

_CEDAR_RELEASE = "cedar-policy-cli-v4.13.0"
_CVC5_RELEASE = "cvc5-1.3.1"
_MAX_ARCHIVE_BYTES = 524_288_000
_RELEASES = {
    ("Windows", "x86_64"): (
        ("cedar-policy-cli-experimental-x86_64-pc-windows-msvc.zip",
         "96d91a32849b73d4e2d010dd4b78685897cd9d8baa5c2ce19b660c8db43919a9"),
        ("cvc5-Win64-x86_64-static.zip",
         "e1e183f50f1bbd9d10b2dc6ab9574cb9fa02acfdc1957f43040db3529f92b94e"),
    ),
    ("Linux", "x86_64"): (
        ("cedar-policy-cli-experimental-x86_64-unknown-linux-gnu.tar.xz",
         "deb3a8340e357b871eb7c3e2a701f9a4f2e2f8404ec249740b1ed9eef5a0aefe"),
        ("cvc5-Linux-x86_64-static.zip",
         "1a1cda20d2df4938fa4944a69f33ddc9172e319ece0eed0aa09c4d7abede3ed1"),
    ),
    ("Linux", "aarch64"): (
        ("cedar-policy-cli-experimental-aarch64-unknown-linux-gnu.tar.xz",
         "c80cb50cf0bc4f71bf4f2decd1f5baf835ce52447d3b48b6842da2773814c6d6"),
        ("cvc5-Linux-arm64-static.zip",
         "fe2b661834a82fd8830f7a757c340f0e20041fa41e19b038fa02ace0eaf1c6f2"),
    ),
    ("Darwin", "x86_64"): (
        ("cedar-policy-cli-experimental-x86_64-apple-darwin.tar.xz",
         "a5048ea398557206566792b913a1def83c896588b16816509c8b572d58e8eca1"),
        ("cvc5-macOS-x86_64-static.zip",
         "e7fe4af9491bd7c0db7591c0a483775735bd1a98b23933fd337a73ae39c10ff9"),
    ),
    ("Darwin", "aarch64"): (
        ("cedar-policy-cli-experimental-aarch64-apple-darwin.tar.xz",
         "0ce877eed8a3b4f35e93f06a177960e5dcd75274d084880b0e72044f5306c2e1"),
        ("cvc5-macOS-arm64-static.zip",
         "a0e7f5b03b1bc4284fbfff7cdfb08c704801701cf7ece83a13f8a505e7581215"),
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, digest: str, destination: Path) -> None:
    hasher = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=30) as response, destination.open("wb") as output:
            size = 0
            while chunk := response.read(1_048_576):
                size += len(chunk)
                if size > _MAX_ARCHIVE_BYTES:
                    raise RuntimeError("release archive exceeded the 500 MiB download limit")
                hasher.update(chunk)
                output.write(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    if hasher.hexdigest() != digest:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"downloaded release archive failed its SHA-256 check: {url}")


def _safe_member(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    return not path.is_absolute() and all(part not in ("", ".", "..") for part in path.parts)


def _copy_binary(archive: Path, binary_name: str, destination: Path) -> None:
    candidates: list[tuple[str, BinaryIO]] = []
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as package:
            for info in package.infolist():
                if info.is_dir() or Path(info.filename).name != binary_name or not _safe_member(info.filename):
                    continue
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    continue
                candidates.append((info.filename, package.open(info, "r")))
            try:
                _copy_candidate(candidates, destination)
            finally:
                for _, content in candidates:
                    content.close()
        return
    with tarfile.open(archive, mode="r:xz") as package:
        for member in package.getmembers():
            if not member.isfile() or PurePosixPath(member.name).name != binary_name or not _safe_member(member.name):
                continue
            content = package.extractfile(member)
            if content is not None:
                candidates.append((member.name, content))
        try:
            _copy_candidate(candidates, destination)
        finally:
            for _, content in candidates:
                content.close()


def _copy_candidate(candidates: list[tuple[str, BinaryIO]], destination: Path) -> None:
    if len(candidates) != 1:
        raise RuntimeError(f"release archive must contain exactly one {destination.name} executable")
    temporary = destination.with_name(destination.name + ".installing")
    with temporary.open("wb") as output:
        shutil.copyfileobj(candidates[0][1], output)
    temporary.chmod(0o755)
    temporary.replace(destination)


def _install_archive(base_url: str, archive_name: str, digest: str, binary_name: str,
                     destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / binary_name
    with tempfile.TemporaryDirectory(prefix="vkit-policy-install-") as temporary:
        archive = Path(temporary) / archive_name
        _download(f"{base_url}/{archive_name}", digest, archive)
        _copy_binary(archive, binary_name, target)
    return target


def _identity(path: Path, expected: str) -> None:
    result = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=15, check=False)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0 or expected not in output:
        raise RuntimeError(f"installed engine version did not match {expected!r}: {output[:240]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, help="binary install directory (defaults to the user cache)")
    args = parser.parse_args()
    system = platform.system()
    machine = platform.machine().lower()
    architecture = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    release = _RELEASES.get((system, architecture))
    if release is None:
        raise RuntimeError(f"no Cedar SymCC and cvc5 release pair is pinned for {system}/{architecture}")
    cedar_archive, cvc5_archive = release
    suffix = ".exe" if system == "Windows" else ""
    destination = args.dest or Path.home() / ".cache" / "vkit" / "policy-engine-4.13.0"
    cedar = _install_archive(f"https://github.com/cedar-policy/cedar/releases/download/{_CEDAR_RELEASE}",
                             *cedar_archive, f"cedar{suffix}", destination)
    cvc5 = _install_archive(f"https://github.com/cvc5/cvc5/releases/download/{_CVC5_RELEASE}",
                             *cvc5_archive, f"cvc5{suffix}", destination)
    _identity(cedar, "cedar-policy-cli 4.13.0")
    _identity(cvc5, "1.3.1")
    print(json.dumps({"VKIT_CEDAR_PATH": str(cedar.resolve()),
                      "VKIT_CEDAR_SHA256": _sha256(cedar),
                      "VKIT_CVC5_PATH": str(cvc5.resolve()),
                      "VKIT_CVC5_SHA256": _sha256(cvc5),
                      "CVC5": str(cvc5.resolve())}, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"policy engine install failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
