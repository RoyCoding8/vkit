from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any

from ..inputs import canonical_digest, is_secret
from ..requests import PackageClaim

OPERATIONS = ("new", "reserve", "release", "reserve_batch", "apply_command", "run_commands")
_RUST_KEYWORDS = frozenset("as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while abstract become box do final macro override priv typeof unsized virtual yield try gen".split())


@dataclass(frozen=True)
class PackageIdentity:
    id: str
    version: str
    sha256: str


@dataclass(frozen=True)
class LedgerBindings:
    new: str
    reserve: str
    release: str
    reserve_batch: str
    apply_command: str
    run_commands: str

    def __post_init__(self) -> None:
        for operation, path in asdict(self).items():
            _rust_path(path, operation)


def _rust_path(path: str, label: str) -> None:
    if (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*", path) is None
            or any(part in _RUST_KEYWORDS or part == "_" for part in path.split("::"))):
        raise ValueError(f"{label}: binding must be a relative Rust identifier path")


@dataclass(frozen=True)
class FormalComponent:
    id: str
    package: PackageIdentity
    contract: str
    entry: str
    profile: str
    max_records: int
    bindings: LedgerBindings
    required: bool
    representation: str | None = None

    def __post_init__(self) -> None:
        if self.representation is not None:
            _rust_path(self.representation, "representation")

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> FormalComponent:
        entry = raw["source"]["entry"]
        path = PurePosixPath(entry)
        if (path.is_absolute() or path.as_posix() != entry or ".." in path.parts
                or "\\" in entry or ":" in entry or path.suffix != ".rs"
                or ".git" in path.parts or is_secret(entry)):
            raise ValueError("formal source.entry must be a normalized repository-relative Rust file")
        return cls(raw["id"], PackageIdentity(**raw["package"]), raw["contract"], entry,
                   raw["profile"], raw["parameters"]["max_records"],
                   LedgerBindings(**raw["bindings"]), raw["required"], raw.get("representation"))

    def to_json(self) -> dict[str, Any]:
        value = {"id": self.id, "package": asdict(self.package), "contract": self.contract,
                "source": {"entry": self.entry}, "profile": self.profile,
                "parameters": {"max_records": self.max_records},
                "bindings": asdict(self.bindings), "required": self.required}
        if self.representation is not None:
            value["representation"] = self.representation
        return value

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_json())

    @property
    def claim(self) -> PackageClaim:
        return PackageClaim(self.id, self.digest, self.package.sha256)

    def is_accepted(self, approval: dict[str, Any], checker_sha256: str) -> bool:
        return (approval.get("kind") == "formal_policy"
                and approval.get("policy_sha256") == self.digest
                and approval.get("package_sha256") == self.package.sha256
                and approval.get("checker_sha256") == checker_sha256)
