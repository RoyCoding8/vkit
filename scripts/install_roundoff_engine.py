"""Build the SHA-pinned Gappa release on a provisioned Linux host."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

URL = "https://gappa.gitlabpages.inria.fr/releases/gappa-1.8.3.tar.gz"
SOURCE_SHA256 = "9b3e14e95a5b6aa0ee909b1d1e5c1fac04042024f29b66377cfb9e1923b25f5c"


def install(output: Path) -> None:
    if os.name != "posix":
        raise RuntimeError("this installer requires the documented Linux build dependencies")
    with urllib.request.urlopen(URL, timeout=60) as response:
        archive = response.read(10_000_001)
    if len(archive) > 10_000_000 or hashlib.sha256(archive).hexdigest() != SOURCE_SHA256:
        raise RuntimeError("Gappa source release identity mismatch")
    with tempfile.TemporaryDirectory(prefix="vkit-gappa-build-") as temporary:
        directory = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            bundle.extractall(directory, filter="data")
        source = directory / "gappa-1.8.3"
        subprocess.run(["./configure"], cwd=source, check=True, timeout=120)
        subprocess.run(["./remake", "-j2"], cwd=source, check=True, timeout=600)
        binary = source / "src/gappa"
        result = subprocess.run([str(binary), "--version"], capture_output=True, check=True, timeout=10)
        if result.stdout != b"Gappa 1.8.3\n" or result.stderr:
            raise RuntimeError("built Gappa release identity mismatch")
        output.parent.mkdir(parents=True, exist_ok=True)
        staged = output.with_suffix(".staged")
        shutil.copyfile(binary, staged)
        staged.chmod(0o755)
        os.replace(staged, output)
    receipt = {"source_url": URL, "source_sha256": SOURCE_SHA256,
               "binary_sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
    output.with_suffix(".json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    install(parser.parse_args().output.resolve())
