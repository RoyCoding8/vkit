#[path = "scalar.rs"]
mod scalar;

#[kani::proof]
fn refactor() {
    let a: i32 = kani::any();
    let b: i32 = kani::any();
    kani::assume(a >= -16 && a <= 16);
    kani::assume(b >= -16 && b <= 16);
    assert_eq!(scalar::add_before(a, b), scalar::add_after(a, b));
}

#[kani::proof]
fn regression() {
    let x: i32 = kani::any();
    kani::assume(x >= 0 && x <= 10);
    assert_eq!(scalar::cap_before(x), scalar::cap_after(x));
}

#[kani::proof]
#[kani::unwind(2)]
fn exhausted() {
    let mut result = 0;
    for _ in 0..4 {
        result += 1;
    }
    assert_eq!(result, 4);
}

#[kani::proof]
fn undefined_behavior() {
    let data = [1u8];
    let index: usize = kani::any();
    kani::assume(index == 2);
    unsafe {
        assert_eq!(*data.as_ptr().add(index), 1);
    }
}

#[kani::proof]
fn observation_mismatch() {
    let before = scalar::add_before(1, 2);
    let after = scalar::add_after(1, 2) + 1;
    assert_eq!(before, after);
}
