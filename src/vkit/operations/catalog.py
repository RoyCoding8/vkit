"""Explicit trusted catalog of installed operations."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import MAX_SOURCE_BYTES, check_rewrite, reduce_failure, simplify_function
from .affine import compare_iteration_sets
from .affine_accesses import analyze_affine_accesses
from .algebra import compare_rational_expressions
from .behavior import find_behavioral_difference
from .c_safety import check_c_safety
from .cache_keys import find_cache_key_collision
from .contract import FileInput, Operation, operation_by_name
from .contracts import CONTRACT_PROFILES, compare_contracts
from .cover import minimize_cover
from .configurations import generate_configurations
from .grammar import find_grammar_ambiguity
from .dependency_resolution import resolve_dependencies
from .history import check_history
from .migrations import compare_migrations
from .policies import compare_authorization_policies
from .protocols import check_protocol_model
from .regex_cost import analyze_regex_cost
from .semantic_merge import check_semantic_merge
from .synthesis import PROFILE as SYNTHESIS_PROFILE, synthesize_function
from .sql_equivalence import compare_sql_queries
from .termination import analyze_loop_termination
from .wheel_artifacts import compare_wheels
from .workloads import compare_workload_cost

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
        name="find_cache_key_collision",
        description=("Search two independent input vectors for equal modeled cache keys and different "
                     "modeled results with pinned cvc5. Captures pure integer/boolean functions from one "
                     "Python source, binds key/value parameters positionally, and independently replays "
                     "collisions. NO_COLLISION_WITHIN_MODEL covers the full supported mathematical domain. "
                     "Does not model effects, hidden state, serialization or application caches."),
        properties={
            "path": {"type": "string", "minLength": 1},
            "function": {"type": "string", "minLength": 1},
            "key_function": {"type": "string", "minLength": 1},
            "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 60_000, "default": 2_000},
        },
        required=("path", "function", "key_function"), handler=find_cache_key_collision,
        outcomes={"NO_COLLISION_WITHIN_MODEL": 0, "COLLISION": 1, "UNKNOWN": 3, "UNSUPPORTED": 2, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="check_protocol_model",
        description=("Check every reachable state of a closed finite protocol model with pinned TLC and "
                     "independent exact saturation. Supports guarded simultaneous updates, invariants, "
                     "deadlock checking and explicit crash/restart of durable/volatile state. Counterexamples "
                     "are replayed. Results concern only the supplied finite model, not application code, "
                     "OS power loss, fairness or liveness. Limits and disagreement return UNKNOWN."),
        properties={
            "path": {"type": "string", "minLength": 1, "description": "repository-relative version-1 finite model JSON"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
        },
        required=("path",), handler=check_protocol_model,
        outcomes={"EXHAUSTIVE": 0, "COUNTEREXAMPLE": 1, "UNKNOWN": 3, "UNSUPPORTED": 2, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="analyze_affine_accesses",
        description=("Build exact ordered memory-access relations for one captured Python affine loop nest "
                     "using pinned ISL. Reports RAW, WAR, WAW and read/read overlaps with independently "
                     "replayed iteration witnesses. Named arrays are distinct abstract allocations by assumption. "
                     "Does not model aliasing, bounds, object effects or establish parallel safety."),
        properties={
            "path": {"type": "string", "minLength": 1},
            "function": {"type": "string", "minLength": 1},
            "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 60_000, "default": 2_000},
        },
        required=("path", "function"), handler=analyze_affine_accesses,
        outcomes={"ANALYZED": 0, "UNKNOWN": 3, "UNSUPPORTED": 2, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="compare_workload_cost",
        description=("Run a paired workload campaign using a current approved passing scenario check and "
                     "a local committed subject baseline. Captures the approved inputs, overlays only declared "
                     "subject files, alternates run order in fresh workspaces, and reports raw pairs with "
                     "median regression and bootstrap intervals. Statistical observations concern this "
                     "driver and environment; MEASURED is not a performance or correctness proof."),
        properties={
            "run_id": {"type": "string", "minLength": 1},
            "before_ref": {"type": "string", "minLength": 40, "description": "full immutable local Git commit SHA"},
            "pairs": {"type": "integer", "minimum": 10, "maximum": 20, "default": 10},
            "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 600, "default": 600},
        },
        required=("run_id", "before_ref"), handler=compare_workload_cost,
        outcomes={"MEASURED": 0, "INCONCLUSIVE": 3, "UNSUPPORTED": 2},
    ),
    Operation(
        version=1,
        name="analyze_loop_termination",
        description=("Synthesize and check a linear ranking for one captured Python integer while loop. "
                     "Checks nonnegative strict decrease over all guarded mathematical integer states with "
                     "pinned cvc5; independently replays guarded fixed points before reporting nontermination. "
                     "Bounded synthesis may return UNKNOWN. Covers the supported loop model, not whole "
                     "applications, machine overflow or resource exhaustion. Never executes project code."),
        properties={
            "path": {"type": "string", "minLength": 1, "description": "repository-relative Python source"},
            "function": {"type": "string", "minLength": 1},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
        },
        required=("path", "function"), handler=analyze_loop_termination,
        outcomes={"TERMINATES": 0, "NONTERMINATING": 1, "UNKNOWN": 3, "UNSUPPORTED": 2, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="check_semantic_merge",
        description=("Check a captured base/left/right/merged pure Python function quartet with pinned cvc5. "
                     "For every mathematical integer/boolean input, retain each branch's changed scalar return "
                     "and retain base where neither branch changed. Counterexamples are independently replayed "
                     "in the installed IR evaluator. Covers supported function return models, not effects, "
                     "source-edit intent or whole-repository merge safety. Never executes project code."),
        properties={
            "path": {"type": "string", "minLength": 1, "description": "repository-relative version-1 merge bundle"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
        },
        required=("path",), handler=check_semantic_merge,
        outcomes=_PROOF_OUTCOMES,
    ),
    Operation(
        version=1,
        name="compare_wheels",
        description=("Validate captured Python wheel archives and compare their static install scheme/path "
                     "mapping, file content, executable bits and package metadata. Checks RECORD integrity, "
                     "filename/metadata agreement, tags, unsafe paths and collisions before comparison. "
                     "Ignores ZIP timestamps/compression and normalized RECORD ordering. Never installs or "
                     "imports package code. Abstract install schemes may alias on a real host; this is not "
                     "a clean-install, runtime or reproducible-build proof."),
        properties={
            "before": {"type": "string", "minLength": 1, "description": "repository-relative before .whl artifact"},
            "after": {"type": "string", "minLength": 1, "description": "repository-relative after .whl artifact"},
        },
        required=("before", "after"), handler=compare_wheels,
        outcomes={"SAME_INSTALL_MAPPING": 0, "DIFFERENT_INSTALL_MAPPING": 1, "UNSUPPORTED": 2, "UNKNOWN": 3, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="compare_authorization_policies",
        description=("Compare captured Cedar policy sets for every well-formed request in one supplied schema, "
                     "action UID, principal type and resource type. Uses release-pinned Cedar SymCC and cvc5, "
                     "then independently replays concrete authorization differences with Cedar's authorizer. "
                     "EQUIVALENT concerns this schema/request family; application routes are outside the model. "
                     "Unknown output or unsupported witness values remain inconclusive."),
        properties={
            "path": {"type": "string", "minLength": 1, "description": "repository-relative version-1 Cedar policy comparison bundle"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 20},
        },
        required=("path",), handler=compare_authorization_policies,
        outcomes={"EQUIVALENT": 0, "COUNTEREXAMPLE": 1, "UNSUPPORTED": 2, "UNKNOWN": 3, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="compare_sql_queries",
        description=("Decide exact SQL SELECT bag equivalence within captured finite database bounds using "
                     "pinned SQLGlot and cvc5. Models nullable integer columns, WHERE and INNER JOIN with "
                     "three-valued NULL logic and duplicate rows. Independently replays every distinguishing "
                     "database in SQLite. EQUIVALENT_WITHIN_BOUNDS concerns the declared row/value bounds. "
                     "Rejects unmodeled SQL clauses and executes only canonical validated read-only queries."),
        properties={
            "path": {"type": "string", "minLength": 1, "description": "repository-relative version-1 bounded SQL query pair"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
        },
        required=("path",), handler=compare_sql_queries,
        outcomes={"EQUIVALENT_WITHIN_BOUNDS": 0, "COUNTEREXAMPLE": 1, "UNSUPPORTED": 2, "UNKNOWN": 3, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="compare_rational_expressions",
        description=("Compare exact rational expressions in Q(x1,...,xn) using pinned SymPy and a separate "
                     "sparse coefficient checker. Returns exact nonidentity evidence and original nonzero "
                     "denominator guards. An identity does not imply equal domains at cancelled poles. "
                     "Accepts a closed JSON AST, never Python evaluation or symbolic assumptions."),
        properties={
            "path": {"type": "string", "minLength": 1, "description": "repository-relative version-1 rational expression bundle"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
        },
        required=("path",), handler=compare_rational_expressions,
        outcomes={"IDENTICAL": 0, "NOT_IDENTICAL": 1, "UNSUPPORTED": 2, "UNKNOWN": 3, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="find_behavioral_difference",
        description=("Find a concrete CPython behavior difference with pinned CrossHair, then replay it twice "
                     "in fresh CPython processes. Observes returns, exceptions and mutated integer-list arguments. "
                     "Accepts only the installed closed language of typed local computation, branches and loops; "
                     "imports, global access, arbitrary calls and attributes are rejected before execution. "
                     "No witness is UNKNOWN, never equivalence. Deadlines are not an OS sandbox."),
        properties={
            "path": {"type": "string", "minLength": 1, "description": "repository-relative version-1 closed Python function pair"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
        },
        required=("path",), handler=find_behavioral_difference,
        outcomes={"COUNTEREXAMPLE": 1, "UNSUPPORTED": 2, "UNKNOWN": 3, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="analyze_regex_cost",
        description=("Classify a bounded regular expression using pinned Recheck's fixed automaton checker. "
                     "Returns completed non-fuzz matching-complexity estimates and theoretical attack pumps "
                     "and hotspots for vulnerable models. SAFE_MODEL concerns this backtracking model only; "
                     "it is not an absolute security guarantee. No target runtime timing or attack replay "
                     "is claimed. Unsupported syntax and unfinished work remain explicit outcomes."),
        properties={
            "pattern": {"type": "string", "maxLength": 2048},
            "flags": {"type": "string", "maxLength": 6, "default": ""},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
        },
        required=("pattern",),
        handler=analyze_regex_cost,
        outcomes={"SAFE_MODEL": 0, "VULNERABLE_MODEL": 1, "UNSUPPORTED": 2, "UNKNOWN": 3, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="synthesize_function",
        description=("Synthesize an equivalent pure integer function within an installed linear expression "
                     "grammar using pinned cvc5 SyGuS. Reuses the source AST model, validates grammar membership, "
                     "then independently proves complete-domain equivalence before SYNTHESIZED. Returns a "
                     "complete candidate function without editing source. NO_SOLUTION concerns the fixed "
                     "grammar; unfinished search is UNKNOWN. Does not claim minimum size or runtime speed."),
        properties={
            **_EXPRESSION,
            "profile": {"type": "string", "enum": [SYNTHESIS_PROFILE], "default": SYNTHESIS_PROFILE},
        },
        required=("path", "function"),
        handler=synthesize_function,
        outcomes={"SYNTHESIZED": 0, "NO_SOLUTION": 1, "UNSUPPORTED": 2, "UNKNOWN": 3, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="generate_configurations",
        description=("Generate a minimum-cardinality suite covering every feasible strength-t interaction "
                     "in a captured finite factor model. Enumerates legal configurations under forbidden partial "
                     "assignments, derives feasible interactions, and reuses the pinned set-cover solver. "
                     "Checks legality and coverage independently of solver selection. OPTIMAL proves minimum "
                     "cardinality; FEASIBLE does not. Enumeration limits return UNKNOWN. No tests execute."),
        properties={
            "path": {"type": "string", "minLength": 1, "description": "repository-relative version-1 factor model"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
        },
        required=("path",),
        handler=generate_configurations,
        outcomes={"OPTIMAL": 0, "FEASIBLE": 3, "INFEASIBLE": 1, "UNSUPPORTED": 2, "UNKNOWN": 3, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="find_grammar_ambiguity",
        description=("Find two distinct derivation trees for a supplied string in a captured literal context-free "
                     "grammar using pinned Earley parsing. Independently checks both trees against productions "
                     "and string before AMBIGUOUS. NO_AMBIGUITY_IN_CORPUS concerns only the supplied samples; "
                     "it is not global grammar unambiguity. Rejects zero-yield cycles and other unsupported "
                     "input. Grammar imports, regex terminals, callbacks, and project code are not accepted."),
        properties={
            "path": {"type": "string", "minLength": 1, "description": "repository-relative version-1 grammar/corpus bundle"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
        },
        required=("path",),
        handler=find_grammar_ambiguity,
        outcomes={"AMBIGUOUS": 1, "NO_AMBIGUITY_IN_CORPUS": 0, "UNSUPPORTED": 2, "UNKNOWN": 3, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="resolve_dependencies",
        description=("Resolve PEP 440/508 Python requirements within a captured finite candidate universe "
                     "and explicit target marker environment. Backtracks with pinned resolvelib, then checks "
                     "every reachable selected requirement independently. RESOLVED returns versions and edges; "
                     "CONFLICT concerns only the supplied universe and is not a minimal conflict core. "
                     "Extras and direct URLs are unsupported. Performs no index access or installation."),
        properties={
            "path": {"type": "string", "minLength": 1, "description": "repository-relative version-1 dependency bundle"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 20},
        },
        required=("path",),
        handler=resolve_dependencies,
        outcomes={"RESOLVED": 0, "CONFLICT": 1, "UNSUPPORTED": 2, "UNKNOWN": 3, "UNAVAILABLE": 5},
    ),
    Operation(
        version=1,
        name="compare_migrations",
        description=("Replay captured SQLite upgrade SQL and compare it with fresh-install SQL in bounded "
                     "in-memory databases. Compares exact SQLite-stored catalog SQL and metadata, excluding "
                     "physical rootpages, plus ordered rows from supplied read-only observations. MATCH concerns "
                     "those snapshots only; DIFFERENT returns catalog or observation differences. SQL functions "
                     "and operations are restricted by installed rules. No repository code or external database "
                     "is opened. Does not prove production data preservation or semantic schema equivalence."),
        properties={
            "path": {"type": "string", "minLength": 1, "description": "repository-relative version-1 SQL bundle"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
        },
        required=("path",),
        handler=compare_migrations,
        outcomes={"MATCH": 0, "DIFFERENT": 1, "UNSUPPORTED": 2, "UNKNOWN": 3, "UNAVAILABLE": 5},
    ),
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
        description=("Compare captured contracts under a fixed OpenAPI, Protobuf, or JSON containment profile. "
                     "OpenAPI validates local document/reference closures and checks protected oasdiff rules. "
                     "Protobuf uses protected Buf FILE, PACKAGE, WIRE_JSON, or WIRE rules. JSON containment "
                     "asks whether every old instance is accepted by new, in a fixed Draft 4 type/object/array "
                     "fragment; it does not generate counterexamples. COMPATIBLE means "
                     "no violation of that profile; BREAKING returns diagnostics. UNSUPPORTED rejects "
                     "uncheckable schemas; UNKNOWN preserves unfinished checks; UNAVAILABLE means the pinned "
                     "runtime cannot run. Project config cannot override rules. Does not establish server behavior."),
        properties={
            "old_path": {"type": "string", "minLength": 1, "description": "repository-relative old contract file or .proto directory"},
            "new_path": {"type": "string", "minLength": 1, "description": "repository-relative new contract file or .proto directory"},
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
                     "register-zero, initially-empty FIFO queue, or atomic multi-key transaction model. "
                     "Transaction keys start at zero; only completed committed transactions are supported. "
                     "LINEARIZABLE includes an "
                     "independently replayed ordering; NOT_LINEARIZABLE concerns this trace only. "
                     "UNKNOWN preserves timeouts; UNAVAILABLE means the configured pinned helper cannot run. "
                     "Does not change gate evidence."),
        properties={
            "path": {"type": "string", "minLength": 1, "description": "repository-relative JSON history file"},
            "model": {"type": "string", "enum": ["register", "queue", "transaction"]},
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
