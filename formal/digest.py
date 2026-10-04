"""The one place a receipt decides what bytes it is a receipt for.

Git stores LF. A checkout on a host with `core.autocrlf=true` — which this
repository's own `.gitattributes` produces for every extension it does not pin
to `eol=lf`, and `.tla` and `.lean` are two of them — writes those same files
back out with CRLF. Hashing `path.read_bytes()` therefore digests the checkout's
line endings, not the file: the identical committed artifact yields a different
digest on Windows than on Linux, and a receipt written on one host cannot be
checked on the other.

So a receipt digests the canonical form, CRLF folded to LF, which is the form
git holds and the form every other platform reads. The bound that leaves is
stated rather than hidden: a change to line endings alone is not a change to the
artifact and does not alter the digest.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def canonical_sha256(path: Path) -> str:
    """The sha256 of a file's content with CRLF folded to LF."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


NORMALIZATION = (
    "Digests are sha256 over the file's bytes with CRLF folded to LF, which is "
    "the form git stores and the form a POSIX checkout reads. A line-ending-only "
    "change does not alter the digest."
)
