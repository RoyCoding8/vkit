use vstd::prelude::*;

verus! {

pub mod ledger_api {

use super::*;

#[derive(Copy, Clone, PartialEq, Eq)]
pub struct Entry {
    pub id: u32,
    pub owner: u32,
    pub amount: u32,
}

#[derive(Copy, Clone, PartialEq, Eq)]
pub enum LedgerError {
    ZeroAmount,
    DuplicateId,
    Full,
    CapacityExceeded,
    Missing,
    OwnerMismatch,
}

#[derive(Copy, Clone)]
pub struct Ledger {
    slots: [Option<Entry>; 3],
    used: u32,
    capacity: u32,
}

#[derive(Copy, Clone, PartialEq, Eq)]
pub struct LedgerView {
    pub slots: [Option<Entry>; 3],
    pub used: u32,
    pub capacity: u32,
}

#[derive(Copy, Clone, PartialEq, Eq)]
pub struct Request {
    pub id: u32,
    pub owner: u32,
    pub amount: u32,
}

#[derive(Copy, Clone, PartialEq, Eq)]
pub enum Command {
    Reserve { id: u32, owner: u32, amount: u32 },
    Release { id: u32, owner: u32 },
}

#[derive(Copy, Clone, PartialEq, Eq)]
pub enum Response {
    Reserved(Result<(), LedgerError>),
    Released(Result<u32, LedgerError>),
}

#[derive(Copy, Clone, PartialEq, Eq)]
pub enum BatchResponse {
    Applied { count: u32 },
    TooManyRequests,
    RequestFailed { index: u32, error: LedgerError },
}

pub open spec fn amount_of(slot: Option<Entry>) -> nat {
    match slot {
        None => 0,
        Some(entry) => entry.amount as nat,
    }
}

pub open spec fn slots_used(slots: Seq<Option<Entry>>) -> nat
    recommends slots.len() == 3,
{
    amount_of(slots[0]) + amount_of(slots[1]) + amount_of(slots[2])
}

pub open spec fn distinct_pair(a: Option<Entry>, b: Option<Entry>) -> bool {
    match (a, b) {
        (Some(left), Some(right)) => left.id != right.id,
        _ => true,
    }
}

pub open spec fn slots_have_distinct_ids(slots: Seq<Option<Entry>>) -> bool
    recommends slots.len() == 3,
{
    distinct_pair(slots[0], slots[1])
        && distinct_pair(slots[0], slots[2])
        && distinct_pair(slots[1], slots[2])
}

pub open spec fn slots_have_positive_amounts(slots: Seq<Option<Entry>>) -> bool
    recommends slots.len() == 3,
{
    positive_amount(slots[0])
        && positive_amount(slots[1])
        && positive_amount(slots[2])
}

pub open spec fn positive_amount(slot: Option<Entry>) -> bool {
    match slot {
        Some(entry) => entry.amount > 0,
        None => true,
    }
}

pub open spec fn full_slots(slots: Seq<Option<Entry>>) -> bool
    recommends slots.len() == 3,
{
    match slots[0] {
        Some(_) => match slots[1] {
            Some(_) => match slots[2] {
                Some(_) => true,
                None => false,
            },
            None => false,
        },
        None => false,
    }
}

pub open spec fn slot_has_id(slot: Option<Entry>, id: u32) -> bool {
    match slot {
        Some(entry) => entry.id == id,
        None => false,
    }
}

pub open spec fn slots_have_id(slots: Seq<Option<Entry>>, id: u32) -> bool
    recommends slots.len() == 3,
{
    slot_has_id(slots[0], id)
        || slot_has_id(slots[1], id)
        || slot_has_id(slots[2], id)
}

pub open spec fn slot_is_empty(slot: Option<Entry>) -> bool {
    match slot {
        None => true,
        Some(_) => false,
    }
}

pub open spec fn slots_after_reserve(
    slots: Seq<Option<Entry>>,
    entry: Entry,
) -> Seq<Option<Entry>>
    recommends slots.len() == 3,
{
    if slot_is_empty(slots[0]) {
        slots.update(0, Some(entry))
    } else if slot_is_empty(slots[1]) {
        slots.update(1, Some(entry))
    } else {
        slots.update(2, Some(entry))
    }
}

pub open spec fn reserve_state(state: LedgerView, request: Request) -> LedgerView {
    let entry = Entry {
        id: request.id,
        owner: request.owner,
        amount: request.amount,
    };
    let slots = if slot_is_empty(state.slots[0]) {
        [Some(entry), state.slots[1], state.slots[2]]
    } else if slot_is_empty(state.slots[1]) {
        [state.slots[0], Some(entry), state.slots[2]]
    } else {
        [state.slots[0], state.slots[1], Some(entry)]
    };
    LedgerView {
        slots,
        used: (state.used + request.amount) as u32,
        capacity: state.capacity,
    }
}

pub open spec fn batch_model_from(
    original: LedgerView,
    current: LedgerView,
    requests: Seq<Request>,
    index: nat,
    remaining: nat,
) -> (LedgerView, BatchResponse)
    recommends requests.len() == 3, index + remaining <= 3,
    decreases remaining,
{
    if remaining == 0 {
        (current, BatchResponse::Applied { count: index as u32 })
    } else {
        let request = requests[index as int];
        match reserve_result(current, request.id, request.amount) {
            Err(error) => (
                original,
                BatchResponse::RequestFailed {
                    index: index as u32,
                    error,
                },
            ),
            Ok(()) => batch_model_from(
                original,
                reserve_state(current, request),
                requests,
                index + 1,
                (remaining - 1) as nat,
            ),
        }
    }
}

pub open spec fn batch_model(
    ledger: LedgerView,
    requests: Seq<Request>,
    count: u32,
) -> (LedgerView, BatchResponse)
    recommends requests.len() == 3,
{
    if count > 3 {
        (ledger, BatchResponse::TooManyRequests)
    } else {
        batch_model_from(ledger, ledger, requests, 0, count as nat)
    }
}

pub open spec fn release_state(state: LedgerView, id: u32) -> LedgerView {
    let slots = [
        if slot_has_id(state.slots[0], id) { None } else { state.slots[0] },
        if slot_has_id(state.slots[1], id) { None } else { state.slots[1] },
        if slot_has_id(state.slots[2], id) { None } else { state.slots[2] },
    ];
    let used = match first_entry(state.slots@, id) {
        Some(entry) => (state.used - entry.amount) as u32,
        None => state.used,
    };
    LedgerView {
        slots,
        used,
        capacity: state.capacity,
    }
}

pub open spec fn apply_model(ledger: LedgerView, command: Command) -> (LedgerView, Response) {
    match command {
        Command::Reserve { id, owner, amount } => {
            let result = reserve_result(ledger, id, amount);
            let next = match result {
                Ok(()) => reserve_state(ledger, Request { id, owner, amount }),
                Err(_) => ledger,
            };
            (next, Response::Reserved(result))
        }
        Command::Release { id, owner } => {
            let result = release_result_for(ledger, id, owner);
            let next = match result {
                Ok(_) => release_state(ledger, id),
                Err(_) => ledger,
            };
            (next, Response::Released(result))
        }
    }
}

pub open spec fn slots_map(slots: Seq<Option<Entry>>) -> Map<u32, (u32, u32)>
    recommends slots.len() == 3,
{
    let empty: Map<u32, (u32, u32)> = Map::empty();
    let first = match slots[0] {
        Some(entry) => empty.insert(entry.id, (entry.owner, entry.amount)),
        None => empty,
    };
    let second = match slots[1] {
        Some(entry) => first.insert(entry.id, (entry.owner, entry.amount)),
        None => first,
    };
    match slots[2] {
        Some(entry) => second.insert(entry.id, (entry.owner, entry.amount)),
        None => second,
    }
}

pub open spec fn same_view(a: LedgerView, b: LedgerView) -> bool {
    a.slots@ == b.slots@ && a.used == b.used && a.capacity == b.capacity
}

pub open spec fn same_state(a: Ledger, b: Ledger) -> bool {
    same_view(a.view(), b.view())
}

impl Ledger {
    pub closed spec fn view(&self) -> LedgerView {
        LedgerView {
            slots: self.slots,
            used: self.used,
            capacity: self.capacity,
        }
    }

    pub open spec fn reservations(&self) -> Map<u32, (u32, u32)> {
        slots_map(self.view().slots@)
    }

    pub open spec fn valid(&self) -> bool {
        let state = self.view();
        state.used as nat == slots_used(state.slots@)
            && state.used <= state.capacity
            && slots_have_distinct_ids(state.slots@)
            && slots_have_positive_amounts(state.slots@)
    }

    pub open spec fn is_empty_with_capacity(&self, capacity: u32) -> bool {
        let state = self.view();
        state.used == 0
            && state.capacity == capacity
            && state.slots@ == seq![None, None, None]
    }
}

pub fn new(capacity: u32) -> (ledger: Ledger)
    ensures
        ledger.valid(),
        ledger.is_empty_with_capacity(capacity),
{
    proof { reveal(Ledger::view); }
    Ledger {
        slots: [None, None, None],
        used: 0,
        capacity,
    }
}

pub fn reserve(
    ledger: Ledger,
    id: u32,
    owner: u32,
    amount: u32,
) -> (out: (Ledger, Result<(), LedgerError>))
    requires ledger.valid(),
    ensures
        out.0.valid(),
        out.1 == reserve_result(ledger.view(), id, amount),
        match out.1 {
            Ok(()) => same_view(
                out.0.view(),
                reserve_state(ledger.view(), Request { id, owner, amount }),
            ),
            Err(_) => same_state(out.0, ledger),
        },
    {
    proof {
        reveal(reserve_result);
        reveal(reserve_state);
        reveal(Ledger::view);
    }
    if amount == 0 {
        return (ledger, Err(LedgerError::ZeroAmount));
    }

    if let Some(entry) = ledger.slots[0] {
        if entry.id == id {
            return (ledger, Err(LedgerError::DuplicateId));
        }
    }
    if let Some(entry) = ledger.slots[1] {
        if entry.id == id {
            return (ledger, Err(LedgerError::DuplicateId));
        }
    }
    if let Some(entry) = ledger.slots[2] {
        if entry.id == id {
            return (ledger, Err(LedgerError::DuplicateId));
        }
    }

    if ledger.slots[0].is_some()
        && ledger.slots[1].is_some()
        && ledger.slots[2].is_some()
    {
        return (ledger, Err(LedgerError::Full));
    }

    if amount > ledger.capacity - ledger.used {
        return (ledger, Err(LedgerError::CapacityExceeded));
    }

    let entry = Entry { id, owner, amount };
    let mut next = ledger;
    if next.slots[0].is_none() {
        next.slots[0] = Some(entry);
    } else if next.slots[1].is_none() {
        next.slots[1] = Some(entry);
    } else {
        next.slots[2] = Some(entry);
    }
    next.used = next.used + amount;
    assert(
        reserve_state(ledger.view(), Request { id, owner, amount }).slots@
            == slots_after_reserve(
                ledger.view().slots@,
                Entry { id, owner, amount },
            )
    );
    assert(next.used == reserve_state(
        ledger.view(),
        Request { id, owner, amount },
    ).used);
    assert(next.capacity == reserve_state(
        ledger.view(),
        Request { id, owner, amount },
    ).capacity);
    assert(same_view(next.view(), reserve_state(
        ledger.view(),
        Request { id, owner, amount },
    )));
    (next, Ok(()))
}

pub open spec fn first_entry(slots: Seq<Option<Entry>>, id: u32) -> Option<Entry>
    recommends slots.len() == 3,
{
    if slot_has_id(slots[0], id) {
        slots[0]
    } else if slot_has_id(slots[1], id) {
        slots[1]
    } else if slot_has_id(slots[2], id) {
        slots[2]
    } else {
        None
    }
}

pub open spec fn reserve_result(
    ledger: LedgerView,
    id: u32,
    amount: u32,
) -> Result<(), LedgerError> {
    if amount == 0 {
        Err(LedgerError::ZeroAmount)
    } else if slots_have_id(ledger.slots@, id) {
        Err(LedgerError::DuplicateId)
    } else if full_slots(ledger.slots@) {
        Err(LedgerError::Full)
    } else if amount > ledger.capacity - ledger.used {
        Err(LedgerError::CapacityExceeded)
    } else {
        Ok(())
    }
}

pub open spec fn release_result(
    slots: Seq<Option<Entry>>,
    id: u32,
    owner: u32,
) -> Result<u32, LedgerError>
    recommends slots.len() == 3,
{
    match first_entry(slots, id) {
        None => Err(LedgerError::Missing),
        Some(entry) => {
            if entry.owner == owner {
                Ok(entry.amount)
            } else {
                Err(LedgerError::OwnerMismatch)
            }
        },
    }
}

pub open spec fn release_result_for(
    ledger: LedgerView,
    id: u32,
    owner: u32,
) -> Result<u32, LedgerError> {
    release_result(ledger.slots@, id, owner)
}

pub fn release(
    ledger: Ledger,
    id: u32,
    owner: u32,
) -> (out: (Ledger, Result<u32, LedgerError>))
    requires ledger.valid(),
    ensures
        out.0.valid(),
        out.1 == release_result_for(ledger.view(), id, owner),
        match out.1 {
            Ok(_) => same_view(out.0.view(), release_state(ledger.view(), id)),
            Err(_) => same_state(out.0, ledger),
        },
{
    proof {
        reveal(release_result_for);
        reveal(release_state);
        reveal(same_state);
        reveal(Ledger::view);
    }
    if let Some(entry) = ledger.slots[0] {
        if entry.id == id {
            if entry.owner != owner {
                return (ledger, Err(LedgerError::OwnerMismatch));
            }
            let mut next = ledger;
            next.slots[0] = None;
            next.used = next.used - entry.amount;
            assert(next.slots@ == release_state(ledger.view(), id).slots@);
            assert(next.used == release_state(ledger.view(), id).used);
            assert(next.capacity == release_state(ledger.view(), id).capacity);
            assert(same_view(next.view(), release_state(ledger.view(), id)));
            return (next, Ok(entry.amount));
        }
    }

    if let Some(entry) = ledger.slots[1] {
        if entry.id == id {
            if entry.owner != owner {
                return (ledger, Err(LedgerError::OwnerMismatch));
            }
            let mut next = ledger;
            next.slots[1] = None;
            next.used = next.used - entry.amount;
            assert(next.slots@ == release_state(ledger.view(), id).slots@);
            assert(next.used == release_state(ledger.view(), id).used);
            assert(next.capacity == release_state(ledger.view(), id).capacity);
            assert(same_view(next.view(), release_state(ledger.view(), id)));
            return (next, Ok(entry.amount));
        }
    }

    if let Some(entry) = ledger.slots[2] {
        if entry.id == id {
            if entry.owner != owner {
                return (ledger, Err(LedgerError::OwnerMismatch));
            }
            let mut next = ledger;
            next.slots[2] = None;
            next.used = next.used - entry.amount;
            assert(next.slots@ == release_state(ledger.view(), id).slots@);
            assert(next.used == release_state(ledger.view(), id).used);
            assert(next.capacity == release_state(ledger.view(), id).capacity);
            assert(same_view(next.view(), release_state(ledger.view(), id)));
            return (next, Ok(entry.amount));
        }
    }

    (ledger, Err(LedgerError::Missing))
}

pub fn apply_command(
    ledger: Ledger,
    command: Command,
) -> (out: (Ledger, Response))
    requires ledger.valid(),
    ensures
        out.0.valid(),
        out.1 == apply_model(ledger.view(), command).1,
        same_view(out.0.view(), apply_model(ledger.view(), command).0),
{
    proof {
        reveal(apply_model);
        reveal(reserve_result);
        reveal(reserve_state);
        reveal(release_result_for);
        reveal(release_state);
        reveal(same_state);
        reveal(Ledger::view);
    }
    match command {
        Command::Reserve { id, owner, amount } => {
            let (next, result) = reserve(ledger, id, owner, amount);
            (next, Response::Reserved(result))
        }
        Command::Release { id, owner } => {
            let (next, result) = release(ledger, id, owner);
            (next, Response::Released(result))
        }
    }
}

pub fn reserve_batch(
    ledger: Ledger,
    requests: [Request; 3],
    count: u32,
) -> (out: (Ledger, BatchResponse))
    requires ledger.valid(),
    ensures
        out.0.valid(),
        out.1 == batch_model(ledger.view(), requests@, count).1,
        same_view(out.0.view(), batch_model(ledger.view(), requests@, count).0),
{
    proof {
        reveal(batch_model);
        reveal_with_fuel(batch_model_from, 4);
        reveal(reserve_result);
        reveal(reserve_state);
        reveal(Ledger::view);
    }
    if count > 3 {
        return (ledger, BatchResponse::TooManyRequests);
    }

    let mut current = ledger;

    if count >= 1 {
        let (next, result) = reserve(
            current,
            requests[0].id,
            requests[0].owner,
            requests[0].amount,
        );
        match result {
            Err(error) => {
                return (
                    ledger,
                    BatchResponse::RequestFailed { index: 0, error },
                );
            }
            Ok(()) => current = next,
        }
    }

    if count >= 2 {
        let (next, result) = reserve(
            current,
            requests[1].id,
            requests[1].owner,
            requests[1].amount,
        );
        match result {
            Err(error) => {
                return (
                    ledger,
                    BatchResponse::RequestFailed { index: 1, error },
                );
            }
            Ok(()) => current = next,
        }
    }

    if count >= 3 {
        let (next, result) = reserve(
            current,
            requests[2].id,
            requests[2].owner,
            requests[2].amount,
        );
        match result {
            Err(error) => {
                return (
                    ledger,
                    BatchResponse::RequestFailed { index: 2, error },
                );
            }
            Ok(()) => current = next,
        }
    }

    (current, BatchResponse::Applied { count })
}

pub fn internal_proof_client() {
    let initial = new(7);

    assert(initial.valid());

    let (one, result) = reserve(initial, 11, 4, 3);
    assert(result == Ok(()));
    assert(one.valid());
    assert(one.used == 3);
    assert(one.reservations().dom().contains(11));

    let (tagged, response) = apply_command(
        initial,
        Command::Reserve {
            id: 20,
            owner: 2,
            amount: 2,
        },
    );
    assert(response == Response::Reserved(Ok(())));
    assert(tagged.used == 2);
    let (tagged_released, response) = apply_command(
        tagged,
        Command::Release { id: 20, owner: 2 },
    );
    assert(response == Response::Released(Ok(2)));
    proof { reveal(Ledger::view); }
    assert(same_state(tagged_released, initial));

    let (duplicate, result) = reserve(one, 11, 9, 1);
    assert(result == Err(LedgerError::DuplicateId));
    assert(same_state(duplicate, one));

    let (zero, result) = reserve(one, 11, 9, 0);
    assert(result == Err(LedgerError::ZeroAmount));
    assert(same_state(zero, one));

    let (over, result) = reserve(one, 12, 9, 5);
    assert(result == Err(LedgerError::CapacityExceeded));
    assert(same_state(over, one));

    let (two, result) = reserve(one, 12, 5, 1);
    assert(result == Ok(()));
    let (three, result) = reserve(two, 13, 6, 1);
    assert(result == Ok(()));

    let batch_requests = [
        Request { id: 14, owner: 8, amount: 1 },
        Request { id: 15, owner: 9, amount: 1 },
        Request { id: 16, owner: 10, amount: 1 },
    ];
    let (batched, batch_response) = reserve_batch(one, batch_requests, 2);
    proof {
        reveal(batch_model);
        reveal_with_fuel(batch_model_from, 4);
        reveal(reserve_result);
        reveal(reserve_state);
    }
    assert(batch_response == BatchResponse::Applied { count: 2 });
    assert(batched.valid());
    assert(batched.used == 5);
    assert(batched.reservations().dom().contains(14));
    assert(batched.reservations().dom().contains(15));
    assert(!batched.reservations().dom().contains(16));

    let failing_requests = [
        Request { id: 14, owner: 8, amount: 1 },
        Request { id: 11, owner: 9, amount: 1 },
        Request { id: 15, owner: 10, amount: 1 },
    ];
    let (rolled_back, batch_response) = reserve_batch(one, failing_requests, 3);
    assert(batch_response == BatchResponse::RequestFailed {
        index: 1,
        error: LedgerError::DuplicateId,
    });
    assert(same_state(rolled_back, one));

    let (too_many, batch_response) = reserve_batch(one, batch_requests, 4);
    assert(batch_response == BatchResponse::TooManyRequests);
    assert(same_state(too_many, one));

    let (full_duplicate, result) = reserve(three, 11, 8, 8);
    assert(result == Err(LedgerError::DuplicateId));
    assert(same_state(full_duplicate, three));
    let (full, result) = reserve(three, 14, 7, 8);
    assert(result == Err(LedgerError::Full));
    assert(same_state(full, three));

    let (released, result) = release(one, 11, 4);
    assert(result == Ok(3));
    assert(released.valid());
    assert(released.used == 0);
    assert(!released.reservations().dom().contains(11));

    let (wrong_owner, result) = release(one, 11, 5);
    assert(result == Err(LedgerError::OwnerMismatch));
    assert(same_state(wrong_owner, one));

    let (missing, result) = release(one, 12, 4);
    assert(result == Err(LedgerError::Missing));
    assert(same_state(missing, one));

    let (released_middle, result) = release(two, 12, 5);
    assert(result == Ok(1));
    assert(released_middle.valid());
    let (released_last, result) = release(three, 13, 6);
    assert(result == Ok(1));
    assert(released_last.valid());
}

}

mod downstream {
    use super::ledger_api::*;

    pub fn external_caller() {
        let empty = new(6);
        assert(empty.valid());
        assert(empty.view().used == 0);
        assert(empty.view().capacity == 6);

        let (held, response) = reserve(empty, 37, 9, 2);
        assert(response == Ok(()));
        assert(held.view().used == 2);
        assert(held.reservations().dom().contains(37));
        assert(held.reservations()[37] == (9, 2));

        let (empty_again, response) = release(held, 37, 9);
        assert(response == Ok(2));
        assert(empty_again.view().used == 0);
        assert(!empty_again.reservations().dom().contains(37));
    }
}

pub fn main() {
    downstream::external_caller()
}

}
