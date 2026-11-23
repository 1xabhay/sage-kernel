"""Is this line for me? One ordered rule list for any body: ``attend`` (design 1-gate, 2026-11-21).

Per heard line: ``ANSWER`` (a turn for the brain), ``ACK`` (a look, no words, no model),
``LISTEN`` (silent) or ``OVERHEARD`` (nothing shown), and the rule that decided. The body supplies
what it has (``Signals``); a mic-only body leaves face, voice and direction out and the same rules
run. A conversation opens on an ANSWER and only an ANSWER keeps it open: not any heard line, which
let a TV hold it open for 243 lines. Unsure, it looks and says nothing: a wrong glance is cheap, a
right one makes the person carry on, and that line is answered.

ponytail: lexical and English, no weights and no model until the labelled set exists
(``eval/noise.py baseline`` scores this function on it).
"""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, replace

from sage_device.choice import _KERNEL_DISTRESS, _LOW, must_speak, requested
from sage_device.floor import _OWN, _YOU, half_heard, overheard
from sage_device.hearing import DISTRESS, trustworthy
from sage_device.media import BROADCAST_WORDS

ANSWER, ACK, LISTEN, OVERHEARD = "answer", "ack", "listen", "overheard"
_STEP_DOWN = {ANSWER: ACK, ACK: LISTEN, LISTEN: LISTEN, OVERHEARD: OVERHEARD}

#: A conversation closes this long, in seconds, after its last answered line or Sage's last word.
#: ponytail: the design's guess; measure on the labelled set: to-me lines by seconds since it spoke.
CLOSE_S = 20.0
#: After a look (ACK), the person's next line within this, in seconds, is answered.
#: ponytail: the design's guess, the same 8 s as ``floor.CARRY_S``; measure on the labelled set.
FOLLOW_S = 8.0
#: Asides in a row ("he/she/they", "and/but/because") that close an open conversation.
ASIDES_CLOSE = 2
#: Mean word confidence under which a line steps down. Replay, 5,197 logged lines of 2026-11-08 to
#: 11-22: median 0.54, a quarter under 0.39, a tenth under 0.28. It rises with length (one word:
#: median 0.34; thirteen or more: 0.83), so the design's 0.5 would doubt 43% of all lines and most
#: short ones, which are the person's; the TV's long clear lines pass any floor. The 14 lines with
#: its name, the only ones known to be for it, go no lower than 0.36.
#: ponytail: the lowest tenth, not an accuracy; set it where ``eval/noise.py baseline`` scores best.
LOW_CONFIDENCE = 0.28
#: A voice from this many degrees off the one that opened the conversation is another voice.
#: ponytail: a guess, no logged direction yet (the ``stt:`` line has it from step 1 on).
DOA_JUMP_DEG = 45.0
#: Voiceprint score at or over which a voice is the owner's: the STT's own default threshold.
OWNER_SIMILARITY = 0.5

#: Everyday words in ``hearing.DISTRESS``: alone they are no cry. Replay: 514 of 5,197 lines read as
#: distress, 427 of them through one of these and nothing else ("don't" 224, "no" 143). They keep
#: their place in ``must_speak`` (words, once it answers); they do not make it answer over a TV.
_EVERYDAY = frozenset(("no", "don't", "stop", "wait", "sorry", "wrong"))
_GREETING = re.compile(
    r"^(?:oh |well |okay |ok )?(?:hi|hello|hey|yo|good (?:morning|afternoon|evening))\b"
)
_BYE = re.compile(r"\b(?:bye|goodbye|good ?night|see you (?:later|soon|tomorrow))\b")
_NOT_YOU = re.compile(r"\bnot (?:talking to |speaking to )?you\b")


@dataclass(frozen=True)
class Signals:
    """What the body knows about one heard line. Only ``now`` is required; None: no such sense."""

    now: float
    spoke_at: float = -math.inf  # Sage's last word, on the clock of ``now``
    asked: bool = False  # Sage asked a question a moment ago: whatever comes back is the answer
    face: bool | None = None  # a face is in view
    confidence: float | None = None  # the STT's, 0 to 1
    low: float = LOW_CONFIDENCE  # the body's own floor for its STT's scale
    similarity: float | None = None  # voiceprint score against the owner's, once enrolled
    doa: float | None = None  # direction of the voice, degrees
    commentary: bool = False  # ``media.heard`` took the line for the TV's
    named: Callable[[str], bool] = lambda word: False  # the word is one of the body's names


@dataclass(frozen=True)
class Attention:
    """The conversation, between lines: open or not, and what the next line is judged against."""

    open: bool = False
    at: float = -math.inf  # the last ANSWER: only that refreshes it
    acked_at: float = -math.inf  # the last look given with no conversation open
    asides: int = 0  # asides in a row
    similarity: float | None = None  # the voice that opened it
    doa: float | None = None


def is_open(state: Attention, now: float, spoke_at: float = -math.inf) -> bool:
    """Whether a conversation is open at ``now``, Sage's last word at ``spoke_at``."""
    return state.open and now - max(state.at, spoke_at) <= CLOSE_S


def opened(state: Attention, now: float) -> Attention:
    """Sage spoke first (a greeting, a remark of its own): what comes back is in a conversation."""
    return replace(state, open=True, at=now, asides=0)


def _other_voice(s: Signals, state: Attention) -> bool:
    """Whether the voice is not the one that opened the conversation, as far as the body knows."""
    if None not in (s.similarity, state.similarity):
        return (s.similarity >= OWNER_SIMILARITY) != (state.similarity >= OWNER_SIMILARITY)
    if None not in (s.doa, state.doa):
        return abs((s.doa - state.doa + 180) % 360 - 180) > DOA_JUMP_DEG
    return False


def attend(line: str, signals: Signals, state: Attention) -> tuple[str, str, Attention]:
    """Return what to do with ``line``, the rule that decided, and the conversation after it."""
    s, now = signals, signals.now
    if not is_open(state, now, s.spoke_at):
        state = replace(state, open=False, asides=0, similarity=None, doa=None)
    if s.commentary:
        return OVERHEARD, "media", state
    plain = line.lower().replace("\u2019", "'")
    words = re.findall(r"[\w'-]+", plain)
    why = must_speak(line, s.named)
    you = not _YOU.isdisjoint(words)
    aside = overheard(line, s.named)
    cry = why == "distress" and (
        not (DISTRESS - _EVERYDAY).isdisjoint(words)
        or _KERNEL_DISTRESS.search(plain)
        or _LOW.search(plain)
    )
    firm = True  # rules 1-4: never stepped down for a doubt
    if _NOT_YOU.search(plain):
        return OVERHEARD, "dismissed", Attention()
    if cry:
        outcome, reason = ANSWER, "distress"
    elif any(map(s.named, words)):
        outcome, reason = ANSWER, "name"
    elif s.asked:
        outcome, reason = ANSWER, "asked"
    elif state.open and (
        why in ("question", "request", "teaching") or you or not _OWN.isdisjoint(words)
    ):
        outcome, reason = ANSWER, "direct"
    else:
        firm = False
        if state.open:
            outcome, reason = (LISTEN, "aside") if aside else (ANSWER, "open")
        elif s.face:
            outcome, reason = ANSWER, "face"
        elif (
            _GREETING.search(plain)
            or why == "teaching"
            or requested(plain)
            or (why == "question" and you)
        ):
            outcome, reason = ANSWER, "direct"
        elif aside and len(words) >= BROADCAST_WORDS:
            outcome, reason = OVERHEARD, "aside"
        elif now - state.acked_at <= FOLLOW_S:
            outcome, reason = ANSWER, "followed"
        else:
            outcome, reason = ACK, "unsure"
    if not firm:
        if s.confidence is not None and s.confidence < s.low:
            outcome, reason = _STEP_DOWN[outcome], reason + ", low confidence"
        elif not trustworthy(line, s.named) or half_heard(line, s.named):
            outcome, reason = _STEP_DOWN[outcome], reason + ", unclear"
    if state.open and reason not in ("distress", "name", "asked") and _other_voice(s, state):
        outcome, reason = _STEP_DOWN[outcome], reason + ", other voice"
    if _BYE.search(plain) or (len(words) <= 3 and "stop" in words):
        return outcome, reason + ", closed", Attention()
    if outcome == ANSWER:
        mine = (state.similarity, state.doa) if state.open else (s.similarity, s.doa)
        return outcome, reason, Attention(True, now, similarity=mine[0], doa=mine[1])
    if outcome == ACK and not state.open:
        return outcome, reason, replace(state, acked_at=now)
    if outcome == LISTEN and state.open and reason == "aside":
        asides = state.asides + 1
        closed = asides >= ASIDES_CLOSE
        return outcome, reason, Attention() if closed else replace(state, asides=asides)
    return outcome, reason, state
