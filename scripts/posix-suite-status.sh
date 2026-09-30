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
DIR="tmp/posix-suite"

if [ ! -d "$DIR" ]; then
    echo "no per-file output yet in $DIR"
    exit 1
fi

total=0
bad=0
for f in "$DIR"/*.txt; do
    [ -e "$f" ] || continue
    total=$((total + 1))
    # Keep only F (failure) and E (error). '.' is a pass, 's' is a skip, and the
    # progress marker "[100%]" carries neither.
    flagged=$(tr -d ' \n' < "$f" | tr -cd 'FE')
    name=$(basename "$f" .txt)
    if [ -n "$flagged" ]; then
        bad=$((bad + 1))
        printf '  %-34s %s\n' "$name" "FLAGGED: $flagged"
        # Name the offending tests from the -rA block if pytest wrote one.
        grep -E "^(FAILED|ERROR)" "$f" | head -5
    else
        printf '  %-34s clean\n' "$name"
    fi
done

echo
echo "files run: $total   with failures or errors: $bad"
[ "$bad" -eq 0 ] || exit 1
