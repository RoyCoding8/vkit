"""vkit: run a registered check in a Git repository and keep a trustworthy outcome.

The package is the durable record. `execution.run_check` is the entry point that
both the CLI and the later per-run supervisor use, so the two never disagree
about what a result means.

Inside the trusted integration launcher this package is already imported, and it
must stay out of the candidate's reach for the length of the check. A driver the
candidate wrote is candidate code, and it must not be able to reach the code
that will read its verdict. The refusal is deliberately narrow: it triggers on
the environment variable the launcher sets, it refuses the plain `vkit` name
only, and it happens at import time so the candidate cannot catch it and carry
on. `vkit.integration` and the submodules already loaded stay usable, which is
what lets the launcher itself finish the job.
"""
import os

__all__ = ["__version__"]

__version__ = "0.1.0"

if os.environ.get("VKIT_TRUSTED_LAUNCHER") == "1":
    raise ImportError(
        "vkit is not importable inside a trusted integration check. The code that "
        "reads this check's result is already loaded in this process, and letting "
        "the code under test reach it would let a candidate report its own "
        "verdict. Run this driver outside the trusted launcher, or verify the "
        "behaviour it depends on directly."
    )
