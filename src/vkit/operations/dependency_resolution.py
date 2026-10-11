"""Resolve Python requirements against a captured, finite candidate universe."""
from __future__ import annotations

import json
import subprocess
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ._data import encode_json, read_json

BACKEND = "resolvelib"
BACKEND_VERSION = "1.2.1"
PACKAGING_VERSION = "26.3"
MAX_BUNDLE_BYTES = 1_000_000
MAX_ROOTS = 256
MAX_CANDIDATES = 2_000
MAX_REQUIREMENTS = 4_000
MAX_REQUIREMENT_BYTES = 2_048
MAX_IDENTITY_BYTES = 256
MAX_ENVIRONMENT_VALUE_BYTES = 256
MAX_TIMEOUT_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 20
MAX_ROUNDS = 200
MAX_OUTPUT_BYTES = 2_000_000

MODEL_SCOPE = (
    "PEP 440/508 resolution over only the captured version-1 candidate universe and explicit marker environment. "
    "No index access, package installation, candidate code, or environment discovery occurs. A conflict is not a "
    "minimal unsatisfiable core. Pre-releases are eligible only when every active requirement for that candidate "
    "explicitly includes a pre-release specifier."
)

Status = Literal["RESOLVED", "CONFLICT", "UNKNOWN", "UNSUPPORTED", "UNAVAILABLE"]

_ENVIRONMENT_FIELDS = (
    "implementation_name",
    "implementation_version",
    "os_name",
    "platform_machine",
    "platform_release",
    "platform_system",
    "platform_version",
    "platform_python_implementation",
    "python_full_version",
    "python_version",
    "sys_platform",
)


def _result(status: Status, bundle_sha256: str | None, *, reason: str | None = None,
            backend_version: str | None = BACKEND_VERSION,
            packaging_version: str | None = PACKAGING_VERSION,
            environment: dict[str, str] | None = None,
            roots: list[str] | None = None,
            resolution: list[dict[str, str | None]] | None = None,
            edges: list[dict[str, str | None]] | None = None,
            causes: list[dict[str, str | None]] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "status": status,
        "backend": BACKEND,
        "backend_version": backend_version,
        "packaging_version": packaging_version,
        "bundle_sha256": bundle_sha256,
        "target_environment": environment,
        "prerelease_policy": "explicit_in_every_active_requirement",
        "model_scope": MODEL_SCOPE,
        "roots": roots or [],
        "resolution": resolution or [],
        "edges": edges or [],
        "causes": causes or [],
    }
    if reason is not None:
        value["reason"] = reason
    return value


def resolve_dependencies(root: Path, path: str,
                         timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Resolve one strict JSON bundle in a timed isolated child process."""
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer from 1 to {MAX_TIMEOUT_SECONDS}")
    try:
        bundle, digest = read_json(root, path, max_bytes=MAX_BUNDLE_BYTES)
    except ValueError as exc:
        return _result("UNSUPPORTED", None, reason=str(exc))

    try:
        request = json.dumps({"bundle": bundle, "bundle_sha256": digest},
                             ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        return _result("UNSUPPORTED", digest, reason=f"bundle is outside the JSON model: {exc}")

    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-m", "vkit.operations.dependency_resolution", "--worker"],
            input=request,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _result("UNKNOWN", digest, reason=f"resolution exceeded {timeout_seconds} seconds")
    except OSError as exc:
        return _result("UNAVAILABLE", digest, reason=f"cannot start isolated resolver: {exc}")

    if completed.returncode != 0:
        return _result("UNKNOWN", digest, reason="isolated resolver exited without a result")
    if len(completed.stdout.encode("utf-8", errors="replace")) > MAX_OUTPUT_BYTES:
        return _result("UNKNOWN", digest, reason="isolated resolver output exceeded its byte limit")
    try:
        result = json.loads(completed.stdout)
    except (json.JSONDecodeError, RecursionError):
        return _result("UNKNOWN", digest, reason="isolated resolver returned malformed JSON")
    if (not isinstance(result, dict)
            or result.get("status") not in {"RESOLVED", "CONFLICT", "UNKNOWN", "UNSUPPORTED", "UNAVAILABLE"}
            or result.get("bundle_sha256") != digest):
        return _result("UNKNOWN", digest, reason="isolated resolver returned an invalid result envelope")
    return result


@dataclass(frozen=True, eq=False)
class _Candidate:
    name: str
    version: Any
    requires_python: Any
    dependencies: tuple[Any, ...]

    @property
    def identity(self) -> str:
        return f"{self.name}=={self.version}"

    def __hash__(self) -> int:
        return hash((self.name, self.version))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Candidate) and (self.name, self.version) == (other.name, other.version)


def _worker_result(bundle: object, bundle_sha256: str) -> dict[str, Any]:
    try:
        from importlib.metadata import PackageNotFoundError, version
        import packaging
        import resolvelib
        from packaging.markers import Marker, UndefinedComparison, UndefinedEnvironmentName
        from packaging.requirements import InvalidRequirement, Requirement
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
        from packaging.utils import InvalidName, canonicalize_name
        from packaging.version import InvalidVersion, Version
        from resolvelib import BaseReporter, Resolver
        from resolvelib.providers import AbstractProvider
        from resolvelib.resolvers import ResolutionImpossible, ResolutionTooDeep
    except ImportError as exc:
        return _result("UNAVAILABLE", bundle_sha256, reason=f"resolver dependency is unavailable: {exc}",
                       backend_version=None, packaging_version=None)

    try:
        installed_resolver = version(BACKEND)
        installed_packaging = version("packaging")
    except PackageNotFoundError as exc:
        return _result("UNAVAILABLE", bundle_sha256, reason=f"required distribution {exc.name!r} is not installed",
                       backend_version=None, packaging_version=None)
    if installed_resolver != BACKEND_VERSION or installed_packaging != PACKAGING_VERSION:
        return _result(
            "UNAVAILABLE", bundle_sha256,
            reason=(f"requires {BACKEND} {BACKEND_VERSION} and packaging {PACKAGING_VERSION}; "
                    f"found {installed_resolver} and {installed_packaging}"),
            backend_version=installed_resolver,
            packaging_version=installed_packaging,
        )

    if (not isinstance(bundle, dict) or set(bundle) != {"version", "roots", "candidates", "environment"}
            or type(bundle.get("version")) is not int or bundle["version"] != 1):
        return _result("UNSUPPORTED", bundle_sha256,
                       reason="bundle must contain exactly version, roots, candidates and environment")

    raw_environment = bundle["environment"]
    if not isinstance(raw_environment, dict) or set(raw_environment) != set(_ENVIRONMENT_FIELDS):
        return _result("UNSUPPORTED", bundle_sha256,
                       reason="environment must provide every PEP 508 marker field except extra")
    environment: dict[str, str] = {}
    try:
        for field in _ENVIRONMENT_FIELDS:
            value = raw_environment[field]
            if not isinstance(value, str) or not value or "\0" in value:
                raise ValueError(f"environment.{field} must be a non-empty string without NUL")
            if len(value.encode("utf-8")) > MAX_ENVIRONMENT_VALUE_BYTES:
                raise ValueError(f"environment.{field} exceeds {MAX_ENVIRONMENT_VALUE_BYTES} UTF-8 bytes")
            environment[field] = value
        target_python = Version(environment["python_full_version"])
        python_version = Version(environment["python_version"])
        if python_version.release[:2] != target_python.release[:2]:
            raise ValueError("python_version must match the major.minor of python_full_version")
    except (UnicodeEncodeError, InvalidVersion, ValueError) as exc:
        return _result("UNSUPPORTED", bundle_sha256, reason=f"invalid target environment: {exc}")
    marker_environment = {**environment, "extra": ""}

    def normalized_name(requirement: Any) -> str:
        return canonicalize_name(requirement.name, validate=True)

    def parse_requirement(raw: object, label: str) -> Any:
        if not isinstance(raw, str) or not raw or "\0" in raw:
            raise ValueError(f"{label} must be a non-empty requirement string without NUL")
        if len(raw.encode("utf-8")) > MAX_REQUIREMENT_BYTES:
            raise ValueError(f"{label} exceeds {MAX_REQUIREMENT_BYTES} UTF-8 bytes")
        try:
            requirement = Requirement(raw)
        except (InvalidRequirement, UnicodeEncodeError) as exc:
            raise ValueError(f"{label} is not a valid PEP 508 requirement") from exc
        if requirement.extras:
            raise ValueError(f"{label} requests extras, which are outside this profile")
        if requirement.url is not None:
            raise ValueError(f"{label} uses a direct URL, which is outside this profile")
        if requirement.marker is not None and _uses_extra_marker(str(requirement.marker)):
            raise ValueError(f"{label} uses the extra marker, which is outside this profile")
        if requirement.marker is not None:
            try:
                requirement.marker.evaluate(environment=marker_environment, context="metadata")
            except (UndefinedComparison, UndefinedEnvironmentName, ValueError) as exc:
                raise ValueError(f"{label} has a marker outside the supported environment: {exc}") from exc
        return requirement

    roots_raw = bundle["roots"]
    candidates_raw = bundle["candidates"]
    if not isinstance(roots_raw, list) or len(roots_raw) > MAX_ROOTS:
        return _result("UNSUPPORTED", bundle_sha256, environment=environment,
                       reason=f"roots must be an array with at most {MAX_ROOTS} entries")
    if not isinstance(candidates_raw, list) or len(candidates_raw) > MAX_CANDIDATES:
        return _result("UNSUPPORTED", bundle_sha256, environment=environment,
                       reason=f"candidates must be an array with at most {MAX_CANDIDATES} entries")

    try:
        roots_all = [parse_requirement(value, f"roots[{index}]") for index, value in enumerate(roots_raw)]
        roots = [req for req in roots_all if req.marker is None or req.marker.evaluate(
            environment=marker_environment, context="metadata")]
        candidates: dict[str, list[_Candidate]] = defaultdict(list)
        seen: set[tuple[str, Version]] = set()
        total_requirements = len(roots_raw)
        for index, raw in enumerate(candidates_raw):
            label = f"candidates[{index}]"
            allowed = {"name", "version", "requires_dist", "requires_python"}
            if not isinstance(raw, dict) or set(raw) - allowed or not {"name", "version", "requires_dist"} <= set(raw):
                raise ValueError(f"{label} must contain name, version and requires_dist, with optional requires_python")
            name_raw = raw["name"]
            if not isinstance(name_raw, str) or len(name_raw.encode("utf-8")) > MAX_IDENTITY_BYTES:
                raise ValueError(f"{label}.name must be a package name of at most {MAX_IDENTITY_BYTES} UTF-8 bytes")
            try:
                name = canonicalize_name(name_raw, validate=True)
            except (InvalidName, TypeError) as exc:
                raise ValueError(f"{label}.name is not a valid package name") from exc
            version_raw = raw["version"]
            if not isinstance(version_raw, str) or len(version_raw.encode("utf-8")) > MAX_IDENTITY_BYTES:
                raise ValueError(f"{label}.version must be a PEP 440 version of at most {MAX_IDENTITY_BYTES} UTF-8 bytes")
            try:
                candidate_version = Version(version_raw)
            except InvalidVersion as exc:
                raise ValueError(f"{label}.version is not a valid PEP 440 version") from exc
            identity = (name, candidate_version)
            if identity in seen:
                raise ValueError(f"candidate identities must be unique after name/version normalization: {name}=={candidate_version}")
            seen.add(identity)

            requires_python_raw = raw.get("requires_python")
            if "requires_python" not in raw:
                requires_python = SpecifierSet()
            elif not isinstance(requires_python_raw, str):
                raise ValueError(f"{label}.requires_python must be a PEP 440 specifier string")
            else:
                try:
                    requires_python = SpecifierSet(requires_python_raw)
                except InvalidSpecifier as exc:
                    raise ValueError(f"{label}.requires_python is not a valid PEP 440 specifier") from exc
            requirements_raw = raw["requires_dist"]
            if not isinstance(requirements_raw, list):
                raise ValueError(f"{label}.requires_dist must be an array of PEP 508 requirement strings")
            total_requirements += len(requirements_raw)
            if total_requirements > MAX_REQUIREMENTS:
                raise ValueError(f"bundle must contain at most {MAX_REQUIREMENTS} root and candidate requirements")
            dependencies_all = tuple(parse_requirement(value, f"{label}.requires_dist[{dep_index}]")
                                     for dep_index, value in enumerate(requirements_raw))
            dependencies = tuple(req for req in dependencies_all if req.marker is None or req.marker.evaluate(
                environment=marker_environment, context="metadata"))
            candidates[name].append(_Candidate(name, candidate_version, requires_python, dependencies))
    except (ValueError, TypeError, RecursionError) as exc:
        return _result("UNSUPPORTED", bundle_sha256, environment=environment, reason=str(exc))

    for choices in candidates.values():
        choices.sort(key=lambda candidate: (not candidate.version.is_prerelease, candidate.version), reverse=True)

    def candidate_matches(candidate: _Candidate, requirements: tuple[Any, ...]) -> bool:
        if not candidate.requires_python.contains(str(target_python), prereleases=True):
            return False
        if candidate.version.is_prerelease and not all(req.specifier.prereleases is True for req in requirements):
            return False
        return all(req.specifier.contains(candidate.version, prereleases=True) for req in requirements)

    class Provider(AbstractProvider[Any, _Candidate, str]):
        def identify(self, requirement_or_candidate: Any) -> str:
            return requirement_or_candidate.name if isinstance(requirement_or_candidate, _Candidate) \
                else normalized_name(requirement_or_candidate)

        def get_preference(self, identifier: str, resolutions: Any, candidates: Any,
                           information: Any, backtrack_causes: Any) -> str:
            return identifier

        def find_matches(self, identifier: str, requirements: Any, incompatibilities: Any) -> list[_Candidate]:
            required = tuple(requirements[identifier])
            rejected = set(incompatibilities[identifier])
            return [candidate for candidate in candidates.get(identifier, ())
                    if candidate not in rejected and candidate_matches(candidate, required)]

        def is_satisfied_by(self, requirement: Any, candidate: _Candidate) -> bool:
            return candidate_matches(candidate, (requirement,))

        def get_dependencies(self, candidate: _Candidate) -> tuple[Any, ...]:
            return candidate.dependencies

    resolver = Resolver(Provider(), BaseReporter())
    try:
        state = resolver.resolve(roots, max_rounds=MAX_ROUNDS)
    except ResolutionImpossible as exc:
        causes = []
        for cause in exc.causes:
            parent = cause.parent
            causes.append({
                "requirement": str(cause.requirement),
                "parent": None if parent is None else parent.identity,
            })
        causes = sorted({(item["requirement"], item["parent"]): item for item in causes}.values(),
                        key=lambda item: (item["requirement"], item["parent"] or ""))
        return _result("CONFLICT", bundle_sha256, environment=environment,
                       roots=sorted(str(req) for req in roots), causes=causes,
                       reason="no assignment exists within the supplied finite candidate universe")
    except ResolutionTooDeep:
        return _result("UNKNOWN", bundle_sha256, environment=environment,
                       roots=sorted(str(req) for req in roots),
                       reason=f"resolver exceeded its fixed {MAX_ROUNDS}-round limit")
    except (RecursionError, OverflowError):
        return _result("UNKNOWN", bundle_sha256, environment=environment,
                       roots=sorted(str(req) for req in roots), reason="resolver exceeded an internal resource limit")
    except Exception as exc:
        return _result("UNKNOWN", bundle_sha256, environment=environment,
                       roots=sorted(str(req) for req in roots),
                       reason=f"resolver failed without a classification: {type(exc).__name__}")

    assignment = state.mapping
    try:
        resolution, edges = _validate_assignment(roots, assignment, candidate_matches, normalized_name)
    except (KeyError, ValueError, TypeError):
        return _result("UNKNOWN", bundle_sha256, environment=environment,
                       roots=sorted(str(req) for req in roots),
                       reason="resolver assignment failed independent dependency validation")
    return _result("RESOLVED", bundle_sha256, environment=environment,
                   roots=sorted(str(req) for req in roots), resolution=resolution, edges=edges)


def _uses_extra_marker(marker: str) -> bool:
    import re

    variables = re.sub(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"", "", marker)
    return re.search(r"(?<![A-Za-z0-9_])extra(?![A-Za-z0-9_])", variables) is not None


def _validate_assignment(roots: list[Any], assignment: Any, candidate_matches: Any,
                         normalized_name: Any) -> tuple[list[dict[str, str | None]], list[dict[str, str | None]]]:
    chosen = {name: candidate for name, candidate in assignment.items()}
    required_by_name: dict[str, list[Any]] = defaultdict(list)
    queued = deque((None, requirement) for requirement in roots)
    expanded: set[str] = set()
    reached: set[str] = set()
    edges: list[dict[str, str | None]] = []

    while queued:
        parent, requirement = queued.popleft()
        name = normalized_name(requirement)
        candidate = chosen.get(name)
        if candidate is None or candidate.name != name:
            raise ValueError("reachable requirement has no matching candidate")
        required_by_name[name].append(requirement)
        reached.add(name)
        edges.append({"from": parent, "requirement": str(requirement)})
        if name in expanded:
            continue
        expanded.add(name)
        for dependency in candidate.dependencies:
            queued.append((candidate.identity, dependency))

    if reached != set(chosen):
        raise ValueError("assignment contains an unreachable candidate")
    for name, candidate in chosen.items():
        if not candidate_matches(candidate, tuple(required_by_name[name])):
            raise ValueError("selected candidate violates an active requirement")

    resolution = [{"name": name, "version": str(candidate.version),
                   "requires_python": str(candidate.requires_python) or None}
                  for name, candidate in sorted(chosen.items())]
    edges.sort(key=lambda edge: (edge["from"] or "", edge["requirement"] or ""))
    return resolution, edges


def _worker_main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict) or set(request) != {"bundle", "bundle_sha256"}:
            return 1
        result = _worker_result(request["bundle"], request["bundle_sha256"])
        try:
            output = encode_json(result, max_bytes=MAX_OUTPUT_BYTES)
        except ValueError:
            output = encode_json(_result("UNKNOWN", request["bundle_sha256"],
                                         reason="isolated resolver output exceeded its byte limit"),
                                 max_bytes=MAX_OUTPUT_BYTES)
        print(output)
        return 0
    except (KeyError, TypeError, ValueError, RecursionError):
        return 1


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    raise SystemExit(_worker_main())
