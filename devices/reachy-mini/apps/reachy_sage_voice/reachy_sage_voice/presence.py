"""Presence from the daemon's face tracker, as events on seen and lost edges only.

A tracker that reports nothing is not an empty room: you lean out of frame, the
light shifts. As the pet's FaceMemory does, the last sighting is held, then
faded, and only when it has faded out is "lost" reported. Time is passed in.
A face that is not fresh (the detector off, or an old stamp) is unknown, not
an empty room: nothing is folded in and nothing sent (LAYERS.md F1, O3 a).
The event follows the contract draft: ``{kind: "presence", state, confidence}``.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from reachy_sage_voice.perception.face import Face

logger = logging.getLogger(__name__)


class Presence:
    """Seen/lost edges from a stream of detections."""

    #: Detections in a row before someone counts as arrived (lead): arrivals
    #: trigger greetings, and one false-positive frame must not greet, then leave.
    SEEN_AFTER = 2

    def __init__(self, hold_s: float = 2.0, fade_s: float = 6.0) -> None:
        """Hold a sighting ``hold_s``, then fade it over ``fade_s`` (the pet's values)."""
        self.hold_s, self.fade_s = hold_s, fade_s
        self.last_seen: float | None = None
        self.present = False
        self._run = 0  # detections in a row

    def confidence(self, now: float) -> float:
        """How much the last sighting is still worth, 1.0 to 0.0."""
        if self.last_seen is None:
            return 0.0
        age = now - self.last_seen
        if age <= self.hold_s:
            return 1.0
        return max(0.0, 1.0 - (age - self.hold_s) / self.fade_s)

    def update(self, detected: bool, now: float) -> dict | None:
        """Fold in one observation; return an event on a seen or lost edge, else None."""
        self._run = self._run + 1 if detected else 0
        if detected and (self.present or self._run >= self.SEEN_AFTER):
            self.last_seen = now
        confidence = self.confidence(now)
        # Seen is a fresh sighting (1.0) and lost a faded one (0.0): either side of
        # the kernel's >= 0.5 rule.
        if confidence > 0.0 and not self.present:
            self.present = True
            return {"kind": "presence", "state": "seen", "confidence": confidence}
        if confidence == 0.0 and self.present:
            self.present = False
            return {"kind": "presence", "state": "lost", "confidence": 0.0}
        return None


async def watch_presence(face: Callable[[], Face],
                         send: Callable[[dict], Awaitable[None]],
                         clock: Callable[[], float] = time.monotonic,
                         sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                         period_s: float = 0.2,
                         until: Callable[[], bool] = lambda: False) -> None:
    """Poll the tracker every ``period_s``, sending only edges, through ``send``.

    ``face`` reads a ``Face`` percept; one not fresh is skipped, so it is never
    "lost". ``make run`` starts it only with SAGE_VOICE_PRESENCE=1, until the
    kernel accepts presence events.
    """
    presence = Presence()
    while not until():
        seen = await asyncio.to_thread(face)
        event = presence.update(seen.detected, clock()) if seen.fresh else None
        if event is not None:
            logger.info("arrived" if event["state"] == "seen" else "left")  # pet.py
            await send(event)
        await sleep(period_s)
