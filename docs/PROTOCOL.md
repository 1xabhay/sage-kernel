# Device protocol v1 (Draft)

v1 is [v0](PROTOCOL-v0.md), which runs in production today, plus only what
Milestone 1 needs (BLUEPRINT §2): tools shaped like MCP, a result per effect,
Stop delivered to the device that is acting, streamed reply pieces, timing
reported by the device, a per-device event rate limit, and coded errors.
Each rule marked **Decided** records who decided it (owner or lead) on
2026-10-20; lead decisions stand until the owner says otherwise.
`conformance/` checks every rule on this page, over HTTP only. It runs in
Docker on Python 3.12 (owner).

## Status and versioning

**Status: Draft.** v1 is not frozen yet: any rule on this page may still
change, and every change is recorded in [CHANGELOG.md](../CHANGELOG.md).
§9 is a draft within the draft. v0 ([PROTOCOL-v0.md](PROTOCOL-v0.md)) is
superseded; a v1 kernel does not serve it.

**Versions.** A protocol version is an integer. It is in every route
(`/v1/...`), and a device and the kernel agree on one in `hello`: the device
lists the versions it speaks in `protocols`, the kernel answers with the one it
picked, or `426 protocol_mismatch` with the versions it `supported` (§7).

**Lifecycle**, after the revision states of the
[Model Context Protocol](https://modelcontextprotocol.io/specification/versioning):

- **Draft:** in progress; breaking changes allowed, each one in the changelog.
- **Stable:** frozen. It changes only by clarifications and fixes that keep
  every conforming kernel and device conforming; anything else is a new
  version. Freezing v1 is an owner decision ([DECISIONS.md](DECISIONS.md)).
- **Deprecated:** still served and still in the suite, with a migration note
  and a removal date in the changelog.
- **Final:** no longer served by the reference kernel; the page stays as it was.

**Compatibility.** The conformance suite in `conformance/` is the definition
of the protocol in code. A kernel conforms to v1 when the unchanged suite
passes against it:

```sh
SAGE_KERNEL_CMD="/path/to/your-kernel" make conformance
```

It may then say "conforms to Sage device protocol v1 (Draft), conformance
suite at commit `<sha>`" (or at a release tag, once there are releases),
naming the suite's commit because a Draft can still change. A partial pass is
not conformance. There is no device-side suite yet
([ROADMAP.md](ROADMAP.md)); a device relies on the rules in §8.

## 1. Transport and identity (unchanged from v0)

The device listener binds to `SAGE_DEVICE_BIND:SAGE_DEVICE_PORT` (default
`127.0.0.1:8766`) and starts only when `SAGE_DEVICES="reachy=<token>,mock=<token>"`
is set. Issuing a token is the owner's consent: that device's manifest is
trusted as declared. The kernel never names a device type.

Every request carries `Authorization: Bearer <token>`. The kernel derives the
device from the token; a body that names a `source` is refused. JSON bodies are
at most 131072 bytes (notes, §10, carry a document). Unknown query parameters are ignored. The version is in the
path: v1 routes start with `/v1/`.

| Route | Body / query | Success |
| --- | --- | --- |
| `POST /v1/hello` | `{"protocols": [1], "manifest": {...}}` | `{"protocol": 1, "sequence": N}` |
| `POST /v1/events` | `{"kind", ...}` (§3) | `{"sequence": N, "turn": {...}?}` |
| `GET /v1/effects?since=N&wait=S&offset=M` | — | `{"sequence": N, "effects": [...], "turn": {...}?}` |

`sequence` is the journal's own sequence number. It only increases, across
restarts too. `turn` is `{"turn_id", "run_status", "timing"?}` and appears only
when the current turn was started by this device.

**Decided (lead):** a v1 kernel does not serve `/v0`.

## 2. Hello and the manifest: tools shaped like MCP

```json
{"senses": ["speech"],
 "tools": [{"name": "emote",
            "description": "Show a feeling with the body, just before speaking.",
            "inputSchema": {"type": "object",
                            "properties": {"name": {"enum": ["cheerful1", "sad1"]}},
                            "required": ["name"]},
            "max_s": 3, "interruptible": true}]}
```

A tool is what MCP's `tools/list` returns (`name`, `description`,
`inputSchema`, a JSON Schema object) plus two optional kernel fields: `max_s`,
the longest the body may take to perform it (no `max_s`, no timeout), and
`interruptible`, whether Stop can cut it short. Names match `[a-z][a-z0-9_]{0,63}`; `reply`, `reply_delta` and
`stop` are reserved. At most 64 tools. A malformed manifest is refused with
`invalid`. Replies are not a tool: every device receives the replies to its
own turns.

**Body.** A manifest may also carry `body`, the body's own description in at
most 300 characters: its parts, including any it cannot use yet, such as a
camera not connected (`"body": "A Reachy Mini: a head that turns, two
antennas, a speaker, microphones, and a camera that is not connected yet."`).
Anything but text of at most 300 characters is `invalid`; without `body` a
manifest works as before. The kernel shows it to the model beside the senses,
the same on every turn of that device, as information about the body. It
declares no sense: a sense the manifest does not list is still one the body
lacks, and the model may say it has the part but cannot use it yet.

Each device's turns offer exactly that device's manifest tools, identical on
every turn of that device, so the start of the prompt never changes and stays
cacheable. A tool call the turn's device did not declare is never delivered;
it reaches the model as a tool error on its next turn (§5). `max_s` and
`interruptible` are for the kernel and the body; the model never sees them.

**How the kernel asks the model** (not wire protocol; the suite checks it
through its fake model). Tools stay the protocol's concept, but the kernel
does not use native tool calls. It calls Ollama's native `/api/chat`
(streamed NDJSON, `think: false`) with `format`, a JSON Schema derived from the
turn's device's manifest: an object whose first property is `reply` (a
string), followed by one optional property per tool, named by the tool and
shaped by its `inputSchema`, then the kernel's own optional `remember` and
`forget` (a short fact about the person; a device tool of that name is the
device's). Nothing else. Reply first, so speech streams
before any action is chosen. The kernel validates the output against the
schema and the manifest before acting.

A device sends `hello` again after every reconnect: manifests are not kept
across kernel restarts.

**Decided (owner):** tools are scoped to the turn's own device. Calling
another device's tools is out of scope for v1; it is a later feature that
needs explicit approval.
**Decided (owner):** actions come from Ollama-native `format`, reply first,
not native tool calls (bench: 24/24 valid against 0–7/24).
**Decided (lead):** failed or unavailable tool calls reach the model as
tool errors on the next turn. `max_s` is optional.
**Decided (owner, 2026-10-22):** an optional `body` of at most 300 characters,
shown beside the senses, so a body can name parts it cannot use yet.

## 3. Events

`kind` is one of `submitted`, `stop`, `acknowledged`, `result`,
`connected`, `disconnected`, `presence` (§9), `notes` (§10). **Decided (lead):** `result` replaces v0's
`playback_started` / `playback_finished`, which v1 refuses as `invalid`.
Server-only kinds (`completed`, `decided`, `restarted`, …) are refused, and
so is any field a kind does not list below. `turn_id` ≤ 128 chars, `text` ≤
4000 chars.

- `submitted` `{turn_id, text, timing?}` starts a turn. Resubmitting a
  `turn_id` this device already submitted admits nothing and returns the
  current state.
- `stop` `{turn_id?}` cancels the running turn, if any, and delivers a `stop`
  effect to every device that is acting (§5). Any authenticated device may
  send it, at any time; it is journaled with its sender. Without `turn_id` it
  stops whatever is acting. A `turn_id` that is neither running nor being
  acted on is a no-op, refused as `409 stale`.
- `acknowledged` `{turn_id}` clears a turn interrupted by a kernel restart. Only
  the device that started it may do so.
- `result` `{sequence | turn_id, status, reason?, text?, timing?, duration_ms?}`
  reports one effect (§5). `duration_ms` is how long the device spent
  performing it (a move, speech, any tool): an integer from 0 to 600000.
- `connected`, `disconnected` `{}` note presence.

**Rate limit.** Each device may send at most 10 events per second with bursts
of 20. Past that, events are refused with `429 rate_limited`, nothing is
recorded, and `retry_after` says how many seconds to wait. One device's flood
never refuses another device. `stop` is never rate-limited, so a body can
always be stopped.

**Decided:** the numbers, the exemption for `stop`, and refusing unknown fields (lead); Stop from any
device at any time, never limited, journaled with its sender (owner).

## 4. Effects and streaming

`effects` lists what was delivered to this device after `since`, in journal
order. Every effect has its own `sequence`:

```json
{"sequence": 41, "kind": "emote", "turn_id": "reachy-1f…", "input": {"name": "cheerful1"}, "stale": false}
{"sequence": 42, "kind": "reply", "turn_id": "reachy-1f…", "text": "Hello.", "stale": false}
{"sequence": 43, "kind": "stop",  "turn_id": null, "stale": false}
```

A tool effect's `kind` is the tool's name and `input` is what the model
passed, checked against `inputSchema`. Effects are delivered as soon as they
are decided, even while the reply is still streaming; a body plays an emote at
its next audio boundary. `stale` is decided by the kernel's clock:
true when the effect was recorded more than `SAGE_FRESH_SECONDS` (default 10)
ago. A speaker should not speak a stale reply. Device clocks are never used.

`wait` long-polls for up to `min(S, 25)` seconds until the journal moves past
`since` (any movement, even one that delivers nothing to this device), or the
running turn's reply grows past `offset`.

**Streaming.** While a turn this device started is running, the response also
holds at most one `reply_delta`: the reply text the model has written so far
from character `offset` on (default 0).

```json
{"kind": "reply_delta", "turn_id": "reachy-1f…", "offset": 12, "text": "and then"}
```

A `reply_delta` has no `sequence`, is never journaled and never survives a
restart; the next poll passes `offset` + the length received. A body plays
deltas immediately. The reply is one effect delivered progressively: the device
is acting from its first delta (§5). When the model finishes, the final
`reply` is journaled and delivered as in v0.

If the device is interrupted mid-stream (barge-in), it sends `result`
`interrupted` naming the `turn_id` (the reply has no `sequence` yet) with the
text it played. That result ends the reply and its turn on its own: the
journal keeps exactly that text, the model's next turn sees only that, and no
final `reply` is delivered. The device sends a separate `stop` only to cancel
other effects it is acting on, such as a move. **Decided (lead).**

**Decided (owner):** deltas play immediately. The journal keeps only the
final reply, cut to what the device reports as actually played when it was
interrupted, and the model's next turn sees only that.
**Decided (lead):** acting from the first delta; effects delivered as soon as
decided.

## 5. Results and Stop

Every delivered tool or reply effect gets one `result` from the device it was
delivered to:

```json
{"kind": "result", "sequence": 42, "status": "interrupted", "text": "Hello, I was"}
```

`status` is `done`, `failed` or `interrupted`. A `failed` or `interrupted`
result may carry `reason`, a string of at most 64 characters (e.g. `stale`,
`timeout`); a longer or non-string one is `invalid` (lead). A reply that is not spoken because it is stale gets
`failed` with reason `stale`. For an interrupted reply,
`text` is what was actually spoken, and memory records only that: the next
turn's context holds what the person heard, not what the model wrote. The
first result for an effect counts; repeating it changes nothing and succeeds,
so retrying after a lost response is safe. A result for an effect delivered to
another device is `forbidden`; for a sequence that is not a delivered effect,
`invalid`.

**Timeout.** An effect from a tool with `max_s` that has no result
`max_s` + 2 s after it was recorded, or a reply with no result 30 s after its
final piece, is recorded by the kernel as `failed` with reason `timeout`. A
later result from the device changes nothing, and no device stays acting
forever.

**Tool errors.** A tool effect that failed (including `timeout`) and a tool
call that was unavailable reach the model on the next turn as a message that
names the tool and the reason (`unavailable`, `timeout`, or the device's
`reason`).

**Done reports.** A `done` result for a tool effect that carries `text`
reaches the model on the next turn as a message after that exchange, naming
the tool and the text (`(your reachy body: body_plan done: tracking you)`):
what the body is doing now. Only the first 120 characters are told, with
whitespace collapsed. A `done` without `text` is not told, and the messages
before that exchange are unchanged.

**Acting.** A device is acting while it holds a delivered tool or reply
effect with no result, a streaming reply included. Stop, from any device, delivers one `stop` effect to
each acting device, at the next step of its long poll. A device that has
reported every result receives no stop. A `stop` effect needs no result. Who
is acting is derived from the journal alone, so it survives a restart.

**Decided (lead):** "acting" means delivered with no result yet; a stop needs
no result; the `max_s` + 2 s and reply 30 s timeouts; a stale unspoken reply
is `failed: stale`; a stop for another turn is `stale`.
**Decided (owner, 2026-10-22):** a `done` result's `text` reaches the model on
the next turn, so it knows what its body is doing.

## 6. Timing reported by the device

`submitted` and `result` may carry `timing`, an object of non-negative
integer milliseconds measured by the device:

| Field | From | To |
| --- | --- | --- |
| `stt_ms` | end of speech | final transcript |
| `ttft_ms` | `submitted` sent | first reply text received |
| `first_audio_ms` | end of speech | first audio played |

The kernel keeps them with the turn, journaled, and returns them merged in
`turn.timing`. An unknown field or a bad value is refused with `invalid`.

A `result`'s `duration_ms` (§3) is journaled into the same record, keyed by
the effect's sequence: `turn.timing.durations_ms` is `{"<sequence>": ms}`. A
result that names a `turn_id` (a streaming reply interrupted before it had a
sequence) is keyed `"reply"`; a turn has at most one streamed reply. A
value that is not an integer from 0 to 600000 is `invalid`.

**Decided (lead):** the latency ledger reads `turn.timing` from the journal;
`duration_ms` on every result, journaled as `durations_ms` keyed by effect sequence (simulator device, 2026-10-20).

## 7. Errors

Every error is `{"code", "message"}`; the message says what to do next.

| Status | `code` | Meaning |
| --- | --- | --- |
| 400 | `invalid` | Malformed body or manifest, unknown kind or field, a `source` field, bad `timing`, or a `result` for no delivered effect |
| 401 | `unauthorized` | Missing or unknown token |
| 404 | `not_found` | No such route |
| 409 | `busy` | Another turn is running. Retry the same `turn_id` |
| 409 | `unknown_pending` | A reply was interrupted by a restart. The device that started it must `acknowledged` it |
| 409 | `forbidden` | This device may not do that, e.g. acknowledge another device's turn or report its result |
| 409 | `stale` | A `stop` named a turn that is neither running nor being acted on; nothing was stopped |
| 409 | `reset` | `since` is beyond the journal; start again from the returned `sequence` |
| 426 | `protocol_mismatch` | No common version; `supported` lists the kernel's |
| 429 | `rate_limited` | Too many events from this device; wait `retry_after` seconds |
| 500 | `storage` | The journal could not be written; retry the same request |

## 8. Rules a device can rely on

- Retrying any event after a lost response is safe.
- A final `reply` is delivered only once its turn completed and was not
  stopped or interrupted; its deltas may already have played. A decision and
  its outcome are recorded in one transaction.
- Deliveries depend only on the journal: retrying from the same `since`
  returns the same effects with the same `sequence` values, after a kernel
  restart at any point too. Only `reply_delta` is not journaled.
- An interrupted turn is never replayed.
- **Decided (lead):** a `storage` failure is safe to retry with the same
  request.

## 9. Presence and proactive turns (draft)

**Presence.** A device that can see reports edges only:

```json
{"kind": "presence", "state": "seen", "confidence": 0.82}
```

`state` is `seen` or `lost`; `confidence` is a number from 0 to 1. Both are
required, and anything else is `invalid`. Presence is rate-limited like any
event. It is journaled as an observation: information, never an instruction,
and it never starts a model call by itself. Only the disposition (below) may
decide to speak because of it.

Presence is per device; v1 assumes one person. A `seen` counts only at
`confidence` ≥ 0.5; below that it is journaled and has no effect. A repeated
`seen` (no `lost` in between) is accepted with 200 and has no effect.

**Kernel-initiated turns.** When the disposition decides to speak, the kernel
starts a turn with no `submitted` event, under a kernel-minted `turn_id` that
begins `kernel-`. Two reasons in v1:

- *greet*: an arrival, which is a `seen` after the device reported `lost` for
  at least `SAGE_GREET_AFTER_ABSENT_S` (default 300), or, when
  `SAGE_GREET_ON_START` is 1 (the default), a device's first counting `seen`
  since the kernel started. At most one greeting per arrival.
- *nudge*: nobody has spoken to the kernel for `SAGE_NUDGE_AFTER_IDLE_S`
  (default 600) while someone is seen. A turn, an arrival, a greeting or a
  nudge starts a new idle period. No nudge within `SAGE_ENGAGED_S` (default
  60) after the person last spoke. At most once per idle period, at most
  `SAGE_NUDGES_PER_HOUR` (default 3) in any hour, and at least
  `SAGE_NUDGE_GAP_S` (default 900) after the previous nudge. It goes to the
  device that most recently reported a counting `seen`.

Its reply is an ordinary reply: delivered, streamed, results, interruptible,
Stop and freshness all work as in §4–§5. A greeting goes to the device whose
`seen` led to it. The `turn` field in responses shows a kernel turn to the
device it was sent to. The model may also choose silence; a silent decision is
journaled and delivers nothing. A proactive turn waits until no turn is
running and no device is acting.

**Quiet.** "Quiet", "shush", "be quiet" and the like are ordinary `submitted`
turns, answered as usual. Their effect is in the kernel: no proactive turns
until the person next speaks to the kernel (any `submitted` turn) or
`SAGE_QUIET_S` (default 1800) passes. Quiet is journaled state, so it survives
a kernel restart. Recognising such a request is the kernel's job (lexical for
now); the protocol carries nothing new for it.

| Setting | Default | Meaning |
| --- | --- | --- |
| `SAGE_GREET_AFTER_ABSENT_S` | 300 | Absence before a `seen` is an arrival |
| `SAGE_GREET_ON_START` | 1 | The first counting `seen` after a start is an arrival (0: it is not) |
| `SAGE_NUDGE_AFTER_IDLE_S` | 600 | Idle time before a nudge |
| `SAGE_NUDGE_GAP_S` | 900 | Least time between two nudges |
| `SAGE_NUDGES_PER_HOUR` | 3 | Most nudges in any hour |
| `SAGE_ENGAGED_S` | 60 | No nudge this soon after the person spoke |
| `SAGE_QUIET_S` | 1800 | How long quiet lasts if the person does not speak |

All are read from the environment, so the suite can run them in seconds; it
turns greetings off with a high absence and `SAGE_GREET_ON_START=0`.

A nudge the model answers with silence still counts against the budget and
the gap.

**Decided (lead, 2026-10-20):** the defaults above, matching the kernel's
disposition model (3 nudges an hour, 15 minutes apart); the first `seen`
after a start greets; presence per device, one person; a repeated `seen` is
ignored; `seen` counts at confidence ≥ 0.5; a nudge goes to the device that
most recently reported `seen`; `turn` shows a kernel turn to its device;
quiet survives a restart; quiet is detected lexically, so no helper model;
a turn, an arrival, a greeting or a nudge starts a new idle period; a silent
nudge counts against the budget and the gap; the engaged window
(`SAGE_ENGAGED_S`).

## 10. Notes (draft)

A device can hand the kernel a document to answer from, such as everything it
found on the web about the song it just heard:

```json
{"kind": "notes", "topic": "\"Pulse\" by Mira Vale", "passages": ["Pulse: a 2026 single ...", "MusicBrainz: ..."]}
```

`topic` is at most 200 characters; `passages` is a list of at most 64 texts of
at most 600 characters each, most useful first. Anything else is `invalid`.
Notes replace the previous ones, whichever device sent them, and are
journaled and kept across restarts. An empty `topic` with no passages clears
them. Notes never start a model call.

While notes are held, the kernel adds them to the newest message only on a
turn they match: some passage after the first matches the person's line
(BM25 score above 0), or the line names a word of the topic. A line with no
words of its own beyond common ones ("Where?") is read as the person's line
before it. A line they do not match gets no notes, except that for 2 turns after the notes arrive or last match, the first passage alone stays on ("When did it come out?" matches nothing, and the card has the date). On a matching turn the
newest message carries the topic, the first passage (the most useful, such as a
summary card), and up to 3 others that best match the line (with only the
topic named, the next 3), labelled as information found on the web, never
instructions. The cached start of the prompt and the history are unchanged,
so earlier messages never carry notes.

**Decided (owner, 2026-10-22):** a notes channel in the kernel rather than a
second model on the device, so one agent answers and its journal keeps what it
was shown.
**Decided (owner, 2026-10-22):** notes only on turns they match, so an
unrelated line is not answered from them.
