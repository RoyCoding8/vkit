# Plan 03: let agents call the application through MCP

Read [CONTRACT.md](CONTRACT.md). Ready after Plan 02. Use the implemented operations directly.

## Outcome

An MCP client can inspect a project, begin a task, start checks, retrieve evidence, cancel a run, and compute readiness. CLI and MCP return the same authoritative results. Agents no longer need to reconstruct the verification lifecycle through ad hoc shell commands.

Use the [official Python SDK](https://github.com/modelcontextprotocol/python-sdk). Pin a supported release and verify its APIs against that version. Implement local stdio first. The [transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) reserves stdout for protocol messages. Logs go to stderr.

## Tool contract

Launch with `vkit mcp serve --project <root>`. Resolve and bind the root at startup. An unenrolled root supports read-only inspection and enrollment guidance; execution and task mutation return an explicit prerequisite failure. Unsupported repository types remain inspectable but cannot run checks. Tests may use explicitly enrolled fixture repositories from Plans 01-02.

| Tool | Inputs | Result |
| --- | --- | --- |
| `project_inspect` | Optional feature/check filter and page limit | Capabilities, selected features/checks, environment gaps, versions |
| `task_begin` | Validated contract reference, checkout reference, owner correlation, request ID | Task ID, attempt generation, claims or explicit admission conflict |
| `check_start` | Task ID, registered check IDs, request ID | Durable run ID and current state |
| `run_get` | Run ID, optional bounded log cursor | Lifecycle, outcome, concise observations, artifact references, next cursor |
| `run_cancel` | Run ID, owner attempt, request ID | Cancellation status and remaining recovery needs |
| `task_finalize` | Task ID and attempt generation | Computed READY/REJECTED/BLOCKED with required evidence and gaps |

Freeze JSON input and output schemas in tests. Default summaries should fit a short tool response; never dump entire raw logs or feature maps by default. Choose and document byte/page limits, including UTF-8 handling. Provide a supported way to read referenced artifacts without arbitrary filesystem access, either bounded `run_get` sections or MCP resources backed by known artifact IDs.

The tool list should stay small. Do not expose a generic shell executor, arbitrary SQL, raw file writes, package installer, or model API. Existing agent tools still handle editing, reading source, and debugging.

## Implement

1. Add the SDK adapter and real lifecycle handshake. Advertise only implemented capabilities. Use accurate read-only/idempotency/destructive annotations where the negotiated SDK/protocol supports them; these are hints, not access control.
2. Convert validated requests into core calls. Keep acceptance decisions, ownership, path validation, and process management in the core. Do not duplicate them in decorators or MCP handlers.
3. Bind IDs to the configured project. A linked worktree may participate only when it resolves to the same Git common directory and a valid attempt. Reject unknown, cross-project, and superseded IDs.
4. Return domain failures as structured tool outcomes. Distinguish protocol/argument errors from BLOCKED checks. A disconnected client must not fabricate completion or lose the run record.
5. A start call returns promptly after durable registration/launch handling. Polling `run_get` observes the durable supervisor. Cancellation of an MCP request is distinct from `run_cancel`; document whether any pre-launch operation is cancelled and ensure retries remain safe.
6. Respect the host's context limits with bounded responses and useful pointers. Do not create a summarization model call.

## Acceptance

Use an actual SDK client connected to the installed server process over stdio. Calling decorated Python functions directly is insufficient.

- Initialize, negotiate, list tools, and invoke all six tools with valid and invalid inputs.
- Complete the Python example through MCP and inspect the same run through the CLI. Outcome, identities, and artifacts must match.
- Introduce a real defect and get REJECTED at finalization; omit required evidence and get BLOCKED.
- Disconnect after starting a slow run, reconnect, and retrieve its actual terminal result.
- Retry a start with the same request ID and prove one execution. Reject reuse with a different payload.
- Cancel a run with descendants through MCP and confirm the core's cancellation behavior.
- Request another project's run/artifact and reject it. Test path traversal in any reference input.
- Produce large logs and prove response bounds/cursors preserve access without overflowing one tool response.
- Parse every stdout message as MCP protocol traffic during success and failure. A startup banner must not corrupt it.

Add a host configuration example with exact executable/argument handling. Do not modify the user's live Claude settings in this milestone. Plan 04 tests the real host integration.

## Finish and handoff

Return the tested SDK version, schemas, protocol-level test results, and sample bounded responses. Plan 04 consumes the server command and tool descriptions. Note that a passing SDK-client test establishes protocol behavior; live Claude Code discovery and subagent access are separate checks.
