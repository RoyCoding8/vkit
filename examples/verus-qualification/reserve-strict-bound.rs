use vstd::prelude::*;
extern crate ledger;

verus! {
    pub open spec fn strict_reserve_admissible(
        state: ledger::ledger_api::LedgerView,
        amount: u32,
    ) -> bool {
        (state.used as nat) + (amount as nat) < (state.capacity as nat)
    }

    pub fn exact_capacity_boundary_control() {
        let empty = ledger::ledger_api::new::<2>([None, None], 10);
        let (state, first) = ledger::ledger_api::reserve(empty, 1, 9, 7);
        assert(first == Ok(()));
        assert(state.view().used == 7);
        assert(state.view().capacity == 10);
        let before = state.view();
        assert(ledger::ledger_api::reserve_result_view(before, 2, 3) == Ok(()));
        assert(before.used as nat + 3nat == before.capacity as nat);
        assert(!strict_reserve_admissible(before, 3));
        let (_, exact_capacity) = ledger::ledger_api::reserve(state, 2, 11, 3);
        assert(exact_capacity == Ok(()));
    }
}
