mod ledger;

use ledger::ledger_api::{
    apply_command, new, release, reserve, reserve_batch, run_commands, BatchResponse,
    Command, Entry, LedgerError, Request, Response,
};

fn main() {
    let initial = new([None; 3], 7);
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

    let requests = [
        Request { id: 21, owner: 8, amount: 1 },
        Request { id: 22, owner: 9, amount: 2 },
        Request { id: 23, owner: 9, amount: 1 },
    ];
    let (batched, result) = reserve_batch(one, requests, 2);
    assert!(matches!(result, BatchResponse::Applied { count: 2 }));
    let (remaining, result) = release(batched, 21, 8);
    assert!(matches!(result, Ok(1)));
    let (_, result) = release(remaining, 22, 9);
    assert!(matches!(result, Ok(2)));
    let failing = [requests[0], Request { id: 11, owner: 9, amount: 1 }, requests[2]];
    let (rolled_back, result) = reserve_batch(one, failing, 3);
    assert!(matches!(result, BatchResponse::RequestFailed {
        index: 1, error: LedgerError::DuplicateId,
    }));
    let (_, result) = release(rolled_back, 21, 8);
    assert!(matches!(result, Err(LedgerError::Missing)));
    let (_, result) = release(rolled_back, 11, 4);
    assert!(matches!(result, Ok(3)));
    let (_, result) = reserve_batch(one, failing, 4);
    assert!(matches!(result, BatchResponse::TooManyRequests));
    let (tagged, result) = apply_command(new([None; 2], 5), Command::Reserve { id: 51, owner: 9, amount: 2 });
    assert!(matches!(result, Response::Reserved(Ok(()))));
    let (_, result) = apply_command(tagged, Command::Release { id: 51, owner: 9 });
    assert!(matches!(result, Response::Released(Ok(2))));

    let supplied = [Some(Entry { id: 99, owner: 99, amount: 99 }); 4];
    let cleared = new(supplied, 10);
    let (_, result) = release(cleared, 99, 99);
    assert!(matches!(result, Err(LedgerError::Missing)));
    let commands = [
        Command::Reserve { id: 11, owner: 4, amount: 3 },
        Command::Batch { requests, count: 2 },
        Command::Release { id: 21, owner: 8 },
        Command::Batch { requests: failing, count: 3 },
        Command::Release { id: 11, owner: 4 },
    ];
    let mut responses = [Response::Reserved(Ok(())); 5];
    let final_state = run_commands(cleared, commands, &mut responses);
    assert!(matches!(responses[0], Response::Reserved(Ok(()))));
    assert!(matches!(responses[1], Response::Batched(BatchResponse::Applied { count: 2 })));
    assert!(matches!(responses[2], Response::Released(Ok(1))));
    assert!(matches!(responses[3], Response::Batched(BatchResponse::RequestFailed {
        index: 1, error: LedgerError::DuplicateId,
    })));
    assert!(matches!(responses[4], Response::Released(Ok(3))));
    let (_, result) = release(final_state, 21, 8);
    assert!(matches!(result, Err(LedgerError::Missing)));
    let (_, result) = release(final_state, 22, 9);
    assert!(matches!(result, Ok(2)));
    let mut empty_responses: [Response; 0] = [];
    let unchanged = run_commands(final_state, [], &mut empty_responses);
    let (_, result) = release(unchanged, 22, 9);
    assert!(matches!(result, Ok(2)));
    println!("VKIT_LEDGER_SMOKE_OK");
}
