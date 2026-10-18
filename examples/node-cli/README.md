# Node bill-splitting example

This CLI splits a bill and optional tip between people with integer weights.
Amounts use whole cents. It distributes leftover cents by fractional share,
with command-line order deciding ties.

```powershell
node src/split-bill.js split 1000 ann=1 bob=1 --tip 333
```

The result assigns 6.67 to Ann and 6.66 to Bob, totaling 13.33. Invalid weights,
negative totals, missing names, and unknown options cause exit code 2.

## Run the registered check

Install vkit and Node. Copy this directory to a separate location, then
initialize and commit a Git repository there. From that directory:

```powershell
vkit doctor --project .
vkit check run --project . --check split-bill-behavior
vkit console --project .
```

The driver launches the CLI and checks its printed splits against expected
values. Cases include weighted shares, zero amounts, one person, tips, and
leftover cents.

See the [main README](../../README.md) for installation and agent setup.
