<p align="center">
  <img src="docs/assets/logo.svg" width="80" height="80" alt="vkit">
</p>
<h2 align="center">VKit</h2>


[![CI](https://github.com/RoyCoding8/vkit/actions/workflows/ci.yml/badge.svg)](https://github.com/RoyCoding8/vkit/actions/workflows/ci.yml)
[![Formal verifiers](https://github.com/RoyCoding8/vkit/actions/workflows/formal-verifiers.yml/badge.svg)](https://github.com/RoyCoding8/vkit/actions/workflows/formal-verifiers.yml)
[![Browser acceptance](https://github.com/RoyCoding8/vkit/actions/workflows/browser-acceptance.yml/badge.svg)](https://github.com/RoyCoding8/vkit/actions/workflows/browser-acceptance.yml)
[![PyPI](https://img.shields.io/pypi/v/vkit.svg)](https://pypi.org/project/vkit/)
[![Python](https://img.shields.io/pypi/pyversions/vkit.svg)](https://pypi.org/project/vkit/)

Run a registered check in a Git repository and keep the evidence for the result.

vkit runs a check you defined, records what passed and what failed, and keeps
each run on disk so you can read it later. Agents drive it over MCP. You read it
in a local web console.

This is a 0.1 beta.

## Install

Python 3.11 or later. Python 3.13.14 is required for the logic cleanup checks.

```powershell
git clone https://github.com/RoyCoding8/vkit.git
Set-Location vkit
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install ".[mcp]"
```

The `mcp` extra installs the MCP server SDK. Use `python -m pip install .` if you
only need the CLI and the console. Each check has its own external tool, such as
pytest, Node, Lean, or TLC. vkit reports which ones are missing.

## Run the first check

Every example ships with a registered check, so copy one out and run it against
a real Git repository.

```powershell
Copy-Item -Recurse examples/python-cli C:\work\totals
Set-Location C:\work\totals
git init
git add .
git commit -m "Add the totals CLI and its check"
```

```powershell
vkit doctor --project .
vkit check run --project . --check totals-behavior
vkit features --project .
```

`doctor` reports whether the tools this project's checks need are installed.
`check run` starts a run. `features` reports which declared features have
coverage and which do not.

Start here for the other two examples: [Node CLI](examples/node-cli/README.md)
and [Node HTTP](examples/node-http/README.md).

## Open the console

```powershell
vkit console --project .
```

The console listens on `127.0.0.1:8765` and opens a browser. Pass `--port 0` to
let the operating system pick a free port, or `--no-browser` to print the URL
without opening it. Stop it with Ctrl+C.

The console has views for the project identity, its checks, the runs and their
logs, the evidence each run published, tasks, cleanup records, recovery, and
settings. It reads the store on disk. Everything it can change is on the
Operations view.

## Register a check in your own repository

```powershell
vkit project inspect --project .
vkit project enroll --project .
```

Enrollment writes `verification/proposed-manifest.json`. Its commands and
expected scenarios are placeholders. Replace them with a driver that exercises
your application and describe the behavior it must observe, then accept the
digest the proposal displays:

```powershell
vkit project enroll --project . --accept --accept-digest <digest>
```

Registered checks live in `verification/manifest.json`.

## Connect an agent

Configure a stdio MCP server with command `vkit` and these arguments:

```text
serve --project D:/path/to/your/repository
```

The host has to find the `vkit` executable. An absolute path to the virtual
environment's executable works when it cannot. To read the tool catalogue:

```powershell
vkit mcp serve --project . --json
```

The six tools are `project_inspect`, `task_begin`, `check_start`, `run_get`,
`run_cancel`, and `task_finalize`. MCP does not pick a model and does not spawn
workers.

For Claude Code, install the plugin from the console's Settings view. The plugin
ships four skills, `vkit-onboard`, `vkit-work`, `vkit-verify`, and
`vkit-status`, plus hooks that bind a session, inspect edits, and check task
completion.

## Commands

| Command | What it does |
| --- | --- |
| `vkit doctor` | Report whether this project's checks could run |
| `vkit project inspect` | Show the repository and what vkit found in it |
| `vkit project enroll` | Propose a manifest, then accept it against its digest |
| `vkit check run` | Start a registered check and record the run |
| `vkit check start` | Start a check and keep the receipt for later |
| `vkit features` | Report declared feature coverage and the gaps |
| `vkit console` | Serve the console on loopback |
| `vkit mcp serve` | Serve the project to an agent over MCP |
| `vkit run show` | Read one run and its evidence |
| `vkit task` | Own a check contract, its evidence, and readiness |
| `vkit recover` | Inspect interrupted runs, claims, and processes |
| `vkit integration` | Decide whether a candidate commit meets the policy |

Every command takes `--project`, and most take `--json`. `vkit <command> --help`
lists the flags.

## Beta limits

- Check logs are not redacted, so a check that prints a secret writes the secret
  to disk.
- Protected integration does not cover the native verifier adapters.
- Lean solutions are BLOCKED unless you reviewed them. vkit does not verify an
  unreviewed proof.
- Automatic cleanup handles Python and the registered rules only. Logic cleanup
  needs CPython 3.13.14.
- POSIX process groups do not contain a descendant that detaches into another
  session.
- Console settings record proposals. They do not change task admission or check
  execution. `verification/manifest.json` is still the check authority.

## Development

```powershell
python -m pip install -e ".[test]"
```

Heavy suites, the formal tools, and browser acceptance run in GitHub Actions.

- [Cross-platform suite](https://github.com/RoyCoding8/vkit/actions/workflows/ci.yml)
- [Formal verifier checks](https://github.com/RoyCoding8/vkit/actions/workflows/formal-verifiers.yml)
- [Installed-wheel browser acceptance](https://github.com/RoyCoding8/vkit/actions/workflows/browser-acceptance.yml)

[docs/verification.md](docs/verification.md) is the reference for outcomes,
tasks, cleanup policy, integration, and the current limits.
[formal/RESULTS.md](formal/RESULTS.md) states the bounds on each committed model
receipt.

## License
Apache 2.0
