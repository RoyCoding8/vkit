"""Install the pinned Joern Linux x86_64 distribution with archive verification."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
import zipfile
from pathlib import Path, PurePosixPath

VERSION = "4.0.653"
ARCHIVE_NAME = "joern-cli-linux-x86_64.zip"
ARCHIVE_URL = f"https://github.com/joernio/joern/releases/download/v{VERSION}/{ARCHIVE_NAME}"
ARCHIVE_SHA256 = "6cf996f2787381cd789322df52d134dc0b189a2f072f208bb4674a7f09edfb73"
MAX_ARCHIVE_BYTES = 1_900_000_000
MAX_EXPANDED_BYTES = 12_000_000_000
MAX_ENTRIES = 100_000


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _safe_members(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ValueError("Joern archive has too many entries")
    expanded = 0
    result: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    names: set[str] = set()
    for info in infos:
        name = info.filename.replace("\\", "/")
        relative = PurePosixPath(name)
        mode = info.external_attr >> 16
        if (not name or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts)
                or name.startswith("/") or (mode and stat.S_ISLNK(mode))):
            raise ValueError("Joern archive contains an unsafe path or symlink")
        key = relative.as_posix().casefold()
        if key in names:
            raise ValueError("Joern archive contains duplicate paths")
        names.add(key)
        expanded += info.file_size
        if expanded > MAX_EXPANDED_BYTES:
            raise ValueError("Joern archive exceeds the expanded-size limit")
        result.append((info, relative))
    return result


def _download(destination: Path) -> None:
    digest = hashlib.sha256()
    total = 0
    request = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "vkit-engine-installer"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        while block := response.read(4 * 1024 * 1024):
            total += len(block)
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError("Joern archive exceeds the download-size limit")
            output.write(block)
            digest.update(block)
        output.flush()
        os.fsync(output.fileno())
    if digest.hexdigest() != ARCHIVE_SHA256:
        raise ValueError("Joern archive SHA-256 did not match the pinned release")


def install(cache_dir: Path, archive_path: Path | None = None) -> Path:
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("this installer supports only Linux x86_64")
    cache_dir = cache_dir.expanduser().resolve()
    destination = cache_dir / VERSION
    marker = destination / ".vkit-joern.json"
    if marker.is_file():
        try:
            metadata = json.loads(marker.read_text(encoding="utf-8"))
            if metadata.get("version") == VERSION and metadata.get("archive_sha256") == ARCHIVE_SHA256 \
                    and metadata.get("joern_sha256") == _digest(destination / "joern") \
                    and metadata.get("joern_parse_sha256") == _digest(destination / "joern-parse"):
                return destination
        except (OSError, ValueError, TypeError):
            pass
    cache_dir.mkdir(parents=True, exist_ok=True)
    if archive_path is None:
        archive_path = cache_dir / ARCHIVE_NAME
        if not archive_path.is_file() or _digest(archive_path) != ARCHIVE_SHA256:
            with tempfile.NamedTemporaryFile(prefix="joern-download-", suffix=".zip",
                                             dir=cache_dir, delete=False) as temporary:
                download_path = Path(temporary.name)
            try:
                _download(download_path)
                os.replace(download_path, archive_path)
            finally:
                download_path.unlink(missing_ok=True)
    archive_path = archive_path.resolve(strict=True)
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES or _digest(archive_path) != ARCHIVE_SHA256:
        raise ValueError("Joern archive SHA-256 did not match the pinned release")

    staging = Path(tempfile.mkdtemp(prefix="joern-install-", dir=cache_dir))
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = _safe_members(archive)
            for info, relative in members:
                target = staging.joinpath(*relative.parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 4 * 1024 * 1024)
                mode = info.external_attr >> 16
                if mode & 0o111:
                    target.chmod(0o755)
        candidates = list(staging.rglob("joern"))
        candidates = [item for item in candidates if item.is_file() and "bin" not in item.parts]
        pair = next(((cli, cli.parent / "joern-parse") for cli in candidates
                     if (cli.parent / "joern-parse").is_file()), None)
        if pair is None:
            raise ValueError("Joern archive did not contain the expected CLI pair")
        cli, parser = pair
        # Preserve the complete release tree: the launchers resolve their runtime beside them.
        install_root = staging / "payload"
        shutil.copytree(cli.parent, install_root)
        (install_root / "joern").chmod(0o755)
        (install_root / "joern-parse").chmod(0o755)
        version_probe = subprocess.run([str(install_root / "joern"), "--version"],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace",
                                       timeout=60, check=False)
        version_text = version_probe.stdout.strip()[-2_000:]
        if version_probe.returncode != 0 or VERSION not in version_text:
            raise RuntimeError(f"Joern version probe failed: {version_text}")
        metadata = {
            "version": VERSION,
            "archive_sha256": ARCHIVE_SHA256,
            "joern_sha256": _digest(install_root / "joern"),
            "joern_parse_sha256": _digest(install_root / "joern-parse"),
            "version_output": version_text,
        }
        (install_root / ".vkit-joern.json").write_text(
            json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(install_root, destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path,
                        default=Path.home() / ".cache" / "vkit" / "joern")
    parser.add_argument("--archive", type=Path,
                        help="use an already downloaded archive after verifying its SHA-256")
    args = parser.parse_args()
    try:
        destination = install(args.cache_dir, args.archive)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile,
            urllib.error.URLError, subprocess.SubprocessError) as exc:
        print(f"Joern installation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"home": str(destination), "version": VERSION,
                      "archive_sha256": ARCHIVE_SHA256}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
