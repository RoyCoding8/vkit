# Use the VKit console

The console runs repository checks and displays evidence, coverage, proposals, and tool settings. Start it from the VKit checkout:

```sh
uv run vkit console --project /path/to/your-repository
```

VKit opens the local console in your browser. The packaged interface needs no Node installation or external web services.

## Run checks

Select a workspace, then open **Checks**. Use **Run** beside a check to submit it, or **Run needed checks** to run checks with stale or missing evidence. Open **Runs** for progress, logs, artifacts, and cancellation.

**Refresh** reloads status. It does not run checks.

The console runs checks declared in `verification/manifest.json` and accepted through the CLI. See [Getting started](getting-started.md) for repository configuration. Formal package verification uses `vkit verify` or the MCP `verify` tool, as described in the [verification reference](verification.md).

## Resolve incomplete evidence

Use the check states to choose the next action:

| State | Action |
| --- | --- |
| Not set up | Add a valid `verification/manifest.json`. Start with the [tutorial](getting-started.md) or an [example](../README.md#documentation). |
| Not approved | Review the check definition with `vkit accept --project /path/to/your-repository`. |
| Missing | Run the check. It has no evidence yet. |
| Stale | Run the check again. Its recorded inputs or definition have changed. |
| Failed | Open the run, inspect its observations, fix the cause, and rerun. |
| Blocked | Open the run and resolve the reported prerequisite or execution problem. |
| Fresh pass | The recorded check passed for its current inputs. |

The gate requires current passing evidence for required checks and formal components. A successful refresh or an old passing run does not complete that requirement.

## Understand capabilities and feature coverage

**Capabilities** lists the adapters for registered checks and their check counts in the selected workspace. **No checks registered** means the project has no checks using that adapter. Install any required tool, add a check definition, and accept it before expecting evidence from that adapter.

Formal package verification, change certification, and model comparison have separate workflows. See the [verification reference](verification.md) for those methods.

**Feature map** describes your application's behavior and its coverage. It reads `verification/features.json`; it is not a list of VKit tools. If the map is empty, add feature entries that name the relevant checks. The [Python example](../examples/python-cli/verification/features.json) includes both covered behavior and explicit gaps.

## Switch workspaces

Open the workspace picker at the top of the sidebar. Select a remembered repository, or choose **Add workspace** and enter a repository path.

VKit remembers repositories used through the CLI, MCP server, or console. It records those paths without scanning other directories.

Choose **All workspaces** for a combined overview and pending-proposal inbox. Select a specific workspace before running checks or changing project settings. Separate browser tabs can work with different repositories.

If you move a repository, choose **Add workspace** and enter its new path. The old entry can remain visible as unavailable. Git worktrees appear as separate choices.

The last-seen time records the most recent VKit use of a workspace.

## Review proposals

The proposal inbox shows suggested changes to check definitions. Open a proposal to review it and copy its acceptance command. Acceptance runs through the CLI; the console does not accept proposals.

In **All workspaces**, proposals identify their repository. Open that workspace before acting on a proposal.

## Configure tools

Open **Setup & tools**, then **Project settings > Show tools** to enable or disable tools and change MCP exposure. Reconnect the MCP client after changing exposure or startup category scope.

Tool settings control which VKit operations are available; repository and host permissions still apply. See [MCP tools and settings](tool-discovery.md) for availability rules.

## Settings and storage

| Data | Location |
| --- | --- |
| Project settings | `<git common directory>/vkit/config.json`, usually `.git/vkit/config.json`. |
| Project approvals, proposals, and evidence | VKit storage in the repository's Git common directory. |
| Launch defaults | `%APPDATA%/vkit/config.json` on Windows; `$XDG_CONFIG_HOME/vkit/config.json` on other systems, defaulting to `~/.config/vkit/config.json`. |
| Remembered workspaces | `workspaces.sqlite3` beside the launch defaults file. |

`VKIT_CONFIG` selects a different launch defaults file and relocates the workspace registry beside it. The registry stores workspace paths and use times. Project evidence and approvals stay with the repository.
