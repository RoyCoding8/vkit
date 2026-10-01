# Plan 09: package, validate, and pilot the complete app

Read [CONTRACT.md](CONTRACT.md). Package validation starts after Plans 01-07 pass. A real pilot additionally requires a selected repository and authorized live-model usage. Plan 08 is optional and must not hold the basic release hostage.

## Outcome

A new user can install the app, choose the skills/hooks integration, enroll a repository, and have a fresh Claude Code session use the app's MCP tools. The release states its actual compatibility and verification limits. It has evidence from a useful real task, not only toy examples.

## Package and distribution

Build an installable wheel and a versioned Claude plugin package from one release revision. Record compatibility between the executable, schemas, plugin, and host. Installation must not depend on a developer's source directory or a transient shell PATH.

Use pinned local artifacts for release validation. Check the availability of the working name before publishing. Publishing to a registry, uploading a marketplace package, or changing a public repository is a separate authorized action; prepare the exact release artifacts first.

Include concise user documentation covering install, setup choices, enrollment, agent use, status/evidence, cancellation, recovery, repair, upgrade, and removal. Include license notices and the actual dependency list. Troubleshooting should name concrete symptoms and commands rather than advise repeated reinstalling.

No desktop shell or remote service is necessary for the terminal-first release. If the user selected a graphical first release, complete the revised Plan 05 UI and exercise the same flow through it before marking this milestone done.

## Fresh-user acceptance

Use isolated test profiles for setup/teardown. Test the built artifacts, not an editable install only.

1. Install into a clean environment on the primary Windows target.
2. Run the app, select the plugin/skills/hooks, inspect the proposed changes, and apply once.
3. Start a fresh supported Claude Code session and verify actual MCP tool discovery.
4. Enroll the Python and Node examples through the user flow. Complete known-good, known-defect, and blocked-prerequisite cases.
5. Open a new session and a linked worktree; confirm they read the correct shared state and preserve per-attempt isolation.
6. Run an authorized fresh subagent task with explicit skill/context delivery and inspect actual evidence.
7. Run the same bounded task using two already-configured models if the user authorizes those calls. Record actual model selection and limits. Core behavior should not require provider-specific changes.
8. Interrupt a check/client, recover it, upgrade or defer an upgrade appropriately, and inspect retained evidence.
9. Repair registration and uninstall the integration. Confirm unrelated settings, existing pstack, source, and required evidence remain intact.

If a required environment or live call is unavailable, keep that acceptance row incomplete. The candidate may be described as locally tested with explicit limits, but not as a fully validated release.

## Real pilot

Select one user-approved repository whose normal test/run path is available. The implementation worker should not choose an unrelated personal project or inspect credentials to force a pilot to proceed. The sample applications allow all other preparation to continue before this choice is made.

Ground the initial feature map in actual source and choose a useful bug fix or small feature. Establish baseline behavior, implement through the kit workflow, demonstrate the intended change, and run integration checks. Stop at a reviewable result unless publishing/merging is separately authorized.

Compare existing pstack use and pstack plus the app on a small fixed task set where practical. Use fresh starting states and comparable model/environment settings. Include a bug, feature, refactor, combined-change conflict, and interruption across the comparison set. Record all outcomes, including failures.

Measure accepted outcomes, false passes, failures caught before integration, human interventions, repair attempts, elapsed time, verification time, and usage/cost when actually available. Include setup and maintenance effort. Unknown cost remains unknown. Avoid PR count or number of spawned agents as the success metric.

## Scale claims

Synthetic process contention establishes local coordination behavior. It does not establish engineering quality at 100 AI agents. Report those two forms of evidence separately.

For any authorized live increase, declare host agent limits, registered writer limits, verification capacity, spending limits, and a stop condition before launch. Increase only if accepted throughput improves without unexplained false passes, resource leakage, or growing integration backlog. No automatic jump to 100 or 1,000 agents.

## Release checks and report

Map every row in [KIT_ACCEPTANCE.md](../tmp/research/KIT_ACCEPTANCE.md) to an executed test, a clearly optional capability, or an explicit unsupported boundary. Required failed or unrun rows prevent the associated support claim. The report must identify actual platforms, host versions, SDK versions, models exercised, plugin version, and candidate revision.

Deliver the built packages, checksums, reproducible install commands, compatibility table, pilot evidence, known limits, and remaining release gates. Update STATUS.md. Keep publication pending if it was not authorized, with artifacts ready for review.
