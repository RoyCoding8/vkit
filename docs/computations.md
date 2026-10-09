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
[egglog 13.2.0](https://egglog-python.readthedocs.io/stable/), and greenery 4.2.2.
Perses and the compiled Porcupine helper are configured separately.

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

## Protect the service

These APIs expose fixed operations. They contain no method for changing the parser, solver translation,
rewrite rules, or failure predicate. To enforce that boundary against an agent with a shell, install vkit,
its dependencies, the Perses JAR, and the history helper under a separate owner or in a service container
the agent cannot write.
Give the service access to candidate repository files. Keep its interpreter, working directory, import path, and environment
under the service owner's control. Running an editable vkit checkout as the same operating-system user as
the agent does not enforce immutability. This release does not provision that deployment isolation.

CLI exit codes are 0 for `PROVED`, `REDUCED`, `EQUIVALENT`, or `LINEARIZABLE`, 1 for `COUNTEREXAMPLE` or
`NOT_LINEARIZABLE`, 2 for invalid or unsupported requests,
3 for `UNKNOWN` or `UNRESOLVED`, 4 for an internal operation contract error, and 5 for `UNAVAILABLE`.
