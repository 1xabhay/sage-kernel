# Device protocol (Sage OS v1, draft)

Every body, plugin and app talks to the kernel through this protocol and nothing
else: a robot, a phone, the agent's computer, a routine runner, another agent.
The kernel never names a device type. A device is a token, a name and the
manifest it sent.

The name `memory` is the kernel's own: the binary plays a `memory` device that offers
`search_memory` (tier `read`) over the owner's memory files. No token may name it.

`SAGE_DEVICES="robot=<token>,phone=<token>"` issues tokens; issuing one is the
owner's consent, and the manifest is trusted as declared. `SAGE_OWNERS="phone,robot"`
names the devices that speak for the owner: their messages are instructions and
they may approve. Everything any other device says is labelled information.

Routines are just a device (`routines/`): a runner that posts a `message` when a
routine is due, with `id` = `<routine>@<scheduled unix second>`, so the kernel's
repeated-id rule makes its retries and restarts safe.

Every request carries `Authorization: Bearer <token>`, compared in constant time.
The kernel derives the device from the token; a body that names `device` is refused.
JSON bodies are at most 8192 bytes.

| Route | Body / query | Success |
| --- | --- | --- |
| `POST /v1/hello` | `{"manifest": {...}}` | `{"protocol": 1, "sequence": N}` |
| `POST /v1/events` | `{"kind", ...}` below | `{"sequence": N}` |
| `GET /v1/effects?since=N&wait=S&offset=M` | | `{"sequence": N, "effects": [...], "working": bool}` |

`sequence` is the journal's position. It only increases, across restarts too.

## Manifest

```json
{"senses": ["speech"], "sandbox": false,
 "tools": [{"name": "emote", "description": "Show a feeling.", "tier": "body", "max_s": 3,
            "inputSchema": {"type": "object", "properties": {"name": {"enum": ["happy", "sad"]}}, "required": ["name"]}}]}
```

A tool is MCP's `tools/list` entry plus `tier` (`read`, `body`, `change`,
`outside`; unstated means `outside`) and `max_s`. `send` is reserved. Every tool any
device declares is offered to every turn, in a fixed order, and never removed: a tool
whose device is offline returns an error the model reads. Send `hello` again after
every reconnect.

## Events

| `kind` | Fields | Meaning |
| --- | --- | --- |
| `message` | `conversation`, `id`, `text`, `timing`? | Wakes the agent. One turn runs at a time per conversation; messages that arrive meanwhile wait and are handled together. A repeated `id` causes nothing |
| `result` | `effect`, `status` (`done`, `failed`, `interrupted`), `text`?, `timing`? | One per delivered effect, from its target only. The first counts. For an interrupted `send`, `text` is what was heard, and memory keeps only that |
| `interrupted` | `turn`, `text`, `timing`? | A body cut off a reply it was still receiving (barge-in). `text` is what was heard; the turn ends with it and memory keeps only that. If the reply was decided meanwhile, it applies to that `send`, which stays in the journal and still arrives: skip effects of a turn you interrupted |
| `approve` | `pending`, `yes` | From an owner's device. No answer within 15 s means no |
| `stop` | `conversation`? | From any device, at any time. Delivers `stop` to every device acting and ends running turns |
| `gone` | | This device is leaving; its tools return errors until its next `hello` |

## Effects

`wait` long-polls up to 25 s until the journal moves past `since`. An empty list
after a wake is normal; poll again from the returned `sequence`.

```json
{"sequence": 5, "kind": "send",  "turn": 3, "input": {"text": "Hello."}, "stale": false}
{"sequence": 6, "kind": "emote", "turn": 3, "input": {"name": "happy"}, "stale": false}
{"sequence": 9, "kind": "stop",  "by": "phone", "stale": false}
```

`working` is true while a turn that answers this device is running or waiting to run.
When it turns false with no `send`, the agent chose to stay quiet.

**Streaming.** While a turn that answers this device is asking the model, a poll
also holds at most one `reply_delta`: the reply written so far from character
`offset` on. It has no `sequence` and is never journaled. Play it at once, then poll
with `offset` + its length. The turn's `send` follows in the journal with the whole
text; speak only what is past your offset, then poll from offset 0 again. A
`reply_delta` never comes in the same response as its turn's journaled effects.

```json
{"kind": "reply_delta", "turn": 3, "offset": 12, "text": "and then"}
```

`stale` is by the kernel's clock: recorded more than `SAGE_FRESH_SECONDS` (10) ago.
A speaker does not speak a stale `send`; it reports `failed` with text `stale`.

## Errors

`{"code", "message"}`; the message says what to do next.

| Status | `code` | Meaning |
| --- | --- | --- |
| 400 | `invalid` | Malformed body, manifest or field |
| 401 | `unauthorized` | Missing or unknown token |
| 404 | `not_found` | No such route |
| 409 | `forbidden` | Not this device's to do: a result for another device's effect, approval from a non-owner |
| 409 | `reset` | `since` is beyond the journal; poll again from the sequence named |
| 500 | `storage` | The journal could not be written; retry the same request |

`timing` holds whole milliseconds a device measured: `stt_ms` (end of speech to
transcript), `ttft_ms` (message sent to first reply text), `first_audio_ms` (end of
speech to first audio). It is journaled with the message or result.

Not yet: per-device rate limits.
