from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .policy import FormalComponent, PackageIdentity

PACKAGE_ID = "vkit.resource-ledger"
VERSION = "0.1.0"
CONTRACT = "ledger.source.v1"
PROFILE = "verus-linux-x86_64-v1"
_ROOT = Path(__file__).with_name("packages") / "resource_ledger"
_ASSETS = ("ledger.rs", "expected.rs.in", "upstream.json")


@dataclass(frozen=True)
class LedgerPackage:
    root: Path
    identity: PackageIdentity
    pins: dict[str, Any]


def load_package() -> LedgerPackage:
    from .policy import PackageIdentity

    files = {name: hashlib.sha256((_ROOT / name).read_bytes()).hexdigest() for name in _ASSETS}
    identity = {"id": PACKAGE_ID, "version": VERSION, "contract": CONTRACT,
                "profile": PROFILE, "files": files}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return LedgerPackage(_ROOT, PackageIdentity(PACKAGE_ID, VERSION, digest),
                         json.loads((_ROOT / "upstream.json").read_bytes()))


def verify_package(component: FormalComponent) -> LedgerPackage:
    package = load_package()
    if (component.package != package.identity or component.contract != CONTRACT
            or component.profile != PROFILE):
        raise ValueError("component policy does not match the installed ledger package and profile")
    return package


def render_expected(component: FormalComponent) -> str:
    template = (_ROOT / "expected.rs.in").read_text(encoding="utf-8")
    expected = template.replace("__VKIT_RECORDS__", str(component.max_records))
    for operation, path in asdict(component.bindings).items():
        expected = expected.replace(f"__VKIT_BIND_{operation.upper()}__", path)
    if "__VKIT_" in expected:
        raise ValueError("the owned expected template has an unresolved parameter")
    return expected


def obligation_ids(component: FormalComponent) -> tuple[str, ...]:
    return (f"{component.id}:ledger_map_accounting",
            *(f"{component.id}:{operation}" for operation in asdict(component.bindings)))
