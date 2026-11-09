"""Barge-in once speech has lasted, on the VAD the segmentation already runs.

A VADAnalyzer moves QUIET -> STARTING -> SPEAKING. Pipecat's VADProcessor only
reports SPEAKING and QUIET, so STARTING, the first evidence someone is talking,
is discarded. An earlier voice bot measured that edge at 160 ms (5 frames of 32 ms) ahead of
SPEAKING over real speech (``speech_onset_vad.py``) and barged in on it. On the owner's
live turns that edge was mostly not words (``min_onset_ms``), so interruption now waits for
speech to last; 0 restores STARTING. One analyzer, two consumers: interruption reads the
onset, segmentation keeps SPEAKING / QUIET with its thresholds untouched.

Raw audio energy was rejected as a trigger: Reachy's mic hears its own speaker,
and the confidence and volume gates are what keep that from counting as speech.
"""

import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pipecat.audio.vad.vad_analyzer import VADState
from pipecat.frames.frames import Frame, InterruptionWorkerFrame
from pipecat.processors.frame_processor import FrameDirection
from sage_device.reflex import PAUSE_S

from reachy_sage_voice.interruption import InterruptionTracker

logger = logging.getLogger(__name__)


class SpeechOnsetMixin:
    """Reports onset and offset from a VADAnalyzer without changing what it returns.

    Onset fires once speech has lasted ``min_onset_ms`` (0: on the first voiced frame, the
    STARTING edge); offset fires on the return to QUIET after an onset, never without one.
    Mix in ahead of the analyzer class. Handlers take no arguments and may be async.
    """

    #: Speech that must last before barge-in, ms. Live 2026-10-23, on STARTING: 20 of 43
    #: barge-ins had no speech the VAD confirmed and 7 more no words. 200 is the VAD's own
    #: start (start_secs), the shortest a segment can be.
    min_onset_ms = 200
    _on_detected: Callable[[], Any] | None = None
    _on_cleared: Callable[[], Any] | None = None
    _voiced = 0  # samples of speech since the last QUIET
    _detected = False
    _on_voice: Callable[[], Any] | None = None
    _on_pause: Callable[[], Any] | None = None
    _on_quiet: Callable[[], Any] | None = None
    _silent = 0  # samples of this silence, in STOPPING
    _paused = False

    def set_onset_handlers(self, *, on_detected: Callable[[], Any] | None = None,
                           on_cleared: Callable[[], Any] | None = None) -> None:
        """Set what runs on the onset and offset edges."""
        self._on_detected = on_detected
        self._on_cleared = on_cleared

    def set_reflex_handlers(self, *, on_voice: Callable[[], Any] | None = None,
                            on_pause: Callable[[], Any] | None = None,
                            on_quiet: Callable[[], Any] | None = None) -> None:
        """Set the body's edges: the first voiced frame, ``PAUSE_S`` of silence, back to QUIET."""
        self._on_voice, self._on_pause, self._on_quiet = on_voice, on_pause, on_quiet

    async def _pausing(self, state: VADState, samples: int) -> None:
        """Fire the pause once per silence; a VAD stopping as early still counts on its QUIET."""
        if state == VADState.STOPPING:
            self._silent += samples
        ended = state == VADState.QUIET and self._silent > 0
        if not self._paused and (ended or self._silent >= PAUSE_S * self.sample_rate):
            self._paused = True
            await _fire(self._on_pause, "pause")
        if state != VADState.STOPPING:
            self._silent, self._paused = 0, False

    async def analyze_audio(self, buffer: bytes) -> VADState:
        """Run the analyzer, then fire a handler if speech crossed the onset or went quiet."""
        state = await super().analyze_audio(buffer)
        await self._pausing(state, len(buffer) // 2)
        if state == VADState.QUIET:
            if self._voiced:
                await _fire(self._on_quiet, "quiet")
            self._voiced = 0
            if self._detected:
                self._detected = False
                await _fire(self._on_cleared, "cleared")
            return state
        if not self._voiced:
            await _fire(self._on_voice, "voice")
        self._voiced += len(buffer) // 2  # int16 mono
        if not self._detected and self._voiced * 1000 >= self.min_onset_ms * self.sample_rate:
            self._detected = True
            await _fire(self._on_detected, "detected")
        return state


async def _fire(handler: Callable[[], Any] | None, label: str) -> None:
    # On the live audio path: a bad handler costs one interruption, never the session.
    if handler is None:
        return
    try:
        result = handler()
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.exception("speech onset: %s handler failed", label)


def build_speech_onset_silero(params, min_onset_ms: float = SpeechOnsetMixin.min_onset_ms) -> Any:
    """Return a SileroVADAnalyzer that also reports speech onset after ``min_onset_ms``."""
    from pipecat.audio.vad.silero import SileroVADAnalyzer

    class SpeechOnsetSileroVAD(SpeechOnsetMixin, SileroVADAnalyzer):
        pass

    analyzer = SpeechOnsetSileroVAD(params=params)
    analyzer.min_onset_ms = min_onset_ms
    return analyzer


def make_speech_onset_handler(
    tracker: InterruptionTracker,
    enqueue_frame: Callable[[Frame, FrameDirection], Awaitable[None]],
    now: Callable[[], float] = time.monotonic,
) -> Callable[[str], Awaitable[None]]:
    """Return the onset handler that makes the bot yield when the policy says so.

    The interruption goes upstream as an ``InterruptionWorkerFrame`` (the 1.4+
    name for an earlier voice bot's ``InterruptionTaskFrame``): the pipeline worker turns it
    into an ``InterruptionFrame`` injected at the source, bypassing the queue a
    downstream frame would wait in. ``enqueue_frame`` is ``PipelineTask.queue_frame``.
    """

    async def on_speech_onset(sid: str) -> None:
        if not tracker.user_onset(now(), sid):
            return
        try:
            await enqueue_frame(InterruptionWorkerFrame(), FrameDirection.UPSTREAM)
        except Exception:
            logger.exception("speech onset: could not interrupt the bot for %s", sid)

    return on_speech_onset
