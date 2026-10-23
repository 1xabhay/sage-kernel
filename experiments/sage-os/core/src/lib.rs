//! Sage core: the pure kernel. No clock, file, network or thread.
//!
//! `step(state, input, now)` returns the facts an input causes, or a coded refusal.
//! `State::apply` folds one fact. The journal is the list of facts, so the state is
//! always the fold of the journal: any run replays without a model, and any prompt
//! can be rebuilt exactly (`compile`).
//!
//! The model proposes (`Input::Decided`), the kernel decides (tiers, schemas, approval),
//! bodies act (`Fact::Effect`, `Fact::Stop`) and report (`Input::Result`).

pub mod memory;
mod prompt;
mod schema;
pub mod speech;

pub use prompt::{Message, Mind, Prompt, compile, summary_format, summary_prompt};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

/// The one built-in tool: the only way to speak. Anything else the model writes is private.
pub const SEND: &str = "send";
/// Model calls one turn may make before it ends as failed.
pub const MAX_STEPS: u32 = 16;
/// Seconds past a tool's `max_s` before its effect fails with `timeout`.
pub const GRACE_S: f64 = 2.0;
const MAX_TOOLS: usize = 64;
const MAX_TEXT: usize = 4000;
const MAX_ID: usize = 128;

/// Whole milliseconds a device measured for a turn.
pub type Timing = BTreeMap<String, u64>;
/// From end of speech to transcript; from message sent to first reply text; from end of speech to first audio.
pub const TIMING: [&str; 3] = ["stt_ms", "ttft_ms", "first_audio_ms"];

/// Who may let an effect happen. Ordered from least to most risk.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Tier {
    /// Search, fetch, read a file: automatic.
    Read,
    /// Speak, emote, look: automatic, bounded, fire and forget.
    Body,
    /// Write files, run commands: automatic only on a sandboxed device.
    Change,
    /// Messages, purchases, posts, the host: the owner approves; no answer means no.
    Outside,
}

/// A tool as MCP's `tools/list` shows it, plus the kernel's own fields.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Tool {
    pub name: String,
    #[serde(default)]
    pub description: String,
    #[serde(rename = "inputSchema")]
    pub input_schema: Value,
    /// A tool that names no tier gets the strictest.
    #[serde(default = "outside")]
    pub tier: Tier,
    /// The longest the device may take; no `max_s`, no timeout.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_s: Option<f64>,
}

fn outside() -> Tier {
    Tier::Outside
}

/// What a device can do. Issuing its token is the owner's consent: it is trusted as declared.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Manifest {
    #[serde(default)]
    pub senses: Vec<String>,
    #[serde(default)]
    pub tools: Vec<Tool>,
    /// Its change-tier tools run in a sandbox, so they need no approval.
    #[serde(default)]
    pub sandbox: bool,
}

/// The owner's rules. Configuration, not code.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Policy {
    /// Devices that speak for the owner: their messages are instructions and they may approve.
    #[serde(default)]
    pub owners: Vec<String>,
    /// Seconds an approval waits; no answer means no.
    #[serde(default = "approval_s")]
    pub approval_s: f64,
    /// Seconds a device has to report a `send`.
    #[serde(default = "reply_s")]
    pub reply_s: f64,
    /// The owner's offset from UTC, for the times the model reads.
    #[serde(default)]
    pub utc_offset_s: i64,
    /// Seconds a conversation stays quiet before it is summarized, while the model's cache is warm.
    #[serde(default = "summarize_s")]
    pub summarize_s: f64,
    /// Entries a conversation must gain after its last summary before it is worth summarizing.
    #[serde(default = "summarize_entries")]
    pub summarize_entries: usize,
}

fn approval_s() -> f64 {
    15.0
}

fn reply_s() -> f64 {
    30.0
}

fn summarize_s() -> f64 {
    600.0
}

fn summarize_entries() -> usize {
    10
}

impl Default for Policy {
    fn default() -> Self {
        Policy {
            owners: vec![],
            approval_s: approval_s(),
            reply_s: reply_s(),
            utc_offset_s: 0,
            summarize_s: summarize_s(),
            summarize_entries: summarize_entries(),
        }
    }
}

/// One tool the model chose.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Call {
    pub tool: String,
    #[serde(default)]
    pub input: Value,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Status {
    Done,
    Failed,
    Interrupted,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum End {
    Completed,
    Stopped,
    Failed,
}

/// What reaches the kernel. Devices are named by the shell from their tokens, never by themselves.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Input {
    Hello {
        device: String,
        manifest: Manifest,
    },
    Gone {
        device: String,
    },
    /// Anything that wakes the agent: the owner, a routine, a helper, another agent.
    Message {
        device: String,
        conversation: String,
        id: String,
        text: String,
        #[serde(default)]
        timing: Timing,
    },
    /// The model's answer for a turn. `prompt` is the hash of what it was sent; `spoken`,
    /// how many characters of its `send` were already streamed to the body.
    Decided {
        turn: u64,
        calls: Vec<Call>,
        #[serde(default)]
        prompt: String,
        #[serde(default)]
        spoken: usize,
    },
    /// The model could not answer.
    Failed {
        turn: u64,
        reason: String,
    },
    Result {
        device: String,
        effect: u64,
        status: Status,
        #[serde(default)]
        text: String,
        #[serde(default)]
        timing: Timing,
    },
    /// A body cut off a reply it was still receiving piece by piece; `text` is what was heard.
    Interrupted {
        device: String,
        turn: u64,
        #[serde(default)]
        text: String,
        #[serde(default)]
        timing: Timing,
    },
    Approve {
        device: String,
        pending: u64,
        yes: bool,
    },
    /// From any device, at any time. Without a conversation it stops everything.
    Stop {
        device: String,
        #[serde(default)]
        conversation: Option<String>,
    },
    /// Time passed: deadlines are checked, and quiet conversations are summarized.
    Tick,
    /// The memory files as they are now (`profile.md`, and the log months in order), from the shell.
    Memory {
        #[serde(default)]
        profile: String,
        #[serde(default)]
        log: String,
    },
    /// The model's summary of a quiet conversation and its changes to memory: facts to `add`
    /// and `remove`, and `events` for the log. `profile` and `log` are the files as they are now.
    Summarized {
        conversation: String,
        summary: String,
        #[serde(default)]
        add: Vec<String>,
        #[serde(default)]
        remove: Vec<String>,
        #[serde(default)]
        events: Vec<String>,
        #[serde(default)]
        profile: String,
        #[serde(default)]
        log: String,
    },
}

/// A journal row. Its sequence number is its position in the journal, from 1;
/// turns, effects and pending approvals are named by the sequence of the fact that began them.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Fact {
    Hello {
        device: String,
        manifest: Manifest,
    },
    Gone {
        device: String,
    },
    Message {
        device: String,
        conversation: String,
        id: String,
        text: String,
        at: f64,
        #[serde(default, skip_serializing_if = "Timing::is_empty")]
        timing: Timing,
    },
    /// A turn takes every message waiting in its conversation.
    Started {
        conversation: String,
    },
    /// `calls` as delivered; `raw`, what the model wrote, when a speech guard changed it.
    Decided {
        turn: u64,
        calls: Vec<Call>,
        prompt: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        raw: Option<Vec<Call>>,
    },
    /// A speech guard refused the answer: nothing was delivered, and the model tries again.
    Rejected {
        turn: u64,
        reason: String,
    },
    /// Delivered to `target`. `wait`: the turn waits for its result before thinking again.
    Effect {
        turn: u64,
        target: String,
        call: Call,
        deadline: Option<f64>,
        wait: bool,
    },
    /// Waits for the owner's approval until `deadline`.
    Pending {
        turn: u64,
        target: String,
        call: Call,
        deadline: f64,
    },
    Approved {
        device: String,
        pending: u64,
    },
    Denied {
        pending: u64,
        reason: String,
    },
    /// A call the kernel would not deliver; the model reads why.
    Refused {
        turn: u64,
        call: Call,
        reason: String,
    },
    /// The turn needs the model again.
    Resume {
        turn: u64,
    },
    Result {
        device: String,
        effect: u64,
        status: Status,
        text: String,
        #[serde(default, skip_serializing_if = "Timing::is_empty")]
        timing: Timing,
    },
    /// What was heard of a reply cut off while streaming; the turn ends with it.
    Interrupted {
        device: String,
        turn: u64,
        text: String,
        #[serde(default, skip_serializing_if = "Timing::is_empty")]
        timing: Timing,
    },
    /// Delivered to `target`: stop what you are doing.
    Stop {
        by: String,
        target: String,
    },
    Ended {
        turn: u64,
        end: End,
        reason: String,
        #[serde(default)]
        at: f64,
    },
    /// The memory snapshot that conversations beginning from now on see.
    Memory {
        memory: String,
    },
    /// The conversation has been quiet long enough: the shell asks the model for a summary.
    Summarize {
        conversation: String,
    },
    /// The conversation now starts from `summary` and its last replies, and sees `memory`.
    /// `add`, `remove` and `log` are the changes the shell writes to the memory files.
    Summarized {
        conversation: String,
        summary: String,
        add: Vec<String>,
        remove: Vec<String>,
        log: Vec<String>,
        memory: String,
    },
}

/// A coded refusal; the message says what to do next.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Refusal {
    pub code: &'static str,
    pub message: String,
}

fn refuse<T>(code: &'static str, message: impl Into<String>) -> Result<T, Refusal> {
    Err(Refusal {
        code,
        message: message.into(),
    })
}

#[derive(Clone, Debug, PartialEq)]
pub struct Device {
    pub manifest: Manifest,
    pub online: bool,
}

/// One entry of a conversation, as the model reads it. Only ever appended to, except that
/// an interrupted `send` is cut to what was heard.
#[derive(Clone, Debug, PartialEq)]
pub enum Entry {
    In {
        device: String,
        text: String,
        at: f64,
        owner: bool,
    },
    Out {
        calls: Vec<Call>,
    },
    Outcome {
        tool: String,
        status: Status,
        text: String,
    },
    /// What came before, as the model summarized it.
    Summary {
        text: String,
    },
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Conversation {
    pub queue: Vec<Entry>,
    pub history: Vec<Entry>,
    pub running: Option<u64>,
    /// The owner device that spoke last: where `send` goes.
    pub reply_to: String,
    /// The memory snapshot this conversation sees. It changes only at its summary, so the
    /// start of its prompt stays the same until then.
    pub memory: String,
    /// The `Summarize` fact waiting for its summary.
    pub summarizing: Option<u64>,
    /// When its last turn ended.
    pub quiet_since: f64,
    /// The history entries its last summary began with.
    pub summarized: usize,
}

impl Conversation {
    /// The last `speech::RECENT` things said, oldest first.
    fn said(&self) -> Vec<String> {
        let history = &self.history;
        // A send followed by its own failure (refused or rejected) was never heard.
        let unheard = |i: usize| matches!(history.get(i + 1), Some(Entry::Outcome { tool, status: Status::Failed, .. }) if tool == SEND);
        let said = history
            .iter()
            .enumerate()
            .filter(|(i, _)| !unheard(*i))
            .filter_map(|(_, e)| match e {
                Entry::Out { calls } => calls
                    .iter()
                    .find(|c| c.tool == SEND)
                    .and_then(|c| c.input["text"].as_str()),
                _ => None,
            });
        let said: Vec<String> = said
            .filter(|t| !t.trim().is_empty())
            .map(String::from)
            .collect();
        said[said.len().saturating_sub(speech::RECENT)..].to_vec()
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Turn {
    pub conversation: String,
    pub reply_to: String,
    pub thinking: bool,
    pub steps: u32,
    /// Effects and approvals whose outcome the model needs before thinking again.
    pub waits: BTreeSet<u64>,
    /// Answers a speech guard refused; one retry is allowed.
    pub rejections: u32,
    /// The first history entry of this turn.
    pub from: usize,
    out: usize,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Open {
    pub turn: u64,
    pub conversation: String,
    pub target: String,
    pub call: Call,
    pub deadline: Option<f64>,
    out: usize,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Waiting {
    pub turn: u64,
    pub target: String,
    pub call: Call,
    pub deadline: f64,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct State {
    pub policy: Policy,
    pub head: u64,
    pub devices: BTreeMap<String, Device>,
    pub conversations: BTreeMap<String, Conversation>,
    pub turns: BTreeMap<u64, Turn>,
    /// Delivered effects with no result yet: their targets are acting.
    pub open: BTreeMap<u64, Open>,
    pub pending: BTreeMap<u64, Waiting>,
    /// The memory snapshot that new conversations see.
    pub memory: String,
    // ponytail: grows with every message; bound it by age once journals are long.
    seen: BTreeSet<(String, String)>,
}

impl State {
    pub fn new(policy: Policy) -> Self {
        State {
            policy,
            ..State::default()
        }
    }

    /// Turns waiting for the model. The shell compiles their prompts and calls it.
    pub fn thinking(&self) -> impl Iterator<Item = u64> + '_ {
        self.turns
            .iter()
            .filter(|(_, t)| t.thinking)
            .map(|(id, _)| *id)
    }

    /// Conversations waiting for a summary, with the `Summarize` fact that asked.
    pub fn summarizing(&self) -> impl Iterator<Item = (u64, String)> + '_ {
        self.conversations
            .iter()
            .filter_map(|(name, c)| Some((c.summarizing?, name.clone())))
    }

    /// Whether a turn is running or waiting to run that will answer `device`.
    pub fn working(&self, device: &str) -> bool {
        self.turns.values().any(|t| t.reply_to == device)
            || self
                .conversations
                .values()
                .any(|c| c.reply_to == device && !c.queue.is_empty())
    }

    /// The last `speech::RECENT` things said in the conversation of `turn`, oldest first.
    pub fn recent_sends(&self, turn: u64) -> Vec<String> {
        let c = self
            .turns
            .get(&turn)
            .and_then(|t| self.conversations.get(&t.conversation));
        c.map(Conversation::said).unwrap_or_default()
    }

    /// The senses of the body `turn` answers.
    pub fn senses(&self, turn: u64) -> Vec<String> {
        let device = self
            .turns
            .get(&turn)
            .and_then(|t| self.devices.get(&t.reply_to));
        device
            .map(|d| d.manifest.senses.clone())
            .unwrap_or_default()
    }

    /// The device that offers `tool`, preferring `near` (the body being talked to).
    fn offer(&self, tool: &str, near: &str) -> Option<(String, Tool, bool, bool)> {
        let offers = |(name, device): (&String, &Device)| {
            let found = device.manifest.tools.iter().find(|t| t.name == tool)?;
            Some((
                name.clone(),
                found.clone(),
                device.manifest.sandbox,
                device.online,
            ))
        };
        self.devices
            .get_key_value(near)
            .and_then(offers)
            .or_else(|| self.devices.iter().find_map(offers))
    }

    /// Where a reply meant for `device` goes: there if it is connected, else the first owner device online.
    // ponytail: streamed pieces and `working` still follow the turn's own device; a reply that moves
    // to another body arrives whole.
    fn reach(&self, device: &str) -> Result<String, String> {
        let online = |name: &str| self.devices.get(name).is_some_and(|d| d.online);
        if online(device) {
            return Ok(device.into());
        }
        if let Some(owner) = self.policy.owners.iter().find(|owner| online(owner)) {
            return Ok(owner.clone());
        }
        Err(match device {
            "" => "unavailable: no one to send to yet".into(),
            _ => format!("unavailable: {device} is not connected; try again when it is back"),
        })
    }

    fn outcome(&mut self, turn: u64, tool: &str, status: Status, text: &str) {
        let Some(t) = self.turns.get(&turn) else {
            return;
        };
        if let Some(c) = self.conversations.get_mut(&t.conversation) {
            c.history.push(Entry::Outcome {
                tool: tool.into(),
                status,
                text: text.into(),
            });
        }
    }

    /// The state after `fact`. Never fails: every fact was checked by `step`.
    pub fn apply(mut self, fact: &Fact) -> Self {
        self.head += 1;
        let seq = self.head;
        match fact {
            Fact::Hello { device, manifest } => {
                self.devices.insert(
                    device.clone(),
                    Device {
                        manifest: manifest.clone(),
                        online: true,
                    },
                );
            }
            Fact::Gone { device } => {
                if let Some(d) = self.devices.get_mut(device) {
                    d.online = false;
                }
            }
            Fact::Message {
                device,
                conversation,
                id,
                text,
                at,
                ..
            } => {
                self.seen.insert((device.clone(), id.clone()));
                let owner = self.policy.owners.contains(device);
                let memory = &self.memory;
                let c = self
                    .conversations
                    .entry(conversation.clone())
                    .or_insert_with(|| Conversation {
                        memory: memory.clone(),
                        ..Conversation::default()
                    });
                // Only a device that said hello can hear a reply; a routine never becomes the place.
                if owner && self.devices.contains_key(device) {
                    c.reply_to = device.clone();
                }
                c.queue.push(Entry::In {
                    device: device.clone(),
                    text: text.clone(),
                    at: *at,
                    owner,
                });
            }
            Fact::Started { conversation } => {
                let c = self.conversations.entry(conversation.clone()).or_default();
                let from = c.history.len();
                c.history.append(&mut c.queue);
                c.running = Some(seq);
                c.summarizing = None; // awake again: the warm cache is used by this turn instead
                let reply_to = c.reply_to.clone();
                let turn = Turn {
                    conversation: conversation.clone(),
                    reply_to,
                    thinking: true,
                    steps: 0,
                    waits: BTreeSet::new(),
                    rejections: 0,
                    from,
                    out: from,
                };
                self.turns.insert(seq, turn);
            }
            Fact::Decided { turn, calls, .. } => {
                if let Some(t) = self.turns.get_mut(turn).filter(|t| t.thinking) {
                    t.thinking = false;
                    t.steps += 1;
                    if let Some(c) = self.conversations.get_mut(&t.conversation) {
                        t.out = c.history.len();
                        c.history.push(Entry::Out {
                            calls: calls.clone(),
                        });
                    }
                }
            }
            Fact::Effect {
                turn,
                target,
                call,
                deadline,
                wait,
            } => {
                let (conversation, out) = match self.turns.get_mut(turn) {
                    Some(t) => {
                        if *wait {
                            t.waits.insert(seq);
                        }
                        (t.conversation.clone(), t.out)
                    }
                    None => (String::new(), 0),
                };
                let open = Open {
                    turn: *turn,
                    conversation,
                    target: target.clone(),
                    call: call.clone(),
                    deadline: *deadline,
                    out,
                };
                self.open.insert(seq, open);
            }
            Fact::Pending {
                turn,
                target,
                call,
                deadline,
            } => {
                if let Some(t) = self.turns.get_mut(turn) {
                    t.waits.insert(seq);
                }
                self.pending.insert(
                    seq,
                    Waiting {
                        turn: *turn,
                        target: target.clone(),
                        call: call.clone(),
                        deadline: *deadline,
                    },
                );
            }
            Fact::Approved { pending, .. } | Fact::Denied { pending, .. } => {
                if let Some(p) = self.pending.remove(pending) {
                    if let Some(t) = self.turns.get_mut(&p.turn) {
                        t.waits.remove(pending);
                    }
                    if let Fact::Denied { reason, .. } = fact {
                        self.outcome(
                            p.turn,
                            &p.call.tool,
                            Status::Failed,
                            &format!("denied: {reason}"),
                        );
                    }
                }
            }
            Fact::Refused { turn, call, reason } => {
                self.outcome(*turn, &call.tool, Status::Failed, reason)
            }
            Fact::Rejected { turn, reason } => {
                if let Some(t) = self.turns.get_mut(turn) {
                    t.rejections += 1;
                }
                self.outcome(*turn, SEND, Status::Failed, reason);
            }
            Fact::Resume { turn } => {
                if let Some(t) = self.turns.get_mut(turn) {
                    t.thinking = true;
                }
            }
            Fact::Result {
                effect,
                status,
                text,
                ..
            } => {
                if let Some(o) = self.open.remove(effect) {
                    if let Some(t) = self.turns.get_mut(&o.turn) {
                        t.waits.remove(effect);
                    }
                    if let Some(c) = self.conversations.get_mut(&o.conversation) {
                        if o.call.tool == SEND {
                            // Memory keeps only what the person heard.
                            if *status == Status::Interrupted {
                                if let Some(Entry::Out { calls }) = c.history.get_mut(o.out) {
                                    if let Some(call) =
                                        calls.iter_mut().find(|call| **call == o.call)
                                    {
                                        let heard = format!("{text} (interrupted)");
                                        call.input = json!({ "text": heard.trim_start() });
                                    }
                                }
                            }
                        } else if *status != Status::Done || !text.is_empty() {
                            c.history.push(Entry::Outcome {
                                tool: o.call.tool.clone(),
                                status: *status,
                                text: text.clone(),
                            });
                        }
                    }
                }
            }
            Fact::Interrupted { turn, text, .. } => {
                // Memory keeps only what the person heard.
                if let Some(t) = self.turns.get(turn) {
                    if let Some(c) = self.conversations.get_mut(&t.conversation) {
                        let heard = format!("{text} (interrupted)");
                        let send = Call {
                            tool: SEND.into(),
                            input: json!({ "text": heard.trim_start() }),
                        };
                        c.history.push(Entry::Out { calls: vec![send] });
                    }
                }
            }
            Fact::Stop { .. } => {}
            Fact::Ended { turn, at, .. } => {
                if let Some(t) = self.turns.remove(turn) {
                    if let Some(c) = self.conversations.get_mut(&t.conversation) {
                        c.running = None;
                        c.quiet_since = *at;
                    }
                }
            }
            Fact::Memory { memory } => self.memory = memory.clone(),
            Fact::Summarize { conversation } => {
                if let Some(c) = self.conversations.get_mut(conversation) {
                    c.summarizing = Some(seq);
                }
            }
            Fact::Summarized {
                conversation,
                summary,
                memory,
                ..
            } => {
                if let Some(c) = self.conversations.get_mut(conversation) {
                    let send = |text: String| Entry::Out {
                        calls: vec![Call {
                            tool: SEND.into(),
                            input: json!({ "text": text }),
                        }],
                    };
                    let kept = c.said().into_iter().map(send); // the last replies, word for word
                    c.history = std::iter::once(Entry::Summary {
                        text: summary.clone(),
                    })
                    .chain(kept)
                    .collect();
                    c.summarized = c.history.len();
                    c.memory = memory.clone();
                    c.summarizing = None;
                }
                self.memory = memory.clone();
            }
        }
        self
    }
}

/// The facts `input` causes at time `now` (seconds, by the kernel's clock), or why it is refused.
/// Retrying an input after a lost response is always safe: a repeat causes nothing.
pub fn step(state: &State, input: &Input, now: f64) -> Result<Vec<Fact>, Refusal> {
    let mut tx = Tx {
        s: state.clone(),
        facts: vec![],
        now,
    };
    match input {
        Input::Hello { device, manifest } => {
            if let Err(message) = check_manifest(manifest) {
                return refuse("invalid", message);
            }
            tx.emit(Fact::Hello {
                device: device.clone(),
                manifest: manifest.clone(),
            });
        }
        Input::Gone { device } => {
            if tx.s.devices.get(device).is_some_and(|d| d.online) {
                tx.emit(Fact::Gone {
                    device: device.clone(),
                });
            }
        }
        Input::Message {
            device,
            conversation,
            id,
            text,
            timing,
        } => {
            check_timing(timing)?;
            if !(1..=MAX_ID).contains(&id.len()) || !(1..=MAX_ID).contains(&conversation.len()) {
                return refuse("invalid", "id and conversation need 1 to 128 characters");
            }
            if text.trim().is_empty() || text.len() > MAX_TEXT {
                return refuse("invalid", "text needs 1 to 4000 characters");
            }
            if tx.s.seen.contains(&(device.clone(), id.clone())) {
                return Ok(vec![]);
            }
            let message = Fact::Message {
                device: device.clone(),
                conversation: conversation.clone(),
                id: id.clone(),
                text: text.clone(),
                at: now,
                timing: timing.clone(),
            };
            tx.emit(message);
            tx.pump(conversation);
        }
        Input::Decided {
            turn,
            calls,
            prompt,
            spoken,
        } => tx.decided(*turn, calls, prompt, *spoken, now),
        Input::Failed { turn, reason } => tx.end(*turn, End::Failed, reason),
        Input::Result {
            device,
            effect,
            status,
            text,
            timing,
        } => {
            check_timing(timing)?;
            if text.len() > MAX_TEXT {
                return refuse("invalid", "text must be at most 4000 characters");
            }
            let Some(open) = tx.s.open.get(effect).cloned() else {
                return Ok(vec![]);
            };
            if open.target != *device {
                return refuse(
                    "forbidden",
                    "That effect was delivered to another device; only it may report the result.",
                );
            }
            tx.emit(Fact::Result {
                device: device.clone(),
                effect: *effect,
                status: *status,
                text: text.clone(),
                timing: timing.clone(),
            });
            tx.resume(open.turn);
        }
        Input::Interrupted {
            device,
            turn,
            text,
            timing,
        } => {
            check_timing(timing)?;
            if text.len() > MAX_TEXT {
                return refuse("invalid", "text must be at most 4000 characters");
            }
            let streaming =
                tx.s.turns
                    .get(turn)
                    .filter(|t| t.thinking)
                    .map(|t| t.reply_to == *device);
            // The reply may have been decided meanwhile: then the cut applies to its send.
            let decided =
                tx.s.open
                    .iter()
                    .find(|(_, o)| o.turn == *turn && o.target == *device && o.call.tool == SEND)
                    .map(|(id, _)| *id);
            match (streaming, decided) {
                (Some(true), _) => {
                    tx.emit(Fact::Interrupted {
                        device: device.clone(),
                        turn: *turn,
                        text: text.clone(),
                        timing: timing.clone(),
                    });
                    tx.end(*turn, End::Stopped, &format!("interrupted by {device}"));
                }
                (Some(false), _) => {
                    return refuse(
                        "forbidden",
                        "Only the device a turn answers may interrupt its reply.",
                    );
                }
                (None, Some(effect)) => {
                    tx.emit(Fact::Result {
                        device: device.clone(),
                        effect,
                        status: Status::Interrupted,
                        text: text.clone(),
                        timing: timing.clone(),
                    });
                    tx.resume(*turn);
                }
                (None, None) => {} // already settled: a retry changes nothing
            }
        }
        Input::Approve {
            device,
            pending,
            yes,
        } => {
            if !tx.s.policy.owners.contains(device) {
                return refuse("forbidden", "Only an owner's device may approve.");
            }
            let Some(p) = tx.s.pending.get(pending).cloned() else {
                return Ok(vec![]);
            };
            if *yes {
                let max_s =
                    tx.s.offer(&p.call.tool, &p.target)
                        .and_then(|(_, tool, _, _)| tool.max_s);
                tx.emit(Fact::Approved {
                    device: device.clone(),
                    pending: *pending,
                });
                let deadline = max_s.map(|s| now + s + GRACE_S);
                tx.emit(Fact::Effect {
                    turn: p.turn,
                    target: p.target,
                    call: p.call,
                    deadline,
                    wait: true,
                });
            } else {
                tx.emit(Fact::Denied {
                    pending: *pending,
                    reason: format!("{device} said no"),
                });
                tx.resume(p.turn);
            }
        }
        Input::Stop {
            device,
            conversation,
        } => {
            let scope = |c: &String| conversation.as_ref().is_none_or(|want| want == c);
            let acting: BTreeSet<String> =
                tx.s.open
                    .values()
                    .filter(|o| scope(&o.conversation))
                    .map(|o| o.target.clone())
                    .collect();
            let turns: Vec<u64> =
                tx.s.turns
                    .iter()
                    .filter(|(_, t)| scope(&t.conversation))
                    .map(|(id, _)| *id)
                    .collect();
            for target in acting {
                tx.emit(Fact::Stop {
                    by: device.clone(),
                    target,
                });
            }
            for turn in turns {
                tx.end(turn, End::Stopped, &format!("stopped by {device}"));
            }
        }
        Input::Tick => {
            let late: Vec<(u64, u64)> =
                tx.s.open
                    .iter()
                    .filter(|(_, o)| o.deadline.is_some_and(|d| now > d))
                    .map(|(id, o)| (*id, o.turn))
                    .collect();
            for (effect, turn) in late {
                tx.emit(Fact::Result {
                    device: "kernel".into(),
                    effect,
                    status: Status::Failed,
                    text: "timeout".into(),
                    timing: Timing::new(),
                });
                tx.resume(turn);
            }
            let unanswered: Vec<(u64, u64)> =
                tx.s.pending
                    .iter()
                    .filter(|(_, p)| now > p.deadline)
                    .map(|(id, p)| (*id, p.turn))
                    .collect();
            for (pending, turn) in unanswered {
                tx.emit(Fact::Denied {
                    pending,
                    reason: "no answer".into(),
                });
                tx.resume(turn);
            }
            // Quiet, grown, and nothing still acting or waiting that would wake it anyway.
            // ponytail: knows nothing of routines yet; skip when one is due within minutes once they exist.
            let policy = &tx.s.policy;
            let quiet: Vec<String> =
                tx.s.conversations
                    .iter()
                    .filter(|(name, c)| {
                        c.running.is_none()
                            && c.queue.is_empty()
                            && c.summarizing.is_none()
                            && c.history.len() >= c.summarized + policy.summarize_entries
                            && now - c.quiet_since >= policy.summarize_s
                            && !tx.s.open.values().any(|o| o.conversation == **name)
                    })
                    .map(|(name, _)| name.clone())
                    .collect();
            for conversation in quiet {
                tx.emit(Fact::Summarize { conversation });
            }
        }
        Input::Memory { profile, log } => {
            let memory = memory::snapshot(profile, log, now);
            if memory != tx.s.memory {
                tx.emit(Fact::Memory { memory });
            }
        }
        Input::Summarized {
            conversation,
            summary,
            add,
            remove,
            events,
            profile,
            log,
        } => {
            // Asked for no longer (it woke meanwhile) or never: nothing changes.
            let Some(c) =
                tx.s.conversations
                    .get(conversation)
                    .filter(|c| c.summarizing.is_some())
            else {
                return Ok(vec![]);
            };
            if summary.trim().is_empty() || summary.len() > MAX_TEXT {
                return refuse("invalid", "summary needs 1 to 4000 characters");
            }
            let one_line = |s: &String| s.split_whitespace().collect::<Vec<_>>().join(" ");
            // Only a fact the model was shown may be removed; anything else it names is ignored.
            let shown = memory::shown(&c.memory);
            let mut gone: Vec<String> = vec![];
            for fact in remove.iter().map(one_line).filter(|f| shown.contains(f)) {
                if !gone.contains(&fact) {
                    gone.push(fact);
                }
            }
            // New facts while the profile still fits; the model removes old ones to make room.
            let mut added: Vec<String> = vec![];
            for fact in add.iter().map(one_line).filter(|f| !f.is_empty()) {
                let before = memory::revise(profile, &gone, &added);
                let after = memory::revise(profile, &gone, &[&added[..], &[fact.clone()]].concat());
                if after != before && memory::fits(&after) {
                    added.push(fact);
                }
            }
            let lines = memory::dated(events, log, now, tx.s.policy.utc_offset_s);
            let memory = memory::snapshot(
                &memory::revise(profile, &gone, &added),
                &format!("{log}\n{}", lines.join("\n")),
                now,
            );
            let summary = summary.trim().to_owned();
            tx.emit(Fact::Summarized {
                conversation: conversation.clone(),
                summary,
                add: added,
                remove: gone,
                log: lines,
                memory,
            });
        }
    }
    Ok(tx.facts)
}

/// One step's facts, applied as they are emitted so each decision sees the ones before it.
// ponytail: clones the state once per step; use persistent maps if the state grows large.
struct Tx {
    s: State,
    facts: Vec<Fact>,
    now: f64,
}

impl Tx {
    fn emit(&mut self, fact: Fact) {
        self.s = std::mem::take(&mut self.s).apply(&fact);
        self.facts.push(fact);
    }

    /// Starts the conversation's next turn when it is idle and messages are waiting.
    fn pump(&mut self, conversation: &str) {
        let idle = self
            .s
            .conversations
            .get(conversation)
            .is_some_and(|c| c.running.is_none() && !c.queue.is_empty());
        if idle {
            self.emit(Fact::Started {
                conversation: conversation.into(),
            });
        }
    }

    /// The turn thinks again once nothing it waits on is left.
    fn resume(&mut self, turn: u64) {
        if self
            .s
            .turns
            .get(&turn)
            .is_some_and(|t| !t.thinking && t.waits.is_empty())
        {
            self.emit(Fact::Resume { turn });
        }
    }

    /// Ends a running turn: its approvals are denied, then the next turn may start.
    fn end(&mut self, turn: u64, end: End, reason: &str) {
        let Some(conversation) = self.s.turns.get(&turn).map(|t| t.conversation.clone()) else {
            return;
        };
        let pending: Vec<u64> = self
            .s
            .pending
            .iter()
            .filter(|(_, p)| p.turn == turn)
            .map(|(id, _)| *id)
            .collect();
        for id in pending {
            self.emit(Fact::Denied {
                pending: id,
                reason: "the turn ended".into(),
            });
        }
        self.emit(Fact::Ended {
            turn,
            end,
            reason: reason.into(),
            at: self.now,
        });
        self.pump(&conversation);
    }

    fn decided(&mut self, turn: u64, calls: &[Call], prompt: &str, spoken: usize, now: f64) {
        let Some(t) = self.s.turns.get(&turn).filter(|t| t.thinking).cloned() else {
            // Stopped meanwhile: recorded, nothing delivered.
            self.emit(Fact::Decided {
                turn,
                calls: calls.to_vec(),
                prompt: prompt.into(),
                raw: None,
            });
            return;
        };
        let (calls, raw) = match self.guard(turn, &t, calls, spoken) {
            Guarded::Pass => (calls.to_vec(), None),
            Guarded::Fixed(fixed) => (fixed, Some(calls.to_vec())),
            Guarded::Retry(reason) => {
                self.emit(Fact::Decided {
                    turn,
                    calls: calls.to_vec(),
                    prompt: prompt.into(),
                    raw: None,
                });
                self.emit(Fact::Rejected { turn, reason });
                self.resume(turn);
                return;
            }
        };
        self.emit(Fact::Decided {
            turn,
            calls: calls.clone(),
            prompt: prompt.into(),
            raw,
        });
        let calls = &calls[..];
        let mut again = false;
        for call in calls {
            let refused = |reason: String| Fact::Refused {
                turn,
                call: call.clone(),
                reason,
            };
            if call.tool == SEND {
                if let Err(problem) = schema::check(&send_schema(), &call.input, "input") {
                    self.emit(refused(format!("invalid input: {problem}")));
                } else if call.input["text"]
                    .as_str()
                    .is_some_and(|text| !text.trim().is_empty())
                {
                    match self.s.reach(&t.reply_to) {
                        Err(reason) => self.emit(refused(reason)),
                        Ok(target) => {
                            let deadline = Some(now + self.s.policy.reply_s);
                            self.emit(Fact::Effect {
                                turn,
                                target,
                                call: call.clone(),
                                deadline,
                                wait: false,
                            });
                        }
                    }
                } // an empty send is quiet, a decision like any other
                continue;
            }
            let Some((target, tool, sandbox, online)) = self.s.offer(&call.tool, &t.reply_to)
            else {
                again = true;
                self.emit(refused(format!(
                    "unavailable: no device offers {}",
                    call.tool
                )));
                continue;
            };
            again |= tool.tier != Tier::Body;
            if !online {
                self.emit(refused(format!(
                    "unavailable: {target} is not connected; try again when it is back"
                )));
            } else if let Err(problem) = schema::check(&tool.input_schema, &call.input, "input") {
                self.emit(refused(format!("invalid input: {problem}")));
            } else if tool.tier <= Tier::Body || tool.tier == Tier::Change && sandbox {
                let deadline = tool.max_s.map(|s| now + s + GRACE_S);
                self.emit(Fact::Effect {
                    turn,
                    target,
                    call: call.clone(),
                    deadline,
                    wait: tool.tier != Tier::Body,
                });
            } else {
                let deadline = now + self.s.policy.approval_s;
                self.emit(Fact::Pending {
                    turn,
                    target,
                    call: call.clone(),
                    deadline,
                });
            }
        }
        if !again {
            self.end(turn, End::Completed, "");
        } else if self
            .s
            .turns
            .get(&turn)
            .is_some_and(|t| t.steps >= MAX_STEPS)
        {
            self.end(turn, End::Failed, "too many steps");
        } else {
            self.resume(turn);
        }
    }
}

fn check_timing(timing: &Timing) -> Result<(), Refusal> {
    match timing.keys().find(|key| !TIMING.contains(&key.as_str())) {
        Some(key) => refuse(
            "invalid",
            format!("timing {key} is unknown; use {}", TIMING.join(", ")),
        ),
        None => Ok(()),
    }
}

enum Guarded {
    Pass,
    Retry(String),
    Fixed(Vec<Call>),
}

impl Tx {
    /// Checks what the answer says against the speech guards. A refused answer delivers
    /// nothing and is retried once; after that, or once its start was streamed, only the
    /// part not yet spoken is fixed.
    fn guard(&self, turn: u64, t: &Turn, calls: &[Call], spoken: usize) -> Guarded {
        let Some((index, text)) = calls.iter().enumerate().find_map(|(i, c)| {
            Some((i, c.input["text"].as_str()?.to_owned()))
                .filter(|_| c.tool == SEND)
                .filter(|(_, text)| !text.trim().is_empty())
        }) else {
            return Guarded::Pass;
        };
        let (recent, senses) = (self.s.recent_sends(turn), self.s.senses(turn));
        let Some(reason) = speech::problem(&recent, &text, &senses) else {
            return Guarded::Pass;
        };
        if spoken == 0 && t.rejections == 0 {
            return Guarded::Retry(reason);
        }
        let head: String = text.chars().take(spoken).collect();
        let rest: String = text.chars().skip(spoken).collect();
        let fixed = format!("{head} {}", speech::fix(&rest, &recent, &senses));
        let fixed = if spoken == 0 && fixed.trim().is_empty() {
            text
        } else {
            fixed.trim().to_owned()
        }; // never silence
        let mut calls = calls.to_vec();
        calls[index].input = json!({ "text": fixed });
        Guarded::Fixed(calls)
    }
}

fn send_schema() -> Value {
    json!({"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": false})
}

fn check_manifest(manifest: &Manifest) -> Result<(), String> {
    if manifest.tools.len() > MAX_TOOLS {
        return Err(format!("A manifest holds at most {MAX_TOOLS} tools"));
    }
    let mut names = BTreeSet::new();
    for tool in &manifest.tools {
        let name = &tool.name;
        let well_formed = name.len() <= 64
            && name.starts_with(|c: char| c.is_ascii_lowercase())
            && name
                .chars()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_');
        if !well_formed || name == SEND || !names.insert(name) {
            return Err(
                "Each tool needs a unique name matching [a-z][a-z0-9_]{0,63}, other than send"
                    .into(),
            );
        }
        if tool.input_schema.get("type") != Some(&json!("object")) {
            return Err(format!(
                "Tool {name}: inputSchema must be a JSON Schema with type object"
            ));
        }
        if tool.description.len() + tool.input_schema.to_string().len() > MAX_TEXT {
            return Err(format!(
                "Tool {name}: keep the description and schema under 4,000 characters"
            ));
        }
        if tool.max_s.is_some_and(|s| !(s >= 0.0)) {
            return Err(format!(
                "Tool {name}: max_s must be a number of seconds of at least 0"
            ));
        }
    }
    Ok(())
}
