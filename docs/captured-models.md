# Captured model tools

These operations answer questions about captured data under an installed, fixed model. Call MCP
`help` to find the currently enabled tools and their exact argument schemas. Inputs never replace
the installed analysis code. A deployment must separately protect the service installation from
the coding agent's filesystem permissions.

## JSON Schema containment

Call `compare_contracts` with `profile="json-containment"`, `old_path`, and `new_path`.
The direction is always **every instance accepted by old is accepted by new**. Choose that direction
to match the question being asked; vkit does not infer request or response compatibility.

The supported Draft 4 fragment contains singleton JSON types, object properties, declared required
properties, boolean `additionalProperties`, and homogeneous array `items`. An empty schema accepts
all instances. Other keywords, references, patterns, unions, bounds, and other dialects are rejected.
The profile uses pinned jsonsubschema and runtime dependencies. `COMPATIBLE` means containment in
this model, `BREAKING` means a negative containment answer, and `UNKNOWN` preserves an undecided
backend answer or deadline. No concrete witness is generated. Exact captured file digests identify
the two inputs. Neither answer establishes application behavior.

Each file is limited to 512 KiB, schema nesting to 64 levels, and schema nodes to 2,048.
The deadline defaults to 20 seconds and cannot exceed 60 seconds. Unsupported constructs are
rejected before backend execution, including the pattern/negation combination involved in
[upstream issue 36](https://github.com/IBM/jsonsubschema/issues/36).

## SQLite migration comparison

Call `compare_migrations(path)` on a JSON bundle containing `version: 1`, `old_sql`, an ordered
`migrations` array of SQL strings, and `fresh_sql`. Optional `observations` contain unique `name`
and `query` strings. Vkit builds two in-memory databases with foreign keys enabled, replays the
upgrade on one, and creates the fresh database on the other.

`MATCH` means equality of exact stored catalog SQL and metadata, excluding physical rootpage
numbers, and equality of observation column names and ordered rows. SQL formatting can therefore
produce `DIFFERENT` even for schemas with the same behavior. CHECK constraints, defaults, collations,
indexes, foreign keys, views, and triggers remain represented in the catalog. Integer and real
observation values are distinct. Use `ORDER BY` for stable row assertions. Seed representative data
in the bundle and add observations when checking data changes; catalog equality alone says nothing
about retained rows.

External databases, extensions, virtual or temporary tables, PRAGMA statements, writes in
observations, and functions outside the installed deterministic allowlist are rejected. Statement,
page, VM-step, row, response-size, and process deadline limits bound execution. Invalid or unsupported
SQL returns `UNSUPPORTED`; resource exhaustion can be inconclusive. The bundle is limited to
256,000 bytes and combined SQL to 200,000 bytes. SQLite runtime semantics are those of the service's
Python installation, not those of another database engine.

## Captured Python dependency resolution

Call `resolve_dependencies(path)` on a JSON bundle with `version: 1`, a `roots` array of PEP 508
requirements, and `candidates`. Each candidate contains `name`, `version`, `requires_dist`, and
optional `requires_python`. The `environment` object must explicitly supply all eleven marker fields:
`implementation_name`, `implementation_version`, `os_name`, `platform_machine`, `platform_release`,
`platform_system`, `platform_version`, `platform_python_implementation`, `python_full_version`,
`python_version`, and `sys_platform`. Marker evaluation uses that target, not the host defaults.

The pinned resolver backtracks within this finite candidate universe. `RESOLVED` returns an
independently checked reachable assignment and its requirement edges. `CONFLICT` means no assignment
exists in that universe under this profile, not that an online package index has no solution.
Conflict causes are not claimed to be minimal. Candidate names and versions are normalized; duplicate
identities are rejected. Extras, direct URLs, and `extra` markers are unsupported. Stable candidates
are preferred; a prerelease is eligible only when every active requirement's specifier opts in.
This is a constraint result, not evidence that wheels exist, packages install, or their code works.

The capture is limited to 1,000,000 bytes, 256 roots, 2,000 candidates, and 4,000 requirements.
Candidate names and versions are limited to 256 UTF-8 bytes each. Worker serialization stops at
2,000,000 output bytes; a larger result returns `UNKNOWN`. The fixed 200-round search budget and
process deadline also produce `UNKNOWN` when exhausted.

## Configuration interaction coverage

Call `generate_configurations(path)` on `{"version":1,"factors":{"os":["linux","windows"],
"db":["sqlite","postgres"]},"strength":2,"forbidden":[]}`. Factor names and levels are strings.
A forbidden object is a partial assignment; every full configuration containing it is excluded.
An empty forbidden object excludes all configurations. Strength defaults to two and may range
from one to three, up to the factor count. Supply two to twelve factors.

Vkit enumerates legal full configurations, derives the interactions that occur in at least one
legal row, and selects a covering suite through the existing set-cover engine. It checks selected
rows and coverage independently of solver selection. `OPTIMAL` proves minimum cardinality in this
finite model. `FEASIBLE` preserves a covering suite without an optimality proof. `INFEASIBLE` means
there is no legal full configuration. More than 4,000 Cartesian rows, 2,000 feasible interactions,
or 100,000 coverage pairs returns `UNKNOWN`. Coverage does not establish that the configurations
pass tests; the operation does not execute them.

Worker serialization stops at 2,000,000 output bytes. Oversized configuration lists return
`UNKNOWN` before the worker writes them to its parent.

## Grammar ambiguity witnesses

Call `find_grammar_ambiguity(path)` on a version-1 bundle with `start`, `productions`, and `samples`.
Each production has a `left` nonterminal name and a `right` array of `{"nonterminal":"name"}` or
`{"literal":"text"}` symbols. An empty right array is an empty production. Terminals are nonempty
literal strings, with no implicit whitespace skipping. Names are data and cannot introduce imports,
parser directives, regular expressions, or callbacks.

Pinned Lark Earley parsing preserves alternative derivations and lexical segmentations.
`AMBIGUOUS` includes two distinct trees whose production choices and terminal yields are checked
independently against the captured grammar and string. Production indices are zero-based.
`NO_AMBIGUITY_IN_CORPUS` only describes the supplied samples and lists samples outside the language.
It never asserts global unambiguity. Zero-yield cycles are rejected because they can create infinitely
many derivations of a fixed string.

The capture is limited to 128,000 bytes, 64 productions, 32 nonterminals, 256 right-hand symbols,
and 64 samples of at most 256 characters. Witness traversal and the isolated process deadline
bound ambiguous forests; unfinished analysis returns `UNKNOWN`.

## Grammar-constrained function synthesis

Call `synthesize_function(path, function)` with the fixed `linear-int-v1` profile. The reference is
parsed through vkit's existing pure integer/boolean frontend without importing its module. This
profile requires integer parameters and an integer return. The candidate grammar is
`E ::= parameter | -1 | 0 | 1 | E + E | E - E`, interpreted over mathematical integers.

Pinned cvc5 SyGuS searches for a candidate equal to the reference for every parameter valuation.
The result is decoded into the shared expression model, checked against the grammar, rendered as
a complete function, and passed through the existing independent equivalence checker. Only a
`PROVED` check permits `SYNTHESIZED`. The returned source is not installed or executed.
`NO_SOLUTION` requires an engine no-solution answer within the grammar. `UNKNOWN` preserves search,
decoding, or proof limits. No minimum-size or performance claim is made.

Source and candidate size, AST nodes, worker input/output, and a hard process deadline bound the
request. The default is 2,000 milliseconds, including worker startup and the independent proof.
There is no OS memory quota; these are bounded model and time limits.

## Regex backtracking complexity

Call `analyze_regex_cost(pattern, flags)` to classify matching complexity in Recheck 4.5.0's fixed
automaton model. Patterns are limited to 2,048 UTF-8 bytes and flags to distinct members of `gimsuy`.
The helper uses the synchronous browser bundle in one timed Node process, with fuzzing and target
runtime recall disabled. It does not launch a native backend child.

`SAFE_MODEL` requires a completed, non-fuzz constant, linear, or safe classification.
`VULNERABLE_MODEL` requires a completed polynomial or exponential classification with bounded
theoretical attack pumps and hotspots. Unsupported constructs, incomplete analysis, and deadlines
never become a safe answer. The attack description is not a replayed JavaScript performance failure;
runtime optimizations can affect actual behavior. See [Recheck diagnostics](https://makenowjust-labs.github.io/recheck/docs/usage/diagnostics/).

Install `tools/regex` with its committed lockfile under the service owner's control, then configure
the existing Node runtime and hash pins. For a development checkout:

```powershell
npm ci --prefix tools/regex --ignore-scripts
$env:VKIT_NODE_BIN = (Get-Command node).Source
$env:VKIT_REGEX_HELPER = (Resolve-Path tools/regex/helper.cjs).Path
$env:VKIT_REGEX_SHA256 = (Get-FileHash $env:VKIT_REGEX_HELPER -Algorithm SHA256).Hash.ToLower()
$env:VKIT_REGEX_LOCKFILE = (Resolve-Path tools/regex/package-lock.json).Path
$env:VKIT_REGEX_LOCK_SHA256 = (Get-FileHash $env:VKIT_REGEX_LOCKFILE -Algorithm SHA256).Hash.ToLower()
```

Node must be exactly 24.16.0. Production pins and the installed helper, lock, and package tree must
be protected from the coding agent. The helper and lock hashes identify configured inputs; they
do not attest the entire mutable package tree. Package installation is a separate deployment step.

## Atomic transaction histories

Call `check_history(path, model="transaction")` with the existing version-1 trace envelope. Every
operation is one completed, committed transaction. Its input contains ordered `actions`; its output
contains one observed integer per read, in read order. For example:

```json
{
  "schema_version": 1,
  "operations": [
    {"id": "commit", "client_id": 0, "call": 1, "return": 2,
     "input": {"actions": [{"op": "write", "key": "x", "value": 7},
                            {"op": "read", "key": "x"}]}, "output": [7]}
  ]
}
```

Every key starts at integer zero. Reads see earlier writes within the same transaction. The whole
transaction takes effect atomically. A successful Porcupine ordering is independently replayed,
including recorded real-time precedence, before `LINEARIZABLE` is returned. Mixed snapshots,
lost updates, and write skew can yield `NOT_LINEARIZABLE` for the supplied trace. A successful
history does not establish the database's behavior across other executions or SQL isolation levels.

The existing 1 MiB capture, 1,000-operation, signed 64-bit value/timestamp, and helper deadline
limits apply. Each transaction contains 1–64 actions. Histories contain at most 128 distinct keys;
keys are nonempty Unicode scalar strings of at most 128 characters. Aborted and pending
transactions are unsupported. Use the [existing protected history helper setup](computations.md#check-a-concurrent-history).
