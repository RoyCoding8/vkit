#[path = "../source/base/cap.rs"]
mod before;
#[path = "../source/candidate/cap.rs"]
mod after;

use std::io::{self, Read};

fn main() {
    let mut input = Vec::new();
    if io::stdin().read_to_end(&mut input).is_err() || input.len() != 4 {
        eprintln!("expected one four-byte u32 input");
        std::process::exit(2);
    }
    let value = u32::from_le_bytes(input.try_into().expect("length checked"));
    println!(
        "{{\"input\":{value},\"before\":{},\"after\":{}}}",
        before::cap(value),
        after::cap(value)
    );
}
