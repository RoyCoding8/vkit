use vstd::prelude::*;
extern crate ledger;

verus! {
    pub proof fn reconstructed_positive_limit_regression()
        ensures
            ledger::ledger_api::valid_view::<0>(ledger::ledger_api::LedgerView {
                slots: Seq::empty(),
                used: 0,
                capacity: 0,
            }),
            0nat >= 0,
            !(0usize > 0),
    {
    }
}
