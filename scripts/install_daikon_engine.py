"""Install one verified Daikon release JAR without extracting archive paths."""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import tempfile
import urllib.request
import zipfile
from pathlib import Path

URL = "https://plse.cs.washington.edu/daikon/download/daikon-5.9.0.zip"
ARCHIVE_SHA256 = "e66c640bdb0c4486aa585037d38b8ad5b139c7f9a866bf8cbe938ee09fd2324d"
JAR_SHA256 = "b795a58df12d0c9a7049dca9a4dc3d922cbfc215b8099502747fa09d024fbe32"


def install(output: Path) -> None:
    if output.is_file():
        with output.open("rb") as source:
            existing = source.read(10_000_001)
        if len(existing) <= 10_000_000 and hashlib.sha256(existing).hexdigest() == JAR_SHA256:
            return
    with urllib.request.urlopen(URL, timeout=60) as response:
        content = response.read(150_000_001)
    if len(content) > 150_000_000 or hashlib.sha256(content).hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError("Daikon distribution identity mismatch")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = [entry for entry in archive.infolist() if entry.filename.endswith("/daikon.jar")]
        if len(entries) != 1 or entries[0].file_size > 10_000_000:
            raise RuntimeError("Daikon distribution has no unique bounded JAR")
        jar = archive.read(entries[0])
    if hashlib.sha256(jar).hexdigest() != JAR_SHA256:
        raise RuntimeError("Daikon JAR identity mismatch")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as temporary:
        temporary.write(jar)
        staged = Path(temporary.name)
    try:
        os.replace(staged, output)
    finally:
        staged.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    install(parser.parse_args().output.resolve())
