#![no_std]
#![feature(register_tool)]
#![feature(custom_inner_attributes)]
#![register_tool(rr)]
#![rr::package("vkit-qualification")]

fn private_helper(value: u32) -> u32 {
    value
}

mod inner {
    pub fn exposed(value: u32) -> u32 {
        value
    }
}

pub use inner::exposed as exported_alias;

#[rr::params("u" : "Z", "c" : "Z", "a" : "Z")]
#[rr::args("u", "c", "a")]
#[rr::requires("0 ≤ u ≤ c")]
#[rr::returns("if decide (0 < a ∧ a ≤ c - u) then u + a else u")]
pub fn reserve(used: u32, capacity: u32, amount: u32) -> u32 {
    if amount == 0 {
        return used;
    }
    if amount > capacity - used {
        return used;
    }
    used + amount
}
