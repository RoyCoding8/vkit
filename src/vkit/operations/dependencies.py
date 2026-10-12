"""Analyze a bounded JavaScript and TypeScript dependency snapshot."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from ._runtime import pinned_node_helper

MAX_FILES = 1_000
MAX_TREE_ENTRIES = 20_000
MAX_FILE_BYTES = 1_048_576
MAX_TOTAL_BYTES = 16_777_216
MAX_OUTPUT_BYTES = 8_388_608
MAX_TIMEOUT_SECONDS = 60
DEPENDENCY_CRUISER_VERSION = "18.5.0"
TYPESCRIPT_VERSION = "5.9.3"
NODE_VERSION = "v24.16.0"
_HELPER_ENV = "VKIT_DEPENDENCY_HELPER"
_HELPER_SHA_ENV = "VKIT_DEPENDENCY_SHA256"
_LOCK_ENV = "VKIT_DEPENDENCY_LOCKFILE"
_LOCK_SHA_ENV = "VKIT_DEPENDENCY_LOCK_SHA256"
_PROTOCOL_VERSION = 1
_SOURCE_EXTENSIONS = {".cjs", ".cts", ".js", ".jsx", ".mjs", ".mts", ".ts", ".tsx"}
_SKIP_DIRECTORIES = {
    ".git", ".hg", ".next", ".nuxt", ".turbo", ".venv", ".vscode", ".yarn",
    "build", "coverage", "dist", "node_modules", "out", "vendor",
}
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_EXTERNAL_TYPES = {"npm", "npm-dev", "npm-optional", "npm-peer", "npm-no-pkg"}
_MODEL_SCOPE = (
    "Static JavaScript and TypeScript dependency extraction from the captured tree. "
    "Bare specifiers are reported as external references, including possible aliases that are not resolved. "
    "Runtime-loaded modules, conditional execution, and package internals are outside this model. "
    "No missing static edge establishes runtime absence."
)


@dataclass(frozen=True)
class _Edge:
    source: str
    target: str | None
    specifier: str
    dynamic: bool
    external: bool

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "target": self.target, "specifier": self.specifier,
                "dynamic": self.dynamic, "external": self.external}


@dataclass(frozen=True)
class _Graph:
    nodes: tuple[str, ...]
    edges: tuple[_Edge, ...]
    adjacency: Mapping[str, tuple[str, ...]]
    reverse: Mapping[str, tuple[str, ...]]


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _relative_path(value: Any, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError(f"{label} must be a repository-relative POSIX path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"{label} must be a repository-relative POSIX path")
    return relative


def _source_directory(root: Path, path: str) -> tuple[Path, str]:
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        raise ValueError("path must identify a repository-relative directory")
    if path == ".":
        relative = PurePosixPath()
    else:
        relative = _relative_path(path, "path")
    if any(part.casefold() in _SKIP_DIRECTORIES for part in relative.parts):
        raise ValueError("path cannot select an excluded build, dependency, or metadata directory")
    base = root.resolve()
    target = base.joinpath(*relative.parts)
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or _is_reparse_point(current):
            raise ValueError("source paths cannot contain symlinks or reparse points")
    resolved = target.resolve()
    if not resolved.is_relative_to(base) or not resolved.is_dir():
        raise ValueError("path must identify a directory inside the repository")
    return resolved, "." if not relative.parts else relative.as_posix()


def _is_reparse_point(path: Path) -> bool:
    try:
        return bool(path.lstat().st_file_attributes & _REPARSE_POINT)
    except AttributeError:
        return False


def _snapshot(source: Path, destination: Path, deadline: float) -> tuple[str, tuple[str, ...]]:
    files: list[tuple[str, Path]] = []
    entries_seen = 0
    pending = [source]
    while pending:
        current = pending.pop()
        if time.monotonic() >= deadline:
            raise TimeoutError
        try:
            if current.is_symlink() or _is_reparse_point(current) \
                    or not current.resolve(strict=True).is_relative_to(source.resolve()):
                raise ValueError("source trees cannot traverse symlinks, reparse points, or escapes")
            with os.scandir(current) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    entries_seen += 1
                    if entries_seen > MAX_TREE_ENTRIES:
                        raise ValueError(f"source tree has more than {MAX_TREE_ENTRIES} entries")
                    candidate = Path(entry.path)
                    if entry.is_symlink() or _is_reparse_point(candidate):
                        raise ValueError("source trees cannot contain symlinks or reparse points")
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.casefold() not in _SKIP_DIRECTORIES:
                            pending.append(candidate)
                    elif entry.is_file(follow_symlinks=False) and candidate.suffix.lower() in _SOURCE_EXTENSIONS:
                        files.append((candidate.relative_to(source).as_posix(), candidate))
        except OSError as exc:
            raise ValueError(f"cannot enumerate source tree: {exc}") from exc
    files.sort(key=lambda item: item[0])
    if len(files) > MAX_FILES:
        raise ValueError(f"source tree has more than {MAX_FILES} supported source files")

    digest = hashlib.sha256()
    total = 0
    relative_paths: list[str] = []
    for relative, source_file in files:
        if time.monotonic() >= deadline:
            raise TimeoutError
        try:
            before = source_file.lstat()
            if source_file.is_symlink() or _is_reparse_point(source_file) or not source_file.is_file():
                raise ValueError("source trees cannot contain symlinks, reparse points, or special files")
            with source_file.open("rb") as handle:
                content = handle.read(MAX_FILE_BYTES + 1)
            after = source_file.lstat()
        except OSError as exc:
            raise ValueError(f"cannot read source file {relative!r}: {exc}") from exc
        if len(content) > MAX_FILE_BYTES:
            raise ValueError(f"source file {relative!r} exceeds {MAX_FILE_BYTES} bytes")
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError(f"source file {relative!r} changed while being snapshotted")
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise ValueError(f"source tree exceeds {MAX_TOTAL_BYTES} total bytes")
        destination_file = destination.joinpath(*PurePosixPath(relative).parts)
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        destination_file.write_bytes(content)
        relative_bytes = relative.encode("utf-8")
        digest.update(len(relative_bytes).to_bytes(4, "big"))
        digest.update(relative_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        relative_paths.append(relative)
    return digest.hexdigest(), tuple(relative_paths)


def _fields(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    missing = expected - set(value)
    if missing:
        raise ValueError(f"{label} is missing fields: {', '.join(sorted(missing))}")
    return value


def _parse_backend(raw: Any, files: tuple[str, ...]) \
        -> tuple[_Graph, tuple[dict[str, Any], ...], tuple[dict[str, Any], ...],
                 tuple[dict[str, Any], ...], bool, tuple[str, ...]]:
    response = _fields(raw, {"protocol_version", "backend_version", "typescript_version", "modules", "complete",
                             "incomplete_reasons"}, "helper response")
    if response["protocol_version"] != _PROTOCOL_VERSION \
            or response["backend_version"] != DEPENDENCY_CRUISER_VERSION \
            or response["typescript_version"] != TYPESCRIPT_VERSION:
        raise RuntimeError("backend identity did not match the pinned package versions")
    if type(response["complete"]) is not bool or not isinstance(response["modules"], list):
        raise ValueError("helper response has invalid graph fields")
    if not isinstance(response["incomplete_reasons"], list) \
            or any(not isinstance(item, str) for item in response["incomplete_reasons"]):
        raise ValueError("helper response has invalid completeness details")
    expected_files = set(files)
    nodes: set[str] = set()
    raw_edges: list[tuple[str, dict[str, Any]]] = []
    modules = response["modules"]
    for index, value in enumerate(modules):
        module = _fields(value, {"source", "dependencies"}, f"modules[{index}]")
        source = module["source"]
        if not isinstance(source, str) or source not in expected_files or source in nodes:
            raise ValueError(f"modules[{index}].source is unknown or duplicated")
        if not isinstance(module["dependencies"], list):
            raise ValueError(f"modules[{index}].dependencies must be a list")
        nodes.add(source)
        for dependency in module["dependencies"]:
            raw_edges.append((source, _fields(dependency, {"module", "resolved", "dynamic", "couldNotResolve",
                                                           "coreModule", "dependencyTypes"}, "dependency")))

    edges: list[_Edge] = []
    unresolved: list[dict[str, Any]] = []
    external_dependencies: list[dict[str, Any]] = []
    dynamic: list[dict[str, Any]] = []
    for source, dependency in raw_edges:
        specifier = dependency["module"]
        resolved = dependency["resolved"]
        if not isinstance(specifier, str) or type(dependency["dynamic"]) is not bool \
                or type(dependency["couldNotResolve"]) is not bool \
                or type(dependency["coreModule"]) is not bool \
                or not isinstance(dependency["dependencyTypes"], list) \
                or any(not isinstance(item, str) for item in dependency["dependencyTypes"]):
            raise ValueError("dependency-cruiser returned an invalid dependency")
        external = dependency["coreModule"] or bool(_EXTERNAL_TYPES.intersection(dependency["dependencyTypes"]))
        target = resolved if isinstance(resolved, str) and resolved in expected_files else None
        edge = _Edge(source, target, specifier, dependency["dynamic"], external)
        edges.append(edge)
        if dependency["dynamic"]:
            dynamic.append(edge.as_dict())
        if target is None:
            (external_dependencies if external else unresolved).append(edge.as_dict())
    edges.sort(key=lambda edge: (edge.source, edge.target or "", edge.specifier, edge.dynamic, edge.external))
    adjacency_lists: dict[str, set[str]] = {node: set() for node in nodes}
    reverse_lists: dict[str, set[str]] = {node: set() for node in nodes}
    for edge in edges:
        if edge.target in nodes:
            adjacency_lists[edge.source].add(edge.target)
            reverse_lists[edge.target].add(edge.source)
    graph = _Graph(tuple(sorted(nodes)), tuple(edges),
                   MappingProxyType({node: tuple(sorted(targets)) for node, targets in adjacency_lists.items()}),
                   MappingProxyType({node: tuple(sorted(sources)) for node, sources in reverse_lists.items()}))
    return (graph, tuple(unresolved), tuple(external_dependencies), tuple(dynamic),
            response["complete"] and nodes == expected_files, tuple(response["incomplete_reasons"]))


def _with_repository_paths(graph: _Graph, path: str) -> _Graph:
    def prefix(value: str) -> str:
        return f"{path}/{value}" if path != "." else value

    edges = tuple(sorted((_Edge(prefix(edge.source), prefix(edge.target) if edge.target else None,
                                edge.specifier, edge.dynamic, edge.external) for edge in graph.edges),
                         key=lambda edge: (edge.source, edge.target or "", edge.specifier,
                                           edge.dynamic, edge.external)))
    nodes = tuple(prefix(node) for node in graph.nodes)
    adjacency = {node: tuple(prefix(neighbor) for neighbor in graph.adjacency[old])
                 for node, old in zip(nodes, graph.nodes)}
    reverse = {node: tuple(prefix(neighbor) for neighbor in graph.reverse[old])
               for node, old in zip(nodes, graph.nodes)}
    return _Graph(nodes, edges, MappingProxyType(adjacency), MappingProxyType(reverse))


def _reachable(starts: tuple[str, ...], adjacency: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    seen = set(starts)
    queue = deque(sorted(starts))
    while queue:
        current = queue.popleft()
        for neighbor in adjacency[current]:
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append(neighbor)
    return tuple(sorted(seen))


def _path(graph: _Graph, source: str, target: str) -> tuple[str, ...] | None:
    pending = deque([source])
    previous: dict[str, str | None] = {source: None}
    while pending:
        current = pending.popleft()
        if current == target:
            result: list[str] = []
            cursor: str | None = current
            while cursor is not None:
                result.append(cursor)
                cursor = previous[cursor]
            return tuple(reversed(result))
        for neighbor in graph.adjacency[current]:
            if neighbor not in previous:
                previous[neighbor] = current
                pending.append(neighbor)
    return None


def _components(graph: _Graph) -> tuple[tuple[str, ...], ...]:
    visited: set[str] = set()
    finished: list[str] = []
    for start in graph.nodes:
        if start in visited:
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            node, expanded = stack.pop()
            if expanded:
                finished.append(node)
                continue
            if node in visited:
                continue
            visited.add(node)
            stack.append((node, True))
            stack.extend((neighbor, False) for neighbor in reversed(graph.adjacency[node]) if neighbor not in visited)
    assigned: set[str] = set()
    components: list[tuple[str, ...]] = []
    for start in reversed(finished):
        if start in assigned:
            continue
        component: list[str] = []
        pending = [start]
        assigned.add(start)
        while pending:
            node = pending.pop()
            component.append(node)
            for neighbor in graph.reverse[node]:
                if neighbor not in assigned:
                    assigned.add(neighbor)
                    pending.append(neighbor)
        components.append(tuple(sorted(component)))
    return tuple(sorted((component for component in components
                         if len(component) > 1 or component[0] in graph.adjacency[component[0]]),
                        key=lambda component: component))


def _request_environment() -> dict[str, str]:
    return {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if key in os.environ}


def analyze_dependencies(root: Path, path: str, query: str = "graph", sources: list[str] | None = None,
                         target: str | None = None, timeout_seconds: int = 20) -> dict[str, Any]:
    """Analyze static dependencies within a selected repository directory."""
    if query not in {"graph", "path", "cycles", "affected", "unreachable"}:
        raise ValueError("query must be graph, path, cycles, affected, or unreachable")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer in [1, {MAX_TIMEOUT_SECONDS}]")
    if query == "path":
        if not isinstance(sources, list) or len(sources) != 1 or target is None:
            raise ValueError("path query requires exactly one source and a target")
    elif target is not None:
        raise ValueError("target is only valid for path queries")
    elif query in {"affected", "unreachable"} and (not isinstance(sources, list) or not sources):
        raise ValueError(f"{query} query requires at least one source path")
    elif query in {"graph", "cycles"} and sources is not None:
        raise ValueError(f"{query} query does not accept sources")
    parsed_sources = tuple(_relative_path(item, "sources entry").as_posix() for item in (sources or []))
    parsed_target = _relative_path(target, "target").as_posix() if target is not None else None
    started = time.monotonic()
    deadline = started + timeout_seconds
    try:
        source, relative_root = _source_directory(root, path)
    except (ValueError, OSError) as exc:
        return {"operation": "analyze_dependencies", "status": "UNSUPPORTED", "query": query,
                "path": path if isinstance(path, str) else "", "detail": str(exc),
                "runtime_absence_proven": False}
    base: dict[str, Any] = {
        "operation": "analyze_dependencies",
        "status": "UNKNOWN",
        "query": query,
        "path": relative_root,
        "backend": f"dependency-cruiser/{DEPENDENCY_CRUISER_VERSION}",
        "model_scope": _MODEL_SCOPE,
        "runtime_absence_proven": False,
    }
    try:
        with tempfile.TemporaryDirectory(prefix="vkit-dependencies-") as temporary:
            workspace = Path(temporary)
            snapshot = workspace / "snapshot"
            snapshot.mkdir()
            try:
                source_digest, files = _snapshot(source, snapshot, deadline)
            except ValueError as exc:
                return {**base, "status": "UNSUPPORTED", "detail": str(exc)}
            base["source_identity"] = {"sha256": source_digest, "file_count": len(files)}
            prefixed_files = tuple(f"{relative_root}/{item}" if relative_root != "." else item for item in files)
            valid_files = set(prefixed_files)
            for item in parsed_sources + ((parsed_target,) if parsed_target else ()):
                if Path(item).suffix.lower() not in _SOURCE_EXTENSIONS or item not in valid_files:
                    return {**base, "status": "UNSUPPORTED",
                            "detail": f"query path {item!r} is outside the captured JavaScript/TypeScript source subset"}

            runtime = pinned_node_helper(_HELPER_ENV, _HELPER_SHA_ENV, _LOCK_ENV, _LOCK_SHA_ENV)
            if runtime is None:
                return {**base, "status": "UNAVAILABLE", "backend_identity": None,
                        "detail": "the configured helper or pinned package lock is unavailable or failed its SHA-256 check"}
            helper, helper_digest, lockfile, lock_digest, node = runtime
            base["backend_identity"] = {
                "dependency_cruiser": DEPENDENCY_CRUISER_VERSION,
                "typescript": TYPESCRIPT_VERSION,
                "node": NODE_VERSION,
                "helper_sha256": helper_digest,
                "package_lock_sha256": lock_digest,
            }
            env = _request_environment()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                version = subprocess.run([str(node), "--version"], cwd=workspace, env=env, capture_output=True,
                                         timeout=remaining, check=False)
            except OSError:
                return {**base, "status": "UNAVAILABLE", "detail": "the configured Node executable could not be started"}
            if version.returncode != 0 or version.stdout.decode("utf-8", "replace").strip() != NODE_VERSION:
                return {**base, "status": "UNAVAILABLE", "detail": "the configured Node executable is not v24.16.0"}
            request = json.dumps({"protocol_version": _PROTOCOL_VERSION, "snapshot": "snapshot"},
                                 separators=(",", ":")).encode("utf-8")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                result = subprocess.run([str(node), str(helper)], input=request, cwd=workspace, env=env,
                                        capture_output=True, timeout=remaining, check=False)
            except OSError:
                return {**base, "status": "UNAVAILABLE", "detail": "the trusted dependency helper could not be started"}
            if time.monotonic() >= deadline:
                raise TimeoutError
            if result.returncode != 0 or len(result.stdout) > MAX_OUTPUT_BYTES:
                return {**base, "status": "UNKNOWN", "detail": "the dependency helper failed or exceeded the output limit"}
            try:
                response = json.loads(result.stdout.decode("utf-8"), object_pairs_hook=_object,
                                      parse_constant=_reject_constant)
                graph, unresolved, external_dependencies, dynamic, extraction_complete, incomplete_reasons \
                    = _parse_backend(response, files)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
                return {**base, "status": "UNKNOWN", "detail": "the dependency helper returned malformed or incomplete graph data"}
            except RuntimeError as exc:
                return {**base, "status": "UNAVAILABLE", "detail": str(exc)}
            graph = _with_repository_paths(graph, relative_root)
            unresolved = tuple({**item, "source": f"{relative_root}/{item['source']}" if relative_root != "."
                                else item["source"]} for item in unresolved)
            external_dependencies = tuple({**item, "source": f"{relative_root}/{item['source']}"
                                           if relative_root != "." else item["source"]
                                           } for item in external_dependencies)
            dynamic = tuple({**item, "source": f"{relative_root}/{item['source']}" if relative_root != "."
                             else item["source"],
                             "target": f"{relative_root}/{item['target']}"
                             if relative_root != "." and item["target"] else item["target"]}
                            for item in dynamic)
            incomplete_local = bool(unresolved)
            complete = extraction_complete
            if not complete or incomplete_local:
                return {**base, "status": "UNKNOWN", "partial_graph": {"nodes": list(graph.nodes),
                        "edges": [edge.as_dict() for edge in graph.edges]},
                        "unresolved_dependencies": list(unresolved[:500]),
                        "external_dependencies": list(external_dependencies[:500]),
                        "dynamic_dependencies": list(dynamic[:500]),
                        "incomplete_reasons": list(incomplete_reasons[:100]),
                        "detail": "the static graph is incomplete because a local import or parser result could not be resolved"}
            query_paths = parsed_sources + ((parsed_target,) if parsed_target else ())
            if any(item not in graph.nodes for item in query_paths):
                return {**base, "status": "UNSUPPORTED",
                        "detail": "a query path is not a node in the captured dependency graph"}
            output_edges = [edge.as_dict() for edge in graph.edges]
            result_data: dict[str, Any]
            if query == "graph":
                result_data = {"nodes": list(graph.nodes), "edges": output_edges}
            elif query == "path":
                start = parsed_sources[0]
                end = parsed_target
                result_data = {"source": start, "target": end, "witness": list(_path(graph, start, end) or ())}
                if not result_data["witness"]:
                    result_data["witness"] = None
            elif query == "cycles":
                result_data = {"cycles": [list(component) for component in _components(graph)]}
            elif query == "affected":
                starts = parsed_sources
                result_data = {"changed": list(sorted(starts)), "affected": list(_reachable(starts, graph.reverse))}
            else:
                starts = parsed_sources
                reachable = set(_reachable(starts, graph.adjacency))
                result_data = {"entrypoints": list(sorted(starts)), "reachable": sorted(reachable),
                               "unreachable": [node for node in graph.nodes if node not in reachable]}
            response_data = {**base, "status": "ANALYZED",
                    "graph": {"node_count": len(graph.nodes), "edge_count": len(graph.edges)},
                    "unresolved_dependencies": list(unresolved[:500]),
                    "external_dependencies": list(external_dependencies[:500]),
                    "dynamic_dependencies": list(dynamic[:500])}
            response_data["result"] = result_data
            return response_data
    except (TimeoutError, subprocess.TimeoutExpired):
        return {**base, "status": "UNKNOWN", "detail": "dependency analysis exceeded its total time budget"}
