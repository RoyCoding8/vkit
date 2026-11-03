# Change-certification engine qualification

This page documents the Kani and Verus qualification fixtures.

`experiment-kani.rs` imports `scalar.rs` from the runner's private output directory. The runner copies the repository scalar fixture there before invoking Kani, so the refactor and regression harnesses call the same function bodies as the native fixture tests.

Run the probes with installed Linux tools and a new output directory:

```sh
python3 scripts/check_change_engines.py --kani /path/to/kani-driver --verus /path/to/verus --output-dir /tmp/change-engines-run
```

The Kani cases cover a bounded refactor proof, a seeded regression, unwind exhaustion, an undefined-behavior safety failure, and an observation mismatch. For the safety failure, the runner records the generated witness and labels native execution `NOT_EXECUTED_UNSAFE`.

The Verus files contain a small relational arithmetic adaptation over inputs from -16 through 16. They restate the arithmetic functions and do not verify the copied Rust source. They are not the sealed Docker ledger profile or a proof of arbitrary Rust.

The runner records copied inputs, tool identities, input hashes, command arguments, return codes, and logs. Tools must be installed separately. Engine results require the expected exit code and proof diagnostics.

A completed run reports `PASS_WITH_UB_PLAYBACK_NOT_EXECUTED`. The runner executes only the safe cap regression playback, which must fail once with values 10 and 9.
