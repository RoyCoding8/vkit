# Built-in computations

These operations give an agent a specific answer or a specific artifact. The agent supplies source,
a function name, or a recorded run. It cannot supply solver assertions, rewrite rules, or a reduction
predicate through these APIs. Results do not create check evidence, accept definitions, or change the gate.

Each operation has one trusted declaration shared by the CLI and MCP.
See [Add a built-in operation](adding-operations.md) for the extension contract and addition procedure.

Install the optional Python engines in the environment that runs vkit.

```powershell
uv sync --extra mcp --extra engines
```

The Python engines are pinned to [cvc5 1.4.2](https://cvc5.github.io/docs/cvc5-1.4.2/),
[egglog 13.2.0](https://egglog-python.readthedocs.io/stable/), greenery 4.2.2,
[OR-Tools 9.15.6755](https://developers.google.com/optimization/cp/cp_solver), and
[islpy 2026.2.2](https://documen.tician.de/islpy/), with pycparser 3.0 for the C frontend.
The islpy dependency is installed on Linux and macOS;
the pinned release has no native Windows wheel. Perses, Buf, CBMC, and the compiled Porcupine helper are
configured separately. The [engine queue](engine-roadmap.md) records the remaining integration candidates.

## Check a replacement

`check_rewrite` compares one top-level function from a repository file with the same function in supplied
replacement source. Both must have the same name, parameter names and order, parameter annotations, and
return annotation. The source is parsed without importing or executing it.

```powershell
vkit compute check-rewrite --project . --path src/score.py --function score --replacement-file candidate.py --json
```

The equivalent MCP request uses `check_rewrite` with `path`, `function`, and `replacement` containing the
source text. `timeout_ms` is optional, defaults to 2000, and must be between 1 and 60000.

The frontend models mathematical integers and booleans. Parameters range over their explicitly annotated
`int` or `bool` domain. Annotations do not enforce caller types at runtime. Module statements are ignored;
the result concerns the selected function AST, not importing the module or running the application.

Supported constructs are integer and boolean literals, parameters, local assignments, return, conditional
statements and expressions, integer `+`, `-`, `*`, comparisons, and boolean `and`, `or`, `not`. Every path
must return. Unbound reads, parameter reassignment, mixed sorts, calls, effects, loops, decorators, defaults,
floating point, division, and unsupported syntax are rejected. Source size and frontend expansion are bounded.

For example, cvc5 can establish that `return (x + 3) * 2` and `return 2 * x + 6` agree for every integer
`x`. For `return x + y` versus `return x - y`, it returns concrete arguments that make the results differ.

| Status | Meaning |
| --- | --- |
| `PROVED` | cvc5 established that differing return values are impossible in the stated model |
| `COUNTEREXAMPLE` | Concrete arguments differ, checked again by the IR evaluator |
| `UNKNOWN` | The engine did not decide the query within its limits or encountered an engine error |
| `UNSUPPORTED` | The source or signature is outside the modeled subset |
| `UNAVAILABLE` | The required engine is not installed |

The response includes the original source SHA-256, model scope, and backend version. A proof concerns those
source bytes and that model. It does not establish resource consumption, caller behavior, or whole-program
correctness. Nonlinear integer arithmetic can return `UNKNOWN`.

## Derive a replacement

```powershell
vkit compute simplify-function --project . --path src/score.py --function score --json
```

The MCP tool is `simplify_function`, with `path`, `function`, and optional `timeout_ms`.

egglog searches with fixed arithmetic, boolean, and conditional identities for four iterations, then extracts
an expression using node count as its cost. For example, `return (x + 0) * 1` becomes `return x`, and
`return x * y + x * z` becomes `return x * (y + z)`. Integer
constant folding uses arbitrary precision. cvc5 then checks the rendered replacement against the original
function through the same frontend. Only that independent check can produce `PROVED`.

The response includes `replacement`, both backend versions, the original hash, model scope, and proof result.
The source file is unchanged. The result is not a globally minimal program or a performance guarantee; an
already simple function can produce an equivalent replacement with the same cost. `timeout_ms` limits the
cvc5 query. The egglog search is bounded by frontend size, a fixed rule set, and iteration count.

## Compare regex acceptance sets

`compare_matchsets` compares two complete regular languages over a supplied finite alphabet.
It uses the fixed [greenery 4.2.2 dialect](https://github.com/qntm/greenery).
This dialect does not provide a compatibility claim for Python, JavaScript, or other regex engines.
Matching covers the entire string. The operation accepts no flags or custom rewrite rules.

```powershell
vkit compute compare-matchsets --project . --old-pattern "a*" --new-pattern "a+" --alphabet "a" --json
```

The equivalent MCP request uses `compare_matchsets` with `old_pattern`, `new_pattern`, and `alphabet`.
The alphabet is a string of at most 64 unique characters. An empty alphabet permits only the empty string.
Each pattern is limited to 2048 UTF-8 bytes. `timeout_ms` defaults to 2000 and ranges from 1 to 60000.

The engine intersects both languages with the alphabet's complete string language, computes both
directional differences, and checks those differences for emptiness. `EQUIVALENT` establishes equality
over that alphabet. `COUNTEREXAMPLE` includes `old_only` and `new_only`, each a shortest string accepted
only by that side, or `null` when that direction has no difference. An empty string witness is `""`.
The service checks each witness against both original automata and the alphabet before returning it.

Library parsing and automata operations run in a separate process under one wall-clock timeout.
`UNKNOWN` means the computation did not finish. `UNSUPPORTED` rejects syntax or inputs outside the
contract. `UNAVAILABLE` means the optional backend cannot run. Neither result establishes equivalence.
The operation does not edit source files or publish gate evidence.

## Reduce a recorded failure

```powershell
vkit compute reduce-failure --project . --run-id RUN_ID --path src/subject.py --timeout-seconds 60 --json
```

The MCP tool is `reduce_failure`, with `run_id`, `path`, and optional `timeout_seconds` between 1 and 300.

The selected run must be a completed failure of an approved `scenario` or `pytest` check in
the current checkout. Its definition and declared input snapshot must still match. The target must be a
recorded Python source input within the check's declared subject. Check drivers, required tests, and
pytest `conftest.py` files cannot be reduction targets.
Budget-only failures and unsupported checks are refused.

vkit copies recorded inputs into temporary workspaces. Perses changes the target source while the built-in
predicate replays the approved check and requires the selected failed scenario ID and exact observation.
vkit independently replays the final result before returning `REDUCED`. The original checkout stays unchanged.
`UNRESOLVED` means no changed, replayed result was obtained, including an engine timeout. It does not mean
that a smaller reproducer is impossible. `UNAVAILABLE` means the configured runtime cannot run.

`timeout_seconds` limits the Perses process; version detection and independent replays take additional time.
The operation returns source text, original and reduced hashes, failure identity, and engine identity.
It does not prove the failure's root cause or that the result is minimal. Approved drivers and tests remain
part of the trusted predicate. Inputs must include every file needed by the replay. Known pytest discovery
files, including `conftest.py` and pytest configuration, must be recorded. External driver file operands
are refused. The check executable must be `{{python}}` or the service's own Python interpreter. Other
launchers and directly executable drivers are unsupported in this release.

Perses requires a POSIX environment, Java 17, and the pinned release JAR. Run vkit inside Linux, macOS, or WSL;
the native Windows process reports `UNAVAILABLE`. Configure these variables in the service environment.

```sh
curl --fail --location https://github.com/uw-pluverse/perses/releases/download/v2.7/perses_deploy.jar -o /trusted/perses.jar
export VKIT_PERSES_JAR=/trusted/perses.jar
export VKIT_PERSES_SHA256=1102ec7e3e601792a3c271c41ac7df52b03fca635df552500c241933c2c1e427
echo "$VKIT_PERSES_SHA256  $VKIT_PERSES_JAR" | sha256sum --check
```

Set `JAVA_HOME` or put Java on `PATH`. vkit checks the configured JAR hash and requires version 2.7.

## Check a concurrent history

`check_history` decides whether a completed operation history has a sequential ordering consistent with
its recorded timing and one fixed built-in model. [Porcupine 1.3.1](https://pkg.go.dev/github.com/anishathalye/porcupine@v1.3.1)
performs the search. The Python adapter independently checks and replays a successful ordering.

```powershell
vkit compute check-history --project . --path traces/register.json --model register --json
```

The MCP request uses `check_history` with `path` and `model`. `path` identifies a JSON file inside the
repository. `timeout_seconds` defaults to 10 and ranges from 1 to 60. No request accepts a custom model,
executable, or helper path. These are the supported models.

| Model | Initial state | Operations |
| --- | --- | --- |
| `register` | Integer zero | `write` takes integer `value` and returns `null`; `read` takes no value and returns the current integer |
| `queue` | Empty FIFO queue | `enqueue` takes integer `value` and returns `null`; `dequeue` takes no value and returns the oldest integer, or `null` when empty |

A trace contains exactly `schema_version` and `operations`. This completed register trace is linearizable.

```json
{
  "schema_version": 1,
  "operations": [
    {"id": "write-7", "client_id": 0, "call": 1, "return": 2,
     "input": {"op": "write", "value": 7}, "output": null},
    {"id": "read-7", "client_id": 1, "call": 3, "return": 4,
     "input": {"op": "read"}, "output": 7}
  ]
}
```

Every operation requires all six fields shown above. IDs are unique nonempty Unicode scalar strings of
at most 128 characters. Client IDs range from 0 to 2147483647. Values and timestamps are signed 64-bit
integers, with `call < return`. Times share one ordering scale. Intervals are closed, so an operation
must precede another only when its return is strictly less than the other's call. Equal endpoints overlap.
The file limit is 1 MiB, with at most 1000 operations. Unknown fields, duplicate keys, pending operations,
and malformed records are rejected before the helper runs.

`LINEARIZABLE` includes every operation ID in a complete ordering. The service checks the permutation,
real-time precedence, and sequential model outputs independently. `NOT_LINEARIZABLE` means no such
ordering exists for this supplied trace and model. It does not establish correctness across other runs.
`UNKNOWN` preserves timeouts and invalid backend responses. `UNAVAILABLE` means the configured helper
is missing, fails its SHA-256 check, or cannot start. No result edits the trace or changes gate evidence.

Build the helper once from this repository with Go 1.26.9, then configure it in the protected service's
environment. Go is not required when invoking the compiled helper.

```powershell
Push-Location tools/history
go build -mod=readonly -trimpath -buildvcs=false -o C:/vkit-runtime/vkit-history.exe .
Pop-Location
$env:VKIT_HISTORY_BIN = 'C:/vkit-runtime/vkit-history.exe'
$env:VKIT_HISTORY_SHA256 = (Get-FileHash -LiteralPath $env:VKIT_HISTORY_BIN -Algorithm SHA256).Hash.ToLowerInvariant()
```

The parent process enforces the helper's wall-clock deadline. File parsing, binary hashing, and final
witness replay occur outside that search deadline. CI builds and exercises the pinned helper on Linux,
Windows, and macOS.

## Choose minimum-cost coverage

`minimize_cover` selects candidates covering every required element in a supplied matrix. It uses a fixed
OR-Tools CP-SAT set-cover model. For example, use observed test coverage as the matrix and measured test
costs as integers. The answer concerns that matrix, not future regression detection.

```powershell
vkit compute minimize-cover --project . --path coverage.json --timeout-seconds 10 --json
```

The MCP tool accepts `path` and optional `timeout_seconds`. A matrix has this shape.

```json
{
  "version": 1,
  "required": ["parse", "validate"],
  "candidates": [
    {"id": "parser-tests", "cost": 3, "covers": ["parse"]},
    {"id": "validation-tests", "cost": 4, "covers": ["validate"]},
    {"id": "integration-tests", "cost": 5, "covers": ["parse", "validate"]}
  ]
}
```

The optimum in this example is `integration-tests` at cost 5. Candidate IDs are unique, and costs are
nonnegative integers. Candidate coverage may include elements outside `required`. The service checks
the returned selection against the captured matrix and recomputes its integer cost.

The matrix limit is 512,000 bytes, 2,000 required IDs, 4,000 candidates, and 100,000 coverage pairs.
IDs contain at most 256 UTF-8 bytes. The sum of candidate costs cannot exceed `2^53 - 1`.
The isolated worker deadline includes imports and solving. Matrix reading and validation occur before it.

`OPTIMAL` means the solver proved minimum cost. `FEASIBLE` returns a checked selection without an
optimality proof. `INFEASIBLE` means no selection covers the required elements. `UNKNOWN` preserves an
unfinished computation. `UNAVAILABLE` means the pinned backend cannot run. A file digest binds the
answer to the supplied matrix. No test is run or deleted.

## Check Protobuf compatibility

`check_proto_compatibility` captures local `.proto` source trees and runs Buf 1.73.0 under one fixed
built-in compatibility category. It ignores repository Buf configuration, ignore rules, and plugins.
Imports must resolve within each captured source tree or Buf's built-in well-known types.

```powershell
vkit compute check-proto-compatibility --project . --old-path api-before --new-path api-after --category WIRE_JSON --json
```

The MCP tool accepts `old_path`, `new_path`, optional `category`, and optional `timeout_seconds`.
The category defaults to `WIRE_JSON`. Buf defines these categories in its
[breaking rule reference](https://buf.build/docs/breaking/rules/).

| Category | Compatibility checked |
| --- | --- |
| `FILE` | Generated source compatibility at the file level |
| `PACKAGE` | Generated source compatibility at the package level |
| `WIRE_JSON` | Binary and JSON wire compatibility |
| `WIRE` | Binary wire compatibility |

`COMPATIBLE` means Buf found no violation of the selected category. `BREAKING` includes rule IDs and
source diagnostics. Neither result proves application behavior. `UNSUPPORTED` means the captured
schemas could not be checked. `UNKNOWN` preserves timeouts or an invalid backend response.
`UNAVAILABLE` means the configured pinned binary cannot run. Results include both captured tree digests.

Each tree admits at most 500 `.proto` files, 1 MiB per file, 16 MiB total, and 10,000 scanned entries.
The process deadline covers version detection and compatibility checking. Snapshotting checks the same
deadline between files. Filesystem reads and executable hashing are synchronous.

Install the matching asset from the [Buf 1.73.0 release](https://github.com/bufbuild/buf/releases/tag/v1.73.0)
under the service owner's control, verify it against the release's `sha256.txt`, and set these variables.
For the Windows x86-64 asset, the configuration is:

```powershell
$env:VKIT_BUF_BIN = 'C:/vkit-runtime/buf-Windows-x86_64.exe'
$env:VKIT_BUF_SHA256 = '13542f2892c4f774150ddb525266d6421d457b3e741297056b64427853526e36'
```

The request cannot select an executable or change the rules. CI downloads and exercises pinned assets
on Linux, Windows, and macOS.

## Compare affine iteration sets

`compare_iteration_sets` compares the coordinate sets yielded by two supported integer generators for
every mathematical integer parameter valuation. It parses source without executing it, constructs
Presburger sets with isl, and checks both directional differences.

The model interprets each selected function AST with mathematical integer parameters and built-in
`range`. Module statements and global bindings are ignored. Every parameter requires the exact `int`
annotation. Decorators, default arguments, and return annotations are unsupported.

```python
def points(n: int):
    for i in range(n):
        for j in range(i, n):
            yield (i, j)
```

A replacement may reverse loop order while yielding the same coordinate set. This check concerns set
membership. Yield order, duplicate multiplicity, runtime types, and resource consumption are outside
the model.

```powershell
vkit compute compare-iteration-sets --project . --path loops.py --function points --replacement-file candidate.py --json
```

The MCP tool accepts `path`, `function`, `replacement`, and optional `timeout_ms`. Supported syntax
includes affine integer expressions, nested `for ... in range(...)` loops with constant nonzero steps,
affine comparisons and Boolean combinations, and fixed-arity tuple yields. Effects, assignments,
nonlinear multiplication, arbitrary calls, floating point, and loop exits are rejected. Loop variables
are scoped to their active loop bodies in this model.

`EQUIVALENT` means both differences are empty. `COUNTEREXAMPLE` gives a parameter valuation and coordinate
in a directional difference. `UNSUPPORTED` rejects source outside the model. `UNKNOWN` preserves an
unfinished computation. `UNAVAILABLE` means the pinned islpy backend cannot run, including its absence
on native Windows. Source digests bind the result to both inputs.

## Check standalone C safety

`check_c_safety` parses one standalone C function, admits a restricted syntax, and checks a normalized
snapshot with CBMC 6.11.0. Parameters are unconstrained scalar inputs. The fixed model is C11 on x86-64
Linux LP64, little-endian, with 32-bit `int`. No repository compiler, preprocessor, or executable runs.

```powershell
vkit compute check-c-safety --project . --path src/bump.c --function bump --unwind 16 --json
```

The MCP tool accepts `path`, `function`, optional `unwind` (1–256, default 16), and optional
`timeout_seconds` (1–60, default 10). Requests cannot change checks, supply assumptions, or select flags.
The fixed properties are array bounds, pointer checks, division by zero, signed integer overflow,
undefined shifts, and loop unwinding assertions.

The source must contain exactly one function definition with an explicit prototype. Parameters and
locals support unqualified `int`, `signed int`, `unsigned int`, and `_Bool`; the return may also be `void`.
Locals require explicit initializers that do not reference the variable being declared.
Fixed one-dimensional local arrays may only be indexed directly or
used with `sizeof`. Assignments and increments must be standalone statements or for-loop updates.
Integer expressions, branches, loops, and switches are supported. Globals, includes, directives, calls,
pointers, structs, attributes, and CBMC intrinsic identifiers are rejected. CBMC performs type checking
after the syntax boundary. Rejection is `UNSUPPORTED`.

`SAFE` means all generated properties succeeded and all loops were completely unwound under the fixed
checks and model. It does not establish absence of every C undefined behavior: uninitialized reads are
not checked, and an explicit initializer alone does not establish definite initialization. It does not
prove functional correctness, termination, portability, or safety of callers or the surrounding program.
`COUNTEREXAMPLE` includes a CBMC model trace for a recognized failed safety property. The trace is not an
independent replay of compiled code. `UNKNOWN` preserves exhausted loop bounds, unfinished checks,
timeouts, and malformed backend results. `UNAVAILABLE` means the pinned parser or configured runtime
cannot run.

Source and normalized-snapshot SHA-256 digests bind the answer to the checked input. Diagnostics use
normalized-snapshot line numbers; results with failed or unfinished properties include `snapshot_source`
so those lines can be inspected. Limits are 64 KiB of source, 8,192 AST nodes, depth 128, 32 parameters,
64 locals, 16 arrays, 256 elements per array, and 1,024 array elements total. Backend output over 8 MiB is
rejected after capture. The native process deadline covers version detection and solving. Source parsing,
filesystem reads, and executable hashing are synchronous outside that deadline.

Obtain CBMC from the [6.11.0 release](https://github.com/diffblue/cbmc/releases/tag/cbmc-6.11.0), verify
the release asset, and put the extracted executable under the service owner's control. Configure its
absolute path and executable SHA-256; requests cannot choose either. For the Windows release executable:

```powershell
$env:VKIT_CBMC_BIN = 'C:/vkit-runtime/cbmc-6.11.0.exe'
$env:VKIT_CBMC_SHA256 = 'e2f6a110906b7c5998f31617c89220f9840735250333c90c56ef467c667d8bda'
```

CI exercises release binaries on Linux and Windows. macOS CI checks the unavailable-runtime behavior.

## Protect the service

These APIs expose fixed operations. They contain no method for changing the parser, solver translation,
rewrite rules, or failure predicate. To enforce that boundary against an agent with a shell, install vkit,
its dependencies, the Perses JAR, Buf and CBMC binaries, and the history helper under a separate owner or in a service container
the agent cannot write.
Give the service access to candidate repository files. Keep its interpreter, working directory, import path, and environment
under the service owner's control. Running an editable vkit checkout as the same operating-system user as
the agent does not enforce immutability. This release does not provision that deployment isolation.

CLI exit codes are 0 for `PROVED`, `REDUCED`, `EQUIVALENT`, `LINEARIZABLE`, `OPTIMAL`, `COMPATIBLE`, or `SAFE`;
1 for `COUNTEREXAMPLE`, `NOT_LINEARIZABLE`, `INFEASIBLE`, or `BREAKING`; 2 for invalid or unsupported requests;
3 for `UNKNOWN`, `UNRESOLVED`, or `FEASIBLE`; 4 for an internal operation contract error; and 5 for `UNAVAILABLE`.
