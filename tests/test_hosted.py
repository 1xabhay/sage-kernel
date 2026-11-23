"""The hosted brain: only an addressed turn's prompt text leaves, a slow or absent network costs a bounded
wait and never the turn, and the key is never shown. Every network call here is to a fake on this machine."""

import json
import re
import socket
import struct
import threading
import time
import types
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sage import policy
from sage.adapters import http, model
from sage.policy import Companion, Exchange, Turn

EMOTE = {"name": "emote", "description": "Show a feeling.",
         "inputSchema": {"type": "object", "properties": {"name": {"enum": ["cheerful1"]}}}}
MANIFEST = {"senses": ("speech",), "tools": (EMOTE,)}
KEY = "sk-test-not-a-real-key-0123456789"


def prompt_for(line="What is the tallest mountain?", input=None, exchanges=()):
    turn = Turn("r1", "reachy", "active", line, input=input or {})
    return policy.compile_prompt(Companion(turn=turn, exchanges=exchanges), "reachy", MANIFEST)


def sse(*events):
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def text(piece):
    return {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}}


START = {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 4200, "output_tokens": 1}}}
END = [{"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 14}},
       {"type": "message_stop"}]


class FakeHosted:
    def __init__(self, events, status=200):
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append((self.path, self.headers, body))
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream" if status == 200 else "application/json")
                self.end_headers()
                self.wfile.write(sse(*events) if status == 200 else json.dumps(events).encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Hosted(unittest.TestCase):
    def fake(self, events, status=200):
        fake = FakeHosted(events, status)
        self.addCleanup(fake.close)
        return fake

    def test_only_the_prompt_s_text_is_sent_and_the_answer_streams_back(self):
        fake = self.fake([START, text('{"reply": "Mount '), text('Everest."}'), *END])
        before = (Exchange("Hello there", "Hi!", device="reachy"),)
        prompt, heard = prompt_for(exchanges=before), []
        answer = model.hosted(KEY, url=fake.url)(prompt, heard.append)
        self.assertEqual(answer, '{"reply": "Mount Everest."}')
        self.assertEqual(heard, ['{"reply": "Mount ', 'Everest."}'])
        path, headers, body = fake.requests[0]
        self.assertEqual((path, headers["x-api-key"], headers["anthropic-version"]), ("/v1/messages", KEY, "2026-10-21"))
        self.assertEqual((body["model"], body["stream"]), ("claude-haiku-5-5", True))
        self.assertTrue(body["system"].startswith(prompt.system))
        self.assertEqual([m["content"] for m in body["messages"]], [m["content"] for m in prompt.context])
        self.assertTrue(all(type(m["content"]) is str for m in body["messages"]), "text only: no audio, no images")
        self.assertEqual({m["role"] for m in body["messages"]}, {"user", "assistant"})
        self.assertEqual(body["messages"][-1]["role"], "user")
        self.assertEqual(set(body), {"model", "max_tokens", "stream", "system", "messages", "thinking", "output_config"})
        self.assertEqual((answer.timing["model"], answer.timing["prompt_tokens"], answer.timing["gen_tokens"]),
                         ("claude-haiku-5-5", 4200, 14))

    def test_the_answer_format_is_the_kernel_s_schema_without_what_the_vendor_refuses(self):
        fake = self.fake([START, text('{"reply": "Hi"}'), *END])
        prompt = prompt_for()
        model.hosted(KEY, url=fake.url)(prompt, lambda piece: None)
        body = fake.requests[0][2]
        format = body["output_config"]["format"]
        self.assertEqual(format["type"], "json_schema")
        schema = format["schema"]
        self.assertEqual((list(schema["properties"]), schema["required"]), (list(prompt.format["properties"]), ["reply"]))
        self.assertEqual(schema["properties"]["reply"], {"type": "string"})  # the cap is said in words instead
        self.assertIn(str(prompt.max_reply), body["system"][len(prompt.system):])
        self.assertEqual(schema["properties"]["emote"]["properties"]["name"], {"enum": ["cheerful1"]})
        self.assertIs(schema["additionalProperties"], False)
        self.assertIs(schema["properties"]["emote"]["additionalProperties"], False)

    def test_a_property_named_like_a_refused_keyword_is_kept(self):
        schema = {"type": "object", "properties": {"maximum": {"type": "integer", "maximum": 3}}}
        self.assertEqual(model.strict(schema), {"type": "object", "additionalProperties": False,
                                                "properties": {"maximum": {"type": "integer"}}})

    def test_a_refused_key_fails_without_showing_the_key(self):
        fake = self.fake({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}, 401)
        with self.assertRaises(OSError) as raised:
            model.hosted(KEY, url=fake.url)(prompt_for(), lambda piece: None)
        self.assertIn("authentication_error", str(raised.exception))
        self.assertNotIn(KEY, str(raised.exception) + repr(raised.exception))

    def test_a_refusal_or_a_stream_error_is_a_failure_not_an_answer(self):
        refusal = [START, {"type": "message_delta", "delta": {"stop_reason": "refusal"}, "usage": {}}, {"type": "message_stop"}]
        overloaded = [START, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}]
        for events in (refusal, overloaded):
            with self.subTest(events=events[1]["type"]), self.assertRaises(OSError):
                model.hosted(KEY, url=self.fake(events).url)(prompt_for(), lambda piece: None)

    def test_it_needs_a_key_and_https_off_this_machine(self):
        for key, url in (("", "https://api.anthropic.com"), (KEY, "http://api.example.com"),
                         (KEY, "https://user:pw@api.example.com")):
            with self.subTest(url=url, key=bool(key)), self.assertRaises(ValueError) as raised:
                model.hosted(key, url=url)
            self.assertNotIn(KEY, str(raised.exception))


def says(reply, wait=0.0, fail=None, after=0):
    """A scripted brain: waits, streams `reply` in two pieces, and may fail before piece number `after`."""
    def decide(prompt, heard):
        decide.calls += 1
        time.sleep(wait)
        pieces = [reply[:5], reply[5:]]
        for index, piece in enumerate(pieces):
            if fail and index == after:
                raise fail
            heard(piece)
        return model.Answer(reply)
    decide.calls = 0
    return decide


class Routing(unittest.TestCase):
    def setUp(self):
        self.local, self.marks = says('{"reply": "local"}'), model.Addressed()

    def route(self, hosted, deadline_s=0.2):
        return model.routed(self.local, hosted, self.marks, deadline_s)

    def test_a_line_not_marked_addressed_stays_on_this_machine(self):
        hosted = says('{"reply": "hosted"}')
        self.marks.mark("reachy", "Something overheard", False)
        self.assertEqual(self.route(hosted)(prompt_for("Something overheard"), lambda piece: None), '{"reply": "local"}')
        self.assertEqual(self.route(hosted)(prompt_for("Never marked at all"), lambda piece: None), '{"reply": "local"}')
        self.assertEqual(hosted.calls, 0)

    def test_an_addressed_line_goes_to_the_hosted_brain(self):
        hosted, heard = says('{"reply": "hosted"}'), []
        self.marks.mark("reachy", "What is the tallest mountain?", True)
        answer = self.route(hosted)(prompt_for(), heard.append)
        self.assertEqual((answer, "".join(heard), self.local.calls), ('{"reply": "hosted"}', '{"reply": "hosted"}', 0))

    def test_a_hosted_brain_with_no_first_piece_by_the_deadline_loses_the_turn_to_the_local_one(self):
        hosted, heard = says('{"reply": "hosted"}', wait=1.0), []
        self.marks.mark("reachy", "What is the tallest mountain?", True)
        began = time.monotonic()
        answer = self.route(hosted, deadline_s=0.1)(prompt_for(), heard.append)
        self.assertLess(time.monotonic() - began, 0.6)
        self.assertEqual((answer, "".join(heard)), ('{"reply": "local"}', '{"reply": "local"}'))
        self.assertEqual(answer.timing["fallback"], "TimeoutError")
        time.sleep(1.1)  # the late answer arrives, and none of it is heard
        self.assertEqual("".join(heard), '{"reply": "local"}')

    def test_an_absent_network_is_answered_locally(self):
        hosted = says("", fail=OSError("Network is unreachable"))
        self.marks.mark("reachy", "What is the tallest mountain?", True)
        answer = self.route(hosted)(prompt_for(), lambda piece: None)
        self.assertEqual((answer, answer.timing["fallback"]), ('{"reply": "local"}', "OSError"))

    def test_a_failure_after_words_were_heard_is_not_papered_over_with_other_words(self):
        hosted, heard = says('{"reply": "hosted"}', fail=OSError("reset"), after=1), []
        self.marks.mark("reachy", "What is the tallest mountain?", True)
        with self.assertRaises(OSError):
            self.route(hosted)(prompt_for(), heard.append)
        self.assertEqual((heard, self.local.calls), (['{"rep'], 0))

    def test_the_kernel_ending_the_call_ends_it_with_no_local_call(self):
        class Abandoned(Exception):
            pass

        def heard(piece):
            raise Abandoned
        self.marks.mark("reachy", "What is the tallest mountain?", True)
        with self.assertRaises(Abandoned):
            self.route(says('{"reply": "hosted"}'))(prompt_for(), heard)
        self.assertEqual(self.local.calls, 0)


class Marks(unittest.TestCase):
    def test_the_mark_follows_the_line_through_its_framing_and_a_retry(self):
        marks = model.Addressed()
        marks.mark("reachy", "wats the tallest", True)
        unsure = {"medium": "speech", "confidence": 0.2, "alternatives": ["what's the tallest"], "when": "Mon 10:00"}
        prompt = prompt_for("wats the tallest", unsure)
        self.assertIn("(unclear)", prompt.context[-1]["content"])
        self.assertTrue(marks(prompt))
        self.assertTrue(marks(policy.retry_prompt(prompt, "[Answer again, in the schema.]")))

    def test_the_device_s_next_line_replaces_the_mark(self):
        marks = model.Addressed()
        marks.mark("reachy", "Tell me a joke", True)
        marks.mark("reachy", "Tell me a joke", False)  # the same words, overheard this time
        self.assertFalse(marks(prompt_for("Tell me a joke")))

    def test_a_mark_does_not_outlive_its_turn_or_match_another_line(self):
        now = [100.0]
        marks = model.Addressed(clock=lambda: now[0])
        marks.mark("reachy", "Hi", True)
        self.assertFalse(marks(prompt_for("This line has hi inside it")))
        self.assertTrue(marks(prompt_for("Hi")))
        now[0] += model.ADDRESSED_S + 1
        self.assertFalse(marks(prompt_for("Hi")))


class Settings(unittest.TestCase):
    def test_hosted_is_off_unless_asked_for(self):
        local = says("{}")
        self.assertEqual(model.from_env({"ANTHROPIC_API_KEY": KEY}, local), (local, None))

    def test_asked_for_without_a_key_stops_the_start(self):
        with self.assertRaises(ValueError):
            model.from_env({"SAGE_HOSTED": "anthropic"}, says("{}"))
        with self.assertRaises(ValueError):
            model.from_env({"SAGE_HOSTED": "somewhere", "ANTHROPIC_API_KEY": KEY}, says("{}"))

    def test_asked_for_with_a_key_routes_marked_lines(self):
        decide, marks = model.from_env({"SAGE_HOSTED": "anthropic", "ANTHROPIC_API_KEY": KEY}, says('{"reply": "local"}'))
        self.assertIsInstance(marks, model.Addressed)
        self.assertEqual(decide(prompt_for("Not marked"), lambda piece: None), '{"reply": "local"}')


class Header(unittest.TestCase):
    """The device marks a submitted line as addressed with a request header: `Sage-Addressed: 1`."""

    def post(self, port, token, event, headers=()):
        request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/events", json.dumps(event).encode(),
                                         {"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                                          **dict(headers)})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_the_header_marks_the_line_and_the_event_reaches_the_kernel_unchanged(self):
        token, events, marks = "t" * 32, [], model.Addressed()
        kernel = types.SimpleNamespace(manifests={"reachy": {}},
                                       event=lambda source, data, **marks: events.append(data) or {"sequence": 1})
        server = http.serve(kernel, http.Devices(f"reachy={token}"), "127.0.0.1", 0, addressed=marks)
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        event = {"kind": "submitted", "turn_id": "r1", "text": "What is the tallest mountain?"}
        self.post(server.server_port, token, event, {"Sage-Addressed": "1"}.items())
        self.assertEqual(events, [event])
        self.assertTrue(marks(prompt_for()))
        self.post(server.server_port, token, {**event, "turn_id": "r2"})  # no header: not addressed
        self.assertFalse(marks(prompt_for()))


class Wire:
    """A hosted endpoint that answers every request with `wire`, byte for byte, then closes the connection,
    resets it, or goes silent."""

    def __init__(self, wire: bytes, then="close"):
        self.listener, self.done = socket.create_server(("127.0.0.1", 0)), threading.Event()
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}"

        def answer(connection):
            with connection:
                asked = b""
                while b"\r\n\r\n" not in asked:
                    asked += connection.recv(65536)
                head, _, body = asked.partition(b"\r\n\r\n")
                length = int(re.search(rb"content-length: *(\d+)", head.lower()).group(1))
                while len(body) < length:
                    body += connection.recv(65536)
                connection.sendall(wire)
                if then == "reset":
                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                elif then == "silent":
                    self.done.wait(5)

        def serve():
            while True:
                try:
                    connection, _ = self.listener.accept()
                except OSError:
                    return  # closed
                threading.Thread(target=answer, args=(connection,), daemon=True).start()
        threading.Thread(target=serve, daemon=True).start()

    def close(self):
        self.done.set()
        self.listener.close()


def chunk(data: bytes) -> bytes:
    return b"%x\r\n%s\r\n" % (len(data), data)


CHUNKED = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
PLAIN = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"
WORD = sse(text('{"reply": "Mount '))
# What a hosted stream can do instead of answering: the name, the bytes on the wire, and how they end.
BROKEN = (
    ("cut inside a chunk", CHUNKED + chunk(sse(START)) + b"400\r\nevent: content_block_delta\n", "close"),
    ("cut between chunks", CHUNKED + chunk(sse(START)), "close"),
    ("ended with no message_stop", PLAIN + sse(START), "close"),
    ("an empty body", PLAIN, "close"),
    ("a page that is no stream", PLAIN + b"<html>Bad gateway</html>\n", "close"),
    ("closed before any status line", b"", "close"),
    ("no HTTP at all", b"what is this\r\n\r\n", "close"),
    ("reset", PLAIN + sse(START), "reset"),
    ("silent mid-read", PLAIN + sse(START), "silent"),
    ("a line that is not JSON", PLAIN + sse(START) + b'data: {"type": "content_blo\n\n', "silent"),
    ("a line that is not an object", PLAIN + sse(START) + b"data: [1, 2]\n\n", "silent"),
    ("a null line", PLAIN + b"data: null\n\n", "silent"),
    ("a delta with no delta", PLAIN + sse(START, {"type": "content_block_delta", "index": 0}), "silent"),
    ("a delta that is null", PLAIN + sse(START, {"type": "content_block_delta", "index": 0, "delta": None}), "silent"),
    ("a delta with no text", PLAIN + sse(START, {"type": "content_block_delta", "delta": {"type": "text_delta"}}), "silent"),
    ("a start that is no message", PLAIN + sse({"type": "message_start", "message": "hello"}), "silent"),
    ("bytes that are no text", PLAIN + b"data: \xff\xfe\n\n", "silent"),
    ("the vendor's error event", PLAIN + sse(START, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}), "silent"),
    ("an error event with no error", PLAIN + sse(START, {"type": "error", "error": None}), "silent"),
    ("a refusal", PLAIN + sse(START, {"type": "message_delta", "delta": {"stop_reason": "refusal"}, "usage": {}}, {"type": "message_stop"}), "close"),
    ("the vendor overloaded", b"HTTP/1.1 529 Overloaded\r\nContent-Length: 0\r\n\r\n", "close"),
)


class CutStreams(unittest.TestCase):
    """A hosted stream cut or malformed before the first word costs a bounded wait, and the local model answers."""

    def hosted(self, wire, then):
        fake = Wire(wire, then)
        self.addCleanup(fake.close)
        return model.hosted(KEY, url=fake.url, stall_s=0.3)

    def test_every_failure_before_the_first_word_is_an_oserror_and_the_local_model_answers(self):
        for name, wire, then in BROKEN:
            with self.subTest(name):
                hosted, local, marks, heard = self.hosted(wire, then), says('{"reply": "local"}'), model.Addressed(), []
                with self.assertRaises(OSError) as raised:
                    hosted(prompt_for(), heard.append)
                self.assertNotIn(KEY, str(raised.exception))
                marks.mark("reachy", "What is the tallest mountain?", True)
                began = time.monotonic()
                answer = model.routed(local, hosted, marks, deadline_s=5)(prompt_for(), heard.append)
                self.assertEqual((answer, "".join(heard), local.calls), ('{"reply": "local"}', '{"reply": "local"}', 1))
                self.assertIn("fallback", answer.timing)
                self.assertLess(time.monotonic() - began, 2)

    def test_a_stream_cut_after_a_word_fails_the_turn_as_an_oserror_with_no_local_words(self):
        cuts = (("cut inside a chunk", CHUNKED + chunk(sse(START) + WORD) + b"400\r\nevent: content", "close"),
                ("ended with no message_stop", PLAIN + sse(START) + WORD, "close"),
                ("a line that is not an object", PLAIN + sse(START) + WORD + b"data: 7\n\n", "silent"),
                ("silent mid-read", PLAIN + sse(START) + WORD, "silent"))
        for name, wire, then in cuts:
            with self.subTest(name):
                local, marks, heard = says('{"reply": "local"}'), model.Addressed(), []
                marks.mark("reachy", "What is the tallest mountain?", True)
                with self.assertRaises(OSError):
                    model.routed(local, self.hosted(wire, then), marks, deadline_s=5)(prompt_for(), heard.append)
                self.assertEqual((heard, local.calls), (['{"reply": "Mount '], 0))

    def test_what_the_kernel_raises_while_hearing_a_piece_is_not_taken_for_a_bad_stream(self):
        class Abandoned(Exception):
            pass

        def heard(piece):
            raise Abandoned
        with self.assertRaises(Abandoned):
            self.hosted(PLAIN + sse(START, text('{"reply": "Hi"}'), *END), "close")(prompt_for(), heard)


if __name__ == "__main__":
    unittest.main()
