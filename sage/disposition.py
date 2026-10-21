"""Disposition: whether Sage may speak first. Pure; every step takes `now` (seconds, the kernel's clock).

quiet: asked to be quiet; nothing proactive until spoken to or QUIET_S passes.
engaged: the person spoke within ENGAGED_S.
restless: someone is present and nothing has happened for RESTLESS_S.
available: otherwise, including when nobody is there.
"""

import re
from dataclasses import dataclass, replace

GREET_AFTER_S = 5 * 60  # an absence at least this long makes the next sighting an arrival worth a greeting
ENGAGED_S = 60.0
RESTLESS_S = 10 * 60
NUDGE_BUDGET = 3  # per hour
NUDGE_COOLDOWN_S = 15 * 60
QUIET_S = 30 * 60
# ponytail: a lexical rule until the System 1 classifier replaces it.
QUIET = re.compile(r"\b(be quiet|quiet,? please|shh+|shush|hush|stop talking|leave me alone|not now)\b")


@dataclass(frozen=True)
class Timings:
    """The settings, configurable: SAGE_GREET_AFTER_ABSENT_S, SAGE_NUDGE_AFTER_IDLE_S, SAGE_QUIET_S,
    SAGE_NUDGE_GAP_S, SAGE_NUDGES_PER_HOUR, SAGE_GREET_ON_START and SAGE_ENGAGED_S. A nudge counts against
    the budget and the gap when it is decided, so one answered with silence counts too (lead)."""
    greet_after_s: float = GREET_AFTER_S
    nudge_after_idle_s: float = RESTLESS_S
    quiet_s: float = QUIET_S
    nudge_gap_s: float = NUDGE_COOLDOWN_S
    nudges_per_hour: int = NUDGE_BUDGET
    greet_on_start: bool = True  # whether the first seen after a start is an arrival
    engaged_s: float = ENGAGED_S  # SAGE_ENGAGED_S


DEFAULT = Timings()


@dataclass(frozen=True)
class Disposition:
    present: bool = False
    absent_since: float | None = None  # None: never seen yet
    last_spoke: float | None = None  # the person's last turn
    last_activity: float | None = None  # the last thing that happened between them: arrival, a turn, a greet, a nudge
    quiet_until: float | None = None
    nudges: tuple[float, ...] = ()  # within the last hour


def asks_for_quiet(text: str) -> bool:
    return bool(QUIET.search(text.lower().replace("’", "'")))


def state(d: Disposition, now: float, t: Timings = DEFAULT) -> str:
    if d.quiet_until is not None and now < d.quiet_until:
        return "quiet"
    if d.last_spoke is not None and now - d.last_spoke < t.engaged_s:
        return "engaged"  # no nudge this soon after the person spoke, whatever the idle setting
    # Restless counts from the last activity (speech included) or from when quiet ended, so the end of
    # quiet is not followed at once by a nudge.
    since = max(d.last_activity or 0.0, d.quiet_until or 0.0)
    if d.present and d.last_activity is not None and now - since >= t.nudge_after_idle_s:
        return "restless"
    return "available"


def step(d: Disposition, kind: str, now: float, text: str = "", t: Timings = DEFAULT) -> tuple[Disposition, tuple[str, ...]]:
    """One event: "seen", "lost", "spoke" (with the person's text) or "tick". Returns what to do: "greet", "nudge"."""
    quiet = state(d, now, t) == "quiet"
    if kind == "seen":
        if d.present:
            return d, ()
        arrival = t.greet_on_start if d.absent_since is None else now - d.absent_since >= t.greet_after_s
        d = replace(d, present=True, last_activity=now if arrival or d.last_activity is None else d.last_activity)
        return d, ("greet",) if arrival and not quiet else ()
    if kind == "lost":
        return replace(d, present=False, absent_since=now), ()
    if kind == "spoke":
        until = now + t.quiet_s if asks_for_quiet(text) else None  # speaking to it ends quiet; asking starts it
        return replace(d, last_spoke=now, last_activity=now, quiet_until=until), ()
    if kind != "tick":
        raise ValueError(f"unknown disposition event {kind!r}")
    recent = tuple(at for at in d.nudges if now - at < 3600)
    d = replace(d, nudges=recent)
    if (state(d, now, t) == "restless" and len(recent) < t.nudges_per_hour
            and (not recent or now - recent[-1] >= t.nudge_gap_s)):
        return replace(d, nudges=recent + (now,), last_activity=now), ("nudge",)
    return d, ()
