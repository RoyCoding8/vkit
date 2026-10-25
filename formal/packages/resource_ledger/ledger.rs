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
pub struct Ledger<const N: usize> {
    slots: [Option<Entry>; N],
    used: u32,
    capacity: u32,
}
pub struct LedgerView {
    pub slots: Seq<Option<Entry>>,
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
    Batch { requests: [Request; 3], count: u32 },
}

#[derive(Copy, Clone, PartialEq, Eq)]
pub enum Response {
    Reserved(Result<(), LedgerError>),
    Released(Result<u32, LedgerError>),
    Batched(BatchResponse),
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

pub open spec fn slots_used_from(slots: Seq<Option<Entry>>, n: int) -> nat
    recommends 0 <= n <= slots.len(),
    decreases n,
{
    if n <= 0 {
        0
    } else {
        amount_of(slots[n - 1]) + slots_used_from(slots, n - 1)
    }
}

pub open spec fn slots_used(slots: Seq<Option<Entry>>) -> nat {
    slots_used_from(slots, slots.len() as int)
}

pub open spec fn slots_count_from(slots: Seq<Option<Entry>>, n: int) -> nat
    recommends 0 <= n <= slots.len(),
    decreases n,
{
    if n <= 0 {
        0
    } else {
        (if slots[n - 1].is_some() { 1nat } else { 0nat })
            + slots_count_from(slots, n - 1)
    }
}

pub open spec fn slots_count(slots: Seq<Option<Entry>>) -> nat {
    slots_count_from(slots, slots.len() as int)
}

pub open spec fn reservations_from(
    slots: Seq<Option<Entry>>,
    n: int,
) -> Map<u32, (u32, u32)>
    recommends 0 <= n <= slots.len(),
    decreases n,
{
    if n <= 0 {
        Map::empty()
    } else {
        let before = reservations_from(slots, n - 1);
        match slots[n - 1] {
            None => before,
            Some(entry) => before.insert(entry.id, (entry.owner, entry.amount)),
        }
    }
}

pub open spec fn slots_map(slots: Seq<Option<Entry>>) -> Map<u32, (u32, u32)> {
    reservations_from(slots, slots.len() as int)
}

pub open spec fn slot_has_id(slot: Option<Entry>, id: u32) -> bool {
    match slot {
        None => false,
        Some(entry) => entry.id == id,
    }
}

pub open spec fn slots_have_id_prefix(
    slots: Seq<Option<Entry>>,
    n: int,
    id: u32,
) -> bool
    recommends 0 <= n <= slots.len(),
{
    exists |i: int| 0 <= i < n && slot_has_id(slots[i], id)
}
pub open spec fn slots_have_id(slots: Seq<Option<Entry>>, id: u32) -> bool {
    slots_have_id_prefix(slots, slots.len() as int, id)
}

pub open spec fn positive_amount(slot: Option<Entry>) -> bool {
    match slot {
        None => true,
        Some(entry) => entry.amount > 0,
    }
}

pub open spec fn slots_positive(slots: Seq<Option<Entry>>) -> bool {
    forall |i: int| 0 <= i < slots.len() ==> positive_amount(slots[i])
}

pub open spec fn distinct_pair(a: Option<Entry>, b: Option<Entry>) -> bool {
    match (a, b) {
        (Some(left), Some(right)) => left.id != right.id,
        _ => true,
    }
}

pub open spec fn slots_unique_ids(slots: Seq<Option<Entry>>) -> bool {
    forall |i: int, j: int| 0 <= i < j < slots.len() ==> distinct_pair(slots[i], slots[j])
}

pub open spec fn full_slots(slots: Seq<Option<Entry>>) -> bool {
    slots_count(slots) == slots.len()
}

pub open spec fn first_empty_prefix(
    slots: Seq<Option<Entry>>,
    n: int,
) -> Option<int>
    recommends 0 <= n <= slots.len(),
    decreases n,
{
    if n <= 0 {
        None
    } else {
        match first_empty_prefix(slots, n - 1) {
            Some(i) => Some(i),
            None => if slots[n - 1].is_none() { Some(n - 1) } else { None },
        }
    }
}

pub open spec fn first_empty(slots: Seq<Option<Entry>>) -> Option<int> {
    first_empty_prefix(slots, slots.len() as int)
}

pub open spec fn slots_after_reserve(
    slots: Seq<Option<Entry>>,
    entry: Entry,
) -> Seq<Option<Entry>> {
    match first_empty(slots) {
        Some(i) => slots.update(i, Some(entry)),
        None => slots,
    }
}

pub open spec fn first_index_prefix(
    slots: Seq<Option<Entry>>,
    n: int,
    id: u32,
) -> Option<int>
    recommends 0 <= n <= slots.len(),
    decreases n,
{
    if n <= 0 {
        None
    } else {
        match first_index_prefix(slots, n - 1, id) {
            Some(i) => Some(i),
            None => if slot_has_id(slots[n - 1], id) { Some(n - 1) } else { None },
        }
    }
}

pub open spec fn first_index(slots: Seq<Option<Entry>>, id: u32) -> Option<int> {
    first_index_prefix(slots, slots.len() as int, id)
}

pub open spec fn first_entry_prefix(
    slots: Seq<Option<Entry>>,
    n: int,
    id: u32,
) -> Option<Entry>
    recommends 0 <= n <= slots.len(),
    decreases n,
{
    if n <= 0 {
        None
    } else {
        match first_entry_prefix(slots, n - 1, id) {
            Some(entry) => Some(entry),
            None => match slots[n - 1] {
                Some(entry) => if entry.id == id { Some(entry) } else { None },
                None => None,
            },
        }
    }
}

pub open spec fn first_entry(slots: Seq<Option<Entry>>, id: u32) -> Option<Entry> {
    first_entry_prefix(slots, slots.len() as int, id)
}

pub open spec fn slots_after_release(slots: Seq<Option<Entry>>, id: u32) -> Seq<Option<Entry>> {
    match first_index(slots, id) {
        Some(i) => slots.update(i, None),
        None => slots,
    }
}
pub proof fn first_index_prefix_extends(
    slots: Seq<Option<Entry>>,
    id: u32,
    prefix: int,
    total: int,
    index: int,
)
    requires
        0 <= prefix <= total <= slots.len(),
        first_index_prefix(slots, prefix, id) == Some(index),
    ensures first_index_prefix(slots, total, id) == Some(index),
    decreases total - prefix,
{
    if prefix < total {
        first_index_prefix_extends(slots, id, prefix, total - 1, index);
        reveal(first_index_prefix);
    }
}

pub proof fn first_entry_prefix_extends(
    slots: Seq<Option<Entry>>,
    id: u32,
    prefix: int,
    total: int,
    entry: Entry,
)
    requires
        0 <= prefix <= total <= slots.len(),
        first_entry_prefix(slots, prefix, id) == Some(entry),
    ensures first_entry_prefix(slots, total, id) == Some(entry),
    decreases total - prefix,
{
    if prefix < total {
        first_entry_prefix_extends(slots, id, prefix, total - 1, entry);
        reveal(first_entry_prefix);
    }
}
pub proof fn slots_used_none_prefix(slots: Seq<Option<Entry>>, n: int)
    requires
        0 <= n <= slots.len(),
        forall |j: int| 0 <= j < n ==> slots[j].is_none(),
    ensures slots_used_from(slots, n) == 0,
    decreases n,
{
    if n > 0 {
        slots_used_none_prefix(slots, n - 1);
        reveal(slots_used_from);
        assert(amount_of(slots[n - 1]) == 0);
    }
}

pub proof fn slots_count_none_prefix(slots: Seq<Option<Entry>>, n: int)
    requires
        0 <= n <= slots.len(),
        forall |j: int| 0 <= j < n ==> slots[j].is_none(),
    ensures slots_count_from(slots, n) == 0,
    decreases n,
{
    if n > 0 {
        slots_count_none_prefix(slots, n - 1);
        reveal(slots_count_from);
        assert(!slots[n - 1].is_some());
    }
}

pub proof fn reservations_none_prefix(slots: Seq<Option<Entry>>, n: int)
    requires
        0 <= n <= slots.len(),
        forall |j: int| 0 <= j < n ==> slots[j].is_none(),
    ensures reservations_from(slots, n) == Map::empty(),
    decreases n,
{
    if n > 0 {
        reservations_none_prefix(slots, n - 1);
        reveal(reservations_from);
    }
}

pub proof fn slots_used_unchanged_before_update(
    slots: Seq<Option<Entry>>,
    index: int,
    replacement: Option<Entry>,
    n: int,
)
    requires 0 <= n <= index <= slots.len(),
    ensures
        slots_used_from(slots.update(index, replacement), n) == slots_used_from(slots, n),
    decreases n,
{
    if n > 0 {
        slots_used_unchanged_before_update(slots, index, replacement, n - 1);
        reveal(slots_used_from);
        assert(n - 1 < index);
        assert(slots.update(index, replacement)[n - 1] == slots[n - 1]);
    }
}

pub proof fn slots_count_unchanged_before_update(
    slots: Seq<Option<Entry>>,
    index: int,
    replacement: Option<Entry>,
    n: int,
)
    requires 0 <= n <= index <= slots.len(),
    ensures
        slots_count_from(slots.update(index, replacement), n) == slots_count_from(slots, n),
    decreases n,
{
    if n > 0 {
        slots_count_unchanged_before_update(slots, index, replacement, n - 1);
        reveal(slots_count_from);
        assert(n - 1 < index);
        assert(slots.update(index, replacement)[n - 1] == slots[n - 1]);
    }
}

pub proof fn reservations_unchanged_before_update(
    slots: Seq<Option<Entry>>,
    index: int,
    replacement: Option<Entry>,
    n: int,
)
    requires 0 <= n <= index <= slots.len(),
    ensures
        reservations_from(slots.update(index, replacement), n) == reservations_from(slots, n),
    decreases n,
{
    if n > 0 {
        reservations_unchanged_before_update(slots, index, replacement, n - 1);
        reveal(reservations_from);
        assert(n - 1 < index);
        assert(slots.update(index, replacement)[n - 1] == slots[n - 1]);
    }
}
pub proof fn slots_used_update_none_prefix(
    slots: Seq<Option<Entry>>,
    index: int,
    entry: Entry,
    n: int,
)
    requires
        0 <= index < n <= slots.len(),
        slots[index].is_none(),
    ensures
        slots_used_from(slots.update(index, Some(entry)), n)
            == slots_used_from(slots, n) + entry.amount as nat,
    decreases n - index,
{
    if index + 1 < n {
        slots_used_update_none_prefix(slots, index, entry, n - 1);
        reveal(slots_used_from);
        assert(slots[n - 1] == slots.update(index, Some(entry))[n - 1]);
    } else {
        assert(index + 1 == n);
        slots_used_unchanged_before_update(slots, index, Some(entry), index);
        reveal(slots_used_from);
        assert(slots.update(index, Some(entry))[index] == Some(entry));
        assert(amount_of(Some(entry)) == entry.amount as nat);
    }
}
pub proof fn slots_count_update_none_prefix(
    slots: Seq<Option<Entry>>,
    index: int,
    entry: Entry,
    n: int,
)
    requires
        0 <= index < n <= slots.len(),
        slots[index].is_none(),
    ensures
        slots_count_from(slots.update(index, Some(entry)), n)
            == slots_count_from(slots, n) + 1,
    decreases n - index,
{
    if index + 1 < n {
        slots_count_update_none_prefix(slots, index, entry, n - 1);
        reveal(slots_count_from);
        assert(slots[n - 1] == slots.update(index, Some(entry))[n - 1]);
    } else {
        assert(index + 1 == n);
        slots_count_unchanged_before_update(slots, index, Some(entry), index);
        reveal(slots_count_from);
        assert(slots.update(index, Some(entry))[index] == Some(entry));
    }
}
pub proof fn map_insert_commutes(
    map: Map<u32, (u32, u32)>,
    id1: u32,
    value1: (u32, u32),
    id2: u32,
    value2: (u32, u32),
)
    requires id1 != id2,
    ensures
        map.insert(id1, value1).insert(id2, value2)
            == map.insert(id2, value2).insert(id1, value1),
{
}

pub proof fn reservations_update_none_prefix(
    slots: Seq<Option<Entry>>,
    index: int,
    entry: Entry,
    n: int,
)
    requires
        0 <= index < n <= slots.len(),
        slots[index].is_none(),
        !slots_have_id(slots, entry.id),
    ensures
        reservations_from(slots.update(index, Some(entry)), n)
            == reservations_from(slots, n).insert(entry.id, (entry.owner, entry.amount)),
    decreases n - index,
{
    if index + 1 < n {
        reservations_update_none_prefix(slots, index, entry, n - 1);
        reveal(reservations_from);
        match slots[n - 1] {
            None => {},
            Some(last) => {
                assert(last.id != entry.id);
                map_insert_commutes(
                    reservations_from(slots, n - 1),
                    entry.id,
                    (entry.owner, entry.amount),
                    last.id,
                    (last.owner, last.amount),
                );
            },
        }
    } else {
        assert(index + 1 == n);
        reservations_unchanged_before_update(slots, index, Some(entry), index);
        reveal(reservations_from);
        assert(slots.update(index, Some(entry))[index] == Some(entry));
    }
}
pub proof fn slots_used_remove_some_prefix(
    slots: Seq<Option<Entry>>,
    index: int,
    n: int,
)
    requires
        0 <= index < n <= slots.len(),
        slots[index].is_some(),
    ensures
        slots_used_from(slots, n)
            == slots_used_from(slots.update(index, None), n) + amount_of(slots[index]),
    decreases n - index,
{
    if index + 1 < n {
        slots_used_remove_some_prefix(slots, index, n - 1);
        reveal(slots_used_from);
        assert(slots[n - 1] == slots.update(index, None)[n - 1]);
    } else {
        assert(index + 1 == n);
        slots_used_unchanged_before_update(slots, index, None, index);
        reveal(slots_used_from);
        assert(slots.update(index, None)[index] == None);
    }
}

pub proof fn slots_count_remove_some_prefix(
    slots: Seq<Option<Entry>>,
    index: int,
    n: int,
)
    requires
        0 <= index < n <= slots.len(),
        slots[index].is_some(),
    ensures
        slots_count_from(slots, n)
            == slots_count_from(slots.update(index, None), n) + 1,
    decreases n - index,
{
    if index + 1 < n {
        slots_count_remove_some_prefix(slots, index, n - 1);
        reveal(slots_count_from);
        assert(slots[n - 1] == slots.update(index, None)[n - 1]);
    } else {
        assert(index + 1 == n);
        slots_count_unchanged_before_update(slots, index, None, index);
        reveal(slots_count_from);
        assert(slots.update(index, None)[index] == None);
    }
}

pub proof fn slots_positive_remove_none(slots: Seq<Option<Entry>>, index: int)
    requires
        slots_positive(slots),
        0 <= index < slots.len(),
    ensures slots_positive(slots.update(index, None)),
{
    assert forall |i: int| 0 <= i < slots.len()
        implies positive_amount(slots.update(index, None)[i]) by {
        if i == index {
            assert(slots.update(index, None)[i] == None);
        } else {
            assert(slots.update(index, None)[i] == slots[i]);
        }
    }
}

pub proof fn slots_unique_remove_none(slots: Seq<Option<Entry>>, index: int)
    requires
        slots_unique_ids(slots),
        0 <= index < slots.len(),
    ensures slots_unique_ids(slots.update(index, None)),
{
    assert forall |i: int, j: int| 0 <= i < j < slots.len()
        implies distinct_pair(slots.update(index, None)[i], slots.update(index, None)[j]) by {
        if i == index || j == index {
            if i == index {
                assert(slots.update(index, None)[i] == None);
            } else {
                assert(slots.update(index, None)[j] == None);
            }
        } else {
            assert(slots.update(index, None)[i] == slots[i]);
            assert(slots.update(index, None)[j] == slots[j]);
        }
    }
}

pub proof fn reservations_dom_iff(slots: Seq<Option<Entry>>, n: int, id: u32)
    requires 0 <= n <= slots.len(),
    ensures reservations_from(slots, n).dom().contains(id) == slots_have_id_prefix(slots, n, id),
    decreases n,
{
    if n > 0 {
        reservations_dom_iff(slots, n - 1, id);
        reveal(reservations_from);
        reveal(slots_have_id_prefix);
        match slots[n - 1] {
            None => {},
            Some(entry) => {},
        }
    }
}

pub proof fn map_insert_remove_absent(
    map: Map<u32, (u32, u32)>,
    id: u32,
    value: (u32, u32),
)
    requires !map.dom().contains(id),
    ensures map.insert(id, value).remove(id) == map,
{
}

pub proof fn map_remove_insert_commutes(
    map: Map<u32, (u32, u32)>,
    removed: u32,
    inserted: u32,
    value: (u32, u32),
)
    requires removed != inserted,
    ensures map.remove(removed).insert(inserted, value)
        == map.insert(inserted, value).remove(removed),
{
}

pub proof fn reservations_remove_some_prefix(
    slots: Seq<Option<Entry>>,
    index: int,
    entry: Entry,
    n: int,
)
    requires
        0 <= index < n <= slots.len(),
        slots[index] == Some(entry),
        slots_unique_ids(slots),
    ensures
        reservations_from(slots.update(index, None), n)
            == reservations_from(slots, n).remove(entry.id),
    decreases n - index,
{
    if index + 1 < n {
        reservations_remove_some_prefix(slots, index, entry, n - 1);
        reveal(reservations_from);
        match slots[n - 1] {
            None => {},
            Some(last) => {
                assert(distinct_pair(slots[index], slots[n - 1]));
                assert(last.id != entry.id);
                map_remove_insert_commutes(
                    reservations_from(slots, n - 1),
                    entry.id,
                    last.id,
                    (last.owner, last.amount),
                );
            },
        }
    } else {
        assert(index + 1 == n);
        slots_unique_ids(slots);
        assert forall |j: int| 0 <= j < index
            implies !slot_has_id(slots[j], entry.id) by {
            assert(distinct_pair(slots[j], slots[index]));
        }
        assert(!slots_have_id_prefix(slots, index, entry.id));
        reservations_dom_iff(slots, index, entry.id);
        assert(!reservations_from(slots, index).dom().contains(entry.id));
        map_insert_remove_absent(
            reservations_from(slots, index),
            entry.id,
            (entry.owner, entry.amount),
        );
        reservations_unchanged_before_update(slots, index, None, index);
        reveal(reservations_from);
        assert(slots.update(index, None)[index] == None);
    }
}
impl<const N: usize> Ledger<N> {
    pub closed spec fn view(&self) -> LedgerView {
        LedgerView { slots: self.slots@, used: self.used, capacity: self.capacity }
    }
    pub open spec fn reservations(&self) -> Map<u32, (u32, u32)> {
        slots_map(self.view().slots)
    }

    pub open spec fn valid(&self) -> bool {
        let state = self.view();
        state.slots.len() == N as int
            && state.used as nat == slots_used(state.slots)
            && state.used <= state.capacity
            && slots_count(state.slots) <= N as nat
            && slots_unique_ids(state.slots)
            && slots_positive(state.slots)
    }
}

pub open spec fn same_view(a: LedgerView, b: LedgerView) -> bool {
    a.slots == b.slots && a.used == b.used && a.capacity == b.capacity
}
pub open spec fn same_state<const N: usize>(a: Ledger<N>, b: Ledger<N>) -> bool {
    same_view(a.view(), b.view())
}
pub open spec fn reserve_result_view(
    state: LedgerView,
    id: u32,
    amount: u32,
) -> Result<(), LedgerError> {
    if amount == 0 {
        Err(LedgerError::ZeroAmount)
    } else if slots_have_id(state.slots, id) {
        Err(LedgerError::DuplicateId)
    } else if full_slots(state.slots) {
        Err(LedgerError::Full)
    } else if amount > state.capacity - state.used {
        Err(LedgerError::CapacityExceeded)
    } else {
        Ok(())
    }
}

pub open spec fn reserve_state(state: LedgerView, request: Request) -> LedgerView {
    let entry = Entry { id: request.id, owner: request.owner, amount: request.amount };
    LedgerView {
        slots: slots_after_reserve(state.slots, entry),
        used: (state.used + request.amount) as u32,
        capacity: state.capacity,
    }
}

pub open spec fn release_result_view(
    state: LedgerView,
    id: u32,
    owner: u32,
) -> Result<u32, LedgerError> {
    match first_entry(state.slots, id) {
        None => Err(LedgerError::Missing),
        Some(entry) => if entry.owner == owner {
            Ok(entry.amount)
        } else {
            Err(LedgerError::OwnerMismatch)
        },
    }
}

pub open spec fn release_state(state: LedgerView, id: u32) -> LedgerView {
    let used = match first_entry(state.slots, id) {
        Some(entry) => (state.used - entry.amount) as u32,
        None => state.used,
    };
    LedgerView {
        slots: slots_after_release(state.slots, id),
        used,
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
        match reserve_result_view(current, request.id, request.amount) {
            Err(error) => (
                original,
                BatchResponse::RequestFailed { index: index as u32, error },
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
    state: LedgerView,
    requests: Seq<Request>,
    count: u32,
) -> (LedgerView, BatchResponse)
    recommends requests.len() == 3,
{
    if count > 3 {
        (state, BatchResponse::TooManyRequests)
    } else {
        batch_model_from(state, state, requests, 0, count as nat)
    }
}

pub open spec fn apply_model(
    state: LedgerView,
    command: Command,
) -> (LedgerView, Response) {
    match command {
        Command::Reserve { id, owner, amount } => {
            let result = reserve_result_view(state, id, amount);
            let next = match result {
                Ok(()) => reserve_state(state, Request { id, owner, amount }),
                Err(_) => state,
            };
            (next, Response::Reserved(result))
        }
        Command::Release { id, owner } => {
            let result = release_result_view(state, id, owner);
            let next = match result {
                Ok(_) => release_state(state, id),
                Err(_) => state,
            };
            (next, Response::Released(result))
        }
        Command::Batch { requests, count } => {
            let (next, result) = batch_model(state, requests@, count);
            (next, Response::Batched(result))
        }
    }
}

pub proof fn apply_model_respects_view(
    left: LedgerView,
    right: LedgerView,
    command: Command,
)
    requires same_view(left, right),
    ensures
        same_view(apply_model(left, command).0, apply_model(right, command).0),
        apply_model(left, command).1 == apply_model(right, command).1,
{
    reveal(same_view);
    reveal(apply_model);
    reveal(reserve_result_view);
    reveal(reserve_state);
    reveal(release_result_view);
    reveal(release_state);
    reveal(batch_model);
    reveal_with_fuel(batch_model_from, 4);
}

pub open spec fn reserve_result<const N: usize>(
    ledger: Ledger<N>,
    id: u32,
    amount: u32,
) -> Result<(), LedgerError> {
    reserve_result_view(ledger.view(), id, amount)
}
pub open spec fn release_result_for<const N: usize>(
    ledger: Ledger<N>,
    id: u32,
    owner: u32,
) -> Result<u32, LedgerError> {
    release_result_view(ledger.view(), id, owner)
}
pub fn new<const N: usize>(mut slots: [Option<Entry>; N], capacity: u32) -> (ledger: Ledger<N>)
    requires N > 0,
    ensures
        ledger.valid(),
        ledger.view().used == 0,
        ledger.view().capacity == capacity,
        forall |i: int| 0 <= i < N ==> ledger.view().slots[i] == None,
        ledger.reservations() == Map::empty(),
{
    let mut i: usize = 0;
    while i < N
        invariant
            i <= N,
            forall |j: int| 0 <= j < i as int ==> slots@[j].is_none(),
        decreases N - i,
    {
        slots[i] = None;
        i += 1;
    }
    proof {
        slots_used_none_prefix(slots@, N as int);
        slots_count_none_prefix(slots@, N as int);
        reservations_none_prefix(slots@, N as int);
    }
    Ledger { slots, used: 0, capacity }
}

pub fn reserve<const N: usize>(
    ledger: Ledger<N>,
    id: u32,
    owner: u32,
    amount: u32,
) -> (out: (Ledger<N>, Result<(), LedgerError>))
    requires N > 0 && ledger.valid(),
    ensures
        out.0.valid(),
        out.1 == reserve_result(ledger, id, amount),
        match out.1 {
            Ok(()) => out.0.view().slots == slots_after_reserve(
                ledger.view().slots,
                Entry { id, owner, amount },
            )
                && out.0.reservations()
                    == ledger.reservations().insert(id, (owner, amount))
                && out.0.view().used == ledger.view().used + amount
                && out.0.view().capacity == ledger.view().capacity,
            Err(_) => same_state(out.0, ledger),
        },
{
    if amount == 0 {
        return (ledger, Err(LedgerError::ZeroAmount));
    }

    let mut i: usize = 0;
    let mut duplicate = false;
    let mut empty: Option<usize> = None;
    let mut occupied: usize = 0;
    while i < N
        invariant
            i <= N,
            duplicate == slots_have_id_prefix(ledger.slots@, i as int, id),
            match empty {
                Some(index) => index < i
                    && ledger.slots@[index as int].is_none()
                    && first_empty_prefix(ledger.slots@, i as int) == Some(index as int),
                None => first_empty_prefix(ledger.slots@, i as int) == None,
            },
            occupied as nat == slots_count_from(ledger.slots@, i as int),
            match empty {
                Some(_) => occupied < i,
                None => occupied == i,
            },
        decreases N - i,
    {
        let slot = ledger.slots[i];
        if let Some(entry) = slot {
            if entry.id == id {
                duplicate = true;
            }
            occupied += 1;
        }
        if empty.is_none() && slot.is_none() {
            empty = Some(i);
        }
        i += 1;
    }

    if duplicate {
        return (ledger, Err(LedgerError::DuplicateId));
    }
    assert(!slots_have_id(ledger.slots@, id));
    if empty.is_none() {
        return (ledger, Err(LedgerError::Full));
    }
    if amount > ledger.capacity - ledger.used {
        return (ledger, Err(LedgerError::CapacityExceeded));
    }

    let entry = Entry { id, owner, amount };
    let index = empty.unwrap();
    assert(index < N);
    assert(ledger.slots@[index as int].is_none());
    let mut next = ledger;
    next.slots[index] = Some(entry);
    next.used = next.used + amount;
    proof {
        slots_used_update_none_prefix(ledger.slots@, index as int, entry, N as int);
        slots_count_update_none_prefix(ledger.slots@, index as int, entry, N as int);
        reservations_update_none_prefix(ledger.slots@, index as int, entry, N as int);
    }
    (next, Ok(()))
}

pub fn release<const N: usize>(
    ledger: Ledger<N>,
    id: u32,
    owner: u32,
) -> (out: (Ledger<N>, Result<u32, LedgerError>))
    requires N > 0 && ledger.valid(),
    ensures
        out.0.valid(),
        out.1 == release_result_for(ledger, id, owner),
        match out.1 {
            Ok(amount) => out.0.view().slots == slots_after_release(ledger.view().slots, id)
                && out.0.reservations() == ledger.reservations().remove(id)
                && out.0.view().used == ledger.view().used - amount
                && out.0.view().capacity == ledger.view().capacity
                && slots_count(out.0.view().slots) + 1 == slots_count(ledger.view().slots),
            Err(_) => same_state(out.0, ledger),
        },
{
    let mut i: usize = 0;
    while i < N
        invariant
            i <= N,
            ledger.valid(),
            first_index_prefix(ledger.slots@, i as int, id) == None,
            first_entry_prefix(ledger.slots@, i as int, id) == None,
        decreases N - i,
    {
        let slot = ledger.slots[i];
        if let Some(entry) = slot {
            if entry.id == id {
                proof {
                    assert(ledger.slots@[i as int] == Some(entry));
                    assert(first_index_prefix(ledger.slots@, (i + 1) as int, id) == Some(i as int));
                    assert(first_entry_prefix(ledger.slots@, (i + 1) as int, id) == Some(entry));
                    first_index_prefix_extends(
                        ledger.slots@, id, (i + 1) as int, N as int, i as int,
                    );
                    first_entry_prefix_extends(
                        ledger.slots@, id, (i + 1) as int, N as int, entry,
                    );
                }
                assert(ledger.valid());
                if entry.owner != owner {
                    proof { reveal(release_result_for); }
                    return (ledger, Err(LedgerError::OwnerMismatch));
                }
                proof {
                    reveal(Ledger::valid);
                    reveal(Ledger::view);
                    assert(ledger.used as nat == slots_used(ledger.slots@));
                    slots_used_remove_some_prefix(ledger.slots@, i as int, N as int);
                    assert(amount_of(ledger.slots@[i as int]) == entry.amount as nat);
                    assert(ledger.used as nat >= entry.amount as nat);
                    assert(ledger.used >= entry.amount);
                }
                let mut next = ledger;
                next.slots[i] = None;
                next.used = next.used - entry.amount;
                proof {
                    slots_count_remove_some_prefix(ledger.slots@, i as int, N as int);
                    slots_unique_remove_none(ledger.slots@, i as int);
                    slots_positive_remove_none(ledger.slots@, i as int);
                    reservations_remove_some_prefix(
                        ledger.slots@, i as int, entry, N as int,
                    );
                    reveal(release_result_for);
                }
                return (next, Ok(entry.amount));
            }
        }
        i += 1;
    }
    proof { reveal(release_result_for); }
    (ledger, Err(LedgerError::Missing))
}
pub fn reserve_batch<const N: usize>(
    ledger: Ledger<N>,
    requests: [Request; 3],
    count: u32,
) -> (out: (Ledger<N>, BatchResponse))
    requires N > 0 && ledger.valid(),
    ensures
        out.0.valid(),
        out.1 == batch_model(ledger.view(), requests@, count).1,
        same_view(out.0.view(), batch_model(ledger.view(), requests@, count).0),
{
    proof {
        reveal(batch_model);
        reveal_with_fuel(batch_model_from, 4);
        reveal(reserve_result_view);
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
            current, requests[0].id, requests[0].owner, requests[0].amount,
        );
        match result {
            Err(error) => return (
                ledger, BatchResponse::RequestFailed { index: 0, error },
            ),
            Ok(()) => current = next,
        }
    }
    if count >= 2 {
        let (next, result) = reserve(
            current, requests[1].id, requests[1].owner, requests[1].amount,
        );
        match result {
            Err(error) => return (
                ledger, BatchResponse::RequestFailed { index: 1, error },
            ),
            Ok(()) => current = next,
        }
    }
    if count >= 3 {
        let (next, result) = reserve(
            current, requests[2].id, requests[2].owner, requests[2].amount,
        );
        match result {
            Err(error) => return (
                ledger, BatchResponse::RequestFailed { index: 2, error },
            ),
            Ok(()) => current = next,
        }
    }
    (current, BatchResponse::Applied { count })
}

pub fn apply_command<const N: usize>(
    ledger: Ledger<N>,
    command: Command,
) -> (out: (Ledger<N>, Response))
    requires N > 0 && ledger.valid(),
    ensures
        out.0.valid(),
        out.1 == apply_model(ledger.view(), command).1,
        same_view(out.0.view(), apply_model(ledger.view(), command).0),
{
    proof {
        reveal(apply_model);
        reveal(reserve_result_view);
        reveal(reserve_result);
        reveal(reserve_state);
        reveal(release_result_view);
        reveal(release_result_for);
        reveal(release_state);
        reveal(batch_model);
        reveal_with_fuel(batch_model_from, 4);
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
        Command::Batch { requests, count } => {
            let (next, result) = reserve_batch(ledger, requests, count);
            (next, Response::Batched(result))
        }
    }
}

pub fn run_commands<const N: usize, const M: usize>(
    initial: Ledger<N>,
    commands: [Command; M],
    responses: &mut [Response; M],
) -> (final_ledger: Ledger<N>)
    requires N > 0 && initial.valid(),
    ensures
        final_ledger.valid(),
        M == 0 ==> same_view(final_ledger.view(), initial.view()),
        exists |states: Seq<LedgerView>| {
            states.len() == M as int + 1
                && final(responses)@.len() == commands@.len()
                && same_view(states[0], initial.view())
                && same_view(states[M as int], final_ledger.view())
                && forall |j: int| 0 <= j < M as int ==> same_view(
                    states[j + 1], apply_model(states[j], commands@[j]).0,
                )
                && forall |j: int| 0 <= j < M as int
                    ==> final(responses)@[j] == apply_model(states[j], commands@[j]).1
        },
{
    let mut current = initial;
    let ghost mut states: Seq<LedgerView> = seq![initial.view()];
    let mut i: usize = 0;

    while i < M
        invariant
            N > 0,
            current.valid(),
            i <= M,
            states.len() == i as int + 1,
            responses@.len() == commands@.len(),
            same_view(states[0], initial.view()),
            same_view(states[i as int], current.view()),
            forall |j: int| 0 <= j < i as int ==> same_view(
                states[j + 1], apply_model(states[j], commands@[j]).0,
            ),
            forall |j: int| 0 <= j < i as int
                ==> responses@[j] == apply_model(states[j], commands@[j]).1,
        decreases M - i,
    {
        let command = commands[i];
        assert(N > 0);
        assert(current.valid());
        let ghost responses_before = responses@;
        let ghost states_before = states;
        let (next, response) = apply_command(current, command);
        responses[i] = response;
        assert(responses@[i as int] == response);
        proof {
            assert(responses@ == responses_before.update(i as int, response));
            assert(states == states_before);
            assert(same_view(states_before[i as int], current.view()));
            assert(same_view(next.view(), apply_model(current.view(), command).0));
            assert(response == apply_model(current.view(), command).1);
            states = states_before.push(next.view());
            assert(same_view(states[i as int + 1], next.view()));
            assert forall |j: int| 0 <= j < i as int implies {
                same_view(states[j + 1], apply_model(states[j], commands@[j]).0)
                    && responses@[j] == apply_model(states[j], commands@[j]).1
            } by {
                assert(j != i as int);
                assert(states[j] == states_before[j]);
                assert(states[j + 1] == states_before[j + 1]);
                assert(responses@[j] == responses_before[j]);
                assert(same_view(
                    states_before[j + 1],
                    apply_model(states_before[j], commands@[j]).0,
                ));
                assert(responses_before[j]
                    == apply_model(states_before[j], commands@[j]).1);
            }
            assert forall |j: int| 0 <= j < i as int + 1 implies
                same_view(states[j + 1], apply_model(states[j], commands@[j]).0)
            by {
                if j < i as int {
                    assert(j != i as int);
                    assert(states[j] == states_before[j]);
                    assert(states[j + 1] == states_before[j + 1]);
                    assert(same_view(
                        states_before[j + 1],
                        apply_model(states_before[j], commands@[j]).0,
                    ));
                } else {
                    assert(j == i as int);
                    assert(same_view(states[j], current.view()));
                    apply_model_respects_view(states[j], current.view(), command);
                    assert(same_view(states[j + 1], next.view()));
                    assert(same_view(states[j + 1], apply_model(states[j], command).0));
                    assert(commands@[j] == command);
                }
            }
            assert forall |j: int| 0 <= j < i as int + 1 implies
                responses@[j] == apply_model(states[j], commands@[j]).1
            by {
                if j < i as int {
                    assert(j != i as int);
                    assert(states[j] == states_before[j]);
                    assert(responses@[j] == responses_before[j]);
                    assert(responses_before[j]
                        == apply_model(states_before[j], commands@[j]).1);
                } else {
                    assert(j == i as int);
                    assert(same_view(states[j], current.view()));
                    apply_model_respects_view(states[j], current.view(), command);
                    assert(response == apply_model(current.view(), command).1);
                    assert(apply_model(states[j], commands@[j]).1
                        == apply_model(current.view(), command).1);
                    assert(responses@[j] == response);
                    assert(commands@[j] == command);
                }
            }
            assert(states.len() == i as int + 2);
            assert(responses@.len() == commands@.len());
            assert(same_view(states[0], initial.view()));
            assert(same_view(states[i as int + 1], next.view()));
        }
        current = next;
        i = i + 1;
    }
    current
}
}

mod downstream {
    use super::ledger_api::*;
    use vstd::prelude::*;

    pub fn external_caller() {
        let empty = new([None, None], 6);
        assert(empty.valid());
        assert(empty.view().used == 0);
        assert(empty.view().capacity == 6);

        let (held, response) = reserve(empty, 37, 9, 2);
        proof {
            reveal(reserve_result);
            reveal(reserve_result_view);
            reveal(slots_have_id);
            reveal(slots_have_id_prefix);
            reveal(full_slots);
            reveal(slots_count);
            reveal(slots_count_from);
            assert(empty.view().slots.len() == 2);
            assert forall |j: int| 0 <= j < 2 implies empty.view().slots[j].is_none() by {
                assert(empty.view().slots[j] == None);
            }
            slots_count_none_prefix(empty.view().slots, 2);
            assert(!slots_have_id(empty.view().slots, 37));
            assert(!full_slots(empty.view().slots));
        }
        assert(response == Ok(()));
        assert(held.view().used == 2);
        assert(held.reservations().dom().contains(37));
        assert(held.reservations()[37] == (9, 2));

        proof {
            reveal(slots_after_reserve);
            reveal(first_empty);
            reveal_with_fuel(first_empty_prefix, 3);
            assert(empty.view().slots[0] == None);
            assert(first_empty(empty.view().slots) == Some(0));
            assert(held.view().slots == slots_after_reserve(
                empty.view().slots, Entry { id: 37, owner: 9, amount: 2 },
            ));
            assert(held.view().slots[0] == Some(Entry { id: 37, owner: 9, amount: 2 }));
            reveal(release_result_for);
            reveal(release_result_view);
            reveal(first_entry);
            reveal_with_fuel(first_entry_prefix, 3);
            assert(first_entry(held.view().slots, 37)
                == Some(Entry { id: 37, owner: 9, amount: 2 }));
        }
        let (empty_again, response) = release(held, 37, 9);
        proof {
            reveal(release_result_for);
            reveal(release_result_view);
            reveal(first_entry);
            reveal_with_fuel(first_entry_prefix, 3);
            assert(response == Ok(2));
        }
        assert(empty_again.view().used == 0);
        assert(!empty_again.reservations().dom().contains(37));
    }

    pub fn batch_rollback_caller() {
        let empty = new([None, None, None, None], 10);
        let requests = [
            Request { id: 51, owner: 1, amount: 3 },
            Request { id: 52, owner: 2, amount: 0 },
            Request { id: 53, owner: 3, amount: 1 },
        ];
        let (after, result) = reserve_batch(empty, requests, 3);
        proof {
            reveal(batch_model);
            reveal_with_fuel(batch_model_from, 4);
            reveal(reserve_result_view);
            reveal(slots_have_id);
            reveal(slots_have_id_prefix);
            reveal(full_slots);
            reveal(slots_count);
            reveal(slots_count_from);
            assert(empty.view().slots.len() == 4);
            assert forall |j: int| 0 <= j < 4 implies empty.view().slots[j].is_none() by {
                assert(empty.view().slots[j] == None);
            }
            slots_count_none_prefix(empty.view().slots, 4);
        }
        assert(result == BatchResponse::RequestFailed {
            index: 1, error: LedgerError::ZeroAmount,
        });
        assert(same_view(after.view(), empty.view()));
        assert(after.valid());
    }

    pub fn batch_success_caller() {
        let empty = new([None, None, None, None], 10);
        let requests = [
            Request { id: 61, owner: 1, amount: 3 },
            Request { id: 62, owner: 2, amount: 2 },
            Request { id: 63, owner: 3, amount: 1 },
        ];
        let (after, result) = reserve_batch(empty, requests, 1);
        proof {
            reveal(batch_model);
            reveal_with_fuel(batch_model_from, 4);
            reveal(reserve_result_view);
            reveal(slots_have_id);
            reveal(slots_have_id_prefix);
            reveal(full_slots);
            reveal(slots_count);
            reveal(slots_count_from);
            slots_count_none_prefix(empty.view().slots, 4);
            reveal(slots_after_reserve);
            reveal(first_empty);
            reveal_with_fuel(first_empty_prefix, 5);
            reveal(reserve_state);
            assert(first_empty(empty.view().slots) == Some(0));
        }
        assert(result == BatchResponse::Applied { count: 1 });
        assert(after.view().used == 3);
        assert(after.view().slots[0] == Some(Entry { id: 61, owner: 1, amount: 3 }));
        assert(after.valid());
    }

    pub fn finite_command_caller() {
        let empty = new([None, None], 10);
        let mut no_responses: [Response; 0] = [];
        let unchanged = run_commands(empty, [], &mut no_responses);
        assert(same_view(unchanged.view(), empty.view()));

        let empty = new([None, None, None, None], 10);
        let batch_requests = [
            Request { id: 41, owner: 2, amount: 3 },
            Request { id: 42, owner: 3, amount: 1 },
            Request { id: 43, owner: 4, amount: 1 },
        ];
        let commands = [
            Command::Reserve { id: 40, owner: 1, amount: 2 },
            Command::Batch { requests: batch_requests, count: 0 },
            Command::Release { id: 40, owner: 1 },
        ];
        let mut responses = [
            Response::Reserved(Err(LedgerError::ZeroAmount)),
            Response::Batched(BatchResponse::TooManyRequests),
            Response::Released(Err(LedgerError::Missing)),
        ];
        let final_state = run_commands(empty, commands, &mut responses);
        let ghost states = choose |states: Seq<LedgerView>| {
            states.len() == 4
                && responses@.len() == commands@.len()
                && same_view(states[0], empty.view())
                && same_view(states[3], final_state.view())
                && forall |j: int| 0 <= j < 3 ==> same_view(
                    states[j + 1], apply_model(states[j], commands@[j]).0,
                )
                && forall |j: int| 0 <= j < 3
                    ==> responses@[j] == apply_model(states[j], commands@[j]).1
        };
        proof {
            assert(empty.view().slots.len() == 4);
            assert forall |j: int| 0 <= j < 4 implies empty.view().slots[j].is_none() by {
                assert(empty.view().slots[j] == None);
            }
            slots_count_none_prefix(empty.view().slots, 4);
            reveal(slots_count);
            reveal(slots_count_from);
            reveal(full_slots);
            reveal(slots_have_id);
            reveal(slots_have_id_prefix);
            assert(!slots_have_id(empty.view().slots, 40));
            assert(!full_slots(empty.view().slots));
            reveal(apply_model);
            reveal(reserve_result);
            reveal(reserve_result_view);
            reveal(slots_have_id);
            reveal(slots_have_id_prefix);
            reveal(full_slots);
            reveal(slots_count);
            reveal(slots_count_from);
            reveal(slots_after_reserve);
            reveal(first_empty);
            reveal_with_fuel(first_empty_prefix, 5);
            reveal(reserve_state);
            reveal(batch_model);
            reveal_with_fuel(batch_model_from, 4);
            reveal(release_result_for);
            reveal(release_result_view);
            reveal(first_entry);
            reveal_with_fuel(first_entry_prefix, 5);
            assert(first_empty(empty.view().slots) == Some(0));
            let initial_reserve = reserve_state(
                empty.view(), Request { id: 40, owner: 1, amount: 2 },
            );
            assert(initial_reserve.slots[0] == Some(Entry { id: 40, owner: 1, amount: 2 }));
            assert(first_entry(initial_reserve.slots, 40)
                == Some(Entry { id: 40, owner: 1, amount: 2 }));
            assert(release_result_view(initial_reserve, 40, 1) == Ok(2));
            assert(same_view(states[2], initial_reserve));
            reveal(release_state);
            reveal(slots_after_release);
            reveal(first_index);
            reveal_with_fuel(first_index_prefix, 5);
            assert(apply_model(states[2], commands@[2]).1 == Response::Released(Ok(2)));
            assert(responses@[2] == Response::Released(Ok(2)));
        }
        assert(commands@[0] == Command::Reserve { id: 40, owner: 1, amount: 2 });
        assert(commands@[1] == Command::Batch { requests: batch_requests, count: 0 });
        assert(commands@[2] == Command::Release { id: 40, owner: 1 });
        assert(responses@[2] == apply_model(states[2], commands@[2]).1);
        assert(responses@[0] == Response::Reserved(Ok(())));
        assert(responses@[1] == Response::Batched(BatchResponse::Applied { count: 0 }));
        assert(responses@[2] == Response::Released(Ok(2)));
        assert(final_state.view().used == 0);

        assert(final_state.valid());
    }
}

pub fn main() {
    downstream::external_caller();
    downstream::batch_rollback_caller();
    downstream::batch_success_caller();
    downstream::finite_command_caller();
}
}