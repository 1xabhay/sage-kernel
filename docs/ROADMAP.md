# Roadmap

What is planned, not promised. Decisions live in [DECISIONS.md](DECISIONS.md);
what has shipped is in [CHANGELOG.md](../CHANGELOG.md).

## What Sage is for

Sage is a new class of pet: one memory and one character that lives on your own
machine and acts through replaceable bodies. It engages on six planes at once:

- **Senses:** it sees and hears, and can tell when a line is meant for it.
- **Situation:** it knows the time, the date and where it lives.
- **The wider world:** it can look up fresh facts.
- **Memory:** it remembers dated facts and notes.
- **Body:** it moves, glances and gestures.
- **Social timing:** it knows when to speak, when to move and when to stay quiet.

Sage acts of its own volition. It is always on and has no wake word; staying
quiet is its own choice, never a rule that the person must address it first.

What it never does: claim to be sentient, identify other people, guilt or nag,
or send what it hears off the machine without a reason the person would expect.

## The version ladder

Each version closes on a measured gate, not on a date. Every deploy records one
row of the scorecard below, so a version can be compared with the one before it.
The behaviour cases are in [evals/behaviour.json](../evals/behaviour.json).

### 0.3: back on its feet

- One command brings the whole of Sage up and one takes it down, and both
  survive a reboot.
- A restore command for the journal, with a drill that proves a backup restores.
- Short whole statements and short commands reach the model again, so a name
  taught in four words is remembered.
- The quiet move is one the body declared, not one body's clip.
- The pet scorecard counts speech again.
- The vision and the refuse list above are tested, not only written.

**Gate:** up and down work after a reboot; the restore drill passes; every suite
is green; the first scorecard row is on record.

### 0.4: knows when a line is meant for it

- A 24 hour baseline of the robot link, then the robot software upgrade, judged
  by the same numbers after.
  `make -C devices/reachy-mini/apps/reachy_sage_voice pet LOG=<after> BASELINE=<before>`
  prints both logs' link drops, drops per hour and seconds down, and whether
  drops per hour fell.
- Voice direction is read in the right frame (today it ignores head turn).
- Speech confidence reaches the kernel.
- Direction, face bearing and confidence are logged per line. They inform Sage's
  own judgment of when to engage; they are not a gate on answering.

**Gate:** on a labelled evening, replies to lines not meant for Sage fall and no
line meant for it is lost; link drops per hour fall after the upgrade.

### 0.5: sees, and says what it keeps

- Sight lands switched off, then on when asked; the body's self-description
  says what it can and cannot see.
- A privacy cue, a mute, and "stop recording".
- A stated retention for audio and logs, and it runs.
- Ambient speech does not trigger a web search.

**Gate:** sight answers on ask without unloading the reply model; in a replay,
nothing unaddressed leaves the machine.

### 0.6: quick

- First reaction is the main gate; the worded reply is tracked second.
- The time between turn start and first words is accounted for, stage by stage.
- When the model is slow or down, a move at the deadline and one line if it
  persists.
- The kernel enforces a body's reply length.

**Gate:** first reaction and worded reply are both on record and inside their
gates (worded reply: p50 1.5 s, p95 3 s is the target carried over).

### 0.7: remembers

- Dated facts, recall across days, and a real "forget".
- The guest rule: Sage talks with a guest, stores nothing about them and names
  no one.
- A three-state mood shown in the body only: no mood from absence, no guilt.

**Gate:** the memory cases in the behaviour suite pass on the live model.

### 1.0: a second body

- The old motion path is deleted.
- The simulator on the shared device client. (The minimal text body is in:
  `python -m sage_device.text`.)
- The device-side conformance suite (`make conformance-device`) grows to cover
  tools, Stop and stale replies, so a body can be checked against the protocol
  the way a kernel is today, as the Model Context Protocol tests both clients
  and servers ([MCP conformance](https://modelcontextprotocol.io/community/sdk-tiers)).
- A browser body: the kernel answers the web origins the owner lists (CORS), off by
  default.
- Protocol v1 goes from Draft to Stable
  ([PROTOCOL.md](PROTOCOL.md#status-and-versioning)).

**Gate:** two bodies pass the same suite against one kernel.

## The scorecard

One row per deploy.

| Measure | Target |
|---|---|
| First reaction after the person stops speaking, median | recorded; gate set in 0.6 |
| Worded reply, p50 and p95 | 1.5 s and 3 s |
| Replies in words to lines not meant for Sage | falls each version |
| Lines meant for Sage that got no answer | none |
| Turns answered with a move alone | recorded |
| Robot link drops per hour | falls after 0.4 |
| Behaviour cases passing, of those built | all |
| Journal backup age, and the last restore drill | under a day; passed |
| The owner's feel, one line | written |

## Journal export as a ConvoKit corpus

Not built yet. The journal could be exported in the corpus format of
[ConvoKit](https://convokit.cornell.edu/) ([data format](https://convokit.cornell.edu/documentation/data_format.html)):
`utterances.jsonl` (`id`, `speaker`, `conversation_id`, `reply_to`,
`timestamp`, `text`, `meta`), `speakers.json`, `conversations.json`,
`corpus.json` and `index.json`. An earlier review found that it maps cleanly
except for three gaps. The journal's columns (`sequence`, `kind`, `source`,
`turn_id`, `body`, `at`) give `id`, `speaker`, `text` and `timestamp`, a
reply's `turn_id` gives `reply_to`, and the rest of `body` can go in `meta`.
The gaps the export would have to fill:

- **Conversation id.** The journal has turns, not conversations; an export
  needs a rule for where one conversation ends (for example the disposition's
  absence and quiet timers).
- **Per-person speakers.** Every submitted line comes from a device, not a
  person (v1 assumes one person per device); the speaker would be the device
  until the protocol carries who spoke.
- **Interrupted replies.** The journal keeps only what was played; ConvoKit
  has no notion of a cut-off utterance, so that needs a `meta` field.
