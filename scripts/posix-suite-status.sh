#!/bin/bash
# Report which per-file suite runs contain a failure or an error.
#
# pytest -q writes progress dots to stdout and its summary line to a separate
# terminal writer, so a redirected file holds only the dots. A dot line with
# anything other than '.' and 's' in it is therefore the whole verdict, and this
# reads it that way instead of grepping for a count line that is not there.
#
# Run:  bash scripts/posix-suite-status.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$REPO_ROOT"
# This script reads files rather than running anything, so it needs no PATH of
# its own. It sources the definition anyway so that "one definition, sourced
# everywhere" is a fact a reader can check by grepping for one string rather
# than a claim they have to take on trust. See scripts/posix-env.sh.
. "$(dirname "${BASH_SOURCE[0]}")/posix-env.sh"
DIR="tmp/posix-suite"

if [ ! -d "$DIR" ]; then
    echo "no per-file output yet in $DIR"
    # Already a nonzero exit, and it says why. Left as it was rather than
    # restated, because this script's verdict is the exit code and a second
    # rule next to it is a second thing to keep in agreement with the first.
    exit 1
fi

total=0
bad=0
unreadable=0
for f in "$DIR"/*.txt; do
    [ -e "$f" ] || continue
    total=$((total + 1))
    # Keep only F (failure) and E (error). '.' is a pass, 's' is a skip, and the
    # progress marker "[100%]" carries neither.
    flagged=$(tr -d ' \n' < "$f" | tr -cd 'FE')
    name=$(basename "$f" .txt)
    if [ -s "$f" ] && [ -z "$flagged" ] && ! grep -qE '[.sFE]|\[ *[0-9]+%\]' "$f"; then
        # A file with no progress marks describes no run at all, which is a
        # death rather than a clean file. Counting it as clean is the false
        # green this script exists to prevent.
        unreadable=$((unreadable + 1))
        printf '  %-34s NO PROGRESS OUTPUT, so it died\n' "$name"
    elif [ -n "$flagged" ]; then
        bad=$((bad + 1))
        printf '  %-34s %s\n' "$name" "FLAGGED: $flagged"
        # Name the offending tests from the -rA block if pytest wrote one.
        grep -E "^(FAILED|ERROR)" "$f" | head -5
    else
        printf '  %-34s clean\n' "$name"
    fi
done

echo
echo "files run: $total   with failures or errors: $bad   that died: $unreadable"
# The exit code is the verdict: a failure, an error, or a file that produced
# nothing to read. `[ "$bad" -eq 0 ] || exit 1` was the rule before, and it
# could not see a dead file because a dead file leaves no F or E to find.
if [ "$bad" -ne 0 ] || [ "$unreadable" -ne 0 ]; then
    exit 1
fi
exit 0
