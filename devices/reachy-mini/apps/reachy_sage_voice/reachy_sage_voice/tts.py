"""Kokoro, with captions: each word timed, so an interruption knows what was heard.

The services/tts app serves ``POST /dev/captioned_speech`` (Kokoro-FastAPI's
path and shape): NDJSON, one sentence per line, ``audio`` as base64 16-bit
24 kHz PCM and ``timestamps`` as ``[{word, start_time, end_time}]`` timed from
the start of the request. Each word becomes a TTSTextFrame with a presentation
time, which the output transport releases as that word is played, so Playback
records exactly the words heard up to an interruption.

Without captions (the endpoint answers 404) the plain ``/v1/audio/speech``
speaks instead, and each clause counts whole once its audio has played.
"""

import base64
import inspect
import json
import logging
import math
import time

import httpx
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InterruptionFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from sage_device.names import spoken, written

from reachy_sage_voice import health
from reachy_sage_voice.voice import VoiceStyleFrame

logger = logging.getLogger(__name__)

RATE = 24000
_BYTES_PER_S = RATE * 2
#: The captioned endpoint is absent, not failing: stop asking for it.
_NO_CAPTIONS = (404, 405, 501)
#: Shown instead of speech when Kokoro cannot speak (a puzzled look: can't hear, can't say).
DOWN_EMOTE = "confused1"
#: At most one emote this often while Kokoro stays down: each clause of a reply fails in turn.
#: ponytail: a time window, not per reply; key it on the reply if long replies repeat the emote.
DOWN_EVERY_S = 10.0
#: How long a clause may take to its first audio before it counts as unspoken. A server answers
#: once the clause is synthesized, so this fits the longest one at the slowest voice: 40 words
#: (the bot logs' longest is 23) are 16 s spoken at 2.5 words/s, so 16 s to make at Pocket TTS's
#: real-time factor of about 1, plus 4 s of margin for a busy GPU. Pipecat's default of 3 s took
#: anything past 7 words at that factor for silence. It costs nothing on the way out: a reply
#: closes as soon as it ends (``on_turn_context_completed``); only a hung server waits this long.
STOP_FRAME_TIMEOUT_S = 20.0


class _NoCaptionsError(Exception):
    pass


class KokoroTTSService(TTSService):
    """Kokoro over HTTP, word-timed when its server can caption."""

    def __init__(self, *, base_url: str, voice: str = "azelma", model: str = "kokoro",
                 names: tuple = (), on_down=None, **kwargs):
        """Speak through the Kokoro server at ``base_url`` (``.../v1`` or its root).

        ``names`` (``names.parse``) are sent respelled and captioned back as written.
        ``on_down()`` shows that a clause could not be spoken, at most every DOWN_EVERY_S.
        """
        super().__init__(push_text_frames=False, push_stop_frames=True, push_start_frame=True,
                         stop_frame_timeout_s=STOP_FRAME_TIMEOUT_S,
                         sample_rate=RATE, settings=TTSSettings(model=model, voice=voice),
                         **kwargs)
        self._root = base_url.rstrip("/").removesuffix("/v1")
        self._captions = True
        self._offset_s = 0.0  # audio already produced in this response
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
        self._style: dict = {}  # voice_style settings for the current reply
        self._names = names
        self._on_down, self._down_at = on_down, -math.inf

    def can_generate_metrics(self) -> bool:
        """Report TTFB like any TTS service."""
        return True

    async def cleanup(self):
        """Close the HTTP client."""
        await super().cleanup()
        await self._client.aclose()

    async def push_frame(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM):
        """Push a frame; a response ending or cut off restarts word timing."""
        await super().push_frame(frame, direction)
        if isinstance(frame, (InterruptionFrame, TTSStoppedFrame)):
            self._offset_s, self._style = 0.0, {}

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Take a voice style for the rest of this reply; everything else as usual."""
        if isinstance(frame, VoiceStyleFrame):
            self._style = dict(frame.settings)
        await super().process_frame(frame, direction)

    async def set_usable(self, is_usable: bool):
        """Never be written off: an outage ends, so the next reply always tries again.

        Pipecat stops giving a service work for good after three silent contexts or a permanent
        error, meant for a bad voice ID. On 2026-10-27 a slow, then unreachable Kokoro muted
        Sage this way for 5h45m, until a restart.
        """
        if is_usable:
            await super().set_usable(True)
        else:
            logger.error("Kokoro keeps failing: Sage is not speaking; every reply still tries it")

    async def push_error_frame(self, error: ErrorFrame, force_treat_as_permanent: bool = False):
        """Show that a clause went unspoken, however it failed (refused, unreachable, silent)."""
        health.report("voice.tts", False, "down")
        if self._on_down and time.monotonic() - self._down_at >= DOWN_EVERY_S:
            self._down_at = time.monotonic()
            try:
                shown = self._on_down()
                if inspect.isawaitable(shown):
                    await shown
            except Exception as shown_error:  # a gesture, not a requirement
                logger.warning("could not show Kokoro is down: %s", shown_error)
        await super().push_error_frame(error, force_treat_as_permanent)

    async def on_turn_context_completed(self):
        """Close the reply's context now, even if its last clause failed.

        ponytail: Pipecat's private flag says "this clause yielded audio", and without it the
        context idles out STOP_FRAME_TIMEOUT_S with every later reply queued behind it. This
        service always yields synchronously. Recheck on a Pipecat upgrade (the tests would tell).
        """
        self._is_yielding_frames_synchronously = True
        await super().on_turn_context_completed()

    async def run_tts(self, text: str, context_id: str):
        """Synthesize one clause; a failure shows ``on_down`` instead (``push_error_frame``)."""
        async for frame in self._speak(text, context_id):
            if not isinstance(frame, ErrorFrame):
                health.report("voice.tts", True, "up")
            yield frame

    async def _speak(self, text: str, context_id: str):
        """Synthesize one clause, captioned if the server can."""
        body = {"input": spoken(text, self._names), "voice": self._settings.voice, "model": self._settings.model,
                "response_format": "pcm", "speed": self._style.get("speed", 1.0)}
        try:
            if self._captions:
                try:
                    async for frame in self._captioned(body, context_id):
                        yield frame
                    return
                except _NoCaptionsError:
                    logger.warning("Kokoro has no captioned speech: whole clauses from now on")
                    self._captions = False
            async for frame in self._plain(body, text, context_id):
                yield frame
        except httpx.HTTPError as error:
            yield ErrorFrame(error=f"Kokoro unreachable: {error}")

    async def _captioned(self, body: dict, context_id: str):
        url = self._root + "/dev/captioned_speech"
        async with self._client.stream("POST", url, json={**body, "stream": True}) as response:
            if response.status_code in _NO_CAPTIONS:
                raise _NoCaptionsError
            if response.status_code != 200:
                yield ErrorFrame(error=f"Kokoro captioned speech: {response.status_code}")
                return
            await self.start_tts_usage_metrics(body["input"])
            produced = 0
            async for raw in response.aiter_lines():
                if not raw.strip():
                    continue
                sentence = json.loads(raw)
                audio = base64.b64decode(sentence["audio"])
                await self.stop_ttfb_metrics()
                yield TTSAudioRawFrame(audio, RATE, 1, context_id=context_id)
                produced += len(audio)
                await self.add_word_timestamps(
                    [(written(w["word"], self._names), self._offset_s + w["start_time"])
                     for w in sentence["timestamps"]], context_id)
            self._offset_s += produced / _BYTES_PER_S

    async def _plain(self, body: dict, text: str, context_id: str):
        url = self._root + "/v1/audio/speech"
        async with self._client.stream("POST", url, json=body) as response:
            if response.status_code != 200:
                yield ErrorFrame(error=f"Kokoro speech: {response.status_code}")
                return
            await self.start_tts_usage_metrics(text)
            held, produced = b"", 0
            async for chunk in response.aiter_bytes():
                held += chunk
                whole = len(held) - len(held) % 2  # never split a 16-bit sample
                if whole:
                    await self.stop_ttfb_metrics()
                    yield TTSAudioRawFrame(held[:whole], RATE, 1, context_id=context_id)
                    produced += whole
                    held = held[whole:]
        # The whole clause as one "word" timed at its end: released once it has played.
        self._offset_s += produced / _BYTES_PER_S
        await self.add_word_timestamps([(text.strip(), self._offset_s)], context_id)
