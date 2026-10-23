//! Memory as files the owner can edit: a profile of lasting facts and a dated log.
//!
//! Text in, text out. The shell reads and writes the files; the core decides what the model
//! sees. The profile is always in the prompt, the log's best lines are, and the rest is
//! found with `search`.

use crate::prompt::clock;

/// The profile in the prompt: at most this many facts...
pub const MAX_FACTS: usize = 100;
/// ...in this many characters.
pub const MAX_PROFILE: usize = 4000;
/// Log lines in the prompt.
pub const MAX_EVENTS: usize = 30;
const PROFILE: &str = "What you know about the owner:";
const EVENTS: &str = "Recent events:";
/// A log line marked `!` after its date ranks this many times higher.
const IMPORTANT: f64 = 4.0;
/// A log line ranks half as high after this many days, a third after twice as many.
const FADE_DAYS: f64 = 30.0;

/// A line as a fact: list marks and spaces trimmed. Headings and blank lines are not facts.
fn fact(line: &str) -> Option<&str> {
    let line = line.trim().trim_start_matches(['-', '*']).trim();
    Some(line).filter(|l| !l.is_empty() && !l.starts_with('#'))
}

/// The profile's facts, one per line.
pub fn facts(profile: &str) -> Vec<String> {
    profile.lines().filter_map(fact).map(String::from).collect()
}

/// Whether a profile is small enough to be shown whole.
pub fn fits(profile: &str) -> bool {
    let facts = facts(profile);
    facts.len() <= MAX_FACTS && facts.iter().map(|f| f.len() + 1).sum::<usize>() <= MAX_PROFILE
}

/// The profile file with `remove` taken out and new facts from `add` put at the end.
/// Every other line stays as the owner wrote it.
pub fn revise(profile: &str, remove: &[String], add: &[String]) -> String {
    let mut lines: Vec<&str> = profile.lines().filter(|l| fact(l).is_none_or(|f| !remove.iter().any(|r| r == f))).collect();
    for new in add {
        if !lines.iter().any(|l| fact(l) == Some(new.as_str())) {
            lines.push(new);
        }
    }
    lines.iter().map(|l| format!("{l}\n")).collect()
}

/// Days since 1970 of a line that starts with `YYYY-MM-DD `.
fn day(line: &str) -> Option<i64> {
    let date = line.get(..10).filter(|_| line[10..].starts_with(' '))?;
    let mut parts = date.split('-').map(|p| p.parse::<i64>().ok());
    let (y, m, d) = (parts.next()??, parts.next()??, parts.next()??);
    if date.as_bytes()[4] != b'-' || !(1..=12).contains(&m) || !(1..=31).contains(&d) {
        return None;
    }
    // Howard Hinnant's days_from_civil.
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (m + if m > 2 { -3 } else { 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    Some(era * 146_097 + doe - 719_468)
}

/// What the model is shown: the profile (the first `MAX_FACTS` facts within `MAX_PROFILE`
/// characters), then the `MAX_EVENTS` dated log lines that rank highest, oldest first.
/// Important lines rank higher; every line ranks lower as it ages. Undated lines are only searched.
// ponytail: ranks by importance and age only; add word overlap with the conversation if the
// wrong lines show up.
pub fn snapshot(profile: &str, log: &str, now: f64) -> String {
    let (mut shown, mut size) = (vec![], 0);
    for f in facts(profile) {
        if shown.len() == MAX_FACTS || size + f.len() + 1 > MAX_PROFILE {
            break;
        }
        size += f.len() + 1;
        shown.push(format!("- {f}"));
    }
    let today = (now / 86_400.0).floor() as i64;
    let mut events: Vec<(usize, i64, &str)> = log.lines().filter_map(fact).enumerate().filter_map(|(i, l)| Some((i, day(l)?, l))).collect();
    let rank = |(_, day, line): &(usize, i64, &str)| {
        let weight = if line[10..].trim_start().starts_with('!') { IMPORTANT } else { 1.0 };
        weight / (1.0 + (today - day).max(0) as f64 / FADE_DAYS)
    };
    events.sort_by(|a, b| rank(b).total_cmp(&rank(a)).then(b.1.cmp(&a.1)));
    events.truncate(MAX_EVENTS);
    events.sort_by_key(|(i, day, _)| (*day, *i));
    let mut parts = vec![];
    if !shown.is_empty() {
        parts.push(format!("{PROFILE}\n{}", shown.join("\n")));
    }
    if !events.is_empty() {
        parts.push(format!("{EVENTS}\n{}", events.iter().map(|(_, _, l)| format!("- {l}")).collect::<Vec<_>>().join("\n")));
    }
    parts.join("\n\n")
}

/// The profile facts a snapshot shows: the only ones a summary may remove.
pub fn shown(snapshot: &str) -> Vec<String> {
    let section = snapshot.lines().skip_while(|l| *l != PROFILE).skip(1).take_while(|l| !l.is_empty());
    section.map(|l| l.strip_prefix("- ").unwrap_or(l).to_owned()).collect()
}

/// Lower-case words, without punctuation.
fn words(text: &str) -> Vec<String> {
    text.to_lowercase().split(|c: char| !c.is_alphanumeric()).filter(|w| !w.is_empty()).map(String::from).collect()
}

/// New log lines, each on one line and dated by the owner's clock. An event whose words
/// match a line already in `log`, whatever its date, is left out.
pub fn dated(events: &[String], log: &str, now: f64, offset: i64) -> Vec<String> {
    let date = &clock(now, offset)[..10];
    let mut logged: Vec<Vec<String>> = log.lines().filter_map(fact).map(|l| words(if day(l).is_some() { &l[10..] } else { l })).collect();
    let mut lines = vec![];
    for event in events.iter().map(|e| e.split_whitespace().collect::<Vec<_>>().join(" ")) {
        let said = words(&event);
        if !said.is_empty() && !logged.contains(&said) {
            lines.push(format!("{date} {event}"));
            logged.push(said);
        }
    }
    lines
}

/// Lines of `text` holding every word of `query`, ignoring case: the newest 20.
// ponytail: scans every line of every file per search; keep an index once the log is megabytes.
pub fn search(text: &str, query: &str) -> String {
    let words = words(query);
    let found: Vec<&str> = text.lines().filter_map(fact).filter(|l| {
        let line = l.to_lowercase();
        !words.is_empty() && words.iter().all(|w| line.contains(w.as_str()))
    }).collect();
    if found.is_empty() {
        return format!("nothing remembered matches \"{query}\"");
    }
    let mut text = found[found.len().saturating_sub(20)..].join("\n");
    while text.len() > crate::MAX_TEXT {
        text.pop(); // a result holds at most MAX_TEXT bytes
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(items: &[&str]) -> Vec<String> {
        items.iter().map(|x| x.to_string()).collect()
    }

    const NOW: f64 = 1_792_497_600.0; // 2026-10-20 12:00 UTC

    #[test]
    fn the_profile_is_one_fact_per_line_and_the_owners_lines_stay() {
        let file = "# About Abby\nAbby likes tea.\n- Abby lives in Boston.\n";
        assert_eq!(facts(file), ["Abby likes tea.", "Abby lives in Boston."]);
        let revised = revise(file, &s(&["Abby lives in Boston."]), &s(&["Abby lives in Philadelphia.", "Abby likes tea."]));
        assert_eq!(revised, "# About Abby\nAbby likes tea.\nAbby lives in Philadelphia.\n");
        assert!(fits(&"x\n".repeat(MAX_FACTS)));
        assert!(!fits(&"x\n".repeat(MAX_FACTS + 1)));
        assert!(!fits(&"x".repeat(MAX_PROFILE + 1)));
    }

    #[test]
    fn important_and_recent_events_rank_first() {
        // Plain lines 1 to 40 days old, and one important line 200 days old.
        let day = |ago: f64| crate::prompt::clock(NOW - ago * 86_400.0, 0)[..10].to_owned();
        let mut log: Vec<String> = (1..=40).rev().map(|ago| format!("{} plain {ago}", day(ago as f64))).collect();
        log.insert(0, format!("{} ! important", day(200.0)));
        log.push("not dated".into());
        let memory = snapshot("", &log.join("\n"), NOW);
        let events: Vec<&str> = memory.lines().skip(1).collect();
        assert_eq!(events.len(), MAX_EVENTS, "{memory}");
        assert!(events[0].ends_with("! important"), "oldest first: {memory}");
        assert!(events[1].ends_with("plain 29") && events[29].ends_with("plain 1"), "{memory}");
        assert!(!memory.contains("plain 30") && !memory.contains("not dated"));
        assert_eq!(snapshot("", "", NOW), "");
    }

    #[test]
    fn the_snapshot_shows_at_most_the_profile_cap() {
        let file: String = (0..120).map(|i| format!("fact {i}\n")).collect();
        let memory = snapshot(&file, "", NOW);
        assert_eq!(shown(&memory).len(), MAX_FACTS);
        assert_eq!(shown(&memory)[0], "fact 0");
        assert_eq!(shown("What you know about the owner:\n- a\n- b\n\nRecent events:\n- 2026-01-01 c"), ["a", "b"]);
        assert_eq!(dated(&s(&[" ! Moved\nhouse ", "", "Had a cold!", "moved house"]), "2026-09-01 had a cold", NOW, 0), ["2026-10-20 ! Moved house"]);
    }

    #[test]
    fn search_finds_lines_with_every_word() {
        let all = "Abby likes tea.\n2026-10-01 ! Started at Penn\n2026-10-02 Penn party";
        assert_eq!(search(all, "penn"), "2026-10-01 ! Started at Penn\n2026-10-02 Penn party");
        assert_eq!(search(all, "PARTY, penn"), "2026-10-02 Penn party");
        assert_eq!(search(all, "cats"), "nothing remembered matches \"cats\"");
    }
}
