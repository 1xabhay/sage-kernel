//! Live evals: every bot that gets something wrong becomes a case in `evals/local/cases.json`.
//!
//! `live` drives the real kernel in-process against a real model (`make eval`; ignored by
//! `make test`). The checks are plain word predicates on what was delivered, tested offline here.
// ponytail: no LLM judge; tone, relevance and helpfulness need one, word checks only catch known slips.

use serde_json::{Value, json};

/// What one message got back, as a body saw it.
#[derive(Debug, Default)]
struct Reply {
    text: String,
    emote: Option<String>,
    /// Seconds from the message to the first streamed piece, and to the journaled `send`.
    first_s: Option<f64>,
    send_s: Option<f64>,
    /// The body cut the reply off at its first piece.
    interrupted: bool,
    /// What the speech guards did, or why the turn failed, from the journal.
    note: String,
}

/// Why `reply` fails `message`'s checks and the defaults every reply must pass; empty means it passes.
/// `earlier` holds the case's earlier replies, oldest first.
fn check(
    message: &Value,
    reply: &Reply,
    earlier: &[String],
    emotes: &[String],
    send_s: f64,
) -> Vec<String> {
    if reply.interrupted {
        return vec![];
    }
    let (text, checks, mut out) = (reply.text.as_str(), &message["checks"], vec![]);
    let phrases = |key: &str| {
        checks[key]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
    };
    if text.trim().is_empty() && message["quiet_ok"] != true {
        out.push("said nothing".to_owned());
    }
    if text.contains(['{', '}']) || text.contains("\"text\"") {
        out.push("speaks JSON".to_owned());
    }
    if let Some(emote) = reply.emote.as_ref().filter(|e| !emotes.contains(e)) {
        out.push(format!("emote {emote:?} is not the body's"));
    }
    if let Some(s) = reply.send_s.filter(|s| *s > send_s) {
        out.push(format!("send after {s:.1} s, over {send_s} s"));
    }
    if message["repeat_ok"] != true
        && let Some(before) = earlier.iter().find(|before| reuses(before, text))
    {
        out.push(format!("reuses {before:?}"));
    }
    let said = phrases("none_of")
        .into_iter()
        .filter(|p| has(text, p))
        .collect::<Vec<_>>();
    if !said.is_empty() {
        out.push(format!("says {said:?}"));
    }
    let wanted = phrases("any_of");
    if !wanted.is_empty() && !wanted.iter().any(|p| has(text, p)) {
        out.push(format!("says none of {wanted:?}"));
    }
    if checks["acts"] == true
        && !sentences(text)
            .iter()
            .any(|s| s.len() >= 3 && !ANNOUNCES.contains(&s[0].as_str()))
    {
        out.push("only announces".to_owned());
    }
    if checks["quotes_previous"] == true {
        let (before, now) = (
            earlier.last().map(|b| words(b)).unwrap_or_default(),
            format!(" {} ", words(text).join(" ")),
        );
        if !before
            .windows(3)
            .any(|run| now.contains(&format!(" {} ", run.join(" "))))
        {
            out.push("does not quote its last reply".to_owned());
        }
    }
    out
}

/// Openings that announce instead of saying something: "I'll…", "Let's see…".
const ANNOUNCES: [&str; 10] = [
    "i'll",
    "let's",
    "let",
    "sure",
    "okay",
    "ok",
    "alright",
    "great",
    "yes",
    "absolutely",
];

/// Lowercase words, apostrophes kept and curly ones made straight: the same reading as `sage_core::speech`.
fn words(text: &str) -> Vec<String> {
    let text = text.to_lowercase().replace('\u{2019}', "'");
    text.split(|c: char| !(c.is_alphanumeric() || c == '\''))
        .filter(|w| !w.is_empty())
        .map(String::from)
        .collect()
}

/// Whether `text` holds `phrase` as whole words.
fn has(text: &str, phrase: &str) -> bool {
    format!(" {} ", words(text).join(" ")).contains(&format!(" {} ", words(phrase).join(" ")))
}

// ponytail: splits on every . ! ?, so "3.5" is two sentences; fine for spoken replies.
fn sentences(text: &str) -> Vec<Vec<String>> {
    text.split(['.', '!', '?'])
        .map(words)
        .filter(|s| !s.is_empty())
        .collect()
}

/// A sentence of three words or more said again, or the same first or last four words: a template filled twice.
fn reuses(before: &str, text: &str) -> bool {
    let (a, b) = (words(before), words(text));
    let template =
        a.len() >= 4 && b.len() >= 4 && (a[..4] == b[..4] || a[a.len() - 4..] == b[b.len() - 4..]);
    let old = sentences(before);
    template
        || sentences(text)
            .iter()
            .any(|s| s.len() >= 3 && old.contains(s))
}

/// The names the body's `emote` tool accepts.
fn emotes(body: &Value) -> Vec<String> {
    let tools = body["tools"].as_array().into_iter().flatten();
    let emote = tools
        .filter(|t| t["name"] == "emote")
        .flat_map(|t| t["inputSchema"]["properties"]["name"]["enum"].as_array())
        .flatten();
    emote.filter_map(Value::as_str).map(String::from).collect()
}

#[test]
fn checks_are_plain_word_predicates() {
    let emotes = emotes(
        &json!({"tools": [{"name": "emote", "inputSchema": {"properties": {"name": {"enum": ["happy", "sad"]}}}}]}),
    );
    assert_eq!(emotes, ["happy", "sad"]);
    let said = |text: &str| Reply {
        text: text.into(),
        send_s: Some(1.0),
        ..Reply::default()
    };
    let plain = json!({"text": "Hi"});
    let fails = |message: &Value, reply: &Reply, earlier: &[&str]| {
        check(
            message,
            reply,
            &earlier.iter().map(|s| s.to_string()).collect::<Vec<_>>(),
            &emotes,
            15.0,
        )
    };
    assert!(fails(&plain, &said("Hello there."), &[]).is_empty());

    let sight = json!({"text": "Can you see me?", "checks": {"none_of": ["i can see"], "any_of": ["can't see", "only hear"]}});
    assert!(
        fails(&sight, &said("I can\u{2019}t see, but I hear you."), &[]).is_empty(),
        "curly apostrophes read as plain"
    );
    assert_eq!(
        fails(&sight, &said("Yes, I can see you!"), &[]).len(),
        2,
        "says a banned phrase and none of the wanted ones"
    );
    let name = json!({"text": "?", "checks": {"any_of": ["priya"]}});
    assert!(fails(&name, &said("Her name is Priya."), &[]).is_empty());
    assert!(
        !fails(&name, &said("Her name is Priyanka."), &[]).is_empty(),
        "phrases match whole words"
    );

    assert!(
        !fails(&plain, &said(r#"{"text": "Hi!"}"#), &[]).is_empty(),
        "JSON spoken aloud"
    );
    assert!(!fails(&plain, &said(""), &[]).is_empty(), "silence");
    assert!(fails(&json!({"text": "Hi", "quiet_ok": true}), &said(""), &[]).is_empty());
    assert!(
        !fails(
            &plain,
            &Reply {
                send_s: Some(20.0),
                ..said("Hi.")
            },
            &[]
        )
        .is_empty(),
        "too slow"
    );
    assert!(
        !fails(
            &plain,
            &Reply {
                emote: Some("angry".into()),
                ..said("Hi.")
            },
            &[]
        )
        .is_empty(),
        "an emote the body lacks"
    );
    assert!(
        fails(
            &plain,
            &Reply {
                emote: Some("happy".into()),
                ..said("Hi.")
            },
            &[]
        )
        .is_empty()
    );

    let before = ["Hello! Let me know if you need any help."];
    assert!(
        !fails(
            &plain,
            &said("Sure thing. Let me know if you need any help."),
            &before
        )
        .is_empty(),
        "a reused closing"
    );
    assert!(
        !fails(&plain, &said("Hello! Let me know what you think."), &before).is_empty(),
        "a reused opening"
    );
    assert!(fails(&plain, &said("Sure thing."), &before).is_empty());
    assert!(
        fails(
            &json!({"text": "Hi", "repeat_ok": true}),
            &said("Hello! Let me know if you need any help."),
            &before
        )
        .is_empty()
    );

    let act = json!({"text": "Go ahead.", "checks": {"acts": true}});
    assert!(
        !fails(&act, &said("I'll tell you something. Let's see..."), &[]).is_empty(),
        "only announces"
    );
    assert!(fails(&act, &said("Okay! Octopuses have three hearts."), &[]).is_empty());
    let quote = json!({"text": "What did you just say?", "repeat_ok": true, "checks": {"quotes_previous": true}});
    assert!(
        fails(
            &quote,
            &said("I said octopuses have three hearts."),
            &["Octopuses have three hearts."]
        )
        .is_empty()
    );
    assert!(
        !fails(
            &quote,
            &said("I said hello."),
            &["Octopuses have three hearts."]
        )
        .is_empty()
    );

    assert!(
        fails(
            &plain,
            &Reply {
                interrupted: true,
                ..said("")
            },
            &[]
        )
        .is_empty(),
        "a cut-off reply is not judged"
    );
}

/// Runs every case against the model, prints a table, and prints a ledger entry for `make eval` to keep.
#[test]
#[ignore = "live: needs a model server; run with make eval"]
fn live() {
    let env = |name: &str, default: &str| std::env::var(name).unwrap_or_else(|_| default.into());
    let (url, model) = (
        env("SAGE_EVAL_MODEL_URL", "http://127.0.0.1:11434"),
        env("SAGE_MODEL", "qwen3:1.7b"),
    );
    let only = env("SAGE_EVAL_CASE", "");
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../evals/local/cases.json");
    let suite: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let send_s = suite["send_s"].as_f64().unwrap_or(15.0);
    // Loaded first, so no case pays for the model coming off disk (43 s once, live).
    ureq::post(&format!("{url}/api/generate"))
        .send_json(json!({"model": model, "prompt": "", "keep_alive": "30m"}))
        .expect("the model server answers");
    let (mut rows, mut failed, mut firsts, mut sends, mut guarded) =
        (vec![], vec![], vec![], vec![], 0);
    for case in suite["cases"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|c| c["name"].as_str().unwrap().contains(&only))
    {
        let (name, body) = (
            case["name"].as_str().unwrap(),
            &suite["bodies"][case["body"].as_str().unwrap_or("reachy")],
        );
        let replies = converse(
            &url,
            &model,
            suite["persona"].as_str().unwrap_or("You are Sage."),
            name,
            body,
            &case["messages"],
        );
        let (mut earlier, mut bad, mut case_firsts, mut case_sends) = (vec![], 0, vec![], vec![]);
        println!("\n{name}");
        for (message, reply) in case["messages"].as_array().unwrap().iter().zip(&replies) {
            let fails = check(message, reply, &earlier, &emotes(body), send_s);
            let clock = |s: Option<f64>| s.map_or("-".to_owned(), |s| format!("{s:.2}"));
            println!(
                "  {} {:?} -> {:?} [{}] first {} s, send {} s{}{}",
                if fails.is_empty() { "ok  " } else { "FAIL" },
                message["text"].as_str().unwrap(),
                reply.text,
                reply.emote.as_deref().unwrap_or(""),
                clock(reply.first_s),
                clock(reply.send_s),
                if reply.interrupted { " (cut off)" } else { "" },
                if reply.note.is_empty() {
                    String::new()
                } else {
                    format!("\n       guard: {}", reply.note)
                }
            );
            for fail in &fails {
                println!("       - {fail}");
                failed.push(format!(
                    "`{name}` {:?} -> {:?}: {fail}",
                    message["text"].as_str().unwrap(),
                    reply.text
                ));
            }
            bad += !fails.is_empty() as usize;
            guarded += !reply.note.is_empty() as usize;
            if !reply.interrupted {
                case_firsts.extend(reply.first_s.or(reply.send_s));
                case_sends.extend(reply.send_s);
                earlier.push(reply.text.clone());
            }
        }
        rows.push((
            name.to_owned(),
            bad,
            replies.len(),
            percentile(&case_firsts, 0.5),
            percentile(&case_sends, 0.5),
            percentile(&case_sends, 1.0),
        ));
        firsts.extend(case_firsts);
        sends.extend(case_sends);
    }
    println!("\n{model}: first words = first streamed piece or the send, whichever came first\n");
    println!(
        "{:<28} {:<5} {:>7} {:>11} {:>10} {:>10}",
        "case", "pass", "replies", "first p50 s", "send p50 s", "send max s"
    );
    for (name, bad, n, first, send, max) in &rows {
        println!(
            "{name:<28} {:<5} {:>7} {first:>11.2} {send:>10.2} {max:>10.2}",
            if *bad == 0 { "yes" } else { "NO" },
            format!("{}/{n}", n - bad)
        );
    }
    let passed = rows.iter().filter(|r| r.1 == 0).count();
    let replies: usize = rows.iter().map(|r| r.2).sum();
    let entry = [
        format!(
            "## {} · `{model}` · {}",
            env("SAGE_EVAL_DATE", "undated"),
            env("SAGE_EVAL_COMMIT", "uncommitted")
        ),
        String::new(),
        format!(
            "- **{passed}/{} cases pass**, {}/{replies} replies. First words p50 {:.2} s, p95 {:.2} s; send p50 {:.2} s, p95 {:.2} s (N = {}).",
            rows.len(),
            replies - rows.iter().map(|r| r.1).sum::<usize>(),
            percentile(&firsts, 0.5),
            percentile(&firsts, 0.95),
            percentile(&sends, 0.5),
            percentile(&sends, 0.95),
            sends.len()
        ),
        format!(
            "- Speech guards stepped in on {guarded} replies (the model's own words were retried or fixed before delivery)."
        ),
    ];
    println!();
    for line in entry.iter().chain(
        failed
            .iter()
            .map(|f| format!("- Failed: {f}"))
            .collect::<Vec<_>>()
            .iter(),
    ) {
        println!("ledger: {line}");
    }
    assert!(failed.is_empty(), "{} checks failed", failed.len());
}

fn percentile(values: &[f64], p: f64) -> f64 {
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    sorted
        .get(((p * sorted.len() as f64).ceil() as usize).saturating_sub(1))
        .copied()
        .unwrap_or(f64::NAN)
}

/// One conversation with a fresh kernel, as a body: hello, then each message once the last turn ended,
/// reporting every effect done (or cutting the reply off at its first piece when the message says `interrupt`).
fn converse(
    url: &str,
    model: &str,
    persona: &str,
    name: &str,
    body: &Value,
    messages: &Value,
) -> Vec<Reply> {
    use sage::{Identity, Kernel, ollama};
    use sage_core::{Input, Policy, Status};
    use std::time::Instant;
    let dir = std::env::temp_dir().join(format!("sage-eval-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let journal = dir.join("journal.jsonl");
    let policy = Policy {
        owners: vec!["robot".into()],
        ..Policy::default()
    };
    let identity = Identity {
        persona: persona.into(),
        memory: journal.with_file_name("memory"),
    };
    let kernel = Kernel::open(
        &journal,
        policy,
        identity,
        10.0,
        Box::new(ollama::decider(url.into(), model.into())),
        Box::new(ollama::summarizer(url.into(), model.into())),
    )
    .unwrap();
    let device = || "robot".to_owned();
    kernel
        .submit(Input::Hello {
            device: device(),
            manifest: serde_json::from_value(body.clone()).unwrap(),
        })
        .unwrap();
    let mut replies = vec![];
    for (i, message) in messages.as_array().unwrap().iter().enumerate() {
        let text = message["text"].as_str().unwrap().to_owned();
        let start = Instant::now();
        let turn = kernel
            .submit(Input::Message {
                device: device(),
                conversation: name.into(),
                id: format!("m{i}"),
                text,
                timing: Default::default(),
            })
            .unwrap();
        let (mut since, mut offset, mut reply) = (turn, 0, Reply::default());
        while start.elapsed().as_secs() < 90 {
            let got = kernel.effects("robot", since, 5.0, offset).unwrap();
            since = got["sequence"].as_u64().unwrap();
            for effect in got["effects"].as_array().unwrap() {
                let at = Some(start.elapsed().as_secs_f64());
                match effect["kind"].as_str().unwrap() {
                    "reply_delta" if !reply.interrupted => {
                        reply.first_s = reply.first_s.or(at);
                        reply.text.push_str(effect["text"].as_str().unwrap());
                        offset = reply.text.chars().count();
                        if message["checks"]["interrupt"] == true {
                            let (turn, text) =
                                (effect["turn"].as_u64().unwrap(), reply.text.clone());
                            let _ = kernel.submit(Input::Interrupted {
                                device: device(),
                                turn,
                                text,
                                timing: Default::default(),
                            });
                            reply.interrupted = true;
                        }
                    }
                    "reply_delta" | "stop" => {}
                    kind => {
                        if kind == "send" && !reply.interrupted {
                            (reply.send_s, offset) = (at, 0);
                            reply.text = effect["input"]["text"]
                                .as_str()
                                .unwrap_or_default()
                                .to_owned();
                        } else if kind == "emote" {
                            reply.emote = effect["input"]["name"].as_str().map(String::from);
                        }
                        let result = Input::Result {
                            device: device(),
                            effect: effect["sequence"].as_u64().unwrap(),
                            status: Status::Done,
                            text: String::new(),
                            timing: Default::default(),
                        };
                        let _ = kernel.submit(result); // a turn already over refuses it; nothing to do
                    }
                }
            }
            if got["working"] == false {
                break;
            }
        }
        reply.note = guards(&journal, turn);
        replies.push(reply);
    }
    replies
}

/// What the journal says the speech guards did to `turn`, or why it failed: what the model wrote, not what was delivered.
fn guards(journal: &std::path::Path, turn: u64) -> String {
    let text = std::fs::read_to_string(journal).unwrap_or_default();
    let facts = text
        .lines()
        .filter_map(|l| serde_json::from_str::<Value>(l).ok())
        .flat_map(|l| l["facts"].as_array().cloned().unwrap_or_default());
    let mut notes = vec![];
    for fact in facts.filter(|f| f["turn"] == turn) {
        match fact["kind"].as_str().unwrap_or_default() {
            "rejected" => notes.push(format!(
                "retried ({})",
                fact["reason"].as_str().unwrap_or_default()
            )),
            "decided" if !fact["raw"].is_null() => {
                notes.push(format!("fixed, the model wrote {}", fact["raw"]))
            }
            "refused" => notes.push(format!(
                "refused {}: {}",
                fact["call"],
                fact["reason"].as_str().unwrap_or_default()
            )),
            "ended" if fact["end"] != "completed" => notes.push(format!(
                "ended {}: {}",
                fact["end"],
                fact["reason"].as_str().unwrap_or_default()
            )),
            _ => {}
        }
    }
    notes.join("; ")
}
