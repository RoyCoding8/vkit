use vstd::prelude::*;
extern crate ledger;
use ledger::ledger_api::*;

verus! {
    pub open spec fn strict_reserve_admissible(
        state: LedgerView,
        amount: u32,
    ) -> bool {
        (state.used as nat) + (amount as nat) < (state.capacity as nat)
    }

    pub fn exact_capacity_boundary_control() {
        let slots = [Some(Entry { id: 1, owner: 9, amount: 7 }), None];
        proof {
            reveal_with_fuel(slots_used_from, 3);
            reveal_with_fuel(slots_count_from, 3);
            assert(valid_view::<2>(LedgerView { slots: slots@, used: 7, capacity: 10 }));
        }
        let state = from_parts(slots, 7, 10);
        let ghost before = state.view();
        assert(reserve_result_view(before, 2, 3) == Ok(()));
        assert(before.used as nat + 3nat == before.capacity as nat);
        assert(!strict_reserve_admissible(before, 3));
        let (_, exact_capacity) = reserve(state, 2, 11, 3);
        assert(exact_capacity == Ok(()));
    }
}
