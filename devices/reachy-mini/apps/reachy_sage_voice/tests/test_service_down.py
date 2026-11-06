"""A local service down, then back (R3): the voice says or shows so, never exits, and recovers.

Each service is a closed loopback port first, then a fake server on that same port.
"""

import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.tests.utils import SleepFrame, run_test
from sage_device.kernel import KernelClient, KernelUnavailableError, Session
from test_pipeline import MOVES, FakeMedia

from reachy_sage_voice import pipeline as voice_pipeline
from reachy_sage_voice import song
from reachy_sage_voice.song import ResearchClient, SongListener, wav
from reachy_sage_voice.tts import DOWN_EMOTE, KokoroTTSService

from conftest import TOKEN


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def serve(port: int, routes: dict):
    """Answer each path in ``routes`` 200 with its JSON (or bytes); anything else 404."""
    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = routes.get(self.path.split("?")[0])
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _serve  # noqa: N815 - http.server names them

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def test_stt_down_says_it_cannot_hear_within_3_s_once_then_hears_again_when_back():
    port = free_port()
    stt = voice_pipeline.SegmentSTTService(
        api_key="local", base_url=f"http://127.0.0.1:{port}/v1",
        settings=OpenAISTTService.Settings(model="parakeet"))
    segment = wav(b"\x00\x01" * 1600)

    async def go():
        async def transcribe():
            return [f async for f in stt.run_stt(segment)]
        began = time.monotonic()
        first = await transcribe()
        took = time.monotonic() - began
        again = await transcribe()  # still down: not said twice in a row
        with serve(port, {"/v1/audio/transcriptions": {"text": "hello"}}):
            back = await transcribe()
        return first, took, again, back

    first, took, again, back = asyncio.run(go())
    assert [f.text for f in first if isinstance(f, TTSSpeakFrame)] == [voice_pipeline.CANT_HEAR]
    assert took < 3
    assert not [f for f in again if isinstance(f, TTSSpeakFrame)]
    assert [f.text for f in back if isinstance(f, TranscriptionFrame)] == ["hello"]


def test_tts_down_shows_an_emote_instead_once_a_reply_then_speaks_when_back():
    port, gestures, back = free_port(), [], []

    def speak(*lines, on_down):
        frames = [f for line in lines for f in (TTSSpeakFrame(line), SleepFrame(0.1))]
        tts = KokoroTTSService(base_url=f"http://127.0.0.1:{port}/v1", on_down=on_down)
        down, _ = asyncio.run(run_test(tts, frames_to_send=frames))
        return [f for f in down if isinstance(f, TTSAudioRawFrame)]

    async def gesture():
        gestures.append("down")
    assert speak("Hello there.", "Bye.", on_down=gesture) == []
    assert gestures == ["down"]  # one emote for the reply, not one per clause

    async def comes_back():  # the service returns once the first line has failed
        gestures.append("back")
        back.append(serve(port, {"/v1/audio/speech": b"\x00\x01" * 2400}))
        back[0].__enter__()
    try:
        assert speak("Hello?", "Back again.", on_down=comes_back)
    finally:
        for server in back:
            server.__exit__(None, None, None)
    assert gestures == ["down", "back"]


def test_research_down_says_it_cannot_look_up_then_looks_up_when_back():
    port = free_port()

    async def no_wait(_):
        pass

    def listen():
        said = []

        async def say(line):
            said.append(line)

        async def notes(topic, passages):
            pass
        listener = SongListener(ResearchClient(f"http://127.0.0.1:{port}", timeout_s=2),
                                lambda s: b"\x00\x00" * 160, sleep=no_wait)
        return asyncio.run(listener.listen(say, notes)), said

    assert listen() == (("failed", "research_unreachable"), [song.UNREACHABLE])
    with serve(port, {"/identify": {"found": True, "title": "Pulse", "artist": "Mira Vale"},
                      "/research": {"topic": "Pulse", "passages": []}}):
        assert listen()[0] == ("done", None)


def test_kernel_down_is_unavailable_then_a_turn_is_admitted_when_back():
    # Down, SageProcessor says OFFLINE_LINE (test_sage_processor); busy, "One moment."
    port = free_port()
    session = Session(KernelClient(f"http://127.0.0.1:{port}", TOKEN, timeout_s=2), {"tools": []})
    with pytest.raises(KernelUnavailableError):
        asyncio.run(session.start("t1", "hi", {}))
    with serve(port, {"/v1/hello": {"protocol": 1, "sequence": 7}, "/v1/events": {}}):
        assert asyncio.run(session.start("t2", "hi", {})) == "admitted"


def test_stt_and_tts_both_down_the_built_voice_shows_the_emote_and_keeps_running():
    stt_port, tts_port, played = free_port(), free_port(), []

    async def play_emote(name, sound):
        played.append((name, sound))
    env = {"SAGE_VOICE_KERNEL_URL": f"http://127.0.0.1:{free_port()}",
           "SAGE_VOICE_KERNEL_TOKEN": TOKEN,
           "SAGE_VOICE_STT_URL": f"http://127.0.0.1:{stt_port}",
           "SAGE_VOICE_TTS_URL": f"http://127.0.0.1:{tts_port}"}
    built = voice_pipeline.build(FakeMedia(), play_emote, MOVES, env).processors
    stt = next(p for p in built if isinstance(p, OpenAISTTService))
    tts = next(p for p in built if isinstance(p, KokoroTTSService))
    audio = InputAudioRawFrame(b"\x00\x01" * 1600, sample_rate=16000, num_channels=1)
    asyncio.run(run_test(Pipeline([stt, tts]), frames_to_send=[
        VADUserStartedSpeakingFrame(), audio, audio, VADUserStoppedSpeakingFrame(),
        SleepFrame(3.0)]))
    assert played == [(DOWN_EMOTE, True)]
