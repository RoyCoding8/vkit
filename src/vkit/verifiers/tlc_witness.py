"""Small structural helpers for TLC's native JSON counterexample format."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any


def trace_states(document: Any) -> tuple[tuple[str, ...], tuple[tuple[int, dict], ...]] | None:
    if not isinstance(document, dict) or not isinstance(document.get("vars"), list):
        return None
    variables = document["vars"]
    if (not variables or not all(isinstance(name, str) and name for name in variables)
            or len(set(variables)) != len(variables)):
        return None
    counterexample = document.get("counterexample")
    states = counterexample.get("state") if isinstance(counterexample, dict) else None
    if not isinstance(states, list) or not states:
        return None
    parsed = []
    for index, state in enumerate(states, 1):
        if (not isinstance(state, list) or len(state) != 2
                or type(state[0]) is not int or state[0] != index
                or not isinstance(state[1], dict)
                or set(state[1]) != set(variables)):
            return None
        parsed.append((state[0], state[1]))
    actions = counterexample.get("action")
    if not isinstance(actions, list) or len(actions) != len(parsed) - 1:
        return None
    for index, action in enumerate(actions):
        if (not isinstance(action, list) or len(action) != 3
                or action[0] != states[index] or action[2] != states[index + 1]
                or not isinstance(action[1], dict)):
            return None
    return tuple(variables), tuple(parsed)


def same_state_sequence(left: Any, right: Any) -> bool:
    parsed_left, parsed_right = trace_states(left), trace_states(right)
    if parsed_left is None or parsed_right is None:
        return False
    left_vars, left_states = parsed_left
    right_vars, right_states = parsed_right
    if left_vars != right_vars or len(left_states) != len(right_states):
        return False
    return all(
        _canonical_state(a[1]) == _canonical_state(b[1])
        for a, b in zip(left_states, right_states)
    )


def _canonical_state(state: dict) -> str:
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def without_internal_state(document: dict, index: int) -> dict | None:
    parsed = trace_states(document)
    if parsed is None:
        return None
    variables, states = parsed
    if index <= 1 or index >= len(states):
        return None

    original = document["counterexample"]
    kept = [state for position, state in enumerate(original["state"], 1) if position != index]
    rewritten_states = [[position, deepcopy(item[1])] for position, item in enumerate(kept, 1)]
    rewritten_actions = []
    for position in range(len(rewritten_states) - 1):
        source = rewritten_states[position]
        target = rewritten_states[position + 1]
        descriptor = {"name": "Next"}
        for action in original["action"]:
            if action[0][1] == source[1] and action[2][1] == target[1]:
                descriptor = deepcopy(action[1])
                break
        rewritten_actions.append([source, descriptor, target])

    result = deepcopy(document)
    result["vars"] = list(variables)
    result["counterexample"]["state"] = rewritten_states
    result["counterexample"]["action"] = rewritten_actions
    return result


def source_locations(document: Any, model_module: str, model_path: str,
                     source_sha256: str | None = None,
                     limit: int = 64) -> tuple[list[dict[str, Any]], bool]:
    parsed = trace_states(document)
    if parsed is None:
        return [], False
    actions = document["counterexample"]["action"]
    result = []
    seen = set()
    for action in actions:
        location = action[1].get("location")
        if not isinstance(location, dict) or location.get("module") != model_module:
            continue
        lines = (location.get("beginLine"), location.get("endLine"))
        columns = (location.get("beginColumn"), location.get("endColumn"))
        if (not all(isinstance(value, int) and not isinstance(value, bool) and value > 0
                    for value in (*lines, *columns))):
            continue
        key = (model_path, model_module, *lines, *columns)
        if key in seen:
            continue
        seen.add(key)
        if len(result) == limit:
            return result, True
        item = {
            "path": model_path,
            "module": model_module,
            "start_line": lines[0],
            "start_column": columns[0],
            "end_line": lines[1],
            "end_column": columns[1],
        }
        if source_sha256 is not None:
            item["source_sha256"] = source_sha256
        result.append(item)
    return result, False
