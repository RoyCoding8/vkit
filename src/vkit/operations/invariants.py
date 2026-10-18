"""Discover sample-supported relations with Daikon and exact integer replay."""
from __future__ import annotations

import ast
import hashlib
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from ..proc import run
from ._data import read_json

JAR_SHA256 = "b795a58df12d0c9a7049dca9a4dc3d922cbfc215b8099502747fa09d024fbe32"
VERSION = "5.9.0"
MAX_BYTES = 2_000_000
MAX_ROWS = 2_000
MAX_CANDIDATES = 512
MAX_REPLAY_WORK = 2_000_000
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
MODEL_SCOPE = (
    "Relations supported by every supplied observation at one program point, or by every supplied "
    "consecutive pair within one trace and point. Observation identities and values are caller data. "
    "No assertion that traces came from an application, are representative, or prove a universal invariant."
)


class _Limit(ValueError):
    pass


def _validate(raw: Any) -> tuple[dict[str, str], list[dict[str, Any]], str]:
    if (not isinstance(raw, dict) or set(raw) != {"version", "variables", "observations", "profile"}
            or type(raw["version"]) is not int or raw["version"] != 1
            or raw["profile"] not in {"states", "consecutive"}):
        raise ValueError("bundle needs version 1, variables, observations and states/consecutive profile")
    variables = raw["variables"]
    maximum = 4 if raw["profile"] == "consecutive" else 8
    if (not isinstance(variables, dict) or not 1 <= len(variables) <= maximum
            or any(not isinstance(name, str) or not NAME.fullmatch(name)
                   or kind not in {"int", "bool"} for name, kind in variables.items())):
        raise ValueError(f"variables must declare one to {maximum} distinct int/bool names")
    rows = raw["observations"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise ValueError("observations must contain one to 2000 rows")
    previous = {}
    points = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"trace_id", "point_id", "step", "values"}:
            raise ValueError("each observation needs trace_id, point_id, step and values")
        if any(not isinstance(row[key], str) or not NAME.fullmatch(row[key]) for key in ("trace_id", "point_id")):
            raise ValueError("trace and point identifiers must be short ASCII identifiers")
        key = (row["trace_id"], row["point_id"])
        if type(row["step"]) is not int or not 0 <= row["step"] < 2**63 or row["step"] <= previous.get(key, -1):
            raise ValueError("steps must strictly increase within each trace and point")
        previous[key] = row["step"]
        points.add(row["point_id"])
        values = row["values"]
        if not isinstance(values, dict) or set(values) != set(variables):
            raise ValueError("observation values must exactly match declared variables")
        for name, kind in variables.items():
            if type(values[name]) is not (int if kind == "int" else bool):
                raise ValueError(f"observation {name!r} has the wrong scalar type")
            if kind == "int" and not -(2**63) <= values[name] < 2**63:
                raise ValueError("observed integers must fit signed 64 bits")
    if len(points) > 16:
        raise ValueError("observations exceed 16 program points")
    return variables, rows, raw["profile"]


def _groups(variables: dict[str, str], rows: list[dict[str, Any]], profile: str):
    names = list(variables)
    slots = [f"v{index}" for index in range(len(names))]
    labels = dict(zip(slots, names))
    if profile == "consecutive":
        labels = {f"{side}{index}": f"{word}.{name}" for side, word in (("b", "before"), ("a", "after"))
                  for index, name in enumerate(names)}
    groups = {}
    previous = {}
    for index, row in enumerate(rows):
        key = (row["trace_id"], row["point_id"])
        if profile == "consecutive":
            if key not in previous:
                previous[key] = (index, row)
                continue
            old_index, old = previous[key]
            values = {f"b{i}": int(old["values"][name]) for i, name in enumerate(names)}
            values.update({f"a{i}": int(row["values"][name]) for i, name in enumerate(names)})
            identity = {"before_row": old_index, "after_row": index, "trace_id": row["trace_id"]}
        else:
            values = {slot: int(row["values"][name]) for slot, name in zip(slots, names)}
            identity = {"row": index, "trace_id": row["trace_id"]}
        groups.setdefault(row["point_id"], []).append((values, identity))
        previous[key] = (index, row)
    if not groups:
        raise ValueError("consecutive profile needs at least one same-trace, same-point observation pair")
    return labels, groups


def _trace(labels, groups):
    lines = ["decl-version 2.0", "var-comparability implicit", ""]
    points = {f"Vkit.point{index}:::POINT": point for index, point in enumerate(sorted(groups))}
    for generated, point in points.items():
        lines.extend((f"ppt {generated}", "ppt-type point"))
        for name in labels:
            lines.extend((f"variable {name}", "  var-kind variable", "  dec-type int",
                          "  rep-type int", "  comparability 1"))
        lines.append("")
        for values, _ in groups[point]:
            lines.append(generated)
            for name in labels:
                lines.extend((name, str(values[name]), "1"))
            lines.append("")
    return "\n".join(lines), points


def _expression(text: str, names: set[str]) -> ast.expr:
    if len(text) > 4_096:
        raise ValueError("candidate exceeds expression byte limit")
    expression = ast.parse(text, mode="eval").body
    allowed = (ast.Expression, ast.Name, ast.Load, ast.Constant, ast.BinOp, ast.UnaryOp,
               ast.Compare, ast.Add, ast.Sub, ast.Mult, ast.USub, ast.UAdd,
               ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)
    nodes = list(ast.walk(expression))
    if len(nodes) > 128 or not isinstance(expression, ast.Compare):
        raise ValueError("candidate is outside the integer comparison profile")
    for node in nodes:
        if not isinstance(node, allowed):
            raise ValueError("candidate uses an unsupported operator")
        if isinstance(node, ast.Name) and node.id not in names:
            raise ValueError("candidate names an unknown variable")
        if isinstance(node, ast.Constant) and (type(node.value) is not int or node.value.bit_length() > 128):
            raise ValueError("candidate literal is not a bounded integer")
    return expression


def _evaluate(node: ast.expr, values: dict[str, int]) -> int | bool:
    if isinstance(node, ast.Name):
        return values[node.id]
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, values)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp):
        a, b = _evaluate(node.left, values), _evaluate(node.right, values)
        return a + b if isinstance(node.op, ast.Add) else a - b if isinstance(node.op, ast.Sub) else a * b
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, values)
        for operator, right_node in zip(node.ops, node.comparators):
            right = _evaluate(right_node, values)
            accepted = (left == right if isinstance(operator, ast.Eq) else left != right if isinstance(operator, ast.NotEq)
                        else left < right if isinstance(operator, ast.Lt) else left <= right if isinstance(operator, ast.LtE)
                        else left > right if isinstance(operator, ast.Gt) else left >= right)
            if not accepted:
                return False
            left = right
        return True
    raise ValueError("invalid candidate expression")


def _candidates(output: str, points: dict[str, str]):
    current = None
    for line in output.splitlines():
        text = line.strip()
        if text in points:
            current = points[text]
        elif text == "Exiting Daikon.":
            current = None
        elif current is not None and text and not set(text) <= {"="}:
            yield current, text


def infer_trace_relations(root: Path, path: str, timeout_seconds: int = 20) -> dict[str, Any]:
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise ValueError("timeout_seconds must be from 1 to 60")
    base = {"operation": "infer_trace_relations", "path": path, "model_scope": MODEL_SCOPE,
            "backend": {"name": "Daikon", "version": VERSION, "jar_sha256": JAR_SHA256}}
    deadline = time.monotonic() + timeout_seconds
    try:
        raw, digest = read_json(root, path, max_bytes=MAX_BYTES, reject_symlinks=True)
        variables, rows, profile = _validate(raw)
        labels, groups = _groups(variables, rows, profile)
        trace, points = _trace(labels, groups)
        base.update(input_sha256=digest, profile=profile, variable_mapping=labels, observations=len(rows))
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        return {**base, "status": "UNSUPPORTED", "reason": str(exc)}
    try:
        jar = Path(os.environ["VKIT_DAIKON_JAR"]).resolve(strict=True)
        with jar.open("rb") as source:
            content = source.read(10_000_001)
        if len(content) > 10_000_000 or hashlib.sha256(content).hexdigest() != JAR_SHA256:
            raise ValueError("Daikon jar does not match the fixed release identity")
    except (OSError, ValueError, KeyError) as exc:
        return {**base, "status": "UNAVAILABLE", "reason": str(exc)}
    try:
        with tempfile.TemporaryDirectory(prefix="vkit-invariants-") as temporary:
            directory = Path(temporary)
            (directory / "samples.dtrace").write_text(trace, encoding="utf-8")
            completed = run(["java", "-Xmx512m", "-cp", str(jar), "daikon.Daikon", "--no_show_progress",
                             "--nohierarchy", "--format", "daikon", "-o", str(directory / "samples.inv.gz"),
                             str(directory / "samples.dtrace")], cwd=directory, stdout_path=directory / "stdout",
                            stderr_path=directory / "stderr", timeout_seconds=max(0.001, deadline - time.monotonic()))
            if completed.launch_error:
                return {**base, "status": "UNAVAILABLE", "reason": completed.launch_error}
            if completed.code != 0 or completed.timed_out or completed.cancelled:
                return {**base, "status": "UNKNOWN", "reason": "Daikon did not complete its bounded campaign"}
            with (directory / "stdout").open("rb") as source:
                output = source.read(MAX_BYTES + 1)
            if len(output) > MAX_BYTES:
                raise _Limit("Daikon output exceeds byte limit")
        if time.monotonic() >= deadline:
            raise _Limit("discovery exceeded the shared deadline")
        text = output.decode("utf-8")
        if not text.startswith(f"Daikon version {VERSION},") or "Exiting Daikon." not in text:
            return {**base, "status": "UNKNOWN", "reason": "Daikon output has no complete release-identified result"}
        accepted, rejected, unsupported = [], [], []
        work = 0
        for index, (point, candidate) in enumerate(_candidates(text, points)):
            if index >= MAX_CANDIDATES or time.monotonic() >= deadline:
                raise _Limit("candidate count or shared deadline exhausted")
            try:
                expression = _expression(candidate, set(labels))
            except (SyntaxError, ValueError, RecursionError) as exc:
                unsupported.append({"point_id": point, "expression": candidate, "reason": str(exc)})
                continue
            work += sum(1 for _ in ast.walk(expression)) * len(groups[point])
            if work > MAX_REPLAY_WORK:
                raise _Limit("independent candidate replay work limit exhausted")
            contradictory = None
            for values, identity in groups[point]:
                if time.monotonic() >= deadline:
                    raise _Limit("independent replay exceeded shared deadline")
                if not _evaluate(expression, values):
                    contradictory = {**identity, "values": values}
                    break
            item = {"point_id": point, "expression": candidate, "samples": len(groups[point]),
                    "traces": len({identity["trace_id"] for _, identity in groups[point]})}
            if contradictory is None:
                accepted.append({**item, "status": "OBSERVED_IN_SAMPLES"})
            else:
                rejected.append({**item, "status": "COUNTEROBSERVED", "counterexample": contradictory})
        return {**base, "status": "ANALYZED_SAMPLES", "candidates": accepted, "counterobserved": rejected,
                "unverified_engine_candidates": unsupported, "engine_output_sha256": hashlib.sha256(output).hexdigest()}
    except (OSError, ValueError, RecursionError) as exc:
        return {**base, "status": "UNKNOWN", "reason": str(exc)}
