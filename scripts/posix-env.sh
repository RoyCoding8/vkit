#!/bin/bash
# The PATH a POSIX verification run needs, and nothing else.
#
# Sourced, not executed, so a script that needs the environment gets it without
# this file having to know what the caller is. Three directories, each for a
# measured reason:
#
#   $VENV/bin        the example manifests and several acceptance rows name the
#                    interpreter `python`, and this host ships only `python3`.
#                    Measured without it: every check reported BLOCKED with
#                    `prerequisite_missing: python: 'python' is not on PATH`,
#                    which is a missing name rather than a product defect. The
#                    venv provides `python`.
#   ~/.local/bin     the `claude` CLI, which tests/test_console.py drives to prove
#                    the host accepts the plugin. Measured without it: 8 tests
#                    skipped, which is a hole rather than a reason. The
#                    documented WSL install puts a Linux build here.
#
# It was duplicated across three scripts and each copy was missing one of the
# two, so a run through the wrong entry point silently lost a directory. One
# definition, sourced everywhere.
#
# VKIT_POSIX_VENV overrides the venv location.

# The one directory every POSIX run writes its reports to, and where
# scripts/posix-totals.sh reads them from. It was defined twice with two
# different values, so a run through posix-suite.sh wrote to tmp/ and a
# read through posix-totals.sh looked in $HOME, and the totals silently
# described a run that had already finished.
posix_reports() {
    echo "${VKIT_POSIX_REPORTS:-$HOME/vkit-posix-reports/posix-suite}"
}

posix_path() {
    local venv="${VKIT_POSIX_VENV:-$HOME/.venvs/vkit-posix}"
    PATH="$venv/bin:$HOME/.local/bin:$PATH"
    export PATH
}

# A git worktree's `.git` is a FILE naming the directory that holds its state,
# and git writes a Windows path into it: `gitdir: D:/AI/Poteto's Style/.git/
# worktrees/gap2-posix`. WSL cannot resolve a drive-letter path, so inside a
# worktree git is blind. Measured: `git cat-file -e b3c52d5^{commit}` returned 128
# for a commit that is in the history, and every revision in the history returned
# 128, while the same question from the main checkout answered "present" for all
# of them. Three tests failed on this and were reporting a missing commit when
# the repository was simply unreachable.
#
# The fix is to point git at the same directory by its WSL path, which resolves.
# Set only when the `.git` file names a path this filesystem cannot reach, so a
# normal checkout keeps git's own answer and this never overrides it.
posix_git_dir() {
    [ -f .git ] || return 0            # a real .git directory: nothing to do
    local named
    named=$(sed -n 's/^gitdir: //p' .git 2>/dev/null | head -1)
    [ -n "$named" ] || return 0
    # Already a path this shell can use, so git will manage it itself.
    [ -d "$named" ] && return 0
    # Translate a Windows path into its /mnt form, which does resolve. The
    # drive letter is lowercased because WSL mounts it that way: a measured `D:`
    # became `/mnt/D/...`, which does not exist, while `/mnt/d/...` does. That
    # mistake is why the first version of this fix appeared to do nothing.
    local translated
    translated=$(printf '%s' "$named" \
        | sed -E 's#^([A-Za-z])[/:]#/mnt/\L\1#; s#\\#/#g; s#:#/#')
    if [ -d "$translated" ]; then
        export GIT_DIR="$translated"
        export GIT_WORK_TREE="$(pwd -P)"
    fi
}
