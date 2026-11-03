#![forbid(unsafe_code)]
#[path = "base/candidate.rs"] mod vkit_base;
#[path = "candidate/candidate.rs"] mod vkit_candidate;
fn vkit_standing(x: u32, y: u32) -> bool { match x {
0u32 => matches!(y, 0u32 | 1u32),
1u32 => matches!(y, 1u32 | 2u32),
_ => false, } }
#[kani::proof]
fn vkit_relational() {
let x: u32 = kani::any();
kani::assume(matches!(x, 0u32 | 1u32));
let before: u32 = vkit_base::convert(x);
let after: u32 = vkit_candidate::convert(x);
assert!(matches!(before, 0u32 | 1u32 | 2u32 | 3u32), "base_observation_model");
assert!(matches!(after, 0u32 | 1u32 | 2u32 | 3u32), "candidate_observation_model");
assert!(vkit_standing(x, after), "candidate_standing_intent");
let relation: bool = match x {
_ => before == after || (!vkit_standing(x, before) && vkit_standing(x, after)),
};
assert!(relation, "relational_policy");
}
