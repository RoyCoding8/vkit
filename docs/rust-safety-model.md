# Rust safety model

`check_rust_safety` asks the pinned Kani verifier to check one captured Rust function over its full primitive input domain. The operation parses the source into a closed typed subset, generates a canonical Rust function and a verifier-owned Kani harness, then runs Kani on that generated file. Candidate text is never pasted into a harness or sent to a shell.

## Supported model

The source file must contain exactly one free function with a body. Parameters and initialized locals are named scalar values of type `u8`, `u16`, `u32`, `u64`, `i8`, `i16`, `i32`, `i64`, or `bool`; return values use one of those types or `()`. The subset includes integer and boolean literals, arithmetic, comparison, bitwise and logical operators, unary minus and not, simple assignments, `if`/`else`, `while`, and `loop` with `break` or `continue`. Rust integer types and operations are retained in a typed intermediate representation before source generation.

Kani checks Rust overflow, division and remainder by zero or signed overflow, invalid shift counts, panics, and the requested loop-unwind assertions. Kani's unconstrained values cover every bit pattern for each primitive parameter. The selected unwind defaults to 8 and is limited to 1–32. A proof is `SAFE` only when Kani reports its single generated harness completed and every reported check succeeded. A function with no applicable safety checks can complete with zero check rows; that means the closed function contains no operation in the selected check families.

The model excludes pointers, references, heap allocation, `unsafe`, macros, attributes, generics, calls, external state, concurrency, async, `for`/`do` loops, and source files with additional items. Unsupported syntax is rejected before invoking Kani. Functions with loops are only proved for the explicit unwind setting; a failed unwind assertion is `UNKNOWN`, not a counterexample or a safety result.

## Results

- `SAFE`: the generated harness completed and all emitted Kani checks succeeded for the stated input domains and unwind setting.
- `COUNTEREXAMPLE`: Kani reported exactly one recognized safety failure, concrete playback supplied parameter bytes, and an independent interpreter of the parsed typed model reproduced the same failure class.
- `UNKNOWN`: timeout, output limit, unfinished or inconsistent engine result, unsupported failure description, replay mismatch, or insufficient unwind.
- `UNSUPPORTED`: source or requested limits fall outside the closed syntax and model.
- `UNAVAILABLE`: the pinned runtime or required parser is absent, its identity does not match, or the host is outside the supported platform.

The independent replay is a check on the witness and lowering semantics. It does not replace Kani's proof engine. Witnesses are decoded as Rust-width little-endian values; replay uses bounded integer arithmetic, truncation for bitwise shifts, signed division truncating toward zero, and Rust overflow checks. Any result the interpreter cannot replay remains `UNKNOWN`.

## Runtime and validation

The proof runtime is Kani 0.68.0 with CBMC 6.11.0 and `nightly-2026-08-21` (`rustc 1.100.0-nightly (8925ea358 2026-08-20)`) on Linux x86_64. The official Kani bundle SHA-256 is `32e2b484d73ede0bbf64a2cf0879c4259422497d8aec4ee67d448de9ae7843d3`. The Rust parser is pinned to `tree-sitter 0.25.2` and `tree-sitter-rust 0.24.2`.

The repository installer follows [Kani's installation guide](https://model-checking.github.io/kani/install-guide.html) and pins the [0.68.0 release](https://github.com/model-checking/kani/releases/tag/kani-0.68.0). It requires Rust and rustup on `PATH` and installs under a caller-selected directory without modifying system tool directories:

```sh
python3 scripts/install_rust_engine.py --root /opt/vkit/kani-runtime
```

It writes `vkit-kani-runtime.json` with `VKIT_KANI_BIN`, its SHA-256, `VKIT_KANI_HOME`, and `VKIT_RUSTUP_HOME`. The exact layout is `cargo/bin/kani` and `cargo/bin/rustup`, `cargo-home/bin/rustup` for Kani setup, `kani-home/kani-0.68.0` for the extracted release, `bundle-cache/kani-0.68.0-x86_64-unknown-linux-gnu.tar.gz` for the verified source archive, and `rustup-home/toolchains/nightly-2026-08-21-x86_64-unknown-linux-gnu` for the compiler. The operation creates a temporary work directory with isolated home and Cargo configuration, copies the pinned rustup launcher into that temporary Cargo home, invokes only the configured Kani binary, and uses a process-tree deadline and output budget. It also compiles the captured source independently with the pinned Rust compiler before proving the generated lowering, so source/lowering disagreements and ordinary Rust type errors are rejected. Set `VKIT_KANI_REQUIRED=1` in the real-engine CI job so missing runtime setup fails instead of skipping the integration tests. Windows and macOS currently return `UNAVAILABLE`; the focused release proof gate is Linux x86_64.

Real-engine tests prove a bounded increment, replay an overflowing `u8` input, replay an invalid shift count, and require an insufficiently unwound loop to remain `UNKNOWN`. The operation does not claim whole-program Rust safety or concurrency verification.
