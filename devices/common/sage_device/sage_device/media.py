"""Watching together, for any body: notice a broadcast from what is heard (owner, 2026-11-08).

Its commentary is kept out of turns, and the kernel is told what is on (heard, not seen) when the
person talks. A session starts with no command: ``START_LINES`` broadcast-like lines within
``WINDOW_S``, or the person saying so ("I'm watching the race"). It ends ``END_S`` after the last
broadcast line. While it is on, a long line that is not the person's (no name, no distress, no
question to "you") is the TV's: it joins the last ``HEARD`` lines the kernel is shown, and never
reaches the kernel as the person's line. Big moments (a profile's words) get a move, and words at
most once every ``SPOKEN_GAP_S``. A film or video keeps quiet company: no spoken cheering.

The evidence (live log, 11-08 F1, 24.5 min): 233 transcripts, 215 kernel turns, 66 spoken
replies, 63 of them "I can't see / I don't know"; commentary lines a median 9 words, the
person's 1. Alone, it starts only on a sport's words; a film or video starts when told.

ponytail: lexical, English and from words alone: SNR and L-R did not separate the two in that
log. A classifier on the noise traces (eval/noise.py labels) replaces ``_broadcast`` once labelled.
"""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, replace

from sage_device.choice import must_speak

WINDOW_S, START_LINES, END_S = 60.0, 4, 180.0
BROADCAST_WORDS = 7  # the person's lines in the F1 log were a median 1 word, the commentary's 9
HEARD, HEARD_CHARS = 6, 200  # what the kernel is shown (its input.media caps: 6 lines of 600)
# ponytail: a distress word only makes a line the person's up to this length. The person's lines in
# the F1 log ran 1-10 words; merged commentary 60-70, "an unsafe rejoin" in one. Tune on labels.
DISTRESS_WORDS = 25
SPOKEN_GAP_S = 240.0  # a few words for a big moment at most this often; a move otherwise
PERSON, COMMENTARY = "person", "commentary"
SPEAK, MOVE = "speak", "move"

_SAYS_SO = re.compile(
    r"\b(?:i'?m|we'?re|i am|we are) (?:watching|looking at)\b|\blet'?s watch\b"
    r"|\b(?:the race|the game|the match|the movie|the film|the show) is on\b")
# Odds and bets are never repeated (watch/behaviour.md H6): a line naming them is the TV's, unkept.
_BETTING = re.compile(r"\b(?:odds|bookmakers?|bookies?|bets?|betting|wagers?|sportsbook)\b")
_TO_YOU = re.compile(r"\b(?:you|your|you're|you'd)\b")
# Profiles: about, its words, and its big moments (None: quiet company). First match wins.
_F1 = re.compile(
    r"\b(?:grand prix|formula (?:one|1)|f1|pit (?:wall|stop|lane)|pole|lap \w+|laps|drs)\b")
_SPORT = re.compile(
    r"\b(?:race|lap|laps|driver|drivers|tyres?|tires?|grid|podium|overtake|goal|match|score"
    r"|scores|team|season|points|championship|league|inning|quarter|half[- ]time|penalty"
    r"|corner)\b")
# Short commentary, while watching: the race's own words (2026 grid, teams, calls). A short line of
# the person's rarely names a driver without asking ("Is Norris winning?" is theirs: a question).
# ponytail: a static list per profile; it ages with the grid, and a person's "Norris is fast." reads
# as the TV's. The noise-trace classifier replaces it once labelled.
_F1_SHORT = re.compile(
    r"\b(?:verstappen|norris|piastri|leclerc|hamilton|russell|antonelli|alonso|stroll|gasly"
    r"|colapinto|ocon|bearman|albon|sainz|hulkenberg|hülkenberg|bortoleto|lawson|lindblad"
    r"|hadjar|perez|pérez|bottas|max|lando|oscar|charles|lewis|george|kimi|fernando"
    r"|red bull|mclaren|ferrari|mercedes|aston martin|alpine|haas|williams|audi|racing bulls"
    r"|cadillac|box|tyres?|tires?|softs?|mediums?|hards?|inters|sector|undercut|overcut|drs"
    r"|safety car|vsc|pit|pits|pitted|pole|lap|laps|grid|chicane|p\d{1,2}|gap|stint|flag)\b")
_SPORT_MOMENT = re.compile(
    r"\b(?:overtakes?|crash(?:ed|es)?|safety car|red flag|fastest lap|wins|won it|goal"
    r"|penalty|he'?s through|into the lead|what a|incredible|unbelievable|oh no|spins?)\b")


@dataclass(frozen=True)
class Watch:
    """What is on: the broadcast lines heard (time, text), and whether a session is on."""

    lines: tuple[tuple[float, str], ...] = ()
    on: bool = False
    spoke_at: float = -math.inf
    since: float = -math.inf  # when it was last on or heard: a session the person began ends too
    about: str = ""  # its most specific profile so far, kept until it ends: it never flickers back


def _broadcast(line: str, named: Callable[[str], bool]) -> bool:
    """Whether ``line`` reads as a broadcast's: long, and nothing in it needs Sage."""
    if len(line.split()) < BROADCAST_WORDS:
        return False
    why = must_speak(line, named)
    if why in ("name", "teaching") or (why == "distress" and len(line.split()) <= DISTRESS_WORDS):
        return False
    to_you = why == "question" and _TO_YOU.search(line.lower())
    return not (to_you and len(line.split()) <= 12)


def heard(watch: Watch, now: float, line: str,
          named: Callable[[str], bool] = lambda word: False) -> tuple[Watch, str]:
    """Take ``line`` at ``now``: the next ``Watch``, and whose line it is (PERSON, COMMENTARY)."""
    if watch.on and now - max((watch.since, *(t for t, _ in watch.lines[-1:]))) > END_S:
        watch = Watch()
    if _SAYS_SO.search(line.lower()):
        return replace(watch, on=True, since=now), PERSON
    if watch.on and _short_commentary(watch, line, named):
        return replace(watch, since=now), COMMENTARY
    if not _broadcast(line, named):
        return watch, PERSON
    if _BETTING.search(line.lower()):
        return watch, COMMENTARY if watch.on else PERSON
    watch = replace(watch, lines=(*watch.lines, (now, line))[-HEARD:])
    # Alone it starts only on a broadcast's words: a meeting or call is overheard, not watched (H7).
    sustained = sum(now - t <= WINDOW_S for t, _ in watch.lines) >= START_LINES
    about, moments = _profile(watch)
    on = watch.on or (sustained and moments is not None)
    return replace(watch, on=on, about=about if on else ""), COMMENTARY if on else PERSON


def _short_commentary(watch: Watch, line: str, named: Callable[[str], bool]) -> bool:
    """Whether a short ``line`` heard while watching is the TV's: the race's words, no need."""
    if len(line.split()) >= BROADCAST_WORDS or must_speak(line, named) not in ("", "too long"):
        return False
    about = _profile(watch)[0]
    vocabulary = (_F1_SHORT if about == "a Formula 1 race"
                  else _SPORT if about == "a sports broadcast" else None)
    return bool(vocabulary and vocabulary.search(line.lower()))


_RANK = {"a show or video": 0, "a sports broadcast": 1, "a Formula 1 race": 2}


def _profile(watch: Watch) -> tuple[str, re.Pattern | None]:
    """Say what is on: the most specific of what was decided and what the last lines say."""
    about = max(watch.about or "a show or video", _heard_profile(watch), key=_RANK.get)
    return about, None if about == "a show or video" else _SPORT_MOMENT


def _heard_profile(watch: Watch) -> str:
    text = " ".join(line for _, line in watch.lines).lower()
    if _F1.search(text):
        return "a Formula 1 race"
    if len(_SPORT.findall(text)) >= 2:
        return "a sports broadcast"
    return "a show or video"


def context(watch: Watch) -> dict | None:
    """Say what is on, for the person's line (the kernel's input.media); None with no session."""
    if not watch.on:
        return None
    return {"about": _profile(watch)[0], "heard": [line[:HEARD_CHARS] for _, line in watch.lines]}


def moment(watch: Watch, now: float, line: str) -> tuple[Watch, str | None]:
    """React to a broadcast ``line``: SPEAK, MOVE or None (no moment, or quiet company)."""
    _, moments = _profile(watch)
    if not (watch.on and moments and moments.search(line.lower())):
        return watch, None
    if now - watch.spoke_at >= SPOKEN_GAP_S:
        return replace(watch, spoke_at=now), SPEAK
    return watch, MOVE
