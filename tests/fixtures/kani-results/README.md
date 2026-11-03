These seven JSON files are sanitized copies of retained Kani 0.68.0 exports from
`rust-relational-qualification`. The corresponding harness bytes are retained
alongside each export. Only temporary output paths were replaced. The fixtures
are transcript samples, not a new Kani run, and they do not assert capture
hashes or authenticate source-check completeness.

`preservation`, `guarded`, `repair`, and `regression` come from `run-002`.
`outside-model` and `overflow` come from `run-003`.
`identity` comes from `run-004`; its candidate source has no overflow check,
while the base source has one.
