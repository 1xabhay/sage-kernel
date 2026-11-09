"""The face percept (LAYERS.md F1): a daemon face reading, and whether it is fresh.

The daemon's detector runs only at a tracking weight above 0, and weight 0
clears its face, so "no face" from a stopped detector looks just like an empty
room. A reading counts only while the detector is on and its stamp is at most
``STALE_S`` old; otherwise it is not fresh, and nobody is seen or lost. Pure,
with time passed in.
"""

from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace

#: The oldest a face reading may be and still count (LAYERS.md F1).
STALE_S = 1.5


@dataclass(frozen=True)
class Face:
    """A face percept. ``bearing`` is the face's x in the image, -1 to 1; ``t`` its stamp."""

    detected: bool
    fresh: bool
    bearing: float | None
    t: float | None


def face_percept(reading, now: float, detector_on: bool) -> Face:
    """Judge ``reading`` (``detected``, ``x``, ``ts`` on ``now``'s clock; or None) at ``now``."""
    t = getattr(reading, "ts", None)
    fresh = detector_on and t is not None and 0.0 <= now - t <= STALE_S
    detected = fresh and bool(reading.detected)
    return Face(detected, fresh, getattr(reading, "x", None) if detected else None, t)


def restamped(clock: Callable[[], float]):
    """Return ``restamp(reading)``: the reading with ``ts`` moved onto ``clock``.

    The daemon stamps faces on its own monotonic clock, which may be another
    machine's. A stamp is put on ``clock`` when it is seen to move; a stamp seen
    first, or none, is None: when it was taken is unknown.
    """
    last = {"ts": None, "at": None}

    def restamp(reading):
        ts = getattr(reading, "ts", None)
        if ts is None:
            last.update(ts=None, at=None)
        elif ts != last["ts"]:
            last.update(ts=ts, at=None if last["ts"] is None else clock())
        return SimpleNamespace(detected=reading.detected, x=getattr(reading, "x", None),
                               ts=last["at"])
    return restamp
