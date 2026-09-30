# Release checklist

Run every command in this document yourself and paste the output into the
release record. A step you did not run is a step you did not do, and the
release is described by what ran, never by what the checklist says can run.

The commands here are the ones that exist in this repository at revision
`de96390`. `tests/test_release_docs.py` resolves every command below against
`build_parser()` and the working tree, so a step that stops existing fails the
suite instead of quietly misleading a maintainer. When that test fails, the
document is the wrong thing to edit. Change the code, or delete the step.

These are the files this document relies on. The gate checks each one.

```text files
# Command and entry point sources.
src/vkit/cli.py
src/vkit/mcp/__init__.py
plugin/.mcp.json
plugin/hooks/hooks.json
plugin/scripts/vkit_hook.py
plugin/.claude-plugin/plugin.json

# The harness, the examples, and the acceptance walker.
scripts/acceptance.py
examples/python-cli
examples/node-cli

# The boundary schemas, and the test that loads them from the package.
schemas/manifest.v1.json
schemas/check-artifact.v1.json
schemas/run-report.v1.json
src/vkit/schemas.py
tests/test_claims.py

# The suite's one skip, and this document's own gate.
tests/test_procidentity.py
tests/test_release_docs.py
README.md
```

These paths are named in this document because they do not exist. The gate
fails if one of them appears, which is the moment a gap closes.

```text absent
KIT_ACCEPTANCE.md
```

## Read the verdict first

This build does not pass its own checklist. Four steps are blockers, and
GAP-1 through GAP-4 each describe a way the release can be wrong in front of a
user while every test in this repository is green.

| Verdict | Meaning |
| --- | --- |
| blocker | A step that fails makes the release unsafe. Fix it or do not ship. |
| verified | The command ran on the stated host and produced the stated result. |
| not verified | The command exists and is believed to work, but was not run. |
| not run | The command cannot run on this build. The gap names the reason. |

A maintainer reading a green test suite has verified the code that exists. The
gaps below name the code that does not exist yet.

## The gap register

Each gap carries the evidence that produced it. Delete a gap only when the
work that closes it has run and left a receipt.

| Gap | What is unverified | Evidence for the claim |
| --- | --- | --- |
| GAP-1 | The `mcp` SDK stdio adapter has never been executed. | `import mcp` fails on this host. The adapter is `serve_stdio` in `src/vkit/mcp/__init__.py`. |
| GAP-2 | The POSIX process path is untested. | Verified on Windows 11 with Python 3.13.14 only. No POSIX host has run this package. |
| GAP-3 | No live Claude Code host session has exercised the plugin. | `claude plugin validate --strict` passed. Install and hook delivery were not exercised. |
| GAP-4 | `vkit mcp serve` does not exist as a CLI subcommand. | `build_parser()` defines `doctor`, `check run`, and `run show` only. |
| GAP-5 | The setup console is incomplete. | Plan 05 is the last build step and is still being built in parallel. |
| GAP-6 | Guided enrollment and integration verification are not built. | No `enroll` or `integration verify` subcommand exists. |
| GAP-7 | The acceptance script proves less than it appears to. | It resolves `vkit` from the environment, which is an editable install. |
| GAP-8 | There is no acceptance table to map. | `KIT_ACCEPTANCE.md` is absent from this repository. |
| GAP-9 | Two documented limits have no check behind them. | A `.cmd` launcher mangles a non-ASCII path. A reverted edit escapes the source digest. |

GAP-4 deserves its own paragraph because it is the one that a user would hit
first. `plugin/.mcp.json` starts the server with `vkit mcp serve --project`,
and that command does not exist. On this build a fresh install of the plugin
starts a host that cannot reach the tool surface, because the executable the
manifest names is not the executable that was built. The six tools themselves
are implemented and covered by tests that drive `Server.call_tool`, the same
entry point an SDK adapter forwards to. What is unexecuted is the transport.

## Build the artifacts

Every command below was run on Windows 11 with Python 3.13.14 at revision
`de96390`.

```console command
$ uv venv --seed <venv>
$ <venv>/Scripts/python.exe -m pip install .
$ <venv>/Scripts/python.exe -m pytest tests/
$ <venv>/Scripts/python.exe scripts/acceptance.py
```

The wheel builds from the same revision.

```console command
$ <venv>/Scripts/python.exe -m pip wheel . --no-deps -w <dist>
```

On this host the wheel build printed
`Created wheel for vkit: filename=vkit-0.1.0-py3-none-any.whl size=77992`.
The build depends on `hatchling`, which is not installed in the shared
virtualenv, and `pip` fetches it into an isolated build environment. A release
host that blocks network access cannot build the wheel with this command.

## Verify the test suite

```console command
$ <venv>/Scripts/python.exe -m pytest tests/
```

Observed on this host: `244 passed, 1 skipped in 111.68s`. The skip is
`tests/test_procidentity.py:440`, which the Windows host cannot exercise.

That count is evidence about the code in this repository, and only that. It is
not evidence about the gaps below, because the tests that would cover them are
either stubbed, skipped, or not written.

## Verify the acceptance script

```console command
$ <venv>/Scripts/python.exe scripts/acceptance.py
```

Observed on this host: `22/22 acceptance rows pass`.

Read GAP-7 before trusting this number. The script resolves the command under
test from `sys.executable`'s directory, so it exercises whichever `vkit` the
virtualenv has on its path. In a worktree that is an editable install pointing
at another checkout, and the rows can pass against code this revision does not
contain. A release verification must run this script in a virtualenv that
holds the built wheel, not a developer's editable install.

## Verify the CLI surface

This proves which commands exist. It is the check that catches GAP-4.

```console command
$ vkit --help
$ vkit doctor --project <repo>
```

`vkit doctor` exits 3 on a repository with no manifest, which is the correct
answer for a repository that has not enrolled a check. The exit code table is
in `README.md`.

## Verify the plugin manifest

```console command
$ claude plugin validate --strict plugin
```

Observed on this host: `Validation passed` for
`plugin/.claude-plugin/plugin.json`.

This proves the manifest parses and its fields are well formed. It does not
prove the plugin runs. It does not start the MCP server named in
`plugin/.mcp.json`, and it does not deliver a single hook. Those are GAP-1,
GAP-3, and GAP-4.

## Verify the hook adapter

The hook script is a plain Python entry point and can be run directly.

```console command
$ <venv>/Scripts/python.exe plugin/scripts/vkit_hook.py SessionStart --project <repo>
```

Observed on this host: a JSON object on stdout carrying `additionalContext`
and the three skill names, exit code 0. This proves the adapter starts, reads
its arguments, and emits the documented shape.

It does not prove the host ever runs it. Claude Code's hook delivery is GAP-3,
and the script is invoked here with a synthesized payload rather than a real
host payload.

## Verify the schemas

```console command
$ <venv>/Scripts/python.exe -m pytest tests/test_claims.py
```

The three authoritative schemas are `schemas/manifest.v1.json`,
`schemas/check-artifact.v1.json`, and `schemas/run-report.v1.json`. They are
force-included into the wheel as `vkit/_schemas`, so validation works from an
installed package with no source tree present. The suite loads them through
`src/vkit/schemas.py`, which is what proves the packaging works rather than the
file existing.

## What the release may and may not say

| Claim | Verdict | Why |
| --- | --- | --- |
| Runs a registered check and records a durable outcome | verified | `scripts/acceptance.py`, 22 of 22 rows. |
| Installs from a wheel and validates its schemas outside the source tree | verified | Wheel built and installed from this revision. |
| Exposes six MCP tools to an agent | not verified | The tools are tested. The stdio transport is unexecuted, GAP-1. |
| Installs into Claude Code as a plugin | not verified | The manifest validates. No live host session ran it, GAP-3. |
| Supplies the plugin's MCP server | blocked | `vkit mcp serve` is not a subcommand, GAP-4. |
| Works on macOS or Linux | blocked | The POSIX path has never run, GAP-2. |
| Enrolls a repository through a user flow | blocked | No `enroll` subcommand and no finished console, GAP-5 and GAP-6. |
| Is ready for a pilot | blocked | See [PILOT.md](PILOT.md). |

## Release gate

Do not release while any row above reads blocked. When the work that closes a
gap lands, run the command that closed it, record the output, and update this
document in the same commit. A gap that closes without a receipt is a gap that
moved.
