"""Whose turn it is, for any body: a reply waits for the person, a cut-off line gets no answer.

Real logs: 22-28% of speech began over a voice in the long sessions of 2026-10-27, 4% with the
shorter replies of 2026-11-05. Most of the turn's budget is the model, so a voice can start again
before a word is played; held here, that voice's transcript replaces the turn (as any fragment
does before audio) and the reply answers all of it. Holding never drops a line: it only delays.

A statement with no end is half-heard, whatever its length up to ``HALF_WORDS``: the body's own
wait for the rest (``hold_s``) has already run out by then. A line needing words is never that.

Talk not meant for it: real logs are full of the owner on a call, a podcast, people in the room.
Always on, no wake word: nothing here asks to be addressed, a line must show it was not. It opens
after a quiet in the middle of a thought, or about other people, and nothing in it says the person,
the body or a need. When unsure it answers: ignoring its person is the failure that matters.

ponytail: lexical, as ``hearing``; STT confidence would do better, if the STT ever sends it.
"""

import math
import re
from collections.abc import Callable

from sage_device.choice import _BAD_DAY, _LEADING, must_speak
from sage_device.hearing import ASKING, COMMON, unfinished

#: Longest hold on a reply, in seconds: it waits out a voice still going, but a TV the VAD takes for
#: a voice must not silence the body. Replay, all logs: 9.1% of voice runs outlast 6 s, 1.6% 15 s.
#: ponytail: tune live.
MAX_HOLD_S = 15.0
#: Distress waits no longer than every reply did before the hold grew (6 s).
DISTRESS_HOLD_S = 6.0
#: How long after a voice stops its transcript may still arrive, in seconds (STT 0.4-0.8 s live).
GRACE_S = 1.0
#: Most words in a statement doubted for having no end; longer ones are mostly clipped real speech.
HALF_WORDS = 12
#: A cut-off statement leads whatever the person says next within this, in seconds: nothing lost.
CARRY_S = 8.0
#: After this long with no exchange, in seconds, a line can be overheard; inside one it never is.
#: ponytail: tune live; real replay, one session: 15 s catches 9 lines, 30 s 8, 120 s 3.
OPEN_S = 30.0
_CONTINUING = frozenset(("and", "but", "because"))  # not "so": people open with it
_OTHERS = frozenset(("he", "he's", "she", "she's", "they", "they're", "they've", "him", "his",
                     "her", "them", "their"))
_YOU = frozenset(("you", "your", "you're", "you've", "yours"))
_OWN = frozenset(("me", "my", "mine", "myself"))
_FEELING_WORDS = 5


def taken(now: float, began: float, voice_on: float, voice_off: float) -> bool:
    """Whether the person has the floor over a turn ``began``: talking, or just stopped since then.

    Stops before ``began`` are the speech the turn answers. Times from one clock; -inf for never.
    """
    return voice_on > voice_off or (voice_off > began and now - voice_off < GRACE_S)


#: Watching, the TV never yields: a reply waits for a gap in the voice, in seconds, not for silence
#: (watch/academic.md rule 2: breakpoints). One that can go unsaid waits ``MEDIA_WAIT_S`` for one,
#: then is dropped, never spoken over the TV; one that needs words takes a short gap, or goes at its
#: cap, which is no later than distress waited before (``DISTRESS_HOLD_S``).
#: ponytail: the gap is any voice's, TV or person; tune live.
GAP_S, MEDIA_WAIT_S = 1.5, 20.0
MUST_GAP_S, MUST_WAIT_S = 0.6, 6.0
WAIT, SPEAK, DROP = "wait", "speak", "drop"


def media_floor(waited: float, gap: float, must: bool) -> str:
    """Watching: WAIT, SPEAK or DROP a reply held ``waited`` s, the voice quiet for ``gap`` s."""
    if gap >= (MUST_GAP_S if must else GAP_S):
        return SPEAK
    if waited >= (MUST_WAIT_S if must else MEDIA_WAIT_S):
        return SPEAK if must else DROP
    return WAIT


def hold_limit(line: str, named: Callable[[str], bool] = lambda word: False) -> float:
    """Longest a reply to ``line`` waits for the person to finish, in seconds."""
    return DISTRESS_HOLD_S if must_speak(line, named) == "distress" else MAX_HOLD_S


def half_heard(line: str, named: Callable[[str], bool] = lambda word: False) -> bool:
    """Whether ``line`` is a statement cut off: answer it and the answer is a guess."""
    words = re.findall(r"[\w'-]+", line.lower())
    opening = next((word for word in words if word not in _LEADING), "")  # "so what did you"
    return (len(words) <= HALF_WORDS and unfinished(line) and not COMMON.issuperset(words)
            and opening not in ASKING and must_speak(line, named) in ("", "too long"))


def overheard(line: str, since_exchange: float = math.inf, *, asked: bool = False,
              named: Callable[[str], bool] = lambda word: False) -> bool:
    """Whether ``line`` is talk it should keep out of: no words, no move, nothing told the kernel.

    ``since_exchange``: seconds since it last answered anything or spoke. ``asked``: it just asked.
    """
    words = re.findall(r"[\w'-]+", line.lower().replace("\u2019", "'"))
    if (asked or since_exchange < OPEN_S or not words or COMMON.issuperset(words)
            or must_speak(line, named) not in ("", "too long")
            or not (_YOU | _OWN).isdisjoint(words) or _BAD_DAY.search(line.lower())):
        return False
    short_feeling = words[0] in ("i", "i'm") and len(words) <= _FEELING_WORDS  # "I miss her."
    return words[0] in _CONTINUING or (not _OTHERS.isdisjoint(words) and not short_feeling)
