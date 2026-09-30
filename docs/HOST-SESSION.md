# GAP-3: a live Claude Code host session

`docs/INTEGRATION-CI.md` opens by saying the integration example has never run.
This file is the same kind of record for GAP-3, which was that no live Claude
Code host session had installed this plugin, discovered the six MCP tools, or
delivered a hook. Everything below was run, and the outputs are quoted as the
host produced them.

**What this is not.** It is not `claude plugin validate --strict`, which reads
the manifests and reports on them without a host ever loading the plugin. It is
not the MCP protocol suite in `tests/test_mcp_stdio.py`, which proves a foreign
client and this server agree about the wire format. Both of those are about
files and about a socket. This is about a different program loading this
plugin.

## Status: executed on a real host, and it found a bug

Claude Code 2.1.285 on Windows 11, model `stealth/space-bunny-alpha`, driver
`tests/test_plugin_host.py` (16 tests, 57s).

The host loads the plugin, connects the MCP server, attaches all six tools, and
delivers three hook events. It also delivered a tool name the hook's `PreToolUse`
path did not recognise, so that gate was answering `{}` to every vkit call. The
reason and the fix are below.

## Isolation

`CLAUDE_CONFIG_DIR` and `HOME` both pointed at a scratch directory for every run.
`claude` writes its settings, plugin cache, session transcripts and history
under those variables, so a run that did not redirect them would leave vkit
installed on the machine. The developer's own `~/.claude` was read once, to
confirm it was unchanged, and written never:

```
$ diff <(baseline) <(after)  # 60 entries under ~/.claude
REAL CONFIG UNCHANGED (60 entries identical)
```

`test_the_real_user_config_is_never_touched` and
`test_the_scratch_config_is_what_the_host_used` are the standing guards.

## What was run, and what came back

### Install

A marketplace route, because `claude plugin install` refuses a bare path.

```
$ claude plugin marketplace add D:/AI/Poteto's Style/.claude/worktrees/agent-a2f21629e378cf48f
Adding marketplace…✔ Successfully added marketplace: vkit (declared in user settings)

$ claude plugin install vkit@vkit \
    --config project_root=<worktree> --config python=<venv>/Scripts/python.exe
Installing plugin "vkit@vkit"...✔ Successfully installed plugin: vkit@vkit (scope: user)
```

The two `--config` values are not optional. Installing without them is a
supported path and a broken one, and this is worth recording because the failure
is silent:

```
2 userConfig options not yet set — run /plugin configure vkit@vkit in Claude Code
```

With the options unset, a session reports the server as `pending` and attaches
no tools at all:

```
===== session with options unset =====
  mcp_servers -> [{"name": "plugin:vkit:vkit", "status": "pending", "source": "plugin"}]
  tools       -> []
===== session with options set =====
  mcp_servers -> [{"name": "plugin:vkit:vkit", "status": "connected", "source": "plugin"}]
  tools       -> ['mcp__plugin_vkit_vkit__check_start', 'mcp__plugin_vkit_vkit__project_inspect',
                  'mcp__plugin_vkit_vkit__run_cancel', 'mcp__plugin_vkit_vkit__run_get',
                  'mcp__plugin_vkit_vkit__task_begin', 'mcp__plugin_vkit_vkit__task_finalize']
```

### What the host loaded

```
$ claude plugin details vkit@vkit
Verification Kit (vkit) 0.1.0
Component inventory
  Skills (4)  vkit-onboard, vkit-status, vkit-verify, vkit-work
  Agents (0)
  Hooks (6)  SessionStart, SubagentStart, PreToolUse, Stop, SubagentStop, TaskCompleted
  MCP servers (1)  vkit  (tool schemas resolved at runtime; not counted)
  LSP servers (0)
```

### The six tools, in a real session

The `init` event is what the host assembled and handed to the model:

```
INIT tools: ['mcp__plugin_vkit_vkit__check_start', 'mcp__plugin_vkit_vkit__project_inspect',
             'mcp__plugin_vkit_vkit__run_cancel', 'mcp__plugin_vkit_vkit__run_get',
             'mcp__plugin_vkit_vkit__task_begin', 'mcp__plugin_vkit_vkit__task_finalize']
```

The model named the same six from its own tool list, then called one:

```
ASSISTANT TEXT: check_start
project_inspect
run_cancel
run_get
task_begin
task_finalize

TOOL_USE: mcp__plugin_vkit_vkit__project_inspect {}

TOOL_RESULT: [{"type": "text", "text": "{\n  \"project_root\": \"D:\\\\AI\\\\Poteto's
 Style\\\\.claude\\\\worktrees\\\\agent-a2f21629e378cf48f\",\n  \"evidence_root\":
 \"D:\\\\AI\\\\Poteto's Style\\\\.git\\\\verification-kit\",\n  \"execution_available\": false,\n
  \"policy_accepted\": false,\n  \"enrollment\": {\n    \"schema_version\": 1,\n
 \"command\": \"project enroll\",\n    \"state\": \"not_enrolled\",\n ...
```

## The hook: what the host sent, and what came back

The session transcript records what a hook returned, not what it was sent. The
request side is only observable by putting something in front of the entry
point, so a copy of the plugin was installed under a second name with its hook
script replaced by a tee that forwards to the real one. `plugin/` in the
repository was not modified.

### SessionStart

What the host wrote to stdin:

```json
{
  "session_id": "ac2d42f4-bf1d-479e-b964-16c7d1fc9335",
  "transcript_path": "...\\projects\\D--AI-Poteto-s-Style--...-home\\ac2d42f4-bf1d-479e-b964-16c7d1fc9335.jsonl",
  "cwd": "D:\\AI\\Poteto's Style\\.claude\\worktrees\\agent-a2f21629e378cf48f\\tmp\\gap3\\tee\\home",
  "hook_event_name": "SessionStart",
  "source": "startup"
}
```

What the hook returned, and the host's record of the call:

```
=== SessionStart:startup | exit 0 | success
   stdout: {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
   "D:\\AI\\Poteto's Style\\...\\plugin holds this plugin. The vkit MCP server exposes
   check_start, project_inspect, run_cancel, run_get, task_begin, task_finalize; use those
   for setup, runs, evidence, and finalization. A task becomes acceptable only when the app
   computes READY from recorded runs, and READY is local readiness, never merge permission.
   Skills: /vkit:work, /vkit:verify, /vkit:status."}}
```

The host logged the command it ran, with the `${user_config.*}` placeholders
unexpanded, which is how it reports a plugin hook rather than a user one:

```json
"command": "${user_config.python} ${CLAUDE_PLUGIN_ROOT}/scripts/vkit_hook.py SessionStart --project ${user_config.project_root}",
"durationMs": 705
```

### PreToolUse: the delivery that was broken

What the host wrote to stdin:

```json
{
  "session_id": "ac2d42f4-bf1d-479e-b964-16c7d1fc9335",
  "transcript_path": "...\\ac2d42f4-bf1d-479e-b964-16c7d1fc9335.jsonl",
  "cwd": "D:\\AI\\Poteto's Style\\.claude\\worktrees\\agent-a2f21629e378cf48f\\tmp\\gap3\\tee\\home",
  "prompt_id": "b2d7dcb1-6c8d-4727-b418-379ef568e0ea",
  "permission_mode": "bypassPermissions",
  "effort": { "level": "high" },
  "hook_event_name": "PreToolUse",
  "tool_name": "mcp__plugin_vkitee_vkit__project_inspect",
  "tool_input": {},
  "tool_use_id": "43d9c21d-1e86-405e-9b42-8ecbffa40ee0",
  "mcp_server": { "name": "plugin:vkitee:vkit", "source": "plugin" }
}
```

What the hook returned, before the fix:

```
=== PreToolUse:mcp__plugin_vkitee_vkit__project_inspect | exit 0 | success
   stdout: {}
```

`{}` is a well-formed response. It exits 0, the host records it as `success`,
and nothing anywhere reports that the gate did not run.

The cause is in the tool name. The host scopes it to the plugin, and this
plugin's name and its server key are both `vkit`, so the segment between `mcp__`
and the tool is `vkitee_vkit`, not `vkit`. `_MCP_TOOL` captured that whole
segment as the server and compared it to the literal `"vkit"`:

```
'mcp__plugin_vkit_vkit__project_inspect'
    _mcp_tool -> None
    _on_pre_tool_use -> {}
```

After the fix, the same live delivery:

```
=== PreToolUse:mcp__plugin_vkitee_vkit__project_inspect | exit 0 | success
   stdout: {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext":
   "This vkit call is not from a session registered against a managed task. task_begin is
   what registers one; the server binds every other operation to the task it created."}}
```

### Stop

```json
{
  "hook_event_name": "Stop",
  "stop_hook_active": false,
  "last_assistant_message": "I called `project_inspect` and stopped. Result:\n\n- **Not enrolled** ...",
  "background_tasks": [],
  "session_crons": []
}
```

```
=== Stop | exit 0 | success
   stdout: {}
```

`{}` is the correct response here. The session is bound to no managed task, and
a payload that matches no binding gets no gate.

## Two host behaviours worth recording

**PATH has to be in the spelling cmd.exe reads.** `.mcp.json` names a bare
`vkit`, so the host resolves it on PATH, and on Windows the host spawns through
cmd.exe. Git Bash exports PATH as `/d/...`, which cmd.exe does not search. The
server dies with a message that reads like a broken plugin:

```
[ERROR] MCP server "plugin:vkit:vkit" Server stderr: 'vkit' is not recognized as an
internal or external command, operable program or batch file.
[DEBUG] MCP server "plugin:vkit:vkit": Connection failed after 73ms (CONNECTION_CLOSED)
```

Every other surface at that moment reports the plugin loaded correctly:
`plugin details` lists all six hooks, `mcp list` health-checks the server in its
own process and prints Connected, and the SessionStart hook fires. The
`test_the_model_calls_a_vkit_tool_and_the_server_answers` fixture puts the venv
Scripts directory on PATH in Windows form, because that is part of getting a
real server to start.

**`claude mcp list` and a real session can disagree.** With the options unset,
`mcp get plugin:vkit:vkit` printed `Status: Connected` and its debug log recorded
`Successfully connected (transport: stdio) in 2696ms`, while a session's `init`
event reported the same server `failed` and attached no tools. Both are the
host's own output. That is why the tests read the `init` event and not
`mcp list`: a health check is a different question from a session.

## What is still open

The gap record, after this run:

- **Closed.** The host installs the plugin from this repository, loads its four
  skills and six hooks, connects the MCP server, attaches the six tools, and
  delivers SessionStart, PreToolUse and Stop with the documented payload.
  `project_inspect` was called through the host and returned the server's own
  JSON.
- **Fixed as a result.** PreToolUse recognised no tool a real host sent, so that
  gate was a silent no-op. Fixed in `3766431`, pinned by
  `test_the_host_tool_names_address_this_plugin` and
  `test_the_pre_tool_use_context_reaches_a_real_host_delivery`, both of which
  fail on the reverted code with the `{}` response quoted above.
- **Not claimed.** A subagent inheriting tool access. A BLOCKED run blocking a
  real turn. `claude plugin validate --strict` implying any of this. Those need
  their own live sessions and are not covered by any test in
  `tests/test_plugin_host.py`.

## Repeating it

```
pip install -e ".[test]"
python -m pytest tests/test_plugin_host.py
```

Every test skips, rather than fails, when the `claude` executable is absent or
this environment cannot authenticate a model call. A host that will not run
here is a fact to record, not a test to satisfy.
