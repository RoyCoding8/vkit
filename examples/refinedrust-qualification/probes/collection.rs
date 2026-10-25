#![no_std]
#![feature(register_tool)]
#![feature(custom_inner_attributes)]
#![feature(stmt_expr_attributes)]
#![register_tool(rr)]
#![rr::package("vkit-qualification")]

#[rr::refined_by("slots" : "list Z")]
pub struct FixedPool {
    #[rr::field("#(<#> slots)")]
    slots: [i32; 3],
}

#[rr::verify]
#[rr::returns("replicate 3 ((1))")]
pub fn fill() -> FixedPool {
    let mut state = FixedPool { slots: [0; 3] };
    let mut i = 0usize;
    while i < 3 {
        let _ =
            #[rr::exists("i_z")]
            #[rr::inv_var("i" : "#i_z")]
            #[rr::inv_var("state" : "#(replicate (Z.to_nat i_z) ((1)) ++ replicate (3 - Z.to_nat i_z) ((0)))")]
            #[rr::inv("(0 ≤ i_z ≤ 3)%Z")]
            #[rr::ignore] || {};
        state.slots[i] = 1;
        i += 1;
    }
    state
}
