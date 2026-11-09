"""Find the owner: voice first, never the same place twice, call out, then give up (LAYERS O4).

Pure: the caller passes the time and what it saw; nothing here moves, sleeps or reads a clock.
Bearings are world yaw in degrees, left positive, as ``limits.LIMITS["yaw"]``; pitch is the
SDK's (down positive).

The caller loops: move to each ``View``, hold it ``dwell_s`` after arriving, then call
``next_step`` with ``face_seen``. On ``CallOut`` it says "<name>, where are you?" and calls
again with the reply's DoA (or None). ``Found`` hands off to tracking; ``GiveUp`` says so.
Pass ``doa`` only when fresh speech came in since the last call.
"""

from dataclasses import dataclass, replace

from reachy_sage_voice import limits

LOW, HIGH = limits.LIMITS["yaw"]
H_FOV = 70.0  # degrees the detector sees across (executor.H_FOV)


@dataclass(frozen=True)
class View:
    """Look here and hold: world yaw and SDK pitch, degrees."""

    bearing: float
    pitch: float


@dataclass(frozen=True)
class CallOut:
    """Say "<name>, where are you?" and listen: the reply's DoA reseeds the search."""



@dataclass(frozen=True)
class Found:
    """A face: hand off to tracking."""



@dataclass(frozen=True)
class GiveUp:
    """Nobody found within the cap: say so, then home."""



@dataclass(frozen=True)
class SearchState:
    """Where the search is: what it checked, what it is looking at, and when."""

    started: float
    phase: float  # when this round started: the search start, or the last reseed
    seeds: tuple[float, ...] = ()  # voice and last-seen bearings, looked at first
    checked: tuple[float, ...] = ()
    view: View | None = None
    since: float = 0.0  # when ``view`` was issued
    called: bool = False  # called out this round
    done: Found | GiveUp | None = None


def _clip(x: float, low: float, high: float) -> float:
    return max(low, min(high, x))


@dataclass(frozen=True)
class Search:
    """The planner's tuning; ``start`` and ``next_step`` are pure."""

    pace_s: float = 4.1  # one view, move and dwell: measured live 2026-10-23 (04 A1)
    dwell_s: float = 0.75  # held after arriving before "nobody here" (03 §4: 0.5-1 s)
    call_out_s: float = 15.0
    # ponytail: a guess at saying the line and the reply starting; tune from the live log.
    listen_s: float = 3.0  # after a call-out, listening for the reply before searching on
    cap_s: float = 60.0
    radius: float = H_FOV / 2  # inhibition of return: a FOV-wide zone around each checked view
    # ponytail: one tilt row; add a second (seated vs standing) if live recall misses faces.
    pitch: float = -10.0

    def start(self, doa: float | None, last_seen: float | None, now: float) -> SearchState:
        """Begin at ``now``: the voice bearing first, then the last face, both clipped to reach."""
        # ponytail: last_seen never decays; weigh it by age if stale faces mislead the search.
        seeds = tuple(_clip(b, LOW, HIGH) for b in (doa, last_seen) if b is not None)
        return SearchState(started=now, phase=now, seeds=seeds)

    def _unchecked(self, b: float, checked: tuple[float, ...]) -> bool:
        return all(abs(b - c) >= self.radius - 1e-9 for c in checked)

    def _nearest(self, here: float, checked: tuple[float, ...]) -> float | None:
        """Return the view on the unseen yaw nearest ``here``, abutting what was seen."""
        gaps, end = [], LOW
        for c in sorted(checked):
            if end + 1.0 < c - self.radius:
                gaps.append((end, c - self.radius))
            end = max(end, c + self.radius)
        if end + 1.0 < HIGH:
            gaps.append((end, HIGH))
        best = None
        for a, b in reversed(gaps):  # on a tie, left first, as the owner's path
            half = min(self.radius, (b - a) / 2)
            at = _clip(here, a + half, b - half) if a <= here <= b else (
                a + half if here < a else b - half)
            if best is None or abs(at - here) < abs(best - here):
                best = at
        return best

    def next_step(self, state: SearchState, now: float, face_seen: bool,
                  doa: float | None) -> tuple[SearchState, View | CallOut | Found | GiveUp]:
        """Return the new state and what to do next; a frozen state is never changed."""
        if state.done:
            return state, state.done
        if face_seen:
            return replace(state, done=Found()), Found()
        if doa is not None:
            doa = _clip(doa, LOW, HIGH)
            if state.called:  # the reply: the owner may have moved, so look everywhere again
                state = replace(state, seeds=(doa,), checked=(), called=False, phase=now)
            else:
                state = replace(state, seeds=(doa, *state.seeds))
        if state.view is not None:
            if now - state.since < self.dwell_s:
                return state, state.view
            state = replace(state, checked=(*state.checked, state.view.bearing), view=None)
        if now + self.pace_s > state.started + self.cap_s:
            return replace(state, done=GiveUp()), GiveUp()
        seeds = tuple(s for s in state.seeds if self._unchecked(s, state.checked))
        here = state.checked[-1] if state.checked else 0.0
        bearing = seeds[0] if seeds else self._nearest(here, state.checked)
        if bearing is None and state.called:
            return replace(state, done=GiveUp()), GiveUp()
        if bearing is None or (not state.called
                               and now + self.pace_s > state.phase + self.call_out_s):
            return replace(state, seeds=seeds, called=True, phase=now), CallOut()
        view = View(bearing, _clip(self.pitch, *limits.LIMITS["pitch"]))
        return replace(state, seeds=seeds[1:], view=view, since=now), view
