<p align="center">
  <img src="docs/assets/logo.svg" width="80" height="80" alt="vkit">
</p>
<h2 align="center">VKit</h2>

[![CI](https://github.com/RoyCoding8/vkit/actions/workflows/ci.yml/badge.svg)](https://github.com/RoyCoding8/vkit/actions/workflows/ci.yml)

Machine-checked evidence for coding agents, and for the people who review their work.

You register checks for a repository and accept them. vkit runs them and records what each one established
about the files as they are now. An agent asks vkit what is known. It might hear that `checkout-flow` passed
for these exact files, that `latency` is stale because `server.js` changed, or that a Lean theorem holds. The
agent cannot supply a command, and it cannot accept a check. The gate is READY only when every check passed
against the current inputs.

This is a 0.2 beta.

## Install

Python 3.11 or later, and [uv](https://docs.astral.sh/uv/).

```powershell
git clone https://github.com/RoyCoding8/vkit.git
Set-Location vkit
uv sync --extra mcp
```

Each check brings its own tool, such as Node, pytest, Lean or ruff. `vkit doctor` reports which ones are
missing.

## Try it

```powershell
Copy-Item -Recurse examples/node-http C:\work\items
Set-Location C:\work\items
git init; git add .; git commit -m "items service"
vkit accept --project .                     # review what each check runs, then accept
vkit check run --project . --needed         # run every check without fresh evidence
vkit gate --project .                       # READY, REJECTED or BLOCKED
```

Edit `src/items-server.js` and `vkit status --project . --path src/items-server.js` reports both checks
`stale`. It also lists the features that file implements. `vkit check run --needed` reruns exactly those two
checks.

## What a check can be

| Kind | Evidence category | What a PASS establishes |
| --- | --- | --- |
| `scenario` | scenario | A driver you wrote ran these named cases and saw these results |
| `pytest`, `node_test` | scenario | These named tests ran and passed |
| `property` | property | Hypothesis found no counterexample under the pinned settings |
| `static` | static analysis | The analyzer's SARIF report has no finding outside the pinned baseline |
| `lean` | theorem | The kernel accepted proofs of the frozen statements, within the permitted axioms |
| `tlc` | finite model | Every reachable state of one finite TLA+ model satisfies the properties |

Each result says what it does not establish as well. A scenario PASS says nothing about inputs that were
not run.

A scenario check can report measurements, such as `p95_ms`, and declare budgets on them. Budgets fail a run
that exceeds a limit or regresses past a baseline that a human pinned with `vkit baseline`. A static check
fails only on findings outside its pinned baseline, so existing debt does not block work and new debt does.

## Connect an agent

Configure a stdio MCP server with command `vkit` and arguments `mcp serve --project <repository>`. The tools
are:

- `status`: freshness of every check, or only the checks that read the paths you pass.
- `features`: the feature map, including what users can do, how they reach it, and the known gaps.
- `check_run`: runs checks by id, or with `needed: true` runs every stale or missing one.
- `run_get`: reads one run's outcome and log.
- `run_cancel`: stops a run.
- `gate`: returns READY, REJECTED or BLOCKED.
- `propose`: suggests a check, a feature entry, or a new file. Nothing changes until a human runs
  `vkit accept --proposal <digest>`.
- `check_rewrite`: prove equivalent return values or produce a counterexample for a supported Python function.
- `simplify_function`: derive a replacement with egglog and independently verify it with cvc5.
- `reduce_failure`: use Perses to shrink a source file while preserving a recorded failure.

The three computation tools use fixed algorithms and do not change the gate. Install their Python
engines with `uv sync --extra mcp --extra engines`. See [built-in computations](docs/computations.md)
for the supported language, CLI commands, Perses setup, and deployment boundary.

For Claude Code, `plugin/` holds two skills and two hooks. The session-start hook reports the gate. The stop
hook holds the session while a check the agent can rerun is stale or failing.

## Watch it

```powershell
vkit console --project .
```

This opens a read-only workspace on `127.0.0.1:8765`. The collapsible sidebar provides an overview,
check definitions and their limits, run outcomes and logs, verification methods, feature coverage,
pending proposals, and project setup. It follows the system's light or dark theme; you can change
the theme in the top bar. Assets ship locally. Pass `--port 0` to pick a free port.

Frontend development and asset rebuild instructions are in [tools/console](tools/console/README.md).

## Commands

| Command | What it does |
| --- | --- |
| `vkit status [--path P]` | Show each check's freshness and the gate |
| `vkit check run --check ID \| --needed` | Run checks in the foreground |
| `vkit gate` | Exit 0 READY, 1 REJECTED, 3 BLOCKED |
| `vkit accept [--check ID] [--proposal D]` | Review and accept check definitions or a proposal |
| `vkit proposals`, `vkit reject --proposal D` | Review what agents proposed |
| `vkit baseline --check ID` | Pin a run's measurements and findings as the baseline |
| `vkit run show --run R`, `vkit run cancel --run R` | Read or stop a run |
| `vkit features` | Show the feature map and audit it |
| `vkit doctor` | Report missing tools |
| `vkit project inspect` | Find the test commands a repository already declares |
| `vkit console` | Serve the read-only console |
| `vkit mcp serve` | Serve MCP over stdio |
| `vkit compute check-rewrite` | Check equivalence of two supported Python functions |
| `vkit compute simplify-function` | Suggest a replacement and prove its equivalence |
| `vkit compute reduce-failure` | Reduce a recorded failure with its approved predicate |

Every command takes `--project` (default `.`) and `--json`.

## Limits

- Acceptance binds MCP clients. An agent with a shell can run `vkit accept` itself.
- Evidence is keyed by the content of a check's declared inputs. A check that reads an undeclared file can be
  reported fresh after that file changes. Leave `inputs` empty to key on the whole tree.
- Check logs are not redacted.
- On Windows, agent-written Lean proofs build without a sandbox. See [examples/lean-proof](examples/lean-proof/README.md).
- TLC checks have no automated coverage in this release.

[docs/verification.md](docs/verification.md) is the reference.

## Development

```powershell
uv sync --extra test --extra engines
uv run pytest
uv run python scripts/e2e.py
```

vkit verifies itself. `verification/manifest.json` registers its end-to-end runs, its tests, ruff, and a
no-comments ratchet.

## License
Apache 2.0
