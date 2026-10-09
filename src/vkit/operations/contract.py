"""Trusted operation definitions shared by the CLI and MCP adapters."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from jsonschema import Draft202012Validator, SchemaError, ValidationError, validate

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


class OperationInputError(ValueError):
    """An operation request does not match its installed input contract."""


class OperationContractError(RuntimeError):
    """An installed operation returned a value outside its declared contract."""


@dataclass(frozen=True)
class FileInput:
    flag: str
    max_bytes: int


@dataclass(frozen=True)
class Operation:
    version: int
    name: str
    description: str
    properties: Mapping[str, Mapping[str, Any]]
    required: tuple[str, ...]
    handler: Callable[..., dict[str, Any]]
    outcomes: Mapping[str, int]
    read_only: bool = True
    cli_files: Mapping[str, FileInput] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise ValueError(f"unsupported operation contract version {self.version!r}")
        if not _NAME.fullmatch(self.name):
            raise ValueError(f"invalid operation name {self.name!r}")
        if not self.description or not callable(self.handler):
            raise ValueError(f"operation {self.name!r} needs a description and handler")
        if "root" in self.properties:
            raise ValueError(f"operation {self.name!r} cannot declare the handler root input")
        if set(self.required) - set(self.properties):
            raise ValueError(f"operation {self.name!r} requires an undeclared property")
        for name, schema in self.properties.items():
            if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(schema, Mapping):
                raise ValueError(f"operation {self.name!r} has an invalid property declaration")
            schema_type = schema.get("type")
            if not isinstance(schema_type, str) or schema_type not in _TYPES:
                raise ValueError(f"operation {self.name!r} property {name!r} has an unsupported type")
            if "default" in schema:
                try:
                    validate(schema["default"], schema)
                except ValidationError as exc:
                    raise ValueError(f"operation {self.name!r} has an invalid default for {name!r}") from exc
        if not self.outcomes or any(not isinstance(name, str) or type(code) is not int or code not in (0, 1, 2, 3, 5)
                                     for name, code in self.outcomes.items()):
            raise ValueError(f"operation {self.name!r} needs declared outcomes with valid CLI exit codes")
        if set(self.cli_files) - set(self.properties):
            raise ValueError(f"operation {self.name!r} maps an undeclared CLI file input")
        flags = {"help", "project", "json"}
        destinations = {"help", "project", "json", "command", "compute_command", "handler", "_vkit_operation"}
        for name, declaration in self.properties.items():
            file_input = self.cli_files.get(name)
            flag = file_input.flag if file_input is not None else name.replace("_", "-")
            destination = name + "_file" if file_input is not None else name
            if file_input is not None and (self.properties[name].get("type") != "string"
                                           or type(file_input.max_bytes) is not int or file_input.max_bytes < 1):
                raise ValueError(f"operation {self.name!r} has an invalid CLI file mapping")
            if not _NAME.fullmatch(flag.replace("-", "_")):
                raise ValueError(f"operation {self.name!r} has an invalid CLI file mapping")
            if flag in flags:
                raise ValueError(f"operation {self.name!r} repeats a CLI flag")
            if destination in destinations:
                raise ValueError(f"operation {self.name!r} repeats a CLI destination")
            flags.add(flag)
            destinations.add(destination)
        try:
            Draft202012Validator.check_schema(self.input_schema)
        except SchemaError as exc:
            raise ValueError(f"operation {self.name!r} has an invalid input schema") from exc

    @property
    def cli_name(self) -> str:
        return self.name.replace("_", "-")

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": dict(self.properties),
                "required": list(self.required), "additionalProperties": False}


def operation_by_name(operations: tuple[Operation, ...]) -> dict[str, Operation]:
    names = [operation.name for operation in operations]
    cli_names = [operation.cli_name for operation in operations]
    if len(names) != len(set(names)):
        raise ValueError("duplicate operation name")
    if len(cli_names) != len(set(cli_names)):
        raise ValueError("duplicate operation CLI name")
    return {operation.name: operation for operation in operations}


def validate_arguments(operation: Operation, arguments: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, Mapping):
        raise OperationInputError("arguments must be an object")
    values = dict(arguments)
    for name, schema in operation.properties.items():
        if name not in values and "default" in schema:
            values[name] = deepcopy(schema["default"])
    try:
        validate(values, operation.input_schema)
    except ValidationError as exc:
        raise OperationInputError(exc.message) from exc
    return values


def invoke(operation: Operation, root: Path, arguments: Mapping[str, Any]) -> dict[str, Any]:
    values = validate_arguments(operation, arguments)
    try:
        result = operation.handler(root, **values)
    except ValueError as exc:
        raise OperationInputError(str(exc)) from exc
    if not isinstance(result, dict):
        raise OperationContractError(f"{operation.name} returned a non-object result")
    status = result.get("status")
    if not isinstance(status, str) or status not in operation.outcomes:
        raise OperationContractError(f"{operation.name} returned undeclared status {status!r}")
    if any(not isinstance(key, str) for key in result):
        raise OperationContractError(f"{operation.name} returned a result with non-string keys")
    try:
        json.dumps(result, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise OperationContractError(f"{operation.name} returned a non-JSON result") from exc
    return result


def cli_value(text: str, schema: Mapping[str, Any]) -> Any:
    kind = schema["type"]
    if kind == "string":
        return text
    if kind == "integer":
        try:
            return int(text)
        except ValueError as exc:
            raise ValueError("expected an integer") from exc
    if kind == "number":
        try:
            return float(text)
        except ValueError as exc:
            raise ValueError("expected a number") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"expected JSON for {kind} input") from exc
    if kind == "boolean" and type(value) is not bool:
        raise ValueError("expected JSON true or false")
    if kind == "array" and not isinstance(value, list):
        raise ValueError("expected a JSON array")
    if kind == "object" and not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def cli_arguments(operation: Operation, namespace: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, schema in operation.properties.items():
        if name in operation.cli_files:
            file_input = operation.cli_files[name]
            flag = file_input.flag
            path = namespace.get(name + "_file")
            if path is None:
                continue
            try:
                with Path(path).open("rb") as handle:
                    data = handle.read(file_input.max_bytes + 1)
            except OSError as exc:
                raise OperationInputError(f"cannot read {flag}: {exc}") from exc
            if len(data) > file_input.max_bytes:
                raise OperationInputError(f"{flag} must be at most {file_input.max_bytes} bytes")
            try:
                values[name] = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise OperationInputError(f"{flag} must be valid UTF-8") from exc
        elif name in namespace:
            values[name] = namespace[name]
    return values
