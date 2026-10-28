pub mod p2_concurrent {
    use crate::ledger_api::*;
    use vstd::prelude::*;

    verus! {
        pub fn lock_protected_reserve_and_release_contracts() {
            let empty = new([None, None], 2);
            let shared = SharedLedger::new(empty);

            let reserved = shared.reserve(9, 5, 1);
            assert(valid_view::<2>(reserved.1@.0));
            assert(valid_view::<2>(reserved.1@.1));
            assert(reserved.0 == reserve_result_view(reserved.1@.0, 9, 1));

            let released = shared.release(9, 5);
            assert(valid_view::<2>(released.1@.0));
            assert(valid_view::<2>(released.1@.1));
            assert(released.0 == release_result_view(released.1@.0, 9, 5));
        }

        pub fn rejected_operation_preserves_snapshot() {
            let empty = new([None, None], 2);
            let shared = SharedLedger::new(empty);

            let rejected = shared.release(12, 8);
            assert(valid_view::<2>(rejected.1@.0));
            assert(valid_view::<2>(rejected.1@.1));
            assert(rejected.0 == release_result_view(rejected.1@.0, 12, 8));
        }
    }
}
