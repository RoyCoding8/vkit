use vstd::prelude::*;
extern crate ledger;

verus! {
    pub open spec fn reserve_admissible_by_total(
        state: ledger::ledger_api::LedgerView,
        amount: u32,
    ) -> bool {
        (state.used as nat) + (amount as nat) <= (state.capacity as nat)
    }

    pub proof fn reserve_capacity_relation<const N: usize>(
        state: ledger::ledger_api::LedgerView,
        amount: u32,
        id: u32,
    )
        requires ledger::ledger_api::valid_view::<N>(state),
        ensures
            (amount <= state.capacity - state.used)
                == reserve_admissible_by_total(state, amount),
            (ledger::ledger_api::reserve_result_view(state, id, amount) == Ok(()))
                == (amount > 0
                    && !ledger::ledger_api::slots_have_id(state.slots, id)
                    && !ledger::ledger_api::full_slots(state.slots)
                    && reserve_admissible_by_total(state, amount)),
    {
        assert(state.used <= state.capacity);
        assert((state.capacity - state.used) as nat
            == state.capacity as nat - state.used as nat);
    }
}
