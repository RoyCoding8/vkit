#![no_std]
#![feature(register_tool)]
#![feature(custom_inner_attributes)]
#![feature(stmt_expr_attributes)]
#![register_tool(rr)]
#![rr::package("vkit-qualification")]
#![rr::include("option")]

#[rr::refined_by("(entry_id, (owner, amount))" : "Z * (Z * Z)")]
pub struct Entry {
    #[rr::field("#(entry_id)")]
    entry_id: i32,
    #[rr::field("#(owner)")]
    owner: i32,
    #[rr::field("#(amount)")]
    amount: i32,
}

#[rr::refined_by("slots" : "list (option (Z * (Z * Z)))")]
pub struct FixedPool {
    #[rr::field("#(<#> (fmap (M:=list) (fmap (M:=option) PlaceIn) slots))")]
    slots: [Option<Entry>; 3],
}

#[rr::verify]
#[rr::returns("[Some (1, (2, 3)); None; None]")]
pub fn fill() -> FixedPool {
    let mut state = FixedPool { slots: [None, None, None] };
    state.slots[0] = Some(Entry { entry_id: 1, owner: 2, amount: 3 });
    state
}
