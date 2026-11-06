"""Whether a transcript is worth answering, for any body: ``PASS``, ``UNSURE`` or ``QUIET``.

The STT writes something for every noise, and a confident answer to noise is worse than none.
``UNSURE`` is what a pet that did not catch you shows; what it looks like is the body's own.
Precision first: ignoring someone who spoke clearly is the failure that matters, so a line is
only doubted when it is a cut-off scrap, a lone word that answers nothing, or a stuck decoder.
Questions, exclamations, distress and the body's names are never doubted. Ported from the archived
pet's ``is_trustworthy``, without its phantom phrases (none in the logs) and counting repeats in a
row: its total counts doubted real talk that says "like" four times.

ponytail: lexical; STT confidence or n-best would do better, if the STT ever sends them.
"""

import math
import re
from collections.abc import Callable

PASS, UNSURE, QUIET = "pass", "unsure", "quiet"
#: At most one ``UNSURE`` this often, in seconds: others are ``QUIET``. Its motors can make the
#: next noise, and this bounds that loop. ponytail: tune live.
COOLDOWN_S = 8.0
#: Most words in a cut-off line still doubted: longer ones are mostly real speech the VAD clipped.
SCRAP_WORDS = 3
#: A phrase this many times in a row is a stuck decoder; people stop at three ("no, no, no").
MAX_REPEATS = 4
#: Words a finished sentence does not end on, even with a period: conjunctions, articles,
#: fillers. Not prepositions: "Turn the lights on." ends on a particle.
TRAILING = frozenset(("and", "but", "or", "because", "if", "the", "a", "an", "my", "your", "our",
                      "their", "um", "uh", "er", "erm", "hmm"))
#: Whole replies people really give: alone, or only these ("Yeah, yeah").
COMMON = frozenset((
    "hi", "hello", "hey", "yo", "okay", "ok", "yes", "yeah", "yep", "nope", "sure", "fine", "maybe",
    "thanks", "thank", "you", "bye", "good", "night", "morning", "right", "cool", "nice", "great",
    "wow", "oh", "hmm", "mm-hmm", "huh", "interesting", "exactly", "really", "please", "again"))
#: The first word of a question, finished or not.
ASKING = frozenset((
    "what", "what's", "why", "why's", "how", "how's", "who", "who's", "when", "when's", "where",
    "where's", "which", "do", "does", "did", "are", "is", "was", "were", "can", "could", "will",
    "would", "should", "shall", "have", "has", "may", "might", "am"))
#: Any of these and the line is heard, however it is cut: better a word too many than a cry missed.
DISTRESS = frozenset((
    "help", "stop", "no", "don't", "ouch", "ow", "hurt", "hurts", "hurting", "pain", "fire",
    "smoke", "emergency", "ambulance", "police", "doctor", "hospital", "die", "dying", "dead",
    "kill", "kills", "suicide", "scared", "afraid", "danger", "careful", "bleeding", "fell", "fall",
    "fallen", "sick", "breathe", "choking", "wait", "sad", "crying", "alone", "sorry", "wrong",
    "broken", "missing"))


def unfinished(text: str) -> bool:
    """Tell whether a transcript stops mid-sentence; the STT punctuates what it hears as finished.

    Unfinished: no closing ``. ? !``, or a closing word no sentence ends on ("and uh.").
    Empty text is not unfinished: there is nothing to wait for.
    """
    text = text.strip()
    words = re.findall(r"[a-z']+", text.lower())
    if not words:
        return False
    return text[-1] not in ".?!" or words[-1] in TRAILING


def _stuck(words: list[str]) -> bool:
    """Whether a phrase of one to three words comes ``MAX_REPEATS`` times in a row."""
    return any(words[i:i + n] * MAX_REPEATS == words[i:i + n * MAX_REPEATS]
               for n in (1, 2, 3) for i in range(len(words) - n * MAX_REPEATS + 1))


def trustworthy(line: str, named: Callable[[str], bool] = lambda word: False) -> bool:
    """Whether ``line`` is worth answering; ``named(word)``: the word is one of the body's names."""
    words = re.findall(r"[\w'-]+", line.lower().replace("\u2019", "'"))
    if (not words or line.rstrip().endswith(("?", "!")) or not DISTRESS.isdisjoint(words)
            or any(map(named, words))):
        return True
    if _stuck(words):
        return False
    if words[0] in ASKING or COMMON.issuperset(words):
        return True
    return not (len(words) == 1 or (len(words) <= SCRAP_WORDS and unfinished(line)))


def decide(line: str, since_unsure: float = math.inf, busy: bool = False, asked: bool = False,
           named: Callable[[str], bool] = lambda word: False) -> str:
    """Return what the body does with ``line``: ``PASS`` it on, show ``UNSURE``, or stay ``QUIET``.

    ``since_unsure``: seconds since it last showed it. ``busy``: voice or body has something else
    to do. ``asked``: it just asked a question, so whatever comes back is the answer.
    """
    if asked or trustworthy(line, named):
        return PASS
    return QUIET if busy or since_unsure < COOLDOWN_S else UNSURE
