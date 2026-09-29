# Plan 04: package the Claude Code integration

Read [CONTRACT.md](CONTRACT.md). Ready after Plan 03 passes through a real MCP client.

## Outcome

A fresh Claude Code session discovers the app's MCP tools and workflow skills. A managed engineering task receives the correct contract. Fast hooks identify missing evidence at completion without blocking unrelated conversations. The selected model remains a host choice.

Use [Claude's plugin reference](https://code.claude.com/docs/en/plugins-reference), [MCP integration](https://code.claude.com/docs/en/mcp), [hook reference](https://code.claude.com/docs/en/hooks), and [subagent reference](https://code.claude.com/docs/en/sub-agents). Check the installed host version and test the syntax actually supported by it.

## Plugin contents

Create `plugin/.claude-plugin/plugin.json`, the MCP server configuration, plugin-level hook configuration, and only the skills needed for these flows:

- `/vkit:work`: inspect the project, open the supplied task contract, implement with relevant pstack guidance, run checks, inspect failures, and finalize through the app.
- `/vkit:verify`: run a selected registered verification path and report evidence without asserting that the whole task is complete.
- `/vkit:status`: inspect the current managed task and outstanding evidence.
- `/vkit:onboard`: added by Plan 06 after enrollment behavior exists. Do not ship a nonfunctional placeholder now.

Use the app's MCP tools for ordinary operations. A CLI diagnostic path may call the same core when MCP is unavailable, but it must report that degraded integration explicitly. There must not be an alternative acceptance algorithm embedded in a skill.

Pstack is a supported workflow dependency, discovered and checked explicitly. Keep minimal app operating instructions in this plugin. If a required pstack skill is missing, report that limitation rather than claiming its procedure ran. Do not copy the entire pstack suite into the plugin.

## Context and task binding

Fresh subagents need explicit task contracts and relevant skill content/pointers. Do not assume they inherit the parent conversation or invoked skills. A custom worker definition is optional only if it makes tested context delivery more reliable. Use documented model inheritance, not a hard-coded model alias.

Bind a native session/agent correlation to an attempt through the app. Where hook payloads lack sufficient identity, require explicit registration through `task_begin`; do not guess from timestamps, recent files, or the last active task. Never apply one worker's completion gate to a sibling.

Subagent-defined hooks are not the reliable packaging point. Put supported hooks at plugin level. The hook adapter consumes host JSON, validates the event, calls a short core query, and emits that event's documented response.

## Hook behavior

| Event | Required behavior |
| --- | --- |
| Session start | Add concise project/app pointers only where relevant; no installation or expensive tests |
| Subagent start | Supply registered task references when identity is known; do not fabricate a task |
| Supported pre-tool operation | Check registration/declared limits for the specific supported operation; no generic shell parsing policy |
| Stop/subagent stop | For a bound managed task, identify missing evidence and bounded corrective action |
| Task completed, where available | Use the documented blocking response when the managed task lacks acceptable evidence |

Core CLI exit codes are not hook semantics. Explicitly translate each outcome. Ordinary hook failure/timeouts can be nonblocking, so protected integration remains required. Hooks never create a passing report. A truly blocked task can end with its blocker recorded and without an endless continuation loop.

Scope gates to managed product tasks. Read-only chats, unenrolled projects, host-internal agents, and unbound sessions must finish normally. Use host continuation indicators and a bounded feedback policy rather than repeatedly demanding an impossible check.

## Acceptance

1. Validate the plugin package and launch it with the documented local-development installation mechanism in an isolated test profile/project.
2. Start a fresh Claude session, list the MCP tools, and run the Python example through `/vkit:verify`. Record the actual host version and observed tool calls.
3. Exercise the required skills in a fresh subagent only if delegation is explicitly authorized. If not, keep the live-subagent acceptance row incomplete and report it as a release gate. Do not silently simulate this claim.
4. Feed real-shaped payloads through the hook executable and also exercise actual host delivery for relevant events. Test successful response, malformed input, executable crash, timeout, missing task binding, and duplicate events.
5. A managed unfinished task receives the correct feedback. A blocked task can stop without being marked accepted. An unrelated chat and an internal host agent are unaffected.
6. A missing/disabled pstack skill is reported. A loaded skill must be shown to be discoverable in the host, not merely present under a Codex-specific filesystem path.
7. Run from an install path containing spaces. Confirm exact executable paths, plugin root, project root, and persistent state locations.
8. Confirm there is no model alias or provider endpoint in the core/plugin defaults. Testing multiple live models belongs to Plan 09 and requires existing user authorization.

Live model calls are not authorized merely by this handoff. Build and run all offline integration checks first. If a required live-host test needs a call or delegation outside the session's authority, present the exact remaining check and keep support unverified.

## Finish and handoff

Return the actual package layout, installation mechanism, tested hook capability table, required skill mapping, and observed fresh-session behavior. Plan 05 installs this concrete package; it must not invent another representation of the hooks or MCP server.
