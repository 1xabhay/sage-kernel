"""Listen to a song, look it up, and hand the kernel what was found (the listen_to_song tool).

The research service (services/research) is faked: ``SongListener`` is tested
against a client double, ``ResearchClient`` against a local HTTP server.
"""

import asyncio
import io
import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from pipecat.frames.frames import InterruptionFrame
from pipecat.tests.utils import SleepFrame
from test_sage_processor import TURN, FakeSession, heard, make, reply, run, spoken

from reachy_sage_voice import song
from reachy_sage_voice.manifest import manifest
from reachy_sage_voice.sage import Playback
from reachy_sage_voice.transport import ReachyInputTransport

FOUND = {"found": True, "title": "Pulse", "artist": "Mira Vale", "album": "Night Glass",
         "year": "2026", "genre": "Pop"}
NOTES = {"topic": '"Pulse" by Mira Vale', "passages": ["Pulse: a 2026 single.", "MusicBrainz: ..."]}


class FakeResearch:
    def __init__(self, found=FOUND, notes=NOTES, fail=None):
        self.found, self.notes, self.fail = found, notes, fail
        self.identified: list[bytes] = []
        self.researched: list[tuple[str, str]] = []

    def identify(self, wav):
        if self.fail == "identify":
            raise OSError("refused")
        self.identified.append(wav)
        return self.found

    def research(self, title, artist):
        if self.fail == "research":
            raise OSError("refused")
        self.researched.append((title, artist))
        return self.notes


def listen(client, snippets=None, music_present=None):
    said, sent, busy = [], [], []

    async def say(line):
        said.append(line)

    async def notes(topic, passages):
        sent.append((topic, passages))

    def snippet(seconds):
        busy.append(listener.busy)
        (snippets if snippets is not None else []).append(seconds)
        return b"\x00\x01" * int(16000 * seconds)

    async def no_wait(_):
        pass

    listener = song.SongListener(client, snippet, sleep=no_wait, music_present=music_present)
    outcome = asyncio.run(listener.listen(say, notes))
    return outcome, said, sent, busy, listener


def test_with_no_music_heard_it_says_so_and_sends_nothing_to_be_named():
    heard = []
    client = FakeResearch()
    outcome, said, sent, _, listener = listen(client, music_present=lambda pcm: heard.append(pcm))
    assert outcome == ("failed", "no_music")
    assert said == [song.NO_MUSIC] and client.identified == [] and sent == [] and not listener.busy
    assert len(heard[0]) == 2 * 16000 * song.SNIPPET_S  # it judged the snippet itself


def test_with_music_heard_it_goes_on_to_name_it():
    outcome, *_ = listen(FakeResearch(), music_present=lambda pcm: True)
    assert outcome == ("done", None)


def test_it_listens_names_the_song_reads_up_then_hands_the_kernel_the_notes():
    client, asked = FakeResearch(), []
    outcome, said, sent, busy, listener = listen(client, asked)
    assert outcome == ("done", None)
    assert asked == [song.SNIPPET_S]
    assert busy == [True] and not listener.busy
    assert client.researched == [("Pulse", "Mira Vale")]
    assert sent == [(NOTES["topic"], NOTES["passages"])]
    assert said == ["That's Pulse by Mira Vale. Let me read up on it.", song.READY]
    with wave.open(io.BytesIO(client.identified[0])) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
        assert w.getnframes() == 16000 * song.SNIPPET_S


def test_a_song_it_cannot_name_is_said_and_nothing_is_sent():
    outcome, said, sent, _, listener = listen(FakeResearch(found={"found": False}))
    assert outcome == ("failed", "not_recognized")
    assert said == [song.NOT_FOUND] and sent == [] and not listener.busy


def test_a_research_service_that_is_down_is_said_and_frees_the_body():
    for where in ("identify", "research"):
        outcome, said, sent, _, listener = listen(FakeResearch(fail=where))
        assert outcome == ("failed", "research_unreachable")
        assert said[-1] == song.UNREACHABLE and sent == [] and not listener.busy


def test_the_tool_is_declared_only_when_a_research_service_is_configured():
    class Move:
        duration, description = 2.0, "A move."
    moves = {"yes1": Move()}
    assert "listen_to_song" not in [t["name"] for t in manifest(moves)["tools"]]
    tools = {t["name"]: t for t in manifest(moves, song=True)["tools"]}
    assert tools["listen_to_song"] == song.TOOL
    assert song.TOOL["inputSchema"] == {"type": "object", "properties": {}}


def test_the_mic_keeps_the_last_seconds_for_a_snippet():
    mic = ReachyInputTransport.__new__(ReachyInputTransport)
    mic._heard = bytearray()
    for n in range(20):  # 20 s of one-second chunks, each sample its second's number
        mic._keep(np.full(16000, n, dtype=np.int16).tobytes(), 16000)
    pcm = np.frombuffer(mic.snippet(10, 16000), dtype=np.int16)
    assert len(pcm) == 16000 * 10 and pcm[0] == 10 and pcm[-1] == 19
    assert len(mic._heard) <= 2 * 16000 * song.KEEP_S


class Research(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Research.seen.append((self.path, self.headers["Content-Type"], body))
        answer = FOUND if self.path == "/identify" else NOTES
        data = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def test_the_client_posts_the_snippet_as_a_file_and_the_song_as_json():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Research)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = song.ResearchClient(f"http://127.0.0.1:{server.server_port}/")
        assert client.identify(b"RIFFfake") == FOUND
        assert client.research("Pulse", "Mira Vale") == NOTES
    finally:
        server.shutdown()
    (path, kind, body), (path2, kind2, body2) = Research.seen
    assert path == "/identify" and kind.startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="snippet.wav"' in body and b"RIFFfake" in body
    assert (path2, kind2) == ("/research", "application/json")
    assert json.loads(body2) == {"title": "Pulse", "artist": "Mira Vale"}


# --- inside the processor ------------------------------------------------------

def listen_effect(sequence):
    return {"sequence": sequence, "kind": "listen_to_song", "turn_id": TURN, "input": {},
            "stale": False}


class Idle(Playback):
    """No TTS in these tests: the reply is played at once."""

    async def idle(self):
        pass


class FakeListener:
    def __init__(self, release=None):
        self.busy, self.started = False, asyncio.Event()
        self.release = release  # listening lasts until this is set; None: 0.2 s

    async def listen(self, say, notes):
        self.busy = True
        self.started.set()
        await say("That's Pulse by Mira Vale. Let me read up on it.")
        await notes("Pulse", ["One."])
        await (self.release.wait() if self.release else asyncio.sleep(0.2))
        self.busy = False
        return "done", None


def until(ready, limit_s=5.0):
    """Yield short sleeps to ``run`` until ``ready()``: wait on an event, not a guess at load."""
    for _ in range(int(limit_s / 0.005)):
        if ready():
            return
        yield SleepFrame(0.005)


def test_the_processor_listens_after_its_reply_and_reports_the_tool_done():
    session, listener = FakeSession([{"effects": [reply(7, "Sure, I'm listening!"),
                                                  listen_effect(8)]}]), FakeListener()
    noted = []

    async def notes(topic, passages):
        noted.append((topic, passages))
    session.notes = notes
    processor = make(session, playback=Idle())
    processor._song = listener
    down, _ = run(processor, [heard("Check out this tune, Sage."), SleepFrame(0.5)])
    assert "That's Pulse by Mira Vale. Let me read up on it." in spoken(down)
    assert noted == [("Pulse", ["One."])]
    assert {"sequence": 8, "status": "done"} in session.results


def test_while_it_listens_transcripts_are_dropped_and_a_barge_in_does_not_stop_it():
    session = FakeSession([{"effects": [reply(7, "Sure!"), listen_effect(8)]}])
    listener, submitted = FakeListener(release=asyncio.Event()), []

    async def notes(topic, passages):
        pass
    session.notes = notes
    processor = make(session, playback=Idle())
    processor._song = listener
    submit = processor._submit

    async def handled(heard, hold=True):
        await submit(heard, hold)
        submitted.append(heard)
    processor._submit = handled

    def frames():  # timed by events (a fixed 0.05 s raced the listen starting, under load)
        yield heard("Check out this tune.")
        yield from until(listener.started.is_set)
        yield InterruptionFrame()
        yield heard("lyrics la la la")
        yield from until(lambda: len(submitted) == 2)  # the lyrics handled while it listens
        listener.release.set()
        yield from until(lambda: any(r.get("sequence") == 8 for r in session.results))
    run(processor, frames())
    assert [t for _, t, _ in session.started] == ["Check out this tune."]
    assert {"sequence": 8, "status": "done"} in session.results
    assert not any(r.get("sequence") == 8 and r["status"] == "interrupted" for r in session.results)


def test_asking_it_to_listen_is_recognized_and_small_talk_about_music_is_not():
    for line in ("Sage, listen to this song.", "Listen to this!", "What song is this?", "Hey, what's playing?",
                 "Can you listen to the music for me?", "Which song is that?", "Do you know this song?",
                 "Can you recognize this track?", "Shazam this."):
        assert song.asks_to_listen(line), line
    for line in ("I like this song.", "Listen, I had a long day.", "What song do you like?",
                 "I listen to the radio every day.", "Do you listen to music?"):
        assert not song.asks_to_listen(line), line


def test_asked_to_listen_it_says_so_and_listens_without_a_kernel_turn():
    session, listener = FakeSession(), FakeListener()

    async def notes(topic, passages):
        pass
    session.notes = notes
    processor = make(session, playback=Idle())
    processor._song = listener
    down, _ = run(processor, [heard("Sage, what song is this?"), SleepFrame(0.5)])
    assert session.started == []  # the model chose the tool 0 of 3 times live: this never asks it
    assert spoken(down)[:2] == [song.LISTENING, "That's Pulse by Mira Vale. Let me read up on it."]
    assert session.results == []  # no effect was delivered, so none is reported
