//! sage-core in a browser tab, with no server: the page keeps the journal (a file the
//! person owns, in Quila's case) and asks the core what each input causes.
//!
//! `sage_call` takes one JSON request and returns one JSON answer:
//! `{"op": "step", "policy", "journal": [facts], "input", "now"}` → `{"facts": [...]}` or `{"refused": {...}}`
//! `{"op": "compile", "policy", "journal", "turn", "persona"}` → `{"prompt": {...}}` (memory is in the journal)
//! The state is rebuilt from the journal on every call: the journal is the state.
// ponytail: replays the whole journal per call; keep a State on the wasm side if journals grow long.

use sage_core::{Fact, Input, Mind, Policy, State, compile, step};
use serde::Deserialize;
use serde_json::{Value, json};

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum Request {
    Step {
        #[serde(default)]
        policy: Policy,
        #[serde(default)]
        journal: Vec<Fact>,
        input: Input,
        now: f64,
    },
    Compile {
        #[serde(default)]
        policy: Policy,
        #[serde(default)]
        journal: Vec<Fact>,
        turn: u64,
        #[serde(default)]
        persona: String,
    },
}

/// The answer to one JSON request. Also the native entry point for tests.
pub fn call(request: &[u8]) -> Value {
    let replay = |policy: Policy, journal: &[Fact]| {
        journal.iter().fold(State::new(policy), |s, f| s.apply(f))
    };
    match serde_json::from_slice::<Request>(request) {
        Ok(Request::Step {
            policy,
            journal,
            input,
            now,
        }) => match step(&replay(policy, &journal), &input, now) {
            Ok(facts) => json!({ "facts": facts }),
            Err(refused) => json!({ "refused": refused }),
        },
        Ok(Request::Compile {
            policy,
            journal,
            turn,
            persona,
        }) => {
            let mind = Mind { persona: &persona };
            json!({ "prompt": compile(&replay(policy, &journal), turn, &mind) })
        }
        Err(error) => json!({ "refused": { "code": "invalid", "message": error.to_string() } }),
    }
}

/// Room for a request of `len` bytes; the page writes it there, then calls `sage_call`.
#[unsafe(no_mangle)]
pub extern "C" fn sage_alloc(len: usize) -> *mut u8 {
    let mut buffer = Vec::<u8>::with_capacity(len);
    let pointer = buffer.as_mut_ptr();
    std::mem::forget(buffer);
    pointer
}

/// Frees what `sage_alloc` or `sage_call` handed out.
///
/// # Safety
/// `pointer` and `len` must come from `sage_alloc` or `sage_call`, once.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn sage_free(pointer: *mut u8, len: usize) {
    drop(unsafe { Vec::from_raw_parts(pointer, len, len) });
}

/// Answers the request at `pointer` (`len` bytes, which this call frees). Returns the answer's
/// pointer in the high 32 bits and its length in the low 32; free it with `sage_free`.
///
/// # Safety
/// `pointer` and `len` must come from `sage_alloc`, filled with the request.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn sage_call(pointer: *mut u8, len: usize) -> u64 {
    let request = unsafe { Vec::from_raw_parts(pointer, len, len) };
    let mut answer = call(&request).to_string().into_bytes();
    answer.shrink_to_fit();
    let (at, size) = (answer.as_mut_ptr() as u64, answer.len() as u64);
    std::mem::forget(answer);
    at << 32 | size
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_page_can_run_a_turn_with_no_server() {
        let policy = json!({"owners": ["tab"]});
        let hello = json!({"op": "step", "policy": policy, "now": 0, "input": {"kind": "hello", "device": "tab", "manifest": {}}});
        let mut journal: Vec<Value> = call(hello.to_string().as_bytes())["facts"]
            .as_array()
            .unwrap()
            .clone();
        let message = json!({"op": "step", "policy": policy, "journal": journal, "now": 1,
                             "input": {"kind": "message", "device": "tab", "conversation": "notebook", "id": "1", "text": "Plan my day"}});
        journal.extend(
            call(message.to_string().as_bytes())["facts"]
                .as_array()
                .unwrap()
                .clone(),
        );
        let prompt = call(json!({"op": "compile", "policy": policy, "journal": journal, "turn": 3, "persona": "You are Sage."}).to_string().as_bytes());
        assert_eq!(
            prompt["prompt"]["messages"][0]["content"],
            "[1970-01-01 00:00 tab] Plan my day"
        );
        assert_eq!(call(b"nonsense")["refused"]["code"], "invalid");
    }
}
