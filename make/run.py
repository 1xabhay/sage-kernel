#!/usr/bin/env python3
"""`make run` / `make smoke`: a throwaway native kernel (`python3 -m sage`) the way the conformance suite starts
one: a free port, a temporary state folder, the suite's fake Ollama. Never the live kernel, its port or its
journal. `run` serves until Ctrl-C; `smoke` walks the README's curl sequence against it, then stops it."""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conformance.test_conformance import TOKENS, KernelProcess, Provider

LIVE_PORTS = {8765, 8766, 8776}  # the old kernel's and the live kernel's


def curl(url: str, token: str, *args: str) -> dict:
    out = subprocess.run(["curl", "-sf", "-H", f"Authorization: Bearer {token}", *args, url],
                         check=True, capture_output=True, text=True).stdout
    print(f"  {url.split('/', 3)[-1]} -> {out.strip()[:160]}")
    return json.loads(out)


def smoke(k: str, token: str) -> None:
    """README, "A first device": hello, submit, poll effects until the reply, report its result."""
    j = ("-H", "Content-Type: application/json", "-d")
    n = curl(f"{k}/v1/hello", token, *j, '{"protocols": [1], "manifest": {"senses": ["speech"], "tools": []}}')
    assert n["protocol"] == 1, n
    curl(f"{k}/v1/events", token, *j, '{"kind": "submitted", "turn_id": "t1", "text": "Hello!"}')
    since, reply, deadline = n["sequence"], None, time.monotonic() + 20
    while reply is None and time.monotonic() < deadline:
        body = curl(f"{k}/v1/effects?since={since}&wait=5", token)
        reply = next((e for e in body["effects"] if e["kind"] == "reply"), None)
        if not any(e["kind"] == "reply_delta" for e in body["effects"]):
            since = body["sequence"]
    assert reply and reply["text"] == "Heard: Hello!", reply
    curl(f"{k}/v1/events", token, *j, json.dumps({"kind": "result", "sequence": reply["sequence"], "status": "done"}))
    print("smoke: ok")


def main(mode: str) -> None:
    provider = Provider()
    with tempfile.TemporaryDirectory() as state:
        kernel = KernelProcess(state, provider)
        assert kernel.device_port not in LIVE_PORTS, kernel.device_port
        kernel.start()
        k, token = f"http://127.0.0.1:{kernel.device_port}", TOKENS["reachy"]
        try:
            if mode == "smoke":
                smoke(k, token)
            else:
                print(f'Throwaway kernel (fake model, state in {state}), Ctrl-C stops it:\n'
                      f'  K={k}; A="Authorization: Bearer {token}"')
                kernel.process.wait()
        except KeyboardInterrupt:
            pass
        finally:
            kernel.stop()
            provider.server.shutdown()


if __name__ == "__main__":
    if sys.argv[1:] not in (["run"], ["smoke"]):
        sys.exit("usage: run.py run | smoke")
    main(sys.argv[1])
