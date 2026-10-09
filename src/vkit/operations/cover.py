"""Minimum-cost set cover over a bounded repository JSON matrix."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

BACKEND = "ortools"
BACKEND_VERSION = "9.15.6755"
MAX_MATRIX_BYTES = 512_000
MAX_REQUIRED = 2_000
MAX_CANDIDATES = 4_000
MAX_COVERAGE_PAIRS = 100_000
MAX_ID_BYTES = 256
# Keep every feasible objective exactly representable even through CP-SAT's float accessors.
MAX_TOTAL_COST = (1 << 53) - 1
MAX_TIMEOUT_SECONDS = 60
MODEL_SCOPE = (
    "Minimum-cost set cover for the required IDs in the supplied version-1 matrix. "
    "Candidate coverage IDs outside the required list do not affect the objective. "
    "This result describes only the supplied matrix and does not guarantee future coverage."
)

Status = Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN", "UNAVAILABLE"]


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def _valid_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(f"{label} must be a non-empty string without NUL")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must contain Unicode scalar values") from exc
    if size > MAX_ID_BYTES:
        raise ValueError(f"{label} must be at most {MAX_ID_BYTES} UTF-8 bytes")
    return value


def _read_matrix(root: Path, path: str) -> tuple[dict[str, Any], str]:
    if (not isinstance(path, str) or not path or Path(path).is_absolute()
            or PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive):
        raise ValueError("path must be a non-empty repository-relative path")
    parts = path.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("path must not contain empty, '.' or '..' segments")
    try:
        resolved_root = Path(root).resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValueError("root must identify a directory")
        target = (resolved_root.joinpath(*parts)).resolve(strict=True)
        if not target.is_relative_to(resolved_root):
            raise ValueError("matrix path must remain inside the repository root")
        if not target.is_file():
            raise ValueError("path must identify a regular file")
        with target.open("rb") as handle:
            raw = handle.read(MAX_MATRIX_BYTES + 1)
    except ValueError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot read repository-relative matrix {path!r}: {exc}") from exc
    if len(raw) > MAX_MATRIX_BYTES:
        raise ValueError(f"coverage matrix must be at most {MAX_MATRIX_BYTES} bytes")
    try:
        matrix = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except RecursionError as exc:
        raise ValueError("coverage matrix nesting exceeds parser limits") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"coverage matrix must be valid UTF-8 JSON: {exc}") from exc
    if not isinstance(matrix, dict) or set(matrix) != {"version", "required", "candidates"}:
        raise ValueError("matrix must contain exactly 'version', 'required' and 'candidates'")
    if type(matrix["version"]) is not int or matrix["version"] != 1:
        raise ValueError("matrix version must be the integer 1")

    required_raw = matrix["required"]
    candidates_raw = matrix["candidates"]
    if not isinstance(required_raw, list) or len(required_raw) > MAX_REQUIRED:
        raise ValueError(f"required must be an array with at most {MAX_REQUIRED} IDs")
    if not isinstance(candidates_raw, list) or len(candidates_raw) > MAX_CANDIDATES:
        raise ValueError(f"candidates must be an array with at most {MAX_CANDIDATES} entries")

    required = [_valid_id(value, "required ID") for value in required_raw]
    if len(set(required)) != len(required):
        raise ValueError("required IDs must be unique")
    candidates: list[dict[str, Any]] = []
    candidate_ids: set[str] = set()
    pair_count = 0
    total_cost = 0
    for candidate in candidates_raw:
        if not isinstance(candidate, dict) or set(candidate) != {"id", "cost", "covers"}:
            raise ValueError("each candidate must contain exactly 'id', 'cost' and 'covers'")
        candidate_id = _valid_id(candidate["id"], "candidate ID")
        if candidate_id in candidate_ids:
            raise ValueError("candidate IDs must be unique")
        candidate_ids.add(candidate_id)
        cost = candidate["cost"]
        if type(cost) is not int or cost < 0:
            raise ValueError(f"candidate {candidate_id!r} cost must be a non-negative integer")
        total_cost += cost
        if total_cost > MAX_TOTAL_COST:
            raise ValueError(f"sum of candidate costs must not exceed {MAX_TOTAL_COST}")
        covers_raw = candidate["covers"]
        if not isinstance(covers_raw, list):
            raise ValueError(f"candidate {candidate_id!r} covers must be an array of IDs")
        covers = [_valid_id(value, f"candidate {candidate_id!r} coverage ID") for value in covers_raw]
        if len(set(covers)) != len(covers):
            raise ValueError(f"candidate {candidate_id!r} coverage IDs must be unique")
        pair_count += len(covers)
        if pair_count > MAX_COVERAGE_PAIRS:
            raise ValueError(f"matrix must contain at most {MAX_COVERAGE_PAIRS} coverage pairs")
        candidates.append({"id": candidate_id, "cost": cost, "covers": covers})

    return {"required": required, "candidates": candidates}, hashlib.sha256(raw).hexdigest()


def _load_backend() -> tuple[Any | None, str | None, str | None]:
    from importlib.metadata import PackageNotFoundError, version

    try:
        installed = version(BACKEND)
    except PackageNotFoundError:
        return None, None, f"{BACKEND} {BACKEND_VERSION} is not installed"
    if installed != BACKEND_VERSION:
        return None, installed, f"requires {BACKEND} {BACKEND_VERSION}; found {installed}"
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        return None, installed, f"{BACKEND} {BACKEND_VERSION} is installed but CP-SAT cannot be imported"
    return cp_model, installed, None


def _worker(request: dict[str, Any]) -> dict[str, Any]:
    cp_model, installed, unavailable = _load_backend()
    if unavailable:
        return {"status": "UNAVAILABLE", "selected": None, "cost": None,
                "backend_version": installed, "reason": unavailable}

    required = request["required"]
    candidates = request["candidates"]
    timeout_seconds = request["timeout_seconds"]
    model = cp_model.CpModel()
    decisions = [model.NewBoolVar(f"candidate_{index}") for index in range(len(candidates))]
    coverers: dict[str, list[Any]] = {element: [] for element in required}
    for decision, candidate in zip(decisions, candidates):
        for element in candidate["covers"]:
            if element in coverers:
                coverers[element].append(decision)
    for decisions_for_element in coverers.values():
        model.AddBoolOr(decisions_for_element)
    model.Minimize(sum((candidate["cost"] * decision
                        for candidate, decision in zip(candidates, decisions)), 0))

    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    solver.parameters.max_time_in_seconds = timeout_seconds * 0.75
    solver_status = solver.Solve(model)
    status_names = {
        cp_model.OPTIMAL: "OPTIMAL",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.UNKNOWN: "UNKNOWN",
    }
    status = status_names.get(solver_status, "UNKNOWN")
    if status not in ("OPTIMAL", "FEASIBLE"):
        return {"status": status, "selected": None, "cost": None,
                "backend_version": installed, "reason": None}
    selected_indexes = [index for index, decision in enumerate(decisions) if solver.Value(decision)]
    selected = [candidates[index]["id"] for index in selected_indexes]
    cost = sum(candidates[index]["cost"] for index in selected_indexes)
    return {"status": status, "selected": selected, "cost": cost,
            "backend_version": installed, "reason": None}


def _main() -> int:
    try:
        request = json.load(sys.stdin)
        result = _worker(request)
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
        return 0
    except (IndexError, KeyError, TypeError, ValueError):
        return 1


def _unavailable(path: str, digest: str, reason: str,
                 backend_version: str | None = None) -> dict[str, Any]:
    return {
        "operation": "minimize_cover", "status": "UNAVAILABLE", "path": path,
        "source_sha256": digest, "selected": None, "total_cost": None,
        "backend": BACKEND, "backend_version": backend_version, "reason": reason,
        "model_scope": MODEL_SCOPE,
    }


def minimize_cover(root: Path, path: str, timeout_seconds: int = 10) -> dict[str, Any]:
    """Find a minimum-cost candidate subset covering the matrix's required IDs."""
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer from 1 to {MAX_TIMEOUT_SECONDS}")
    matrix, digest = _read_matrix(root, path)
    request = json.dumps(
        {**matrix, "timeout_seconds": timeout_seconds}, ensure_ascii=True, separators=(",", ":")
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--worker"],
            input=request, capture_output=True, encoding="utf-8", timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "operation": "minimize_cover", "status": "UNKNOWN", "path": path,
            "source_sha256": digest, "selected": None, "total_cost": None,
            "backend": BACKEND, "backend_version": None,
            "reason": f"isolated CP-SAT computation exceeded {timeout_seconds} seconds",
            "model_scope": MODEL_SCOPE,
        }
    except OSError as exc:
        return _unavailable(path, digest, f"cannot start isolated CP-SAT worker: {exc}")
    if completed.returncode != 0:
        return _unavailable(path, digest, "isolated CP-SAT worker exited without a result")
    try:
        result = json.loads(completed.stdout, object_pairs_hook=_object_without_duplicates,
                            parse_constant=_reject_constant)
    except (ValueError, json.JSONDecodeError):
        return _unavailable(path, digest, "isolated CP-SAT worker returned invalid JSON")
    if not isinstance(result, dict):
        return _unavailable(path, digest, "isolated CP-SAT worker returned an invalid backend version")
    status = result.get("status")
    if status == "UNAVAILABLE":
        installed = result.get("backend_version")
        return _unavailable(
            path, digest,
            str(result.get("reason") or "CP-SAT backend is unavailable"),
            installed if isinstance(installed, str) else None,
        )
    if result.get("backend_version") != BACKEND_VERSION:
        return _unavailable(path, digest, "isolated CP-SAT worker returned an invalid backend version")
    if status not in ("OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN"):
        return _unavailable(path, digest, "isolated CP-SAT worker returned an invalid status")

    selected = result.get("selected")
    total_cost = result.get("cost")
    if status in ("OPTIMAL", "FEASIBLE"):
        if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
            return _unavailable(path, digest, "isolated CP-SAT worker returned an invalid selection")
        candidate_by_id = {candidate["id"]: candidate for candidate in matrix["candidates"]}
        if len(set(selected)) != len(selected) or any(item not in candidate_by_id for item in selected):
            return _unavailable(path, digest, "isolated CP-SAT worker selected an unknown or duplicate candidate")
        selected_candidates = [candidate_by_id[item] for item in selected]
        covered = {element for candidate in selected_candidates for element in candidate["covers"]}
        computed_cost = sum(candidate["cost"] for candidate in selected_candidates)
        if not set(matrix["required"]).issubset(covered) or type(total_cost) is not int or total_cost != computed_cost:
            return _unavailable(path, digest, "isolated CP-SAT worker returned an invalid coverage or cost")
        return {
            "operation": "minimize_cover", "status": status, "path": path,
            "source_sha256": digest, "selected": selected, "total_cost": computed_cost,
            "backend": BACKEND, "backend_version": BACKEND_VERSION,
            "reason": None, "model_scope": MODEL_SCOPE,
        }
    if selected is not None or total_cost is not None:
        return _unavailable(path, digest, "isolated CP-SAT worker returned a solution for a non-solution status")
    return {
        "operation": "minimize_cover", "status": status, "path": path,
        "source_sha256": digest, "selected": None, "total_cost": None,
        "backend": BACKEND, "backend_version": BACKEND_VERSION,
        "reason": result.get("reason"), "model_scope": MODEL_SCOPE,
    }


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    raise SystemExit(_main())
