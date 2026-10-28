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
ASSETS = ("ledger.rs", "expected.rs.in", "runtime.rs.in", "upstream.json")


@dataclass(frozen=True)
class LedgerPackage:
    root: Path
    identity: PackageIdentity
    pins: dict[str, Any]


def load_package() -> LedgerPackage:
    from .policy import PackageIdentity

    files = {name: hashlib.sha256((_ROOT / name).read_bytes()).hexdigest() for name in ASSETS}
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
    return _render(component, "expected.rs.in")


def render_runtime(component: FormalComponent) -> str:
    return _render(component, "runtime.rs.in")


def _render(component: FormalComponent, template_name: str) -> str:
    template = (_ROOT / template_name).read_text(encoding="utf-8")
    expected = template.replace("__VKIT_RECORDS__", str(component.max_records))
    state = (f"candidate_api::{component.representation}<N>" if component.representation else
             "ledger::ledger_api::Ledger<N>")
    expected = expected.replace("__VKIT_STATE__", state)
    for operation, path in asdict(component.bindings).items():
        expected = expected.replace(f"__VKIT_BIND_{operation.upper()}__", path)
    if "__VKIT_" in expected:
        raise ValueError("the owned ledger template has an unresolved parameter")
    return expected


def obligation_ids(component: FormalComponent) -> tuple[str, ...]:
    return (f"{component.id}:ledger_map_accounting",
            *(f"{component.id}:{operation}" for operation in asdict(component.bindings)))


def claim_coverage(component: FormalComponent) -> dict[str, Any]:
    observations = {
        "new": ["valid_state", "empty_reservations", "zero_usage", "supplied_capacity"],
        "reserve": ["ordered_errors", "ownership_and_amount", "capacity", "unchanged_on_error"],
        "release": ["ownership", "released_amount", "reservation_removal", "unchanged_on_error"],
        "reserve_batch": ["bounded_requests", "ordered_errors", "atomic_rollback"],
        "apply_command": ["tagged_result", "model_state_transition"],
        "run_commands": ["every_response", "every_model_state_transition", "empty_trace"],
    }
    return {
        "scope": "accepted_source_contract",
        "dependency_closure": "captured_whole_tree_and_accepted_package_checker",
        "claims": [{"id": f"{component.id}:{operation}", "binding": path,
                    "source": component.entry, "observations": observations[operation]}
                   for operation, path in asdict(component.bindings).items()],
        "package": {"sha256": component.package.sha256,
                    "claims": [{"id": f"{component.id}:ledger_map_accounting",
                                "observations": ["usage_equals_reservation_map_sum"]},
                               {"id": "ledger:transfer", "binding": "ledger_api::transfer",
                                "observations": ["matches_atomic_pair_model", "successful_conservation",
                                                 "error_origin", "rollback_on_failure"]},
                               {"id": "ledger:shared_operations", "binding": "ledger_api::SharedLedger",
                                "observations": ["lock_invariant", "completed_call_state_transition",
                                                 "unchanged_on_error"],
                                "assumptions": ["vstd_rwlock_specification", "completed_calls"],
                                "unclaimed": ["global_history_linearizability", "liveness", "abort_recovery"]}]},
        "unclaimed": ["unbound_application_code", "compiler_correctness", "specification_completeness"],
    }
