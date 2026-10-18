<p align="center">
  <img src="docs/assets/logo.svg" width="80" height="80" alt="vkit">
</p>
<h2 align="center">VKit</h2>

Machine-checked evidence for coding agents, and for the people who review their work.

Use the built-in tools to compare contracts, analyze supported source fragments, and inspect captured
traces and build artifacts. Each result states its scope. Search the catalog through MCP `help`
or the web console's **Computations** page.

For repeatable repository verification, register checks and accept them. vkit records what each one established
about the files as they are now. An agent asks vkit what is known. It might hear that `checkout-flow` passed
for these exact files, that `latency` is stale because `server.js` changed, or that a Lean theorem holds. The
agent cannot supply a command, and it cannot accept a check. The gate is READY only when every check passed
against the current inputs.

This is a 0.2 beta.

## Install

Python 3.11 or later, and [uv](https://docs.astral.sh/uv/).

From a clone of this repository:

```powershell
uv sync --extra mcp
```

The launch wrappers use this environment. To run a `vkit ...` command from this checkout,
prefix it with `uv run --no-sync`, or activate `.venv` before using `vkit` directly.

Each check brings its own tool, such as Node, pytest, Lean or ruff. `vkit doctor` reports which ones are
missing.

## Launch

From this checkout, start the terminal workspace for the repository you want to work on:

```powershell
.\start.bat --project C:\work\my-repo
```

On Linux or macOS:

```sh
./start.sh --project /path/to/my-repo
```

Both wrappers launch `tui.py`. They use this checkout's virtual environment, or `uv` to create it
if absent. Arguments and paths with spaces are supported. Without `--project`, CLI commands use
your saved default repository, then your current directory if none is saved. The selected project
must be a Git repository. The installed command is
`vkit tui --project <repository>`, also available as `uv run --no-sync vkit tui` from this checkout.

The Rich launcher has one Home screen. Type a command shown there and press **Enter**.
Enter **q** to quit.

In **Home**, enter **open** to start the local server and open your browser.
Enter **start** if you only want its URL, or **stop** to shut it down. Use **project**
to type a repository path, or **browse** to select a folder. Both switch the repository
for the current session. Cancelling the dialog keeps the current repository. The saved port is used;
`0` chooses a free port. Pass `--port 8765` to override it. Quitting or changing repositories
stops the server owned by that launcher. Keep the terminal open while using the web console.
Use the web console for checks, run logs, computations, settings, and agent connections.

In the web console, open **Setup & tools > Project settings > Show tools** to search
the tool list and enable or disable tools. Click **Save settings** to persist changes.
Its **Launch defaults** form saves the default repository, web port, and browser preference.
The form shows the exact JSON path. A blank default repository uses the current directory.
The launcher's folder dialog uses Python's Tk support. If Tk or a graphical desktop is unavailable,
type the path instead. Changes to launch defaults apply to future launches.
Explicit `--project`, `--port`, `--browser`, or `--no-browser`
options override saved values for that invocation without changing the JSON.

Launch defaults live outside repositories at `%APPDATA%\vkit\config.json` on Windows, or
`$XDG_CONFIG_HOME/vkit/config.json` on Linux and macOS. If `XDG_CONFIG_HOME` is unset, the path is
`~/.config/vkit/config.json`. The UI shows the exact path. You can edit the same file directly:

```json
{
  "version": 1,
  "project": "D:/work/my-repo",
  "port": 8765,
  "open_browser": true
}
```

Use an absolute repository path, or `null` to use the current directory. The browser preference
applies to `vkit console`; entering **open** in the TUI explicitly opens it.
Set `VKIT_CONFIG` to select another file, such as this checkout's gitignored `settings/config.json`.
Repository tool settings remain in the Git common directory. Neither standard settings file is committed.

Open **Computations** and click the current operation name to search tools by name or description.
Choose a tool, fill in its inputs, and inspect its scoped result.
Install the Python engines with `uv sync --extra mcp --extra engines`. Native engines have
additional setup documented in [built-in computations](docs/computations.md).

To connect an agent, copy the JSON from **Setup & tools > Connect an agent** into its MCP settings.
See [Connect an agent](#connect-an-agent) for discovery and tool visibility.

For registered repository checks, review and accept definitions with `vkit accept`,
then use the web console's **Checks** page to run them and **Runs** to inspect their results.
Definition acceptance is not yet available in the web console. This workflow needs
a `verification/manifest.json`; the [example below](#try-it) supplies one. Computations and tool
discovery do not require that manifest.

## Try it

Run these commands from the vkit checkout after installation:

```powershell
Copy-Item -Recurse examples/node-http C:\work\items
git -C C:\work\items init
git -C C:\work\items add .
git -C C:\work\items commit -m "items service"
uv run --no-sync vkit accept --project C:\work\items
uv run --no-sync vkit check run --project C:\work\items --needed
uv run --no-sync vkit gate --project C:\work\items
```

The acceptance command asks you to review each check. The final command reports READY, REJECTED, or BLOCKED.
Edit `C:\work\items\src\items-server.js`, then run
`uv run --no-sync vkit status --project C:\work\items --path src/items-server.js`.
It reports both checks as `stale` and lists the features that file implements.
Rerun `uv run --no-sync vkit check run --project C:\work\items --needed` to refresh their evidence.

## What a check can be

| Kind | Evidence category | What a PASS establishes |
| --- | --- | --- |
| `scenario` | scenario | A driver you wrote ran these named cases and saw these results |
| `pytest`, `node_test` | scenario | These named tests ran and passed |
| `property` | property | Hypothesis found no counterexample under the pinned settings |
| `static` | static analysis | The analyzer's SARIF report has no finding outside the pinned baseline |
| `lean` | theorem | The kernel accepted proofs of the frozen statements, within the permitted axioms |
| `tlc` | finite model | Every reachable state of one finite TLA+ model satisfies the properties |
| `build` | build artifact | Paired offline wheel builds match byte for byte, and declared clean-install checks pass |

Each result says what it does not establish as well. A scenario PASS says nothing about inputs that were
not run.

A scenario check can report measurements, such as `p95_ms`, and declare budgets on them. Budgets fail a run
that exceeds a limit or regresses past a baseline that a human pinned with `vkit baseline`. A static check
fails only on findings outside its pinned baseline, so existing debt does not block work and new debt does.

## Connect an agent

Copy the MCP JSON from **Setup & tools > Connect an agent**. It uses the console server's Python
interpreter and selected repository, so the harness does not need `vkit` on its PATH.
Put this short pointer in your repository's `AGENTS.md` or `CLAUDE.md`:

> Call vkit's `help` tool to discover currently enabled tools and their input schemas.
> Pass `query` to search for a question you need answered. Use each result within its stated scope.

An empty `help` request lists every enabled tool. A request such as `{"query": "regex alphabet"}`
finds matching tools with their descriptions and complete JSON input schemas. The catalog is generated
from installed contracts, so onboarding instructions need no manually maintained tool inventory.

Use **Setup & tools > Project settings > Show tools** to search and enable or disable tools, then save settings.
The same controls are available from the CLI:

```powershell
vkit tools --project . --json
vkit tools --project . --query "regex alphabet" --json
vkit tools --project . --disable compare_matchsets
vkit tools --project . --enable compare_matchsets
```

Settings live in `<git common directory>/vkit/config.json`, outside tracked source and shared by worktrees.
Disabled tools disappear from MCP discovery and `help`, and calls to them are refused.
Reconnect an agent after changing settings because clients may cache tool lists. `help` always remains
available. Enable/disable controls belong to the operator console and CLI; MCP exposes no such control.
Enabling an engine does not install its runtime. These switches control MCP exposure, not a shell agent's
ability to inspect installed software or edit local configuration.

The computation tools use fixed algorithms. Most do not change the gate; `check_release_artifact`
runs an accepted build check and records its evidence. Install their Python
engines with `uv sync --extra mcp --extra engines`. See [built-in computations](docs/computations.md)
for supported inputs, CLI commands, backend setup, and the deployment boundary.

For Claude Code, `plugin/` holds two skills and two hooks. The session-start hook reports the gate. The stop
hook holds the session while a check the agent can rerun is stale or failing.

## Watch it

```powershell
vkit console --project .
```

This opens a workspace on `127.0.0.1` using your saved port and browser preference. Run registered checks, cancel active runs, and use the
built-in computations from their catalog-generated forms. The sidebar also shows check definitions,
run outcomes and logs, verification methods, feature coverage, and pending proposals.

Setup & tools saves the theme, sidebar state, refresh interval, and run-history limit to
`<git common dir>/vkit/config.json`, usually `.git/vkit/config.json`. Git metadata is excluded from
commits automatically, and settings survive console restarts. Check acceptance stays in the CLI.
Assets ship locally. Pass `--port 0` to pick a free port.

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
| `vkit console` | Browse evidence, run checks and computations, and save console settings |
| `vkit tui` | Select a repository and start, open, or stop its web console |
| `vkit mcp serve` | Serve MCP over stdio |
| `vkit tools [--query TEXT]` | Discover enabled tools, or change availability with `--enable` / `--disable` |
| `vkit compute --help` | List installed computations; each command has generated input help |

Every command takes `--project`, defaulting to your saved repository or `.`.
Noninteractive commands also take `--json`.

## Limits

- Acceptance binds MCP clients. An agent with a shell can run `vkit accept` itself.
- Evidence is keyed by the content of a check's declared inputs. A check that reads an undeclared file can be
  reported fresh after that file changes. Leave `inputs` empty to key on the whole tree.
- Check logs are not redacted.
- On Windows, agent-written Lean proofs build without a sandbox. See [examples/lean-proof](examples/lean-proof/README.md).
- TLC checks have no automated coverage in this release.

[docs/verification.md](docs/verification.md) is the reference.

## Development

The [repository architecture](docs/architecture.md) maps code ownership and execution paths.

To add a verifier operation, follow [Add a built-in operation](docs/adding-operations.md).
One trusted contract supplies the CLI and MCP wiring.

See the [engine integration status](docs/engine-roadmap.md) and the
[ranked tool backlog](docs/tool-backlog.md) for implemented operations and researched additions.

```powershell
uv sync --extra test --extra engines
uv run pytest
uv run python scripts/e2e.py
```

vkit verifies itself. `verification/manifest.json` registers its end-to-end runs, its tests, ruff, and a
no-comments ratchet.

## License
Apache 2.0
