fn pair_before(input: &str) -> Option<(&str, &str)> {
    let mut parts = input.split('=');
    let key = parts.next()?;
    let value = parts.next()?;
    if key.is_empty() || value.is_empty() || parts.next().is_some() {
        return None;
    }
    Some((key, value))
}

fn pair_after(input: &str) -> Option<(&str, &str)> {
    let (index, _) = input.char_indices().find(|(_, ch)| *ch == '=')?;
    let key = &input[..index];
    let value = &input[index + 1..];
    if key.is_empty() || value.is_empty() || value.contains('=') {
        return None;
    }
    Some((key, value))
}

fn render_pair(pair: Option<(&str, &str)>) -> String {
    match pair {
        Some((key, value)) => format!("{key}:{value}"),
        None => "None".to_owned(),
    }
}

fn upper_pair_before(input: &str) -> Option<(&str, &str)> {
    let (key, value) = pair_after(input)?;
    if !key.is_empty() && key.bytes().all(|byte| byte.is_ascii_uppercase()) {
        Some((key, value))
    } else {
        None
    }
}

fn upper_pair_after(input: &str, allow_lowercase: bool) -> Option<(&str, &str)> {
    let (key, value) = pair_after(input)?;
    let uppercase = !key.is_empty() && key.bytes().all(|byte| byte.is_ascii_uppercase());
    let lowercase = allow_lowercase && !key.is_empty()
        && key.bytes().all(|byte| byte.is_ascii_lowercase());
    if uppercase || lowercase { Some((key, value)) } else { None }
}

fn number_before(input: &str) -> Option<u8> {
    input.parse().ok()
}

fn number_after(input: &str) -> Option<u8> {
    let digits: String = input.chars().take_while(|ch| ch.is_ascii_digit()).collect();
    if digits.is_empty() { None } else { digits.parse().ok() }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pa_01_single_delimiter_parser_refactor() {
        for (input, expected) in [("ID=7", "ID:7"), ("x=y", "x:y"), ("NO=MORE=1", "None")] {
            let before = render_pair(pair_before(input));
            let after = render_pair(pair_after(input));
            assert_eq!(before, expected);
            assert_eq!(after, expected);
            eprintln!("OBS|PA-01|{input}|before={before}|after={after}");
        }
    }

    #[test]
    fn pa_02_guarded_lowercase_key_feature() {
        for (input, allow_lowercase, before_expected, after_expected) in [
            ("text=7", false, "None", "None"),
            ("text=7", true, "None", "text:7"),
            ("TEXT=7", false, "TEXT:7", "TEXT:7"),
        ] {
            let before = render_pair(upper_pair_before(input));
            let after = render_pair(upper_pair_after(input, allow_lowercase));
            assert_eq!(before, before_expected);
            assert_eq!(after, after_expected);
            eprintln!("OBS|PA-02|{input},allow={allow_lowercase}|before={before}|after={after}");
        }
    }

    #[test]
    fn pa_03_unapproved_numeric_prefix_change() {
        for (input, before_expected, after_expected) in [("12", "12", "12"), ("7x", "None", "7")] {
            let before = number_before(input)
                .map(|value| value.to_string())
                .unwrap_or_else(|| "None".to_owned());
            let after = number_after(input)
                .map(|value| value.to_string())
                .unwrap_or_else(|| "None".to_owned());
            assert_eq!(before, before_expected);
            assert_eq!(after, after_expected);
            eprintln!("OBS|PA-03|{input}|before={before}|after={after}");
        }
    }
}
