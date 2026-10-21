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
