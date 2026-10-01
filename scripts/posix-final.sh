#!/bin/bash
# The full POSIX suite, writing its output where a cut wsl.exe wrapper cannot
# lose it.
#
# The wrapper on this host is cut mid-invocation often enough that a log under
# /tmp or $HOME has been lost twice, while a log on the /mnt side survived every
# time. The suite is slow there, which is the trade, and it is the one that keeps
# the number.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
. scripts/posix-env.sh
posix_path
VENV="${VKIT_POSIX_VENV:-$HOME/.venvs/vkit-posix}"
LOG="${VKIT_POSIX_LOG:-$(pwd -P)/tmp/posix-final.txt}"
mkdir -p "$(dirname "$LOG")"

echo "repo:  $(pwd -P)"
echo "log:   $LOG"
echo "python: $(command -v python || echo NONE)"
echo "claude: $(command -v claude || echo NONE)"
echo "GIT_DIR: ${GIT_DIR:-<unset>}"
echo

# PIPESTATUS, because a pipeline reports the LAST stage's status and `tail` is
# always the last one. `set -o pipefail` does not help here: it reports that some
# stage failed, not which, and it makes this script's exit status a fact about
# a shell pipeline rather than about the suite. pytest's own status is the
# verdict, so it is read out of PIPESTATUS rather than inferred from the log.
"$VENV/bin/python" -m pytest tests/ -p no:cacheprovider --tb=line --color=no -rf \
    2>&1 | tee "$LOG" | tail -30
status="${PIPESTATUS[0]}"
[ "$status" -ne 0 ] && echo "the run above did not pass (pytest exit $status)" >&2
exit "$status"
