# Change-certification test corpus

This development corpus contains twelve labeled changes. The frozen requirements, supported domains, guards, literal observations, and expected judgments live in `manifest.json`. The Rust files under `fixtures/` provide small before-and-after implementations for the eight runtime cases. Four policy-only cases record a required judgment and witness without claiming executable evidence.

Run the corpus from the repository root with:

```powershell
python scripts/check_change_corpus.py --output-dir tmp/change-certification
```

The runner compiles each fixture with plain `rustc --test`, runs its tests, and compares emitted observations with the manifest. It exits nonzero if `rustc` is missing, compilation or execution fails, or observations differ. The output directory stores the compiled test binaries, one captured native log per source, and `report.json`. The report records the manifest and source hashes and the `rustc --version` output.

The runtime cases contain finite literal samples. The runner checks that native outputs match those recorded observations and that the Rust tests pass. It reports manifest judgments as expected labels, but does not adjudicate them. The policy-only expectations are listed, not executed. The run does not prove general equivalence or establish certification accuracy on unseen changes.

The [change-analysis integration reference](../../docs/change-analysis.md) describes intent preflight and the execution APIs.
