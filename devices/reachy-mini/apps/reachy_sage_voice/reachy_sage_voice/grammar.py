"""The device grammar: a fast lane for a few commands, matched on the device before the kernel.

Only stop, stop tracking, the owner's commands (find me, track me, dance), turns
and looks, and recorded moves by voice ("show me happy", "do your groovy dance", "what moves
can you do?", ``expressions.py``) are here (owner, 2026-10-23): everything else is
understood by the kernel's model, which has the ``body_plan`` tool. Conservative
on purpose: every clause of the utterance must be a whole command, or the
utterance is the kernel's ("don't stop believing", "I love to dance"); only
chit-chat before the robot's name and a comma, filler ("okay, so"), and whole
sentences before a closing command sentence ("Bro, I'm here. Turn around.") are
skipped (``plan_for``). Clauses are split on "then", "and" and commas. The verbs
tolerate STT's known slips ("stopp", "Luke for me", "fist track"; a lone "top").
Anything matched is checked by ``plan.parse`` like a kernel plan.

``route`` is the intent router over it: a transcript is a ``Command``, a ``Song`` request, a
``Gesture``, a ``KernelTurn`` or a ``Drop``; a command, stop first, wins even while a song is
listened to.
The widened phrasings come from ``tests/data/commands_real.jsonl`` and its untracked local
twin (gates I1, I2).
"""

import re
from dataclasses import dataclass

from sage_device import reflex

from reachy_sage_voice.expressions import WORDS
from reachy_sage_voice.plan import Plan, parse
from reachy_sage_voice.song import asks_to_listen

#: Filler before a command. "here's age" is how the STT can hear "hey Sage".
_FILLER = re.compile(r"^(?:(?:hey|hi|ok|okay|so|alright|well|um|uh|hmm|oh|bro|now|then you"
                     r"|here's age) (?:, )?)+")
_LEAD = re.compile(r"^(?:(?:can|could|would|will) you )?(?:please )?")
#: The robot's names, and "Reachy" as the STT can hear it.
NAMES = frozenset({"sage", "reachy", "richie", "ritchie", "reachie", "ricci", "reechy", "vigi"})
#: English words within two edits of "reachy" that have its shape (r..., ending y, i or e).
_NOT_NAMES = frozenset({"ready", "really", "realty", "racy", "reamy", "reasy", "reccy", "rache"})


def _distance(a: str, b: str) -> int:
    """Levenshtein distance."""
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
    return row[-1]


def is_name(word: str) -> bool:
    """Whether ``word`` is the robot addressed: a name, or a near miss of "reachy" that is no word.

    ponytail: the shape rule and the short list keep ordinary words out ("ready",
    "reach", "peachy", "each"); a dictionary would, if the STT finds new ones.
    """
    return word in NAMES or (word[:1] == "r" and word[-1:] in ("y", "i", "e")
                             and word not in _NOT_NAMES and _distance(word, "reachy") <= 2)


def _strip_lead(words: str) -> str:
    words = _FILLER.sub("", words)
    first, _, rest = words.partition(" ")
    if is_name(first):
        words = _FILLER.sub("", rest.removeprefix(", "))
    return _LEAD.sub("", words)
#: A closing word or hint: "Then you find me, I'm behind you."
_TAIL = re.compile(r" ?,? ?\b(?:please|now|i'm (?:behind you|over here|right here|here))$")
_SPLIT = re.compile(r" ?, ?(?:and then |then |and )?| (?:and then|then|and) ")
_STOP = r"(?:stop|stopp|stap)"
_MUSIC = (r"(?: (?:to|with) (?:the|this|that|my|some) (?:music|song|beat|tune)"
          r"(?: (?:coming |playing )?(?:from|on|out of) (?:my|the) speakers?)?)?")
#: "face" as the STT hears it (live 2026-10-23: "Start fist track it"): only before "track".
_FACE = r"(?:face|fist|phase|faith)"
_BODY = r"(?: your (?:whole |lower )?body| yourself)?"
_FEELING = "|".join(sorted(map(re.escape, WORDS), key=len, reverse=True))


def _look(match: re.Match) -> str:
    said = next(filter(None, match.groups()))
    return {"at me": "me"}.get(said, said if said in ("left", "right", "up", "down") else "center")


#: Degrees of "turn left a little" (script C27).
LITTLE = 30


def _turn(match: re.Match) -> int:
    degrees = int(match["degrees"] or (LITTLE if match["little"] else 90))
    return degrees if match["side"] == "left" else -degrees


#: (pattern, skill), or (pattern, skill, arg): the arg is fixed, a function of the match, or
#: the pattern's ``arg`` group. A skill starting "_" is a preamble (``_plan``).
#: Each pattern must match a whole clause.
_COMMANDS = [
    (rf"{_STOP}(?: it| that| moving| everything| dancing"
     rf"| listening(?: to (?:the|this|that) (?:song|music))?)?|freeze|halt", "stop"),
    (rf"{_STOP} (?:(?:{_FACE}[ -])?tracking|following)(?: me| my face)?|{_STOP} looking at me"
     r"|(?:do not|don't) (?:follow|track) me",
     "stop_tracking"),
    (r"(?:(?:let's|lets|it's|its) )?(?:(?:look|luke) for (?:me|my face|it)"
     r"|(?:search for|find) (?:me|my face))(?: with (?:face )?tracking on)?|where am i",
     "search_face"),
    (rf"(?:start )?(?:face )?(?:track|tracking|follow|following) (?:me|my face)"
     rf"|start (?:face )?tracking|keep (?:looking at|watching|following) me"
     rf"|(?:start )?{_FACE} track(?:ing)?(?: me| it| my face)?", "track_face"),
    (r"look (?:to (?:the |your )?)?(left|right)|look (up|down)|look (at me)"
     r"|look (ahead|straight(?: ahead)?|forward)", "look", _look),
    (rf"turn{_BODY} around{_BODY}|(?:turn|rotate) your (?:whole |lower )?body", "turn", "around"),
    (rf"(?:turn|rotate){_BODY} (?:to(?:wards)? )?(?:the |your )?(?P<side>left|right)"
     r"(?: (?P<degrees>\d{1,3}) degrees| (?P<little>a little|a bit|slightly))?", "turn", _turn),
    (rf"(?:accordingly |now |just )?(?:start |go |let's |keep )?danc(?:e|ing)"
     rf"(?: accordingly| along)?"
     rf"(?: for (?:me|us)| with me)?{_MUSIC}(?: accordingly| to(?: it)?)?", "dance"),
    # "listen to the music and dance": the dance listens anyway (beat.py); alone, the kernel's
    (r"listen to (?:the|this|that|my) (?:music|song|beat|tune)(?: (?:that )?(?:i am|i'm) \w+)?",
     "_listen"),
    (rf"(?:show me|be|look|act|seem|get|make|do)(?: a| an| your| some)?(?: you're| you are)?"
     rf" (?P<arg>{_FEELING})(?: face| look| emotion| one)?", "emote"),
    (r"(?:do|show (?:me|us)) (?:your |the |a )?groov(?:y dance|e)", "dance_move", "groovy"),
    (r"(?:do|show (?:me|us)) (?:a |an |another |your |some )?"
     r"(?:little dance|(?:cool |little )?dance(?: (?:move|step)s?)?)", "dance_move", "any"),
    (r"what (?:moves|emotions|dances|tricks) (?:can you do|do you (?:know|have|do))"
     r"|list your moves", "moves"),
]
_COMMANDS = [(re.compile(p), skill, *arg) for p, skill, *arg in _COMMANDS]


def _step(clause: str) -> dict | None:
    for pattern, skill, *arg in _COMMANDS:
        if match := pattern.fullmatch(clause):
            given = (arg[0](match) if callable(arg[0]) else arg[0]) if arg else (
                match.groupdict().get("arg"))
            return {"skill": skill} | ({"arg": given} if given else {})
    return None


#: Words before the name that make it reported speech, or negate what follows ("I told Sage, ...").
_REPORTED = frozenset({"told", "tell", "tells", "telling", "said", "say", "says", "saying",
                       "asked", "ask", "asks", "asking"})
_NARRATIVE = _REPORTED | {"when", "if", "wish", "don't", "dont", "not", "never", "didn't",
                          "doesn't", "won't"}


def plan_for(text: str) -> Plan | None:
    """Return the plan ``text`` commands, or None when it is not (only) a fast-lane command.

    Chit-chat may come before the robot's name when a comma follows the name and the
    rest is whole commands ("I think you're sleepy sage, find me"), unless a word
    before it reports or negates ("I told Reachy, look for me").
    """
    if plan := _anchored(text):
        return plan
    # A command closing the utterance after a statement ("Hey, I'm right here. Turn
    # around."); not after a question ("Ready? Find me.") or reported speech
    # ("I told him. Stop.").
    *before, last = re.split(r"(?<=[.!?])\s+", text.strip())
    if before and before[-1].endswith((".", "!")) and _REPORTED.isdisjoint(
            re.findall(r"[\w']+", before[-1].lower())):
        return _anchored(last)
    return None


def _anchored(text: str) -> Plan | None:
    words = re.sub(r"[^\w\s,'-]", " ", text.lower()).replace(",", " , ")
    words = " ".join(words.split())
    if plan := _plan(words):
        return plan
    tokens = words.split(" ")
    for i in range(1, len(tokens) - 1):
        if not _NARRATIVE.isdisjoint(tokens[:i]):
            return None
        if tokens[i + 1] == "," and is_name(tokens[i]) and (plan := _plan(" ".join(tokens[i + 2:]))):
            return plan
    return None


def _plan(words: str) -> Plan | None:
    words = _TAIL.sub("", _strip_lead(words)).strip(" ,").replace("look over there , to", "look to")
    if words == "top":  # STT's slip on a lone "stop"; "on top of it" is not a command
        words = "stop"
    steps = []
    for clause in filter(None, _SPLIT.split(words)):
        step = _step(clause)
        if step is None:
            return None
        steps.append(step)
    skills = [step["skill"] for step in steps]
    if skills[-2:] == ["_listen", "dance"]:  # listening to the music is part of the dance
        del steps[-2]
    if skills == ["track_face", "search_face"]:  # "track me and look for me": find, then track
        steps.reverse()
    if not steps or any(step["skill"].startswith("_") for step in steps):
        return None
    plan = parse({"steps": steps}, device=True)
    return None if isinstance(plan, str) else plan


# --- the router ------------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    """Run ``plan`` on the body, now, without the kernel."""

    plan: Plan


@dataclass(frozen=True)
class Song:
    """Listen to the music and name it (``song.py``)."""


@dataclass(frozen=True)
class KernelTurn:
    """Talk: ``text`` is the kernel's."""

    text: str


@dataclass(frozen=True)
class Drop:
    """Not the person's words to act on; ``reason`` is logged."""

    reason: str


@dataclass(frozen=True)
class Gesture:
    """Greeted or praised: the body answers alone, no kernel, no words (``sage_device.reflex``)."""

    reaction: str


@dataclass(frozen=True)
class Context:
    """What the body can do now: commands, songs, whether it listens, and whether it may gesture."""

    body: bool = True
    song: bool = True
    listening: bool = False
    gesture: bool = False


def route(heard: str, context: Context) -> Command | Song | Gesture | KernelTurn | Drop:
    """Return where a (name-corrected, echo-trimmed) transcript goes.

    A command, stop first, always wins: even while a song is listened to, when the rest is the
    music.
    """
    if not heard.strip():
        return Drop("nothing heard")
    if context.body and (plan := plan_for(heard)):
        return Command(plan)
    if context.listening:
        return Drop("listening to a song")
    if context.gesture and (reaction := reflex.reaction_to(heard, is_name)):
        return Gesture(reaction)
    if context.song and asks_to_listen(heard):
        return Song()
    return KernelTurn(heard)
