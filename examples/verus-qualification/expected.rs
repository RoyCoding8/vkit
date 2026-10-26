use vstd::prelude::*;
extern crate ledger;
extern crate candidate_api;

verus! {
    pub fn package_claim<const N: usize>(
        input: ledger::ledger_api::Ledger<N>,
        id: u32,
        owner: u32,
        amount: u32,
    ) -> (out: (ledger::ledger_api::Ledger<N>, Result<(), ledger::ledger_api::LedgerError>))
        requires N > 0 && input.valid(),
        ensures
            out.0.valid(),
            out.1 == ledger::ledger_api::reserve_result(input, id, amount),
            match out.1 {
                Ok(()) => out.0.view().slots == ledger::ledger_api::slots_after_reserve(
                    input.view().slots,
                    ledger::ledger_api::Entry { id, owner, amount },
                )
                    && out.0.reservations()
                        == input.reservations().insert(id, (owner, amount))
                    && out.0.view().used == input.view().used + amount
                    && out.0.view().capacity == input.view().capacity,
                Err(_) => ledger::ledger_api::same_state(out.0, input),
            },
    {
        candidate_api::reserve(input, id, owner, amount)
    }
}
