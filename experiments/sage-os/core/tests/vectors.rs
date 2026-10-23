//! Golden vectors: language-neutral cases any Sage core must pass (`vectors/*.json`).
//!
//! A case is a policy and steps. Each step gives `now` and an `input`, then either the
//! `refused` code or the `facts` it must cause: each expected fact is a subset of the
//! actual one, and `seq` is its journal position. `thinking` lists the turns waiting for
//! the model afterwards, and `summarizing` the conversations waiting for a summary.
//! Finally the journal must replay to the same state.

use sage_core::{Input, Policy, State, step};
use serde_json::Value;
use std::path::Path;

#[test]
fn vectors() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../vectors");
    let mut paths: Vec<_> = std::fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    paths.sort();
    assert!(!paths.is_empty(), "no vectors found");
    for path in paths {
        run(&path);
    }
}

fn run(path: &Path) {
    let name = path.file_name().unwrap().to_string_lossy();
    let case: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let policy: Policy = case
        .get("policy")
        .map(|p| serde_json::from_value(p.clone()).unwrap())
        .unwrap_or_default();
    let (mut state, mut journal) = (State::new(policy.clone()), vec![]);
    for (i, s) in case["steps"].as_array().unwrap().iter().enumerate() {
        let at = format!("{name} step {i}");
        let input: Input = serde_json::from_value(s["input"].clone())
            .unwrap_or_else(|e| panic!("{at}: bad input: {e}"));
        match step(&state, &input, s["now"].as_f64().unwrap_or(0.0)) {
            Ok(facts) => {
                assert!(
                    s.get("refused").is_none(),
                    "{at}: expected refusal {}, got {facts:?}",
                    s["refused"]
                );
                let got: Vec<Value> = facts
                    .iter()
                    .enumerate()
                    .map(|(k, f)| {
                        let mut v = serde_json::to_value(f).unwrap();
                        v["seq"] = (state.head + k as u64 + 1).into();
                        v
                    })
                    .collect();
                if let Some(want) = s.get("facts") {
                    assert!(
                        subset(want, &Value::Array(got.clone())),
                        "{at}:\n want {want:#}\n got  {:#}",
                        Value::Array(got)
                    );
                }
                state = facts.iter().fold(state, |s, f| s.apply(f));
                journal.extend(facts);
            }
            Err(r) => assert_eq!(
                Some(r.code),
                s["refused"].as_str(),
                "{at}: unexpected refusal {r:?}"
            ),
        }
        if let Some(want) = s.get("thinking") {
            let got: Vec<u64> = state.thinking().collect();
            assert_eq!(serde_json::to_value(got).unwrap(), *want, "{at}: thinking");
        }
        if let Some(want) = s.get("summarizing") {
            let got: Vec<String> = state
                .summarizing()
                .map(|(_, conversation)| conversation)
                .collect();
            assert_eq!(
                serde_json::to_value(got).unwrap(),
                *want,
                "{at}: summarizing"
            );
        }
    }
    let replayed = journal.iter().fold(State::new(policy), |s, f| s.apply(f));
    assert_eq!(
        replayed, state,
        "{name}: the journal must replay to the same state"
    );
}

fn subset(want: &Value, got: &Value) -> bool {
    match (want, got) {
        (Value::Object(w), Value::Object(g)) => w
            .iter()
            .all(|(k, v)| g.get(k).is_some_and(|x| subset(v, x))),
        (Value::Array(w), Value::Array(g)) => {
            w.len() == g.len() && w.iter().zip(g).all(|(a, b)| subset(a, b))
        }
        (Value::Number(a), Value::Number(b)) => a.as_f64() == b.as_f64(),
        _ => want == got,
    }
}
