# Node bill-splitting example

## Application behavior

| Rule | Behavior |
| --- | --- |
| Amounts | Whole cents. |
| Shares | Integer weights per person. |
| Leftover cents | Assigned by fractional share, with command-line order breaking ties. |
| Invalid input | Invalid weights, negative totals, missing names, or unknown options produce exit code `2`. |

```powershell
node src/split-bill.js split 1000 ann=1 bob=1 --tip 333
```

| Person | Share |
| --- | --- |
| Ann | 6.67 |
| Bob | 6.66 |
| Total | 13.33 |

## Run the check

1. Install VKit and Node.
2. Copy this directory to a separate location.
3. Initialize and commit a Git repository there.
4. Run these commands from that directory:

```powershell
vkit doctor --project .
vkit accept --project .
vkit check run --project . --check split-bill-behavior
vkit console --project .
```

## Coverage

| Registered check | Cases |
| --- | --- |
| `split-bill-behavior` | Weighted shares, zero amounts, one person, tips, and leftover cents. |

The driver launches the CLI and compares printed splits with expected values.

See the [main README](../../README.md) for installation and agent setup.
