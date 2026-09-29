"""vkit: run a registered check in a Git repository and keep a trustworthy outcome.

The package is the durable record. `execution.run_check` is the entry point that
both the CLI and the later per-run supervisor use, so the two never disagree
about what a result means.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
