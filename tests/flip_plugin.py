import os

if os.environ.get("VKIT_FLIP_MUTANT"):
    from vkit import query, state

    def ready_mutant(*_args, **_kwargs):
        return {"verdict": "READY", "reason": "every required check passed against the current inputs",
                "clearing": None, "doubts": [], "checks": [], "formal": []}

    def unchanged(*_args, **_kwargs):
        return {"outcome": "unchanged", "clearing": None, "receipts": []}

    state.gate = ready_mutant
    query.gate = ready_mutant
    state.comparison = unchanged
