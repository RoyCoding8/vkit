# vkit

Run a registered check in a Git repository and keep a durable, trustworthy
outcome. A real defect fails. Missing evidence cannot pass.

The premise is that an agent goes fast only when the mechanical layer absorbs
the checking. This package is that layer: it launches a check as an owned
process, decides PASS, FAIL, or BLOCKED from what actually happened, and writes
a report that survives the checkout that produced it.

## Install and run

Every command below was executed from a clean virtualenv outside this source
tree, against the example application.

```console
$ uv venv --seed /tmp/venv
$ /tmp/venv/Scripts/python.exe -m pip install "D:/AI/Poteto's Style"
Successfully installed attrs-26.1.0 jsonschema-4.26.0 ... vkit-0.1.0
```

That install covers the CLI, the console, and the hook. The MCP server needs
the SDK, which is an extra rather than a hard dependency because the six tools
are useful without a transport and a missing optional package must not break
anything else.

```console
$ /tmp/venv/Scripts/python.exe -m pip install "D:/AI/Poteto's Style[mcp]"
Successfully installed mcp-2.2.0 ... vkit-0.1.0
```

Initialize a repository holding a real check. The example ships its own
manifest and a driver that runs the actual application.

```console
$ cp -r "D:/AI/Poteto's Style/examples/python-cli" "/tmp/demo repo"
$ cd "/tmp/demo repo" && git init -q && git add -A
$ git -c user.email=t@t -c user.name=t commit -qm example
```

Report whether the project could run its checks. This launches nothing and
installs nothing.

```console
$ vkit doctor --project .
project : C:\Users\roysh\AppData\Local\Temp\demo repo
state   : C:\Users\roysh\AppData\Local\Temp\demo repo\.git\verification-kit (writable)
head    : e899d4c43ffc  dirty=False
checks  : totals-behavior
  [ok  ] python: C:\CLI\cx\.venv\Scripts\python.EXE
ready
$ echo $?
0
```

Run the check.

```console
$ vkit check run --project . --check totals-behavior
run 27d967e7a1014f538d480309bba3b8e4  check totals-behavior
  PASS empty-cart: printed 0
  PASS single-positive: printed 42
  PASS several-positives: printed 42
  PASS mixed-sign: printed 42
  PASS negatives-only: printed -12
  PASS cancels-to-zero: printed 0
PASS
$ echo $?
0
```

Break the application and watch the harness notice. The defect is a real change
to the arithmetic, not a flag the driver is told about.

```console
$ sed -i 's/running += amount$/running += amount + 1/' src/totals.py
$ vkit check run --project . --check totals-behavior
run 7fc7f3ca354b40f9a6871a87818a0e70  check totals-behavior
  PASS empty-cart: printed 0
  FAIL single-positive: expected 42, printed 43
  FAIL several-positives: expected 42, printed 45
  FAIL mixed-sign: expected 42, printed 45
  FAIL negatives-only: expected -12, printed -10
  FAIL cancels-to-zero: expected 0, printed 2
FAIL
$ echo $?
1
```

Inspect a stored run. The report is on disk under the shared Git directory, so
it outlives the checkout.

```console
$ vkit run show --project . --run fd15ebd15cca4b6096ab76ec5a59da69
run fd15ebd15cca4b6096ab76ec5a59da69  PASS
  ...
$ ls .git/verification-kit/runs/*/report.json
.git/verification-kit/runs/27d967e7a1014f538d480309bba3b8e4/report.json
```

Add `--json` to any command for one structured object on stdout. Diagnostics go
to stderr, so `vkit ... --json | jq` always works.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | The command succeeded, and the check passed |
| 1 | The check completed and failed |
| 2 | Invalid invocation: unknown check, bad manifest, unsupported schema |
| 3 | BLOCKED: evidence was insufficient to decide, and the report says why |
| 4 | Internal application error |

## How a run is decided

A run is preparing, running, or terminal. A terminal run has exactly one
outcome, and it is derived from the process and its artifact, never from a
flag.

| Outcome | When |
| --- | --- |
| PASS | The process exited zero and the artifact reported every required scenario passing |
| FAIL | A valid artifact reported a required scenario failing |
| BLOCKED | Timeout, cancellation, missing tool, missing or malformed artifact, no scenarios, an unknown required scenario, source changed mid-run, or a refused launch |

Both conditions are required for PASS. A command that exits zero and writes no
artifact is BLOCKED, because a silent success is the failure this product
exists to catch. A report is published atomically, so a reader never sees half
of one, and a terminal run can never be republished: the first result survives a
later attempt.

## Writing a check

A check is declared in `verification/manifest.json` and identifies itself by id,
never by command line, so an agent can select a check without being able to run
an arbitrary shell string.

```json
{
  "schema_version": 1,
  "checks": [
    {
      "id": "totals-behavior",
      "command": ["python", "verify_totals.py", "--out", "{{run_dir}}/result.json"],
      "timeout_seconds": 120,
      "required_scenarios": ["empty-cart", "mixed-sign"],
      "artifact": "result.json"
    }
  ]
}
```

`command` is an argument array, never a shell string. `timeout_seconds` must be
positive and finite. `required_scenarios` must name at least one scenario. The
only two placeholders are `{{run_dir}}` and `{{python}}`, both substituted
literally with nothing evaluated.

The check writes a versioned artifact:

```json
{
  "schema_version": 1,
  "scenarios": [
    { "id": "mixed-sign", "result": "PASS", "observation": "printed 42" }
  ]
}
```

`observation` is what a reader needs in order to believe the result without
re-running. The driver in the example has no flag that tells it what to report,
so it cannot agree with the application by construction.

## Verifying this package

```console
$ .venv/Scripts/python.exe -m pytest tests/ --collect-only -q
715 tests collected

$ .venv/Scripts/python.exe scripts/acceptance.py
22/22 acceptance rows pass
```

The first line is a collection count, not a pass count. Run the suite without
`--collect-only` to see what actually passed; a collected test that skips and a
collected test that fails are both in that total, which is why the number here
carries no verdict.

The second command walks the Plan 01 acceptance table against the installed
command and is the artifact to rerun before trusting anything here.

The MCP transport has its own suite, and it is separate on purpose. It spawns
the shipped `vkit mcp serve` as a subprocess and speaks JSON-RPC to it over
stdin and stdout, so it needs the extra installed to run at all.

```console
$ .venv/Scripts/python.exe -m pytest tests/test_mcp_stdio.py
```

`tests/mcp_client.py` holds the client, and it imports neither this package
nor the SDK. That is deliberate: a client built from the same SDK would agree
with the server about a changed contract instead of disagreeing with it.

## Known limits

The POSIX path is written from documented semantics. This milestone was
verified on Windows 11 with Python 3.13.14. CI runs this suite on
`ubuntu-latest`, `windows-latest` and `macos-latest`, so the local POSIX harness
has been deleted. **No POSIX run of this suite is recorded in this
repository**: a CI job's receipt is the forge's run log rather than a file here,
and that job is red at this revision. POSIX-CLAIM: no-receipt.
`docs/RELEASE-CHECKLIST.md` carries this as GAP-2, and
`tests/test_release_docs.py::_posix_position_derived_from_the_tree` is where
the position is derived rather than restated.
Its timeout and completion control flow is exercised on Windows with the two
POSIX-only mechanisms stubbed, because an ordinary timeout there used to raise
`UnboundLocalError` instead of reporting a timeout. Group signalling itself is
unverified. The boundary is a descendant that calls `setsid` or `setpgid` leaves
the process group, and no signal reaches it.

A `.cmd` launcher cannot carry a non-ASCII path, because batch file contents are
read in the active ANSI code page. That is `cmd.exe` behavior, not this
package's. A check registered as a `.cmd` under a non-ASCII repository path will
mangle its arguments.

The source digest cannot detect an edit that is made and reverted while a check
runs. It compares inventory before and after, which is a real limit rather than
a known gap.

Dirty trees are reported honestly and are not integration evidence. A report
says whether the tree was dirty at run time so nobody mistakes development
evidence for a clean integration result.

The MCP transport is pinned to the `mcp` 2.x server API. That line is a
rewrite rather than an increment, so handlers are constructor arguments
rather than decorators and a tool call returns a `CallToolResult`. A release
outside the pin in `pyproject.toml` has not been verified here. Without the
extra installed, `vkit mcp serve` exits 5 and says which package to install;
it does not hang, and it does not write to the protocol channel.

The protocol suite proves a real client and this server agree on the wire. It
proves nothing about whether Claude Code loads the plugin that starts it.
