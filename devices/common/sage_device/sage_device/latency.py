"""Voice-to-voice latency from the bot's log: ``make latency LOG=<bot log>``.

``v2v_ms`` (docs/design/LAYERS.md §0) runs from the person's last voiced frame to the
first audio handed to the robot's transport, plus ``LEAD_MS``. It is read from two
lines of sage.py per turn: its start, which names the lane and how long ago the voice
ended, and the first "bot started speaking" after it. A turn replaced before any
audio, ended with no words, or with no end of speech, is counted unmeasured.

Every line is stamped by ``FORMAT`` and ``stamp`` (``__main__.configure_logging``): wall
time, ``time.monotonic()`` and the turn in hand (``TURN_ID``, "-" outside a turn).
"""

import contextvars
import logging
import math
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

NO_TURN = "-"
#: The turn in hand, set where a turn starts (``SageProcessor._begin``) and on every frame.
TURN_ID: contextvars.ContextVar[str] = contextvars.ContextVar("turn_id", default=NO_TURN)
FORMAT = "%(asctime)s %(mono).3f %(turn_id)s %(levelname)s:%(name)s:%(message)s"
LINE = re.compile(r"(?P<wall>\S+ \S+) (?P<mono>\d+\.\d+) (?P<turn>\S+) "
                  r"(?P<level>[A-Z]+):(?P<name>[\w.]+):(?P<message>.*)")
START = re.compile(r"turn start \((?P<lane>\w+) lane\): voice ended (?P<ago>\d+|-) ms ago")
AUDIO = "bot started speaking"
SILENT = "turn ended with no words"
#: From audio handed to the transport to audio out of the speaker.
#: ponytail: a guess, which transport.PLAYBACK_LEAD_S also uses; measure it once on the robot.
LEAD_MS = 150


@dataclass(frozen=True)
class Turn:
    """One turn: its id, its lane ("reflex" or "kernel"), and its v2v_ms (None: unmeasured)."""

    turn_id: str
    lane: str
    v2v_ms: int | None


def stamp(record: logging.LogRecord) -> bool:
    """Give every record the monotonic time and the turn ``FORMAT`` shows: a handler filter."""
    record.mono = time.monotonic()
    record.turn_id = TURN_ID.get()
    return True


def turns(lines) -> list[Turn]:
    """Each turn sage.py started in these log lines, in order, with its v2v_ms."""
    found: list[Turn] = []
    ended = None  # the open turn's last voiced frame, monotonic; None once it has audio
    for line in lines:
        m = LINE.match(line)
        if not m or not m["name"].endswith(".sage"):  # any body's sage.py
            continue
        if start := START.match(m["message"]):
            found.append(Turn(m["turn"], start["lane"], None))
            ended = None if start["ago"] == "-" else float(m["mono"]) - int(start["ago"]) / 1000
        elif m["message"] == SILENT:
            ended = None
        elif m["message"] == AUDIO and ended is not None:
            found[-1] = Turn(found[-1].turn_id, found[-1].lane,
                             round((float(m["mono"]) - ended) * 1000) + LEAD_MS)
            ended = None
    return found


def pct(values, p: float):
    """Nearest-rank percentile, as eval/score.py and sage-kernel bench/latency.py compute it."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def summary(found: list[Turn]) -> dict[str, dict]:
    """Per lane: how many turns were measured and not, and the measured p50 and p95."""
    out = {}
    for lane in sorted({t.lane for t in found}):
        values = [t.v2v_ms for t in found if t.lane == lane and t.v2v_ms is not None]
        unmeasured = sum(t.lane == lane for t in found) - len(values)
        out[lane] = {"n": len(values), "unmeasured": unmeasured,
                     "p50": pct(values, 50) if values else None,
                     "p95": pct(values, 95) if values else None}
    return out


def main(argv: list[str]) -> None:
    """Print v2v_ms per lane for the bot log named in ``argv``."""
    rows = summary(turns(Path(argv[0]).read_text(errors="replace").splitlines()))
    if not rows:
        sys.exit("no turns found: is it a log from a stamped voice (make run)?")
    print(f"v2v_ms, lead {LEAD_MS} ms included")
    for lane, row in rows.items():
        print(f"{lane:7} n {row['n']:4}  unmeasured {row['unmeasured']:4}  "
              f"p50 {row['p50']}  p95 {row['p95']}")


if __name__ == "__main__":
    main(sys.argv[1:])
