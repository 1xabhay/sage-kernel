"""Only the owner barges in, once the STT has their voiceprint (owner.py)."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from reachy_sage_voice import owner


class FakeOwner:
    def __init__(self, answer=True):
        self.answer, self.heard = answer, []

    def is_owner(self, pcm):
        self.heard.append(len(pcm))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def onsets(check, delay_s=0.0):
    fired, slept = [], []

    async def on_onset():
        fired.append(True)

    async def sleep(seconds):
        slept.append(seconds)

    async def go():
        handler = owner.verified_onset(on_onset, check, lambda s: b"\x00\x00" * int(16000 * s), sleep)
        assert handler() is None  # never waits on the audio path
        await asyncio.sleep(0.05)
    asyncio.run(go())
    return fired, slept


def test_the_owner_barges_in_after_their_voice_is_checked():
    check = FakeOwner(True)
    fired, slept = onsets(check)
    assert fired == [True] and slept == [owner.VERIFY_S]
    assert check.heard == [2 * int(16000 * (owner.VERIFY_S + owner.BEFORE_S))]


def test_anyone_else_or_music_does_not():
    assert onsets(FakeOwner(False))[0] == []


def test_an_stt_that_cannot_check_lets_the_barge_in_through():
    assert onsets(FakeOwner(OSError("refused")))[0] == [True]


class Stt(BaseHTTPRequestHandler):
    seen: list = []
    enrolled = True

    def answer(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.answer({"ready": True, **({"enrolled": True} if Stt.enrolled else {})})

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Stt.seen.append((self.path, self.headers["Content-Type"], body))
        self.answer({"owner": False, "similarity": 0.12})

    def log_message(self, *args):
        pass


def test_the_check_asks_the_stt_whether_a_voiceprint_is_enrolled_and_whose_voice_it_is():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stt)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        check = owner.OwnerCheck(f"http://127.0.0.1:{server.server_port}/v1")
        assert check.enrolled() is True
        assert check.is_owner(b"\x00\x00" * 160) is False
        Stt.enrolled = False
        assert check.enrolled() is False  # an older STT says nothing: no voiceprint
    finally:
        server.shutdown()
    path, kind, body = Stt.seen[0]
    assert path == "/v1/speaker/verify" and kind.startswith("multipart/form-data")
    assert b'name="file"' in body and b"RIFF" in body
