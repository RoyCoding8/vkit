# Plan 05: ship the user-facing setup application

Read [CONTRACT.md](CONTRACT.md). Ready after Plan 04 provides a tested plugin package and installation mechanism.

## Outcome

A user runs `vkit setup`, sees what is installed and missing, selects the desired integration and scope, and gets a working app connection without hand-editing several unrelated configuration files. The app offers skills and hooks explicitly and can later repair or remove its own integration.

Default presentation: terminal wizard using ordinary prompts and clear text. No TUI framework is required. A graphical-first user preference would change the presentation work in this plan; the core operations and acceptance criteria remain the same. Do not build two independent setup engines.

## User flow

1. Explain that vkit supplies verification tools and uses the existing Claude Code model/session.
2. Detect the installed app version, supported Claude version, plugin registration, current project, and required skill availability. Inspect only relevant installation/configuration paths; avoid reading unrelated conversations or secrets.
3. Offer project-only or user-level installation using the host's supported scopes. Explain which files/settings will change. Keep this choice distinct from repository enrollment.
4. Show components: app MCP connection, vkit skills, lifecycle hooks, and optional installation/connection of required pstack skills. Detect compatible existing components and offer reuse.
5. Present the resolved change set, source/version of downloaded components, and any conflict. Ask once to apply the selected changes. Selection must not silently enable unrestricted shell tools, new model spending, or future agent delegation.
6. Apply through official host installation commands where possible. Preserve unrelated configuration. Run a local connection/doctor test and show what works plus any remaining host restart or user action.

The app should ask to install skills, as requested. It must distinguish app-provided skills from pstack. Reusing an existing installation should not create duplicate names or stale copies. Use pinned provenance and retained license notices for any approved vendored material.

## Commands and mechanics

```text
vkit setup
vkit integration status --json
vkit integration repair
vkit integration remove
```

Provide a noninteractive mode that accepts a saved explicit selection/change set for repeatable testing and managed installation. Noninteractive invocation without required choices returns instructions; it does not assume consent.

Keep installation receipts separate from repository runtime state. The receipt records installed component IDs, versions, scope, and exact owned changes, not a mirror of all user settings. Use official uninstall mechanisms where available. For files the app must edit, validate the format, write atomically, and preserve recoverable prior values.

Repair and uninstall must compare current values with the recorded owned values. If a user changed an entry, preserve it and report the conflict; do not restore a whole old settings file over newer work. Do not delete a user's preexisting pstack installation just because vkit used it.

Handle interruption between installation steps by inspecting actual host state on the next invocation. Idempotent reapplication should converge on the selected setup. A small installation receipt is sufficient; no background repair watcher or generic deployment engine.

Hook execution and MCP startup must never run a package installation or network update. Install the pinned executable during setup. Test invocation through paths with spaces and without relying on the initiating shell's PATH.

## Acceptance

Use an isolated home/profile and fixture host configuration for destructive installation scenarios. Do not experiment on the user's real configuration.

| Scenario | Expected outcome |
| --- | --- |
| Fresh install | Selected components installed and doctor identifies the actual versions |
| Compatible pstack already present | Reused without duplicate skill copies |
| Missing skills | Clear offer and explicit selection before installation |
| User declines | No configuration mutation or download |
| Unrelated MCP servers/hooks/settings exist | Preserved byte-for-byte where untouched, semantically preserved where structured editing is necessary |
| Interrupted install then rerun | Correctly resumes/reconciles without duplicate entries |
| Network unavailable | Reports failed dependency with recoverable state; no false success |
| Host configuration changed concurrently | Detects conflict before overwriting |
| Upgrade while tasks are active | Existing tasks retain compatible pinned code/contracts, or upgrade is deferred with a specific reason |
| Repair | Fixes owned broken registration without resetting unrelated preferences |
| Uninstall | Removes owned integration while preserving product source, evidence, and external skills |
| Noninteractive explicit selection | Same resulting setup as wizard; no hidden prompt/hang |
| New shell after installation | App and plugin executable paths still resolve |

The wizard must use the working MCP/plugin integration from Plans 03-04. A page that merely prints generic setup instructions does not complete this plan. Unsupported host versions may receive precise manual instructions, but must be labeled unsupported rather than installed successfully.

## Finish and handoff

Return a short recorded setup transcript, exact configuration diffs, install/repair/remove counterchecks, supported scope/version matrix, and recovery behavior. This milestone makes the app installable by a person. Plan 06 supplies repository enrollment, and Plan 09 validates the complete fresh-user path.
