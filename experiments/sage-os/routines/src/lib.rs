//! Routines as a device. A routine is a file: a short header with its schedule and the
//! owner's approval, then the prompt. When one is due, the runner posts the prompt to the
//! kernel as a `message` from its own device, like any other body would.
//!
//! The scheduling is pure: every function takes the time it is asked about.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

/// Missed fires older than this are skipped; the latest one inside it fires once.
pub const GRACE_S: i64 = 600;

#[derive(Clone, Debug, PartialEq)]
pub enum Schedule {
    /// Minute of the local day.
    Daily(i64),
    /// Monday to Friday, at a minute of the local day.
    Weekdays(i64),
    /// A weekday (0 is Monday) and a minute of the local day.
    Weekly(i64, i64),
    /// Every N minutes, on the minutes since 1970 that N divides.
    Every(i64),
}

#[derive(Clone, Debug, PartialEq)]
pub struct Routine {
    pub name: String,
    pub schedule: Schedule,
    pub conversation: String,
    /// Only the owner writes `approved: yes`; until then the routine never fires.
    pub approved: bool,
    pub prompt: String,
}

pub fn parse_schedule(text: &str) -> Result<Schedule, String> {
    let clock = |w: &str| {
        let (h, m) = w.split_once(':').filter(|(h, m)| (1..=2).contains(&h.len()) && m.len() == 2)?;
        let (h, m): (i64, i64) = (h.parse().ok()?, m.parse().ok()?);
        (w.bytes().all(|b| b.is_ascii_digit() || b == b':') && h < 24 && m < 60).then_some(h * 60 + m)
    };
    let words: Vec<&str> = text.split_whitespace().collect();
    let found = match words[..] {
        ["daily", at] => clock(at).map(Schedule::Daily),
        ["weekdays", at] => clock(at).map(Schedule::Weekdays),
        ["weekly", day, at] => {
            let day = day.to_ascii_lowercase();
            let days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
            days.iter().position(|d| day.len() >= 3 && d.starts_with(&day)).zip(clock(at)).map(|(d, at)| Schedule::Weekly(d as i64, at))
        }
        ["every", n, "minute" | "minutes"] => n.parse().ok().filter(|n| *n > 0).map(Schedule::Every),
        _ => None,
    };
    found.ok_or_else(|| format!("schedule {text:?}: write daily 8:00, weekdays 8:00, weekly monday 8:00 or every 15 minutes"))
}

/// A routine file named `name`: front matter between `---` lines, then the prompt.
pub fn parse(name: &str, text: &str) -> Result<Routine, String> {
    let mut lines = text.lines();
    if lines.next().map(str::trim) != Some("---") {
        return Err("start with a --- line, then schedule: and approved: lines, then ---".into());
    }
    let (mut schedule, mut conversation, mut approved) = (None, "owner".to_owned(), false);
    loop {
        let line = lines.next().ok_or("close the header with a --- line")?.trim();
        if line == "---" {
            break;
        }
        if line.is_empty() {
            continue;
        }
        let (key, value) = line.split_once(':').ok_or_else(|| format!("{line:?}: header lines are key: value"))?;
        match key.trim() {
            "schedule" => schedule = Some(parse_schedule(value)?),
            "conversation" => conversation = value.trim().to_owned(),
            "approved" => approved = value.trim().eq_ignore_ascii_case("yes"),
            other => return Err(format!("unknown header {other:?}: use schedule, conversation, approved")),
        }
    }
    let prompt = lines.collect::<Vec<_>>().join("\n").trim().to_owned();
    // The kernel's limits, checked here so a bad file is reported once instead of refused each minute.
    if prompt.is_empty() || prompt.len() > 4000 {
        return Err("the prompt after the header needs 1 to 4000 characters".into());
    }
    if !(1..=128).contains(&conversation.len()) || !(1..=100).contains(&name.len()) {
        return Err("the conversation needs 1 to 128 characters and the file name 1 to 100".into());
    }
    let schedule = schedule.ok_or("add a schedule: line")?;
    Ok(Routine { name: name.into(), schedule, conversation, approved, prompt })
}

const DAY: i64 = 86400;

/// The first time strictly after `after` (seconds since 1970) that `schedule` fires.
pub fn next_fire(schedule: &Schedule, after: i64, utc_offset_s: i64) -> i64 {
    let (weekdays, minute) = match *schedule {
        Schedule::Every(n) => return (after.div_euclid(60).div_euclid(n) + 1) * n * 60,
        Schedule::Daily(at) => (0b111_1111, at),
        Schedule::Weekdays(at) => (0b001_1111, at),
        Schedule::Weekly(day, at) => (1 << day, at),
    };
    let today = (after + utc_offset_s).div_euclid(DAY);
    // 1970-01-01 was a Thursday, weekday 3 when Monday is 0.
    (today..today + 8)
        .map(|day| (day, day * DAY + minute * 60 - utc_offset_s))
        .find(|&(day, at)| at > after && weekdays & (1 << (day + 3).rem_euclid(7)) != 0)
        .expect("every schedule fires within a week")
        .1
}

/// The one fire to post at `now`: the latest in (since, now] that is within the grace window.
pub fn due(schedule: &Schedule, since: i64, now: i64, utc_offset_s: i64) -> Option<i64> {
    // ponytail: only the latest missed fire, and only within GRACE_S: a brief that is hours late
    // is noise. Make the window a per-routine header if some routine must always catch up.
    let (mut fire, mut at) = (None, next_fire(schedule, since.max(now - GRACE_S), utc_offset_s));
    while at <= now {
        fire = Some(at);
        at = next_fire(schedule, at, utc_offset_s);
    }
    fire
}

/// Every `*.md` routine in `dir`, and a line for each file that could not be read.
pub fn load(dir: &Path) -> (Vec<Routine>, Vec<String>) {
    let Ok(entries) = std::fs::read_dir(dir) else { return (vec![], vec![format!("{}: no routines folder yet", dir.display())]) };
    let mut paths: Vec<PathBuf> = entries.filter_map(|e| Some(e.ok()?.path())).filter(|p| p.extension().is_some_and(|e| e == "md")).collect();
    paths.sort();
    let (mut routines, mut problems) = (vec![], vec![]);
    for path in paths {
        let name = path.file_stem().unwrap_or_default().to_string_lossy();
        match std::fs::read_to_string(&path).map_err(|e| e.to_string()).and_then(|text| parse(&name, &text)) {
            Ok(routine) => routines.push(routine),
            Err(problem) => problems.push(format!("{}: {problem}", path.display())),
        }
    }
    (routines, problems)
}

/// Posts each due, approved routine to the kernel as the `routines` device.
pub struct Runner {
    pub dir: PathBuf,
    pub url: String,
    pub token: String,
    pub utc_offset_s: i64,
    /// Everything up to here has been posted.
    pub since: i64,
    told: BTreeSet<String>,
}

impl Runner {
    /// A runner starting at `now`: a fire missed within the grace window still goes out.
    pub fn new(dir: PathBuf, url: String, token: String, utc_offset_s: i64, now: i64) -> Runner {
        Runner { dir, url, token, utc_offset_s, since: now - GRACE_S, told: BTreeSet::new() }
    }

    /// Posts what is due at `now`; returns when to look again.
    // ponytail: rereads the folder every minute, so a new file or an approval needs no restart;
    // watch the folder instead if it ever holds thousands of routines.
    pub fn tick(&mut self, now: i64) -> i64 {
        let (routines, mut notes) = load(&self.dir);
        let (mut next, mut posted) = (now + 60, true);
        for routine in routines {
            if !routine.approved {
                notes.push(format!("{}: waits for the owner to write approved: yes", routine.name));
                continue;
            }
            if let Some(at) = due(&routine.schedule, self.since, now, self.utc_offset_s) {
                let id = format!("{}@{at}", routine.name);
                match self.post(&routine, &id) {
                    Ok(()) => eprintln!("sage-routines: posted {id}"),
                    Err(e) => {
                        eprintln!("sage-routines: could not post {id}, retrying: {e}");
                        posted = false;
                    }
                }
            }
            next = next.min(next_fire(&routine.schedule, now, self.utc_offset_s));
        }
        // Each problem is told once, until it is fixed.
        let notes: BTreeSet<String> = notes.into_iter().collect();
        notes.difference(&self.told).for_each(|note| eprintln!("sage-routines: {note}"));
        self.told = notes;
        // A failed post is retried next tick; the kernel ignores the ids already posted.
        if posted {
            self.since = now;
        }
        next
    }

    fn post(&self, routine: &Routine, id: &str) -> Result<(), String> {
        let body = serde_json::json!({"kind": "message", "conversation": routine.conversation, "id": id, "text": routine.prompt});
        let request = ureq::post(&format!("{}/v1/events", self.url)).timeout(std::time::Duration::from_secs(10));
        request.set("Authorization", &format!("Bearer {}", self.token)).send_json(body).map(drop).map_err(|e| e.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MON: i64 = 1792368000; // Monday 2026-10-19 00:00 UTC
    const H: i64 = 3600;
    const D: i64 = 86400;

    #[test]
    fn schedules_parse() {
        assert_eq!(parse_schedule("daily 8:00"), Ok(Schedule::Daily(480)));
        assert_eq!(parse_schedule(" weekdays 08:05 "), Ok(Schedule::Weekdays(485)));
        assert_eq!(parse_schedule("weekly sunday 23:59"), Ok(Schedule::Weekly(6, 1439)));
        assert_eq!(parse_schedule("weekly Mon 0:00"), Ok(Schedule::Weekly(0, 0)));
        assert_eq!(parse_schedule("every 15 minutes"), Ok(Schedule::Every(15)));
        assert_eq!(parse_schedule("every 1 minute"), Ok(Schedule::Every(1)));
        for bad in ["", "daily", "daily 24:00", "daily 8:60", "daily 8", "weekly funday 8:00", "every 0 minutes", "every x minutes", "every 5 hours", "hourly"] {
            assert!(parse_schedule(bad).is_err(), "{bad:?} parsed");
        }
    }

    #[test]
    fn files_parse_and_need_approval() {
        let file = "---\nschedule: weekdays 8:00\napproved: yes\n---\nBrief me on today.\n";
        let r = parse("morning-brief", file).unwrap();
        assert_eq!(r, Routine { name: "morning-brief".into(), schedule: Schedule::Weekdays(480), conversation: "owner".into(), approved: true, prompt: "Brief me on today.".into() });
        let other = parse("x", "---\nschedule: daily 9:00\nconversation: garden\n---\nWater?").unwrap();
        assert_eq!((other.conversation.as_str(), other.approved), ("garden", false));
        assert!(!parse("x", "---\nschedule: daily 9:00\napproved: maybe\n---\nWater?").unwrap().approved);
        for bad in ["schedule: daily 9:00\n---\nno opening", "---\napproved: yes\n---\nno schedule", "---\nschedule: daily 9:00\n---\n  ", "---\nschedule: daily 9:00\naproved: yes\n---\ntypo", "---\nschedule: daily 9:00\nno closing"] {
            assert!(parse("x", bad).is_err(), "{bad:?} parsed");
        }
    }

    #[test]
    fn daily_and_weekdays() {
        let daily = Schedule::Daily(480);
        assert_eq!(next_fire(&daily, MON, 0), MON + 8 * H);
        assert_eq!(next_fire(&daily, MON + 8 * H, 0), MON + D + 8 * H, "strictly after");
        assert_eq!(next_fire(&daily, MON + 8 * H - 1, 0), MON + 8 * H);
        let weekdays = Schedule::Weekdays(480);
        assert_eq!(next_fire(&weekdays, MON + 4 * D + 9 * H, 0), MON + 7 * D + 8 * H, "Friday after 8 waits for Monday");
        assert_eq!(next_fire(&weekdays, MON + 5 * D, 0), MON + 7 * D + 8 * H, "Saturday too");
    }

    #[test]
    fn weekly_and_every() {
        let sunday = Schedule::Weekly(6, 570);
        assert_eq!(next_fire(&sunday, MON, 0), MON + 6 * D + 9 * H + 1800);
        assert_eq!(next_fire(&sunday, MON + 6 * D + 9 * H + 1800, 0), MON + 13 * D + 9 * H + 1800);
        let every = Schedule::Every(15);
        assert_eq!(next_fire(&every, MON, 0), MON + 900);
        assert_eq!(next_fire(&every, MON + 1, 0), MON + 900);
        assert_eq!(next_fire(&every, MON + 899, 0), MON + 900);
        assert_eq!(next_fire(&every, MON + 900, 7 * H), MON + 1800, "the offset does not move it");
    }

    #[test]
    fn the_offset_is_local_time() {
        // 8:00 at UTC-5 is 13:00 UTC; at UTC+9 it is 23:00 UTC the day before.
        assert_eq!(next_fire(&Schedule::Daily(480), MON, -5 * H), MON + 13 * H);
        assert_eq!(next_fire(&Schedule::Daily(480), MON, 9 * H), MON + 23 * H);
        // Monday 8:00 at UTC+9 is Sunday 23:00 UTC; Saturday 20:00 at UTC-5 is Sunday 01:00 UTC.
        assert_eq!(next_fire(&Schedule::Weekdays(480), MON - 2 * D, 9 * H), MON - H);
        assert_eq!(next_fire(&Schedule::Weekly(5, 1200), MON - 3 * D, -5 * H), MON - D + H);
    }

    #[test]
    fn due_fires_the_latest_missed_once_within_grace() {
        let every = Schedule::Every(1);
        assert_eq!(due(&every, MON, MON + 30, 0), None);
        assert_eq!(due(&every, MON, MON + 60, 0), Some(MON + 60));
        assert_eq!(due(&every, MON + 60, MON + 60, 0), None, "nothing twice");
        assert_eq!(due(&every, MON, MON + 3 * H, 0), Some(MON + 3 * H), "many missed: only the latest");
        let daily = Schedule::Daily(480);
        assert_eq!(due(&daily, MON, MON + 8 * H + 599, 0), Some(MON + 8 * H), "down 10 minutes: still sent");
        assert_eq!(due(&daily, MON, MON + 8 * H + 601, 0), None, "too late: skipped");
    }
}
