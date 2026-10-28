pub mod p2_transfer {
    use crate::ledger_api::*;
    use vstd::prelude::*;

    verus! {
        pub fn successful_transfer<const N: usize>(
            source: Ledger<N>,
            destination: Ledger<N>,
            id: u32,
            owner: u32,
            amount: u32,
        ) -> (out: (Ledger<N>, Ledger<N>, TransferResult))
            requires
                N > 0 && source.valid() && destination.valid(),
                release_result_for(source, id, owner) == Ok(amount),
                reserve_result(destination, id, amount) == Ok(()),
            ensures
                out.2 == (TransferResult::Moved { amount }),
                out.0.view().used as nat + out.1.view().used as nat
                    == source.view().used as nat + destination.view().used as nat,
        {
            transfer(source, destination, id, owner)
        }

        pub fn destination_failure_rolls_back<const N: usize>(
            source: Ledger<N>,
            destination: Ledger<N>,
            id: u32,
            owner: u32,
            amount: u32,
            destination_error: LedgerError,
        ) -> (out: (Ledger<N>, Ledger<N>, TransferResult))
            requires
                N > 0 && source.valid() && destination.valid(),
                release_result_for(source, id, owner) == Ok(amount),
                reserve_result(destination, id, amount) == Err(destination_error),
            ensures
                out.2 == (TransferResult::DestinationFailed(destination_error)),
                same_view(out.0.view(), source.view()),
                same_view(out.1.view(), destination.view()),
        {
            transfer(source, destination, id, owner)
        }

        pub fn source_failure_preserves_both<const N: usize>(
            source: Ledger<N>,
            destination: Ledger<N>,
            id: u32,
            owner: u32,
            source_error: LedgerError,
        ) -> (out: (Ledger<N>, Ledger<N>, TransferResult))
            requires
                N > 0 && source.valid() && destination.valid(),
                release_result_for(source, id, owner) == Err(source_error),
            ensures
                out.2 == (TransferResult::SourceFailed(source_error)),
                same_view(out.0.view(), source.view()),
                same_view(out.1.view(), destination.view()),
        {
            transfer(source, destination, id, owner)
        }
    }
}
