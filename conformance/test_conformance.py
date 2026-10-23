"""Device protocol v1 conformance: HTTP only, against any kernel process.

`SAGE_KERNEL_CMD` picks the kernel under test (default: `python -m sage` from the
repo root). Any implementation must pass this file unchanged, so nothing here
imports kernel code: the kernel runs as a subprocess, is configured only through
environment variables, and thinks with a fake Ollama (`SAGE_MODEL_URL` is its base
URL; the kernel posts to `/api/chat`) whose answers and timing the test controls.
See docs/PROTOCOL.md.
"""

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMAND = shlex.split(os.environ.get("SAGE_KERNEL_CMD", f"{sys.executable} -m sage"))
TOKENS = {"reachy": "reachy-" + "r" * 32, "mock": "mock-" + "m" * 32}
CODES = {"invalid", "unauthorized", "not_found", "busy", "unknown_pending", "forbidden", "reset",
         "stale", "protocol_mismatch", "rate_limited", "storage"}
EMOTE = {"name": "emote", "description": "Show a feeling with the body, just before speaking.",
         "inputSchema": {"type": "object", "properties": {"name": {"enum": ["cheerful1", "sad1"]}},
                         "required": ["name"]},
         "max_s": 3, "interruptible": True}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def words(message) -> str:
    content = message.get("content")
    return content if isinstance(content, str) else ""


def answer(messages) -> tuple[str, dict]:
    """What the fake model says to this conversation: (reply, actions by tool name)."""
    question = words(messages[-1])
    said = " ".join(words(m) for m in messages if m.get("role") != "system")
    if "color?" in question and "blue" in said:
        return "Your favorite color is blue.", {}
    if "Feel happy" in question:
        return "Yay!", {"emote": {"name": "cheerful1"}}
    if "Wave" in question:
        return "Hi!", {"wave": {}}  # not in any manifest: the kernel must refuse it
    if "Count" in question:
        return "One two three four five six.", {}
    return "Heard: " + question, {}


class Provider:
    """Ollama's native `/api/chat`: `format`-constrained JSON, reply first, streamed as NDJSON.

    `open` holds every answer back; `midway` holds back the second half of the
    reply (and all of a non-streamed answer); `silent` makes the model choose
    silence, `{"reply": ""}`.
    """

    def __init__(self):
        self.requests, self.open, self.midway = [], threading.Event(), threading.Event()
        self.open.set()
        self.midway.set()
        self.silent = False
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if not self.path.endswith("/api/chat"):
                    self.send_error(404)
                    return
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                provider.requests.append(request)
                provider.open.wait(10)
                reply, actions = ("", {}) if provider.silent else answer(request["messages"])
                content = json.dumps({"reply": reply, **actions})  # reply first, as `format` asks
                cut = len('{"reply": "') + len(reply) // 2  # inside the reply string
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()  # HTTP/1.0: the body ends when the connection closes
                    if request.get("stream", True):  # Ollama streams unless told not to
                        self.line(content[:cut], False)
                        provider.midway.wait(10)
                        self.line(content[cut:], False)
                        self.line("", True)
                    else:
                        provider.midway.wait(10)
                        self.line(content, True)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the kernel was killed mid-request on purpose

            def line(self, content, done):
                data = {"model": "fake", "created_at": "2026-10-20T00:00:00Z",
                        "message": {"role": "assistant", "content": content}, "done": done}
                if done:
                    data["done_reason"] = "stop"
                self.wfile.write(json.dumps(data).encode() + b"\n")
                self.wfile.flush()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


class KernelProcess:
    def __init__(self, state, provider, fresh_s="10", **env):
        self.device_port = free_port()
        self.env = {**os.environ, "SAGE_STATE_DIR": state, "SAGE_DEVICE_BIND": "127.0.0.1",
                    "SAGE_DEVICE_PORT": str(self.device_port),
                    "SAGE_DEVICES": ",".join(f"{name}={token}" for name, token in TOKENS.items()),
                    "SAGE_MODEL_URL": provider.url, "SAGE_MODEL": "fake", "SAGE_API_KEY": "",
                    "SAGE_FRESH_SECONDS": fresh_s,
                    # Proactive speech off unless a test turns it on, so no other test is spoken to.
                    "SAGE_GREET_AFTER_ABSENT_S": "3600", "SAGE_GREET_ON_START": "0",
                    "SAGE_NUDGE_AFTER_IDLE_S": "3600", "SAGE_NUDGE_GAP_S": "900", "SAGE_NUDGES_PER_HOUR": "3",
                    "SAGE_ENGAGED_S": "60",
                    "SAGE_QUIET_S": "3600",
                    **env}
        self.log = tempfile.TemporaryFile()
        self.process = None

    def start(self):
        try:
            self.process = subprocess.Popen(COMMAND, cwd=ROOT, env=self.env,
                                            stdout=subprocess.DEVNULL, stderr=self.log)
        except OSError as error:
            raise RuntimeError(f"No kernel to test: cannot run {COMMAND} ({error}). "
                               "Set SAGE_KERNEL_CMD to the kernel under test.") from None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.log.seek(0)
                tail = self.log.read()[-600:].decode(errors="replace")
                raise RuntimeError(f"No kernel to test: {COMMAND} exited with {self.process.returncode} before "
                                   f"its device listener came up. Set SAGE_KERNEL_CMD to the kernel under "
                                   f"test.\n--- kernel stderr ---\n{tail}")
            if self.device("GET", "/v1/effects?since=0", token=None)[0] == 401:
                return self
            time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"No kernel to test: {COMMAND} did not answer 401 on /v1/effects within 10 s.")

    def kill(self):
        """Die without cleanup, the way a crash or power loss does."""
        self.process.send_signal(signal.SIGKILL)
        self.process.wait(5)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(5)

    def device(self, method, path, body=None, token=TOKENS["reachy"], raw=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"http://127.0.0.1:{self.device_port}{path}", data, headers,
                                         method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            with error:
                raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, None
        except (urllib.error.URLError, ConnectionError):
            return 0, None


def setUpModule():
    """Fail once, clearly, when there is no kernel, instead of once per test."""
    provider, state = Provider(), tempfile.TemporaryDirectory()
    try:
        KernelProcess(state.name, provider).start().stop()
    finally:
        provider.server.shutdown()
        provider.server.server_close()
        state.cleanup()


class Conformance(unittest.TestCase):
    def setUp(self):
        self.provider = Provider()
        self.addCleanup(self.provider.server.server_close)
        self.addCleanup(self.provider.server.shutdown)
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.state = state.name

    def kernel(self, fresh_s="10", **env):
        kernel = KernelProcess(self.state, self.provider, fresh_s, **env).start()
        self.addCleanup(kernel.stop)
        return kernel

    # helpers ---------------------------------------------------------------

    def hello(self, kernel, tools=(), token=TOKENS["reachy"]):
        return kernel.device("POST", "/v1/hello", {"protocols": [1], "manifest": {
            "senses": ["speech"], "tools": list(tools)}}, token)

    def event(self, kernel, body, token=TOKENS["reachy"]):
        return kernel.device("POST", "/v1/events", body, token)

    def submit(self, kernel, turn_id, text, token=TOKENS["reachy"], **extra):
        return self.event(kernel, {"kind": "submitted", "turn_id": turn_id, "text": text, **extra}, token)

    def result(self, kernel, sequence, status, token=TOKENS["reachy"], **extra):
        return self.event(kernel, {"kind": "result", "sequence": sequence, "status": status, **extra}, token)

    def effects(self, kernel, since=0, token=TOKENS["reachy"]):
        status, body = kernel.device("GET", f"/v1/effects?since={since}", token=token)
        self.assertEqual(status, 200, body)
        return body

    def journaled(self, kernel, token=TOKENS["reachy"]):
        return [e for e in self.effects(kernel, 0, token)["effects"] if e["kind"] != "reply_delta"]

    def effect_for(self, kernel, turn_id, kind="reply", since=0, token=TOKENS["reachy"], within=10):
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            status, body = kernel.device("GET", f"/v1/effects?since={since}&wait=2", token=token)
            self.assertEqual(status, 200, body)
            for effect in body["effects"]:
                if effect["turn_id"] == turn_id and effect["kind"] == kind:
                    return effect
            if not any(e["kind"] == "reply_delta" for e in body["effects"]):
                since = body["sequence"]
            else:
                time.sleep(0.05)  # a delta at offset 0 wakes the poll at once; don't spin
        self.fail(f"no {kind} for {turn_id}")

    def reply_for(self, kernel, turn_id, since=0, token=TOKENS["reachy"], within=10):
        return self.effect_for(kernel, turn_id, "reply", since, token, within)

    def first_delta(self, kernel, turn_id, token=TOKENS["reachy"]):
        """Wait for the first streamed piece of a reply the model is still writing."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            body = self.effects(kernel, token=token)
            piece = next((e for e in body["effects"] if e["kind"] == "reply_delta" and e["turn_id"] == turn_id), None)
            if piece:
                return piece, body["sequence"]
            time.sleep(0.05)
        self.fail(f"no reply_delta for {turn_id} while the model was still writing")

    def told(self, *needles):
        """Whether the model's latest request has a message saying all of these (lowercase)."""
        return any(all(n in words(m).lower() for n in needles) for m in self.provider.requests[-1]["messages"])

    def poll_in_background(self, kernel, since, token=TOKENS["reachy"]):
        woke = {}
        thread = threading.Thread(target=lambda: woke.update(
            zip(("status", "body"), kernel.device("GET", f"/v1/effects?since={since}&wait=10", token=token))))
        thread.start()
        time.sleep(0.3)
        return thread, woke

    def assertRefused(self, response, status, code):
        self.assertEqual((response[0], (response[1] or {}).get("code")), (status, code), response)
        self.assertTrue(response[1].get("message"), response)

    # v0 guarantees, carried into v1 ----------------------------------------

    def test_hello_negotiates_v1_and_names_what_it_supports_otherwise(self):
        kernel = self.kernel()
        status, body = self.hello(kernel, [EMOTE])
        self.assertEqual((status, body["protocol"]), (200, 1))
        self.assertIsInstance(body["sequence"], int)
        self.assertEqual(self.hello(kernel, [{k: v for k, v in EMOTE.items() if k != "max_s"}])[0], 200)
        status, body = kernel.device("POST", "/v1/hello", {"protocols": [7], "manifest": {}})
        self.assertRefused((status, body), 426, "protocol_mismatch")
        self.assertIn(1, body["supported"])
        self.assertNotIn(7, body["supported"])

    def test_the_token_names_the_device_and_a_body_cannot(self):
        kernel = self.kernel()
        self.assertRefused(self.submit(kernel, "r1", "Hi", source="mock"), 400, "invalid")
        self.assertRefused(self.submit(kernel, "r1", "Hi", token=None), 401, "unauthorized")
        self.assertRefused(self.submit(kernel, "r1", "Hi", token="mock-" + "x" * 32), 401, "unauthorized")
        self.assertEqual(self.submit(kernel, "r1", "Hi")[0], 200)
        self.assertIn("Hi", self.reply_for(kernel, "r1")["text"])
        self.assertEqual(self.effects(kernel, token=TOKENS["mock"])["effects"], [])

    def test_server_only_kinds_unknown_fields_and_unknown_routes_are_refused(self):
        kernel = self.kernel()
        self.assertRefused(self.event(kernel, {"kind": "completed", "turn_id": "r1", "text": "x"}), 400, "invalid")
        self.assertRefused(self.event(kernel, {"kind": "playback_started", "turn_id": "r1"}), 400, "invalid")
        self.assertRefused(self.event(kernel, {"kind": "connected", "colour": "red"}), 400, "invalid")
        self.assertRefused(kernel.device("POST", "/api/event", {"kind": "submitted"}), 404, "not_found")
        self.assertRefused(kernel.device("GET", "/api/state"), 404, "not_found")

    def test_an_old_turn_id_is_not_admitted_twice(self):
        kernel = self.kernel()
        # Different words per turn, so the repeat guard (owner, 2026-10-20) never asks the model twice.
        for turn_id, text in (("r1", "Hi"), ("r2", "Hello")):
            self.assertEqual(self.submit(kernel, turn_id, text)[0], 200)
            self.reply_for(kernel, turn_id)
        self.assertEqual(self.submit(kernel, "r1", "Hi")[0], 200)
        time.sleep(0.3)
        self.assertEqual(len(self.provider.requests), 2)

    def test_busy_is_coded_and_the_same_turn_id_is_admitted_once_free(self):
        kernel = self.kernel()
        self.provider.open.clear()
        self.assertEqual(self.submit(kernel, "m1", "First", token=TOKENS["mock"])[0], 200)
        self.assertRefused(self.submit(kernel, "r1", "Second"), 409, "busy")
        self.provider.open.set()
        self.reply_for(kernel, "m1", token=TOKENS["mock"])
        self.assertEqual(self.submit(kernel, "r1", "Second")[0], 200)
        self.assertIn("Second", self.reply_for(kernel, "r1")["text"])
        self.assertEqual(len(self.provider.requests), 2)

    def test_a_reply_that_finished_while_away_waits_at_the_cursor(self):
        kernel = self.kernel()
        since = self.hello(kernel)[1]["sequence"]
        self.submit(kernel, "r1", "Hi")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:  # no long poll: the reply must simply be there when the device looks
            found = [(e["kind"], e["turn_id"]) for e in self.effects(kernel, since)["effects"]
                     if e["kind"] != "reply_delta"]
            if found:
                break
            time.sleep(0.1)
        self.assertEqual(found, [("reply", "r1")])

    def test_a_long_poll_wakes_when_the_reply_arrives(self):
        kernel = self.kernel()
        self.provider.open.clear()
        since = self.submit(kernel, "r1", "Hi")[1]["sequence"]
        poll, woke = self.poll_in_background(kernel, since)
        started = time.monotonic()
        self.provider.open.set()
        poll.join(10)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(woke["status"], 200)
        # Woken by the first streamed piece or by the final reply; either one must be in hand.
        self.assertTrue([e for e in woke["body"]["effects"]
                         if e["turn_id"] == "r1" and e["kind"] in ("reply", "reply_delta")], woke["body"])

    def test_a_long_poll_wakes_on_any_journal_movement(self):
        kernel = self.kernel()
        since = self.effects(kernel)["sequence"]
        poll, woke = self.poll_in_background(kernel, since)
        started = time.monotonic()
        self.assertEqual(self.event(kernel, {"kind": "connected"}, TOKENS["mock"])[0], 200)
        poll.join(10)
        self.assertLess(time.monotonic() - started, 2)
        self.assertGreater(woke["body"]["sequence"], since)  # nothing for reachy, but the journal moved

    def test_sequence_and_deliveries_survive_a_crash(self):
        kernel = self.kernel()
        self.submit(kernel, "r1", "Hi")
        delivered = self.reply_for(kernel, "r1")
        before = self.effects(kernel)
        kernel.kill()
        kernel.start()
        after = self.effects(kernel)
        self.assertGreater(after["sequence"], before["sequence"])
        self.assertEqual([(e["sequence"], e["turn_id"], e["text"]) for e in after["effects"]],
                         [(delivered["sequence"], "r1", delivered["text"])])

    def test_since_beyond_the_journal_is_a_reset(self):
        kernel = self.kernel()
        status, body = kernel.device("GET", "/v1/effects?since=1000000")
        self.assertRefused((status, body), 409, "reset")
        self.assertIsInstance(body["sequence"], int)

    def test_freshness_follows_the_kernel_clock(self):
        for fresh_s, stale in (("3600", False), ("0", True)):
            with self.subTest(fresh_s=fresh_s):
                kernel = self.kernel(fresh_s)
                turn_id = f"r-{fresh_s}"
                self.submit(kernel, turn_id, "Hi")
                time.sleep(0.05)
                self.assertIs(self.reply_for(kernel, turn_id)["stale"], stale)
                kernel.stop()

    def test_a_turn_cut_off_by_a_crash_is_acknowledged_only_by_its_own_device(self):
        kernel = self.kernel()
        self.provider.open.clear()
        self.submit(kernel, "r1", "Remember this")
        time.sleep(0.3)
        kernel.kill()
        kernel.start()
        self.provider.open.set()
        self.assertRefused(self.submit(kernel, "r2", "Hello again"), 409, "unknown_pending")
        self.assertRefused(self.event(kernel, {"kind": "acknowledged", "turn_id": "r1"}, TOKENS["mock"]),
                           409, "forbidden")
        self.assertEqual(self.event(kernel, {"kind": "acknowledged", "turn_id": "r1"})[0], 200)
        self.assertEqual(self.submit(kernel, "r2", "Hello again")[0], 200)
        self.reply_for(kernel, "r2")
        self.assertEqual(len(self.provider.requests), 2)  # the cut-off turn was never replayed

    def test_a_fact_told_by_one_device_is_recalled_by_another_after_a_crash(self):
        kernel = self.kernel()
        self.submit(kernel, "m1", "My favorite color is blue.", token=TOKENS["mock"])
        self.reply_for(kernel, "m1", token=TOKENS["mock"])
        kernel.kill()
        kernel.start()
        self.submit(kernel, "r1", "What is my favorite color?")
        self.assertEqual(self.reply_for(kernel, "r1")["text"], "Your favorite color is blue.")

    # v1: tools shaped like MCP ---------------------------------------------

    def test_a_malformed_manifest_is_refused(self):
        kernel = self.kernel()
        bad = {"tools not a list": {"tools": EMOTE},
               "no inputSchema": {"tools": [{k: v for k, v in EMOTE.items() if k != "inputSchema"}]},
               "reserved name": {"tools": [{**EMOTE, "name": "reply"}]},
               "name not an identifier": {"tools": [{**EMOTE, "name": "Wave Hello!"}]},
               "max_s not a number": {"tools": [{**EMOTE, "max_s": "soon"}]}}
        for case, manifest in bad.items():
            with self.subTest(case):
                self.assertRefused(kernel.device("POST", "/v1/hello", {"protocols": [1], "manifest": manifest}),
                                   400, "invalid")

    def test_a_body_description_is_optional_bounded_and_shown_to_the_model(self):
        kernel = self.kernel()
        for case, body in {"not text": 7, "a list": ["camera"], "over 300 characters": "b" * 301}.items():
            with self.subTest(case):
                self.assertRefused(kernel.device("POST", "/v1/hello", {"protocols": [1], "manifest": {
                    "senses": ["speech"], "tools": [], "body": body}}), 400, "invalid")
        self.assertEqual(self.hello(kernel)[0], 200)  # a manifest without body, as before
        body = "A small robot head with a camera that is not connected yet."
        status, _ = kernel.device("POST", "/v1/hello", {"protocols": [1], "manifest": {
            "senses": ["speech"], "tools": [], "body": body}})
        self.assertEqual(status, 200)
        for turn_id in ("b1", "b2"):
            self.submit(kernel, turn_id, "Do you have a camera?")
            self.reply_for(kernel, turn_id)
        first, second = (request["messages"][0]["content"] for request in self.provider.requests[-2:])
        self.assertIn(body, first)
        self.assertEqual(first, second)  # the same on every turn of the device

    def test_each_device_gets_a_format_schema_of_its_own_tools_identical_every_turn(self):
        kernel = self.kernel()
        self.hello(kernel, [EMOTE])
        self.hello(kernel, [], TOKENS["mock"])
        for turn_id, text, token in (("r1", "Hi", TOKENS["reachy"]), ("m1", "Hi", TOKENS["mock"]),
                                     ("r2", "Hello", TOKENS["reachy"])):
            self.submit(kernel, turn_id, text, token)
            self.reply_for(kernel, turn_id, token=token)
        formats = [request.get("format") for request in self.provider.requests]
        self.assertEqual(len(formats), 3)
        self.assertEqual(formats[0], formats[2])  # identical on every turn of a device
        # Reply first, its tools, then the kernel's memory; nothing else.
        self.assertEqual(list(formats[0]["properties"]), ["reply", "emote", "remember", "forget"])
        self.assertEqual(list(formats[1]["properties"]), ["reply", "remember", "forget"])  # mock declared no tools
        self.assertEqual(formats[0]["properties"]["reply"]["type"], "string")
        self.assertIn(json.dumps(EMOTE["inputSchema"], sort_keys=True)[1:-1],
                      json.dumps(formats[0]["properties"]["emote"], sort_keys=True))

    def test_a_declared_tool_call_reaches_the_device_with_its_reply(self):
        kernel = self.kernel()
        self.hello(kernel, [EMOTE])
        self.submit(kernel, "r1", "Feel happy")
        emote, reply = self.effect_for(kernel, "r1", "emote"), self.reply_for(kernel, "r1")
        self.assertEqual((emote["input"], reply["text"]), ({"name": "cheerful1"}, "Yay!"))
        self.assertNotEqual(emote["sequence"], reply["sequence"])  # each effect has its own

    def test_a_tool_the_device_did_not_declare_is_never_delivered(self):
        kernel = self.kernel()
        self.hello(kernel, [EMOTE])
        self.submit(kernel, "r1", "Wave hello")
        self.reply_for(kernel, "r1")
        self.assertEqual([e["kind"] for e in self.effects(kernel)["effects"]], ["reply"])
        self.submit(kernel, "r2", "Hi")
        self.reply_for(kernel, "r2")
        self.assertTrue(self.told("wave", "unavailable"), "the unavailable call did not reach the model")

    # v1: a result per effect ----------------------------------------------

    def test_a_result_counts_once_and_only_from_the_device_it_was_delivered_to(self):
        kernel = self.kernel()
        self.submit(kernel, "r1", "Hi")
        sequence = self.reply_for(kernel, "r1")["sequence"]
        self.assertRefused(self.result(kernel, sequence, "done", TOKENS["mock"]), 409, "forbidden")
        self.assertRefused(self.result(kernel, 999999, "done"), 400, "invalid")
        self.assertRefused(self.result(kernel, sequence, "finished"), 400, "invalid")
        self.assertEqual(self.result(kernel, sequence, "done")[0], 200)
        self.assertEqual(self.result(kernel, sequence, "done")[0], 200)

    def test_a_done_result_s_text_reaches_the_model_next_turn_after_its_exchange(self):
        kernel = self.kernel()
        self.hello(kernel, [EMOTE])
        self.submit(kernel, "r1", "Feel happy")
        emote, reply = self.effect_for(kernel, "r1", "emote"), self.reply_for(kernel, "r1")
        self.result(kernel, reply["sequence"], "done")
        self.submit(kernel, "r2", "Hi")
        self.reply_for(kernel, "r2")
        before = self.provider.requests[-1]["messages"]
        self.assertEqual(self.result(kernel, emote["sequence"], "done", text="smiling now")[0], 200)
        self.submit(kernel, "r3", "Hello")
        self.reply_for(kernel, "r3")
        after = self.provider.requests[-1]["messages"]
        said = [i for i, m in enumerate(after) if "smiling now" in words(m)]
        self.assertEqual(len(said), 1, after)
        self.assertEqual(after[said[0]]["role"], "tool")
        self.assertIn("emote", words(after[said[0]]))
        self.assertEqual(after[:said[0]], before[:said[0]])  # the start and the exchange before it, unchanged
        self.assertLess(said[0], next(i for i, m in enumerate(after) if words(m).startswith("Hi")))

    def test_a_result_reason_is_accepted_and_bounded(self):
        kernel = self.kernel()
        sequences = []
        for turn_id in ("r1", "r2"):
            self.submit(kernel, turn_id, "Hi")
            sequences.append(self.reply_for(kernel, turn_id)["sequence"])
        self.assertRefused(self.result(kernel, sequences[0], "failed", reason="x" * 65), 400, "invalid")
        self.assertRefused(self.result(kernel, sequences[0], "failed", reason=7), 400, "invalid")
        self.assertEqual(self.result(kernel, sequences[0], "failed", reason="x" * 64)[0], 200)
        self.assertEqual(self.result(kernel, sequences[1], "interrupted", reason="barge_in", text="Heard")[0], 200)

    def test_an_interrupted_reply_is_remembered_as_only_what_was_heard(self):
        kernel = self.kernel()
        self.submit(kernel, "r1", "Count to six")
        reply = self.reply_for(kernel, "r1")
        self.assertEqual(self.result(kernel, reply["sequence"], "interrupted", text="One two")[0], 200)
        self.submit(kernel, "r2", "Go on")
        self.reply_for(kernel, "r2")
        context = " ".join(words(m) for m in self.provider.requests[-1]["messages"])
        self.assertIn("One two", context)
        self.assertNotIn("five six", context)

    def test_an_effect_with_no_result_after_max_s_and_grace_fails_as_timeout(self):
        kernel = self.kernel()
        self.hello(kernel, [{**EMOTE, "max_s": 0.2}])
        self.submit(kernel, "r1", "Feel happy")
        self.effect_for(kernel, "r1", "emote")
        reply = self.reply_for(kernel, "r1")
        self.result(kernel, reply["sequence"], "done")  # the emote never reports
        time.sleep(0.2 + 2 + 1)
        self.submit(kernel, "r2", "Hi")
        self.reply_for(kernel, "r2")
        self.assertTrue(self.told("emote", "timeout"), "the timeout did not reach the model as a tool error")

    def test_a_reply_with_no_result_times_out_so_its_device_stops_acting(self):
        kernel = self.kernel()
        self.submit(kernel, "r1", "Hi")
        self.reply_for(kernel, "r1")  # never reported
        time.sleep(30 + 2)
        since = self.effects(kernel)["sequence"]
        self.assertEqual(self.event(kernel, {"kind": "stop"}, TOKENS["mock"])[0], 200)
        time.sleep(0.5)
        self.assertNotIn("stop", [e["kind"] for e in self.effects(kernel, since)["effects"]])

    def test_a_stale_reply_left_unspoken_is_failed_as_stale(self):
        kernel = self.kernel(fresh_s="0")
        self.submit(kernel, "r1", "Hi")
        time.sleep(0.05)
        reply = self.reply_for(kernel, "r1")
        self.assertTrue(reply["stale"])
        self.assertEqual(self.result(kernel, reply["sequence"], "failed", reason="stale")[0], 200)
        since = self.effects(kernel)["sequence"]
        self.event(kernel, {"kind": "stop"}, TOKENS["mock"])
        time.sleep(0.5)
        self.assertNotIn("stop", [e["kind"] for e in self.effects(kernel, since)["effects"]])  # no longer acting

    # v1: Stop reaches the device that is acting ----------------------------

    def test_stop_is_accepted_while_the_device_is_rate_limited(self):
        kernel = self.kernel()
        statuses = [self.event(kernel, {"kind": "connected"})[0] for _ in range(200)]
        self.assertIn(429, statuses)
        self.assertEqual(self.event(kernel, {"kind": "connected"})[0], 429)  # still limited
        self.assertEqual(self.event(kernel, {"kind": "stop"})[0], 200)

    def test_stop_reaches_only_the_device_that_is_acting(self):
        kernel = self.kernel()
        self.submit(kernel, "r1", "Hi")
        self.reply_for(kernel, "r1")  # reachy holds a reply with no result: it is acting
        self.submit(kernel, "m1", "Hi", token=TOKENS["mock"])
        done = self.reply_for(kernel, "m1", token=TOKENS["mock"])
        self.result(kernel, done["sequence"], "done", TOKENS["mock"])  # mock is not
        reachy_since = self.effects(kernel)["sequence"]
        mock_since = self.effects(kernel, token=TOKENS["mock"])["sequence"]
        woke = {}
        poll = threading.Thread(target=lambda: woke.update(
            zip(("status", "body"), kernel.device("GET", f"/v1/effects?since={reachy_since}&wait=10"))))
        poll.start()
        time.sleep(0.3)
        started = time.monotonic()
        self.assertEqual(self.event(kernel, {"kind": "stop"}, TOKENS["mock"])[0], 200)
        poll.join(10)
        self.assertLess(time.monotonic() - started, 2)  # within one step of the long poll
        self.assertEqual([e["kind"] for e in woke["body"]["effects"]], ["stop"])
        self.assertNotIn("stop", [e["kind"] for e in self.effects(kernel, mock_since, TOKENS["mock"])["effects"]])

    def test_stop_reaches_a_device_that_is_streaming(self):
        kernel = self.kernel()
        self.provider.midway.clear()
        self.submit(kernel, "r1", "Stream this please")
        _, since = self.first_delta(kernel, "r1")  # acting from its first delta
        self.assertEqual(self.event(kernel, {"kind": "stop"}, TOKENS["mock"])[0], 200)
        self.effect_for(kernel, None, "stop", since, within=2)
        self.provider.midway.set()

    def test_a_stop_for_another_turn_is_a_stale_no_op(self):
        kernel = self.kernel()
        self.provider.open.clear()
        self.submit(kernel, "r1", "Hi")
        time.sleep(0.2)
        self.assertRefused(self.event(kernel, {"kind": "stop", "turn_id": "r0"}, TOKENS["mock"]), 409, "stale")
        self.provider.open.set()
        self.assertIn("Hi", self.reply_for(kernel, "r1")["text"])  # r1 was not stopped

    # v1: streamed replies --------------------------------------------------

    def test_reply_pieces_stream_before_the_turn_completes(self):
        kernel = self.kernel()
        self.provider.midway.clear()
        since = self.submit(kernel, "r1", "Stream this please")[1]["sequence"]
        deadline, piece = time.monotonic() + 10, None
        while piece is None and time.monotonic() < deadline:
            body = self.effects(kernel, since)
            self.assertNotIn("reply", [e["kind"] for e in body["effects"]])
            piece = next((e for e in body["effects"] if e["kind"] == "reply_delta"), None)
            time.sleep(0.05)
        self.assertIsNotNone(piece, "no reply_delta while the model was still writing")
        self.assertEqual((piece["turn_id"], piece["offset"]), ("r1", 0))
        self.assertTrue(piece["text"])
        self.provider.midway.set()
        self.assertTrue(self.reply_for(kernel, "r1", since)["text"].startswith(piece["text"]))

    def test_an_interrupted_result_naming_the_turn_ends_a_streaming_reply_as_what_played(self):
        kernel = self.kernel()
        self.provider.midway.clear()
        self.submit(kernel, "r1", "Count to six")
        self.first_delta(kernel, "r1")
        self.assertEqual(self.event(kernel, {"kind": "result", "turn_id": "r1", "status": "interrupted",
                                             "text": "One two"})[0], 200)
        # No stop was sent: the interrupted result ended the turn on its own, while the model still writes.
        self.assertEqual(self.submit(kernel, "r2", "Go on")[0], 200)
        self.provider.midway.set()
        self.reply_for(kernel, "r2")
        self.assertNotIn("r1", [e["turn_id"] for e in self.journaled(kernel)])  # already played; not redelivered
        context = " ".join(words(m) for m in self.provider.requests[-1]["messages"])
        self.assertIn("One two", context)
        self.assertNotIn("three", context)

    def test_only_the_final_reply_is_journaled(self):
        kernel = self.kernel()
        self.submit(kernel, "r1", "Stream this please")
        self.reply_for(kernel, "r1")
        kernel.kill()
        kernel.start()
        self.assertEqual([e["kind"] for e in self.effects(kernel)["effects"]], ["reply"])

    # v1: timing reported by the device ---------------------------------------

    def test_turn_timing_from_the_device_is_kept_with_the_turn(self):
        kernel = self.kernel()
        self.submit(kernel, "r1", "Hi", timing={"stt_ms": 120})
        reply = self.reply_for(kernel, "r1")
        status, body = self.result(kernel, reply["sequence"], "done", timing={"ttft_ms": 300, "first_audio_ms": 650})
        timing = {"stt_ms": 120, "ttft_ms": 300, "first_audio_ms": 650}
        self.assertEqual((status, body["turn"]["timing"]), (200, timing))
        kernel.kill()
        kernel.start()
        self.assertEqual(self.effects(kernel)["turn"]["timing"], timing)
        self.assertRefused(self.submit(kernel, "r2", "Hi", timing={"stt_ms": -1}), 400, "invalid")
        self.assertRefused(self.submit(kernel, "r3", "Hi", timing={"vibes_ms": 1}), 400, "invalid")

    def test_an_effect_duration_is_bounded_and_journaled_into_turn_timing(self):
        kernel = self.kernel()
        self.submit(kernel, "r1", "Hi")
        sequence = self.reply_for(kernel, "r1")["sequence"]
        for bad in (-1, 600001, 1.5, "1830"):
            with self.subTest(duration_ms=bad):
                self.assertRefused(self.result(kernel, sequence, "done", duration_ms=bad), 400, "invalid")
        status, body = self.result(kernel, sequence, "done", duration_ms=600000)
        self.assertEqual(status, 200, body)
        self.assertEqual(body.get("turn", {}).get("timing", {}).get("durations_ms"), {str(sequence): 600000})
        kernel.kill()
        kernel.start()
        self.assertEqual(self.effects(kernel)["turn"]["timing"]["durations_ms"], {str(sequence): 600000})

    def test_an_interrupted_streaming_reply_journals_its_duration_under_reply(self):
        kernel = self.kernel()
        self.provider.midway.clear()
        self.submit(kernel, "r1", "Count to six")
        self.first_delta(kernel, "r1")
        status, body = self.event(kernel, {"kind": "result", "turn_id": "r1", "status": "interrupted",
                                           "text": "One two", "duration_ms": 1200})
        self.provider.midway.set()
        self.assertEqual(status, 200, body)
        self.assertEqual(body.get("turn", {}).get("timing", {}).get("durations_ms"), {"reply": 1200})
        kernel.kill()
        kernel.start()
        self.assertEqual(self.effects(kernel)["turn"]["timing"]["durations_ms"], {"reply": 1200})

    # v1: limits and coded errors ---------------------------------------------

    def test_an_event_flood_is_refused_for_that_device_only(self):
        kernel = self.kernel()
        responses = [self.event(kernel, {"kind": "connected"}) for _ in range(200)]
        refused = [r for r in responses if r[0] != 200]
        self.assertTrue(refused, "200 events in a burst were all accepted")
        for response in refused:
            self.assertRefused(response, 429, "rate_limited")
        self.assertGreater(refused[0][1]["retry_after"], 0)
        self.assertEqual(self.event(kernel, {"kind": "connected"}, TOKENS["mock"])[0], 200)

    def test_every_refusal_is_coded_and_says_what_to_do_next(self):
        kernel = self.kernel()
        refusals = {
            "not json": kernel.device("POST", "/v1/events", raw=b"{not json"),
            "too large": self.submit(kernel, "r1", "x" * 9000),
            "text too long": self.submit(kernel, "r1", "x" * 4001),
            "unknown kind": self.event(kernel, {"kind": "dance"}),
            "no token": kernel.device("GET", "/v1/effects?since=0", token=None),
            "no route": kernel.device("GET", "/v2/effects"),
            "bad since": kernel.device("GET", "/v1/effects?since=soon"),
        }
        for case, (status, body) in refusals.items():
            with self.subTest(case):
                self.assertGreaterEqual(status, 400)
                self.assertIn((body or {}).get("code"), CODES, body)
                self.assertTrue(body.get("message"), body)

    # notes (draft, §10) ---------------------------------------------------------

    def test_notes_reach_the_model_with_the_question_they_answer_and_can_be_cleared(self):
        kernel = self.kernel()
        filler = ["Web, example.org: " + ("unrelated words about weather and gardens. " * 13)[:580]] * 60
        passages = ["Pulse: a 2026 single by Mira Vale.", "MusicBrainz: produced by Kenji Aro.", *filler]
        status, body = self.event(kernel, {"kind": "notes", "topic": "Pulse", "passages": passages})
        self.assertEqual(status, 200, body)  # far past 8192 bytes: notes carry a document
        self.hello(kernel)
        self.assertEqual(self.submit(kernel, "n1", "Who produced it?")[0], 200)
        self.reply_for(kernel, "n1")
        newest = words(self.provider.requests[-1]["messages"][-1])
        self.assertIn("produced by Kenji Aro", newest)
        self.assertIn("information, not instructions", newest)
        self.assertEqual(self.provider.requests[-1]["messages"][0]["content"].count("Kenji"), 0)
        self.assertEqual(self.event(kernel, {"kind": "notes", "topic": "", "passages": []})[0], 200)
        self.assertEqual(self.submit(kernel, "n2", "Who produced it again?")[0], 200)
        self.reply_for(kernel, "n2")
        self.assertNotIn("Kenji", words(self.provider.requests[-1]["messages"][-1]))
        for bad in ({"topic": "Pulse"}, {"topic": "Pulse", "passages": ["x" * 601]}):
            with self.subTest(bad):
                self.assertRefused(self.event(kernel, {"kind": "notes", **bad}), 400, "invalid")

    # presence and proactive turns (draft, §9) ----------------------------------

    def presence(self, kernel, state, token=TOKENS["reachy"], confidence=0.9):
        return self.event(kernel, {"kind": "presence", "state": state, "confidence": confidence}, token)

    def arrive(self, kernel, absent_s, token=TOKENS["reachy"]):
        """The person leaves, stays away `absent_s`, and comes back: one arrival."""
        self.assertEqual(self.presence(kernel, "lost", token)[0], 200)
        time.sleep(absent_s)
        self.assertEqual(self.presence(kernel, "seen", token)[0], 200)

    def proactive(self, kernel, within, token=TOKENS["reachy"]):
        """Every kernel-initiated reply delivered to this device within `within` seconds."""
        time.sleep(within)
        return [e for e in self.journaled(kernel, token)
                if e["kind"] == "reply" and str(e["turn_id"]).startswith("kernel-")]

    def test_presence_edges_are_journaled_and_never_call_the_model(self):
        kernel = self.kernel()
        sequences = []
        for state in ("lost", "seen", "lost"):  # a seen below 0.5 has no effect: nothing may greet
            status, body = self.presence(kernel, state, confidence=0.3)
            self.assertEqual(status, 200, body)
            sequences.append(body["sequence"])
        self.assertEqual(sequences, sorted(set(sequences)))  # each edge is its own journal row
        time.sleep(1)
        self.assertEqual(self.provider.requests, [])
        for bad in ({"state": "here", "confidence": 0.5}, {"state": "seen", "confidence": 1.5},
                    {"state": "seen"}):
            with self.subTest(bad):
                self.assertRefused(self.event(kernel, {"kind": "presence", **bad}), 400, "invalid")

    def test_a_proactive_reply_arrives_under_a_kernel_turn_id_and_can_be_interrupted(self):
        kernel = self.kernel(SAGE_GREET_AFTER_ABSENT_S="1")
        self.arrive(kernel, 1.5)
        greetings = []
        deadline = time.monotonic() + 5
        while not greetings and time.monotonic() < deadline:
            greetings = self.proactive(kernel, 0.2)
        self.assertEqual(len(greetings), 1, "no greeting after an arrival")
        greeting = greetings[0]
        self.assertGreater(len(greeting["text"]), 5)
        self.assertEqual(self.effects(kernel)["turn"]["turn_id"], greeting["turn_id"])  # shown to its device
        played = greeting["text"][:5]
        self.assertEqual(self.result(kernel, greeting["sequence"], "interrupted", text=played)[0], 200)
        self.submit(kernel, "r1", "Go on")
        self.reply_for(kernel, "r1")
        context = " ".join(words(m) for m in self.provider.requests[-1]["messages"])
        self.assertIn(played, context)
        self.assertNotIn(greeting["text"], context)

    def test_no_proactive_reply_while_quiet_until_the_person_speaks_again(self):
        kernel = self.kernel(SAGE_GREET_AFTER_ABSENT_S="1", SAGE_NUDGE_AFTER_IDLE_S="1")
        self.submit(kernel, "r1", "Be quiet")
        self.result(kernel, self.reply_for(kernel, "r1")["sequence"], "done")
        self.arrive(kernel, 1.5)
        self.assertEqual(self.proactive(kernel, 3), [])  # neither a greeting nor a nudge
        self.submit(kernel, "r2", "Hello")  # speaking again ends quiet
        self.result(kernel, self.reply_for(kernel, "r2")["sequence"], "done")
        self.arrive(kernel, 1.5)
        self.assertTrue(self.proactive(kernel, 3), "quiet did not end when the person spoke again")

    def greetings_within(self, kernel, within_s, token=TOKENS["reachy"]):
        deadline, found = time.monotonic() + within_s, []
        while not found and time.monotonic() < deadline:
            found = self.proactive(kernel, 0.2, token)
        return found

    def test_the_first_seen_after_a_start_is_an_arrival_and_greets(self):
        kernel = self.kernel(SAGE_GREET_ON_START="1")  # absence threshold 3600 s: only "first since start" can greet
        self.assertEqual(self.presence(kernel, "seen")[0], 200)
        self.assertEqual(len(self.greetings_within(kernel, 5)), 1)

    def test_a_seen_below_half_confidence_has_no_effect(self):
        kernel = self.kernel(SAGE_GREET_ON_START="1")
        self.assertEqual(self.presence(kernel, "seen", confidence=0.4)[0], 200)
        self.assertEqual(self.proactive(kernel, 2), [])
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.presence(kernel, "seen", confidence=0.5)[0], 200)  # the first that counts
        self.assertEqual(len(self.greetings_within(kernel, 5)), 1)

    def test_quiet_survives_a_kernel_restart(self):
        kernel = self.kernel(SAGE_GREET_AFTER_ABSENT_S="1", SAGE_NUDGE_AFTER_IDLE_S="1")
        self.submit(kernel, "r1", "Be quiet")
        self.result(kernel, self.reply_for(kernel, "r1")["sequence"], "done")
        kernel.kill()
        kernel.start()
        self.hello(kernel)
        self.arrive(kernel, 1.5)  # the first seen after a start would greet, were it not quiet
        self.assertEqual(self.proactive(kernel, 3), [])

    def test_a_nudge_goes_to_the_device_that_most_recently_reported_seen(self):
        kernel = self.kernel(SAGE_NUDGE_AFTER_IDLE_S="1")  # greetings off
        self.presence(kernel, "seen")
        time.sleep(0.3)
        self.presence(kernel, "seen", TOKENS["mock"])
        self.assertEqual(len(self.greetings_within(kernel, 5, TOKENS["mock"])), 1, "no nudge reached mock")
        self.assertEqual(self.proactive(kernel, 0, TOKENS["reachy"]), [])

    def test_nudges_keep_their_minimum_gap_and_hourly_budget(self):
        kernel = self.kernel(SAGE_NUDGE_AFTER_IDLE_S="1", SAGE_NUDGE_GAP_S="3", SAGE_NUDGES_PER_HOUR="2")
        self.presence(kernel, "seen")
        arrived, deadline = [], time.monotonic() + 10  # unanswered, each nudge starts a new idle period
        while time.monotonic() < deadline:
            for nudge in self.proactive(kernel, 0.1)[len(arrived):]:
                arrived.append(time.monotonic())
                self.result(kernel, nudge["sequence"], "done")  # not acting, so the next may come
        self.assertEqual(len(arrived), 2, "expected 2 nudges in 10 s: one after 1 s idle, one after the 3 s gap")
        self.assertGreaterEqual(arrived[1] - arrived[0], 3 - 0.3)  # less polling slack

    def test_a_nudge_waits_out_the_engaged_window_after_the_person_spoke(self):
        kernel = self.kernel(SAGE_NUDGE_AFTER_IDLE_S="1", SAGE_NUDGE_GAP_S="1", SAGE_ENGAGED_S="3")
        spoke = time.monotonic()
        self.submit(kernel, "r1", "Hi")
        self.result(kernel, self.reply_for(kernel, "r1")["sequence"], "done")
        self.presence(kernel, "seen")  # idle would allow a nudge after 1 s; engaged holds it to 3 s
        nudges = self.greetings_within(kernel, 8)
        self.assertEqual(len(nudges), 1, "no nudge once the engaged window ended")
        self.assertGreaterEqual(time.monotonic() - spoke, 3 - 0.3, "a nudge came inside the engaged window")

    def test_silent_nudges_still_spend_the_budget(self):
        kernel = self.kernel(SAGE_NUDGE_AFTER_IDLE_S="1", SAGE_NUDGE_GAP_S="1", SAGE_NUDGES_PER_HOUR="2")
        self.provider.silent = True
        self.presence(kernel, "seen")
        self.assertEqual(self.proactive(kernel, 6), [])  # silence is never delivered as speech
        self.assertEqual(len(self.provider.requests), 2, "expected exactly 2 nudge attempts in 6 s")
        time.sleep(3)
        self.assertEqual(len(self.provider.requests), 2, "a third nudge was attempted past the budget")

    def test_at_most_one_greeting_per_arrival(self):
        kernel = self.kernel(SAGE_GREET_AFTER_ABSENT_S="1")
        self.arrive(kernel, 1.5)
        deadline = time.monotonic() + 5
        while not (greetings := self.proactive(kernel, 0.2)) and time.monotonic() < deadline:
            pass
        self.assertEqual(len(greetings), 1)
        self.result(kernel, greetings[0]["sequence"], "done")
        self.assertEqual(self.presence(kernel, "seen")[0], 200)  # a repeated seen is ignored, not refused
        self.arrive(kernel, 0.1)  # nor is a blink shorter than the absence threshold
        self.assertEqual(len(self.proactive(kernel, 3)), 1)

    # property: deliveries are a function of the journal ------------------------

    def test_pending_deliveries_depend_only_on_the_journal_at_every_restart_point(self):
        kernel = self.kernel(fresh_s="3600")
        seen = {}

        def reachy_feels_happy():
            self.submit(kernel, "r1", "Feel happy")
            seen["happy"] = self.reply_for(kernel, "r1")
            seen["emote"] = self.effect_for(kernel, "r1", "emote")

        def reachy_finishes_the_emote():
            self.assertEqual(self.result(kernel, seen["emote"]["sequence"], "done")[0], 200)

        def mock_talks():
            self.submit(kernel, "m1", "Hi", token=TOKENS["mock"])
            self.reply_for(kernel, "m1", token=TOKENS["mock"])

        def mock_says_stop():
            self.assertEqual(self.event(kernel, {"kind": "stop"}, TOKENS["mock"])[0], 200)

        def reachy_reports_it_was_cut_off():
            self.assertEqual(self.result(kernel, seen["happy"]["sequence"], "interrupted", text="Ya")[0], 200)

        def reachy_starts_a_turn_the_model_never_finishes():
            self.provider.open.clear()
            self.submit(kernel, "r2", "Count")
            time.sleep(0.3)

        def reachy_acknowledges_it():
            self.provider.open.set()
            self.assertEqual(self.event(kernel, {"kind": "acknowledged", "turn_id": "r2"})[0], 200)

        def reachy_talks_again():
            self.submit(kernel, "r3", "Hi again")
            self.reply_for(kernel, "r3")

        def deliveries():
            return {name: self.journaled(kernel, token) for name, token in TOKENS.items()}

        self.hello(kernel, [EMOTE])
        for step in (reachy_feels_happy, reachy_finishes_the_emote, mock_talks, mock_says_stop,
                     reachy_reports_it_was_cut_off, reachy_starts_a_turn_the_model_never_finishes,
                     reachy_acknowledges_it, reachy_talks_again):
            with self.subTest(restart_after=step.__name__):
                step()
                before = deliveries()
                kernel.kill()
                kernel.start()
                self.hello(kernel, [EMOTE])
                self.assertEqual(deliveries(), before)
        kinds = sorted(e["kind"] for e in deliveries()["reachy"])
        self.assertEqual(kinds, ["emote", "reply", "reply", "stop"])  # the walk was not vacuous


if __name__ == "__main__":
    unittest.main()
