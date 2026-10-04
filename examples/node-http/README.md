# Node HTTP example

This service creates items with `POST /items` and lists them with `GET /items`.
Each instance has a separate state file. The check driver starts two instances,
uses ports allocated by the operating system, and sends real HTTP requests.

## Run the registered check

Install vkit and Node 18 or later. Copy this directory to a separate location,
then initialize and commit a Git repository there. From that directory:

```powershell
vkit doctor --project .
vkit check run --project . --check items-api
vkit features --project .
vkit console --project .
```

The driver checks the empty list, creation and readback, insertion order,
invalid input, unknown routes, and state isolation between instances.

The feature map reports two uncovered behaviors. The service has no update
route, and overlapping writes can lose updates. Passing the registered check
does not establish concurrent-write safety.

See the [main README](../../README.md) for installation and agent setup.
