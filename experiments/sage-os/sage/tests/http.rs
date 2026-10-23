//! The kernel over real HTTP with a fake model: a turn end to end, auth, restart and a torn
//! journal; memory searched, summarized and written back to its files.

use sage::{Decide, Identity, Kernel, serve};
use sage_core::{Call, Input, Policy};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;

/// Echoes the newest message, streaming it a word at a time.
fn echo() -> Decide {
    Box::new(|prompt, heard| {
        let last = prompt
            .messages
            .last()
            .map(|m| m.content.rsplit("] ").next().unwrap_or("").to_owned())
            .unwrap_or_default();
        let reply = format!("echo: {last}");
        let mut said = String::new();
        for word in reply.split_inclusive(' ') {
            said.push_str(word);
            heard(&said);
            std::thread::sleep(std::time::Duration::from_millis(150));
        }
        Ok(vec![Call {
            tool: "send".into(),
            input: json!({"text": reply}),
        }])
    })
}

fn start(journal: &Path) -> (Arc<Kernel>, String) {
    let policy = Policy {
        owners: vec!["phone".into()],
        ..Policy::default()
    };
    let identity = Identity {
        persona: "You are Sage.".into(),
        memory: journal.with_file_name("memory"),
    };
    let kernel = Kernel::open(
        journal,
        policy,
        identity,
        10.0,
        echo(),
        Box::new(|_| Err("no model".into())),
    )
    .unwrap();
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let url = format!("http://{}", server.server_addr().to_ip().unwrap());
    let tokens = BTreeMap::from([("phone-token".to_owned(), "phone".to_owned())]);
    let serving = Arc::clone(&kernel);
    std::thread::spawn(move || serve(server, serving, tokens));
    (kernel, url)
}

fn call(method: &str, url: &str, token: &str, body: Option<Value>) -> (u16, Value) {
    let request = ureq::request(method, url).set("Authorization", &format!("Bearer {token}"));
    let response = match body {
        Some(body) => request.send_json(body),
        None => request.call(),
    };
    let response = match response {
        Ok(r) => r,
        Err(ureq::Error::Status(_, r)) => r,
        Err(e) => panic!("{e}"),
    };
    (response.status(), response.into_json().unwrap())
}

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("sage-test-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    dir.join("journal.jsonl")
}

#[test]
fn a_turn_over_http_survives_a_restart() {
    let journal = scratch("turn");
    let (kernel, url) = start(&journal);

    assert_eq!(
        call(
            "POST",
            &format!("{url}/v1/hello"),
            "wrong",
            Some(json!({"manifest": {}}))
        )
        .0,
        401
    );
    let (status, hello) = call(
        "POST",
        &format!("{url}/v1/hello"),
        "phone-token",
        Some(json!({"manifest": {}})),
    );
    assert_eq!(
        (status, hello["sequence"].as_u64()),
        (200, Some(2)),
        "after the kernel's own memory device"
    );

    let spoofed = json!({"kind": "message", "device": "robot", "conversation": "owner", "id": "m0", "text": "hi"});
    assert_eq!(
        call(
            "POST",
            &format!("{url}/v1/events"),
            "phone-token",
            Some(spoofed)
        )
        .1["code"],
        "invalid"
    );
    let tick = json!({"kind": "tick"});
    assert_eq!(
        call(
            "POST",
            &format!("{url}/v1/events"),
            "phone-token",
            Some(tick)
        )
        .1["code"],
        "invalid"
    );

    let message = json!({"kind": "message", "conversation": "owner", "id": "m1", "text": "hello there", "timing": {"stt_ms": 80}});
    let (_, sent) = call(
        "POST",
        &format!("{url}/v1/events"),
        "phone-token",
        Some(message),
    );
    // Stream: follow reply_delta pieces by offset until the journaled send arrives.
    let (mut since, mut offset, mut streamed, mut effects) =
        (sent["sequence"].as_u64().unwrap(), 0, String::new(), vec![]);
    for _ in 0..40 {
        let (_, got) = call(
            "GET",
            &format!("{url}/v1/effects?since={since}&wait=2&offset={offset}"),
            "phone-token",
            None,
        );
        since = got["sequence"].as_u64().unwrap();
        for effect in got["effects"].as_array().unwrap() {
            if effect["kind"] == "reply_delta" {
                assert_eq!(
                    effect["offset"], offset,
                    "a piece starts where the body asked"
                );
                streamed.push_str(effect["text"].as_str().unwrap());
                offset = streamed.chars().count();
                assert_eq!(got["working"], true);
            } else {
                effects.push(effect.clone());
            }
        }
        if !effects.is_empty() {
            assert_eq!(got["working"], false, "the turn ended with its send");
            break;
        }
    }
    assert!(
        streamed.starts_with("echo: "),
        "pieces arrived before the send: {streamed:?}"
    );
    assert_eq!(effects.len(), 1, "{effects:?}");
    assert_eq!(effects[0]["kind"], "send");
    assert_eq!(effects[0]["input"]["text"], "echo: hello there");
    assert_eq!(effects[0]["stale"], false);

    let sequence = effects[0]["sequence"].clone();
    let result = json!({"kind": "result", "effect": sequence, "status": "done"});
    assert_eq!(
        call(
            "POST",
            &format!("{url}/v1/events"),
            "phone-token",
            Some(result)
        )
        .0,
        200
    );
    let (_, beyond) = call(
        "GET",
        &format!("{url}/v1/effects?since=999"),
        "phone-token",
        None,
    );
    assert_eq!(beyond["code"], "reset");

    let head = kernel.head();
    drop(kernel);
    std::fs::OpenOptions::new()
        .append(true)
        .open(&journal)
        .unwrap()
        .write_all(b"{\"at\": 1, \"facts\": [{\"kin")
        .unwrap();
    let (again, _) = start(&journal);
    assert_eq!(
        again.head(),
        head,
        "a restart replays the journal and cuts the torn line"
    );
}

#[test]
fn memory_is_searched_summarized_and_written_back() {
    let journal = scratch("memory");
    let memory = journal.with_file_name("memory");
    std::fs::create_dir_all(memory.join("log")).unwrap();
    std::fs::write(memory.join("profile.md"), "# Abby\nAbby likes tea.\n").unwrap();
    std::fs::write(
        memory.join("log/2026-10.md"),
        "2026-10-01 ! Started at Penn\n",
    )
    .unwrap();

    // Looks up anything asked with a question mark, then says what it found.
    let decide: Decide = Box::new(|prompt, _| {
        let last = prompt.messages.last().unwrap();
        let call = if last.role == "user" && last.content.ends_with('?') {
            Call {
                tool: "search_memory".into(),
                input: json!({"query": "penn"}),
            }
        } else {
            Call {
                tool: "send".into(),
                input: json!({"text": last.content}),
            }
        };
        Ok(vec![call])
    });
    let summarize: sage::Summarize = Box::new(|prompt| {
        assert!(
            prompt.system.contains("- Abby likes tea."),
            "{}",
            prompt.system
        );
        Ok(
            json!({"summary": "Abby asked about Penn.", "add": ["Abby lives in Philadelphia."],
                  "remove": ["Abby likes tea.", "Abby owns a cat."], "log": ["! Moved to Philadelphia"]}),
        )
    });
    let policy = Policy {
        owners: vec!["phone".into()],
        summarize_s: 0.0,
        summarize_entries: 2,
        ..Policy::default()
    };
    let identity = || Identity {
        persona: "You are Sage.".into(),
        memory: memory.clone(),
    };
    let kernel = Kernel::open(
        &journal,
        policy.clone(),
        identity(),
        10.0,
        decide,
        summarize,
    )
    .unwrap();
    kernel
        .submit(Input::Hello {
            device: "phone".into(),
            manifest: Default::default(),
        })
        .unwrap();
    let message = Input::Message {
        device: "phone".into(),
        conversation: "owner".into(),
        id: "m1".into(),
        text: "Where do I work?".into(),
        timing: Default::default(),
    };
    kernel.submit(message).unwrap();

    let mut said = None;
    for _ in 0..50 {
        let got = kernel.effects("phone", 0, 0.0, 0).unwrap();
        said = got["effects"]
            .as_array()
            .unwrap()
            .iter()
            .find(|e| e["kind"] == "send")
            .cloned();
        if said.is_some() {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    let said = said.expect("a reply");
    assert_eq!(
        said["input"]["text"],
        "(search_memory done: 2026-10-01 ! Started at Penn)"
    );
    let result = Input::Result {
        device: "phone".into(),
        effect: said["sequence"].as_u64().unwrap(),
        status: sage_core::Status::Done,
        text: String::new(),
        timing: Default::default(),
    };
    kernel.submit(result).unwrap();

    kernel.submit(Input::Tick).unwrap();
    let profile = || std::fs::read_to_string(memory.join("profile.md")).unwrap();
    for _ in 0..50 {
        if profile().contains("Philadelphia") {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    assert_eq!(
        profile(),
        "# Abby\nAbby lives in Philadelphia.\n",
        "only the shown fact is removed"
    );
    let month: Vec<_> = std::fs::read_dir(memory.join("log"))
        .unwrap()
        .map(|e| e.unwrap().file_name())
        .collect();
    let log: String = month
        .iter()
        .map(|m| std::fs::read_to_string(memory.join("log").join(m)).unwrap())
        .collect();
    assert!(log.contains(" ! Moved to Philadelphia\n"), "{log}");

    let head = kernel.head();
    drop(kernel);
    let again = Kernel::open(
        &journal,
        policy,
        identity(),
        10.0,
        echo(),
        Box::new(|_| Err("no model".into())),
    )
    .unwrap();
    assert_eq!(
        again.head(),
        head,
        "unchanged files and the same memory device add nothing on restart"
    );
}
