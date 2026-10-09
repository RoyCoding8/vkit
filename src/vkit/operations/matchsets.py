"""Exact finite-alphabet comparison in the pinned Greenery regex dialect."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from greenery.fsm import Fsm

BACKEND = "greenery"
BACKEND_VERSION = "4.2.2"
DIALECT = "greenery-4.2.2"
MAX_PATTERN_BYTES = 2_048
MAX_ALPHABET_SIZE = 64
MAX_TIMEOUT_MS = 60_000
MODEL_SCOPE = (
    "Exact full-match regular-language semantics in the greenery-4.2.2 dialect, "
    "restricted to strings over the supplied finite alphabet."
)

Status = Literal["EQUIVALENT", "COUNTEREXAMPLE", "UNKNOWN", "UNSUPPORTED", "UNAVAILABLE"]


@dataclass(frozen=True)
class MatchsetResult:
    status: Status
    alphabet: str
    old_only: str | None = None
    new_only: str | None = None
    reason: str | None = None
    backend: str = BACKEND
    backend_version: str | None = None
    dialect: str = DIALECT
    model_scope: str = MODEL_SCOPE

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "old_only": self.old_only,
            "new_only": self.new_only,
            "reason": self.reason,
            "backend": self.backend,
            "backend_version": self.backend_version,
            "dialect": self.dialect,
            "alphabet": self.alphabet,
            "model_scope": self.model_scope,
        }


def _utf8_size(value: str, name: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must contain Unicode scalar values") from exc


def _validate(old_pattern: str, new_pattern: str, alphabet: str, timeout_ms: int) -> None:
    for value, name in ((old_pattern, "old_pattern"), (new_pattern, "new_pattern"), (alphabet, "alphabet")):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
    if len(old_pattern) > MAX_PATTERN_BYTES or _utf8_size(old_pattern, "old_pattern") > MAX_PATTERN_BYTES:
        raise ValueError(f"old_pattern must be at most {MAX_PATTERN_BYTES} UTF-8 bytes")
    if len(new_pattern) > MAX_PATTERN_BYTES or _utf8_size(new_pattern, "new_pattern") > MAX_PATTERN_BYTES:
        raise ValueError(f"new_pattern must be at most {MAX_PATTERN_BYTES} UTF-8 bytes")
    if len(alphabet) > MAX_ALPHABET_SIZE:
        raise ValueError(f"alphabet must contain at most {MAX_ALPHABET_SIZE} unique codepoints")
    _utf8_size(alphabet, "alphabet")
    if len(set(alphabet)) != len(alphabet):
        raise ValueError(f"alphabet must contain at most {MAX_ALPHABET_SIZE} unique codepoints")
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= MAX_TIMEOUT_MS:
        raise ValueError(f"timeout_ms must be an integer from 1 to {MAX_TIMEOUT_MS}")


def compare_matchsets(old_pattern: str, new_pattern: str, alphabet: str,
                      timeout_ms: int = 2_000) -> MatchsetResult:
    """Compare full-match languages over `alphabet` and return shortest directional witnesses."""
    _validate(old_pattern, new_pattern, alphabet, timeout_ms)
    request = json.dumps(
        {"old_pattern": old_pattern, "new_pattern": new_pattern, "alphabet": alphabet},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--worker"],
            input=request,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout_ms / 1_000,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return MatchsetResult("UNKNOWN", alphabet, reason=f"computation exceeded {timeout_ms} ms")
    except OSError as exc:
        return MatchsetResult("UNAVAILABLE", alphabet, reason=f"cannot start isolated backend: {exc}")

    if completed.returncode != 0:
        return MatchsetResult("UNAVAILABLE", alphabet, reason="isolated backend exited without a result")
    try:
        payload = json.loads(completed.stdout)
        return MatchsetResult(
            status=payload["status"],
            alphabet=alphabet,
            old_only=payload.get("old_only"),
            new_only=payload.get("new_only"),
            reason=payload.get("reason"),
            backend=payload.get("backend", BACKEND),
            backend_version=payload.get("backend_version"),
            dialect=payload.get("dialect", DIALECT),
            model_scope=payload.get("model_scope", MODEL_SCOPE),
        )
    except (ValueError, KeyError, TypeError):
        return MatchsetResult("UNAVAILABLE", alphabet, reason="isolated backend returned an invalid result")


def _worker_result(old_pattern: str, new_pattern: str, alphabet: str) -> dict[str, Any]:
    try:
        from greenery import Charclass, parse
        from greenery.parse import NoMatch
    except ImportError:
        return _payload("UNAVAILABLE", alphabet, reason="greenery 4.2.2 is not installed", backend_version=None)

    try:
        from importlib.metadata import PackageNotFoundError, version

        backend_version = version(BACKEND)
    except PackageNotFoundError:
        backend_version = None
    if backend_version != BACKEND_VERSION:
        return _payload(
            "UNAVAILABLE",
            alphabet,
            reason=f"requires greenery {BACKEND_VERSION}; found {backend_version or 'unknown version'}",
            backend_version=backend_version,
        )

    try:
        old_source = parse(old_pattern)
        new_source = parse(new_pattern)
    except (NoMatch, IndexError) as exc:
        return _payload("UNSUPPORTED", alphabet, reason=str(exc), backend_version=backend_version)
    except RecursionError:
        return _payload(
            "UNSUPPORTED", alphabet, reason="pattern exceeds the backend parser's recursion limit",
            backend_version=backend_version,
        )

    try:
        old = old_source.to_fsm()
        new = new_source.to_fsm()
        sigma_star = parse(str(Charclass(alphabet)) + "*").to_fsm()
    except RecursionError:
        return _payload("UNKNOWN", alphabet, reason="automata conversion exceeded the backend recursion limit",
                        backend_version=backend_version)
    old_domain = old.intersection(sigma_star)
    new_domain = new.intersection(sigma_star)
    old_only, old_valid = _shortest_checked(old_domain.difference(new_domain), alphabet, old, new)
    if not old_valid:
        return _payload("UNKNOWN", alphabet, reason="backend produced a witness that failed membership validation",
                        backend_version=backend_version)
    new_only, new_valid = _shortest_checked(new_domain.difference(old_domain), alphabet, new, old)
    if not new_valid:
        return _payload("UNKNOWN", alphabet, reason="backend produced a witness that failed membership validation",
                        backend_version=backend_version)
    if old_only is None and new_only is None:
        return _payload("EQUIVALENT", alphabet, backend_version=backend_version)
    return _payload("COUNTEREXAMPLE", alphabet, old_only, new_only, backend_version=backend_version)


def _shortest_checked(difference: Fsm, alphabet: str, accepted: Fsm,
                      rejected: Fsm) -> tuple[str | None, bool]:
    witness = next(difference.strings(()), None)
    if witness is None:
        return None, True
    if (not all(char in alphabet for char in witness)
            or not accepted.accepts(witness)
            or rejected.accepts(witness)):
        return None, False
    return witness, True


def _payload(status: Status, alphabet: str, old_only: str | None = None,
             new_only: str | None = None, *, reason: str | None = None,
             backend_version: str | None = None) -> dict[str, Any]:
    return MatchsetResult(status, alphabet, old_only, new_only, reason,
                          backend_version=backend_version).to_json()


def _main() -> int:
    try:
        request = json.load(sys.stdin)
        result = _worker_result(request["old_pattern"], request["new_pattern"], request["alphabet"])
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
        return 0
    except (IndexError, KeyError, TypeError, ValueError):
        return 1


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    raise SystemExit(_main())
