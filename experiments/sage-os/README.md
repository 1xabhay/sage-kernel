# Sage OS

The kernel of an operating system whose processor is a language model, written
from scratch in Rust. It draws on what we learned in the Python kernel at the
[monorepo root](../../README.md) (once the `sage` and `sage-kernel` repos), the Reachy
pet, Quila and Worldmonitor; the reasons are in
[`docs/history/sage/VISION.md`](../../docs/history/sage/VISION.md). Those projects keep
running until this one passes their cases.

**The model proposes, the kernel decides, bodies act.**

## Shape

```
core/      sage-core: the pure kernel. No clock, file, network or thread.
           Built to run anywhere the same: a server, a robot, a browser tab.
sage/      the binary: a journal file, the device protocol over HTTP
           (docs/PROTOCOL.md), local model calls through Ollama, a ticker.
routines/  sage-routines: a device that posts due routines as messages (below)
wasm/      sage-core for a browser tab with no server: JSON in, JSON out (`make wasm`)
vectors/   golden cases any implementation must pass (JSON, language-neutral)
```

Measured on 2026-10-20 (Mac, Colima, `qwen3:1.7b`): a 2.5 MB binary, 752 KiB of
memory and 0.01% CPU when idle. Streamed: the first spoken piece 0.26 s after the
message and the whole reply at 1.1 s, warm (2.6 s on a conversation's first turn). It recalls across turns and container rebuilds, and uses `emote` as a
real tool call.

Its home (`SAGE_HOME`, `/data` in Docker) holds `journal.jsonl`, `persona.md` and
`memory/`: `profile.md` (lasting facts about the owner, one per line) and
`log/YYYY-MM.md` (dated events, `YYYY-MM-DD text`; `YYYY-MM-DD ! text` matters for
months). They are plain text the owner may edit. The kernel journals a snapshot of them,
so every prompt rebuilds from the journal alone: at most 100 facts in 4,000 characters,
and the 30 log lines that rank highest (important ones higher, older ones lower). Lines
left out are found with the `search_memory` tool, which the binary offers as its own
`memory` device.

## The core in one screen

- `step(state, input, now) -> facts | refusal`. The input is a message, the model's
  decision, a result, an approval, a Stop or a tick.
- `state.apply(fact) -> state`. **The journal is the list of facts.** The state is
  always the journal folded from the start, so any run replays without a model and
  any prompt can be rebuilt exactly.
- `compile(state, turn, mind) -> prompt`, in a fixed order: tools, then the system
  prompt (persona, rules, memory snapshot), then the conversation, which only ever
  grows at the end. Each part is hashed, and a cache miss is a bug.
- **Summarize while away.** When a conversation has grown (`summarize_entries`, 10) and
  been quiet for `summarize_s` (600) with nothing acting or waiting, a tick asks for a
  summary on the same, still warm prompt start (`summary_prompt`). The summary pass also
  adds facts, removes facts it was shown, and adds log lines; the binary writes those to
  the files. The conversation then starts from the summary, its last three replies word
  for word and a fresh memory snapshot: the only time its prompt start changes.

| Rule | Where it is checked |
| --- | --- |
| One turn at a time per conversation. Messages wait, then go to the model together | `02-queue` |
| A repeated message ID causes nothing; a retry is always safe | `02-queue`, `01-turn` |
| `send` is the only way to speak. Saying nothing is a decision | `01-turn` |
| Model, tools, repeat: the turn waits only for results the model needs | `03-tools` |
| An unknown or offline tool, or bad input, is refused with a reason the model reads | `03-tools`, `05-body-stop` |
| Risk tiers: read and body run automatically, change only in a sandbox, outside needs the owner. A tool that names no tier is outside | `04-approval`, `06-manifest` |
| No answer to an approval means no | `04-approval` |
| Stop from any device reaches whichever body is acting | `05-body-stop` |
| Every effect has a deadline and one result; only its target may report it | `01-turn`, `05-body-stop` |
| Memory keeps only what was heard | `memory_keeps_only_what_was_heard` |
| Messages from anyone but the owner are labelled information; the prompt start never moves | `the_start_of_the_prompt_never_moves` |
| A body cut off mid-stream: memory keeps what was heard, a late answer delivers nothing | `07-streaming` |
| No repeated replies or reused openings and closings, no claimed senses: retried once, then fixed | `08-guards` |
| A quiet, grown conversation is summarized on the warm prompt, never while anything acts or waits; waking first cancels it | `09-summary` |
| A summary may remove only profile facts it was shown; new facts stop when the profile is full | `09-summary`, `memory` tests |
| The memory snapshot comes from the journal and changes only at the conversation's summary; the last replies are kept word for word | `a_summary_starts_the_next_prompt` |
| Memory is searched and written back to the owner's files; a restart with unchanged files adds nothing | `memory_is_searched_summarized_and_written_back` |

## Run

```sh
make test   # unit tests, golden vectors and HTTP tests, in Docker (rust 1.99.1, pinned)
make up     # the kernel on 127.0.0.1:8786; needs SAGE_DEVICES and SAGE_OWNERS in .env
make down
make lock   # after changing dependencies
make eval   # live conversations (evals/local/cases.json) against Ollama on the host; appends to evals/local/LEDGER.md
            # SAGE_MODEL=qwen2.5:7b to compare a model, SAGE_EVAL_CASE=recall to run one case
```

## Routines

A routine is a file in `SAGE_HOME/routines/`, its name the file's name:

```
---
schedule: weekdays 8:00
approved: yes
---
Give me a short brief for today: the weather, my first meeting, anything I asked you to remind me of.
```

`schedule` is `daily 8:00`, `weekdays 8:00`, `weekly monday 8:00` or `every 15 minutes`, in
local time by `SAGE_UTC_OFFSET_S` (a fixed offset, as the kernel's: change it at DST).
`conversation` defaults to `owner`. **A routine without `approved: yes` never fires**, and
only the owner should write that line. Nothing runs between fires; the folder is reread
each minute, so a new file needs no restart. A fire missed while the runner was down goes
out once if it is less than 10 minutes late, else it is skipped.

`sage-routines` is just another device (`make routines` starts it in its own sealed
container): add `routines=<token>` to `SAGE_DEVICES` and set `SAGE_ROUTINES_TOKEN` to the
same token. Its messages are labelled information unless `routines` is in `SAGE_OWNERS`;
anyone who can write a routine file then speaks as the owner, so leave it out unless that
is what you want. `routines` never says hello, so it never becomes where replies go: a
routine's reply reaches the owner device that spoke last, or the first owner device online.

**Every bot that gets something wrong becomes an eval.** Add the conversation to
`evals/local/cases.json` with a check on what was delivered, see it fail, fix the harness, prompt or
model, see it pass, ship. The checks are word predicates (`sage/tests/eval.rs`); no LLM judge yet.

## Next

1. Point [`reachy_sage_voice`](../../devices/reachy-mini/apps/reachy_sage_voice/) at port 8786
   (its client speaks the Python kernel's v1).
2. Live evals are in (`make eval`, `evals/local/LEDGER.md`). They show the speech guards' gap:
   after its one retry a small model usually writes the same reply again, and the kernel
   delivers it, because `fix` drops only whole repeated sentences, not a reused opening or
   closing, and a reply made only of repeats is still spoken (`sage-kernel`'s "never
   silence" rule). Owner call open: silence, an emote, or a second retry. A sentence
   repeated mid-reply, or a reused closing, streams before the guard can see it.
3. Memory as files: summarize while away, profile and dated log are in. Open: skills
   and routines; summarizing during a very long turn; ranking log lines by relevance to
   the conversation, not only importance and age; a summary that fails waits for the
   next quiet spell. Owner calls: "notes that fade" are log lines without `!`, not a
   separate file; a summary keeps the last three replies (the speech guards' window).
4. The browser build works (`wasm/`, tested) but weighs 448 KB, **128 KB gzipped**, against
   Quila's 74 KB budget for its whole Sage page. `wasm-opt -Oz` does not help (131 KB);
   the bulk is serde code generated for each JSON shape, spread over 2,500 small
   functions. The core's own logic is about 43 KB. Owner call: let Quila load the core
   lazily as its own asset, keep Quila's TypeScript Sage and hold it to `vectors/`
   (they are language-neutral for exactly this), or hand-write a small JSON codec
   for the browser build.

## Changes from `sage-kernel` (owner confirmed, 2026-10-20)

1. **A message during a turn waits instead of being refused as `busy`.** Several
   quick messages become one turn (the slides' queue rules).
2. **Any connected device's tools are offered to every turn**, preferring the body
   being talked to. This is how the agent's computer becomes a plugin. Tiers
   replace the earlier v1 rule "tools only from the turn's own device".
3. **Speech is a `send` tool call**, not a `reply` field in a JSON answer. The
   adapter can still use Ollama's `format` (24/24 on the bench) and translate the
   answer into calls.
