# Interface audit: Plans 03–06 and 09

Audited the current workspace against `plans/CONTRACT.md` and plans 03–06/09. Findings below come from code inspection and isolated scratch probes. I did not edit product files, invoke Claude, or use a paid session. Existing worker changes were preserved.

## Findings

### P1 — The unauthenticated loopback console accepts cross-origin GET mutations

- **Evidence:** `src/vkit/console/server.py:81-86` dispatches any `/api/...` GET. It does not validate `Host`, `Origin`, or `Sec-Fetch-Site`. The writable `run_check` route is reachable this way (`src/vkit/console/api.py:32-55`; operation implementation at `src/vkit/console/operations.py:355-380`). Binding only to `127.0.0.1` does not prevent a browser page from sending a request to loopback.
- **Repro:** Parent's isolated probe in `review/probe_worker.py` and `review/probe-results.json` sent a GET with a foreign Host/Origin and observed the registered operation execute. No Claude or non-loopback listener was used.
- **Contract:** Plan 05 says the no-auth console is safe because it cannot be reached from another machine (`plans/05-setup-app.md:68-79`). A hostile web origin can reach the loopback listener from the user's browser, so that assumption is incomplete.
- **Limit:** This establishes request-triggered execution, not response exfiltration or execution of an unregistered command. Checks are still selected by manifest ID.

### P1 — The built wheel cannot install the plugin it advertises

- **Evidence:** `pyproject.toml:45-49` packages only `src/vkit` and force-includes schemas. `src/vkit/console/operations.py:499-522` searches for a source-tree `plugin/` or `vkit/console/_plugin`, but the built wheel contains no plugin files.
- **Repro:** Parent's isolated wheel probe, `review/wheel-inspection.json`, records a 45-file wheel with `plugin_files: []`; importing `_plugin_source_dir()` from the extracted wheel raises `Refused`.
- **Contract:** Plan 09 requires a versioned plugin package from the same release and installation independent of a developer source directory (`plans/09-release-and-pilot.md:9-13`).
- **Limit:** This is verified for the wheel inspected in `review/wheel-inspection.json`; no registry or host install was attempted.

### P1 — Console enrollment writes a record the core always reads as NOT_ENROLLED

- **Evidence:** `src/vkit/console/operations.py:783-795` writes `root`, `configuration_digest`, `accepted`, and `policy`. `src/vkit/enroll.py:174-196` requires `state` and returns `NOT_ENROLLED` on the missing key.
- **Repro:** Parent's isolated enrollment probe, `review/enrollment-result.json`, shows the console returning `enrolled: true, accepted: true` while a subsequent core read returns `NOT_ENROLLED`.
- **Contract:** Plan 06 requires enrollment to enable execution after explicit acceptance and requires the fresh-session flow to find the enrolled contract (`plans/06-repository-onboarding.md:19, 36-43`).
- **Limit:** The probe verifies the receipt/read mismatch. The final behavior of each caller depends on whether it checks enrollment; MCP `project_inspect` does call `read_enrollment` at `src/vkit/mcp/_tools.py:333-336`.

### P1 — Finalization does not use a stable approved required-check floor

- **Evidence:** MCP finalization forms its floor from task `required_checks`, then adds every check in the current manifest if one parses (`src/vkit/mcp/_tools.py:870-887`). If the manifest is absent, it uses only the task contract. CLI finalization uses the task contract's `required_checks` plus additions (`src/vkit/cli.py:574-600`). Neither path loads a pinned approved policy baseline for task acceptance.
- **Hook path:** Completion also takes only `task.contract["required_checks"]` and passes that list to `compute_readiness` (`plugin/scripts/vkit_hook.py:337-347`). The persisted probe in `review/probe-interface-results.json` shows a two-check manifest, a one-check task contract, a PASS for the selected check, and an empty Stop response despite the second check having no evidence. Treat this as the same core R1 required-floor defect; the fix belongs in the shared accepted-baseline path, not a hook-specific fallback.
- **Repro:** Parent's isolated `review/baseline-results.json` has a repository with two mandatory checks. A CLI task contract containing one check and a run for that check finalizes `READY`; MCP returns `BLOCKED` for the second. Removing the manifest lets MCP return `READY` for the same task. This demonstrates both interface drift and a mutable floor.
- **Contract:** A task may add checks but cannot remove the approved baseline (`plans/CONTRACT.md:49`); acceptance is based on exact source and policy identities (`plans/CONTRACT.md`, State and identity / Outcome sections).
- **Limit:** The repro uses the isolated sample repository and does not claim a live protected-CI integration result.

### P2 — Oversized POST rejection does not stop the requested operation

- **Evidence:** `src/vkit/console/server.py:88-93` always calls `_answer` after `_read_body`. On an oversized body, `_read_body` writes 413 and returns `{}` (`:107-115`), so the route still dispatches from query parameters.
- **Repro:** With `.venv\Scripts\python.exe`, I started the server on an ephemeral loopback port in a scratch process, replaced only `api.dispatch` with a call recorder, and POSTed `MAX_BODY_BYTES + 1` bytes to `/api/run_check?check_id=registered`. The client got 413, while the server logged a second 200 and the recorder showed one `run_check` dispatch. Python then parsed leftover body bytes as a new request and logged 414.
- **Contract:** Plan 05 says the boundary validates requests and that refused operations preserve the operation's behavior (`plans/05-setup-app.md:32-42, 97-101`). A rejection must not also perform the operation.
- **Limit:** The dispatch was intercepted; this probe proves control flow, not a real check execution. The query-driven GET route independently remains callable as covered by the first finding.

### P2 — Hook lookup accepts missing and ambiguous subagent identity

- **Evidence:** `_bound_task_id` matches `session_id` and only checks `agent_id` when the payload supplies one (`plugin/scripts/vkit_hook.py:163-198`). `SubagentStop` calls it without requiring an agent ID (`:366-377`).
- **Repro:** `review/probe_interface.py` creates a scratch Git repository and temporary vkit store, then calls `_bound_task_id` with a `SubagentStop`-shaped payload containing only the shared session ID. The hook returns `parent-task`; after adding a second active task with that same session, it still selects the first row without disambiguation. `review/probe-interface-results.json` is the latest run receipt.
- **Contract:** Plan 04 says never apply one worker's completion gate to a sibling and says to require explicit registration where payloads lack enough identity (`plans/04-claude-plugin.md:24-30, 44`).
- **Limit:** This establishes the adapter's behavior when identity is missing or duplicated. I did not run a Claude host event. Existing host evidence asserts SessionStart and PreToolUse payload fields, not SubagentStop identity (`tests/test_plugin_host.py:681-729`), so whether a supported Claude version emits an incomplete payload remains unverified.

### P2 — Fresh plugin installation does not configure its required project or Python values, and MCP launch depends on PATH

- **Evidence:** The plugin requires `project_root` and declares `python` defaulting to the bare string `python` (`plugin/.claude-plugin/plugin.json:12-23`). Hooks execute that value (`plugin/hooks/hooks.json:3-18` and repeated event blocks), while the MCP server uses bare `vkit` (`plugin/.mcp.json:1-7`). Console install calls `claude plugin install <id> --scope user` without `--config` (`src/vkit/console/operations.py:589-605`) and does not derive either value from its project or interpreter.
- **Repro/evidence:** `docs/HOST-SESSION.md:52-65` records a working host install passing both `--config project_root=...` and `--config python=...`; it says both values are required and an install without them leaves the server pending. No Claude command was run during this audit.
- **Contract:** Plan 05 calls for a reviewed, concrete install change set (`plans/05-setup-app.md:12-15, 65-66`). Plan 09 requires installation not to depend on a developer source path or transient shell PATH (`plans/09-release-and-pilot.md:9-13`). The plugin config currently depends on host-level `python` and `vkit` resolution.
- **Limit:** A user may have configured these values already; this finding concerns the fresh setup path and clean virtual-environment installs.

## Request-size boundary notes

- Oversized POST bodies are rejected with 413 but still dispatch as described above.
- Invalid or missing `Content-Length` is treated as zero (`server.py:107-115`); negative lengths also bypass the positive-size check and read no body. I did not establish a separate exploit impact for those malformed headers.
- GET request lines have the standard library parser's own request-line limit. I did not find an application-level query byte limit; a too-long line is rejected by `BaseHTTPRequestHandler` before route dispatch.

## Audit limits

- No current-tree product edits, configuration changes, actual Claude installation/session, paid call, full suite, or live protected integration run.
- Review artifacts added: `review/agent-interface.md`, `review/probe_interface.py`, and `review/probe-interface-results.json`. Existing worker files and review probes were left intact.
- Parent's wheel, enrollment, baseline, and cross-origin probes are identified as parent-run evidence; my independent runtime probes are the oversized POST and hook lookup checks above.
