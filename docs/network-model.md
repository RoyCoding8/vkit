# Network reachability changes

`compare_network_paths` compares successful packet reachability in two supplied Batfish
configuration snapshots. It runs Batfish's fixed `differentialReachability` question in both
snapshot orders. It does not accept question text, arbitrary Batfish parameters, engine commands,
or a service address from the input document.

The input paths are repository-relative directories. Each snapshot must contain `configs/` and may
contain `hosts/`; both directories may contain flat regular files only. Other snapshot inputs,
including explicit layer-1 topology, are unsupported by this operation. Files are captured once into
a temporary tree before upload. Each snapshot is limited to 128 files and 2 MiB total, with each
file limited to 256 KiB. Names must be short ASCII names; content must be UTF-8 without NUL bytes;
host files must be duplicate-key-free JSON objects. Symlinks, reparse points, nested directories,
and additional top-level entries are rejected. The result records a deterministic digest of the
captured paths, byte counts, and file hashes, so it identifies the exact configuration inputs sent
to Batfish.

The selector is a closed object with these required fields:

```json
{
  "start_node": "edge",
  "start_interface": "GigabitEthernet0/1",
  "end_node": "host-www",
  "destination_ip": "203.0.113.10",
  "protocol": "TCP",
  "destination_port": 443
}
```

`source_ip` is optional. IP fields accept one IP address or CIDR. `protocol` is TCP, UDP, or ICMP;
`destination_port` is an integer from 0 through 65535 and is only valid for TCP or UDP. Start node,
interface, and end node names are concrete identifiers used to build the fixed Batfish path
constraint. The question searches successful flows with filters applied, no inverted header search,
and at most one returned trace per direction.

The engine is `pybatfish==2026.9.17.3748` connected to port 9996 at the operator-configured
`VKIT_BATFISH_HOST` (default `127.0.0.1`). The API must report the exact Batfish build
`2026.08.27.3685` from the pinned image tag; the
expected multi-platform service-only image index is
`batfish/batfish@sha256:7909378197c5a3974f2e8180af58cef0ca17b74d1cd9ba39f2d1fddb64737e55`.
The operation checks the backend's reported Batfish release. The API does not attest the container
digest, so the digest field is a deployment pin that CI must enforce, not a runtime measurement.
The official image index includes Linux amd64 and arm64 images. Its `2026.08.27.3685` tag resolves
to this index, and its amd64 child manifest was confirmed from the registry. The service-only image
omits the built-in Jupyter server from the `allinone` image. The operation creates a uniquely named
network and deletes it after analysis. It runs the synchronous Batfish client in a fixed, isolated
worker process that the parent kills at a shared deadline of at most 300 seconds (default 120).
Each HTTP request is bounded by the remaining analysis time; the outer process deadline also covers
client polling and snapshot initialization.

For each snapshot, the operation checks `initIssues`, `fileParseStatus`, and `parseWarning`. Any
initialization/conversion issue, parse warning, non-`PASSED` file, missing file status, or more than
64 diagnostic rows makes the result `UNKNOWN`. The fixed differential question requests one trace
per direction and rejects more than four witness rows before converting a response frame to Python
records. Batfish may still materialize the full diagnostic dataframe before that row check; the
worker and wall deadline bound elapsed time and serialized output, not backend query memory. Only
clean diagnostics permit a reachability conclusion. It then
runs `differentialReachability` once with candidate as `snapshot` and base as `reference_snapshot`,
and once in the opposite order. This makes new and lost successful reachability visible without
assuming the question reports both directions. Each witness preserves the concrete flow and both
trace sets under the snapshot names used for that direction.
The operation maps Batfish's `Snapshot_*` result columns to the `snapshot=` name passed to
`answer()` and `Reference_*` columns to `reference_snapshot=`, then labels each trace set with
the explicit captured snapshot name. This follows Batfish's failure-analysis example, where the
failed snapshot is the `snapshot=` argument and its baseline is `reference_snapshot=`.

`DIFFERENT` means Batfish found at least one successful flow in one supplied snapshot that differs
from the other for the selector. `NO_DIFFERENCE` means both directional questions returned no
witness after both snapshots passed the diagnostics gate. It says nothing about route changes that
preserve successful reachability, other headers or paths, live devices, unmodeled topology, or
runtime state. `UNAVAILABLE` means the pinned client or expected backend release is missing;
`UNSUPPORTED` means the input is outside the closed contract; `UNKNOWN` covers timeout, service
failure, incomplete diagnostics, malformed results, or analysis failure. Network snapshots are
caller-provided evidence: this operation does not authenticate their source or establish that they
match deployed devices.

## Real-engine fixtures

`tests/test_network_paths.py` builds a literal two-router and host snapshot. The candidate adds a
static route from `edge` to `host-www`; the fixed selector expects Batfish to return a concrete
successful-flow witness. A second fixture uses identical snapshots and expects zero witnesses in
both query directions. These tests are skipped in ordinary local runs. Set
`VKIT_BATFISH_REQUIRED=1` in CI after starting the digest-pinned service; the flag makes missing or
incompatible engine setup fail the assertions instead of treating a skipped or mocked test as proof.

The public call copies validated captures into an internal temporary directory and invokes only the
fixed worker module with closed JSON arguments. It accepts no caller-supplied command or worker
source path. Snapshot enumeration stops as soon as a fixed directory-entry cap is exceeded, before
sorting or collecting the rest. IP selectors are IPv4-only; the fixed ICMP question therefore uses
Batfish's IPv4 protocol semantics.

This checkout has no Docker runtime in Windows or WSL, so the real Batfish fixture has not yet run
locally. Linux CI must exercise both fixtures before this operation is treated as engine-verified.
