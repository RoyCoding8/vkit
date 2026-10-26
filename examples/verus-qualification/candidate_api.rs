use vstd::prelude::*;
extern crate ledger;

verus! {
    pub fn new<const N: usize>(
        slots: [Option<ledger::ledger_api::Entry>; N],
        capacity: u32,
    ) -> (out: ledger::ledger_api::Ledger<N>)
        requires N > 0,
        ensures
            out.valid(),
            out.view().used == 0,
            out.view().capacity == capacity,
            forall |i: int| 0 <= i < N ==> out.view().slots[i] == None,
            out.reservations() == Map::empty(),
    {
        ledger::ledger_api::new(slots, capacity)
    }

    pub fn reserve<const N: usize>(
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
        ledger::ledger_api::reserve(input, id, owner, amount)
    }

    pub fn release<const N: usize>(
        input: ledger::ledger_api::Ledger<N>,
        id: u32,
        owner: u32,
    ) -> (out: (ledger::ledger_api::Ledger<N>, Result<u32, ledger::ledger_api::LedgerError>))
        requires N > 0 && input.valid(),
        ensures
            out.0.valid(),
            out.1 == ledger::ledger_api::release_result_for(input, id, owner),
            match out.1 {
                Ok(amount) => out.0.view().slots == ledger::ledger_api::slots_after_release(
                    input.view().slots,
                    id,
                )
                    && out.0.reservations() == input.reservations().remove(id)
                    && out.0.view().used == input.view().used - amount
                    && out.0.view().capacity == input.view().capacity
                    && ledger::ledger_api::slots_count(out.0.view().slots) + 1
                        == ledger::ledger_api::slots_count(input.view().slots),
                Err(_) => ledger::ledger_api::same_state(out.0, input),
            },
    {
        ledger::ledger_api::release(input, id, owner)
    }

    pub fn reserve_batch<const N: usize>(
        input: ledger::ledger_api::Ledger<N>,
        requests: [ledger::ledger_api::Request; 3],
        count: u32,
    ) -> (out: (ledger::ledger_api::Ledger<N>, ledger::ledger_api::BatchResponse))
        requires N > 0 && input.valid(),
        ensures
            out.0.valid(),
            out.1 == ledger::ledger_api::batch_model(input.view(), requests@, count).1,
            ledger::ledger_api::same_view(
                out.0.view(),
                ledger::ledger_api::batch_model(input.view(), requests@, count).0,
            ),
    {
        ledger::ledger_api::reserve_batch(input, requests, count)
    }

    pub fn apply_command<const N: usize>(
        input: ledger::ledger_api::Ledger<N>,
        command: ledger::ledger_api::Command,
    ) -> (out: (ledger::ledger_api::Ledger<N>, ledger::ledger_api::Response))
        requires N > 0 && input.valid(),
        ensures
            out.0.valid(),
            out.1 == ledger::ledger_api::apply_model(input.view(), command).1,
            ledger::ledger_api::same_view(
                out.0.view(),
                ledger::ledger_api::apply_model(input.view(), command).0,
            ),
    {
        ledger::ledger_api::apply_command(input, command)
    }

    pub fn run_commands<const N: usize, const M: usize>(
        initial: ledger::ledger_api::Ledger<N>,
        commands: [ledger::ledger_api::Command; M],
        responses: &mut [ledger::ledger_api::Response; M],
    ) -> (final_ledger: ledger::ledger_api::Ledger<N>)
        requires N > 0 && initial.valid(),
        ensures
            final_ledger.valid(),
            M == 0 ==> ledger::ledger_api::same_view(final_ledger.view(), initial.view()),
            exists |states: Seq<ledger::ledger_api::LedgerView>| {
                states.len() == M as int + 1
                    && final(responses)@.len() == commands@.len()
                    && ledger::ledger_api::same_view(states[0], initial.view())
                    && ledger::ledger_api::same_view(states[M as int], final_ledger.view())
                    && forall |j: int| 0 <= j < M as int ==> ledger::ledger_api::same_view(
                        states[j + 1],
                        ledger::ledger_api::apply_model(states[j], commands@[j]).0,
                    )
                    && forall |j: int| 0 <= j < M as int ==> final(responses)@[j]
                        == ledger::ledger_api::apply_model(states[j], commands@[j]).1
            },
    {
        ledger::ledger_api::run_commands(initial, commands, responses)
    }
}
