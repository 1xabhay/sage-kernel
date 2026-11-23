# Changelog

All notable changes to the Sage kernel and its device protocol are recorded
here. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/);
the package follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: anything may change). The device protocol is versioned on its own,
as an integer (see [docs/PROTOCOL.md](docs/PROTOCOL.md#status-and-versioning)),
and every protocol change is listed under **Protocol**.

## [Unreleased]

### Changed

- Memory is written after the conversation, not by the model mid-turn (owner, 2026-11-21: only what
  is said to Sage; a guest for one conversation; overheard speech never). `remember` and `forget`
  are gone from the answer's schema, the prompt and the examples. A conversation ends 5 minutes
  after the last line said to Sage (`SAGE_SESSION_IDLE_S`) or at 40 such lines; its context is then
  emptied, where it used to run on for hundreds of lines. Between conversations a local model
  (`SAGE_WRITER_MODEL`, default the talking one; `off` for none) reads only the lines said to Sage,
  not by a guest and clearly heard, and proposes at most 5 changes and a line about the day; the
  kernel refuses any that cites no such line. The call is dropped the moment a turn starts.
- `facts` in the checkpoint are records `{id, who, kind, text, at, from}`. A checkpoint of an earlier
  kernel loads with every fact kept (`who` unknown, `kind` person). At most 200, was 20.
- The system prompt ends with at most 600 characters of memory (who they are, the last day, what is
  open), records from an unknown voice apart and never greeted by name from; past 20 records, 2 more
  that match ride on the newest line. The greeting may ask after one open thing, never more.
- "Forget about …" and "what do you remember?", said to Sage, are answered by the kernel with no
  model call. A forget also keeps everything said so far in that conversation from the writer.
- Character keys added: `prompt.memory_none`, `memory_unplaced`, `recalled`, `recall_kinds`, `writer`,
  `writer_known`, `writer_said`, `writer_answered`, `lines.forgot_none`, `remembers`, `remembers_none`,
  `templates.forgot`, `remembers_more`, `patterns.forgets`, `asks_memory`, `words.forget_that`;
  `prompt.memory` and `proactive.greet` reworded. A custom character file needs the new keys.
- Sage is a companion on every speaking body (owner, 2026-11-21). The "mostly a move" persona, its
  examples and the kernel's guess at who a short line was said to are gone; the character file's
  persona asks for one or two short spoken sentences, the answer first, with a feeling alongside.
  An example's tool call is shown only to a body that can perform it. Character keys removed:
  `name_misheard`, `prompt.moving_persona`, `moving_stay`, `move_lines`, `spoken_examples`,
  `no_repeat`, `templates.repeat`, `words.addressed`, `requests`, `hanging`. Added: `prompt.text_stay`,
  `unclear_again`, `unknown_again`, `asked_last`, `lines.move_on`, `patterns.admits`, `asks_again`.
- Not knowing and not hearing are each handled once, in code: a line only partly heard is marked
  "(unclear)" on the newest message alone; a second one running adds a note not to ask again, and a
  third gets a move with no model call. After a reply that said it did not know, a note says not to
  say so again, a sentence that does is dropped, and the last resort is a line that moves on, never
  a second "don't know". After its own question, a note asks for none. The last resort for a
  question is "I'm not sure about that one.", and a garbled question never gets it.
- Context keeps only the turns Sage spoke in: a turn answered with no words, or interrupted before
  one, is not kept, and an exchange the kernel's last resort answered is not shown to the model.
- The repeat guard matches only the same words (of 373 rejections in 96 h of the live journal, 151
  were exact and 222 were the same four opening words), and asks the model nothing again: where
  silence would do, the repeat becomes its move alone; a question asked again gets its answer again.
- Nudges: at most one in 20 minutes (`SAGE_NUDGE_GAP_S` 1200, was 900), and each one left unanswered
  doubles the wait, replacing "never twice unanswered".
- `evals/companion.py` scores the conversation design on a journal's lines: as recorded, the recorded answers through this kernel
  (no model), and, with `--model`, answered again.

### Added

- `python -m sage memory list | forget ID | edit ID "text" | add "text"`: what Sage remembers, each
  record with the line it came from, for its owner to see and fix while the kernel runs.
- **Protocol** (v1.6, additive): a `submitted` request may carry `Sage-Speaker: owner` or `guest`
  beside `Sage-Addressed: 1`; both marks are journaled on the line's row. A line with no
  `Sage-Addressed` mark that was not typed is never part of a conversation or of memory.
  `sage_device` sends the speaker when told one, and the Reachy voice sends the voiceprint's verdict.
- `evals/memory.py`: `sessions <journal>` counts conversations under the rule and the writer's share
  of lines (no model); `writer --model <name>` scores the writer on scripted conversations.
- A hosted model for addressed turns, off by default. `SAGE_HOSTED=anthropic` with `ANTHROPIC_API_KEY`
  sends the prompt of a line its device marked as addressed to `SAGE_HOSTED_MODEL` (default
  `claude-haiku-5-5`); every other line stays on the local model. No first piece within
  `SAGE_HOSTED_DEADLINE_S` (default 2.5), or any failure before words were heard, and the local model
  answers the turn instead; the decided row's `model_timing` names the hosted `model`, or the `fallback`.
  Text only, and the key is never logged. SECURITY.md says exactly what leaves. Unset: no change.
- **Protocol** (v1.5, additive): a `submitted` request may carry the header `Sage-Addressed: 1`. A kernel
  that does not know it ignores it; no event field changes.
- The pet's character is a file: its name and the near-misses of it, the persona, the examples, the notes
  to the model and the fixed spoken lines moved from `sage/policy.py` to `sage/character.json`.
  `SAGE_CHARACTER` names another file, read once at start; one missing a key, or with a key of the wrong
  kind, stops the start and names the file and the key. No prompt change with the default file.
- The language rules are in that file too: the patterns for distress, a question, the time and date, a
  request for quiet or a repeat, a web search, a claim of a sense or of being alive, and the word lists
  (common replies, requests, backchannels, stopwords, month and day names). Each pattern is a list of
  alternatives; one that is no regular expression, or that is empty, stops the start. The default file's
  rules are the ones the code held.
- Sage never claims to be sentient: a reply that says, in the first person, that it is alive,
  conscious or has feelings is held back from streaming, retried once with a note, and then that
  sentence is replaced by "I'm not alive, and I don't have feelings like you do." (behaviour row B34).
  No prompt change.
- **Protocol** (v1.4, additive): a browser body. `SAGE_ORIGINS` lists the web origins whose
  pages may call the kernel (CORS); unset, no page is answered, and any other origin gets 403.
  `"search": false` in a manifest keeps that body's lines from the web, and the `search` feature
  says the kernel heeds it.
- The text body (`python -m sage_device.text`) and the device conformance suite
  (`make conformance-device`): a body run against this kernel, with a tool check.

- **Protocol** (v1.1, additive): `moves` in a manifest and the `moves` feature. A
  turn may end with a move and no words on any body; a direct question or a line of
  distress is never silent (retried once, then a short line of the kernel's own).

- `SAGE_DEVICES_FILE`: device tokens in a file, re-read on change without a
  restart; a removed token is refused, an added one accepted, a bad file keeps
  the old tokens and is logged. `SAGE_DEVICES` alone works as before.
- Notes: a device can hand the kernel a topic and up to 64 passages (such as a
  song looked up on the web); on each turn they match, the first and the 3 that
  best match the newest line ride on the newest message, as information. Journaled and checkpointed.
- Prompt cache warm-up: when a device says hello with a manifest the kernel has
  not seen from it (the first hello after a restart, or a changed body), the
  kernel sends Ollama one background call with that device's next prompt and
  `num_predict: 1`, so its next turn skips the ~6 s cold prefill. Never while a
  turn runs, abandoned when one starts, journals nothing (stderr says `warmed`).
- Model timing: the `decided` row keeps Ollama's own counts and durations from
  its final chunk as `model_timing` {prompt_tokens, prefill_ms, gen_tokens,
  decode_ms, load_ms}; `bench/latency.py` reports prefill and decode p50/p95.
- `sage.policy.relevant(passages, query, k)`: the BM25 retriever that picks
  which notes ride on a turn, importable on its own so a device's eval can score
  the kernel's own retrieval.
- `bench/audio_llm.py`: Gemma 4 E4B/E2B audio-in (llama.cpp) measured against
  Parakeet STT on 58 synthetic clips and 20 live ones; verdict no (worse WER
  outside clean speech, and its joint "heard + reply" JSON invents what it
  heard). Results in `bench/LEDGER.md`.

### Fixed

- **Protocol:** the `stop` effect delivered for a Stop naming a turn carries that turn's `turn_id` (a
  bare Stop's stays null). The Reachy voice body ignores a stop naming a turn it has since replaced:
  the kernel's answer to the body's own stop of "Okay, Sage." no longer ends the question that followed.
- The pet's name heard a little wrong ("age", "saige", "sayge", "sarge" for "Sage") as a short
  line's first or last word counts as said to Sage and reaches the model; it got a head tilt and
  no answer before. No prompt change.
- A reply is never longer than the body's `max_chars`: one the model server did not cap (it ended
  in a full stop past the cap), a claim the kernel put right, and the last-resort line for a
  question or distress are cut back to a whole sentence, or else a whole word. The last resort was
  cut mid-word before; what streams stops at the cap too.
- `make backup` names the `colima` Docker context, so a default context that has fallen back to
  another daemon is never the one asked for the journal.
- The push guard also scans commit messages for a private address, a MAC address or a home folder.
- The voice's pet scorecard counts what Sage says again (its pattern named a Pipecat function that
  1.15 renamed).
- `bench/quiet.py` fails when Sage puts words to more than 10% of the lines not said to it (a first
  value, to tune on a labelled evening); it counted them and passed at 12 of 44. A move or nothing
  to such a line is never a miss.

### Protocol

- **Changed** (v1.5): the manifest's `moves` field is ignored and `moves` is no longer listed in
  `features`; a line is framed `heard (unclear): "…"`, on the newest message only, and without a
  `confidence` only an unfinished or repeating line is; an exact repeat where no words are needed
  is delivered as its tool effects alone, with no reply; a turn with no reply is not kept in the
  model's context; `SAGE_NUDGE_GAP_S` defaults to 1200 and doubles per unanswered nudge.

- v1.2, per-device turn lanes (§1, §3, §5, §7): each device has its own turn.
  Another body's turn no longer makes a device `busy`; a turn cut off by a
  restart is `unknown_pending` only for its own device. A bare `stop` still
  stops every body; a `stop` naming a turn reaches only the sender's own turn
  and effects (a device naming another device's turn now gets `409 stale`).
  A body's `acted` reports wait for its own next exchange (at most 4 per
  body); before, any device's next exchange took them all. A greeting or
  nudge waits only on its own device's turn and effects; before, any body's
  running or `unknown` turn held every greeting.
  Still protocol `1`; a lone device sees no change. The checkpoint gains
  `lanes` (the other devices' turns); earlier checkpoints read as one lane, and
  v1.1 kernels still read the new ones.
- New error `409 hello_first` (§2, §7): `POST /v1/events` and `GET /v1/effects`
  refuse a device that has not said hello since the kernel started, so no turn
  runs without its manifest. The device says hello again and retries, as for
  `reset`. A device that says hello on every connect only sees it after a
  restart it did not notice; before, its turns ran with no tools and no body.
- v1.1, additive (§1, §3): new event `acted` `{act_id, origin, what, input?,
  status, reason?, said?}`, a body's own plan or aside, journaled and told to the
  model inside its next exchange; `submitted.input` `{medium, confidence?,
  alternatives?}`, so a typed or confidently heard line is never "(may be
  misheard)" (the 4-word guess stays for devices that send none); `features` in
  the `hello` answer; `GET /v1/health` `{ok, protocol, features}`, with a token.
- Manifests may carry `body`, the body's own description in at most 300
  characters (§2). The model sees it beside the senses and may name parts it
  cannot use yet ("I have a camera, but I can't see through it yet"); it declares
  no sense. Manifests without it work as before.
- A `done` result's `text` for a tool effect now reaches the model on the next
  turn, after that exchange, as the body's report (§5); before, only failures did.
- New event `notes` `{topic, passages}` (§10, draft). JSON bodies may be up to
  131072 bytes (was 8192). Notes ride only on turns they match (a passage after
  the first matches the line, or the line names the topic); an unrelated line
  gets none.

## [0.2.0] - 2026-10-21

### Added

- Remembered facts, as Pollen's Reachy Mini conversation app does it: the model
  may add `remember` or `forget` to its answer; facts are journaled, kept in the
  checkpoint (up to 20, 120 characters each) and rendered at the end of the fixed
  prompt prefix, so a name outlives the history window and a restart.

### Removed

- Guards that an eval ablation on qwen3:4b-instruct showed do nothing measurable:
  the act-don't-announce retry, the assistant-speak filter and its retry, the
  hotter (0.9) retry temperature, the `SAGE_REPEAT_FALLBACK` experiment, hiding
  repeated replies from the model, and dropping or holding recently said
  sentences. The repeat retry, the sense guard, misheard framing, the reminder
  line and "never unsay" stay.

### Changed

- The prompt cache now holds across turns once the window is full: the history
  is cut from 8 back to 5 exchanges in one block instead of sliding by one
  every turn, which made Ollama re-read the whole history on every turn.

## [0.1.0] - 2026-10-21

The first public release, with device protocol v1 in Draft.

### Protocol

- Device protocol v1 (Draft): MCP-shaped tool manifests, a result per effect,
  Stop delivered to every acting device, streamed reply pieces
  (`reply_delta`), timing and `duration_ms` reported by the device, a
  per-device event rate limit, coded errors, presence and kernel-initiated
  turns (§9, draft). v0 is superseded and no longer served.
- A status and versioning section: the Draft, Stable, Deprecated and Final
  states, and how a conformance-passing kernel can claim v1.

### Added

- The kernel (`sage/`), standard library only: journal, policy, disposition,
  the HTTP device listener and the Ollama `/api/chat` adapter.
- The black-box conformance suite (`conformance/`), unit tests (`tests/`) and
  an opt-in eval against a real model (`evals/`, written fixtures; live ones
  stay local in `evals/local/`).
- Docker and Make for every task; CI running lint, the dependency check,
  a secret scan, the unit tests and the conformance suite.
- Apache-2.0 license, README, CONTRIBUTING, SECURITY, Code of Conduct
  (Contributor Covenant 3.0), issue and pull request templates.

[Unreleased]: https://github.com/1xabhay/sage-kernel/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/1xabhay/sage-kernel/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/1xabhay/sage-kernel/releases/tag/v0.1.0
