# Plan 05: the setup console

Status: **owner decision, 2026-09-29.** This plan replaces the original terminal
wizard and absorbs the former Plan 10 management console. There is now exactly
one setup surface: a local browser console over the setup operations.

Read [CONTRACT.md](CONTRACT.md) first. Ready after Plan 04 provides a tested
plugin package and installation mechanism.

## Outcome

A user opens a local page, sees what is installed and what is missing, selects
components and scope, sees the exact changes that will be made, and applies
them. The same page monitors runs, shows stored evidence, and can cancel a run.
No hand-editing of unrelated configuration files, and no second setup engine.

## Why one plan and not two

The owner originally asked for a terminal setup wizard and, separately, a
management console. Those are the same product. Building both would mean two
renderers over one core, and the second one would rot. So the console is the only
setup surface, and the operations live in a layer it does not own.

## Shape

A local HTTP server over the same core the CLI and MCP use. No web framework:
the standard library's `http.server` covers a loopback JSON API plus a small
static page. Adding a framework here is the kind of dependency the rest of the
project refuses.

```text
src/vkit/console/
    operations.py   install, repair, remove, enroll. Pure. The real logic.
    plan.py         the exact change set, computed and shown before applying
    server.py       loopback HTTP and routing. No business logic.
    api.py          request and response shapes. Validation at the boundary.
    static/         index.html, app.js, style.css
```

It shipped as `console/`, not `setup/`. The name changed with no change to the
layering, and this is the only file that still says otherwise.

The dependency direction matters and is the whole design: `operations.py` knows
nothing about HTTP, and `server.py` knows nothing about installing. A test drives
`operations.py` directly and never starts a server.

## The writable surface

This is the file to read when asking "what can this thing change?" It is one
list, in `src/vkit/setup/plan.py`, and nothing outside it writes.

| Operation | Effect |
| --- | --- |
| enroll | Register a repository as verified |
| install | Install the host plugin, skills, and hooks at a chosen scope |
| repair | Re-apply a drifted installation |
| remove | Undo an installation, preserving recoverable prior values |
| run check | Start a registered check |
| cancel run | Stop a running check, verifying process identity first |

**The manifest is not on this list, and that is deliberate.**
`verification/manifest.json` is executable repository policy. It is committed,
reviewed in a pull request, and its digest is recorded in every run report. A
console that could rewrite it would let an operator weaken the contract the
evidence is measured against without any diff to review. Changing the manifest
means making a commit. The same applies to the schemas and to the policy digest.

Every mutation goes through an operation that already exists, displays the
resolved change set before applying it, and records what it did.

## Local-only, and why that is a constraint rather than a caveat

The server binds `127.0.0.1` and refuses any other address, at the socket. A
request from a non-loopback peer is refused. There is no authentication because
there is no remote peer, and it must not be described as a network service.

A loopback server with no auth is only safe while it genuinely cannot be reached
from another machine. That is why the bind address is refused rather than
warned about.

Single user. No accounts, no multi-user, no HTTPS. If a second user ever needs
it, that is a new plan, not a configuration flag.

## What it shows

All of it is already a durable record from Plan 01. The console is a view, not a
new source of truth. If it displays something the store does not hold, that is a
bug in the console, not a reason to add storage to it.

1. **Project.** Root, Git common directory, HEAD, dirty status.
2. **Readiness.** What `doctor` reports, running nothing.
3. **Installation.** Plugin, skills, and hooks present, missing, or drifted.
4. **Checks.** Every registered check with its id, command, timeout, scenarios.
5. **Runs.** Outcome, reason, timings, exit code, log paths.
6. **One run in full.** The stored report and the tail of its logs.

Logs are paginated. A run can produce tens of megabytes, and a console that
loads one to render it is a console that hangs.

## What it does

Only the six operations above, each calling `operations.py`. The console never
holds its own idea of what is valid; if the operation refuses, the console shows
that refusal rather than a friendlier invention.

Install must distinguish app-provided skills from pstack, must not create
duplicate names or stale copies, and must preserve recoverable prior values.
Reusing an existing installation is the default when a compatible one is found.

## Acceptance

| Exercise | Required evidence |
| --- | --- |
| Server binds loopback only | Binding `0.0.0.0` is refused |
| Remote peer refused | Rejected at the socket |
| Operations work without a server | `operations.py` tested directly; no HTTP in its tests |
| Run list matches the CLI | Same runs and outcomes as `vkit run show` |
| A refused mutation shows the core's reason | A bad check id yields the operation's message, not a reworded one |
| Install shows changes before applying | The change set is visible in a test, not only in the browser |
| No endpoint writes the manifest | Asserted against the writable list |
| Large log does not hang | A 10 MB log renders its tail, not the whole file |
| Killing the console loses nothing | Every run still readable afterwards |
| Two browsers agree | Both read the store; no console-held state |
| Prior settings preserved | An install over an existing config records the previous value |

## Limits

Browser-based, local-only, single user. Not a network service. No accounts, no
remote access, no HTTPS. The console is a view; a fact it shows must exist in the
store first.
