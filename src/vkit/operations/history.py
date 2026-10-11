"""Check completed concurrent histories against vkit's fixed sequential models."""
from __future__ import annotations

import json
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._data import _object_without_duplicates as _object, _reject_constant, read_json
from ._runtime import pinned_binary

MAX_TRACE_BYTES = 1_048_576
MAX_OPERATIONS = 1_000
MAX_ID_CHARS = 128
MAX_ACTIONS_PER_TRANSACTION = 64
MAX_TRANSACTION_KEYS = 128
MAX_KEY_CHARS = 128
MAX_TIMEOUT_SECONDS = 60
MIN_INT64 = -(2**63)
MAX_INT64 = 2**63 - 1
_MAX_CLIENT_ID = 2**31 - 1
_BACKEND_ENV = "VKIT_HISTORY_BIN"
_BACKEND_SHA_ENV = "VKIT_HISTORY_SHA256"
_PROTOCOL_VERSION = 1
_BACKEND_VERSION = "1.3.1"
_MODELS = {"register", "queue", "transaction"}


@dataclass(frozen=True)
class _Operation:
    id: str
    client_id: int
    call: int
    return_: int
    op: str
    value: int | None
    output: int | None

    def helper_record(self) -> dict[str, Any]:
        input_: dict[str, Any] = {"op": self.op}
        if self.value is not None:
            input_["value"] = self.value
        return {
            "id": self.id,
            "client_id": self.client_id,
            "call": self.call,
            "return": self.return_,
            "input": input_,
            "output": self.output,
        }


@dataclass(frozen=True)
class _ReadAction:
    key: str


@dataclass(frozen=True)
class _WriteAction:
    key: str
    value: int


_TransactionAction = _ReadAction | _WriteAction


@dataclass(frozen=True)
class _Transaction:
    id: str
    client_id: int
    call: int
    return_: int
    actions: tuple[_TransactionAction, ...]
    observed_reads: tuple[int, ...]

    def helper_record(self) -> dict[str, Any]:
        actions = []
        for action in self.actions:
            if isinstance(action, _ReadAction):
                actions.append({"op": "read", "key": action.key})
            else:
                actions.append({"op": "write", "key": action.key, "value": action.value})
        return {
            "id": self.id,
            "client_id": self.client_id,
            "call": self.call,
            "return": self.return_,
            "input": {"actions": actions},
            "output": list(self.observed_reads),
        }


_HistoryOperation = _Operation | _Transaction


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer in [{minimum}, {maximum}]")
    return value


def _parse_operation_fields(raw: Any, index: int) -> tuple[str, dict[str, Any], str, int, int, int]:
    label = f"operations[{index}]"
    item = _fields(raw, {"id", "client_id", "call", "return", "input", "output"}, label)
    operation_id = item["id"]
    if not isinstance(operation_id, str) or not operation_id or len(operation_id) > MAX_ID_CHARS:
        raise ValueError(f"{label}.id must be a nonempty string of at most {MAX_ID_CHARS} characters")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in operation_id):
        raise ValueError(f"{label}.id must contain only Unicode scalar characters")
    client_id = _integer(item["client_id"], f"{label}.client_id", 0, _MAX_CLIENT_ID)
    call = _integer(item["call"], f"{label}.call", MIN_INT64, MAX_INT64)
    returned = _integer(item["return"], f"{label}.return", MIN_INT64, MAX_INT64)
    if call >= returned:
        raise ValueError(f"{label} must have call < return")
    return label, item, operation_id, client_id, call, returned


def _fields(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    actual = set(value)
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if extra:
            detail.append("unknown " + ", ".join(sorted(extra)))
        raise ValueError(f"{label} has " + " and ".join(detail))
    return value


def _parse_transaction(raw: Any, index: int) -> _Transaction:
    label, item, operation_id, client_id, call, returned = _parse_operation_fields(raw, index)

    input_ = _fields(item["input"], {"actions"}, f"{label}.input")
    raw_actions = input_["actions"]
    if not isinstance(raw_actions, list) or not raw_actions or len(raw_actions) > MAX_ACTIONS_PER_TRANSACTION:
        raise ValueError(f"{label}.input.actions must contain 1 to {MAX_ACTIONS_PER_TRANSACTION} actions")
    actions: list[_TransactionAction] = []
    read_count = 0
    for action_index, raw_action in enumerate(raw_actions):
        action_label = f"{label}.input.actions[{action_index}]"
        if not isinstance(raw_action, dict) or "op" not in raw_action:
            raise ValueError(f"{action_label} must contain an operation")
        op = raw_action["op"]
        if op not in ("read", "write"):
            raise ValueError(f"{action_label}.op must be 'read' or 'write'")
        action = _fields(raw_action, {"op", "key"} if op == "read" else {"op", "key", "value"},
                         action_label)
        key = action["key"]
        if (not isinstance(key, str) or not key or len(key) > MAX_KEY_CHARS
                or any(0xD800 <= ord(character) <= 0xDFFF for character in key)):
            raise ValueError(f"{action_label}.key must be a nonempty Unicode scalar string of at most {MAX_KEY_CHARS} characters")
        if op == "read":
            actions.append(_ReadAction(key))
            read_count += 1
        else:
            value = _integer(action["value"], f"{action_label}.value", MIN_INT64, MAX_INT64)
            actions.append(_WriteAction(key, value))

    observed_reads = item["output"]
    if not isinstance(observed_reads, list) or len(observed_reads) != read_count:
        raise ValueError(f"{label}.output must contain one integer per read action")
    reads = tuple(_integer(value, f"{label}.output[{index}]", MIN_INT64, MAX_INT64)
                  for index, value in enumerate(observed_reads))
    return _Transaction(operation_id, client_id, call, returned, tuple(actions), reads)


def _parse_operation(raw: Any, model: str, index: int) -> _HistoryOperation:
    if model == "transaction":
        return _parse_transaction(raw, index)
    label, item, operation_id, client_id, call, returned = _parse_operation_fields(raw, index)

    operations = {"register": {"read", "write"}, "queue": {"dequeue", "enqueue"}}[model]
    input_ = item["input"]
    if not isinstance(input_, dict) or "op" not in input_:
        raise ValueError(f"{label}.input must contain an operation")
    op = input_["op"]
    if not isinstance(op, str) or op not in operations:
        raise ValueError(f"{label}.input.op is not supported by model {model!r}")
    takes_value = op in {"write", "enqueue"}
    _fields(input_, {"op", "value"} if takes_value else {"op"}, f"{label}.input")
    value = _integer(input_["value"], f"{label}.input.value", MIN_INT64, MAX_INT64) if takes_value else None

    output = item["output"]
    returns_value = op == "read" or op == "dequeue"
    if op == "read":
        if output is None:
            raise ValueError(f"{label}.output must be an integer for read")
        output = _integer(output, f"{label}.output", MIN_INT64, MAX_INT64)
    elif op == "dequeue" and output is not None:
        output = _integer(output, f"{label}.output", MIN_INT64, MAX_INT64)
    elif not returns_value and output is not None:
        raise ValueError(f"{label}.output must be null for {op}")
    return _Operation(operation_id, client_id, call, returned, op, value, output)


def _load_trace(root: Path, path: str, model: str) -> tuple[list[_HistoryOperation], str]:
    if not isinstance(path, str) or Path(path).suffix.lower() != ".json":
        raise ValueError("path must identify a JSON file inside the repository")
    try:
        trace, digest = read_json(root, path, max_bytes=MAX_TRACE_BYTES)
    except ValueError as exc:
        if str(exc).startswith("path "):
            raise ValueError("path must identify a JSON file inside the repository") from exc
        raise ValueError(f"cannot read history {path!r}: {exc}") from exc
    try:
        trace = _fields(trace, {"schema_version", "operations"}, "history")
        if type(trace["schema_version"]) is not int or trace["schema_version"] != 1:
            raise ValueError("history.schema_version must be 1")
        raw_operations = trace["operations"]
        if not isinstance(raw_operations, list) or len(raw_operations) > MAX_OPERATIONS:
            raise ValueError(f"history.operations must be a list with at most {MAX_OPERATIONS} entries")
        operations = [_parse_operation(raw, model, i) for i, raw in enumerate(raw_operations)]
        ids = [operation.id for operation in operations]
        if len(set(ids)) != len(ids):
            raise ValueError("operation ids must be unique")
        if model == "transaction":
            keys = {action.key for operation in operations if isinstance(operation, _Transaction)
                    for action in operation.actions}
            if len(keys) > MAX_TRANSACTION_KEYS:
                raise ValueError(f"history must use at most {MAX_TRANSACTION_KEYS} distinct transaction keys")
    except TypeError as exc:
        raise ValueError(str(exc)) from exc
    return operations, digest


def _replay(operations: list[_HistoryOperation], witness: list[str], model: str) -> bool:
    by_id = {operation.id: operation for operation in operations}
    if len(witness) != len(operations) or set(witness) != set(by_id):
        return False
    positions = {operation_id: index for index, operation_id in enumerate(witness)}
    for first in operations:
        for second in operations:
            if first.return_ < second.call and positions[first.id] >= positions[second.id]:
                return False

    if model == "transaction":
        state: dict[str, int] = {}
        for operation_id in witness:
            operation = by_id[operation_id]
            if not isinstance(operation, _Transaction):
                return False
            pending = state.copy()
            read_index = 0
            for action in operation.actions:
                if isinstance(action, _ReadAction):
                    if operation.observed_reads[read_index] != pending.get(action.key, 0):
                        return False
                    read_index += 1
                else:
                    pending[action.key] = action.value
            state = pending
        return True

    if model == "register":
        state = 0
        for operation_id in witness:
            operation = by_id[operation_id]
            if not isinstance(operation, _Operation):
                return False
            if operation.op == "read":
                if operation.output != state:
                    return False
            else:
                assert operation.value is not None
                state = operation.value
        return True

    queue: deque[int] = deque()
    for operation_id in witness:
        operation = by_id[operation_id]
        if not isinstance(operation, _Operation):
            return False
        if operation.op == "enqueue":
            assert operation.value is not None
            queue.append(operation.value)
        elif queue:
            if operation.output != queue.popleft():
                return False
        elif operation.output is not None:
            return False
    return True


def check_history(root: Path, path: str, model: str, timeout_seconds: int = 10) -> dict[str, Any]:
    """Check a completed register, FIFO queue or transaction history against its fixed model."""
    if not isinstance(model, str) or model not in _MODELS:
        raise ValueError("model must be 'register', 'queue' or 'transaction'")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer in [1, {MAX_TIMEOUT_SECONDS}]")
    operations, trace_sha256 = _load_trace(root, path, model)
    base: dict[str, Any] = {
        "operation": "check_history",
        "path": path,
        "model": model,
        "trace_sha256": trace_sha256,
        "backend": "porcupine/" + _BACKEND_VERSION,
    }
    runtime = pinned_binary(_BACKEND_ENV, _BACKEND_SHA_ENV)
    if runtime is None:
        return {**base, "status": "UNAVAILABLE", "backend_sha256": None,
                "detail": "the configured Porcupine helper is unavailable or failed its SHA-256 check"}
    binary, backend_sha256 = runtime
    request = json.dumps({"schema_version": _PROTOCOL_VERSION,
                          "operations": [operation.helper_record() for operation in operations]},
                         separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    command = [str(binary), "--model", model, "--timeout-seconds", str(timeout_seconds)]
    try:
        result = subprocess.run(command, input=request, capture_output=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired:
        return {**base, "status": "UNKNOWN", "backend_sha256": backend_sha256,
                "detail": "the history check exceeded its time budget"}
    except OSError:
        return {**base, "status": "UNAVAILABLE", "backend_sha256": backend_sha256,
                "detail": "the configured Porcupine helper could not be started"}
    if result.returncode != 0 or len(result.stdout) > MAX_TRACE_BYTES:
        return {**base, "status": "UNKNOWN", "backend_sha256": backend_sha256,
                "detail": "the Porcupine helper did not return a valid result"}
    try:
        response = json.loads(result.stdout.decode("utf-8"), object_pairs_hook=_object,
                              parse_constant=_reject_constant)
        response = _fields(response, {"protocol_version", "backend_version", "status", "linearization"},
                           "helper response")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return {**base, "status": "UNKNOWN", "backend_sha256": backend_sha256,
                "detail": "the Porcupine helper returned malformed output"}
    if type(response["protocol_version"]) is not int or response["protocol_version"] != _PROTOCOL_VERSION \
            or response["backend_version"] != _BACKEND_VERSION:
        return {**base, "status": "UNKNOWN", "backend_sha256": backend_sha256,
                "detail": "the Porcupine helper identity did not match the supported protocol"}
    if response["status"] == "UNKNOWN":
        return {**base, "status": "UNKNOWN", "backend_sha256": backend_sha256,
                "detail": "Porcupine exhausted its time budget"}
    if response["status"] == "ILLEGAL":
        if response["linearization"] is not None:
            return {**base, "status": "UNKNOWN", "backend_sha256": backend_sha256,
                    "detail": "the helper returned a witness for a non-linearizable history"}
        return {**base, "status": "NOT_LINEARIZABLE", "backend_sha256": backend_sha256,
                "linearization": None}
    witness = response["linearization"]
    if response["status"] != "OK" or not isinstance(witness, list) \
            or any(not isinstance(operation_id, str) for operation_id in witness) \
            or not _replay(operations, witness, model):
        return {**base, "status": "UNKNOWN", "backend_sha256": backend_sha256,
                "detail": "the helper did not return a complete independently replayable witness"}
    return {**base, "status": "LINEARIZABLE", "backend_sha256": backend_sha256,
            "linearization": witness}
