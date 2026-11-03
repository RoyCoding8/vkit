pub fn add_before(a: i32, b: i32) -> i32 {
    a + b
}

pub fn add_after(a: i32, b: i32) -> i32 {
    b + a
}

fn divide_before(a: i32, b: i32) -> i32 {
    a / b
}

fn divide_after(a: i32, b: i32) -> Option<i32> {
    a.checked_div(b)
}

pub fn cap_before(x: i32) -> i32 {
    x.min(10)
}

pub fn cap_after(x: i32) -> i32 {
    x.min(9)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::panic::catch_unwind;

    #[test]
    fn sc_01_commutative_addition_refactor() {
        for (a, b, expected) in [(3, 5, 8), (-2, 7, 5), (0, 0, 0)] {
            assert_eq!(add_before(a, b), expected);
            assert_eq!(add_after(a, b), expected);
            eprintln!("OBS|SC-01|a={a},b={b}|before={expected}|after={expected}");
        }
    }

    #[test]
    fn sc_02_guarded_checked_division_bugfix() {
        for (a, b, expected_before, expected_after) in [
            (10, 2, "Some(5)", "Some(5)"),
            (10, 0, "panic", "None"),
            (i32::MIN, -1, "panic", "None"),
        ] {
            let before = catch_unwind(|| divide_before(a, b))
                .map(|value| format!("Some({value})"))
                .unwrap_or_else(|_| "panic".to_owned());
            let after = divide_after(a, b)
                .map(|value| format!("Some({value})"))
                .unwrap_or_else(|| "None".to_owned());
            assert_eq!(before, expected_before);
            assert_eq!(after, expected_after);
            let input = if a == i32::MIN { "a=i32::MIN,b=-1".to_owned() } else { format!("a={a},b={b}") };
            eprintln!("OBS|SC-02|{input}|before={before}|after={after}");
        }
    }

    #[test]
    fn sc_03_unapproved_cap_change() {
        for (x, expected_before, expected_after) in [(0, 0, 0), (5, 5, 5), (10, 10, 9)] {
            assert_eq!(cap_before(x), expected_before);
            assert_eq!(cap_after(x), expected_after);
            eprintln!("OBS|SC-03|x={x}|before={expected_before}|after={expected_after}");
        }
    }
}
