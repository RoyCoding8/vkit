# Lean proofs against frozen statements

## Files and verification roles

| Component | Role |
| --- | --- |
| `Challenge.lean` | Statements written and frozen by a human. |
| `challenge_sha256` | Manifest pin for the challenge digest. Editing the challenge blocks checking until new statements are accepted. |
| `Solution.lean` | Proofs written by the agent. |
| [comparator](https://github.com/leanprover/comparator) | Checks matching theorem statements, permitted axioms, and kernel acceptance. |
| `sorry` | Introduces `sorryAx`, so the check fails. |

## Required tools

Build each Lean tool with `lake build` and put its `.lake/build/bin` directory on PATH.

| Platform | Tools |
| --- | --- |
| All | [comparator](https://github.com/leanprover/comparator) and [lean4export](https://github.com/leanprover/lean4export). |
| Windows | Build [tools/comparator-windows](../../tools/comparator-windows/README.md) for the `landrun` and `which` stand-ins. |
| Linux | Install real [landrun](https://github.com/Zouuup/landrun). |

## Windows isolation limit

| Item | Behavior |
| --- | --- |
| `landrun` stand-in | Runs the command without a sandbox. |
| Evidence | Records `build sandbox: none (landrun passthrough)`. |
| Comparator checking | Still checks statements, permitted axioms, and kernel acceptance. |

## Run the check

From this example's repository root:

```powershell
vkit accept --project .
vkit check run --project . --check cart-theorems
```
