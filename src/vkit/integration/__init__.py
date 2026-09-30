"""Integration verification: whether one candidate commit, built on one target
commit, satisfies the approved policy.

The package exists to make three refusals mechanical. A passing worker branch
cannot satisfy the candidate, because the checks run in a checkout of the
candidate commit and nowhere else. A candidate cannot lower its own bar, because
the required set and the executed manifest come from trusted configuration read
before the candidate is checked out. A moved target cannot be published onto,
because the tested target is recorded and a publish must re-check it.

`verify.py` is the operation. `policy.py` decides what is required and compares
it. `gitidentity.py` answers what a commit is. `checkout.py` makes the tree the
checks run in. `sandbox.py` and `launcher.py` keep the candidate's own verifier
code from being the thing that reads its own verdict. `concurrency.py` holds the
two independent bounds. There is no queue, no scheduler and no merge loop here,
and `CONTRACT.md` forbids one.
"""
from __future__ import annotations

__all__ = [
    "concurrency",
    "checkout",
    "gitidentity",
    "launcher",
    "policy",
    "sandbox",
    "verify",
]
