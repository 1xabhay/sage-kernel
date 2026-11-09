"""Listen to a song, look it up, and hand the kernel what was found: the ``listen_to_song`` tool.

Declared only when SAGE_VOICE_RESEARCH_URL names the research service
(services/research). After the reply that accepted the request has played, the
body keeps listening for ``SNIPPET_S``, sends that much of the mic to the
service to name the song (Shazam: only a fingerprint leaves this machine), says
what it is, has the service read up on it (MusicBrainz, Wikipedia, LRCLIB, web
and news through SearXNG), and sends the result to the kernel as notes
(protocol §10). The kernel answers later questions from them.
"""

import asyncio
import io
import json
import logging
import re
import urllib.request
import uuid
import wave
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

#: How much of the song is sent to be named; Shazam needs a few seconds, a room more.
SNIPPET_S = 10
#: How much of the mic is kept for a snippet, so it can be taken at once.
KEEP_S = 15
RATE = 16000

LISTENING = "Okay, I'm listening."
NOT_FOUND = "I couldn't tell what song that is."
NO_MUSIC = "I don't hear any music."
UNREACHABLE = "I can't look songs up right now."
READY = "Okay, ask me anything about it."

# Asked to listen, the body listens without asking the model, which chose the tool 0 of 3 times live
# (qwen3:4b-instruct) and said "I'm listening" anyway. ponytail: phrases; the tool stays for the rest.
ASKS = re.compile(r"\b(listen to (this|that)\b|listen to the (song|music|track)|(what|which) song is (this|that|playing)"
                  r"|what(\s+i|')s playing|(know|recogni[sz]e|identify|name) (this|that|the) (song|tune|track)"
                  r"|capture (this|that|the) song"
                  r"|what song (this|that) is"
                  r"|what(\s+i|')s (this|that) (song|tune|track)\b(?! about)"
                  r"|shazam)", re.IGNORECASE)


def asks_to_listen(line: str) -> bool:
    """Whether the person asked the body to listen to the music and name it."""
    return bool(ASKS.search(line))


TOOL = {
    "name": "listen_to_song",
    "description": ("Listen to the music playing now and look the song up, when the person asks you to "
                    "listen to a song or what song this is. Say briefly that you are listening."),
    "inputSchema": {"type": "object", "properties": {}},
    "max_s": 90,  # the snippet, naming it, and up to ~20 s of research, with room to spare
    "interruptible": False,
}


def wav(pcm: bytes, rate: int = RATE) -> bytes:
    """Mono int16 PCM as a WAV file."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def form_wav(data: bytes, filename: str) -> tuple[bytes, str]:
    """A WAV as the one multipart field ``file``: (body, Content-Type)."""
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: audio/wav\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


class ResearchClient:
    """The research service over stdlib HTTP: name a snippet, read up on a song."""

    def __init__(self, url: str, timeout_s: float = 40.0):
        """Take the service's base URL."""
        self.url, self.timeout_s = url.rstrip("/"), timeout_s

    def identify(self, snippet: bytes) -> dict:
        """``{"found": False}`` or the song: title, artist, album, year, genre."""
        return self._post("/identify", *form_wav(snippet, "snippet.wav"))

    def research(self, title: str, artist: str) -> dict:
        """``{"topic": ..., "passages": [...]}``, ready to send as notes."""
        return self._post("/research", json.dumps({"title": title, "artist": artist}).encode(),
                          "application/json")

    def _post(self, path: str, body: bytes, kind: str) -> dict:
        request = urllib.request.Request(self.url + path, body, {"Content-Type": kind}, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            return json.loads(response.read())


class SongListener:
    """One listen at a time; ``busy`` while it listens, names and reads up."""

    def __init__(self, client, snippet: Callable[[float], bytes],
                 sleep: Callable[[float], Awaitable] = asyncio.sleep, snippet_s: float = SNIPPET_S,
                 music_present: Callable[[bytes], bool] | None = None):
        """``snippet(seconds)`` returns the mic's last ``seconds`` as 16 kHz mono int16.

        ``music_present(pcm)`` (the music listener, ``beat.py``) checks the snippet
        before anything is sent; None sends it unchecked.
        """
        self.client, self.snippet, self.sleep, self.snippet_s = client, snippet, sleep, snippet_s
        self.music_present = music_present
        self.busy = False

    async def listen(self, say: Callable[[str], Awaitable], notes: Callable[[str, list], Awaitable]
                     ) -> tuple[str, str | None]:
        """Listen, name, read up, send notes: the tool's (status, reason)."""
        self.busy = True
        try:
            await self.sleep(self.snippet_s)
            pcm = self.snippet(self.snippet_s)
            if self.music_present is not None and not self.music_present(pcm):
                await say(NO_MUSIC)  # not silence to Shazam
                return "failed", "no_music"
            found = await asyncio.to_thread(self.client.identify, wav(pcm))
            if not found.get("found"):
                await say(NOT_FOUND)
                return "failed", "not_recognized"
            title, artist = found["title"], found["artist"]
            await say(f"That's {title} by {artist}. Let me read up on it.")
            research = await asyncio.to_thread(self.client.research, title, artist)
            await notes(research["topic"], research["passages"])
            await say(READY)
            return "done", None
        except (OSError, ValueError, KeyError) as error:  # down, garbled, or the kernel refused the notes
            logger.warning("could not look the song up: %s", error)
            await say(UNREACHABLE)
            return "failed", "research_unreachable"
        finally:
            self.busy = False
