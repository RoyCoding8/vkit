mod ledger;

use ledger::{new, release, reserve, LedgerError};

fn main() {
    let initial = new(7);
    let (one, result) = reserve(initial, 11, 4, 3);
    assert!(matches!(result, Ok(())));
    let (_, result) = reserve(one, 11, 9, 0);
    assert!(matches!(result, Err(LedgerError::ZeroAmount)));
    let (_, result) = reserve(one, 11, 9, 1);
    assert!(matches!(result, Err(LedgerError::DuplicateId)));
    let (_, result) = reserve(one, 12, 9, 5);
    assert!(matches!(result, Err(LedgerError::CapacityExceeded)));
    let (two, result) = reserve(one, 12, 5, 1);
    assert!(matches!(result, Ok(())));
    let (three, result) = reserve(two, 13, 6, 1);
    assert!(matches!(result, Ok(())));
    let (_, result) = reserve(three, 11, 8, 8);
    assert!(matches!(result, Err(LedgerError::DuplicateId)));
    let (_, result) = reserve(three, 14, 7, 8);
    assert!(matches!(result, Err(LedgerError::Full)));
    let (_, result) = release(one, 11, 5);
    assert!(matches!(result, Err(LedgerError::OwnerMismatch)));
    let (_, result) = release(one, 12, 4);
    assert!(matches!(result, Err(LedgerError::Missing)));
    let (released, result) = release(one, 11, 4);
    assert!(matches!(result, Ok(3)));
    let (_, result) = reserve(released, 11, 5, 7);
    assert!(matches!(result, Ok(())));
    println!("VKIT_LEDGER_SMOKE_OK");
}
