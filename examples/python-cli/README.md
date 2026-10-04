# Python totals example

This CLI reads integer amounts and prints their sum. With no amounts it prints
zero. The check driver launches the application and compares its output with
literal expected totals.

```powershell
python src/totals.py 10 20 12
# 42
python src/totals.py 50 -20 12
# 42
python src/totals.py
# 0
```

## Run the registered check

Install vkit, copy this directory to a separate location, and initialize and
commit a Git repository there. From that directory:

```powershell
vkit doctor --project .
vkit check run --project . --check totals-behavior
vkit features --project .
vkit console --project .
```

The check covers an empty cart, one positive amount, several positive amounts,
mixed signs, negative amounts, and amounts that cancel to zero.

To see a failure, change `running += amount` in `src/totals.py` to
`running += amount + 1`. Cases with amounts now produce incorrect totals.
Restore the original line and run the check again.

See the [main README](../../README.md) for installation and agent setup.
