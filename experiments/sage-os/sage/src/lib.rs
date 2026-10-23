//! The Sage binary's shell around `sage-core`: it journals each step, asks the model for
//! turns that are thinking and for summaries, delivers effects to devices, checks deadlines,
//! and keeps the memory files: it plays the `memory` device that searches them, and writes
//! each summary's changes back to them.
//!
//! Devices speak the protocol in `docs/PROTOCOL.md` over HTTP. The core decides everything;
//! this file only moves bytes and keeps time.

pub mod journal;
pub mod ollama;

use journal::Journal;
use sage_core::{
    Call, Fact, Input, Manifest, Mind, Policy, Prompt, Refusal, State, Status, compile, memory,
    speech, step, summary_prompt,
};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tiny_http::{Header, Method, Request, Response, Server};

/// Asks the model. It calls `heard` with everything it has said so far, as it streams.
pub type Decide = Box<dyn Fn(&Prompt, &dyn Fn(&str)) -> Result<Vec<Call>, String> + Send + Sync>;
/// Asks the model for a summary: JSON fitting `sage_core::summary_format()`.
pub type Summarize = Box<dyn Fn(&Prompt) -> Result<Value, String> + Send + Sync>;

/// The device the shell plays to search memory. No token may name it.
pub const MEMORY: &str = "memory";

const MAX_BODY: usize = 8192;
const MAX_WAIT_S: f64 = 25.0;

/// Who this Sage is, and the folder of what it remembers: `profile.md` and `log/YYYY-MM.md`.
/// The owner may edit them; a conversation sees the change at its next summary.
pub struct Identity {
    pub persona: String,
    pub memory: PathBuf,
}

struct Inner {
    state: State,
    /// Every fact with its sequence and the time it was recorded, for deliveries.
    // ponytail: the whole journal in memory, scanned per poll; index by target when it grows.
    rows: Vec<(u64, f64, Fact)>,
    journal: Journal,
    asking: BTreeSet<u64>,
    /// What each turn's model call has said so far: streamed to its body, never journaled.
    written: BTreeMap<u64, String>,
    /// Summary requests already asked of the model in this process.
    // ponytail: a failed summary waits for the conversation's next request or a restart.
    summaries: BTreeSet<u64>,
}

pub struct Kernel {
    inner: Mutex<Inner>,
    changed: Condvar,
    decide: Decide,
    summarize: Summarize,
    identity: Identity,
    fresh_s: f64,
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("the clock is after 1970")
        .as_secs_f64()
}

fn refusal(code: &'static str, message: impl Into<String>) -> Refusal {
    Refusal {
        code,
        message: message.into(),
    }
}

impl Kernel {
    /// Replays the journal at `path`, then asks the model for any turn left thinking.
    /// Then it offers the memory search and reads the memory files, journaling either only if changed.
    pub fn open(
        path: &Path,
        policy: Policy,
        identity: Identity,
        fresh_s: f64,
        decide: Decide,
        summarize: Summarize,
    ) -> io::Result<Arc<Kernel>> {
        let (journal, steps) = Journal::open(path)?;
        let (mut state, mut rows) = (State::new(policy), vec![]);
        for (at, facts) in steps {
            for fact in facts {
                state = state.apply(&fact);
                rows.push((state.head, at, fact));
            }
        }
        let kernel = Arc::new(Kernel {
            inner: Mutex::new(Inner {
                state,
                rows,
                journal,
                asking: BTreeSet::new(),
                written: BTreeMap::new(),
                summaries: BTreeSet::new(),
            }),
            changed: Condvar::new(),
            decide,
            summarize,
            identity,
            fresh_s,
        });
        let mut inner = kernel.lock();
        let manifest = search_manifest();
        if inner
            .state
            .devices
            .get(MEMORY)
            .is_none_or(|d| !d.online || d.manifest != manifest)
        {
            kernel
                .commit(
                    &mut inner,
                    &Input::Hello {
                        device: MEMORY.into(),
                        manifest,
                    },
                )
                .map_err(|r| io::Error::other(r.message))?;
        }
        let (profile, log) = kernel.files();
        kernel
            .commit(&mut inner, &Input::Memory { profile, log })
            .map_err(|r| io::Error::other(r.message))?;
        kernel.think(&mut inner);
        drop(inner);
        Ok(kernel)
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    pub fn head(&self) -> u64 {
        self.lock().state.head
    }

    /// Applies one input: journaled before anyone is told. Returns the journal's head.
    pub fn submit(self: &Arc<Self>, input: Input) -> Result<u64, Refusal> {
        let mut inner = self.lock();
        self.commit(&mut inner, &input)
    }

    fn commit(self: &Arc<Self>, inner: &mut Inner, input: &Input) -> Result<u64, Refusal> {
        let at = now();
        let facts = step(&inner.state, input, at)?;
        if !facts.is_empty() {
            inner.journal.append(at, &facts).map_err(|e| {
                refusal(
                    "storage",
                    format!("The journal could not be written ({e}). Retry the same request."),
                )
            })?;
            let mut searches = vec![];
            for fact in facts {
                inner.state = std::mem::take(&mut inner.state).apply(&fact);
                match &fact {
                    Fact::Effect { target, call, .. } if target == MEMORY => {
                        searches.push((
                            inner.state.head,
                            call.input["query"].as_str().unwrap_or_default().to_owned(),
                        ));
                    }
                    Fact::Summarized {
                        add, remove, log, ..
                    } => self.write(add, remove, log),
                    _ => {}
                }
                inner.rows.push((inner.state.head, at, fact));
            }
            self.changed.notify_all();
            for (effect, query) in searches {
                let (profile, log) = self.files();
                let text = memory::search(&format!("{profile}\n{log}"), &query);
                let result = Input::Result {
                    device: MEMORY.into(),
                    effect,
                    status: Status::Done,
                    text,
                    timing: Default::default(),
                };
                if let Err(refused) = self.commit(inner, &result) {
                    eprintln!(
                        "sage: could not record a memory search: {}",
                        refused.message
                    );
                }
            }
            self.think(inner);
        }
        Ok(inner.state.head)
    }

    /// Asks the model, on its own thread, for each thinking turn not already being asked.
    fn think(self: &Arc<Self>, inner: &mut Inner) {
        let thinking: Vec<u64> = inner
            .state
            .thinking()
            .filter(|turn| !inner.asking.contains(turn))
            .collect();
        for turn in thinking {
            let mind = Mind {
                persona: &self.identity.persona,
            };
            let Some(prompt) = compile(&inner.state, turn, &mind) else {
                continue;
            };
            inner.asking.insert(turn);
            let (recent, senses) = (inner.state.recent_sends(turn), inner.state.senses(turn));
            let kernel = Arc::clone(self);
            std::thread::spawn(move || {
                let heard = |said: &str| {
                    // Held back while it could still become a repeat or a claim the body cannot make.
                    if speech::may_repeat(&recent, said) || speech::may_claim(said, &senses) {
                        return;
                    }
                    let mut inner = kernel.lock();
                    if inner.asking.contains(&turn) {
                        inner.written.insert(turn, said.to_owned());
                        kernel.changed.notify_all();
                    }
                };
                let answer = (kernel.decide)(&prompt, &heard);
                let mut inner = kernel.lock();
                inner.asking.remove(&turn);
                let spoken = inner
                    .written
                    .remove(&turn)
                    .map_or(0, |said| said.chars().count());
                let input = match answer {
                    Ok(calls) => Input::Decided {
                        turn,
                        calls,
                        prompt: prompt.hash(),
                        spoken,
                    },
                    Err(reason) => Input::Failed { turn, reason },
                };
                if let Err(refused) = kernel.commit(&mut inner, &input) {
                    eprintln!(
                        "sage: could not record the answer for turn {turn}: {}",
                        refused.message
                    );
                }
            });
        }
        let wanted: Vec<(u64, String)> = inner
            .state
            .summarizing()
            .filter(|(id, _)| !inner.summaries.contains(id))
            .collect();
        for (request, conversation) in wanted {
            let mind = Mind {
                persona: &self.identity.persona,
            };
            let Some(prompt) = summary_prompt(&inner.state, &conversation, &mind) else {
                continue;
            };
            inner.summaries.insert(request);
            let kernel = Arc::clone(self);
            std::thread::spawn(move || {
                let answer = match (kernel.summarize)(&prompt) {
                    Ok(answer) => answer,
                    Err(reason) => {
                        return eprintln!("sage: no summary for {conversation}: {reason}");
                    }
                };
                let lines = |key: &str| {
                    answer[key]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter_map(Value::as_str)
                        .map(String::from)
                        .collect()
                };
                let mut inner = kernel.lock();
                let (profile, log) = kernel.files();
                let summary = answer["summary"].as_str().unwrap_or_default().to_owned();
                let input = Input::Summarized {
                    conversation,
                    summary,
                    add: lines("add"),
                    remove: lines("remove"),
                    events: lines("log"),
                    profile,
                    log,
                };
                if let Err(refused) = kernel.commit(&mut inner, &input) {
                    eprintln!("sage: could not record a summary: {}", refused.message);
                }
            });
        }
    }

    /// The memory files as text: the profile, and every log month in order.
    // ponytail: reads every month each time; read only recent months once the log is large.
    fn files(&self) -> (String, String) {
        let dir = &self.identity.memory;
        let profile = fs::read_to_string(dir.join("profile.md")).unwrap_or_default();
        let mut months: Vec<PathBuf> = fs::read_dir(dir.join("log"))
            .into_iter()
            .flatten()
            .flatten()
            .map(|e| e.path())
            .collect();
        months.retain(|p| p.extension().is_some_and(|x| x == "md"));
        months.sort();
        let log: Vec<String> = months
            .iter()
            .filter_map(|p| fs::read_to_string(p).ok())
            .collect();
        (profile, log.join("\n"))
    }

    /// Writes a summary's changes to the memory files, after the journal holds them.
    // ponytail: a crash between the two leaves the files behind the journal; replay its
    // `summarized` facts into the files if that ever matters.
    fn write(&self, add: &[String], remove: &[String], log: &[String]) {
        let dir = &self.identity.memory;
        let written = (|| -> io::Result<()> {
            fs::create_dir_all(dir.join("log"))?;
            let path = dir.join("profile.md");
            let profile = fs::read_to_string(&path).unwrap_or_default();
            let revised = memory::revise(&profile, remove, add);
            if revised != profile {
                fs::write(&path, revised)?;
            }
            for line in log {
                let path = dir.join("log").join(format!("{}.md", &line[..7])); // lines start YYYY-MM-DD
                let old = fs::read_to_string(&path).unwrap_or_default();
                let gap = if old.is_empty() || old.ends_with('\n') {
                    ""
                } else {
                    "\n"
                };
                write!(
                    OpenOptions::new().create(true).append(true).open(&path)?,
                    "{gap}{line}\n"
                )?;
            }
            Ok(())
        })();
        if let Err(e) = written {
            eprintln!(
                "sage: could not write the memory files in {}: {e}",
                dir.display()
            );
        }
    }

    /// Effects delivered to `device` after `since`, and the reply it is being given past `offset`,
    /// waiting up to `wait` seconds for either to move.
    pub fn effects(
        &self,
        device: &str,
        since: u64,
        wait: f64,
        offset: usize,
    ) -> Result<Value, Refusal> {
        let inner = self.lock();
        if since > inner.state.head {
            return Err(Refusal {
                code: "reset",
                message: format!(
                    "Your cursor is beyond the journal. Fetch again from {}.",
                    inner.state.head
                ),
            });
        }
        let wait = Duration::from_secs_f64(wait.clamp(0.0, MAX_WAIT_S));
        let streaming = |inner: &Inner| {
            let turn = inner
                .state
                .turns
                .iter()
                .find(|(id, t)| t.reply_to == device && inner.written.contains_key(id))?;
            Some((*turn.0, inner.written[turn.0].clone()))
                .filter(|(_, said)| said.chars().count() > offset)
        };
        let (inner, _) = self
            .changed
            .wait_timeout_while(inner, wait, |inner| {
                inner.state.head <= since && streaming(inner).is_none()
            })
            .unwrap_or_else(|p| p.into_inner());
        let at = now();
        let effects: Vec<Value> = inner.rows.iter().filter(|(seq, _, _)| *seq > since).filter_map(|(seq, recorded, fact)| {
            let stale = at - recorded > self.fresh_s;
            match fact {
                Fact::Effect { turn, target, call, .. } if target == device => {
                    Some(json!({"sequence": seq, "kind": call.tool, "turn": turn, "input": call.input, "stale": stale}))
                }
                Fact::Stop { by, target } if target == device => Some(json!({"sequence": seq, "kind": "stop", "by": by, "stale": stale})),
                _ => None,
            }
        }).collect();
        // A piece of a reply still being written. Never alongside its turn's journaled effects,
        // so a body never mixes the offsets of two model calls.
        let mut effects = effects;
        if let Some((turn, said)) =
            streaming(&inner).filter(|(turn, _)| !effects.iter().any(|e| e["turn"] == *turn))
        {
            let text: String = said.chars().skip(offset).collect(); // offsets count characters, as clients do
            effects
                .push(json!({"kind": "reply_delta", "turn": turn, "offset": offset, "text": text}));
        }
        Ok(
            json!({"sequence": inner.state.head, "effects": effects, "working": inner.state.working(device)}),
        )
    }
}

/// The tool the shell offers as the `memory` device.
fn search_manifest() -> Manifest {
    let tool = json!({"name": "search_memory", "tier": "read", "max_s": 5,
        "description": "Search everything you remember, the profile and the dated log, for lines with all these words. \
                        Use it when the person mentions something your memory above does not hold.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}});
    serde_json::from_value(json!({"tools": [tool]})).expect("a valid manifest")
}

/// Answers devices until the server stops. Each token names one device.
// ponytail: a thread per request, and long polls hold theirs; fine for a household of devices.
pub fn serve(server: Server, kernel: Arc<Kernel>, tokens: BTreeMap<String, String>) {
    let tokens = Arc::new(tokens);
    for request in server.incoming_requests() {
        let (kernel, tokens) = (Arc::clone(&kernel), Arc::clone(&tokens));
        std::thread::spawn(move || answer(&kernel, &tokens, request));
    }
}

fn answer(kernel: &Arc<Kernel>, tokens: &BTreeMap<String, String>, mut request: Request) {
    let (status, body) = match route(kernel, tokens, &mut request) {
        Ok(body) => (200, body),
        Err(r) => {
            let status = match r.code {
                "invalid" => 400,
                "unauthorized" => 401,
                "not_found" => 404,
                "storage" => 500,
                _ => 409,
            };
            (status, json!({"code": r.code, "message": r.message}))
        }
    };
    let json = Header::from_bytes("Content-Type", "application/json").expect("a valid header");
    let _ = request.respond(
        Response::from_string(body.to_string())
            .with_status_code(status)
            .with_header(json),
    );
}

fn route(
    kernel: &Arc<Kernel>,
    tokens: &BTreeMap<String, String>,
    request: &mut Request,
) -> Result<Value, Refusal> {
    let presented = request
        .headers()
        .iter()
        .find(|h| h.field.equiv("Authorization"))
        .and_then(|h| h.value.as_str().strip_prefix("Bearer "));
    let device = presented
        .and_then(|token| device_for(tokens, token))
        .ok_or_else(|| {
            refusal(
                "unauthorized",
                "Send Authorization: Bearer <your device token>.",
            )
        })?;
    let url = request.url().to_owned();
    let (path, query) = url.split_once('?').unwrap_or((&url, ""));
    match (request.method(), path) {
        (Method::Post, "/v1/hello") => {
            let mut body = read_json(request)?;
            let manifest = serde_json::from_value(body["manifest"].take())
                .map_err(|e| refusal("invalid", format!("manifest: {e}")))?;
            let sequence = kernel.submit(Input::Hello { device, manifest })?;
            Ok(json!({"protocol": 1, "sequence": sequence}))
        }
        (Method::Post, "/v1/events") => {
            let mut body = read_json(request)?;
            let object = body
                .as_object_mut()
                .ok_or_else(|| refusal("invalid", "Send a JSON object with a kind."))?;
            if object.contains_key("device") {
                return Err(refusal(
                    "invalid",
                    "Remove device: the kernel knows the device from its token.",
                ));
            }
            if !matches!(
                object.get("kind").and_then(Value::as_str),
                Some("message" | "result" | "interrupted" | "approve" | "stop" | "gone")
            ) {
                return Err(refusal(
                    "invalid",
                    "kind is one of message, result, interrupted, approve, stop, gone.",
                ));
            }
            object.insert("device".into(), device.into());
            let input =
                serde_json::from_value(body).map_err(|e| refusal("invalid", e.to_string()))?;
            Ok(json!({"sequence": kernel.submit(input)?}))
        }
        (Method::Get, "/v1/effects") => {
            let param = |name: &str| {
                query
                    .split('&')
                    .find_map(|pair| pair.strip_prefix(name)?.strip_prefix('='))
                    .and_then(|v| v.parse::<f64>().ok())
            };
            let (since, wait, offset) = (
                param("since").unwrap_or(0.0),
                param("wait").unwrap_or(0.0),
                param("offset").unwrap_or(0.0),
            );
            kernel.effects(&device, since as u64, wait, offset as usize)
        }
        _ => Err(refusal(
            "not_found",
            "Routes: POST /v1/hello, POST /v1/events, GET /v1/effects?since=N&wait=S&offset=M.",
        )),
    }
}

fn read_json(request: &mut Request) -> Result<Value, Refusal> {
    let mut body = vec![];
    request
        .as_reader()
        .take(MAX_BODY as u64 + 1)
        .read_to_end(&mut body)
        .map_err(|e| refusal("invalid", e.to_string()))?;
    if body.len() > MAX_BODY {
        return Err(refusal("invalid", "Bodies are at most 8192 bytes."));
    }
    serde_json::from_slice(&body).map_err(|e| refusal("invalid", format!("Not JSON: {e}")))
}

/// The device a token names, compared in constant time so timing reveals nothing.
fn device_for(tokens: &BTreeMap<String, String>, presented: &str) -> Option<String> {
    let mut found = None;
    for (token, device) in tokens {
        let same = token.len() == presented.len()
            && token
                .bytes()
                .zip(presented.bytes())
                .fold(0u8, |acc, (a, b)| acc | (a ^ b))
                == 0;
        if same {
            found = Some(device.clone());
        }
    }
    found
}
