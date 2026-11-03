# Getting started

This guide uses a Python CLI example to demonstrate check execution, evidence freshness, and verification gates.

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/), Git, and Python 3.11 or newer. Clone VKit as shown in the [README](../README.md#get-started), then run the following commands from the VKit checkout.

## Run the example

```sh
uv run python examples/quickstart.py
```

The command creates a separate repository under `tmp/`, accepts the bundled `totals-behavior` definition, runs its six scenarios, and evaluates the gate. A commit is not required.

The expected result is `totals-behavior: PASS`, six passing scenarios, and a `READY` gate. Each execution creates a new directory.

The command prints the example repository's path. Replace `DEMO_PATH` below with that path. Its `verification/manifest.json` registers a driver that compares the program's output with expected totals.

## Evidence freshness

Open `src/totals.py` inside the printed demo directory. Change this line:

```python
running += amount
```

to:

```python
running += amount + 1
```

Check the status and gate without rerunning the check:

```sh
uv run vkit status --project DEMO_PATH
uv run vkit gate --project DEMO_PATH
```

Status marks `totals-behavior` as `stale`. The gate reports `BLOCKED` and exits with code `3`, because the previous pass covers a different source snapshot.

Run the check against the changed program:

```sh
uv run vkit check run --project DEMO_PATH --check totals-behavior
uv run vkit gate --project DEMO_PATH
```

The check reports `FAIL` with the incorrect totals, and the gate reports `REJECTED`. Both commands exit with code `1`.

## Restore passing evidence

Restore `running += amount`, then rerun:

```sh
uv run vkit check run --project DEMO_PATH --check totals-behavior
uv run vkit gate --project DEMO_PATH
```

The check returns `PASS`, and the gate returns `READY`.

## Console

```sh
uv run vkit console --project DEMO_PATH
```

Open **Runs** to inspect the attempts and their scenario results. In **Checks**, use **Run** to submit another attempt. **Feature map** shows which application behaviors the check covers and which still have gaps.

The feature map includes coverage gaps for non-integer input and very large amounts. The gate evaluates registered requirements; the feature map describes application coverage.

## Repository configuration

Start with an [example](../README.md#documentation) close to your application. Define its checks in `verification/manifest.json` using the [manifest schema](../schemas/manifest.v3.json).

Review the declared commands and inputs before accepting a repository's definitions:

```sh
uv run vkit doctor --project /path/to/your-repository
uv run vkit accept --project /path/to/your-repository
uv run vkit check run --project /path/to/your-repository --needed
uv run vkit gate --project /path/to/your-repository
```

Add `verification/features.json` to map application behavior to checks, following the [example feature map](../examples/python-cli/verification/features.json).

The [console guide](console.md) explains daily use. For proofs and model checking, see the [verification reference](verification.md).
