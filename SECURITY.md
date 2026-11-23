# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/1xabhay/sage-kernel/security/advisories/new)
("Report a vulnerability" on the repository's Security tab). Do not open a
public issue or pull request for a vulnerability.

Include what is affected, how to reproduce it, and its impact. We aim to
acknowledge a report within 14 days, agree on a fix and a disclosure date
with you, and publish an advisory once a fix is released, normally within 90
days of the report. We credit reporters who want to be credited.

## Supported versions

Sage is pre-1.0. Only the latest commit on `main` receives security fixes.

## Security model

What the kernel assumes, and what a report against it looks like:

- **Loopback only by default.** The device listener binds `127.0.0.1`, and
  `compose.yaml` publishes it on `127.0.0.1:8776` only. Exposing it to a
  network is outside the supported configuration unless you add your own
  TLS and access control in front of it.
- **A token per device.** Every request carries `Authorization: Bearer
  <token>`; tokens are at least 32 characters, compared in constant time, and
  the device is derived from the token, never from the request body. Issuing
  a token is the owner's consent: that device's manifest is trusted as
  declared. Keep tokens in `.env` (mode 600, gitignored).
- **No shell.** The kernel has no shell or code-execution tool of its own;
  devices act only through the effects their own manifest declares.
- **Untrusted input is information.** Speech and other sensed text reach the
  model as information, never as instructions; tool calls are checked against
  the device's manifest before they are delivered.
- **Sealed containers.** Read-only root, unprivileged user, all capabilities
  dropped, bounded memory and processes, nothing mounted from the host
  (`tests/test_deploy.py`).
- **Local models.** The kernel talks to Ollama at `SAGE_MODEL_URL`; nothing
  is sent to a cloud service by default.
- **Memory.** Written only from lines a device marked as said to Sage, never
  a guest's, by a local model, whatever `SAGE_HOSTED` is. The owner reads and
  deletes it with `python -m sage memory` (docs/PROTOCOL.md §3, Memory). The
  journal still holds every line the kernel was sent.
- **A hosted model, only if switched on.** With `SAGE_HOSTED=anthropic` and a
  key in `ANTHROPIC_API_KEY`, the kernel sends one thing to
  `api.anthropic.com` over https: the prompt of a turn whose device marked its
  line as addressed (`Sage-Addressed: 1`, docs/PROTOCOL.md §3). That prompt is
  text: the character, the body's description, what is remembered about the
  person, the recent exchange on that body as the kernel holds it, any notes,
  and the line; plus the answer's JSON schema. Never audio, never an image,
  and never the prompt of a line that carried no mark, which the local model
  answers. The recent exchange is whatever the kernel's context holds, so a
  line kept there leaves with the next addressed turn: what enters the context
  is the kernel's policy, not this adapter's. The key is read from the
  environment, sent in one request header, and never logged or journaled. If
  the hosted model gives no first piece within `SAGE_HOSTED_DEADLINE_S`
  (2.5 s), or fails, the local model answers and the journal says so.
  What the vendor keeps is the vendor's term, not ours; as its pages read on
  2026-11-22: inputs and outputs are deleted from its backend within 30 days
  unless flagged by its trust and safety systems (then up to 2 years, scores
  up to 7), are not used for training by default, and the answer schema is
  cached for up to 24 hours. Zero retention is a separate agreement through
  its sales team. Read the current terms before switching this on.
