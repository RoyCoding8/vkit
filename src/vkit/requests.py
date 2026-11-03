from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schemas import VERIFICATION_REQUEST, validate


@dataclass(frozen=True)
class PackageClaim:
    component: str
    policy_sha256: str
    package_sha256: str

    def to_json(self) -> dict[str, str]:
        return {"kind": "package", "component": self.component,
                "policy_sha256": self.policy_sha256, "package_sha256": self.package_sha256}


@dataclass(frozen=True)
class WorkingTree:
    def to_json(self) -> dict[str, str]:
        return {"kind": "working_tree"}


@dataclass(frozen=True)
class GitCommit:
    oid: str

    def to_json(self) -> dict[str, str]:
        return {"kind": "git_commit", "oid": self.oid}


@dataclass(frozen=True)
class PackageVerificationRequest:
    claim: PackageClaim
    candidate: WorkingTree | GitCommit
    wait_seconds: int = 0
    reuse: bool = False

    @classmethod
    def from_json(cls, raw: Any) -> PackageVerificationRequest:
        validate("verification request", VERIFICATION_REQUEST, raw)
        claim, source = raw["claim"], raw["candidate"]
        candidate = (WorkingTree() if source["kind"] == "working_tree"
                     else GitCommit(source["oid"]))
        return cls(PackageClaim(claim["component"], claim["policy_sha256"], claim["package_sha256"]),
                   candidate, raw.get("wait_seconds", 0), raw.get("reuse", False))

    def semantic_json(self) -> dict[str, Any]:
        return {"schema_version": 2, "claim": self.claim.to_json(),
                "candidate": self.candidate.to_json()}

    def to_json(self) -> dict[str, Any]:
        return {**self.semantic_json(), "wait_seconds": self.wait_seconds, "reuse": self.reuse}
