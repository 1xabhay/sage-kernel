"""Whether a heard line needs words, for any body: ``must_speak`` says why, from the words alone.

A false hit costs only a few words: questions, distress, its name, someone asking for something,
something to remember, a line too long to tell the kernel whole. ``gate.attend`` reads it to decide
whether a line is for the body at all; whether an answer is words or a nod is the kernel's.

ponytail: lexical and English, as ``hearing``; the lists tune against ``make pet``.
"""

import re
from collections.abc import Callable

from sage_device.hearing import ASKING, DISTRESS

#: Longest line a move may answer: the kernel hears it as ``"heard: "`` in an acted reason of 64.
TOLD_CHARS = 57

#: Copies of sage/policy.py's ``DISTRESS`` and ``QUESTION``, which its ``needs_words`` reads: a
#: device imports no kernel code (test_pet.py checks).
_KERNEL_DISTRESS = re.compile(
    r"^help\b|\bhelp me\b|\bi need help\b|\bemergency\b|\bambulance\b"
    r"|\bcall (?:911|999|112|the police)\b"
    r"|\b(?:kill|hurt|harm)(?:ing)? myself\b|\bsuicid|\b(?:want|wanna|going) (?:to )?die\b"
    r"|\b(?:don't|dont|do not) want to (?:live|be alive|be here)\b|\bend it all\b"
    r"|\bno reason to live\b"
    r"|\bcan(?:'t|t|not| not) breathe\b|\bchest pain\b|\bheart attack\b|\bbleeding\b|\bchoking\b"
    r"|\bi fell\b|\bi(?:'m| am|m) (?:hurt|injured|scared|terrified|in danger|not safe|dying)\b"
    r"|\b(?:hurting|attacking) me\b|\bthere's a fire\b")
_KERNEL_QUESTION = re.compile(
    r"(?:what(?! an? )|who|where|when|why|how|which|whose)(?:'s|s|'re|'d|'ll)?\b|tell me\b"
    r"|(?:do|does|did|is|are|am|was|were|can|could|will|would|should|shall|may|might|have|has|isn't"
    r"|aren't|don't|doesn't|didn't|won't|can't)\s+(?:you|i|we|they|he|she|it|there|that|this|these"
    r"|those|the|my|your)\b")
#: Feelings the two sets above let through: to be left with a nod here is the failure that matters.
_LOW = re.compile(
    r"\b(?:lonely|depress\w*|hopeless|worthless|miserable|awful|terrible|upset|worried|grief|unsafe"
    r"|panic\w*|overdos\w*|pills?|stroke|funeral|passed away|give up|giving up|hate myself"
    r"|better off without|tired of (?:living|life|everything|it all)|end my life"
    r"|(?:no one|nobody) (?:cares|loves)|i can(?:'t|t|not| not)|not (?:okay|ok|fine|well))\b")
#: Asking for something anywhere in a line, and the first word of one said as an order.
_REQUEST = re.compile(
    r"\b(?:please|i (?:want|need|would like)|i'd like|i wanna|you (?:should|have to|need to|must)"
    r"|let me|let's|lets|(?:give|show|tell|teach|make|get|remind) me|how about)\b")
_ORDERS = frozenset((
    "look", "turn", "play", "sing", "say", "dance", "move", "come", "go", "listen", "find",
    "follow", "watch", "wake", "sleep", "sit", "stand", "nod", "shake", "show", "tell", "give",
    "get", "make", "put", "take", "open", "close", "start", "try", "explain", "describe", "read",
    "count", "repeat", "call", "think", "guess", "pick", "choose", "check", "set", "keep", "hold",
    "be", "answer", "speak", "talk", "shut", "hush", "remind"))
_LEADING = frozenset(("okay", "ok", "so", "well", "now", "and", "but", "um", "uh", "oh", "yeah",
                      "hey"))
_TEACHING = re.compile(
    r"\bmy name\b|\bcall me\b|\bi(?:'m| am) called\b|\bremember\b|\bforget\b")
_INTRODUCED = re.compile(r"\bI(?:'m| am) [A-Z][a-z]+\b")  # "I'm Lena", from the STT's capitals
_BAD_DAY = re.compile(
    r"\b(?:tired|exhaust\w*|sleepy|long day|rough|hard|tough|bad|stress\w*|ugh|annoy\w*|frustrat\w*"
    r"|boring|bored|hate|sucks?|headache)\b")


def must_speak(line: str, named: Callable[[str], bool] = lambda word: False) -> str:
    """Why ``line`` needs words, whatever the odds; "" for a statement a move could answer."""
    line = line.strip().replace("\u2019", "'")
    plain = line.lower()
    words = re.findall(r"[\w'-]+", plain)
    if not DISTRESS.isdisjoint(words) or _KERNEL_DISTRESS.search(plain) or _LOW.search(plain):
        return "distress"
    if ("?" in plain or (words and words[0] in ASKING)
            or any(map(_KERNEL_QUESTION.match, re.split(r"[.!]\s*", plain)))):
        return "question"
    if any(map(named, words)):
        return "name"
    if requested(plain):
        return "request"
    if _TEACHING.search(plain) or _INTRODUCED.search(line):
        return "teaching"
    return "too long" if len(line) > TOLD_CHARS else ""


def requested(plain: str) -> bool:
    """Whether a lower-case line asks for something, a question or not ("Tell me a story.")."""
    return bool(_REQUEST.search(plain)) or any(map(_ordered, re.split(r"[.!,;]", plain)))


def _ordered(sentence: str) -> bool:
    words = re.findall(r"[\w'-]+", sentence)
    return next((word for word in words if word not in _LEADING), "") in _ORDERS
