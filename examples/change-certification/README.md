# Change certification fixtures

This development corpus contains twelve labeled changes. The frozen requirements, supported domains, guards, literal observations, and expected judgments live in `manifest.json`. The Rust files under `fixtures/` provide small before-and-after implementations for the eight runtime cases. Four policy-only cases record a required judgment and witness without claiming executable evidence.

Run the corpus from the repository root with:

```powershell
python scripts/check_change_corpus.py --output-dir tmp/change-certification
```

The runner compiles each fixture with plain `rustc --test`, runs its tests, and compares emitted observations with the manifest. It exits nonzero if `rustc` is missing, compilation or execution fails, or observations differ. The output directory stores the compiled test binaries, one captured native log per source, and `report.json`. The report records the manifest and source hashes and the `rustc --version` output.

The runtime cases contain finite literal samples. The runner checks that native outputs match those recorded observations and that the Rust tests pass. It reports manifest judgments as expected labels, but does not adjudicate them. The policy-only expectations are listed, not executed. The run does not prove general equivalence or establish certification accuracy on unseen changes.

## Accepted finite intent preflight

`vkit.intent.preflight_change_request(project, request_json, trusted_target_oid=...)` binds a finite request to an accepted intent in an exact Git target. The trusted caller supplies the target separately. The request's base must equal that target, and the target must be an ancestor of its candidate. An unchanged candidate is allowed. Branch names and working-tree sources are unsupported.

The target tree contains two kinds of files:

- `verification/change-intents/accepted.v1.json` lists accepted revision digests under `accepted`, with `schema_version` set to `1`.
- `verification/change-intents/<digest>.v1.json` contains the corresponding intent revision. Its digest is `IntentRevision.from_json(document).digest`.

The [revision schema](../../schemas/intent-revision.v1.json) requires a literal requirement source and statement, a finite contract, and four semantic artifacts. Each artifact pins the path and raw-byte SHA-256 of the binding map, codec, observation policy, or environment model. The last three hashes must also match the finite domain. The revision digest includes the complete normalized contract, requirement, and artifact identities. Acceptance is a separate target-owned list, described by the [acceptance schema](../../schemas/accepted-intents.v1.json).

The accepted statement is stored literally. `requirement.source` is a citation label, which can name a document, issue, or external reference. Preflight retains that label but does not resolve it or check that external text matches the accepted statement.

Git retains revisions and acceptance changes through the project's protected review process. Preflight reads captured target objects, not the working tree, candidate policy, or local `approved.json`. It checks that every declared semantic artifact is present and unchanged in both exact commits. Candidate-side intent files cannot accept a new revision. Application source may change.

The request's claim must cover every legal input and output of the accepted intent on the same semantic domain. Empty intent domains, changed domains, and claims that exclude legal behavior fail preflight. A weaker entailed claim can bind, but this grants no delegated authority.

The returned `BoundChangeRequest` contains the typed request and intent, source capture identities, and policy blob identities. Its JSON projection reports `scope: "request_preflight"` and `candidate_behavior_checked: false`. Its digest identifies that preflight data, not a reusable proof receipt. Preflight is read-only and does not execute application code, grant approvals, or report a certification pass.

This finite preflight is not exposed through the public CLI or MCP `verify` request. Public verification currently accepts only version 2 package claims with exact component-policy and package SHA-256 pins. The CLI's `vkit verify COMPONENT` form resolves those pins from the current manifest. No finite request can create a source-verification job or receipt.

This preflight trusts the caller's target selection and VKit installation. It does not authenticate a human, discover transitive dependencies, interpret the declared binding map, or establish source conformance. The finite request remains separate from package source verification and the protected gate.

## Held-out cases

`held-out/README.md` reserves a separate location for future held-out material. It contains no cases or evaluation results. The twelve cases in this development corpus are not unseen evaluation data.
