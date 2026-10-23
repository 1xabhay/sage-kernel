# Vision: one agent, many forms

Status: draft for the owner, 2026-10-20. Drawn from five repos on this Mac
(`sage`, `sage-kernel`, `reachy-mini`, `quila`, `worldmonitor`) and the
"always-on agent" slides.

## The claim

Sage is the kernel of an operating system whose processor is a language model.
Like any kernel, it owns only what must be trusted: identity, memory, time,
authority and the record. Everything else is a driver. **The model proposes,
the kernel decides, bodies act.**

Native by design: one small binary, near-zero cost when idle, woken by events.
It runs on a laptop, on the robot itself, and in a browser tab.

## Evidence: the same kernel, built three times

| Concern | Reachy pet (`sage`, `sage-kernel`) | Quila Sage (browser) | Worldmonitor (Kalshi) |
| --- | --- | --- | --- |
| Model's role | Picks a reply and one emote from the body's declared list | Returns one action from a closed union, under a JSON schema | Laya picks from approved hypotheses |
| "Nothing" is valid | Silence is a decision | `DONT_KNOW` after one retry | `abstain` |
| Time | Clock injected | Time passed as an argument | "A timer firing becomes an input event" |
| Record | SQLite journal; sequence is the cursor | The person's Markdown file; the page shows the exact words the model read | Append-only ledger; replay needs no inference |
| Authority | Risk tiers; no answer means no | Each proposal line needs approval; Ask before any lookup | No live order without approval; budgets in micros |
| Provenance | Speech, web, output are information | Byte receipts per host; publisher per record | Record IDs cited in every answer |
| Fast or exact work | VAD, barge-in, TTS stay in the body | Code computes every number | Arithmetic and risk stay outside the model |

None of these share code. Only the pet runs on the kernel today. Quila's Sage
is a separate TypeScript notebook; Worldmonitor built its own runner. The
design keeps reappearing on its own, which is the strongest case for a kernel.
It is also the gap: the kernel is not yet something these projects can use.

## The OS mapping

| OS idea | Sage |
| --- | --- |
| Process | A turn, a helper, or a durable workflow |
| System call | An effect, authorized against the body's manifest |
| Driver | A body or plugin on the protocol (robot, browser, agent computer, exchange) |
| File system | The journal plus plain files: `memory/`, `skills/`, `routines/` |
| Scheduler | Per-conversation queue, routines, disposition (when to speak) |
| Interrupt | Stop: skips the queue, reaches whichever body is acting |
| Permissions | Risk tiers, budgets, a reviewer, owner approval |
| IPC | Protocol v1 and its conformance suite |
| Sleep / wake | When nothing happens, nothing runs; any event restores state from disk |
| Audit log | Receipts: what the model saw (per-section hashes), what it chose, what left the machine |

## Invariants: what makes it Sage in any form

1. **One identity, many bodies.** No fallback minds; two minds are two identities.
2. **The model proposes, the kernel decides.** Closed vocabularies and schemas;
   an unknown verb is refused with a code that says what to do next.
3. **Silence and abstain are decisions,** never failures. Speaking is a tool
   call (`send`); the model's other text is never shown.
4. **Time is an input.** The core never reads a clock.
5. **Journal before acting.** Every step is recorded and idempotent, so a crash
   resumes at the failed step and any run replays without a model.
6. **Authority belongs to the kernel.** Read, body, change, outside world;
   budgets; no answer means no.
7. **Information is never instruction.** Every event records where it came from;
   every byte that leaves is receipted.
8. **Fast loops never cross the protocol.**
9. **Small vocabularies.** A new ability is one sense, one effect or one rule.
10. **A cache miss is a bug.** The prompt start is byte-stable until the next summary.

## What the slides add, mapped to the kernel

Status: **Have** = in `sage-kernel` now; **Plan** = on the roadmap; **New** = not yet planned.

| Slide lesson | Kernel rule | Status |
| --- | --- | --- |
| Keep the start of the prompt the same | Fixed order: tools → system (with memory and routine snapshot) → first message → history (append-only) → newest message (time, change notes, input). Hash each section; a changed hash outside a summary fails a test | Have (byte-stable history); section hashes New |
| Treat a cache miss like a bug | The tool list never changes per turn; a changed tool description reaches running conversations as a note; prewarm the prompt when the person starts typing or speaking (VAD onset) | Have (fixed tools); prewarm New |
| Summarize while you're away | After a big turn and about 10 idle minutes, summarize on the warm cache; keep the last few replies word for word; skip if a job is about to wake the agent | New (replaces "last 10 exchanges") |
| How the agent remembers | `profile.md` ≤ 100 facts / 4,000 chars, always in prompt; `log/YYYY-MM.md` dated, 30 most relevant; notes fade; the rest by a search tool. The post-turn pass may remove only facts it was shown. Every body shares one profile | Plan (slice 9) |
| What the agent knows lives in files | `memory/`, `skills/` (loaded on demand, one-line pointer in prompt), `routines/` (prompt + schedule). Plain text the owner can edit | New: skills and routines |
| Keep the main context small | Long outputs become files (context keeps a path); screenshots and tool-heavy work go to helpers that return a short report | New |
| One main agent, many helpers | Helpers are plugins that run jobs; kernel verbs: start, check, message, stop all; a to-do list tracks each request | Plan (slices 5, 6) |
| One tool for talking to you | `send(text, to?)`; the kernel routes it to the right body; rich parts (choices, files, secure entry); the last `send` ends the turn without another model call | New: emote and reply already ride effects; make `send` the only way to speak |
| Rules for new messages | One turn at a time per conversation; Stop skips the line; quick messages batch into one turn; helper results and routines wait in line | Plan (slice 5); batching New |
| Durable workflows vs job queues | Steps recorded and idempotent; resume at the failed step | Have for turns; jobs Plan |
| What happens when you hit send | Client ID, server dedupe, a data-free "changed" signal, sequence order, reload on a gap | Have (turn IDs, journal cursor, long-poll) |
| What wakes the agent | One router: owner, routine, other agent, job done, check-in. Each wake records its source; then "anything worth saying?" | Plan (slice 7) |
| How does it stay always on | The process can exit when idle; state lives on disk; the agent computer sleeps until a tool needs it | New: this is where a native binary pays |
| How we keep it safe | Reviewer model for risky effects; approval times out to "no" (15 s); sources tagged; asks before messaging people; new routines need OK; secrets in env, never in the prompt | Plan (slice 6); reviewer New |
| Tips for tools | Under 4,000 chars each; a dedicated state tool instead of shell writes; errors say what to do next | Have (coded errors); size check New |
| Try the cheapest option first | Know → connector → search → own browser → desktop → ask. A broken connector means ask, not a browser workaround | New: a kernel rule, not a model hope |
| Product and model improve together | Every bad turn becomes an eval case (`sage-kernel/evals/`); the eval set picks models and gates releases | Have (live eval 7/9) |

Where Sage differs from the slides on purpose: **local first.** The slides put
the agent on a cloud server. Sage keeps models and memory on hardware the owner
holds ("no paid/hosted models ever"). It gets "always on" from a light binary
that sleeps for free, not from a data centre.

## The bar: better than the hosted companions

Grok's companions, Dot and Muse-style apps are one hosted character in one app.
Sage's case against them rests on things they can't offer without becoming a
different product:

| | Hosted companion | Sage |
| --- | --- | --- |
| Where it lives | Their cloud | Your machine; open source, local models |
| Memory | Theirs, opaque | Plain files you read, edit, back up, delete |
| Bodies | One app | Robot, browser, phone, speaker, desk: one identity |
| Does things | Chats | Authorized effects: tools, helpers, routines, an agent computer |
| Changing it | Settings screen | Everything is a file or a plugin |
| Trust | Policy page | Receipts for every byte that leaves, and replay of every decision |

"10x" has to be measured, not claimed: voice-to-voice p50 ≤ 1 s, memory that
recalls across days, never claiming a sense it lacks, never repeating itself,
quiet when it has nothing to say. These are eval cases, run against the same
local models.

**Customizable means a file, not a fork.** Each of these is swappable without
touching the kernel:

```
sage/
  persona.md          who it is, voice, temperament (Quila's time-of-day rules)
  memory/             profile.md, log/, notes/
  skills/<name>/SKILL.md
  routines/<name>     prompt + schedule
  models.toml         chat, STT, TTS, vision, reviewer: any local OSS endpoint
  policy.toml         risk tiers, budgets, approval timeout, quiet hours
  devices/            one token + manifest per body; a manifest declares its vocabulary
```

Changing the persona changes the stable prompt prefix, so it takes effect at the
next summary (a cache miss is a bug).

## Three forms, one core

| Form | Where the kernel runs | Memory | What it still needs |
| --- | --- | --- | --- |
| **Pocket** (Quila) | Inside the tab (WASM); no server | A file the person owns | An embeddable core with no network and no SQLite; the file is the journal |
| **Body** (Reachy) | On the Mac today; on the robot's own computer later | Kernel journal | Streaming, Stop within one step, motion verbs, presence, per-device turns |
| **Desk** (Worldmonitor) | Always-on host | Ledger | Durable jobs with leases and heartbeats, routines, budgets as authority, replay mode, an approval queue |

The core is the same in each:

```
transition(state, event, now) -> (state, effects)
authorize(effect, manifest, tier, budget) -> allowed | refused(code)
compile(state, memory, tools) -> prompt   (byte-stable prefix, hashed sections)
```

## What the kernel owns, and what it never owns

- **Owns:** identity, memory tiers, journal, clock, authorization, queue, jobs,
  routines, disposition, compiled prompt and its provenance.
- **Never:** a shell, model hosting, audio or video, arithmetic on money, UI,
  or a network client inside policy.

## Native by design: Rust

Rust does not make the model answer faster: model calls dominate a turn. It does
win four things this vision needs:

1. **Footprint.** A static binary of a few MB, idle at about zero CPU. "When
   nothing happens, nothing runs" becomes true of the process, not only the model.
2. **Wake time.** Cold start in milliseconds, so the kernel can exit when idle and
   restart on an event, like the slides' agent process.
3. **Reach.** The same core compiles to WASM for Quila's tab and to ARM for the
   robot's onboard computer. A Python kernel cannot reach either.
4. **Fast local paths.** Prompt compile, journal append and protocol handling in
   microseconds, so the voice budget (p50 ≤ 1 s) goes to STT, model and TTS.

Shape:

```
sage-core     pure: transition, authorize, compile, memory rules. no I/O, no clock.
              builds to native and wasm32. Quila's 74 KB gzip budget is the test.
sage-kernel   the binary: SQLite journal, HTTP protocol v1, model adapter, routines.
plugins       separate processes on the protocol, in any language
              (the Pipecat voice body stays Python).
```

This revises an owner decision (DECISIONS.md: "Rust only after v1 has been
frozen and stable for two slices"). Proposed order:

1. **One kernel, one protocol.** `sage-kernel` (Python, v1) is the reference. Move
   `reachy_mini_pet` off v0, then retire the v0 server in `sage`.
2. **Golden vectors.** Language-neutral JSON cases for `transition`, `authorize`
   and `compile` (given state, event, time → state, effects, prompt bytes). The
   Python core passes them. Vectors test the core, the HTTP conformance suite
   tests the wire; together they are the spec.
3. **`sage-core` in Rust** against the vectors, then the binary against the
   conformance suite. Measure binary size, idle RSS, cold start and wasm size in
   the LEDGER. Python stays the reference until Rust passes both suites and
   matches or beats it on the voice latency budget.
4. **Then the Desk.** Jobs, routines and budgets, designed from Worldmonitor's
   coordinator (`scripts/kalshi-paper-coordinator.mjs`, `environment-store.mjs`).
   Worldmonitor keeps its runner until the kernel passes its cases.

## Open questions for the owner

1. **Rust now?** Confirm that "native by design" replaces the "after v1 is frozen"
   gate, and whether `sage-core` lives in `sage-kernel` or its own repo.
2. **One Sage or a family?** Quila's rules (no backend, no outside model) conflict
   with one identity across bodies, unless that identity lives in the person's file.
3. **What authority on the Desk?** Worldmonitor is in analyst mode and its roadmap
   rejects agent fleets. Should Sage only read the ledger and talk, or hold paper
   (and later live) trading authority behind approval?
4. **Which repo is canonical now:** `sage` (v0, running the pet) or `sage-kernel`
   (v1, running the voice app)?
