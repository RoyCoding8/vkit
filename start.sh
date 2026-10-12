#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -x "$root/.venv/bin/python" ]; then
    exec "$root/.venv/bin/python" "$root/tui.py" "$@"
elif [ -x "$root/.venv/Scripts/python.exe" ]; then
    exec "$root/.venv/Scripts/python.exe" "$root/tui.py" "$@"
elif command -v uv >/dev/null 2>&1; then
    exec uv run --project "$root" --extra mcp python "$root/tui.py" "$@"
fi
echo 'Install uv, then run: uv sync --extra mcp' >&2
exit 1
