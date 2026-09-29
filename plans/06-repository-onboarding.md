# Plan 06: connect real repositories and feature maps

Read [CONTRACT.md](CONTRACT.md). The core work can start after Plan 01. Complete the plugin acceptance after Plan 04. Coordinate ownership with Plan 02 if implemented concurrently.

## Outcome

A user or agent can inspect a repository, reuse its existing run/test tools, and establish a small verified feature map. The app identifies missing coverage rather than pretending to understand every feature automatically. Python and Node examples demonstrate that the core is reusable.

Use `create-verification-skill` to establish the first real path and `maintain-verification-skill` to maintain it. Read their installed instructions and adapt runner-specific output paths. Do not invoke their delegation steps without authorization. Reuse valid Claude `/run` recipes and pstack drivers.

## Operations

```text
vkit project inspect --project <repo> --json
vkit project enroll --project <repo>
vkit features --project <repo> --json
```

Extend MCP `project_inspect` with the same bounded discovery output. Add `/vkit:onboard` to the plugin. Before enrollment it can inspect and help the host agent draft configuration; execution stays disabled until the user accepts the repository's executable policy.

The core makes no LLM calls. Mechanical inspection discovers package scripts, test configuration, existing CI commands, and launch recipes. The host agent or engineer proposes intended behaviors and feature descriptions, grounded in source and user requirements. The app validates and exercises that proposal. Inferred features remain unverified until the corresponding paths are actually checked.

## Implement

1. Inspect supported Python and Node project files without executing arbitrary install scripts. Report discovered commands with provenance, ambiguity, and prerequisites.
2. Enrollment writes or proposes a minimal manifest and feature index. Never overwrite a hand-maintained file blindly. Preview the exact command policy and selected scenarios before enabling execution.
3. A feature document states the user-visible behavior, entry point, setup/fixtures, checks, expected outcomes, and known coverage gaps. Assertions remain in the check implementation.
4. Reuse the Plan 01 Python example. Add a small Node HTTP app with two interacting behaviors, such as creating an item and retrieving the updated list. Drive it through actual HTTP requests and literal assertions. Node's existing test tools and HTTP facilities are sufficient; use browser automation only for a real browser behavior.
5. Give app instances isolated ports and fixtures. Prefer dynamic port allocation communicated through an owned readiness artifact rather than finding a free port and later assuming it stayed free. Register resources through Plan 02 once available.
6. Translate existing test reports through focused adapters into the check-artifact schema. Do not create a generic parser for every testing framework. Show zero-tests, process failures, and malformed output as incomplete evidence.
7. Implement conservative affected-check selection. Exact explicit mappings may narrow development checks. Missing mappings select a broader relevant suite or report a gap. Shared configuration/driver changes require their dependent scenarios to rerun.
8. Keep maintenance proposals separate from acceptance changes. A behavior regression cannot silently rewrite the expected outcome in the feature map.

## Acceptance

- Enroll the Python example and the Node example from fresh Git repositories and run one complete setup/interaction/assertion/cleanup cycle in each.
- Demonstrate PASS, a real application defect yielding FAIL, and an unavailable prerequisite yielding BLOCKED in each ecosystem.
- Existing launch recipes remain the single source for the reused setup facts; changing a recipe invalidates affected evidence.
- Two Node app instances operate concurrently with distinct resources and neither reads the other's fixtures.
- A changed unmapped file triggers broader checks or a declared coverage gap, never an empty passing selection.
- A driver change forces live re-exercise of its path.
- A missing feature is visible in the map as uncovered; it is not counted as verified.
- `/vkit:onboard` and subsequent `/vkit:work` in a fresh supported session find the enrolled contract and real checks. Record any unrun live-host tests explicitly.
- Declining enrollment leaves commands unexecuted and existing files unchanged.
- Retained evidence survives cleanup of application fixtures and owned temporary processes.

Use the sample repositories to test portability; do not claim they establish useful coverage of an unrelated personal project. The real pilot arrives in Plan 09.

## Finish and handoff

Return both examples, runnable onboarding instructions, manifests/feature maps, source-backed discovery output, and failure counterchecks. Plans 07 and 09 use these examples as known-good inputs. If the worker improves a shared manifest interface, coordinate it with the core owner and migrate all callers together.
