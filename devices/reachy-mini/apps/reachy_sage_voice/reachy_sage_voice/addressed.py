"""Reply only when addressed (SAGE_VOICE_ADDRESSED=1; default off).

A transcript reaches the kernel only when a face is tracked, the robot's name
("Sage", or an alias in SAGE_VOICE_ALIASES) was said, or the owner began
speaking within SAGE_VOICE_EXCHANGE_S seconds (default 8) of the conversation's
last sign of life: the pet speaking, or a turn that passed. Anything else, such
as lyrics or TV, is dropped here: protocol v1 has no "heard, context only"
event, and every ``submitted`` starts a turn.

Pipecat's FunctionFilter does the dropping. Its WakePhraseUserTurnStartStrategy
needs the LLM user aggregator, which this pipeline has not, keeps itself awake
on any user speech (which TV is), and knows nothing of faces.
"""

import asyncio
import logging
import re
import time
from collections.abc import Callable, Mapping

from pipecat.frames.frames import (
    BotSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.filters.function_filter import FunctionFilter

logger = logging.getLogger(__name__)

NAME = "Sage"
EXCHANGE_S = 8.0


class Addressed:
    """A FunctionFilter predicate: True for every frame but an unaddressed transcript."""

    def __init__(self, aliases: tuple[str, ...] = (), face: Callable[[], bool] | None = None,
                 exchange_s: float = EXCHANGE_S, now: Callable[[], float] = time.monotonic):
        """Answer to its name, ``aliases``, a tracked ``face``, or ``exchange_s`` after it spoke."""
        names = "|".join(re.escape(n) for n in (NAME, *aliases))
        self._name = re.compile(rf"\b(?:{names})\b", re.IGNORECASE)
        self._face, self.exchange_s, self._now = face, exchange_s, now
        self._alive_at: float | None = None  # the pet spoke, or a turn passed
        self._started_at: float | None = None  # the owner began the speech being transcribed

    async def __call__(self, frame: Frame) -> bool:
        """Pass ``frame`` on unless it is a transcript nobody addressed to the pet."""
        if isinstance(frame, (BotSpeakingFrame, BotStoppedSpeakingFrame)):
            self._alive_at = self._now()  # BotSpeaking comes every ~0.2 s while speaking
            return True
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._started_at = self._now()
            return True
        if not isinstance(frame, TranscriptionFrame):
            return True
        began = self._started_at if self._started_at is not None else self._now()
        addressed = ((self._alive_at is not None and began - self._alive_at < self.exchange_s)
                     or bool(self._name.search(frame.text))
                     # ponytail: the face as tracked now, not held like presence.py's; add a hold
                     # if people turning away mid-sentence get ignored on the robot.
                     or (self._face is not None and await asyncio.to_thread(self._face)))
        if addressed:
            self._alive_at = self._now()  # a turn that passed keeps the conversation open
        else:
            logger.info("not addressed, not sent: %r", frame.text)
        return addressed


def addressed_filter(env: Mapping[str, str],
                     face: Callable[[], bool] | None) -> FunctionFilter | None:
    """Return the filter SAGE_VOICE_ADDRESSED=1 asks for, else None."""
    if env.get("SAGE_VOICE_ADDRESSED", "0") != "1":
        return None
    aliases = tuple(filter(None, (a.strip() for a in
                                  env.get("SAGE_VOICE_ALIASES", "Reachy").split(","))))
    # Both ways and system frames too: the pet's speaking frames come upstream from the output
    # transport, and the VAD's start is a system frame; FunctionFilter skips its predicate for both.
    return FunctionFilter(Addressed(aliases, face, float(env.get("SAGE_VOICE_EXCHANGE_S", EXCHANGE_S))),
                          direction=None, filter_system_frames=True)
