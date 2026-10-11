"""Explicit trusted catalog of installed operations."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import MAX_SOURCE_BYTES, check_rewrite, reduce_failure, simplify_function
from .affine import compare_iteration_sets
from .c_safety import check_c_safety
from .contract import FileInput, Operation, operation_by_name
from .contracts import CONTRACT_PROFILES, compare_contracts
from .cover import minimize_cover
from .history import check_history

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


def _analyze_dependencies(root: Path, **arguments: Any) -> dict[str, Any]:
    from .dependencies import analyze_dependencies

    return analyze_dependencies(root, **arguments)


def _mutation_survivors(root: Path, **arguments: Any) -> dict[str, Any]:
    from .mutation import mutation_survivors

    return mutation_survivors(root, **arguments)


OPERATIONS = (
    Operation(
        version=1,
        name="mutation_survivors",
        description=("Generate fixed Stryker mutations of one JavaScript source file from a current, approved "
                     "passing node_test run. Replay the unmodified baseline, test each literal edit in a fresh "
                     "captured workspace, and independently replay survivors. Returns observed test detection "
                     "and exact edits, with unexamined mutations and inconclusive executions reported. "
                     "A survivor can be equivalent and does not establish a defect. Executes approved candidate "
                     "tests with service permissions; temporary copies are not an OS security sandbox. "
                     "Does not modify the checkout or gate evidence."),
        properties={
            "run_id": {"type": "string", "minLength": 1,
                       "description": "completed passing run of an approved node_test check"},
            "path": {"type": "string", "minLength": 1,
                     "description": "repository-relative JavaScript subject file in recorded inputs"},
            "max_mutants": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300, "default": 60},
        },
        required=("run_id", "path"),
        handler=_mutation_survivors,
        read_only=False,
        outcomes={"ANALYZED": 0, "UNKNOWN": 3, "UNSUPPORTED": 2, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="analyze_dependencies",
        description=("Extract a bounded JavaScript/TypeScript repository-local dependency graph from captured "
                     "source with fixed dependency-cruiser configuration. Query the graph, a shortest path, "
                     "strongly connected cycles, reverse impact, or nodes unreachable from supplied entrypoints. "
                     "Results concern extracted static edges, with unresolved and dynamic dependencies reported. "
                     "Graph unreachability does not prove runtime unreachability or safe deletion. Does not "
                     "execute project code/configuration or change gate evidence."),
        properties={
            "path": {"type": "string", "minLength": 1, "description": "repository-relative source directory"},
            "query": {"type": "string", "enum": ["graph", "path", "cycles", "affected", "unreachable"], "default": "graph"},
            "sources": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True,
                        "maxItems": 500, "description": "repository-relative start, changed modules, or entrypoints"},
            "target": {"type": "string", "minLength": 1, "description": "repository-relative target for a path query"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 20},
        },
        required=("path",),
        handler=_analyze_dependencies,
        outcomes={"ANALYZED": 0, "UNKNOWN": 3, "UNSUPPORTED": 2, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="check_c_safety",
        description=("Check a supported standalone C function for fixed bounds, pointer, division-by-zero, "
                     "signed-overflow, and undefined-shift violations with CBMC under a fixed C11 LP64 model. "
                     "SAFE requires completed checks and sufficient unwinding. COUNTEREXAMPLE returns a model "
                     "trace. UNKNOWN preserves insufficient unwinding or unfinished work. UNSUPPORTED rejects "
                     "other source; UNAVAILABLE means a pinned runtime cannot run. No project compiler executes. "
                     "Does not check every form of C undefined behavior or change gate evidence."),
        properties={
            "path": {"type": "string", "minLength": 1, "description": "repository-relative standalone C source"},
            "function": {"type": "string", "minLength": 1},
            "unwind": {"type": "integer", "minimum": 1, "maximum": 256, "default": 16},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
        },
        required=("path", "function"),
        handler=check_c_safety,
        outcomes={"SAFE": 0, "COUNTEREXAMPLE": 1, "UNSUPPORTED": 2, "UNKNOWN": 3, "UNAVAILABLE": 5},
    ),
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
        name="compare_contracts",
        description=("Compare captured API contracts under a fixed OpenAPI or Protobuf compatibility profile. "
                     "OpenAPI validates local document/reference closures and checks protected oasdiff rules. "
                     "Protobuf uses protected Buf FILE, PACKAGE, WIRE_JSON, or WIRE rules. COMPATIBLE means "
                     "no violation of that profile; BREAKING returns diagnostics. UNSUPPORTED rejects "
                     "uncheckable schemas; UNKNOWN preserves unfinished checks; UNAVAILABLE means the pinned "
                     "runtime cannot run. Project config cannot override rules. Does not establish server behavior."),
        properties={
            "old_path": {"type": "string", "minLength": 1, "description": "repository-relative old OpenAPI file or .proto directory"},
            "new_path": {"type": "string", "minLength": 1, "description": "repository-relative new OpenAPI file or .proto directory"},
            "profile": {"type": "string", "enum": list(CONTRACT_PROFILES)},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 20},
        },
        required=("old_path", "new_path", "profile"),
        handler=compare_contracts,
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
