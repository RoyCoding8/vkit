# Lean proofs against frozen statements

`Challenge.lean` holds statements a human wrote and froze. Its digest is pinned in the manifest as
`challenge_sha256`, so editing it blocks the check until someone accepts the new statements. An agent writes the
proofs in `Solution.lean`. [comparator](https://github.com/leanprover/comparator) checks that the solution
states exactly the same theorems, uses only the permitted axioms, and passes the kernel. `sorry` fails because
it adds the `sorryAx` axiom.

## Tools

Build each of these with `lake build` and put its `.lake/build/bin` on PATH:

- [comparator](https://github.com/leanprover/comparator) and [lean4export](https://github.com/leanprover/lean4export).
- On Windows, `tools/comparator-windows` in this repository. It provides `landrun` and `which` stand-ins.
  The `landrun` stand-in gives no sandbox, and the evidence records `build sandbox: none (landrun passthrough)`.
  On Linux, install the real [landrun](https://github.com/Zouuup/landrun).

## Run

```powershell
vkit accept --project .
vkit check run --project . --check cart-theorems
```
