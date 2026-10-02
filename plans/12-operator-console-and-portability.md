# Plan 12: operator dashboard and portable integration

Status: ready for implementation. Checkpoint 12.1 can start before Plan 10.
Read [the proposed contract extension](NEXT-CONTRACT.md) and [the worker workflow](../MASTER-PLAN.md#skills-and-parallel-work). Record checkpoint evidence in the master plan.

## Give the operator direct control

Provide a local web dashboard where a person can understand the project, run checks, inspect evidence, change approved configuration, and connect an agent harness. The app must work without asking an agent to explain or configure it.

Reuse the current `src/vkit/console/` HTTP server, API admission gate, operations, HTML, JavaScript, and CSS. Current views include Project, Readiness, Checks, Runs, Recovery, and Operations. Existing mutations cover enrollment, plugin install/repair/remove, running checks, and cancellation. Recovery is read-only, and policy editing is currently prohibited.

The gaps are a public launcher, clear task/evidence views, a real configuration flow, and portable connection instructions. Keep the current Python backend and plain browser assets. Do not replace them with a new web framework, Electron app, or permanent service.

## Checkpoint 12.1: launch the existing app

Add this public command:

```text
vkit console --project ROOT
```

Bind to `127.0.0.1`, print the actual URL, and open the default browser after successful binding. Support `--no-browser` and `--port`. Give a readable error for an occupied port. Reuse `console.operations.open_context` and `console.server.serve`, then run `serve_forever` with clean shutdown.

Ship the static assets in the installed wheel and exercise the command outside the source checkout. Keep console session tokens out of logs and URLs. Preserve existing host, origin, method, token, and body-size guards.

Rewrite the first page around operator questions: which project is open, what is available, what needs setup, and what action is available next? Distinguish installation health from task readiness. The current Readiness view reports doctor facts, which does not mean a task has passed verification.

Deliver this small checkpoint independently. It makes existing functionality accessible while the verification engine is built.

## Checkpoint 12.2: show the actual engineering state

Prototype the Home and configuration flow using the existing page before expanding its implementation. Use a real project with checks and run records. Settle the layout from the operator's main actions.

Use these sections:

- Overview: project root, enrollment, integration health, active work, and concrete blockers.
- Checks and evidence: registered check, category, claim, scope, prerequisites, Run action, and latest evidence.
- Tasks and runs: task generation, required claims, held resources, verdict, missing evidence, cancellation, and bounded logs.
- Cleanup: mode, rules, protected paths, proposals, applied patches, preservation receipts, and refusal reasons.
- Settings and integrations: editable project configuration, component versions, connection status, and setup actions.

Use Plan 10's typed evidence and Plan 11's cleanup receipts when available. Hide unavailable actions or explain their missing prerequisite. Do not invent capability from a planned schema or installed dependency.

Readiness displays the existing core's decision. Provide an explicit Refresh action when a user wants a new decision. Do not mutate task state just by viewing a page. Run actions create or use a clearly identified operator task through the existing admission path. Avoid an untracked console-only verification lifecycle.

Retain bounded refresh and pagination. Render project text as text, rather than HTML. Support keyboard navigation, labels, readable contrast, and narrow windows. No full-repository JSON dumps as the normal experience.

## Checkpoint 12.3: edit configuration without weakening evidence

Start with typed controls for registered check timeouts, required obligations, verifier/toolchain selections, cleanup mode/rules/exclusions, and harness connection scope. Show advanced argv as an argument list with validation. Keep an advanced JSON editor for supported project fields, with the same validation as the forms.

Separate a saved proposal from approval and activation. Show current values, changed values, affected checks, and the evidence that will become stale. Explicitly show when a change removes an obligation or weakens a validation profile.

Reuse enrollment and approved-policy mechanisms. The current console prohibits writes under `verification/` and `schemas/`. Replace that blanket policy prohibition with a specific validated project-policy operation. Keep arbitrary paths and writes to vkit's schemas prohibited. Do not disable `under_protected_path` for every endpoint.

Save only fixed project configuration paths. Validate the whole proposed document and cross references before writing. Use the expected current digest to reject conflicting browser tabs. Preserve the previous document for recovery and publish each changed document atomically. Avoid splitting one policy decision across partially updated files.

A project configuration change produces a candidate policy revision and records its operator action. It does not approve that revision for protected integration. Protected CI continues to use its independently approved policy ref. Show both the local configuration and the approved integration policy, including any mismatch.

Existing tasks retain their pinned contracts. Changing configuration cannot retroactively release obligations or make old evidence current. Offer a new task attempt under the newly accepted local policy through the normal core operation. Preserve failures and previous evidence.

Do not put model credentials in project policy. The user's chosen harness owns its model and provider settings. No unrestricted command textbox or fake enable switch for missing tools.

## Checkpoint 12.4: make MCP the portable agent connection

The existing Claude plugin already starts vkit's stdio MCP server. Keep it as an optional installer for that harness. Standalone MCP must provide the same operations without requiring a Claude plugin or pstack skills.

Add a connection panel that generates host-neutral configuration from the installed executable's actual absolute path:

```json
{
  "mcpServers": {
    "vkit": {
      "command": "ABSOLUTE_PATH_TO_INSTALLED_VKIT",
      "args": ["mcp", "serve", "--project", "ABSOLUTE_PROJECT_ROOT"]
    }
  }
}
```

This is an illustrative configuration shape. Provide copy/export and identify hosts that require a different wrapper. Do not label a generated snippet as a verified connection. Probe the real MCP transport and report configured versus connected separately.

Use standard [MCP discovery and calls](https://modelcontextprotocol.io/specification/2025-11-25/server/tools). Enrich `project_inspect` with backend capabilities, usable check IDs, prerequisite gaps, and cleanup support. The current `vkit mcp serve --project ROOT --json` catalogue is also useful for CLI discovery. Verify its actual output and keep the catalogue consistent with MCP tool definitions.

Keep MCP process binding to one root. Do not let a tool call switch the server to an arbitrary project. Keep backend modules independent of host payloads. Map session and agent identity at host-specific hook adapters.

A standalone MCP host can invoke verification and finalization explicitly. Automatic edit cleanup and Stop blocking require that host's lifecycle support. The integration panel must show those capability gaps. A host without hooks does not inherit Claude's automatic completion gate.

Retain the current four skills initially. Their distinct roles are onboarding guidance, the engineering work sequence, running verification, and explaining status. Make tool descriptions sufficient for ordinary discovery. Keep skills short and remove duplicated rules already enforced by the core. Offer them as workflow help rather than a prerequisite for correctness.

Pstack skills remain optional engineering methods. Do not install all of pstack or load every skill on each task. Show available versus installed versus enabled integrations accurately. Installing more guidance must not grant execution or integration approval.

Do not build another harness adapter until a specific target host is selected. The portable delivery for this plan is an installed CLI, a working standalone MCP server, and an honest capability checklist.

## Acceptance exercises

| Exercise | Required result |
| --- | --- |
| Installed `vkit console` outside the checkout | Browser serves real project data and packaged assets |
| Operator runs a check and reads its evidence | Same run and core verdict visible through CLI/MCP |
| Required tool is absent | Useful setup explanation, no misleading green status |
| Invalid settings or stale expected digest | No write, readable errors, old configuration remains usable |
| Operator changes an obligation or cleanup mode | Preview identifies effects and old evidence cannot be reused improperly |
| Candidate policy differs from protected approved policy | Difference shown, protected approval remains intact |
| Cross-origin request, missing token, GET mutation, or path escape | Existing boundary refuses it |
| Candidate log or description contains HTML | Displayed safely as text |
| MCP client connects without a Claude plugin or any skills | Discovers tools, inspects project, runs a task, and reads finalization |
| Host lacks lifecycle hooks | UI shows the missing automation and explicit verification still works |

Use small Python HTTP checks locally. Run the full suite in GitHub CI. Add one browser acceptance flow in CI for navigation, configuration preview/save, running a check, and reading evidence. Use Python-driven browser tooling if needed. Do not install a heavyweight local browser test stack or use WSL.

## Handback

Provide each checkpoint's commit, actual launch command, installed-package receipt, configuration conflict examples, transport exercise, CI tested SHA, and operator-flow evidence. Update user documentation to explain the app before agent integration. Live Claude or another harness behavior requires an actual session receipt, beyond a simulated payload.
