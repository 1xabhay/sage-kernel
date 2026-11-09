"""A fake clock, a mic that plays a click track on it, and a FakeMini that dances on it."""

import asyncio
from types import SimpleNamespace

import numpy as np
from fake_mini import FakeMini, m1

RATE = 16000


class FakeClock:
    """Time moves only when someone sleeps; ``at(t, fn)`` runs ``fn`` once time reaches ``t``."""

    def __init__(self):
        self.t = 0.0
        self._due: list[tuple[float, object]] = []

    def __call__(self) -> float:
        return self.t

    def at(self, when, fn):
        self._due.append((when, fn))

    async def sleep(self, seconds):
        self.t += max(seconds, 0.0)
        for due in [d for d in self._due if d[0] <= self.t]:
            self._due.remove(due)
            due[1]()
        await asyncio.sleep(0)


def click_track(clock, bpm, start=-60.0, end=float("inf"), offset=0.05, louder=float("inf")):
    """Make ``audio(seconds)``: the last ``seconds`` of clicks at ``offset + k*60/bpm``.

    The music plays from ``start`` to ``end`` (clock time) and is silent otherwise; from
    ``louder`` on, the clicks are three times as loud (a drop).
    """
    n = np.arange(int(0.01 * RATE))
    blip = 0.5 * np.sin(2 * np.pi * 2000 * n / RATE) * np.exp(-n / 40)

    def audio(seconds):
        now = clock()
        x = np.zeros(int(seconds * RATE), np.float32)
        t0 = now - seconds
        period = 60 / bpm
        k = np.ceil((max(t0, start) - offset) / period)
        while (t := offset + k * period) < min(now, end):
            i = round((t - t0) * RATE)
            x[i:i + len(blip)] += blip[:len(x) - i] * (3 if t >= louder else 1)
            k += 1
        return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()

    audio.beats = lambda a, b: [offset + k * 60 / bpm
                                for k in range(int(np.ceil((a - offset) * bpm / 60)),
                                               int((b - offset) * bpm / 60) + 1)]
    return audio


class FakeDanceMini(FakeMini):
    """Records set_target with antennas on the fake clock; recorded moves play on it too."""

    def __init__(self, clock: FakeClock, yaw=0.0):
        super().__init__()
        self.clock = clock
        self.yaw = yaw
        self.targets: list[SimpleNamespace] = []
        self._cancelled = False

    def set_target(self, head=None, antennas=None, body_yaw=None):
        assert antennas is None or len(antennas) == 2, f"the SDK takes 2 antennas, got {len(antennas)}"
        m1("set_target")
        self.calls.append(("set_target", self.clock(), head, body_yaw))
        self.targets.append(SimpleNamespace(t=self.clock(), head=head, antennas=antennas,
                                            body_yaw=body_yaw))

    def goto_target(self, head=None, antennas=None, duration=0.5, method=None, body_yaw=0.0):
        assert antennas is None or len(antennas) == 2, f"the SDK takes 2 antennas, got {len(antennas)}"
        # No wall-clock wait: the fake clock is the only time here.
        m1("goto_target")
        self.calls.append(("goto_target", self.clock(), head, antennas, duration, body_yaw))

    def cancel_move(self):
        self._cancelled = True
        self.calls.append(("cancel_move", self.clock()))

    async def async_play_move(self, move, play_frequency=100.0, initial_goto_duration=0.0,
                              sound=True):
        m1("async_play_move")
        self.calls.append(("play_move", self.clock(), move, sound))
        self._cancelled = False
        start = self.clock()
        while self.clock() - start < move.duration and not self._cancelled:
            await self.clock.sleep(0.01)
        self.calls.append(("move_ended", self.clock(), move))
