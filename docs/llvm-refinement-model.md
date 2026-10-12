# LLVM refinement model

`check_llvm_refinement` asks whether one captured LLVM function in `after_path` refines the
same function in `before_path`. The direction matters. The operation does not check the reverse
transformation unless you call it again with the inputs swapped.

The operation reads each repository-relative `.ll` file once, records its SHA-256, and writes
those captured bytes to a private temporary directory. It runs a fixed LLVM20 validator and the
pinned Alive2 `alive-tv` binary. Callers cannot provide engine options, commands, or query text.

Each module must contain exactly one definition with the requested function name and no other
function declarations, globals, aliases, inline assembly, or named metadata. The function must
have the same C calling convention and scalar integer signature in both files. Integer types
range from `i1` through `i64`. The validator accepts integer arithmetic, signed and unsigned
division and remainder, shifts, bitwise operations, comparisons, selects, integer casts, `phi`
nodes, branches, and returns. It accepts `nsw` and `nuw` only for add, subtract, multiply, and
shift-left. It accepts `exact` only for unsigned or signed division and right shifts. The
control-flow graph must be acyclic. The validator rejects memory operations, calls, loops,
explicit `undef` and `poison` constants, constant expressions, metadata attachments, attributes,
and all other instructions and flags. Function parameters still use Alive2's default semantics,
which allow an input to be `undef` or `poison`.

The validator parses both modules with LLVM 20.1.8 and reports the selected function signature,
target triple, data layout, block count, and instruction count. The operation requires equal
signatures, target triples, and data layouts before it calls Alive2. A malformed or unsupported
module returns `UNSUPPORTED`. A missing or incorrectly pinned binary returns `UNAVAILABLE`.

`REFINES` means Alive2 completed one check with one correct function, no incorrect functions,
no failed proofs, and no errors. `COUNTEREXAMPLE` means Alive2 completed the check, reported a
value mismatch, and returned a witness. The witness comes from Alive2 and is not independently
replayed by this operation. A timeout, output limit, process failure, parser mismatch, or any
unrecognized result returns `UNKNOWN`. No result covers source-language behavior, compiler
translation, other functions, linked modules, runtime behavior, or loops.

## Install the Linux engine

Install `llvm-20-dev=1:20.1.8-2ubuntu8` and the build tools. The verified Ubuntu build used
`cmake`, `ninja-build`, `g++`, `git`, `re2c`, `libz3-dev`, `zlib1g-dev`, `libedit-dev`,
`libcurl4-openssl-dev`, and `libzstd-dev`. Then run the installer with a dedicated destination
directory:

```sh
python3 scripts/install_llvm_engine.py --root /opt/vkit/alive2-v20
```

The installer clones the official Alive2 `v20.0` tag and checks that its commit is
`c0f5434f402ad91714ee0952f686cd0f524920ad`. It requires `llvm-config-20 --version` to return
`20.1.8`, builds `alive-tv`, compiles the validator against LLVM 20, and writes a manifest with
both binary hashes. It does not install system packages or change global environment settings.

Configure `VKIT_ALIVE2_BIN` and `VKIT_ALIVE2_SHA256` from the manifest. Configure
`VKIT_LLVM_IR_VALIDATOR_BIN` and `VKIT_LLVM_IR_VALIDATOR_SHA256` from the same file. The
operation verifies both binary hashes and checks each binary's release identity before use.
The installer supports Linux. Other hosts return `UNAVAILABLE` unless they have matching native
Alive2 and validator binaries with configured SHA-256 pins.

## Example

For this `before.ll`:

```llvm
define i8 @step(i8 %value) {
entry:
  %next = add i8 %value, 1
  ret i8 %next
}
```

and this `after.ll`:

```llvm
define i8 @step(i8 %value) {
entry:
  %next = add i8 %value, 1
  ret i8 %next
}
```

the operation can establish `REFINES` for the selected `step` function. Replacing the after
body with `ret i8 %value` gives Alive2 a directional value-mismatch witness.
