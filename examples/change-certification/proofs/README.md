# Change-certification engine probes

`experiment-kani.rs` imports `scalar.rs` from the runner's private output directory. The runner copies the repository scalar fixture there before invoking Kani, so the refactor and regression harnesses call the same function bodies as the native fixture tests.

Run the probes with installed Linux tools and a new output directory:

```sh
python3 scripts/check_change_engines.py --kani /path/to/kani-driver --verus /path/to/verus --output-dir /tmp/change-engines-run
```

The Kani cases separate a bounded refactor proof, a seeded regression, unwind exhaustion, an undefined-behavior safety failure, and a fixed observation mismatch. The pinned Kani run produced concrete playback for the safety failure. The runner records the proof failure and generated witness, then labels native execution `NOT_EXECUTED_UNSAFE`. It does not claim a native UB reproduction or a non-replayable UB case.

The Verus files contain a small relational arithmetic adaptation over inputs from -16 through 16. They restate the arithmetic functions and do not verify the copied Rust source. They are not the sealed Docker ledger profile or a proof of arbitrary Rust.

The runner records copied inputs, tool identities, input hashes, command arguments, return codes, and logs in the empty output directory. It never downloads tools. A successful version command identifies a tool. The runner classifies an engine result only from its exit code and required proof diagnostics.

A completed run reports `PASS_WITH_UB_PLAYBACK_NOT_EXECUTED`. The runner executes only the safe cap regression playback, which must fail once with values 10 and 9.
