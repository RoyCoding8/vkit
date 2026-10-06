"""Read a SARIF report and fail only on findings a human has not accepted."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from ..outcome import Blocked, BlockedReason, Failed, Outcome, Passed, ScenarioResult


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    line: int | None
    message: str

    @property
    def fingerprint(self) -> str:
        return f"{self.rule}|{self.path}|{self.message}"

    def to_json(self) -> dict[str, Any]:
        return {"rule": self.rule, "path": self.path, "line": self.line, "message": self.message,
                "fingerprint": self.fingerprint}


@dataclass(frozen=True)
class StaticReading:
    findings: tuple[Finding, ...]


def _location(result: dict) -> tuple[str, int | None]:
    for location in result.get("locations") or []:
        physical = location.get("physicalLocation") or {}
        uri = (physical.get("artifactLocation") or {}).get("uri", "")
        line = (physical.get("region") or {}).get("startLine")
        if uri.startswith("file:"):
            uri = PurePath(url2pathname(urlparse(uri).path)).as_posix()
        return uri.replace("\\", "/"), line
    return "", None


def read(raw: bytes, root_uri: str = "") -> StaticReading | Blocked:
    try:
        document = json.loads(raw.decode("utf-8-sig"))
        runs = document["runs"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return Blocked(BlockedReason.ARTIFACT_MALFORMED, f"not a SARIF log: {exc}")
    findings = []
    for run in runs:
        for result in run.get("results") or []:
            path, line = _location(result)
            if root_uri and path.lower().startswith(root_uri.lower()):
                path = path[len(root_uri):]
            message = (result.get("message") or {}).get("text", "")
            findings.append(Finding(str(result.get("ruleId", "?")), path.lstrip("/"), line, message))
    return StaticReading(tuple(findings))


def judge(reading: StaticReading, accepted: dict[str, int] | None) -> Outcome:
    allowance = Counter(accepted or {})
    new = []
    for finding in reading.findings:
        if allowance[finding.fingerprint] > 0:
            allowance[finding.fingerprint] -= 1
        else:
            new.append(finding)
    if not new:
        return Passed((ScenarioResult("no-new-findings", True,
                                      f"{len(reading.findings)} finding(s), all in the accepted baseline"),))
    return Failed(tuple(ScenarioResult(f"new:{f.rule}", False, f"{f.path}:{f.line}: {f.message}") for f in new))


def counts(reading: StaticReading) -> dict[str, int]:
    return dict(Counter(f.fingerprint for f in reading.findings))
