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
#                    documented WSL install puts a Linux build here. Measured
#                    on this host: the distribution's default PATH does not
#                    carry it, so this directory is the only reason those 8
#                    tests run at all.
#
# It was duplicated across three scripts and each copy was missing one of the
# two, so a run through the wrong entry point silently lost a directory. One
# definition, sourced everywhere.
#
# Sourced by every entry point in scripts/ that runs or reads a suite, which is
# what "everywhere" now means. Measured on this host, because the comment above
# claimed it while eight scripts inlined a PATH without ~/.local/bin: through
# one of those, tests/test_console.py reported 8 skipped with the reason "the
# 'claude' CLI is not on PATH"; through this, 0 skipped. The skip count is the
# receipt, and it is per-entry-point, so a fix that does not hold at every entry
# point does not hold.
#
# The one script that cannot have sourced it when it started is posix-venv.sh,
# which builds the venv this file points at. It sources it as soon as the
# interpreter exists, which is before it installs anything, and its own comment
# says why it cannot do better.
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
