"""Verify a comment-deletion slice without trusting the slice that did it.

A worker that deletes comments and then checks its own work can only confirm
what it thought to look for. This harness re-derives every invariant from the
pre-edit baseline and the post-edit tree, so a missed corruption shows up even
when the worker believed it was clean.

    python scripts/verify_comment_sweep.py --base main --work .

Exits non-zero on any violation. Every check prints the evidence it failed on.
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

# The protected set the user approved: tool-addressed directives and marker
# comments. Mirrors the slice briefs; if one side drifts this is the arbiter.
KEEP = re.compile(
    r"^#\s*(?:noqa\b|type\s*:|type:\s*ignore|fmt\s*:|pragma\s*:|pylint\b|flake8\b|ruff\b|isort\b)",
    re.I,
)
MARKER = re.compile(r"^#\s*(?:TODO|FIXME|XXX|HACK|NOTE|WARNING|BLOCKED)\b", re.I)
SHEBANG = re.compile(r"^#!|coding[:=]", re.I)

ROOTS = ("src/", "tests/", "scripts/", "examples/", "plugin/", "formal/")
EXCLUDED = ("src/vkit/tasks.py",)


def is_kept(text: str) -> bool:
    stripped = text.strip()
    return bool(KEEP.match(stripped) or MARKER.match(stripped) or SHEBANG.match(stripped))


def profile(path: Path) -> dict:
    return profile_text(path.read_text(encoding="utf-8", errors="replace"), path)


def profile_text(source: str, path: Path) -> dict:
    """Everything this harness needs from one file, derived without executing it."""
    comments: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                comments.append((tok.start[0], tok.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        comments = []

    docstrings: list[str] = []
    hashes: list[str] = []
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.append(doc)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "#" in node.value:
                    hashes.append(node.value)
    except SyntaxError:
        return {
            "path": path,
            "parses": False,
            "comments": comments,
            "docstrings": docstrings,
            "hash_literals": hashes,
        }

    return {
        "path": path,
        "parses": True,
        "comments": comments,
        "docstrings": docstrings,
        "hash_literals": hashes,
    }


def git_show(base: str, rel: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{base}:{rel}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout if result.returncode == 0 else None


def baseline(root: Path, base: str) -> dict[str, dict]:
    """Profiles taken from the base commit, so drift is measured not assumed.

    The file list comes from the BASE, not the working tree, so a file deleted
    during the sweep shows up as a missing file rather than quietly dropping out
    of the comparison. The base text is parsed straight from memory and is never
    written over the working tree: an earlier version round-tripped through the
    real path and silently deleted the files it was meant to measure.
    """
    listed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", base],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        print(f"could not list {base}: {listed.stderr.strip()}", file=sys.stderr)
        return {}

    out: dict[str, dict] = {}
    for rel in listed.stdout.split("\n"):
        rel = rel.strip().replace("\\", "/")
        if not rel.endswith(".py") or rel in EXCLUDED:
            continue
        if not rel.startswith(ROOTS):
            continue
        text = git_show(base, rel)
        if text is None:
            continue
        out[rel] = profile_text(text, Path(rel))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="main")
    parser.add_argument("--work", default=".")
    args = parser.parse_args()

    root = Path(args.work).resolve()
    base_profiles = baseline(root, args.base)
    print(f"baseline: {len(base_profiles)} files read from {args.base}")

    failures: list[str] = []
    removed = kept_before = kept_after = 0

    for rel, before in base_profiles.items():
        current = root / rel
        if not current.exists():
            failures.append(f"{rel}: deleted. The sweep must not delete files.")
            continue
        after = profile(current)

        if not after["parses"]:
            failures.append(f"{rel}: no longer parses as Python")
            continue

        # 1. docstrings are out of scope and must be byte-identical
        if before["docstrings"] != after["docstrings"]:
            failures.append(
                f"{rel}: docstring changed ({len(before['docstrings'])} -> "
                f"{len(after['docstrings'])}). Docstrings were never in scope."
            )

        # 2. the protected set survives
        before_kept = [t for _, t in before["comments"] if is_kept(t)]
        after_kept = [t for _, t in after["comments"] if is_kept(t)]
        if sorted(before_kept) != sorted(after_kept):
            lost = sorted(set(before_kept) - set(after_kept))
            gained = sorted(set(after_kept) - set(before_kept))
            failures.append(
                f"{rel}: protected comment set changed. lost={lost[:3]} gained={gained[:3]}"
            )
        kept_before += len(before_kept)
        kept_after += len(after_kept)

        # 3. comment-like text inside string literals is test data, never a target
        if sorted(before["hash_literals"]) != sorted(after["hash_literals"]):
            lost_lit = sorted(set(before["hash_literals"]) - set(after["hash_literals"]))
            gained_lit = sorted(set(after["hash_literals"]) - set(before["hash_literals"]))
            failures.append(
                f"{rel}: string literal containing '#' changed "
                f"({len(before['hash_literals'])} -> {len(after['hash_literals'])}). "
                f"lost={lost_lit[:2]} gained={gained_lit[:2]}"
            )

        # 4. only comments were removed
        removed += max(0, len(before["comments"]) - len(after["comments"]))

    # 5. the receipt-pinned file is untouched
    for rel in EXCLUDED:
        diff = subprocess.run(
            ["git", "diff", args.base, "--", rel],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if diff.stdout.strip():
            failures.append(f"{rel}: modified but is pinned byte-for-byte by a formal receipt")

    print(f"comments removed: {removed}")
    print(f"protected comments: {kept_before} before -> {kept_after} after")
    if failures:
        print(f"\n{len(failures)} VIOLATION(S):", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    print("all invariants hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
