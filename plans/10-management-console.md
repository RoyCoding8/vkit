# Plan 10: the management console

Status: **accepted by the repository owner, 2026-09-29.** Sequenced LAST, after
Plans 01-09. Read [CONTRACT.md](CONTRACT.md) first, then Plan 05, which now owns
the same operations in a browser.

## Why this plan exists

The owner asked for a management console to monitor runs and manage the
installation. Plans 01 through 09 build the verifier, the agent interfaces, and
the setup operations. This plan is the one surface a human looks at.

It is last on purpose. A console over a moving core is a console that gets
rewritten. Built after 09, it is a view.

## Why it is not a settings editor

The original request included "modify settings." That is deliberately reduced,
and the reduction is the design.

`verification/manifest.json` is executable repository policy. It is committed,
reviewed in a pull request, and its digest is recorded in every run report. It
is what makes a PASS mean something. A console that can rewrite it is a console
that can quietly weaken the contract the evidence is measured against, without
anyone reviewing a diff.

So the console's writable surface is a fixed list, defined in
`src/vkit/admin.py` and nowhere else. Enrollment, install, repair, remove, and
run cancellation, because each already exists as a core operation with a
recorded result. Nothing else. The console does not edit the manifest, the
schemas, or the policy.

## Shape

A local HTTP server over the same core the CLI uses. No framework: the standard
library's `http.server` is sufficient for a loopback JSON API plus a small
static page, and adding a web framework to satisfy this is exactly the kind of
dependency the rest of the project refuses.

```text
src/vkit/console/
    server.py     loopback HTTP, routing, no business logic
    api.py        request/response shapes, validation at the boundary
    admin.py      THE writable surface: the one list of permitted mutations
    static/       index.html, app.js, style.css
```

`admin.py` is the file to review when asking "what can this thing change?"
Everything else is plumbing.

The server binds to `127.0.0.1` only and refuses any other address. It is not a
network service, does not authenticate because it has no remote peer, and must
not be described as one. A request that arrives from anywhere but loopback is
refused at the socket.

## What it shows

Every one of these is already a durable record from Plan 01. This is a view, not
a new source of truth.

1. **Project state.** Resolved root, Git common directory, HEAD, dirty status.
2. **Readiness.** What `doctor` reports, without running anything.
3. **Checks.** Each registered check with its id, command, timeout, and required
   scenarios.
4. **Runs.** Outcome, reason, timings, exit code, and log paths.
5. **One run in full.** The stored report and the tail of its logs.

Logs are paginated. A run can produce tens of megabytes, and a console that
loads one to render it is a console that hangs.

## What it can do

Only what `admin.py` permits, and each of these calls the existing core:

1. Run a registered check by id.
2. Cancel a run, naming the run and verifying process identity first.
3. Enroll a repository.
4. Install, repair, or remove the host integration.

Every mutation displays the exact change before applying it, writes an entry to
the run log, and is refused unless the core operation would accept it. The
console never has its own idea of what is valid.

## Acceptance

| Exercise | Required evidence |
| --- | --- |
| Server binds loopback only | Binding `0.0.0.0` is refused |
| Remote request refused | A non-loopback peer is rejected at the socket |
| Run list matches the CLI | Same runs, same outcomes, as `vkit run show` |
| A mutation is refused when the core refuses | A bad check id is rejected with the core's reason |
| A mutation is auditable | The console's action appears in the run log with the run id |
| A large log does not hang the console | A run with a 10 MB log renders the tail, not the whole file |
| Console cannot edit the manifest | No endpoint writes `verification/manifest.json` |
| Two browsers, one source of truth | Concurrent views agree because both read the store |
| Console down, nothing lost | Killing the server leaves every run readable |

## Limits

Browser-based, local-only, single user. No accounts, no multi-user, no remote
access, no HTTPS. A loopback server with no authentication is correct only while
it truly cannot be reached from another machine, which is why the bind address
is refused rather than warned about.

The console is a view. If it shows something the store does not, that is a bug in
the console, not a reason to add storage to it.
