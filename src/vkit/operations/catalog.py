"""Explicit trusted catalog of installed operations."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import MAX_SOURCE_BYTES, check_rewrite, reduce_failure, simplify_function
from .affine import compare_iteration_sets
from .contract import FileInput, Operation, operation_by_name
from .cover import minimize_cover
from .history import check_history
from .protobuf import check_proto_compatibility

_EXPRESSION = {
    "path": {"type": "string", "description": "repository-relative Python source file"},
    "function": {"type": "string", "minLength": 1},
    "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 60_000, "default": 2_000},
}
_PROOF_OUTCOMES = {"PROVED": 0, "COUNTEREXAMPLE": 1, "UNKNOWN": 3, "UNSUPPORTED": 2, "UNAVAILABLE": 5}


def _compare_matchsets(root: Path, old_pattern: str, new_pattern: str, alphabet: str,
                       timeout_ms: int = 2_000) -> dict[str, Any]:
    from .matchsets import compare_matchsets

    return compare_matchsets(old_pattern, new_pattern, alphabet, timeout_ms).to_json()


OPERATIONS = (
    Operation(
        version=1,
        name="compare_iteration_sets",
        description=("Compare yielded coordinate sets of supported affine integer generator ASTs for every "
                     "mathematical integer parameter valuation, using fixed built-in range semantics. "
                     "Module globals, yield order, and multiplicity are outside the model. EQUIVALENT means "
                     "both differences are empty; COUNTEREXAMPLE returns directional witnesses. "
                     "UNSUPPORTED rejects other source; UNKNOWN preserves unfinished work; UNAVAILABLE "
                     "means pinned islpy cannot run. Does not execute or modify source."),
        properties={
            **_EXPRESSION,
            "replacement": {"type": "string", "maxLength": 65_536},
        },
        required=("path", "function", "replacement"),
        handler=compare_iteration_sets,
        outcomes={"EQUIVALENT": 0, "COUNTEREXAMPLE": 1, "UNSUPPORTED": 2, "UNKNOWN": 3, "UNAVAILABLE": 5},
        cli_files={"replacement": FileInput("replacement-file", MAX_SOURCE_BYTES)},
    ),
    Operation(
        version=1,
        name="minimize_cover",
        description=("Find a minimum-cost candidate selection covering the required IDs in a supplied JSON matrix. "
                     "Uses a fixed integer set-cover model. OPTIMAL includes a checked selection and exact cost; "
                     "FEASIBLE has no optimality proof. INFEASIBLE concerns this matrix only. UNKNOWN preserves "
                     "timeouts; UNAVAILABLE means the pinned backend cannot run. Does not execute or delete tests."),
        properties={
            "path": {"type": "string", "minLength": 1, "description": "repository-relative JSON coverage matrix"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
        },
        required=("path",),
        handler=minimize_cover,
        outcomes={"OPTIMAL": 0, "FEASIBLE": 3, "INFEASIBLE": 1, "UNKNOWN": 3, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="check_proto_compatibility",
        description=("Compare captured local Protobuf source trees with protected Buf compatibility rules. "
                     "COMPATIBLE means no violation of the selected FILE, PACKAGE, WIRE_JSON, or WIRE category. "
                     "BREAKING returns source diagnostics. Project config and plugins cannot override the rules. "
                     "UNSUPPORTED rejects uncheckable schemas; UNKNOWN preserves unfinished checks; "
                     "UNAVAILABLE means the configured pinned binary cannot run. Does not establish runtime behavior."),
        properties={
            "old_path": {"type": "string", "minLength": 1, "description": "repository-relative old .proto directory"},
            "new_path": {"type": "string", "minLength": 1, "description": "repository-relative new .proto directory"},
            "category": {"type": "string", "enum": ["FILE", "PACKAGE", "WIRE_JSON", "WIRE"], "default": "WIRE_JSON"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
        },
        required=("old_path", "new_path"),
        handler=check_proto_compatibility,
        outcomes={"COMPATIBLE": 0, "BREAKING": 1, "UNSUPPORTED": 2, "UNKNOWN": 3, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="check_history",
        description=("Decide whether a completed concurrent history can be ordered under the built-in "
                     "register-zero or initially-empty FIFO queue model. LINEARIZABLE includes an "
                     "independently replayed ordering; NOT_LINEARIZABLE concerns this trace only. "
                     "UNKNOWN preserves timeouts; UNAVAILABLE means the configured pinned helper cannot run. "
                     "Does not change gate evidence."),
        properties={
            "path": {"type": "string", "minLength": 1, "description": "repository-relative JSON history file"},
            "model": {"type": "string", "enum": ["register", "queue"]},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
        },
        required=("path", "model"),
        handler=check_history,
        outcomes={"LINEARIZABLE": 0, "NOT_LINEARIZABLE": 1, "UNKNOWN": 3, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="compare_matchsets",
        description=("Compare full-match acceptance sets in the fixed greenery regex dialect over the supplied "
                     "alphabet. Returns EQUIVALENT or COUNTEREXAMPLE with shortest directional witnesses; "
                     "UNKNOWN on timeout, UNSUPPORTED for unsupported inputs, or UNAVAILABLE without the backend."),
        properties={
            "old_pattern": {"type": "string", "maxLength": 2_048},
            "new_pattern": {"type": "string", "maxLength": 2_048},
            "alphabet": {"type": "string", "maxLength": 64},
            "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 60_000, "default": 2_000},
        },
        required=("old_pattern", "new_pattern", "alphabet"),
        handler=_compare_matchsets,
        outcomes={"EQUIVALENT": 0, "COUNTEREXAMPLE": 1, "UNKNOWN": 3, "UNSUPPORTED": 2, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="check_rewrite",
        description=("Prove equal return values for a supported pure Python int/bool function and a replacement "
                     "function with the same signature. Returns PROVED, COUNTEREXAMPLE, UNKNOWN, UNSUPPORTED "
                     "or UNAVAILABLE, with the modeled domain and source digest. Does not execute or modify the source."),
        properties={
            **_EXPRESSION,
            "replacement": {"type": "string", "maxLength": 65_536},
        },
        required=("path", "function", "replacement"),
        handler=check_rewrite,
        outcomes=_PROOF_OUTCOMES,
        cli_files={"replacement": FileInput("replacement-file", MAX_SOURCE_BYTES)},
    ),
    Operation(
        version=1,
        name="simplify_function",
        description=("Derive a smaller equivalent pure Python int/bool function using fixed egglog rules and "
                     "independently check it with cvc5. Returns a suggested replacement and the proof status; "
                     "does not modify files or establish a global minimum."),
        properties={
            **_EXPRESSION,
        },
        required=("path", "function"),
        handler=simplify_function,
        outcomes=_PROOF_OUTCOMES,
    ),
    Operation(
        version=1,
        name="reduce_failure",
        description=("Reduce one input file of a recorded, still-current failed run using Perses. Reuses the "
                     "approved check and preserves the recorded failure. Returns a validated reproducer without "
                     "modifying the checkout. Requires the configured POSIX Java/Perses runtime."),
        properties={
            "run_id": {"type": "string"},
            "path": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300, "default": 60},
        },
        required=("run_id", "path"),
        handler=reduce_failure,
        read_only=False,
        outcomes={"REDUCED": 0, "UNRESOLVED": 3, "UNAVAILABLE": 5},
    ),
)

OPERATIONS_BY_NAME = operation_by_name(OPERATIONS)
