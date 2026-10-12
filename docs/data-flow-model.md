# C data-flow model

`find_data_flows` runs the pinned Joern C frontend over a bounded snapshot and asks one fixed query for paths from an exact method parameter to an exact call argument. Callers provide method names and one-based parameter/argument positions; they cannot provide Joern scripts, CPGQL, compiler flags, or semantic models. The returned `flows[].table` is Joern's path table, including node kind, tracked code, line, method, and file where those fields are available. Capture is limited to 200 C/header files, 256 KB per file, 4 MB total source, 5,000 tree entries, and 10 seconds. Results and combined engine logs are each capped at 256 KiB; at most two paths are returned, and additional paths make the result `UNKNOWN` rather than a complete finding set.

The operation reports `PATH_FOUND` only after Joern resolves one selected source node, one selected sink node, and returns at least one complete path. `NO_PATH_IN_MODEL` means the pinned C frontend and Joern's configured data-flow semantics found no path between those selected nodes in the captured tree. It is not a claim that a vulnerability, exploit, or runtime path is absent. External calls without summaries may conservatively propagate values across arguments and returns, which can add paths. The result includes the source-tree digest, selected endpoints, exact endpoint/path counts, whole-CPG count of call sites without a resolved callee, engine release and archive digest, and any frontend diagnostics.

Use `UNSUPPORTED` for an invalid or ambiguous modeled selection or source tree beyond fixed capture bounds. Use `UNKNOWN` for a failed, timed out, over-limit, or malformed frontend/query result. Use `UNAVAILABLE` when the verified engine is not installed or the host cannot run this bounded POSIX adapter. Incomplete evidence never becomes `NO_PATH_IN_MODEL`.

The first backend is Joern v4.0.653 on Linux x86_64. The adapter uses the shared vkit process-tree timeout/cancel runner and bounds file-backed logs and CPG size. The installer downloads the official release archive, verifies its pinned SHA-256, safely extracts it, and checks `joern --version`. It requires a Java runtime accepted by that Joern release. Install and configure it with:

```sh
python scripts/install_dataflow_engine.py --cache-dir "$HOME/.cache/vkit/joern"
export VKIT_JOERN_HOME="$HOME/.cache/vkit/joern/4.0.653"
```

CI should cache the verified archive or installed directory by release and platform; the archive is about 1.8 GB. The operation currently reports `UNAVAILABLE` outside Linux x86_64. Although upstream publishes Windows and macOS binaries, this adapter has no native live engine and bounded-output probe on those systems yet.
