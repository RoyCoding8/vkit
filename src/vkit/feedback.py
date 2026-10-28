"""Repository-local requests for missing verification capabilities."""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from .inputs import canonical_digest
from .store import Store, now

KINDS = ("formal", "reliable")
_LIMITS = {"task": 1000, "missing_capability": 2000, "benefit": 2000}


def submit(store: Store, request: dict[str, Any]) -> dict[str, Any]:
    if set(request) - {*_LIMITS, "kind", "run_id"}:
        raise ValueError("unknown feedback fields")
    content = {}
    for name, limit in _LIMITS.items():
        value = request.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{name} must contain 1 to {limit} characters")
        content[name] = value.strip()
    kind = request.get("kind", "formal")
    if kind not in KINDS:
        raise ValueError("feedback kind must be formal or reliable")
    content["kind"] = kind
    if request.get("run_id") is not None:
        run_id = request["run_id"]
        if not isinstance(run_id, str) or store.record(run_id) is None:
            raise ValueError("feedback run_id must name a retained run")
        content["run_id"] = run_id
    identity = canonical_digest(content)
    directory = store.root / "feedback"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{identity}.md"
    text = (f"# Verification feedback\n\nKind: {kind}\nCreated: {now()}\n"
            + (f"Run: {content['run_id']}\n" if "run_id" in content else "")
            + f"\n## Task\n\n{content['task']}\n\n## Missing capability\n\n"
            + f"{content['missing_capability']}\n\n## Verification benefit\n\n{content['benefit']}\n")
    temporary = directory / f".{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(text, encoding="utf-8")
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return _entry(path)


def _entry(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("feedback must be a regular Markdown file")
    with path.open(encoding="utf-8") as source:
        text = source.read(500)
    kind_line = text.splitlines()[2:3]
    kind = kind_line[0].removeprefix("Kind: ") if kind_line else "unclassified"
    if kind not in KINDS:
        kind = "unclassified"
    return {"id": path.stem, "kind": kind, "path": str(path), "preview": text}


def collect(store: Store, *, kind: str | None = None, offset: int = 0,
            limit: int = 20) -> dict[str, Any]:
    if kind is not None and kind not in KINDS:
        raise ValueError("feedback kind must be formal or reliable")
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("feedback requires limit 1 to 100 and nonnegative offset")
    entries = [_entry(path) for path in (store.root / "feedback").glob("*.md")
               if not path.is_symlink()]
    entries = [entry for entry in entries if kind is None or entry["kind"] == kind]
    entries.sort(key=lambda entry: entry["id"])
    entries.sort(key=lambda entry: (*KINDS, "unclassified").index(entry["kind"]))
    page = entries[offset:offset + limit]
    return {"items": page, "total": len(entries),
            "next_offset": offset + len(page) if offset + len(page) < len(entries) else None}
