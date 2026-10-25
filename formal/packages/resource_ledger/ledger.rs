use vstd::prelude::*;

verus! {

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
    pub slots: [Option<Entry>; 3],
    pub used: u32,
    pub capacity: u32,
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

pub open spec fn same_state(a: Ledger, b: Ledger) -> bool {
    a.slots@ == b.slots@ && a.used == b.used && a.capacity == b.capacity
}

impl Ledger {
    pub open spec fn reservations(&self) -> Map<u32, (u32, u32)> {
        slots_map(self.slots@)
    }

    pub open spec fn valid(&self) -> bool {
        self.used as nat == slots_used(self.slots@)
            && self.used <= self.capacity
            && slots_have_distinct_ids(self.slots@)
            && slots_have_positive_amounts(self.slots@)
    }
}

pub fn new(capacity: u32) -> (ledger: Ledger)
    ensures
        ledger.valid(),
        ledger.used == 0,
        ledger.capacity == capacity,
        ledger.slots@ == seq![None, None, None],
{
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
        out.1 == reserve_result(ledger, id, amount),
        match out.1 {
            Ok(()) => out.0.slots@ == slots_after_reserve(
                ledger.slots@,
                Entry { id, owner, amount },
            )
                && out.0.reservations()
                == ledger.reservations().insert(id, (owner, amount))
                && out.0.used == ledger.used + amount
                && out.0.capacity == ledger.capacity,
            Err(_) => same_state(out.0, ledger),
        },
    {
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
    ledger: Ledger,
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

pub fn release(
    ledger: Ledger,
    id: u32,
    owner: u32,
) -> (out: (Ledger, Result<u32, LedgerError>))
    requires ledger.valid(),
    ensures
        out.0.valid(),
        out.1 == release_result(ledger.slots@, id, owner),
        match out.1 {
            Ok(amount) => out.0.reservations() == ledger.reservations().remove(id)
                && out.0.used == ledger.used - amount
                && out.0.capacity == ledger.capacity,
            Err(_) => same_state(out.0, ledger),
        },
{
    if let Some(entry) = ledger.slots[0] {
        if entry.id == id {
            if entry.owner != owner {
                return (ledger, Err(LedgerError::OwnerMismatch));
            }
            let mut next = ledger;
            next.slots[0] = None;
            next.used = next.used - entry.amount;
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
            return (next, Ok(entry.amount));
        }
    }

    (ledger, Err(LedgerError::Missing))
}

pub fn main() {
    let initial = new(7);

    assert(initial.valid());

    let (one, result) = reserve(initial, 11, 4, 3);
    assert(result == Ok(()));
    assert(one.valid());
    assert(one.used == 3);
    assert(one.reservations().dom().contains(11));

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



