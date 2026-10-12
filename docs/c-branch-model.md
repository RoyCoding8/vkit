# C branch reachability model

`inspect_c_branch_reachability` asks whether one selected arm of an `if` statement can execute in a captured C
function. It reuses the bounded standalone scalar subset in [`c_safety.py`](../src/vkit/operations/c_safety.py),
then asks CBMC to fail one protected assertion placed at that arm. The operation models C11 on x86_64 Linux LP64
with little-endian byte order and 32-bit `int`.

The function must be the only definition in the source file. It may have up to 32 named `int`, `unsigned int`, or
`_Bool` parameters. The existing C safety profile limits the source to 65,536 bytes, 8,192 AST nodes, a nesting
depth of 128, 64 locals, and fixed local arrays of at most 256 elements. Globals, includes, external calls,
pointers, and unsupported C syntax are rejected. Loops are bounded by `unwind`, which ranges from 1 to 256.

The result includes every selectable arm as a `branch_id`, source line, source column, and arm name. IDs use the
form `if@<line>:<column>:then` or `if@<line>:<column>:else`; an `else` ID exists only when the source has an
explicit `else`. Callers should use the returned inventory instead of reconstructing source coordinates.

`REACHABLE` requires CBMC to report failure for the exact selected assertion, provide a typed value for every
scalar input, and finish with the expected process status. The operation then compiles a fixed replay harness with
the operator-pinned x86_64 Linux GCC using UBSan and no recovery. Static assertions check the modeled ABI. The
replay must exit cleanly and print exactly `VKIT_BRANCH_ID:<branch_id>`. A solver failure alone never establishes a
hit. Replay compiles a protected AST rendering of the captured source and returns the SHA-256 of the raw captured
bytes alongside hashes for the instrumented model and replay source. The native helper prints the marker and calls
`_Exit(0)` at the selected arm's entry. This confirms the path through that arm's condition and entry point; it does
not validate statements after the selected arm begins or normal function completion. UBSan catches the selected
undefined behaviors enabled by the compiler runtime, but it does not establish that every C execution is defined.

`UNREACHABLE` requires the selected assertion, every selected safety property, and every unwinding assertion to
report success, with CBMC's successful process status. An incomplete unwind, failed unrelated safety property,
missing property, timeout, malformed trace, or replay mismatch returns `UNKNOWN`. Unsupported source or an
unknown ID returns `UNSUPPORTED`. Missing or mismatched toolchains, or a non-Linux host, return `UNAVAILABLE`.

Configure the trusted backends with `VKIT_CBMC_BIN` and `VKIT_CBMC_SHA256` for CBMC 6.11.0, and
`VKIT_C_BRANCH_GCC_BIN` and `VKIT_C_BRANCH_GCC_SHA256` for the native replay compiler. Both executables are
verified by SHA-256 before use. The compiler must identify its target as `x86_64-linux-gnu`. CBMC and replay share
one timeout budget and each process has an 8 MiB combined stdout/stderr limit. Source and generated translation
units are bounded by the C safety profile's source limit. `timeout_seconds` ranges from 1 to 60.

| Status | CLI exit code | Meaning |
| --- | ---: | --- |
| `REACHABLE` | 0 | A concrete typed input reached the selected arm under native UBSan replay. |
| `UNREACHABLE` | 1 | The selected arm is unreachable within the completed model and checks. |
| `UNKNOWN` | 3 | The model, checks, trace, or replay did not establish either result. |
| `UNSUPPORTED` | 2 | The source shape or requested branch ID is outside the supported model. |
| `UNAVAILABLE` | 5 | A required pinned runtime or Linux ABI is unavailable. |

This result concerns one selected arm of one captured function. It is not full-program reachability or a claim
about behavior outside the stated scalar model and loop bound.
