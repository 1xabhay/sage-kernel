//! Decisions from a local model through Ollama's native `/api/chat`.
//!
//! The answer is constrained by `format` to one JSON object: `send` (what to say, maybe
//! nothing) and one optional property per tool, shaped by its `inputSchema`. The owner's
//! bench chose this over native tool calls (24/24 valid against 0–7/24 on qwen3:1.7b).

use sage_core::{Call, Prompt, SEND, summary_format};
use serde_json::{Map, Value, json};
use std::io::{BufRead, BufReader};
use std::time::Duration;

/// A decision function for the kernel, asking `model` at `url`.
pub fn decider(
    url: String,
    model: String,
) -> impl Fn(&Prompt, &dyn Fn(&str)) -> Result<Vec<Call>, String> + Send + Sync {
    move |prompt: &Prompt, heard: &dyn Fn(&str)| {
        let body = json!({"model": model, "messages": messages(prompt), "format": format(&prompt.tools),
                          "stream": true, "think": false, "options": {"temperature": if prompt.retry { 0.9 } else { 0.4 }}});
        let response = ureq::post(&format!("{url}/api/chat"))
            .timeout(Duration::from_secs(60))
            .send_json(body)
            .map_err(|e| format!("model: {e}"))?;
        // Streamed as NDJSON: what `send` says so far goes to the body before the answer is done.
        let (mut content, mut said) = (String::new(), String::new());
        for line in BufReader::new(response.into_reader()).lines() {
            let line = line.map_err(|e| format!("model: {e}"))?;
            if line.trim().is_empty() {
                continue;
            }
            let chunk: Value = serde_json::from_str(&line).map_err(|e| format!("model: {e}"))?;
            if let Some(error) = chunk["error"].as_str() {
                return Err(format!("model: {error}"));
            }
            content.push_str(chunk["message"]["content"].as_str().unwrap_or_default());
            let now = partial_send(&content);
            if now != said {
                said = now;
                heard(&said);
            }
            if chunk["done"] == true {
                break;
            }
        }
        calls(&content, &prompt.tools)
    }
}

/// A summary function for the kernel, asking `model` at `url`. Its messages start exactly as
/// the last turn's did, so Ollama reuses the cached start; only `format` differs.
pub fn summarizer(
    url: String,
    model: String,
) -> impl Fn(&Prompt) -> Result<Value, String> + Send + Sync {
    move |prompt: &Prompt| {
        let body = json!({"model": model, "messages": messages(prompt), "format": summary_format(),
                          "stream": false, "think": false, "options": {"temperature": 0.2}});
        let response: Value = ureq::post(&format!("{url}/api/chat"))
            .timeout(Duration::from_secs(120))
            .send_json(body)
            .map_err(|e| format!("model: {e}"))?
            .into_json()
            .map_err(|e| format!("model: {e}"))?;
        let content = response["message"]["content"]
            .as_str()
            .ok_or("model: no answer")?;
        serde_json::from_str(content).map_err(|e| format!("model: not JSON: {e}"))
    }
}

/// The prompt as Ollama chat messages: the system prompt with the tools and answer shape, then the conversation.
fn messages(prompt: &Prompt) -> Vec<Value> {
    let tools = prompt
        .tools
        .iter()
        .filter(|t| t["name"] != SEND)
        .map(|t| {
            format!(
                "- {}: {} Input: {}",
                t["name"].as_str().unwrap_or(""),
                t["description"].as_str().unwrap_or(""),
                t["inputSchema"]
            )
        })
        .collect::<Vec<_>>();
    let tools = if tools.is_empty() {
        "none".to_owned()
    } else {
        tools.join("\n")
    };
    let mut messages = vec![
        json!({"role": "system", "content": format!("{}\n\nAnswer in JSON. \"send\" is the plain words \
        you say, or \"\" to stay quiet. To use a tool, add a property named after it, holding its input.\nTools:\n{tools}", prompt.system)}),
    ];
    // The model's own past turns are shown in the shape it answers in, or a small model copies the other shape.
    messages.extend(prompt.messages.iter().map(|m| match m.role {
        "assistant" => json!({"role": m.role, "content": answer_form(&m.content)}),
        _ => json!({"role": m.role, "content": m.content}),
    }));
    messages
}

/// What the `send` string of a streamed answer says so far: `{"send": "Hel` reads `Hel`.
fn partial_send(answer: &str) -> String {
    let Some(start) = answer.find("\"send\"") else {
        return String::new();
    };
    let rest = answer[start + 6..].trim_start();
    let Some(rest) = rest
        .strip_prefix(':')
        .map(str::trim_start)
        .and_then(|r| r.strip_prefix('"'))
    else {
        return String::new();
    };
    let (mut said, mut chars) = (String::new(), rest.chars());
    while let Some(c) = chars.next() {
        match c {
            '"' => break,
            '\\' => {
                let decoded = match chars.next() {
                    Some('n') => Some('\n'),
                    Some('t') => Some('\t'),
                    Some('r') => Some('\r'),
                    Some('u') => {
                        let hex: String = chars.by_ref().take(4).collect();
                        if hex.len() < 4 {
                            break; // the escape is still arriving
                        }
                        u32::from_str_radix(&hex, 16).ok().and_then(char::from_u32)
                    }
                    Some(other) => Some(other), // \" \\ \/ and the rest
                    None => break,
                };
                said.extend(decoded);
            }
            c => said.push(c),
        }
    }
    said
}

/// The JSON Schema the answer must fit.
fn format(tools: &[Value]) -> Value {
    let mut properties = Map::new();
    properties.insert(SEND.into(), json!({"type": "string"}));
    for tool in tools.iter().filter(|t| t["name"] != SEND) {
        properties.insert(
            tool["name"].as_str().unwrap_or_default().into(),
            tool["inputSchema"].clone(),
        );
    }
    json!({"type": "object", "properties": properties, "required": [SEND], "additionalProperties": false})
}

/// Recorded calls, `[{"tool": "send", "input": {"text": "Hi"}}]`, as the answer that made them: `{"send": "Hi"}`.
fn answer_form(calls: &str) -> String {
    let Ok(calls) = serde_json::from_str::<Vec<Call>>(calls) else {
        return calls.to_owned();
    };
    let mut answer = Map::new();
    answer.insert(SEND.into(), json!(""));
    for call in calls {
        let value = if call.tool == SEND {
            call.input["text"].clone()
        } else {
            call.input
        };
        answer.insert(call.tool, value);
    }
    Value::Object(answer).to_string()
}

/// The answer as tool calls: `send` first, then the tools in the order they are offered.
fn calls(content: &str, tools: &[Value]) -> Result<Vec<Call>, String> {
    let answer: Map<String, Value> =
        serde_json::from_str(content).map_err(|e| format!("model: not JSON: {e}"))?;
    let mut calls = vec![];
    if let Some(text) = answer.get(SEND).and_then(Value::as_str) {
        calls.push(Call {
            tool: SEND.into(),
            input: json!({"text": text}),
        });
    }
    for tool in tools
        .iter()
        .filter_map(|t| t["name"].as_str())
        .filter(|name| *name != SEND)
    {
        if let Some(input) = answer.get(tool) {
            calls.push(Call {
                tool: tool.into(),
                input: input.clone(),
            });
        }
    }
    Ok(calls)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_answer_becomes_calls() {
        let tools = [
            json!({"name": "send"}),
            json!({"name": "emote", "inputSchema": {"type": "object"}}),
        ];
        let got = calls(r#"{"emote": {"name": "happy"}, "send": "Hi!"}"#, &tools).unwrap();
        assert_eq!(
            got,
            vec![
                Call {
                    tool: "send".into(),
                    input: json!({"text": "Hi!"})
                },
                Call {
                    tool: "emote".into(),
                    input: json!({"name": "happy"})
                },
            ]
        );
        assert!(calls("not json", &tools).is_err());
        assert_eq!(
            format(&tools)["properties"]["emote"],
            json!({"type": "object"})
        );
    }

    #[test]
    fn past_turns_read_in_the_answer_shape() {
        let recorded =
            r#"[{"tool":"send","input":{"text":"Hi!"}},{"tool":"emote","input":{"name":"happy"}}]"#;
        assert_eq!(
            answer_form(recorded),
            r#"{"send":"Hi!","emote":{"name":"happy"}}"#
        );
        assert_eq!(answer_form("[]"), r#"{"send":""}"#);
    }

    #[test]
    fn speech_streams_before_the_answer_is_complete() {
        assert_eq!(partial_send(""), "");
        assert_eq!(partial_send(r#"{"send": "Hel"#), "Hel");
        assert_eq!(
            partial_send(r#"{"send":"Say \"hi\"\n and"#),
            "Say \"hi\"\n and"
        );
        assert_eq!(
            partial_send(r#"{"send": "caf\u00e9!", "emote": {"#),
            "café!"
        );
        assert_eq!(
            partial_send(r#"{"send": "caf\u00"#),
            "caf",
            "a half-arrived escape waits"
        );
        assert_eq!(partial_send(r#"{"send": "ok\"#), "ok");
        let keys: Vec<_> = format(&[json!({"name": "emote", "inputSchema": {}})])["properties"]
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect();
        assert_eq!(
            keys,
            ["send", "emote"],
            "send comes first so speech streams first"
        );
    }
}
