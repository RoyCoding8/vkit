from __future__ import annotations

import ast
import json
import re
import subprocess
import sys

from helpers import ROOT

NARRATION = re.compile(r"Plan \d|[Cc]heckpoint|CONTRACT\.md|milestone|supervisor|task_finalize")


def test_self_verification_requires_tests_that_pytest_collects():
    manifest = json.loads((ROOT / "verification/manifest.json").read_text(encoding="utf-8"))
    required = {nodeid for check in manifest["checks"] if check["kind"] in {"pytest", "property"}
                for nodeid in check["required_tests"]}
    assert required
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts=",
         "-p", "no:cacheprovider", *sorted(required)],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    assert required <= set(collected.stdout.splitlines()), collected.stdout


def test_source_describes_what_the_code_does_now_not_how_it_got_here():
    hits = [f"{path.relative_to(ROOT)}:{number}: {line.strip()}"
            for base in ("src", "plugin")
            for path in sorted((ROOT / base).rglob("*"))
            if path.suffix in {".py", ".md", ".json", ".html"}
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if NARRATION.search(line)]
    assert hits == []


def test_tests_do_not_directly_write_sys_path():
    hits = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.Call, ast.Assign, ast.AugAssign)):
                expression = ast.unparse(node)
                if expression.startswith(("sys.path.insert(", "sys.path.append(", "sys.path.extend(",
                                          "sys.path =", "sys.path[", "sys.path +=")):
                    hits.append(f"{path.relative_to(ROOT)}:{node.lineno}: {expression}")
    assert hits == []
