# Cedar policy comparison

`compare_authorization_policies` checks whether two captured Cedar policy sets make the same authorization decision for every request in one declared family: one action UID, one principal entity type, and one resource entity type under a supplied Cedar schema. It uses Cedar's experimental SymCC command with the pinned cvc5 solver. An `EQUIVALENT` result concerns that model and family; it does not prove that application routes call Cedar correctly or that the supplied schema and policies capture every application rule.

The operation accepts a repository-relative `path` to a version-1 JSON bundle:

```json
{
  "version": 1,
  "schema": "entity User = { department: String };\nentity Document;\naction \"view\" appliesTo { principal: [User], resource: [Document], context: {} };",
  "action": "Action::\"view\"",
  "principal_type": "User",
  "resource_type": "Document",
  "before": [
    "permit(principal, action == Action::\"view\", resource) when { principal.department == \"eng\" };"
  ],
  "after": [
    "permit(principal, action == Action::\"view\", resource) when { principal.department == \"sales\" };"
  ]
}
```

The schema and policy strings are captured input data. Policies are static Cedar policies; template-linked policies are outside this operation. Type identifiers must be unnamespaced, and the action must use the `Action::"id"` form. The bundle is bounded to 512 KiB, each source string to 128 KiB, and each policy set to 128 entries; empty sets use Cedar's default-deny behavior. Cedar remains responsible for parsing and validating its policy and schema language.

`COUNTEREXAMPLE` includes a SymCC request and entity store only after the pinned Cedar CLI's witness has been parsed and independently replayed by Cedar's concrete `authorize` command against both policy sets. The returned before/after decisions must differ. `EQUIVALENT` requires SymCC's completed no-counterexample result. Unknown CLI output, solver timeout, an unsupported witness value, or a replay disagreement returns `UNKNOWN`; malformed bundles are rejected as input errors; missing or hash-mismatched configured executables return `UNAVAILABLE`.

Witness replay currently accepts Cedar's displayed strings, booleans, integers, sets, records, entity UIDs as parents, and simple entity UIDs. If SymCC emits a value outside that parser, the operation reports `UNKNOWN` rather than treating the symbolic result as a concrete authorization example. The operation never loads, imports, or executes application code.

Install the fixed release archives with:

```powershell
python scripts/install_policy_engine.py --dest .cache/policy-engine
```

The installer verifies the release archive SHA-256 and executable version, then prints the exact paths and executable hashes as JSON. Configure those values as `VKIT_CEDAR_PATH`, `VKIT_CEDAR_SHA256`, `VKIT_CVC5_PATH`, and `VKIT_CVC5_SHA256`. Cedar receives the configured cvc5 path through its `CVC5` child-process environment variable. Supported release pairs are Cedar CLI 4.13.0 experimental and cvc5 1.3.1 on Windows x86-64, Linux x86-64/aarch64, and macOS x86-64/arm64. The experimental archive is required because the standard Cedar CLI archive does not include SymCC.

The Cedar project describes SymCC as a symbolic compiler and exposes policy-set equivalence checks with counterexample variants. Its Lean specification README identifies the symbolic authorizer and `isAuthorized` model. This vkit operation reports a result from the released Rust CLI plus cvc5; it does not independently re-prove the upstream implementation. See [Cedar SymCC API](https://docs.rs/cedar-policy-symcc/0.7.0/cedar_policy_symcc/) and the [Cedar Lean model](https://github.com/cedar-policy/cedar-spec/blob/main/cedar-lean/README.md).
