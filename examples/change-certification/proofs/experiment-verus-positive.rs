use vstd::prelude::*;

verus! {
fn before(a: i32, b: i32) -> (r: i32)
    requires -16 <= a <= 16, -16 <= b <= 16,
    ensures r == a + b,
{
    a + b
}

fn after(a: i32, b: i32) -> (r: i32)
    requires -16 <= a <= 16, -16 <= b <= 16,
    ensures r == a + b,
{
    b + a
}

fn paired(a: i32, b: i32)
    requires -16 <= a <= 16, -16 <= b <= 16,
{
    let old = before(a, b);
    let new = after(a, b);
    assert(old == new);
}

fn main() {}
}
