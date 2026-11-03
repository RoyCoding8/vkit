# Contributing

## Development setup

Requires Python 3.11 or newer, Git, and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --extra test
uv run pytest
uv build
```

The [CI workflow](.github/workflows/ci.yml) runs the test suite on Windows, Linux, and macOS, checks wheel installation, and exercises Lean and TLC. Additional verifier tools are required for the [ledger qualification workflow](.github/workflows/ledger-qualification.yml).

## Frontend

See [Console assets](tools/console/README.md) for the Node build commands and dependency licenses. Generated assets are included in the Python package.

## Documentation media

The README animation uses recorded CLI output from the Python example. Rebuild it from the repository root with:

```sh
uv run --with pillow python scripts/record_readme_demo.py
```

The recorder validates the check outcomes and gate transitions before writing `docs/assets/broken-change.gif`. Raw command output is retained under `tmp/`. Pillow is an asset-build dependency only.

## Verifier pins

The resource-ledger package pins the official Verus archive and the inventory of its extracted files. To derive the inventory pin from the pinned archive:

```sh
uv run python scripts/check_verus_ledger.py --pin-extracted --archive /path/to/pinned/archive
```

The command validates the archive hash before deriving the inventory. Pin changes alter package identity and require renewed acceptance.

## Verification models and test data

- [Change-analysis integration reference](docs/change-analysis.md)
- [TLA+ qualification models](verification/certification/README.md)
- [Change-certification corpus](examples/change-certification/README.md)
- [Verifier engine probes](examples/change-certification/proofs/README.md)
- [Kani export fixtures](tests/fixtures/kani-results/README.md)
