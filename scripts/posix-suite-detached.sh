#!/bin/bash
# Start the POSIX suite detached and write its result to a file.
#
# Invoking this through `wsl.exe` from PowerShell was itself being cut: the
# wsl.exe process died mid-run while the distribution stayed alive and
# reachable, so the work inside WSL was fine and the Windows-side wrapper was
# not. Evidence: after one such cut, `wsl.exe -d Ubuntu -- echo alive` answered
# immediately, the junit reports already written were intact, and a following
# command read them fine.
#
# So the run is started with nohup and setsid inside WSL, which detaches it from
# whatever the wrapper does, and the result lands in a file that survives.
#
# Run:  bash scripts/posix-suite-detached.sh start
#       bash scripts/posix-suite-detached.sh status
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
OUTDIR="$HOME/vkit-posix-reports/detached"
LOG="$OUTDIR/run.log"

mkdir -p "$OUTDIR"

case "${1:-start}" in
status)
    echo "== log tail =="
    tail -20 "$LOG" 2>/dev/null || echo "no log yet"
    echo
    echo "== totals =="
    # posix-totals.sh carries the run's verdict in its exit code: it sums the
    # junit reports and exits nonzero on a failure, an error, an unreadable
    # report, or no reports at all. Pass that status on instead of deciding
    # again here, so `status` and a direct totals read cannot disagree.
    bash "$REPO_ROOT/scripts/posix-totals.sh" "$HOME/vkit-posix-reports/posix-suite"
    exit $?
    ;;

start)
    cd "$REPO_ROOT"
    # One definition of the PATH a POSIX run needs, so no entry point can
    # silently lose a directory. See scripts/posix-env.sh for why each is there.
    . "$(dirname "${BASH_SOURCE[0]}")/posix-env.sh"
    posix_path
    : > "$LOG"
    setsid nohup bash scripts/posix-suite-total.sh >> "$LOG" 2>&1 < /dev/null &
    echo "started, pid $!"
    echo "log:    $LOG"
    echo "reports: $HOME/vkit-posix-reports/posix-suite"
    echo "check with: bash scripts/posix-suite-detached.sh status"
    # This exit code is about the start, not the suite. The run is still going,
    # so there is no verdict to give yet; `status` is what reads the outcome and
    # it carries the run's own. A reader who takes 0 from `start` as a pass has
    # read it wrong, which is why it is said here rather than left to be found.
    exit 0
    ;;

*)
    echo "usage: $0 start|status" >&2
    exit 2
    ;;
esac
