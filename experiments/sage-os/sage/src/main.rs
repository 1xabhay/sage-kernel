//! `sage`: the kernel as one small process. Configuration comes from the environment
//! and from files in `SAGE_HOME` (persona.md, memory/profile.md, memory/log/YYYY-MM.md,
//! journal.jsonl).

use sage::{Identity, Kernel, ollama, serve};
use sage_core::{Input, Policy};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::Duration;

fn main() {
    let env = |name: &str, default: &str| std::env::var(name).unwrap_or_else(|_| default.into());
    let home = PathBuf::from(env("SAGE_HOME", "home"));
    let tokens: BTreeMap<String, String> = env("SAGE_DEVICES", "").split(',').filter_map(|pair| {
        let (device, token) = pair.trim().split_once('=')?;
        Some((token.to_owned(), device.to_owned())).filter(|(token, device)| !token.is_empty() && !device.is_empty())
    }).collect();
    if tokens.values().any(|device| device == sage::MEMORY) {
        eprintln!("sage: the device name {} is the kernel's own; rename it in SAGE_DEVICES", sage::MEMORY);
        std::process::exit(2);
    }
    if tokens.is_empty() {
        eprintln!("sage: set SAGE_DEVICES=name=token,... ; issuing a device a token is the owner's consent");
        std::process::exit(2);
    }
    let policy = Policy {
        owners: env("SAGE_OWNERS", "").split(',').map(str::trim).filter(|s| !s.is_empty()).map(String::from).collect(),
        utc_offset_s: env("SAGE_UTC_OFFSET_S", "0").parse().unwrap_or(0),
        summarize_s: env("SAGE_SUMMARIZE_S", "600").parse().unwrap_or(600.0),
        ..Policy::default()
    };
    let persona = std::fs::read_to_string(home.join("persona.md")).unwrap_or_default();
    let persona = Some(persona).filter(|p| !p.trim().is_empty()).unwrap_or_else(|| "You are Sage.".into());
    let identity = Identity { persona, memory: home.join("memory") };
    let (url, model) = (env("SAGE_MODEL_URL", "http://127.0.0.1:11434"), env("SAGE_MODEL", "qwen3:1.7b"));
    let summarize = ollama::summarizer(url.clone(), model.clone());
    let decide = ollama::decider(url, model);
    let fresh_s = env("SAGE_FRESH_SECONDS", "10").parse().unwrap_or(10.0);
    let kernel = Kernel::open(&home.join("journal.jsonl"), policy, identity, fresh_s, Box::new(decide), Box::new(summarize)).unwrap_or_else(|e| {
        eprintln!("sage: cannot open the journal in {}: {e}", home.display());
        std::process::exit(1);
    });
    let ticker = kernel.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs(1));
        if let Err(refused) = ticker.submit(Input::Tick) {
            eprintln!("sage: tick: {}", refused.message);
        }
    });
    let bind = env("SAGE_BIND", "127.0.0.1:8766");
    let server = tiny_http::Server::http(&bind).unwrap_or_else(|e| {
        eprintln!("sage: cannot listen on {bind}: {e}");
        std::process::exit(1);
    });
    eprintln!("sage: listening on {bind}, journal at {} (head {})", home.display(), kernel.head());
    serve(server, kernel, tokens);
}
