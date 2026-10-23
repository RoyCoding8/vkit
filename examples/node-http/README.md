# Node HTTP example

## Service behavior

| Operation | Behavior |
| --- | --- |
| `POST /items` | Create an item. |
| `GET /items` | List items. |
| Instance state | Each instance has a separate state file. |
| Check driver | Starts two instances, uses OS-allocated ports, and sends real HTTP requests. |

## Run the check

1. Install VKit and Node 18 or later.
2. Copy this directory to a separate location.
3. Initialize and commit a Git repository there.
4. Run these commands from that directory:

```powershell
vkit doctor --project .
vkit accept --project .
vkit check run --project . --check items-api
vkit features --project .
vkit console --project .
```

## Coverage and gaps

| Status | Behavior |
| --- | --- |
| Checked by `items-api` | Empty list, creation and readback, insertion order, invalid input, unknown routes, and state isolation. |
| Uncovered in the feature map | Updating an item. The service has no update route. |
| Uncovered in the feature map | Concurrent-write safety. Overlapping writes can lose updates. |

Passing `items-api` does not establish concurrent-write safety.

See the [main README](../../README.md) for installation and agent setup.
