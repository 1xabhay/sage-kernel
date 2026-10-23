//! The runner against a real kernel over HTTP, driven by a fake clock: a due routine wakes
//! the agent once, a restart never fires it twice, and an unapproved one never fires.

use sage::{Identity, Kernel, serve};
use sage_core::{Call, Policy};
use sage_routines::Runner;
use serde_json::{Value, json};
use std::collections::BTreeMap;

const MON: i64 = 1792368000; // Monday 2026-10-19 00:00 UTC

#[test]
fn a_due_routine_wakes_the_agent_once() {
    let home = std::env::temp_dir().join(format!("sage-routines-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&home);
    let dir = home.join("routines");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("morning-brief.md"), "---\nschedule: weekdays 8:00\napproved: yes\n---\nBrief me on today.\n").unwrap();
    std::fs::write(dir.join("unasked.md"), "---\nschedule: weekdays 8:00\n---\nNot approved yet.\n").unwrap();

    let journal = home.join("journal.jsonl");
    let identity = Identity { persona: "You are Sage.".into(), memory: home.join("memory") };
    let quiet = Box::new(|_: &sage_core::Prompt, _: &dyn Fn(&str)| Ok(vec![Call { tool: "send".into(), input: json!({"text": ""}) }]));
    let kernel = Kernel::open(&journal, Policy { owners: vec!["phone".into()], ..Policy::default() }, identity, 10.0, quiet, Box::new(|_| Err("no model".into()))).unwrap();
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let url = format!("http://{}", server.server_addr().to_ip().unwrap());
    let tokens = BTreeMap::from([("routines-token".to_owned(), "routines".to_owned())]);
    std::thread::spawn(move || serve(server, kernel, tokens));

    let mut runner = Runner::new(dir.clone(), url.clone(), "routines-token".into(), 0, MON + 7 * 3600);
    let next = runner.tick(MON + 7 * 3600);
    assert_eq!(next, MON + 7 * 3600 + 60, "looks again within a minute while nothing is due");
    runner.tick(MON + 8 * 3600);
    runner.tick(MON + 8 * 3600 + 30);
    // A restart five minutes later re-sends the same fire; the kernel ignores the repeated id.
    Runner::new(dir, url, "routines-token".into(), 0, MON + 8 * 3600 + 300).tick(MON + 8 * 3600 + 300);

    let facts: Vec<Value> = std::fs::read_to_string(&journal).unwrap().lines()
        .flat_map(|line| serde_json::from_str::<Value>(line).unwrap()["facts"].as_array().unwrap().clone())
        .collect();
    let messages: Vec<&Value> = facts.iter().filter(|f| f["kind"] == "message").collect();
    assert_eq!(messages.len(), 1, "{facts:?}");
    assert_eq!(messages[0]["device"], "routines");
    assert_eq!(messages[0]["conversation"], "owner");
    assert_eq!(messages[0]["id"], format!("morning-brief@{}", MON + 8 * 3600));
    assert_eq!(messages[0]["text"], "Brief me on today.");
    assert!(facts.iter().any(|f| f["kind"] == "started" && f["conversation"] == "owner"), "a turn started: {facts:?}");
}
