# Discover and control tools

## Available operations

Repository jobs and evidence operations share one tool set. Adding an external backend adds a manifest entry, not a new transport tool.

| Operation | Tool |
| --- | --- |
| Discover enabled tools | `help` |
| Submit registered checks | `check_run` |
| Verify an accepted package claim | `verify` |
| Inspect, wait, or cancel an attempt | `run_get`, `run_wait`, `run_cancel` |
| Export or recheck evidence | `evidence_export`, `evidence_replay` |

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

Finite intent requests are not accepted by the public `verify` tool. The finite intent preflight remains a library operation and does not verify candidate source behavior or create verification evidence.

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
