# Repair verification

Repair branch: `codex/vkit-recheck-repairs`. Review baseline: `76aef82dcb28649bcc076646c9bd0f45293b2d79`.

## Changes

- Acceptance rechecks pinned checkout, generation and required claims. Verification, decision and READY publication share one SQLite write transaction.
- Recorded fixture identity participates in acceptance; absent identity cannot satisfy a declared fixture.
- Public task admission accepts the session/agent binding used by completion hooks.
- Protected integration requires an explicit expectation declaration. External expectation files and checker code are compared with the approved revision. Product inputs remain mutable.
- Detached supervisor ownership is claimed atomically with task ownership verification. Recovery abandonment fences delayed supervisors.
- POSIX terminal publication waits for the owned process group; recovery refuses release while a recorded group survives.
- Console installation and validation resolve the packaged marketplace through the same resource authority.
- CI test entry points run through Python on all three platforms and print skip reasons.

## Verification

The combined core/oracle regressions passed: 11 tests. An independent claim-release race probe returned READY with one claim still held and the concurrent release blocked. Both Python guard mutations produced the expected assertion failures after the mutation harness was updated for the moved guard. The installed-wheel console witness passed without invoking a live host.

The detached public MCP fixture regression passed on the combined tree. Collection reported 757 tests. Full repair CI is pending. Full suites have not been run locally during this repair.

## Remaining limits

The manifest declaration is a reviewed contract: vkit does not infer undeclared checker reads. POSIX process groups cannot contain hostile `setsid` escapes, and cross-process cancellation limits remain. A claim-refused launch stays recovery-visible without inventing a terminal receipt.

Fresh host/subagent completion gating, finished operator enrollment, hosted protected integration and a large-worker pilot still need direct verification. The acceptance matrix retains 57 rows without specific evidence mappings. These repairs do not close those release gates or establish a 100-agent scale claim.
