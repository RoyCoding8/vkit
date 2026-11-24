"""Report every comment in vkit's Python source as a SARIF finding.

A comment is where an agent justifies code instead of fixing it. vkit's own `no-comments` check runs this and
fails on any comment that is not in the baseline a human pinned. `noqa` and `type:` pragmas are tool directives,
not prose, and are not reported.
"""
from __future__ import annotations

import io
import json
import sys
import tokenize
from pathlib import Path


def findings(root: Path) -> list[dict]:
    results = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            text = token.string.lstrip("#").strip()
            if token.type != tokenize.COMMENT or text.startswith(("noqa", "type:")):
                continue
            results.append({"ruleId": "no-comments", "message": {"text": text},
                            "locations": [{"physicalLocation": {"artifactLocation": {"uri": path.as_posix()},
                                                                "region": {"startLine": token.start[0]}}}]})
    return results


def main() -> int:
    report = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "vkit-comment-lint"}},
                                             "results": findings(Path(sys.argv[1]))}]}
    Path(sys.argv[2]).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 1 if report["runs"][0]["results"] else 0


if __name__ == "__main__":
    sys.exit(main())
