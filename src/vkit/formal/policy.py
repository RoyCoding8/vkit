from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any

from ..inputs import canonical_digest, is_secret
from ..schemas import VERIFICATION_REQUEST, validate

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
            if any(part in _RUST_KEYWORDS or part == "_" for part in path.split("::")):
                raise ValueError(f"{operation}: binding must be a relative Rust identifier path")


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
                   LedgerBindings(**raw["bindings"]), raw["required"])

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "package": asdict(self.package), "contract": self.contract,
                "source": {"entry": self.entry}, "profile": self.profile,
                "parameters": {"max_records": self.max_records},
                "bindings": asdict(self.bindings), "required": self.required}

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_json())


@dataclass(frozen=True)
class WorkingTree:
    def to_json(self) -> dict[str, str]:
        return {"kind": "working_tree"}


@dataclass(frozen=True)
class GitCommit:
    oid: str
    target_commit: str | None = None

    def to_json(self) -> dict[str, str]:
        value = {"kind": "git_commit", "oid": self.oid}
        if self.target_commit is not None:
            value["target_commit"] = self.target_commit
        return value


@dataclass(frozen=True)
class VerificationRequest:
    component: str
    candidate: WorkingTree | GitCommit
    wait_seconds: int = 0

    @classmethod
    def from_json(cls, raw: Any) -> VerificationRequest:
        validate("verification request", VERIFICATION_REQUEST, raw)
        source = raw["candidate"]
        candidate = (WorkingTree() if source["kind"] == "working_tree"
                     else GitCommit(source["oid"], source.get("target_commit")))
        return cls(raw["component"], candidate, raw.get("wait_seconds", 0))

    def semantic_json(self) -> dict[str, Any]:
        return {"component": self.component, "goal": {"kind": "implements"},
                "candidate": self.candidate.to_json()}
