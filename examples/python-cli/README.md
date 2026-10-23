# Python totals example

## Application behavior

| Input | Output |
| --- | --- |
| `python src/totals.py 10 20 12` | `42` |
| `python src/totals.py 50 -20 12` | `42` |
| `python src/totals.py` | `0` |

The CLI reads integer amounts and prints their sum. The check driver launches the application and compares output with literal expected totals.

## Run the check

1. Install VKit and Python.
2. Copy this directory to a separate location.
3. Initialize and commit a Git repository there.
4. Run these commands from that directory:

```powershell
vkit doctor --project .
vkit accept --project .
vkit check run --project . --check totals-behavior
vkit features --project .
vkit console --project .
```

## Coverage

| Registered check | Cases |
| --- | --- |
| `totals-behavior` | Empty cart, one positive amount, several positive amounts, mixed signs, negative amounts, and cancellation to zero. |

## Observe a failure

1. In `src/totals.py`, replace `running += amount` with `running += amount + 1`.
2. Run `totals-behavior` again. Cases with amounts now produce incorrect totals.
3. Restore the original line and rerun the check.

See the [main README](../../README.md) for installation and agent setup.
