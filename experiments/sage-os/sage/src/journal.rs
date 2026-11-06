//! The journal file: one JSON line per step, holding every fact that step caused.
//!
//! A line is written whole and synced before anyone is told, so a step is either in the
//! journal or not. A line torn by a crash is cut off when the journal is opened.

use sage_core::Fact;
use serde::{Deserialize, Serialize};
use std::fs::{File, OpenOptions};
use std::io::{self, BufRead, BufReader, Write};
use std::path::Path;

#[derive(Serialize)]
struct LineRef<'a> {
    at: f64,
    facts: &'a [Fact],
}

#[derive(Deserialize)]
struct Line {
    at: f64,
    facts: Vec<Fact>,
}

pub struct Journal {
    file: File,
}

/// One recorded step: its time and the facts it produced.
pub type Step = (f64, Vec<Fact>);

impl Journal {
    /// Opens or creates the journal; returns it with every step already recorded.
    pub fn open(path: &Path) -> io::Result<(Journal, Vec<Step>)> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let mut options = OpenOptions::new();
        options.read(true).append(true).create(true);
        #[cfg(unix)]
        std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
        let file = options.open(path)?;
        let (mut steps, mut good) = (vec![], 0u64);
        for line in BufReader::new(&file).split(b'\n') {
            let line = line?;
            match serde_json::from_slice::<Line>(&line) {
                Ok(step) => {
                    steps.push((step.at, step.facts));
                    good += line.len() as u64 + 1;
                }
                Err(_) => break, // torn by a crash: everything after it is cut off below
            }
        }
        if good < file.metadata()?.len() {
            eprintln!("sage: cutting a torn last line from {}", path.display());
            file.set_len(good)?;
        }
        Ok((Journal { file }, steps))
    }

    /// Writes one step's facts and syncs them to disk.
    pub fn append(&mut self, at: f64, facts: &[Fact]) -> io::Result<()> {
        let mut line = serde_json::to_vec(&LineRef { at, facts })?;
        line.push(b'\n');
        self.file.write_all(&line)?;
        self.file.sync_data()
    }
}
