"""What a body does at once, before any words: pure decisions, with no body in them.

Abstract reactions go in and come out; which clip or antenna move shows one is the body's. A
reflex is never the kernel's and never speaks. A body says what it is doing (``speaking``,
``busy``), so these rules hold for any body: nothing over its own voice, or over a dance or a
search; a cooldown so it never reads as nervous; never the same clip twice in a row.
"""

import random
import re
from collections.abc import Callable, Sequence

GREET, PRAISED = "greet", "praised"  # said to it: answered by the body alone
LISTENING, PAUSE = "listening", "pause"  # a voice began; it paused
#: Silence that is a pause: too soon to know the turn is over, late enough to acknowledge.
PAUSE_S = 0.2
#: Seconds before a reaction may fire again. ponytail: first guesses, to tune live. A gesture's
#: outlasts the longest clip any body should answer with: a repeat lands on the one playing.
GESTURE_S = 6.0
COOLDOWN_S = {GREET: GESTURE_S, PRAISED: GESTURE_S, LISTENING: 2.0, PAUSE: 1.5}
#: What a gesture does now: plays, is swallowed (a repeat: no move, no turn), or passes to a turn.
FIRE, SWALLOW, PASS = "fire", "swallow", "pass"

_WORDS = {
    GREET: ("hi", "hello", "hey", "hiya", "howdy", "hi there", "hello there", "hey there",
            "good morning", "good afternoon", "good evening", "morning"),
    PRAISED: ("good girl", "good boy", "good robot", "good job", "good work", "great job",
              "nice job", "nice work", "well done", "thanks", "thank you", "thanks a lot",
              "thanks so much", "thank you so much", "thank you very much"),
}
_REACTIONS = {phrase: reaction for reaction, phrases in _WORDS.items() for phrase in phrases}


def reaction_to(heard: str, is_name: Callable[[str], bool] = lambda word: False) -> str | None:
    """Return the reaction a line is, whole, once the body's name is taken out; else None.

    Only a whole line: "hi, how are you" and "I said hi" are a turn's. "Good." alone is no
    praise: it answers a question.
    """
    # "here's age" is how the STT hears "hey Sage" (live 2026-10-23)
    words = re.sub(r"[^\w\s']", " ", heard.lower()).replace("here's age", "hey").split()
    return _REACTIONS.get(" ".join(word for word in words if not is_name(word)))


def fires(reaction: str, now: float, last_at: float, *, speaking: bool, busy: str | None) -> bool:
    """Whether a reaction may show now: nothing over the voice or a busy body, cooled down."""
    return not speaking and not busy and now - last_at >= COOLDOWN_S[reaction]


def gesture_phase(now: float, last_at: float, *, speaking: bool, busy: str | None) -> str:
    """Whether a gesture plays (``FIRE``), or ``SWALLOW``s a quick repeat, or ``PASS``es to a turn.

    The cooldown comes before ``busy``: what keeps the body busy may be the clip itself.
    """
    if speaking:
        return PASS
    if now - last_at < GESTURE_S:
        return SWALLOW
    return PASS if busy else FIRE


def pick(options: Sequence[str], last: str | None, rng: random.Random = random) -> str:
    """Pick among ``options``, never ``last`` while there is another."""
    return rng.choice([option for option in options if option != last] or options)
