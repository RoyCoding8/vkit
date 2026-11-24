# Contributing to VKit

This guide covers local setup, repository layout, test and lint commands, formal checks, and asset rebuilds.

## Prerequisites

Install Python 3.11 or newer, Git, and [uv](https://docs.astral.sh/uv/getting-started/installation/).

## Development setup

Sync the test dependencies, run the test suite, run the linter, and build the wheel from the repository root:

```sh
uv sync --extra test
uv run pytest -ra
uvx ruff@0.14.0 check --isolated --select E4,E7,E9,F src tests scripts formal
uv build
```

Verify that the built wheel runs cleanly in an isolated environment:

```sh
uv run python -c "from pathlib import Path; import subprocess; wheel = next(Path('dist').glob('*.whl')); subprocess.run(['uv', 'run', '--isolated', '--no-project', '--with', str(wheel.resolve()), 'vkit', '--help'], check=True)"
```

The [CI workflow](.github/workflows/ci.yml) runs these checks on Linux, Windows, and macOS, plus Lean and TLC formal verification jobs on Ubuntu. Additional verifier toolchains run in the [ledger qualification workflow](.github/workflows/ledger-qualification.yml).

## Repository layout

Keep code, tests, formal models, and helper scripts in their designated directories:

| Path | Contents |
| --- | --- |
| `src/vkit/cli.py` | CLI entry point, argument parser, and exit code mapping. |
| `src/vkit/core/` | Manifests, durable store, job runner, evidence identity, gates, proposals, and schemas. |
| `src/vkit/change/` | Change analysis, certification, intent binding, paired execution, and witnesses. |
| `src/vkit/rust/` | Rust source capture, relational analysis, and Kani execution and result parsing. |
| `src/vkit/formal/` | Formal verification service, Verus support, resource ledger, policy, and transition comparison. |
| `src/vkit/verifiers/` | Verifier adapters for pytest, Node, property tests, static checks, Lean, TLC, and external verifiers. |
| `src/vkit/console/` | Loopback HTTP console server, packaged static assets, TUI launcher, settings, and workspace registry. |
| `src/vkit/runtime/` | Subprocess execution, hidden-window helpers, container runtime, and project path resolution. |
| `src/vkit/mcp/` | MCP stdio server and tool catalog. |
| `formal/models/` | TLA+ models and configurations for certification, lifecycle, delegated claims, and finite transitions. |
| `formal/checks/` | Executable Python scripts that run Lean, TLC, authority, lifecycle, and verifier qualification checks. |
| `tests/` | Pytest suite mirroring `src/vkit/` (`core/`, `change/`, `rust/`, `formal/`, `verifiers/`, `console/`, `cli/`, `fixtures/`). |
| `scripts/tools/` | Maintainer scripts (`comment_lint.py`, `e2e.py`, `record_readme_demo.py`). |
| `tools/console/` | Node frontend sources for rebuilding `src/vkit/console/static/`. |
| `schemas/` | JSON schemas packaged into the wheel as `vkit/_schemas`. |
| `examples/` | Runnable example repositories and qualification fixtures. |
| `docs/` | User guides and technical references. |

## Test and hygiene rules

Run targeted suites while developing, then run the full suite before opening a pull request:

```sh
uv run pytest tests/core/test_source_hygiene.py
uv run pytest tests/core/ tests/cli/ tests/console/
```

`tests/core/test_source_hygiene.py` enforces four repository invariants:

1. Every test listed in `verification/manifest.json` must be collected by pytest.
2. Files under `src/` and `plugin/` must describe current behavior without planning narration.
3. Tests must not mutate `sys.path` directly.
4. Subprocess calls in `tests/` must unpack `**NO_WINDOW` from `tests/helpers.py` so Windows runs do not spawn console windows.

## Formal verification checks

Formal checks require Lean (via `elan`) and a pinned `tla2tools.jar` pointed to by `VKIT_TLA2TOOLS_JAR`. With both tools available, run the same formal suite that CI runs:

```sh
uv run python formal/checks/verify_formal.py --backend all --output tmp/vkit-formal-evidence
uv run pytest tests/formal/test_logical_trust.py tests/formal/test_tlc_evidence.py -q
uv run python formal/checks/check_lifecycle.py
uv run python formal/checks/check_authority.py --output tmp/vkit-authority-evidence
uv run python formal/checks/check_authority.py --suite differences --output tmp/vkit-difference-evidence
uv run python formal/checks/check_authority.py --suite shadow --output tmp/vkit-shadow-authority-evidence
uv run python formal/checks/check_delegated_claim_authority.py --output tmp/vkit-delegated-claim-evidence
uv run python formal/checks/check_difference_decisions.py --output tmp/vkit-difference-decisions
uv run python formal/checks/check_finite_decisions.py --output tmp/vkit-finite-decisions/report.json
uv run pytest tests/formal/test_transition_model.py -q
```

## Console frontend assets

Frontend build sources live in `tools/console/`. Generated assets are committed under `src/vkit/console/static/assets` so end users can run the console without Node.

Rebuild the assets from the repository root after editing frontend sources:

```sh
npm ci --prefix tools/console
npm run build --prefix tools/console
```

See [Console assets](tools/console/README.md) for icon updates and bundled dependency licenses.

## Documentation media

The animation in `README.md` uses recorded CLI output from `examples/python-cli/`. Rebuild it from the repository root:

```sh
uv run --with pillow python scripts/tools/record_readme_demo.py
```

The script validates the check outcomes and gate transitions before writing `docs/assets/broken-change.gif`. Raw command output stays under `tmp/`. Pillow is used only for building this asset.

## Verifier pins

The resource-ledger package pins the official Verus archive and the inventory of its extracted files. Derive the extracted-file inventory pin from a pinned archive:

```sh
uv run python formal/checks/check_verus_ledger.py --pin-extracted --archive /path/to/pinned/archive
```

The command validates the archive digest before generating the inventory. Changing a pin alters package identity and requires renewed acceptance.

## Verification models and test data

- [Change-analysis integration reference](docs/change-analysis.md)
- [TLA+ qualification models](formal/models/certification/README.md)
- [Change-certification corpus](examples/change-certification/README.md)
- [Verifier engine probes](examples/change-certification/proofs/README.md)
- [Kani export fixtures](tests/fixtures/kani-results/README.md)
