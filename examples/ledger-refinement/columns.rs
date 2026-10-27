pub mod columns {
use vstd::prelude::*;
extern crate ledger;
use ledger::ledger_api::*;
verus! {
pub struct Columns<const N: usize> {
    ids: [Option<u32>; N],
    values: [(u32, u32); N],
    used: u32,
    capacity: u32,
}
impl<const N: usize> Columns<N> {
    pub closed spec fn view(&self) -> LedgerView {
        LedgerView {
            slots: Seq::new(N as nat, |i: int| match self.ids@[i] {
                Some(id) => Some(Entry { id, owner: self.values@[i].0, amount: self.values@[i].1 }),
                None => None,
            }),
            used: self.used, capacity: self.capacity,
        }
    }
    pub open spec fn valid(&self) -> bool { valid_view::<N>(self.view()) }
    pub open spec fn reservations(&self) -> Map<u32, (u32, u32)> { slots_map(self.view().slots) }
}
pub fn to_model<const N: usize>(input: Columns<N>) -> (out: Ledger<N>)
    requires input.valid(),
    ensures out.valid(), same_view(out.view(), input.view()),
{
    reveal(Columns::view);
    let mut slots: [Option<Entry>; N] = [None; N];
    let mut i = 0usize;
    while i < N
        invariant i <= N, forall |k: int| 0 <= k < i ==> slots@[k] == input.view().slots[k],
        decreases N - i,
    {
        slots[i] = match input.ids[i] {
            Some(id) => Some(Entry { id, owner: input.values[i].0, amount: input.values[i].1 }),
            None => None,
        };
        i += 1;
    }
    assert(slots@ =~= input.view().slots);
    from_parts(slots, input.used, input.capacity)
}
pub fn from_model<const N: usize>(input: Ledger<N>) -> (out: Columns<N>)
    requires input.valid(),
    ensures out.valid(), same_view(out.view(), input.view()),
{
    reveal(Columns::view);
    let ghost original = input.view();
    let (slots, used, capacity) = into_parts(input);
    let mut ids: [Option<u32>; N] = [None; N];
    let mut values: [(u32, u32); N] = [(0, 0); N];
    let mut i = 0usize;
    while i < N
        invariant
            i <= N, slots@ == original.slots,
            forall |k: int| 0 <= k < i ==> match #[trigger] slots@[k] {
                Some(entry) => ids@[k] == Some(entry.id) && values@[k] == (entry.owner, entry.amount),
                None => ids@[k] == None,
            },
        decreases N - i,
    {
        match slots[i] {
            Some(entry) => { ids[i] = Some(entry.id); values[i] = (entry.owner, entry.amount); },
            None => { ids[i] = None; },
        }
        i += 1;
    }
    let out = Columns { ids, values, used, capacity };
    assert forall |k: int| 0 <= k < N implies out.view().slots[k] == original.slots[k] by {
        match #[trigger] slots@[k] { Some(entry) => {}, None => {}, }
    }
    assert(out.view().slots =~= original.slots);
    out
}
    pub fn new<const N: usize>(
        slots: [Option<ledger::ledger_api::Entry>; N],
        capacity: u32,
    ) -> (out: Columns<N>)
        requires N > 0,
        ensures
            out.valid(),
            out.view().used == 0,
            out.view().capacity == capacity,
            forall |i: int| 0 <= i < N ==> out.view().slots[i] == None,
            out.reservations() == Map::empty(),
    {
        from_model(ledger::ledger_api::new(slots, capacity))
    }

    pub fn reserve<const N: usize>(
        input: Columns<N>,
        id: u32,
        owner: u32,
        amount: u32,
    ) -> (out: (Columns<N>, Result<(), ledger::ledger_api::LedgerError>))
        requires N > 0 && input.valid(),
        ensures
            out.0.valid(),
            out.1 == ledger::ledger_api::reserve_result_view(input.view(), id, amount),
            match out.1 {
                Ok(()) => out.0.view().slots == ledger::ledger_api::slots_after_reserve(
                    input.view().slots,
                    ledger::ledger_api::Entry { id, owner, amount },
                )
                    && out.0.reservations()
                        == input.reservations().insert(id, (owner, amount))
                    && out.0.view().used == input.view().used + amount
                    && out.0.view().capacity == input.view().capacity,
                Err(_) => ledger::ledger_api::same_view(out.0.view(), input.view()),
            },
    {
        let (next, result) = ledger::ledger_api::reserve(to_model(input), id, owner, amount);
        (from_model(next), result)
    }

    pub fn release<const N: usize>(
        input: Columns<N>,
        id: u32,
        owner: u32,
    ) -> (out: (Columns<N>, Result<u32, ledger::ledger_api::LedgerError>))
        requires N > 0 && input.valid(),
        ensures
            out.0.valid(),
            out.1 == ledger::ledger_api::release_result_view(input.view(), id, owner),
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
                Err(_) => ledger::ledger_api::same_view(out.0.view(), input.view()),
            },
    {
        let (next, result) = ledger::ledger_api::release(to_model(input), id, owner);
        (from_model(next), result)
    }

    pub fn reserve_batch<const N: usize>(
        input: Columns<N>,
        requests: [ledger::ledger_api::Request; 3],
        count: u32,
    ) -> (out: (Columns<N>, ledger::ledger_api::BatchResponse))
        requires N > 0 && input.valid(),
        ensures
            out.0.valid(),
            out.1 == ledger::ledger_api::batch_model(input.view(), requests@, count).1,
            ledger::ledger_api::same_view(
                out.0.view(),
                ledger::ledger_api::batch_model(input.view(), requests@, count).0,
            ),
    {
        let (next, result) = ledger::ledger_api::reserve_batch(to_model(input), requests, count);
        (from_model(next), result)
    }

    pub fn apply_command<const N: usize>(
        input: Columns<N>,
        command: ledger::ledger_api::Command,
    ) -> (out: (Columns<N>, ledger::ledger_api::Response))
        requires N > 0 && input.valid(),
        ensures
            out.0.valid(),
            out.1 == ledger::ledger_api::apply_model(input.view(), command).1,
            ledger::ledger_api::same_view(
                out.0.view(),
                ledger::ledger_api::apply_model(input.view(), command).0,
            ),
    {
        let (next, result) = ledger::ledger_api::apply_command(to_model(input), command);
        (from_model(next), result)
    }

    pub fn run_commands<const N: usize, const M: usize>(
        initial: Columns<N>,
        commands: [ledger::ledger_api::Command; M],
        responses: &mut [ledger::ledger_api::Response; M],
    ) -> (final_ledger: Columns<N>)
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
        from_model(ledger::ledger_api::run_commands(to_model(initial), commands, responses))
    }

}

}
