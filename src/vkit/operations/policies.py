"""Compare captured Cedar policy sets with SymCC and replay concrete witnesses."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import proc
from ._data import read_json
from ._runtime import pinned_binary

MAX_BUNDLE_BYTES = 524_288
MAX_SOURCE_BYTES = 131_072
MAX_POLICIES = 128
MAX_OUTPUT_BYTES = 2_097_152
MAX_TIMEOUT_SECONDS = 60
_CEDAR_ENV = "VKIT_CEDAR_PATH"
_CEDAR_SHA_ENV = "VKIT_CEDAR_SHA256"
_CVC5_ENV = "VKIT_CVC5_PATH"
_CVC5_SHA_ENV = "VKIT_CVC5_SHA256"
MODEL_SCOPE = (
    "Cedar authorization equivalence for all well-formed requests in the supplied schema and the single "
    "supplied action UID, principal type, and resource type, using Cedar CLI 4.13.0 experimental SymCC "
    "with cvc5 1.3.1. This does not establish application route behavior."
)
_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_UID_START = re.compile(r"([A-Za-z][A-Za-z0-9_]{0,63})::")


@dataclass(frozen=True)
class _Bundle:
    schema: str
    action: str
    principal_type: str
    resource_type: str
    before: tuple[str, ...]
    after: tuple[str, ...]
    digest: str


class _WitnessError(ValueError):
    pass


class _EngineUnavailable(RuntimeError):
    pass


class _CedarText:
    """Parse the value subset printed by the pinned Cedar counterexample formatter."""

    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def _space(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def _take(self, token: str) -> bool:
        self._space()
        if self.text.startswith(token, self.pos):
            self.pos += len(token)
            return True
        return False

    def _expect(self, token: str) -> None:
        if not self._take(token):
            raise _WitnessError(f"expected {token!r} in counterexample")

    def uid(self) -> dict[str, str]:
        self._space()
        match = _UID_START.match(self.text, self.pos)
        if match is None:
            raise _WitnessError("counterexample contains an unsupported entity UID")
        self.pos = match.end()
        identifier = self.value()
        if not isinstance(identifier, str):
            raise _WitnessError("counterexample entity UID identifier is not a string")
        return {"type": match.group(1), "id": identifier}

    def value(self) -> Any:
        self._space()
        if self._take("{"):
            self._space()
            if self._take("}"):
                return {}
            start = self.pos
            key_match = re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*:", self.text[self.pos:])
            if key_match:
                result: dict[str, Any] = {}
                while True:
                    self._space()
                    match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", self.text[self.pos:])
                    if not match:
                        raise _WitnessError("counterexample contains an invalid record key")
                    key = match.group(0)
                    self.pos += len(key)
                    self._expect(":")
                    if key in result:
                        raise _WitnessError("counterexample record repeats an attribute")
                    result[key] = self.value()
                    if self._take("}"):
                        return result
                    self._expect(",")
                    if self._take("}"):
                        return result
            else:
                self.pos = start
                items = []
                while True:
                    items.append(self.value())
                    if self._take("}"):
                        return items
                    self._expect(",")
                    if self._take("}"):
                        return items
        if self._take("["):
            items = []
            if self._take("]"):
                return items
            while True:
                items.append(self.uid())
                if self._take("]"):
                    return items
                self._expect(",")
        self._space()
        if self.pos < len(self.text) and self.text[self.pos] == '"':
            start = self.pos
            self.pos += 1
            escaped = False
            while self.pos < len(self.text):
                char = self.text[self.pos]
                self.pos += 1
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    try:
                        return json.loads(self.text[start:self.pos])
                    except json.JSONDecodeError as exc:
                        raise _WitnessError("counterexample contains an invalid Cedar string") from exc
            raise _WitnessError("counterexample contains an unterminated Cedar string")
        for token, value in (("true", True), ("false", False)):
            if self.text.startswith(token, self.pos):
                self.pos += len(token)
                return value
        match = re.match(r"-?(?:0|[1-9][0-9]*)", self.text[self.pos:])
        if match:
            self.pos += len(match.group(0))
            return int(match.group(0))
        raise _WitnessError("counterexample contains an unsupported Cedar value")

    def entity_store(self) -> list[dict[str, Any]]:
        entities: list[dict[str, Any]] = []
        self._expect("[")
        while not self._take("]"):
            uid = self.uid()
            parents: list[dict[str, str]] = []
            if self._take("in"):
                parents = self.value()
                if not isinstance(parents, list):
                    raise _WitnessError("counterexample entity parents are malformed")
            attrs: dict[str, Any] = {}
            if self._take("{"):
                while not self._take("}"):
                    self._space()
                    match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", self.text[self.pos:])
                    if not match:
                        raise _WitnessError("counterexample contains an unsupported entity attribute")
                    key = match.group(0)
                    self.pos += len(key)
                    self._expect(":")
                    if key in attrs:
                        raise _WitnessError("counterexample entity repeats an attribute")
                    attrs[key] = self.value()
                    if self._take("}"):
                        break
                    self._expect(",")
            entities.append({"uid": uid, "attrs": attrs, "parents": parents})
            if self._take("]"):
                break
            self._expect(",")
        self._space()
        if self.pos != len(self.text):
            raise _WitnessError("counterexample entity list contains trailing output")
        return entities


def _fields(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} must be an object with exactly {', '.join(sorted(expected))}")
    return value


def _source(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty UTF-8 text of at most {MAX_SOURCE_BYTES} bytes")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must contain only Unicode scalar values") from exc
    if size > MAX_SOURCE_BYTES:
        raise ValueError(f"{label} must be non-empty UTF-8 text of at most {MAX_SOURCE_BYTES} bytes")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL")
    return value


def _policies(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_POLICIES:
        raise ValueError(f"{label} must contain at most {MAX_POLICIES} static Cedar policies")
    policies = tuple(_source(item, f"{label}[{index}]") for index, item in enumerate(value))
    if sum(len(item.encode("utf-8")) for item in policies) > MAX_SOURCE_BYTES:
        raise ValueError(f"{label} exceeds the {MAX_SOURCE_BYTES}-byte aggregate limit")
    return policies


def _load_bundle(root: Path, path: str) -> _Bundle:
    raw, digest = read_json(root, path, max_bytes=MAX_BUNDLE_BYTES, reject_symlinks=True)
    item = _fields(raw, {"version", "schema", "action", "principal_type", "resource_type", "before", "after"},
                   "policy bundle")
    if type(item["version"]) is not int or item["version"] != 1:
        raise ValueError("policy bundle version must be the integer 1")
    schema = _source(item["schema"], "schema")
    action = _source(item["action"], "action")
    principal_type = item["principal_type"]
    resource_type = item["resource_type"]
    if not isinstance(principal_type, str) or not _TYPE.fullmatch(principal_type):
        raise ValueError("principal_type must be an unnamespaced Cedar entity type identifier")
    if not isinstance(resource_type, str) or not _TYPE.fullmatch(resource_type):
        raise ValueError("resource_type must be an unnamespaced Cedar entity type identifier")
    if not action.startswith('Action::"') or not action.endswith('"'):
        raise ValueError('action must be a Cedar Action UID such as Action::"view"')
    try:
        identifier = json.loads(action[len("Action::"):])
    except json.JSONDecodeError as exc:
        raise ValueError("action must contain a valid quoted Cedar identifier") from exc
    if not isinstance(identifier, str) or not identifier or len(identifier) > 128 or "\x00" in identifier:
        raise ValueError("action identifier must be a non-empty string of at most 128 characters")
    try:
        identifier.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("action identifier must contain only Unicode scalar values") from exc
    return _Bundle(schema, action, principal_type, resource_type,
                   _policies(item["before"], "before"), _policies(item["after"], "after"), digest)


def _run(binary: Path, args: list[str], cwd: Path, env: dict[str, str], deadline: float) -> tuple[int, bytes]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired([str(binary), *args], 0)
    command = [str(binary), *args]
    stdout_path, stderr_path = cwd / "engine.stdout", cwd / "engine.stderr"

    def output_exceeded() -> bool:
        return (stdout_path.stat().st_size + stderr_path.stat().st_size) > MAX_OUTPUT_BYTES

    outcome = proc.run(command, cwd=cwd, stdout_path=stdout_path, stderr_path=stderr_path,
                       timeout_seconds=remaining, env=env, should_cancel=output_exceeded)
    if outcome.cancelled:
        raise _WitnessError("Cedar engine output exceeded its size limit")
    if outcome.timed_out:
        raise subprocess.TimeoutExpired(command, remaining)
    if outcome.launch_error is not None:
        raise _EngineUnavailable("a configured Cedar engine could not be started")
    assert outcome.code is not None

    stdout_size, stderr_size = stdout_path.stat().st_size, stderr_path.stat().st_size
    if stdout_size + stderr_size > MAX_OUTPUT_BYTES:
        raise _WitnessError("Cedar engine output exceeded its size limit")
    with stdout_path.open("rb") as stdout_file:
        stdout = stdout_file.read(MAX_OUTPUT_BYTES + 1)
    if len(stdout) > MAX_OUTPUT_BYTES:
        raise _WitnessError("Cedar engine output exceeded its size limit")
    stderr_limit = MAX_OUTPUT_BYTES - len(stdout)
    with stderr_path.open("rb") as stderr_file:
        stderr = stderr_file.read(stderr_limit + 1)
    if len(stderr) > stderr_limit:
        raise _WitnessError("Cedar engine output exceeded its size limit")
    output = stdout + (b"\n" if stderr else b"") + stderr
    if len(output) > MAX_OUTPUT_BYTES:
        raise _WitnessError("Cedar engine output exceeded its size limit")
    return outcome.code, output


def _write_text(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _versions_match(cedar: Path, cvc5: Path, cwd: Path,
                    env: dict[str, str], deadline: float) -> bool:
    cedar_code, cedar_output = _run(cedar, ["--version"], cwd, env, deadline)
    solver_code, solver_output = _run(cvc5, ["--version"], cwd, env, deadline)
    cedar_lines = cedar_output.decode("utf-8", errors="strict").splitlines()
    solver_lines = solver_output.decode("utf-8", errors="strict").splitlines()
    return (cedar_code == 0 and cedar_lines == ["cedar-policy-cli 4.13.0"]
            and solver_code == 0 and bool(solver_lines)
            and re.match(r"This is cvc5 version 1\.3\.1(?:\s|$)", solver_lines[0]) is not None)


def _policy_text(policies: tuple[str, ...]) -> str:
    return "\n\n".join(policies) + "\n"


def _uid_json(uid: dict[str, str]) -> str:
    return f'{uid["type"]}::{json.dumps(uid["id"], ensure_ascii=False)}'


def _parse_witness(output: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lines = output.splitlines()
    if len(lines) < 6 or lines[1].strip() != "Counterexample found:":
        raise _WitnessError("SymCC counterexample output did not match the supported CLI format")
    request_uids: dict[str, str] = {}
    index = 2
    request_line = lines[index] if index < len(lines) else ""
    prefix = "principal: "
    if not request_line.startswith(prefix):
        raise _WitnessError("SymCC counterexample request fields are incomplete")
    parser = _CedarText(request_line[len(prefix):])
    for position, field in enumerate(("principal", "action", "resource")):
        uid = parser.uid()
        request_uids[field] = _uid_json(uid)
        if position < 2:
            parser._expect(",")
            next_prefix = ("action: " if position == 0 else "resource: ")
            if not parser._take(next_prefix):
                raise _WitnessError("SymCC counterexample request fields are malformed")
    parser._space()
    if parser.pos != len(parser.text):
        raise _WitnessError("SymCC counterexample request UID has trailing data")
    index += 1
    prefix = "context: "
    if index >= len(lines) or not lines[index].startswith(prefix):
        raise _WitnessError("SymCC counterexample context is missing")
    context_parser = _CedarText(lines[index][len(prefix):])
    context = context_parser.value()
    context_parser._space()
    if not isinstance(context, dict) or context_parser.pos != len(context_parser.text):
        raise _WitnessError("SymCC counterexample context is malformed")
    index += 1
    if index >= len(lines) or lines[index].strip() != "entities: [":
        raise _WitnessError("SymCC counterexample entity store is missing")
    entities_text = "\n".join([lines[index][len("entities: "):], *lines[index + 1:]])
    entity_store = _CedarText(entities_text).entity_store()
    return ({**request_uids, "context": context}, entity_store)


def _request_matches_scope(request: dict[str, Any], bundle: _Bundle) -> bool:
    return (request["principal"].startswith(bundle.principal_type + "::")
            and request["action"] == bundle.action
            and request["resource"].startswith(bundle.resource_type + "::"))


def _authorization(binary: Path, request: dict[str, Any], entities: list[dict[str, Any]], schema: Path,
                   policies: Path, directory: Path, deadline: float, env: dict[str, str]) -> str | None:
    request_path = directory / "request.json"
    entities_path = directory / "entities.json"
    request_path.write_text(json.dumps(request, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    entities_path.write_text(json.dumps(entities, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    try:
        returncode, output = _run(binary, ["authorize", "--request-json", str(request_path),
                                           "--entities", str(entities_path), "--policies", str(policies),
                                           "--schema", str(schema), "--error-format", "plain"],
                                  directory, env, deadline)
    except subprocess.TimeoutExpired:
        raise
    if output.strip() == b"ALLOW" and returncode == 0:
        return "ALLOW"
    if output.strip() == b"DENY" and returncode == 2:
        return "DENY"
    return None


def compare_authorization_policies(root: Path, path: str,
                                   timeout_seconds: int = 20) -> dict[str, Any]:
    """Compare two captured Cedar policy sets for one action and typed request family."""
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer in [1, {MAX_TIMEOUT_SECONDS}]")
    bundle = _load_bundle(root, path)
    base: dict[str, Any] = {
        "operation": "compare_authorization_policies",
        "path": path,
        "bundle_sha256": bundle.digest,
        "model_scope": MODEL_SCOPE,
        "scope": {"action": bundle.action, "principal_type": bundle.principal_type,
                  "resource_type": bundle.resource_type},
    }
    cedar = pinned_binary(_CEDAR_ENV, _CEDAR_SHA_ENV)
    cvc5 = pinned_binary(_CVC5_ENV, _CVC5_SHA_ENV)
    if cedar is None or cvc5 is None:
        return {**base, "status": "UNAVAILABLE",
                "detail": "configured Cedar or cvc5 binary is unavailable or failed its SHA-256 check"}
    cedar_path, cedar_sha = cedar
    cvc5_path, cvc5_sha = cvc5
    env = os.environ.copy()
    env["CVC5"] = str(cvc5_path)
    env["NO_COLOR"] = "1"
    deadline = time.monotonic() + timeout_seconds
    try:
        with tempfile.TemporaryDirectory(prefix="vkit-cedar-") as temporary:
            directory = Path(temporary)
            if not _versions_match(cedar_path, cvc5_path, directory, env, deadline):
                return {**base, "status": "UNAVAILABLE", "cedar_sha256": cedar_sha,
                        "cvc5_sha256": cvc5_sha,
                        "detail": "configured binaries do not report Cedar CLI 4.13.0 and cvc5 1.3.1"}
            base["backend"] = "cedar-policy-cli 4.13.0 experimental SymCC / cvc5 1.3.1"
            schema = _write_text(directory, "schema.cedarschema", bundle.schema)
            before = _write_text(directory, "before.cedar", _policy_text(bundle.before))
            after = _write_text(directory, "after.cedar", _policy_text(bundle.after))
            for policy_file in (before, after):
                returncode, _ = _run(cedar_path, ["validate", "--schema", str(schema),
                                                  "--policies", str(policy_file), "--error-format", "plain"],
                                     directory, env, deadline)
                if returncode != 0:
                    return {**base, "status": "UNSUPPORTED", "cedar_sha256": cedar_sha,
                            "cvc5_sha256": cvc5_sha,
                            "detail": "Cedar rejected the supplied schema or static policy set"}
            command = ["symcc", "--principal-type", bundle.principal_type, "--action", bundle.action,
                       "--resource-type", bundle.resource_type, "--schema", str(schema),
                       "--error-format", "plain", "--counterexample", "equivalent",
                       "--policies1", str(before), "--policies2", str(after)]
            returncode, output_bytes = _run(cedar_path, command, directory, env, deadline)
            output = output_bytes.decode("utf-8", errors="strict").strip()
            lines = output.splitlines()
            if returncode == 0 and output.strip() == "✓ Policy sets are equivalent: VERIFIED":
                return {**base, "status": "EQUIVALENT", "cedar_sha256": cedar_sha,
                        "cvc5_sha256": cvc5_sha, "witness": None}
            if (returncode != 0 or not lines
                    or lines[0] != "✗ Policy sets are equivalent: DOES NOT HOLD"):
                return {**base, "status": "UNKNOWN", "cedar_sha256": cedar_sha,
                        "cvc5_sha256": cvc5_sha, "detail": "SymCC did not return a recognized completed result"}
            request, entities = _parse_witness(output)
            if not _request_matches_scope(request, bundle):
                raise _WitnessError("SymCC witness falls outside the requested action or type scope")
            before_decision = _authorization(cedar_path, request, entities, schema, before,
                                             directory, deadline, env)
            after_decision = _authorization(cedar_path, request, entities, schema, after,
                                            directory, deadline, env)
            if before_decision is None or after_decision is None or before_decision == after_decision:
                return {**base, "status": "UNKNOWN", "cedar_sha256": cedar_sha,
                        "cvc5_sha256": cvc5_sha, "detail": "SymCC witness did not replay as a concrete authorization difference"}
            witness = {"request": request, "entities": entities,
                       "before_decision": before_decision, "after_decision": after_decision}
            return {**base, "status": "COUNTEREXAMPLE", "cedar_sha256": cedar_sha,
                    "cvc5_sha256": cvc5_sha, "witness": witness}
    except subprocess.TimeoutExpired:
        return {**base, "status": "UNKNOWN", "cedar_sha256": cedar_sha,
                "cvc5_sha256": cvc5_sha, "detail": "Cedar policy comparison exceeded its time budget"}
    except _EngineUnavailable:
        return {**base, "status": "UNAVAILABLE", "cedar_sha256": cedar_sha,
                "cvc5_sha256": cvc5_sha, "detail": "a configured Cedar engine could not be started"}
    except (OSError, UnicodeError, _WitnessError, json.JSONDecodeError, RecursionError) as exc:
        return {**base, "status": "UNKNOWN", "cedar_sha256": cedar_sha,
                "cvc5_sha256": cvc5_sha, "detail": f"Cedar returned an unusable result: {type(exc).__name__}"}
