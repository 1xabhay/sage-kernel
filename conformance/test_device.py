"""Device protocol v1 conformance, from the other side: a body, against this repo's kernel.

`SAGE_DEVICE_CMD` picks the body under test (default: the text body, `python -m sage_device.text`).
The suite starts it with `SAGE_KERNEL_URL` and `SAGE_TOKEN` in its environment, writes what a person
says to its stdin, one line each, and reads what the body shows them from its stdout. A body with
no stdin of its own (a web page, a robot) is wrapped in a command that does this for it. A page in a
browser also needs `SAGE_ORIGINS` set to its origin when the suite is run. The kernel
is real and thinks with the fake model of test_conformance.py; every request the body makes passes
a recorder on its way there, so the kernel itself judges each one. See docs/PROTOCOL.md.
"""

import json
import os
import queue
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_conformance
from test_conformance import ROOT, TOKENS, KernelProcess, Provider

BODY = ROOT / "devices" / "common" / "sage_device"
COMMAND = shlex.split(os.environ.get("SAGE_DEVICE_CMD", f"{sys.executable} -m sage_device.text"))
TOKEN = TOKENS["phone"]


def minimal(schema: dict):
    """The smallest input a tool's inputSchema takes: required fields only, first choices."""
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        return {name: minimal(schema.get("properties", {}).get(name, {})) for name in schema.get("required", ())}
    return {"string": "x", "integer": 1, "number": 1, "boolean": True, "array": []}.get(kind)


class Recorder:
    """Passes each request on to the kernel and keeps what was asked and what was answered."""

    def __init__(self, kernel: KernelProcess):
        self.seen, recorder = [], self

        class Handler(BaseHTTPRequestHandler):
            def relay(self):
                data = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or None
                headers = {name: self.headers[name] for name in self.headers  # a page's Origin and preflight too
                           if name.lower() in ("authorization", "content-type", "origin")
                           or name.lower().startswith("access-control-request-")}
                request = urllib.request.Request(f"http://127.0.0.1:{kernel.device_port}{self.path}", data, headers,
                                                 method=self.command)
                try:
                    with urllib.request.urlopen(request, timeout=40) as response:
                        status, answer, told = response.status, response.read(), response.headers
                except urllib.error.HTTPError as error:
                    with error:
                        status, answer, told = error.code, error.read(), error.headers
                except (urllib.error.URLError, ConnectionError):  # the kernel is down: so is this
                    self.close_connection = True
                    return
                if self.command != "OPTIONS":  # a browser's own question, not the body's request
                    recorder.seen.append({"method": self.command, "path": self.path, "status": status,
                                          "token": self.headers.get("Authorization", ""),
                                          "sent": json.loads(data) if data else None,
                                          "got": json.loads(answer or b"null")})
                self.send_response(status)
                for name, value in told.items():
                    if name.lower().startswith("access-control-") or name.lower() == "vary":
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            do_GET = do_POST = do_OPTIONS = relay

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def events(self, kind):
        return [r for r in self.seen if r["path"] == "/v1/events" and r["sent"]["kind"] == kind]


@unittest.skipUnless("SAGE_DEVICE_CMD" in os.environ or BODY.is_dir(), "no body to test: set SAGE_DEVICE_CMD")
class Device(unittest.TestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.provider = Provider()
        plain = test_conformance.answer

        def answer(messages):  # asked to, the fake model calls the first tool the body under test declared
            tools = self.recorder.seen[0]["sent"]["manifest"].get("tools", ()) if self.recorder.seen else ()
            if "Use your first tool" in test_conformance.words(messages[-1]) and tools:
                return "", {tools[0]["name"]: minimal(tools[0]["inputSchema"])}
            return plain(messages)
        test_conformance.answer = answer
        self.addCleanup(setattr, test_conformance, "answer", plain)
        self.kernel = KernelProcess(state.name, self.provider).start()
        self.addCleanup(lambda: self.kernel.stop())
        self.recorder = Recorder(self.kernel)
        self.addCleanup(self.recorder.server.shutdown)
        self.shown = queue.Queue()

    def body(self):
        env = {**os.environ, "SAGE_KERNEL_URL": self.recorder.url, "SAGE_TOKEN": TOKEN,
               "PYTHONPATH": os.pathsep.join(filter(None, [str(BODY), os.environ.get("PYTHONPATH")])),
               "PYTHONUNBUFFERED": "1"}
        body = subprocess.Popen(COMMAND, cwd=ROOT, env=env, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.addCleanup(body.wait, 5)
        self.addCleanup(body.kill)
        self.addCleanup(body.stdout.close)
        self.addCleanup(body.stdin.close)
        threading.Thread(target=lambda: [self.shown.put(line) for line in body.stdout], daemon=True).start()
        return body

    def say(self, body, line):
        body.stdin.write(line + "\n")
        body.stdin.flush()

    def shows(self, text, within=15):
        """Everything the body showed up to and including `text`; fails if it never shows it."""
        deadline, shown = time.monotonic() + within, ""
        while text not in shown:
            try:
                shown += self.shown.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                self.fail(f"the body never showed {text!r}; it showed {shown!r}")
        return shown

    def until(self, check, within=10):
        deadline = time.monotonic() + within
        while not check() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(check())

    def talked(self):
        """A body that was told one line and showed the answer."""
        body = self.body()
        self.say(body, "Is the tea ready yet?")
        self.shows("Heard: Is the tea ready yet?")
        return body

    def test_the_first_thing_a_body_does_is_say_hello_with_a_manifest_the_kernel_accepts(self):
        self.talked()
        first = self.recorder.seen[0]
        self.assertEqual((first["method"], first["path"], first["status"]), ("POST", "/v1/hello", 200))
        self.assertIn(1, first["sent"]["protocols"])

    def test_every_request_carries_the_body_s_token(self):
        self.talked()
        self.assertEqual({r["token"] for r in self.recorder.seen}, {f"Bearer {TOKEN}"})

    def test_nothing_a_body_sends_is_refused(self):
        self.talked()
        self.until(lambda: self.recorder.events("result"))
        self.assertEqual([(r["path"], r["sent"], r["got"]) for r in self.recorder.seen if r["status"] != 200], [])

    def test_a_line_is_submitted_once_under_its_own_turn_id(self):
        self.talked()
        (turn,) = self.recorder.events("submitted")
        self.assertEqual(turn["sent"]["text"], "Is the tea ready yet?")
        self.assertRegex(turn["sent"]["turn_id"], r"\S{8,}")

    def test_a_reply_it_showed_gets_one_done_result_naming_the_reply(self):
        self.talked()
        self.until(lambda: self.recorder.events("result"))
        delivered = [e["sequence"] for r in self.recorder.seen for e in (r["got"] or {}).get("effects", ())
                     if e["kind"] == "reply"]
        self.assertEqual([(r["sent"].get("sequence"), r["sent"]["status"]) for r in self.recorder.events("result")],
                         [(delivered[0], "done")])

    def test_a_body_never_asks_for_what_was_said_before_it_arrived(self):
        self.kernel.device("POST", "/v1/hello", {"protocols": [1], "manifest": {"senses": ["speech"], "tools": []}})
        self.kernel.device("POST", "/v1/events", {"kind": "submitted", "turn_id": "earlier-turn-1", "text": "Count"})
        self.until(lambda: self.kernel.device("GET", "/v1/effects?since=0")[1]["effects"])
        body = self.body()
        self.say(body, "Is the tea ready yet?")
        shown = self.shows("Heard: Is the tea ready yet?")
        head = self.recorder.seen[0]["got"]["sequence"]
        polls = [r["path"] for r in self.recorder.seen if r["path"].startswith("/v1/effects")]
        self.assertTrue(polls and all(int(p.split("since=")[1].split("&")[0]) >= head for p in polls), polls)
        self.assertNotIn("One two three", shown)

    def test_a_body_that_puts_out_text_says_its_lines_were_typed(self):
        self.talked()
        if self.recorder.seen[0]["sent"]["manifest"].get("output", {}).get("medium") != "text":
            self.skipTest("this body speaks")
        self.assertEqual(self.recorder.events("submitted")[0]["sent"]["input"], {"medium": "text"})

    def test_a_tool_the_body_declared_gets_one_result_when_the_model_uses_it(self):
        body = self.talked()
        tools = [tool["name"] for tool in self.recorder.seen[0]["sent"]["manifest"].get("tools", ())]
        if not tools:
            self.skipTest("this body declared no tools")
        self.say(body, "Use your first tool, please.")

        def used():
            return [e for r in self.recorder.seen for e in (r["got"] or {}).get("effects", ()) if e["kind"] in tools]
        self.until(used)
        self.until(lambda: [r for r in self.recorder.events("result") if r["sent"].get("sequence") == used()[0]["sequence"]])
        (result,) = [r for r in self.recorder.events("result") if r["sent"].get("sequence") == used()[0]["sequence"]]
        self.assertEqual(result["status"], 200)
        self.assertIn(result["sent"]["status"], ("done", "failed", "interrupted"))

    def test_a_page_s_preflight_and_permission_pass_through_the_recorder(self):
        """The suite's own plumbing: a body in a browser is answered through the recorder as by the kernel."""
        self.kernel.stop()
        self.kernel = KernelProcess(self.kernel.env["SAGE_STATE_DIR"], self.provider,
                                    SAGE_ORIGINS="https://notebook.example").start()
        self.recorder = Recorder(self.kernel)
        self.addCleanup(self.recorder.server.shutdown)
        asked = urllib.request.Request(self.recorder.url + "/v1/hello", method="OPTIONS", headers={
            "Origin": "https://notebook.example", "Access-Control-Request-Method": "POST"})
        with urllib.request.urlopen(asked, timeout=10) as answer:
            self.assertEqual((answer.status, answer.headers["Access-Control-Allow-Origin"]),
                             (204, "https://notebook.example"))
        self.assertEqual(self.recorder.seen, [])

    def test_after_the_kernel_restarts_a_body_says_hello_again_and_the_next_line_is_answered(self):
        body = self.talked()
        self.until(lambda: self.recorder.events("result"))
        self.kernel.stop()
        self.kernel.start()
        before = len(self.recorder.seen)
        self.say(body, "Did the post come today?")
        self.shows("Heard: Did the post come today?", 40)
        later = self.recorder.seen[before:]
        self.assertIn(("/v1/hello", 200), [(r["path"], r["status"]) for r in later])


if __name__ == "__main__":
    unittest.main()
