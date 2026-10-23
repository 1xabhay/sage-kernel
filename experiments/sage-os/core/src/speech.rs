//! Speech guards, from what the first live robot conversations got wrong (`sage-kernel`
//! bench/LEDGER.md): a reply must not repeat a recent one or reuse its opening or closing,
//! and must not claim a sense the body lacks. Plain word matching: no regex engine.

/// Replies in a conversation a new reply must not repeat.
pub const RECENT: usize = 3;
/// A reply that opens or closes with the same words as a recent one is a template filled in twice.
const TEMPLATE_WORDS: usize = 4;

/// Narrow first-person claims of each sense. Each pattern is words in order;
/// `a|b` matches either. Idioms such as "I see what you mean" pass.
const CLAIMS: [(&str, &[&str]); 3] = [
    ("vision", &[
        "i can|could see|watch",
        "i can|could look at",
        "i see you|your|him|her|them|it|a|an|the|someone|something|my",
        "i'm|im seeing|watching",
        "i'm|im looking at",
        "i am seeing|watching",
        "i am looking at",
        "i saw|notice|notices|noticed|spot|spotted you|your|a|an|the|that|someone|something",
    ]),
    ("speech", &["i can|could hear", "i hear you|your|that|it|a|the|someone", "i heard"]),
    ("touch", &["i can|could feel you|your|that|the|it", "i feel your"]),
];

fn verb(sense: &str) -> &'static str {
    match sense {
        "vision" => "see",
        "speech" => "hear",
        _ => "feel touch",
    }
}

fn words(text: &str) -> Vec<String> {
    text.to_lowercase()
        .replace('\u{2019}', "'")
        .split(|c: char| !(c.is_alphanumeric() || c == '\''))
        .filter(|w| !w.is_empty())
        .map(String::from)
        .collect()
}

fn normal(text: &str) -> String {
    words(text).join(" ")
}

/// Sentences, each with its closing punctuation.
fn sentences(text: &str) -> Vec<&str> {
    let mut out = vec![];
    let mut start = 0;
    let bytes = text.as_bytes();
    for (i, c) in text.char_indices() {
        let next_is_space = bytes.get(i + 1).is_some_and(u8::is_ascii_whitespace);
        if matches!(c, '.' | '!' | '?') && next_is_space {
            out.push(text[start..=i].trim());
            start = i + 1;
        }
    }
    out.push(text[start..].trim());
    out.into_iter().filter(|s| !s.is_empty()).collect()
}

fn template(a: &[String], b: &[String]) -> bool {
    let n = TEMPLATE_WORDS;
    a.len() >= n && b.len() >= n && (a[..n] == b[..n] || a[a.len() - n..] == b[b.len() - n..])
}

/// Whether `text` repeats one of `recent`, or reuses its opening or closing words.
pub fn repeats(recent: &[String], text: &str) -> bool {
    let said = words(text);
    !said.is_empty() && recent.iter().map(|r| words(r)).any(|r| said == r || template(&said, &r))
}

/// Whether a reply still streaming could turn out to repeat one of `recent`, so it is held back.
pub fn may_repeat(recent: &[String], partial: &str) -> bool {
    let said = words(partial);
    let n = TEMPLATE_WORDS;
    !said.is_empty()
        && recent.iter().any(|r| {
            let r_words = words(r);
            normal(r).starts_with(&normal(partial)) || said.len() >= n && r_words.len() >= n && said[..n] == r_words[..n]
        })
}

fn matches(said: &[String], pattern: &str) -> bool {
    let steps: Vec<&str> = pattern.split(' ').collect();
    said.windows(steps.len()).any(|window| window.iter().zip(&steps).all(|(w, step)| step.split('|').any(|a| a == w)))
}

/// The senses the body did not declare that `text` claims.
pub fn claims(text: &str, senses: &[String]) -> Vec<&'static str> {
    let said = words(text);
    CLAIMS.iter()
        .filter(|(sense, patterns)| !senses.iter().any(|s| s == sense) && patterns.iter().any(|p| matches(&said, p)))
        .map(|(sense, _)| *sense)
        .collect()
}

/// Whether a reply still streaming opens with what could become a claim of a missing sense.
pub fn may_claim(partial: &str, senses: &[String]) -> bool {
    if CLAIMS.iter().all(|(sense, _)| senses.iter().any(|s| s == sense)) {
        return false;
    }
    let said = words(partial);
    let opens_with_self = said.first().is_some_and(|w| matches!(w.as_str(), "i" | "i'm" | "im" | "yes"));
    !claims(partial, senses).is_empty() || opens_with_self && said.len() < TEMPLATE_WORDS
}

/// Why the model must try again, if it must.
pub fn problem(recent: &[String], text: &str, senses: &[String]) -> Option<String> {
    if repeats(recent, text) {
        return Some(format!("You just said: \"{text}\". That repeats an earlier reply. Answer in new words; keep what is true."));
    }
    let claimed = claims(text, senses);
    if claimed.is_empty() {
        return None;
    }
    let verbs: Vec<_> = claimed.iter().map(|s| verb(s)).collect();
    Some(format!("This body cannot {}. Do not say you can; say you cannot.", verbs.join(" or ")))
}

/// The last resort, once a retry was used or the start was already spoken: sentences said
/// recently are dropped and sentences claiming a missing sense say what the body can do.
/// May return "" when every sentence was a repeat.
pub fn fix(text: &str, recent: &[String], senses: &[String]) -> String {
    let said_before: Vec<String> = recent.iter().flat_map(|r| sentences(r).into_iter().map(normal).collect::<Vec<_>>()).collect();
    let can: Vec<&str> = [("speech", "hear you"), ("vision", "see you"), ("touch", "feel touch")]
        .iter().filter(|(sense, _)| senses.iter().any(|s| s == sense)).map(|(_, can)| *can).collect();
    let mut out: Vec<String> = vec![];
    for sentence in sentences(text) {
        if said_before.contains(&normal(sentence)) {
            continue;
        }
        let claimed = claims(sentence, senses);
        let line = if claimed.is_empty() {
            sentence.to_owned()
        } else {
            let verbs: Vec<_> = claimed.iter().map(|s| verb(s)).collect();
            let only = if can.is_empty() { ".".to_owned() } else { format!("; I can only {}.", can.join(" and ")) };
            format!("I can't {} yet{only}", verbs.join(" or "))
        };
        if !out.contains(&line) {
            out.push(line);
        }
    }
    out.join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(items: &[&str]) -> Vec<String> {
        items.iter().map(|x| x.to_string()).collect()
    }

    #[test]
    fn repeats_and_templates() {
        let recent = s(&["Hello! Let me know if you need any help."]);
        assert!(repeats(&recent, "hello, let me know if you need any help"));
        assert!(repeats(&recent, "Sure. Let me know if you need any help!"), "same closing");
        assert!(!repeats(&recent, "Sure, happy to."));
        assert!(may_repeat(&recent, "Hello! Let me"));
        assert!(!may_repeat(&recent, "Good morning"));
    }

    #[test]
    fn sense_claims() {
        let hears = s(&["speech"]);
        assert_eq!(claims("Yes, I can see you!", &hears), ["vision"]);
        assert!(claims("I see what you mean.", &hears).is_empty());
        assert!(claims("I hear you.", &hears).is_empty(), "this body hears");
        assert_eq!(claims("I hear you.", &[]), ["speech"]);
        assert!(may_claim("I", &hears));
        assert!(!may_claim("I think that is great", &hears));
        assert!(!may_claim("I", &s(&["speech", "vision", "touch"])));
    }

    #[test]
    fn the_last_resort_keeps_what_is_new_and_true() {
        let recent = s(&["Hello! Let me know if you need any help."]);
        assert_eq!(fix("Sure thing. Let me know if you need any help.", &recent, &s(&["speech"])), "Sure thing.");
        assert_eq!(fix("I can see you clearly. Nice day.", &[], &s(&["speech"])), "I can't see yet; I can only hear you. Nice day.");
        assert_eq!(fix("Let me know if you need any help.", &recent, &[]), "");
    }
}
