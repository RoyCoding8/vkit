"""Provision the SHA-pinned Linux Frama-C bundle without running its system installer."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tempfile
import urllib.request
from pathlib import Path

URL = "https://www.frama-c.com/download/frama-c-linux-x86-64-33.0-Arsenic.run"
SHA256 = "c754c31bb32d151acf7db3bcfdc5181b076bdbff813b19049e984f639eba3735"
MAX_BYTES = 260_000_000


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def install(destination: Path) -> dict[str, str]:
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("this installer requires Linux x86_64")
    destination = destination.expanduser().absolute()
    marker = destination / "receipt.json"
    binary = destination / "bundle/bin/frama-c"
    if destination.exists():
        receipt = json.loads(marker.read_text(encoding="utf-8"))
        if receipt.get("archive_sha256") != SHA256 or receipt.get("binary_sha256") != _digest(binary):
            raise RuntimeError("existing runtime does not match its receipt; choose a fresh destination")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="vkit-frama-install-", dir=destination.parent) as temporary:
            staging = Path(temporary)
            archive = staging / "frama-c.run"
            digest = hashlib.sha256()
            size = 0
            with urllib.request.urlopen(URL, timeout=60) as response, archive.open("wb") as output:
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > MAX_BYTES:
                        raise RuntimeError("Frama-C download exceeded its fixed byte limit")
                    digest.update(block)
                    output.write(block)
            if digest.hexdigest() != SHA256:
                raise RuntimeError("Frama-C release archive SHA-256 mismatch")
            payload = staging / "payload"
            payload.mkdir()
            # Makeself --noexec extracts the pinned payload without invoking install.sh.
            subprocess.run(["sh", str(archive), "--noexec", "--target", str(payload / "bundle")],
                           check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            staged_binary = payload / "bundle/bin/frama-c"
            version = subprocess.run([str(staged_binary), "-version"], check=True, capture_output=True, timeout=10)
            if version.stdout.strip() != b"33.0 (Arsenic)" or version.stderr:
                raise RuntimeError("Frama-C release version mismatch")
            receipt = {"url": URL, "archive_sha256": SHA256, "binary_sha256": _digest(staged_binary)}
            (payload / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
            os.replace(payload, destination)
    return {"VKIT_FRAMAC_BIN": str(binary), "VKIT_FRAMAC_SHA256": receipt["binary_sha256"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, required=True)
    print(json.dumps(install(parser.parse_args().dest)))
