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
`VKIT_BATFISH_HOST` (default `127.0.0.1`). The expected Batfish release is `2026.08.27`; the
expected multi-platform image index is
`batfish/allinone@sha256:54cb0ed94fd9a3c1ca0985f73e5be479e955cee9b9f6a6799b66be3364fd8e5c`.
The operation checks the backend's reported Batfish release. The API does not attest the container
digest, so the digest field is a deployment pin that CI must enforce, not a runtime measurement.
The operation creates a uniquely named network and deletes it after analysis. It polls background
work under a shared deadline of at most 60 seconds and bounds every client request by the remaining
time.

For each snapshot, the operation checks `initIssues`, `fileParseStatus`, and `parseWarning`. Any
initialization/conversion issue, parse warning, non-`PASSED` file, missing file status, or diagnostic
limit makes the result `UNKNOWN`. Only clean diagnostics permit a reachability conclusion. It then
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

This checkout has no Docker runtime in Windows or WSL, so the real Batfish fixture has not yet run
locally. Linux CI must exercise both fixtures before this operation is treated as engine-verified.
