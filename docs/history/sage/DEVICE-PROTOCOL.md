# Device protocol v0 (unstable)

Any body (Reachy, a phone, a speaker, a screen) talks to the kernel through
this protocol and nothing else. The kernel never names a device type: a device
is a token, a source name, and the manifest it sent. v0 stays unstable until
emote and presence have run through it (slice 3), then freezes as v1.

The device listener is separate from the browser's. It binds to
`SAGE_DEVICE_BIND:SAGE_DEVICE_PORT` (default `127.0.0.1:8766`) and starts only
when `SAGE_DEVICES="reachy=<token>,mock=<token>"` is set (in Docker: put it in
`.env`; compose publishes the listener on `127.0.0.1:8766`). Issuing a token is the
owner's consent for that device: its manifest is trusted as declared.

## Requests

Every request carries `Authorization: Bearer <token>`. The server derives
`source` from the token; a body that names a `source` is rejected. JSON bodies
are at most 8192 bytes. Unknown query parameters are ignored.

| Route | Body / query | Success |
| --- | --- | --- |
| `POST /v0/hello` | `{"protocols": [0], "manifest": {...}}` | `{"protocol": 0, "sequence": N}` |
| `POST /v0/events` | `{"kind", "turn_id"?, "text"?}` | `{"sequence": N, "turn": {...}?}` |
| `GET /v0/effects?since=N&wait=S` | — | `{"sequence": N, "effects": [...], "turn": {...}?}` |

The manifest says what the body can do. Today the kernel reads one part of it:

```json
{"senses": ["speech"], "effects": {"reply": {}, "emote": {"vocabulary": ["cheerful1", "sad1"]}}}
```

`vocabulary` is at most 64 names of at most 64 characters. When this device
starts a turn, the model is offered exactly these names, on the newest message
only, so the start of the prompt never changes. It may pick one; a name the
device did not declare is never delivered. A device sends `hello` again after
every reconnect: manifests are not kept across kernel restarts.

`kind` is one of `submitted`, `stop`, `acknowledged`, `playback_started`,
`playback_finished`, `connected`, `disconnected`. Server-only kinds
(`completed`, `decided`, `restarted`, …) are refused. `turn_id` ≤ 128 chars,
`text` ≤ 4000 chars, as in the kernel.

`turn` is `{"turn_id", "run_status"}` and appears only when the current turn
was started by this device.

`sequence` is the journal's own sequence number. It only increases, across
restarts too. `effects` lists what was delivered to this device after `since`:

```json
{"sequence": 42, "kind": "reply", "turn_id": "reachy-1f…", "text": "Hello.", "stale": false}
```

An `emote` effect has the same shape with the feeling's name in `text`. It is
delivered with its reply, in the same journal row and listed just before it,
so the body can set the tone before the voice starts. `stale` is decided by the kernel's clock: true when the reply was recorded more
than `SAGE_FRESH_SECONDS` (default 10) ago. A speaker should not speak a stale
reply; a screen may show it. Device clocks are never used.

`wait` long-polls for up to `min(S, 25)` seconds until the journal moves past
`since`. An empty list after a wake is normal; fetch again from the returned
`sequence`.

## Errors

Every error is `{"code", "message"}`. The message says what to do next.

| Status | `code` | Meaning |
| --- | --- | --- |
| 400 | `invalid` | Malformed body, unknown kind, or a `source` field |
| 401 | `unauthorized` | Missing or unknown token |
| 409 | `busy` | Another turn is running. Retry the same `turn_id` |
| 409 | `unknown_pending` | A reply was interrupted. The device that started it must `acknowledged` it |
| 409 | `forbidden` | This device may not do that, e.g. acknowledge another device's turn |
| 409 | `reset` | `since` is beyond the journal; start again from the returned `sequence` |
| 426 | `protocol_mismatch` | No common version; `supported` lists the kernel's |

## Rules a device can rely on

- Resubmitting a `turn_id` this device already submitted admits nothing and
  returns the current state. Retrying after a lost response is always safe.
- A reply is delivered only once its turn completed and was not stopped. A
  decision and its outcome are recorded in one transaction.
- Retrying from the same `since` returns the same deliveries with the same
  `sequence` values, including after a kernel restart.
