# MCP tools and settings

VKit's MCP server gives agents access to repository checks, formal jobs, evidence, and proposals. The CLI and MCP server share the same job service and repository settings.

## Connect an MCP client

Install the MCP extra from the VKit checkout:

```sh
uv sync --extra mcp
```

Configure your client to launch a stdio server with an absolute path to the VKit checkout and the repository to check:

```json
{
  "mcpServers": {
    "vkit": {
      "command": "uv",
      "args": [
        "run", "--directory", "/absolute/path/to/vkit",
        "vkit", "mcp", "serve",
        "--project", "/absolute/path/to/your-repository"
      ]
    }
  }
}
```

This is the `mcpServers` configuration format used by clients such as Claude Desktop. Use your client's equivalent stdio-server settings if its format differs. On Windows, use paths such as `D:/projects/vkit` or escape backslashes in JSON.

The MCP client starts the server process. Running `vkit mcp serve` in a terminal alone does not connect it to an agent.

## Available operations

| Operation | Tool |
| --- | --- |
| Discover enabled tools and their input contracts | `help` |
| Call a discovered tool in compact mode | `invoke` |
| Inspect evidence freshness and the gate | `status` |
| Inspect application behavior and coverage gaps | `features` |
| Submit registered checks | `check_run` |
| Verify an accepted package claim | `verify` |
| Compare explicit finite transition models | `model_compare` |
| Analyze a change under a supported certification profile | `change_certify` |
| Inspect, wait for, or cancel an attempt | `run_get`, `run_wait`, `run_cancel` |
| Export or recheck evidence | `evidence_export`, `evidence_replay` |
| Propose a change to check definitions | `propose` |
| Require current passing evidence | `gate` |
| Record or list verification capability requests | `feedback_add`, `feedback_list` |

Check definitions come from the repository's `verification/manifest.json`. MCP tools do not accept arbitrary shell commands. Review and accept definitions through `vkit accept` before running them. Acceptance records trust in those definitions; an agent with shell access can also invoke the CLI, so acceptance is not a host security boundary.

## Exposure modes

| Mode | Advertised tools | Invocation |
| --- | --- | --- |
| Compact | `help` and `invoke` | Call a domain tool through `invoke`. Direct domain-name calls are refused. |
| Named | Enabled tools individually | Call the tool directly. |

Both modes use the same handlers and availability rules.

```sh
vkit mcp serve --exposure named
```

## Discover and submit in compact mode

1. Call `help(all=true)` for paged tool summaries.
2. Call `help(name="check_run")` for its exact input contract.
3. Submit the check through `invoke`.
4. Inspect, wait, or cancel using the returned run ID.

```text
invoke(name="check_run", arguments={"check_ids":["proof"],"wait_seconds":0})
invoke(name="verify", arguments={"schema_version":2,"claim":{"kind":"package","component":"allocation","policy_sha256":"<64 lowercase hex characters>","package_sha256":"<64 lowercase hex characters>"},"candidate":{"kind":"git_commit","oid":"<full commit object ID>"}})
```

`verify` accepts only a package claim with exact policy and package SHA-256 identities. The claim must match the current accepted manifest component before any job is submitted. The package digest is listed in `verification/manifest.json`; the policy digest is the canonical digest of the complete component policy. The CLI shorthand `vkit verify COMPONENT --working-tree` or `--candidate REVISION` resolves both pins from the current manifest. A request file can be supplied with `vkit verify --request FILE`; it uses the same version 2 shape and cannot supply checker or target authority.

## Discovery contract

| Field or rule | Value |
| --- | --- |
| Help contract version | `2`. |
| Individual tool contract version | `1`. |
| `offset` | Nonnegative. |
| `limit` | `1` to `25`, default `10`. |
| Pagination | Follow `next_offset` until it is null. |
| Exact-name lookup | Cannot combine selectors. |
| Unknown or unavailable name | Same refusal. |
| Invocation arguments | Validated against the published schema. |
| Invocation targets | Cannot target `help` or `invoke` itself. |

## Availability rules

| Control | Effect |
| --- | --- |
| Category | The sole category is `repository`, labelled Repository checks and evidence. |
| Operator settings | Can disable individual tools or the entire category. |
| Startup scope | Can narrow available categories. Discovery cannot widen it. |
| `help` | Remains available. |
| Authorization | Discovery does not grant it. |
| Accepted verifier definitions | Unchanged by tool visibility settings. Checker pins also remain unchanged. |

## Configure the console

| Setting | Location or behavior |
| --- | --- |
| UI | **Setup & tools > Project settings > Show tools**. |
| Controls | Availability, MCP exposure, and startup category scope. |
| Settings file | `<git common directory>/vkit/config.json`. |
| Exposure or startup-scope changes | Reconnect the MCP client. |
| Disabled calls | Refused immediately. |

## Configure the CLI

| Command or option | Effect |
| --- | --- |
| `vkit tools --disable NAME` | Disable one tool. |
| `vkit tools --enable NAME` | Enable one tool, subject to category and scope rules. |
| `vkit tools --category repository` | Select the repository category for the tool controls. |
| `vkit mcp serve --categories repository` | Narrow startup scope to the repository category. |
| Empty `--categories` | Expose no domain tools. |
| `--categories all` | Use every enabled category. |
