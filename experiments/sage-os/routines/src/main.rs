//! `sage-routines`: posts due routines from `SAGE_HOME/routines/*.md` to the kernel at
//! `SAGE_KERNEL_URL`, as the device whose token is `SAGE_ROUTINES_TOKEN`.

use sage_routines::Runner;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("the clock is after 1970")
        .as_secs() as i64
}

fn main() {
    let env = |name: &str, default: &str| std::env::var(name).unwrap_or_else(|_| default.into());
    let token = env("SAGE_ROUTINES_TOKEN", "");
    if token.is_empty() {
        eprintln!(
            "sage-routines: set SAGE_ROUTINES_TOKEN to the token SAGE_DEVICES gives `routines`"
        );
        std::process::exit(2);
    }
    let dir = std::path::PathBuf::from(env("SAGE_HOME", "home")).join("routines");
    let url = env("SAGE_KERNEL_URL", "http://127.0.0.1:8766");
    // ponytail: a fixed UTC offset like the kernel's; DST moves routines an hour until the offset is changed.
    let offset = env("SAGE_UTC_OFFSET_S", "0").parse().unwrap_or(0);
    eprintln!("sage-routines: reading {}, posting to {url}", dir.display());
    let mut runner = Runner::new(dir, url, token, offset, now());
    loop {
        let at = now();
        let next = runner.tick(at);
        std::thread::sleep(Duration::from_secs((next - now()).clamp(1, 60) as u64));
    }
}
