"""The kernel's pure policy: one turn at a time per device, one memory of what was heard, effects and results.

No clock, file or network. Each step takes the state (and `now` when time matters) and returns
the next state with the journal rows that record the step; the kernel commits both in one
transaction. See docs/PROTOCOL.md. The pet's name, its wording and the word lists and patterns its language is
read with are data: sage/character.py reads them, once.
"""

import json
import math
import re
from dataclasses import asdict, dataclass, field, replace

from sage import character, manifest, speech

# The pet, as its character file has it (sage/character.json, or the file SAGE_CHARACTER names).
NAME, _PROMPT, _LINES, _NOTES = (character.CHARACTER[key] for key in ("name", "prompt", "lines", "templates"))
# How its language is read: patterns, word lists, and the names of days, months, seasons and parts of the day.
_RULES, _WORDS, _CALENDAR = (character.CHARACTER[key] for key in ("patterns", "words", "calendar"))
# The fixed prefix: a short character sheet, then in-character examples, then the body note. Byte for byte
# the same on every turn of a device, so Ollama reuses its cache (bench/LEDGER.md "kernel").
PERSONA = _PROMPT["persona"]
# A text body (manifest output): typed lines are never misheard, and no body stands in front of the person.
TEXT_PERSONA = _PROMPT["text_persona"]
TEXT_EXAMPLES = "\n".join((_PROMPT["examples_head"], *_PROMPT["text_examples"]))
STAY, TEXT_STAY = _PROMPT["stay"], _PROMPT["text_stay"]  # on every line, as it was sent (persona drift, cache)
# The lines silence must never answer, on any body, whatever the model does (owner, 2026-11-05).
# ponytail: patterns (the default file's are English), generous on purpose: a miss is a silent reply to someone in
# trouble, a false hit a few words. Add a classifier only if the replay shows real misses.
DISTRESS = re.compile(_RULES["distress"])
QUESTION = re.compile(_RULES["question"])
DISTRESS_LINE = _LINES["distress"]
QUESTION_LINE = _LINES["question"]
WATCHING_LINE = _LINES["watching"]  # the same last resort while watching along (§11)
# The last resort right after Sage said it did not know: it moves on, never a second "don't know", never silence
# on a question. ponytail: one fixed line (Bohus and Rudnicky 2005: moving on recovers more than asking again);
# the owner's ear decides its wording, and evals/companion.py counts how often the kernel had to say it.
MOVE_ON_LINE = _LINES["move_on"]
RESORTS = (QUESTION_LINE, WATCHING_LINE, MOVE_ON_LINE)  # the kernel's words, never shown to the model as its own
NEEDS_WORDS = _PROMPT["needs_words"]
# Exchanges kept in the prompt: short against persona drift and prefill; facts outlive them (MEMORY). Past WINDOW
# they are cut back to the last KEPT, in one block: the model server re-reads all that follows the first changed
# message, so a window sliding by one re-read the whole history every turn.
WINDOW, KEPT = 8, 5  # 8/5 beat 10/6 on the eval: 14, 14 failed checks against 16, 15; p95 first piece 2.0 s against 2.5 s
# Memory (design 3-memory, 2026-11-21): written after a conversation, from the lines said to Sage, never mid-turn.
# A conversation ends SESSION_IDLE_S after the last line said to Sage, or at SESSION_TURNS such lines.
# ponytail: both are guesses. The journal of 10-21 to 11-20 (5,428 heard lines, replayed through the body's gate
# without its confidence, face or TV signals) shows no knee in the gaps between answered lines: 183 over 3 min,
# 120 over 5, 72 over 10. Five minutes gives a median of 13 lines to a conversation, and 23 that pass 40.
# Measure again with `python3 evals/memory.py sessions <journal>` once the gate's marks are in the journal;
# SAGE_SESSION_IDLE_S overrides the first.
SESSION_IDLE_S, SESSION_TURNS = 300.0, 40
SPEAKERS = ("owner", "guest")  # who a body may say spoke (Sage-Speaker); anything else is unknown
KINDS = ("person", "like", "thread", "promise")  # what the writer may add; "day" is the kernel's, one a conversation
MAX_FACT = 120  # characters of one record
MAX_OPS = 5  # changes the writer may make after one conversation; ponytail: a guess, count the refused in `written` rows
# ponytail: a guess; one flat list in the checkpoint, rewritten on every commit. A table past roughly 500.
MAX_RECORDS = 200
RECALL_CHARS = 600  # the block at the end of the system prompt; ponytail: a guess, ~170 of the prompt's ~3k tokens
CORE_KNOWN = 5  # person and like records in that block
RECALL_EXTRA = 2  # records the block has no room for that may ride on the newest line, the best matches (`relevant`)
STALE_S = 14 * 86400.0  # a day line or an open thread older than this is dropped; ponytail: a guess
DAY_LINES = 7  # day lines kept, newest; only the last is in the block
READ_BACK = 5  # records read aloud when asked what it remembers
MEMORY, UNPLACED, NOTHING_YET = _PROMPT["memory"], _PROMPT["memory_unplaced"], _PROMPT["memory_none"]
# In place of the block on a guest's turn (owner, 2026-11-23: memory is the owner's; a guest cannot read it back).
# ponytail: wording unmeasured on any model; whatever the model makes of it, the records are not in its prompt.
WITHHELD = _PROMPT["memory_withheld"]
# A record that says the person's name: first in the block. ponytail: the word "name", English in the default
# character file; a "name" kind for the writer if a name is ever told without it.
NAMES = re.compile(_RULES["names"], re.IGNORECASE)
RECALLED, AS_KIND = _PROMPT["recalled"], _PROMPT["recall_kinds"]
WRITER = {key: _PROMPT[key] for key in ("writer", "writer_known", "writer_said", "writer_answered")}
FORGETS, ASKS_MEMORY = re.compile(_RULES["forgets"]), re.compile(_RULES["asks_memory"])
FORGET_THAT = frozenset(_WORDS["forget_that"])  # "forget that": nothing named, so only this conversation is let go
GRACE_S = 2.0
REPLY_TIMEOUT_S = 30.0
TIMING = ("stt_ms", "ttft_ms", "first_audio_ms")
STATUSES = ("done", "failed", "interrupted")
FIELDS = {"submitted": {"turn_id", "text", "timing", "input"}, "stop": {"turn_id"}, "acknowledged": {"turn_id"},
          "result": {"sequence", "turn_id", "status", "reason", "text", "timing", "duration_ms"},
          "connected": set(), "disconnected": set(), "presence": {"state", "confidence"},
          "notes": {"topic", "passages"}, "acted": {"act_id", "origin", "what", "input", "status", "reason", "said"}}
RATE, BURST = 10.0, 20.0
MAX_DURATION_MS = 600_000  # how long a device may say it spent performing one effect
# Replies to a device that a new reply must not repeat word for word. Only the same words are a repeat: of 373
# rejections in 96 h (journal 11-20), 151 were exact; the other 222 were the same four opening words (87 whole
# answers, 135 cut at the fourth word), each a second model call for a paraphrase.
RECENT = 3
OPENING_WORDS = 4  # words of a reply held while they could still open a claim of a missing sense (`may_claim`)
# The loops a reply must not go round twice, each with the note that says so (the character's) and, for the two
# it can be heard in, the sentences that are dropped if the model does it anyway (`repairs`, `moved_on`).
REPAIRS = {name: _PROMPT[name] for name in ("unclear_again", "unknown_again", "asked_last")}
ADMITS = re.compile(_RULES["admits"])  # "I don't know.", and the kernel's own last resort
ASKS_AGAIN = re.compile(_RULES["asks_again"])  # "Sorry, say that again?"
CLAUSE = re.compile(r"[,;:\u2014]|\bbut\b")  # where a sentence that admits may go on to answer (`moved_on`)
FILLER = frozenset(("", "sorry", "pardon", "hmm", "hm", "well", "honestly", "oh", "um", "uh", "huh", "what"))  # no answer
# Unclear lines in a row, the newest included, from which the answer is a move and the model is not asked.
# ponytail: a guess (the design's "third in a row"); score replies to unclear lines in evals/companion.py.
MAX_UNCLEAR = 3
LISTENING = "attentive"  # the feeling of that move, when the body lists it; else the first it lists
MAX_NOTES, MAX_PASSAGE, MAX_TOPIC = 64, 600, 200  # a song's worth of web; the body cap is the HTTP adapter's
DONE_TEXT = 120  # characters of a done result's text told to the model
NOTES_LINGER = 2  # turns the card stays on after notes arrive or last match: "When did it come out?" matches nothing
SURE = 0.9  # a device's confidence at or above which a heard line is never "(unclear)" (lead, v1.1)
MAX_ALTERNATIVES = 3  # other hearings of a line a device may send, shown beside an unclear line
WHAT = ("body_plan", "aside", "saw")  # what a body may report it did on its own (acted, v1.1; saw, v1.3)
MAX_SEEN = 200  # characters of what a body's camera saw: words only cross, never the image
MAX_ACTS = 4  # acts per body told before its next line, newest kept: ponytail: a body acting 10 times between lines loses 6
NOTES_SHOWN = 3  # best matches on the newest message, after the first: the live prompt is ~3k of 4096 tokens
# Watching together (§11): what is on nearby, as the body heard it. Warm, never a list of what it cannot do.
MAX_ABOUT, MAX_HEARD = 80, 6
WATCHING = _NOTES["watching"]
MOMENT = _NOTES["moment"]
NOTES = _NOTES["notes"]


class Refused(Exception):
    """A coded refusal (docs/PROTOCOL.md §7); the message says what to do next."""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


@dataclass(frozen=True)
class Row:
    """One journal row. An effect has a `target`: the device it is delivered to."""
    kind: str
    source: str
    turn_id: str | None = None
    body: dict = field(default_factory=dict)
    target: str | None = None
    sequence: int | None = None


@dataclass(frozen=True)
class Call:
    """A tool the model chose, why it failed if it did, and what it reported when done, if anything."""
    name: str
    input: object
    sequence: int | None = None  # the delivered effect, if it was delivered
    outcome: str = ""  # "" while running or when done
    done: str = ""  # a done result's text ("tracking you"): what the body is doing now (owner, 2026-10-22)


@dataclass(frozen=True)
class Act:
    """What a body did or said on its own, outside any turn (acted, v1.1), as the model is told it."""
    device: str
    act_id: str
    text: str


@dataclass(frozen=True)
class Exchange:
    user: str
    reply: str  # what was heard: cut to what was played when the reply was interrupted
    reply_sequence: int | None = None
    calls: tuple[Call, ...] = ()
    device: str = ""
    interrupted: bool = False  # noted to the model after its words, so it never continues a half sentence
    input: dict = field(default_factory=dict)  # how the line came in (submitted.input), if the device said
    acts: tuple[Act, ...] = ()  # what the body did on its own before this line, told just before it
    output: str = ""  # the model's raw answer, as it streamed: the bytes the model server has cached
    guest: bool = False  # the body said a guest spoke the line: answered with no memory in the prompt


@dataclass(frozen=True)
class Open:
    """A delivered tool or reply effect with no result yet: its target is acting."""
    sequence: int
    target: str
    turn_id: str
    kind: str
    deadline: float | None = None  # by the kernel's clock; None: no timeout


@dataclass(frozen=True)
class Turn:
    turn_id: str | None = None
    source: str | None = None
    status: str = "none"  # none | active | completed | cancelled | interrupted | failed | unknown
    text: str = ""
    timing: dict = field(default_factory=dict)
    input: dict = field(default_factory=dict)
    addressed: bool = False  # the body judged the line was said to Sage, or it was typed
    speaker: str = "unknown"  # owner | guest | unknown, as the body's voiceprint has it


@dataclass(frozen=True)
class Companion:
    turn: Turn = Turn()
    exchanges: tuple[Exchange, ...] = ()
    open: tuple[Open, ...] = ()
    quiet_until: float | None = None  # asked to be quiet: no proactive turn before this, by the kernel's clock
    facts: tuple[dict, ...] = ()  # records {id, who, kind, text, at, from}, in the order learned
    topic: str = ""  # what the notes are about; "" when there are none
    notes: tuple[str, ...] = ()  # passages a device found, most useful first
    notes_idle: int = 0  # turns since the notes arrived or last matched
    acts: tuple[Act, ...] = ()  # since each body's last exchange; its next one keeps that body's
    lanes: tuple[Turn, ...] = ()  # the other devices' turns; `turn` is the lane touched last (v1.2)
    spoke_at: float | None = None  # when the last line was said to Sage; None: no conversation is open
    said: int = 0  # lines said to Sage in the open conversation
    due: int = 0  # conversations closed and not yet read by the writer
    cursor: int = 0  # the journal sequence the writer has read through: nothing before it is read again


@dataclass(frozen=True)
class CompiledPrompt:
    """Exactly what the model receives for one decision, and exactly what the journal keeps.

    BORROW an earlier chatbot's `CompiledPrompt`: one value is both sent and stored, so every reply
    traces to what the model saw. `system` and `format` never change for a device, so the
    start of the prompt stays cacheable.
    """
    system: str
    context: tuple[dict, ...]  # chat messages after the system message
    format: dict  # the JSON Schema the answer is constrained to

    def messages(self) -> list[dict]:
        return [{"role": "system", "content": self.system}, *self.context]

    @property
    def max_reply(self) -> int:
        """The reply's cap in characters, as the decoder enforces it."""
        return self.format["properties"]["reply"]["maxLength"]


# Boundary checks ---------------------------------------------------------------------------

def _text(value, limit, nonblank=False) -> bool:
    return isinstance(value, str) and len(value) <= limit and (not nonblank or bool(value.strip()))


def check_timing(timing) -> dict:
    if timing is None:
        return {}
    if not isinstance(timing, dict) or any(key not in TIMING or type(value) is not int or value < 0
                                           for key, value in timing.items()):
        raise Refused("invalid", "timing holds only " + ", ".join(TIMING) + ", as whole milliseconds ≥ 0")
    return dict(timing)


def _media(value) -> bool:
    """Whether `value` is a well-formed input.media: {about?, heard?, moment?} (§11)."""
    return (isinstance(value, dict) and not set(value) - {"about", "heard", "moment"}
            and ("about" not in value or _text(value["about"], MAX_ABOUT))
            and ("heard" not in value or isinstance(value["heard"], list) and len(value["heard"]) <= MAX_HEARD
                 and all(_text(line, MAX_PASSAGE) for line in value["heard"]))
            and ("moment" not in value or type(value["moment"]) is bool))


def _input(value) -> bool:
    """Whether `value` is a well-formed submitted.input: {medium, confidence?, alternatives?, media?}."""
    return (isinstance(value, dict) and not set(value) - {"medium", "confidence", "alternatives", "media"}
            and ("media" not in value or _media(value["media"]))
            and value.get("medium") in ("speech", "text")
            and ("confidence" not in value or type(value["confidence"]) in (int, float) and 0 <= value["confidence"] <= 1)
            and ("alternatives" not in value or isinstance(value["alternatives"], list)
                 and len(value["alternatives"]) <= MAX_ALTERNATIVES
                 and all(_text(other, 4000, nonblank=True) for other in value["alternatives"])))


def check_event(data: dict) -> str:
    """The event's kind, if the event is well formed; Refused("invalid") otherwise."""
    if "source" in data:
        raise Refused("invalid", "Remove source: the kernel knows the device from its token")
    kind = data.get("kind")
    if kind not in FIELDS or set(data) - FIELDS[kind] - {"kind"}:
        raise Refused("invalid", "kind is one of " + ", ".join(FIELDS) + "; send only the fields it lists: "
                      + "; ".join(f"{k}: {', '.join(sorted(f)) or 'none'}" for k, f in FIELDS.items()))
    turn_id = data.get("turn_id")
    if not (turn_id is None and kind not in ("submitted", "acknowledged") or _text(turn_id, 128, nonblank=True)):
        raise Refused("invalid", f"{kind} needs a turn_id of 1 to 128 characters")
    if not (data.get("text") is None and kind != "submitted"
            or _text(data.get("text"), 4000, nonblank=kind == "submitted")):
        raise Refused("invalid", "text must be at most 4000 characters, and submitted needs some")
    if kind == "result" and (("sequence" in data) == ("turn_id" in data)
                             or ("sequence" in data and type(data["sequence"]) is not int)
                             or data.get("status") not in STATUSES):
        raise Refused("invalid", "result needs either the effect's sequence or a streaming reply's turn_id, "
                                 "and a status: " + ", ".join(STATUSES))
    if kind == "presence" and (data.get("state") not in ("seen", "lost") or type(data.get("confidence")) not in (int, float)
                               or not 0 <= data["confidence"] <= 1):
        raise Refused("invalid", "presence needs state (seen or lost) and confidence, a number from 0 to 1")
    if kind == "notes" and not (_text(data.get("topic"), MAX_TOPIC) and isinstance(data.get("passages"), list)
                                and len(data["passages"]) <= MAX_NOTES
                                and all(_text(p, MAX_PASSAGE) for p in data["passages"])):
        raise Refused("invalid", f"notes needs a topic of at most {MAX_TOPIC} characters and passages: a list of "
                                 f"at most {MAX_NOTES} texts of at most {MAX_PASSAGE} characters each")
    if kind == "submitted" and "input" in data and not _input(data["input"]):
        raise Refused("invalid", "input is {medium: speech or text, confidence?: a number from 0 to 1, alternatives?: "
                                 f"a list of at most {MAX_ALTERNATIVES} texts}}")
    if kind == "acted" and not (_text(data.get("act_id"), 128, nonblank=True) and _text(data.get("origin"), 64, nonblank=True)
                                and data.get("what") in WHAT and data.get("status") in STATUSES
                                and ("said" not in data or _text(data["said"], 4000))
                                and (data.get("what") != "saw" or _text(data.get("input"), MAX_SEEN, nonblank=True))):
        raise Refused("invalid", "acted needs act_id (1 to 128 characters), origin (1 to 64), what ("
                      + " or ".join(WHAT) + ") and status (" + ", ".join(STATUSES) + "); said is at most 4000 characters;"
                      f" saw needs input: what was seen, a text of at most {MAX_SEEN} characters")
    if "duration_ms" in data and not (type(data["duration_ms"]) is int and 0 <= data["duration_ms"] <= MAX_DURATION_MS):
        raise Refused("invalid", f"duration_ms must be whole milliseconds from 0 to {MAX_DURATION_MS}")
    if "reason" in data and not _text(data["reason"], 64):
        raise Refused("invalid", "reason must be text of at most 64 characters")
    check_timing(data.get("timing"))
    return kind


def take(bucket: tuple[float, float] | None, now: float) -> tuple[bool, tuple[float, float], float]:
    """A token bucket, RATE per second with bursts of BURST: (allowed, next bucket, retry_after seconds)."""
    tokens, at = bucket or (BURST, now)
    tokens = min(BURST, tokens + max(0.0, now - at) * RATE)
    if tokens >= 1:
        return True, (tokens - 1, now), 0.0
    return False, (tokens, now), max(0.001, round((1 - tokens) / RATE, 3))


def stale(recorded_at: float, now: float, fresh_s: float) -> bool:
    return now - recorded_at > fresh_s


def numbered(rows, first: int) -> tuple[Row, ...]:
    return tuple(replace(row, sequence=first + index) for index, row in enumerate(rows))


# Turns -------------------------------------------------------------------------------------

def focus(state: Companion, device: str) -> Companion:
    """`state` with `device`'s lane as `turn`, the one a step works on; the other lanes wait in `lanes`.
    With one device this is `state` itself, so a single body's state is exactly as before lanes."""
    if state.turn.source == device:
        return state
    own = next((t for t in state.lanes if t.source == device), Turn())
    rest = tuple(t for t in state.lanes if t.source != device) + ((state.turn,) if state.turn.source else ())
    return replace(state, turn=own, lanes=rest)


def lane(state: Companion, device: str) -> Turn:
    return focus(state, device).turn


def turns(state: Companion) -> tuple[Turn, ...]:
    return (state.turn, *state.lanes)


MONTHS, DAYS = tuple(_CALENDAR["months"]), tuple(_CALENDAR["days"])  # January first, Monday first
# ponytail: northern-hemisphere seasons by month; a SAGE_HEMISPHERE setting when a body lives south of the equator.
SEASONS = tuple(_CALENDAR["seasons"])
PARTS = tuple(_CALENDAR["day_parts"])  # morning, afternoon, evening, night
ASKS_TIME = re.compile(_RULES["asks_time"])
ASKS_DATE = re.compile(_RULES["asks_date"])
ASKS_DAY = re.compile(_RULES["asks_day"])


def _clock(dt) -> str:
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def situation(dt, place: str = "") -> str:
    """When a line was heard, as the model is told it: weekday, date, time, day part and season (and where)."""
    part = PARTS[3 if dt.hour < 5 else 0 if dt.hour < 12 else 1 if dt.hour < 17 else 2 if dt.hour < 21 else 3]
    when = f"{DAYS[dt.weekday()]}, {MONTHS[dt.month - 1]} {dt.day}, {dt.year}, {_clock(dt)}, {part}, {SEASONS[dt.month - 1]}"
    return when + (f", in {place}" if place else "")


def clock_answer(line: str, dt) -> str:
    """The exact answer to "what time / date / day is it", from the clock and never the model; "" otherwise."""
    plain = " ".join(re.sub(r"[^\w\s']", " ", line.lower()).split())
    if ASKS_TIME.search(plain):
        return _NOTES["clock_time"].format(time=_clock(dt))
    if ASKS_DATE.search(plain):
        return _NOTES["clock_date"].format(day=DAYS[dt.weekday()], month=MONTHS[dt.month - 1], date=dt.day)
    if ASKS_DAY.search(plain):
        return _NOTES["clock_day"].format(day=DAYS[dt.weekday()])
    return ""


def submit(state: Companion, source: str, turn_id: str, text: str, timing: dict, seen: bool, input: dict | None = None,
           when: str = "", addressed: bool = False, speaker: str = "unknown", now: float = 0.0):
    """`addressed` and `speaker` are the body's marks on the line (PROTOCOL.md §3): a line said to Sage opens
    or continues the conversation, and only such a line, not from a guest, is ever written to memory."""
    state = focus(state, source)
    if seen:
        return state, ()  # a retry after a lost response never starts a second turn
    if state.turn.status == "active":
        raise Refused("busy", "Another turn of this device is running. Retry the same turn_id shortly.")
    if state.turn.status == "unknown":
        raise Refused("unknown_pending", "A reply was interrupted by a restart. The device that started it "
                                         "must send acknowledged with its turn_id.")
    # `when` rides in the turn's input, so the exchange keeps it and the line is rebuilt byte for byte (cache).
    addressed = addressed or (input or {}).get("medium") == "text"  # typed to it: no TV types
    speaker = speaker if speaker in SPEAKERS else "unknown"
    closing = ()
    if addressed:
        if state.said >= SESSION_TURNS:  # a long conversation is written in pieces, and goes on
            state, closing = _close(state, "cap")
        state = replace(state, spoke_at=now, said=state.said + 1)
    turn = Turn(turn_id, source, "active", text, dict(timing), {**(input or {}), **({"when": when} if when else {})},
                addressed, speaker)
    kept = _unheard(input)
    marks = {**({"addressed": True} if addressed else {}), **({"speaker": speaker} if speaker != "unknown" else {})}
    return replace(state, turn=turn), closing + (
        Row("submitted", source, turn_id, {"text": text, "timing": timing, **({"input": kept} if kept else {}), **marks}),)


def _close(state: Companion, reason: str):
    """The conversation ends: the writer is owed its lines, and the window is emptied (at the cap, cut to KEPT)."""
    return (replace(state, exchanges=state.exchanges[-KEPT:] if reason == "cap" else (), spoke_at=None, said=0,
                    due=state.due + 1),
            (Row("session", "kernel", None, {"turns": state.said, "reason": reason}),))


def rest(state: Companion, now: float, idle_s: float = SESSION_IDLE_S):
    """Close the conversation once nothing was said to Sage for `idle_s`: the next one starts from memory, not
    from the last one's lines."""
    if state.spoke_at is None or now - state.spoke_at <= idle_s or any(t.status == "active" for t in turns(state)):
        return state, ()
    return _close(state, "idle")


def _cut(exchange: Exchange) -> list[dict]:
    """A barge-in, noted after the model's own words and never inside them: a note inside the answer
    was copied aloud as "(interrupted)" (live)."""
    if not exchange.interrupted:
        return []
    played = f'after: "{exchange.reply}"' if exchange.reply else "before you said anything"
    return [{"role": "tool", "content": f"(the person interrupted you {played})"}]


PROACTIVE = _PROMPT["proactive"]  # the note that starts a turn of the kernel's own, by its reason
TEXT_NOTES = {PROACTIVE[reason]: note for reason, note in _PROMPT["text_proactive"].items() if reason in PROACTIVE}


def _fitted(example: str, tools) -> str:
    """One of the character's examples with only the tool calls this body can perform: the character names
    feelings ("calming") that one body lists and another does not, and a small model copies what it is shown."""
    line, _, shown = example.partition(" -> ")
    answer = {name: value for name, value in json.loads(shown).items()
              if name == "reply" or not manifest.authorize(name, value, tools)}
    return f"{line} -> {json.dumps(answer, ensure_ascii=False)}"


def _examples(tools) -> str:
    # Positive examples only: small models echo what they see, including a list of phrases not to say.
    return "\n".join((_PROMPT["examples_head"], *(_fitted(example, tools) for example in _PROMPT["examples"])))


EXAMPLES = _examples(())  # for a body with no tools; read here so a character file with a broken example stops the start


def medium(declared: dict) -> tuple[str, str, dict | None]:
    """The wording for how the body puts out a reply (manifest output): persona, examples, and for a text body
    how kernel notes read instead (with no lines ever misheard); None for speech, whose notes read as they are.
    A speaking body is a companion whatever it can do with itself: taught "mostly a move", it answered 53% of
    its turns with no words (journal 11-20)."""
    if declared.get("output", {}).get("medium") == "text":
        return TEXT_PERSONA, TEXT_EXAMPLES, TEXT_NOTES
    return PERSONA, _examples(declared["tools"]), None


def start_proactive(state: Companion, device: str, turn_id: str, reason: str, when: str = ""):
    """A turn the kernel starts for `device` (docs/PROTOCOL.md §9): a note to the model, never the person's words."""
    state = focus(state, device)
    if state.turn.status in ("active", "unknown"):
        raise Refused("busy", "A turn is running; a proactive turn waits for it.")
    text = PROACTIVE[reason]
    return replace(state, turn=Turn(turn_id, device, "active", text, input={"when": when} if when else {})), (
        Row("proactive", "kernel", turn_id, {"reason": reason, "device": device, "text": text}),)


def _failures(exchange: Exchange) -> list[dict]:
    """What the body could not do, then what it reported done, as the body's own reports: tool messages,
    never the person's words. A done result without text is not reported."""
    failed = [f"could not do {call.name}: {call.outcome}" for call in exchange.calls if call.outcome]
    body = f"your {exchange.device} body" if exchange.device else "your body"  # older checkpoints name no device
    done = [f"{call.name} done: {call.done}" for call in exchange.calls if call.done]
    return ([{"role": "tool", "content": f"({body} " + "; ".join(failed) + ")"}] if failed else []) + (
        [{"role": "tool", "content": f"({body}: " + "; ".join(done) + ")"}] if done else [])


COMMON = set(_WORDS["common"])


def misheard(line: str, input: dict | None = None) -> bool:
    """Whether a line was only partly heard. Never when typed, a note of the kernel's own, or heard with
    confidence SURE or more. Otherwise: unsure (confidence below SURE), unfinished (no end mark), or repeating
    itself as song lyrics do. Who a line was said to is the body's to judge, before it sends one."""
    input = input or {}
    if line.startswith("[") or input.get("medium") == "text" or input.get("confidence", 0) >= SURE:
        return False
    said, end = re.findall(r"[\w']+", line.lower()), line.rstrip()[-1:]
    if not said or " ".join(said) in COMMON:
        return False
    repeating = any(w[i:i + n] == w[i + n:i + 2 * n] for w in (re.findall(r"[\w']+", part)
                                                            for part in re.split(r"[.!?]", line.lower()))
                    for n in (1, 2, 3) for i in range(len(w) - 2 * n + 1))  # within a sentence
    return end not in ".!?" or "confidence" in input or repeating


BACKCHANNEL = set(_WORDS["backchannel"])


def clips(tools) -> tuple[str, ...]:
    """The emote names a body declared; () when it declared none or left them open."""
    return tuple(next((t.get("inputSchema", {}).get("properties", {}).get("name", {}).get("enum", ())
                       for t in tools if t["name"] == "emote"), ()))


def unclear_streak(state: Companion, device: str) -> int:
    """How many lines in a row were unclear, the newest included, over the turns Sage spoke in."""
    turn = lane(state, device)
    lines = [(turn.text, turn.input), *((x.user, x.input) for x in reversed(state.exchanges)
                                        if x.device in (device, "") and x.reply.strip())]
    return next((n for n, (line, input) in enumerate(lines) if not misheard(line, input)), len(lines))


def repairs(state: Companion, device: str) -> tuple[str, ...]:
    """What the answer to `device`'s newest line must not do again, as names in REPAIRS: ask them to say it
    again (a second unclear line running), say it does not know (it just did), end on a question (it just did).
    Counted from what was said, so nothing is kept for it; none for a note of the kernel's own."""
    if lane(state, device).text.startswith("["):
        return ()
    last = _plain((last_replies(state, device) or ("",))[-1])
    return tuple(name for name, on in (("unclear_again", unclear_streak(state, device) >= 2),
                                       ("unknown_again", bool(ADMITS.search(last))),
                                       ("asked_last", last.rstrip().endswith("?"))) if on)


def moved_on(text: str, again, closed: bool = True) -> str:
    """`text` without the sentences that go round a loop again, whatever the note said: a second "I don't know"
    running, a second "say that again". While streaming (`closed` false) the sentence being written is held:
    it could still become one. Only a sentence that says nothing else is dropped: "I don't know much about cars,
    but the Civic is solid." is an answer, so every clause of it must go round the loop, filler aside.
    ponytail: patterns (the character's) over clauses cut at commas and "but"; a miss is one more "I don't know",
    never a lost answer. evals/companion.py on a journal counts both."""
    dropped = [loop for name, loop in (("unclear_again", ASKS_AGAIN), ("unknown_again", ADMITS)) if name in again]
    if not dropped:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    def loops(sentence: str) -> bool:
        plain = _plain(sentence).strip()
        clauses = [clause.strip() for clause in CLAUSE.split(plain) if clause.strip(" .!?") not in FILLER]
        return any(loop.search(plain) for loop in dropped) and all(
            any(loop.search(clause) for loop in dropped) for clause in clauses)
    return " ".join(sentence for sentence in (sentences if closed else sentences[:-1]) if not loops(sentence))


def move_only(state: Companion, device: str, tools) -> str:
    """The kernel's own answer to `device`'s newest heard line, as model output: a move and no words; "" to
    ask the model. A bare "yeah"/"okay" when Sage did not just ask, or one more unclear line after MAX_UNCLEAR - 1
    running: by then it has asked and guessed, and waits for a clear line. Never to distress.
    ponytail: a list of backchannels; the body's gate decides who a line was for before it is sent."""
    turn = lane(state, device)
    if turn.text.startswith("[") or turn.input.get("medium") != "speech" or DISTRESS.search(_plain(turn.text)):
        return ""
    words = set(_normal(turn.text).split())
    asked = (last_replies(state, device) or ("",))[-1].rstrip().endswith("?")  # "Yeah." to its question is an answer
    if not (words and words <= BACKCHANNEL and not asked or unclear_streak(state, device) >= MAX_UNCLEAR):
        return ""
    names, listed = [tool["name"] for tool in tools], clips(tools)
    feeling = LISTENING if not listed or LISTENING in listed else listed[0]
    return json.dumps({"reply": "", **({"emote": {"name": feeling}} if "emote" in names else {})})


def last_replies(state: Companion, device: str) -> tuple[str, ...]:
    """The replies heard on `device`, as said."""
    return tuple(x.reply for x in state.exchanges if x.device in (device, "") and x.reply.strip())[-RECENT:]


def needs_words(line: str, input: dict | None = None) -> str:
    """What to say if the model stays silent on `line`: a direct question or distress always gets words. "" when
    silence is fine. ponytail: a naive reading of questions ("How nice." counts); a false hit costs a few words."""
    if (input or {}).get("media", {}).get("moment"):
        return ""  # the TV's line, not the person's: a move is answer enough
    plain = _plain(line).strip()
    if plain.startswith("["):  # a kernel note
        return ""
    if DISTRESS.search(plain):
        return DISTRESS_LINE
    if (input or {}).get("medium") == "speech" and misheard(line, input):  # "How do you": cut off, not asked;
        return ""  # a garbled question is the model's to ask about, never the last resort's to wave away
    asked = "?" in plain or any(QUESTION.match(sentence.strip()) for sentence in re.split(r"[.!]", plain))
    return (WATCHING_LINE if (input or {}).get("media") else QUESTION_LINE) if asked else ""


# What a 4B model cannot know: what is fresh, what is out in the world, or a plain "look it up". A rule, not a
# second model call (~1 s each); ponytail: patterns, a classifier on labelled turns (Adaptive-RAG,
# arXiv 2403.14403) when the replay shows misses.
FRESH = re.compile(_RULES["fresh"], re.IGNORECASE)
# A request opens its sentence ("Can you look it up?"); "Google" inside a heard song line is none.
LOOK_UP = re.compile(_RULES["look_up"], re.IGNORECASE)
WHO_IS = re.compile(_RULES["who_is"])  # a name: "Who is Taylor Swift?"
# About Sage's own words or thoughts ("what were you talking about when you said..."): its history answers.
ABOUT_SAGE = re.compile(_RULES["about_self"], re.IGNORECASE)


def needs_search(line: str, input: dict | None, facts: tuple[str, ...]) -> str:  # `facts`: the records' texts
    """The query to ask the web for `line`, or "" when its answer needs no web. Never for a line that may be
    misheard: noise does not leave the Mac. Names the person told Sage (capitalized in a fact) are dropped:
    the line alone leaves, never memory or history."""
    if line.startswith("[") or misheard(line, input) or not (needs_words(line, input) or LOOK_UP.search(line)):
        return ""
    if ABOUT_SAGE.search(line) and not LOOK_UP.search(line):
        return ""
    about = ((input or {}).get("media") or {}).get("about", "")
    if not (FRESH.search(line) or LOOK_UP.search(line) or WHO_IS.search(line) or about):
        return ""
    names = {w.lower() for fact in facts for w in re.findall(r"(?<!^)(?<![.!?] )\b[A-Z][\w-]+", fact)}
    words = [w for w in line.split() if re.sub(r"('s|[^\w-])+$", "", w).lower() not in names]
    query = " ".join(words) + (f" {about}" if about else "")  # "Who's leading?" while a race is on
    return query if _terms(re.sub(_RULES["search_drop"], "", query, flags=re.IGNORECASE)) else ""


def _heard(line: str, input: dict | None = None) -> str:
    """The newest line as the model sees it: information, framed with what else it may have been when it
    was only partly heard. Only the newest: a history of "(unclear)" lines taught the model that all are."""
    if not misheard(line, input):
        return line
    others = ", ".join(f'"{other}"' for other in (input or {}).get("alternatives", ()))
    return f'heard (unclear): "{line}"' + (f" (or {others})" if others else "")


def act(state: Companion, source: str, data: dict):
    """A body's own act or aside (acted, v1.1): journaled, never a model call, and told to the model just
    before the next line, inside that line's exchange, so it takes no place in the window of its own."""
    known = state.acts + tuple(a for exchange in state.exchanges for a in exchange.acts)
    if any(a.device == source and a.act_id == data["act_id"] for a in known):
        return state, ()  # a retry after a lost response. ponytail: only acts still in memory are known

    def cut(value) -> str:
        return " ".join(str(value).split())[:DONE_TEXT]
    what = data["what"] + (" " + cut(json.dumps(data["input"], ensure_ascii=False)) if "input" in data else "")
    outcome = data["status"] + (f" ({cut(data['reason'])})" if data.get("reason") else "")
    said = f'; you said "{spoken}"' if (spoken := cut(data.get("said", ""))) else ""
    told = Act(source, data["act_id"], f"(your {source} body's camera saw: {cut(data['input'])})" if data["what"] == "saw"
               else f"(your {source} body, on its own ({cut(data['origin'])}): {what}: {outcome}{said})")
    mine, rest = _own(state.acts, source)
    return (replace(state, acts=rest + (mine + (told,))[-MAX_ACTS:]),
            (Row("acted", source, None, {k: v for k, v in data.items() if k != "kind"}),))


def _own(acts, device: str) -> tuple[tuple[Act, ...], tuple[Act, ...]]:
    """`device`'s acts, then every other body's: each body's are kept and cleared on their own."""
    return tuple(a for a in acts if a.device == device), tuple(a for a in acts if a.device != device)


def _acts(acts, device: str) -> list[dict]:
    """The acts of `device`'s own body: another body's are its own business, plan JSON and all."""
    return [{"role": "tool", "content": a.text} for a in acts if a.device == device]


def _narrated(exchange: Exchange) -> dict:
    """An exchange on another body, told in one line: its answer JSON, tools and reports would be echoed as
    this body's own (design §10 row 5). A guest's exchange likewise, on a turn that is not a guest's: in full it
    would read as the person's own line. ponytail: each side cut to DONE_TEXT; a summary if that loses too much."""
    def cut(text) -> str:
        return " ".join(text.split())[:DONE_TEXT]
    they = "" if exchange.user.startswith("[") else f'they said "{cut(exchange.user)}"; '  # [a kernel note]
    said = f'you said "{cut(exchange.reply)}"' if exchange.reply else "you said nothing"
    where = "a guest" if exchange.guest else f"on {exchange.device}"
    return {"role": "tool", "content": f"({where}) {they}{said}"}


def take_notes(state: Companion, source: str, topic: str, passages):
    """Notes replace the last ones; an empty topic with no passages clears them."""
    passages = tuple(passages)
    return (replace(state, topic=topic if passages else "", notes=passages, notes_idle=0),
            (Row("notes", source, None, {"topic": topic, "passages": list(passages)}),))


STOPWORDS = frozenset(_WORDS["stopwords"])


def _terms(text: str) -> list[str]:
    """Words for matching. ponytail: a crude suffix strip (produced ~ producer); a real stemmer if it misses."""
    return [re.sub(_RULES["suffix"], "", w) if len(w) > 4 else w
            for w in re.findall(r"\w+", text.lower()) if w not in STOPWORDS]


def relevant(passages: tuple[str, ...], query: str, k: int = NOTES_SHOWN) -> list[str]:
    """Up to `k` passages that match `query`, best by BM25, in their own order; none if nothing matches."""
    docs = [_terms(p) for p in passages]
    if not docs:
        return []
    asked, n, average = set(_terms(query)), len(docs), sum(map(len, docs)) / len(docs) or 1

    def score(doc):
        total = 0.0
        for term in asked & set(doc):
            df, f = sum(term in d for d in docs), doc.count(term)
            total += math.log(1 + (n - df + 0.5) / (df + 0.5)) * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * len(doc) / average))
        return total
    scores = [score(doc) for doc in docs]
    ranked = sorted(range(n), key=lambda i: -scores[i])[:k]  # stable: ties keep the most useful first
    return [passages[i] for i in sorted(i for i in ranked if scores[i] > 0)]  # never pad with what matched nothing


def _notes_match(state: Companion) -> tuple[list[str], bool]:
    """The passages after the card that match the newest line, and whether the line names the topic."""
    query = state.turn.text  # a line with no words of its own, "Where?", is read with the one before it
    if not _terms(query):
        query = state.exchanges[-1].user if state.exchanges else ""
    matched = relevant(state.notes[1:], query)
    return matched, bool(matched) or bool(set(_terms(state.topic)) & set(_terms(query)))


def _notes(state: Companion) -> str:
    """Notes only on a line they match or that names their topic (owner, 2026-10-22): shown on every line,
    they were answered from when the person talked of something else. For NOTES_LINGER turns after they
    arrive or last match, the card alone stays on, so "When did it come out?" still has the date."""
    if not state.topic:
        return ""
    matched, about = _notes_match(state)
    if about:
        shown = state.notes[:1] + tuple(matched or state.notes[1:1 + NOTES_SHOWN])
    elif state.notes_idle < NOTES_LINGER:
        shown = state.notes[:1]
    else:
        return ""
    return "\n\n" + NOTES.format(topic=state.topic) + "".join(f"\n- {p}" for p in shown)


def _watching(input: dict | None) -> str:
    """What is on nearby, on the newest line only (like notes): every line would carry the commentary."""
    media = (input or {}).get("media")
    if not media:
        return ""
    heard = "".join(f"\n- {line}" for line in media.get("heard", ()))
    return "\n\n" + WATCHING.format(about=media.get("about") or _PROMPT["on_screen"]) + heard


def _unheard(input: dict | None) -> dict | None:
    """`input` without what the body heard on the TV: this turn's context only, never kept (§11)."""
    if not input or "media" not in input:
        return input
    return {**input, "media": {k: v for k, v in input["media"].items() if k != "heard"}}


def told(exchange: Exchange) -> bool:
    """Whether the model is shown `exchange`: Sage spoke in it, or a tool of it failed, was refused or reported
    something. A turn it answered with no words is not conversation: 53% of its own past turns in the prompt
    were empty replies it then copied (journal 11-20). ponytail: a wordless turn's tool is not shown while it
    still runs ("What are you doing?" mid-song has no trace); show open calls if that is missed."""
    return bool(exchange.reply.strip()) or any(call.outcome or call.done for call in exchange.calls)


def _worth(exchanges, open) -> tuple[Exchange, ...]:
    """`exchanges` without those that tell nothing and never will: no words, and no tool still to report."""
    running = {effect.sequence for effect in open}
    return tuple(x for x in exchanges if told(x) or any(call.sequence in running for call in x.calls))


def windowed(exchanges: tuple[Exchange, ...], exchange: Exchange) -> tuple[Exchange, ...]:
    """`exchanges` with one more, if Sage spoke in it or chose a tool: the tool's result needs its exchange to
    settle on, and `_settle` lets the exchange go if that result tells nothing (`told`)."""
    if not (exchange.reply.strip() or exchange.calls):
        return exchanges
    exchanges += (exchange,)
    return exchanges if len(exchanges) <= WINDOW else exchanges[-KEPT:]


def compile_prompt(state: Companion, device: str, declared: dict, now: float | None = None) -> CompiledPrompt:
    """The prompt for the current turn of `device`, whose manifest is `declared`. With the time `now`, a day line
    or open thread gone stale is left out of what is remembered (`_fresh`).

    The body's senses and tools follow the persona, the same on every turn of that device. Another
    body's exchanges are one narrated line each. This body's past answers appear as the JSON the model gave, cut to what was heard, each followed by what its
    body could not do, if anything. Every message is rebuilt byte for byte as it was sent, reminder and
    the model's own answer included, so the model server's prompt cache holds (bench/LEDGER.md "kernel").
    What is said of the newest line alone is not kept, so the turn after re-reads from that line: its notes,
    its "(unclear)" mark and what its answer must not do again (`repairs`). An exchange the kernel's last
    resort answered is left out: those are not the model's words, and it would say them again as its own.
    """
    persona, examples, typed = medium(declared)
    # Memory is the owner's: a turn the body says a guest spoke is compiled with none of it, block or ride-along,
    # so no phrasing can read it back. A body that sends no mark cannot tell voices apart (no voiceprint
    # enrolled): every voice is its person's there, as it was before voiceprints, or the owner would lose
    # their own memory. ponytail: the model server re-reads the prompt after a guest's turn (the system text changed).
    # The same line runs through the conversation's earlier exchanges. A guest's turn holds only those a guest
    # spoke: every other was answered with memory in the prompt, so its reply may say what is remembered (a
    # read-back, an answer that used a record), and a guest's were themselves compiled from guests' alone.
    # A turn that is not a guest's holds a guest's exchange as one narrated line, never as its person's own.
    # ponytail: the owner's own line scored as a guest's is answered without the conversation so far; a body
    # unsure of the voice should send no mark rather than `guest` if that is heard too often.
    mine = state.turn.speaker != "guest"
    state = replace(state, facts=_fresh(state.facts, now) if mine else ())

    def framed(line: str, input: dict | None = None, newest: bool = False) -> str:
        when = f"[{input['when']}]\n" if input and input.get("when") else ""
        if (input or {}).get("media", {}).get("moment"):
            return when + MOMENT.format(line=line)
        return when + ((_heard(line, input) if newest else line) if typed is None else typed.get(line, line))
    context = []
    for exchange in state.exchanges:
        if exchange.reply in RESORTS or not told(exchange) or (not mine and not exchange.guest):
            continue
        # older checkpoints name no device: one body's, in full
        if exchange.device not in (device, "") or (mine and exchange.guest):
            context += [*_acts(exchange.acts, device), _narrated(exchange)]
            continue
        context += [*_acts(exchange.acts, device),
                    {"role": "user", "content": framed(exchange.user, exchange.input) + reminder(declared)},
                    {"role": "assistant", "content": _answer(exchange)}, *_cut(exchange), *_failures(exchange)]
    context += _acts(state.acts, device)
    context.append({"role": "user", "content": framed(state.turn.text, state.turn.input, True) + _watching(state.turn.input)
                    + _notes(state) + _recalled(state)
                    + reminder(declared) + "".join("\n\n" + REPAIRS[name] for name in repairs(state, device))})
    # Memory last: it changes only between conversations, and then the rest of the prefix stays cached.
    system = persona + "\n\n" + examples + "\n\n" + manifest.describe(device, declared) + "\n\n" + (
        recall(state.facts) if mine else WITHHELD)
    return CompiledPrompt(system, tuple(context), manifest.format_schema(declared["tools"], manifest.max_chars(declared)))


def _answer(exchange: Exchange) -> str:
    """The model's answer as it streamed when it says what was kept; re-serialized when cut or fixed."""
    answer = {"reply": exchange.reply, **{call.name: call.input for call in exchange.calls}}
    try:
        if json.loads(exchange.output) == answer:
            return exchange.output
    except ValueError:
        pass
    return json.dumps(answer)


def reminder(declared: dict) -> str:
    """A one-line reminder of the device's tools for the newest message (bench/LEDGER.md "kernel":
    without it, qwen3:1.7b at temperature 0.4 chose no emote in 12 turns; with one, 12 of 12)."""
    names = [tool["name"] for tool in declared["tools"]]
    # About the JSON, not about speech: "(You may add emote.)" was copied aloud as "(emote: )" and
    # "You may also use your emote tool." was said as the whole reply (live run 3; bench/LEDGER.md).
    stay = TEXT_STAY if declared.get("output", {}).get("medium") == "text" else STAY
    return "\n\n" + stay + ("\n" + _NOTES["fields"].format(names=", ".join(names)) if names else "")


# Repeats -----------------------------------------------------------------------------------

def _normal(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def recent_replies(state: Companion, device: str) -> tuple[str, ...]:
    """The last RECENT replies heard on `device`, normalised."""
    heard = [_normal(x.reply) for x in state.exchanges if x.device in (device, "") and x.reply.strip()]
    return tuple(heard[-RECENT:])


ASKS_TO_REPEAT = re.compile(_RULES["asks_to_repeat"])


def asks_to_repeat(line: str) -> bool:
    """Whether the person asks to hear the last reply again: then repeating it is the honest answer."""
    return bool(ASKS_TO_REPEAT.search(_plain(line).strip()))


def repeats(recent, text: str) -> bool:
    """Whether `text` says one of `recent` again, word for word."""
    return bool(said := _normal(text)) and said in recent


def may_repeat(recent, text: str) -> bool:
    """Whether a reply still streaming could turn out to repeat one of `recent` (so it is held back)."""
    said = _normal(text)
    return bool(said) and any(reply.startswith(said) for reply in recent)


def wordless(output: str) -> str:
    """The model's answer with its moves and no words, as model output: what a repeat becomes on a line
    that needs no words, instead of a second call that paraphrased the first."""
    return json.dumps({"reply": "", **dict(speech.parse_output(output)[1])})


def retry_prompt(prompt: CompiledPrompt, note: str) -> CompiledPrompt:
    """The same prompt, with a note on the newest message."""
    *rest, newest = prompt.context
    return replace(prompt, context=(*rest, {**newest, "content": newest["content"] + "\n\n" + note}))


# Senses ------------------------------------------------------------------------------------

# Narrow, first-person claims of each sense; idioms such as "I see what you mean" and "Let me see" pass.
CLAIMS = {sense: re.compile(claim) for sense, claim in _RULES["claims"].items()}
CAN = {"speech": "hear you", "vision": "see you", "touch": "feel touch"}


def _plain(text: str) -> str:
    return text.lower().replace("\u2019", "'")


def false_senses(text: str, senses) -> list[str]:
    """The senses the body did not declare that `text` claims, in a fixed order."""
    return [sense for sense, claim in CLAIMS.items() if sense not in senses and claim.search(_plain(text))]


def sense_note(claimed) -> str:
    verbs = " or ".join(manifest.SENSES[sense] for sense in claimed)
    return f"[This body cannot {verbs}. Do not say you can; say you cannot.]"


# Sage never claims to be sentient (owner rule): narrow, first-person claims of life, consciousness or feelings.
# "I'm not alive" and "I feel like a nap" pass. ponytail: patterns, and "I don't think I'm alive" is a
# false hit, put right by the same line; a classifier only if a replay shows misses.
ALIVE = re.compile(_RULES["alive"])
FORMING = re.compile(_RULES["forming"])  # could still become one
ALIVE_LINE = _LINES["alive"]
ALIVE_NOTE = _PROMPT["alive_note"]


def claims_life(text: str) -> bool:
    """Whether `text` says, in the first person, that Sage is alive, conscious or has feelings."""
    return bool(ALIVE.search(_plain(text)))


def honest(text: str, senses) -> str:
    """`text` with each sentence claiming a missing sense replaced, once, by what the body can do, and each
    claiming to be alive or to have feelings, once, by ALIVE_LINE."""
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        line = ""
        if claimed := false_senses(sentence, senses):
            can = [CAN[sense] for sense in CAN if sense in senses]
            line = (f"I can't {' or '.join(manifest.SENSES[s] for s in claimed)} yet"
                    + (f"; I can only {' and '.join(can)}." if can else "."))
        elif claims_life(sentence):
            line = ALIVE_LINE
        if line:
            sentence = "" if line in out else line
        out.append(sentence)
    return " ".join(filter(None, out))


def may_claim(text: str, senses) -> bool:
    """Whether a reply still streaming opens with what could be a claim of a missing sense, or holds or could
    still become a claim to be alive (so it is held)."""
    if claims_life(text) or FORMING.search(_plain(text).rstrip(" ,")):
        return True
    if not any(sense not in senses for sense in CLAIMS):
        return False
    said = re.findall(r"[a-z']+", _plain(text))
    if not said or said[0] not in _WORDS["claim_openers"]:
        return bool(false_senses(text, senses))
    return len(said) < OPENING_WORDS or bool(false_senses(text, senses))


def rejected(state: Companion, turn_id: str, prompt: CompiledPrompt, output: str, reason: str):
    """The journal row for an answer the kernel refused to deliver, while its turn still runs."""
    if state.turn.turn_id != turn_id or state.turn.status != "active":
        return ()
    return (Row("rejected", "kernel", turn_id, {"prompt": asdict(prompt), "output": output, "reason": reason}),)


def decided(state: Companion, turn_id: str, prompt: CompiledPrompt, output: str, tools, sequence: int, now: float,
            senses=None, streamed: str = "", model_timing: dict | None = None):
    """Record what the model answered and, if its turn is still running, deliver what it decided.

    `sequence` is the journal sequence the first row will get. Tool effects are listed just
    before the reply, all in one transaction.
    """
    turn = state.turn
    reply, calls, problem = speech.parse_output(output)
    text = speech.speakable(speech.whole_sentences(reply, prompt.max_reply), manifest.labels(tools), True, tuple(tool["name"] for tool in tools))
    if senses is not None:  # the last resort, after the guard's retry
        text = speech.whole_sentences(honest(text, senses), prompt.max_reply)  # the kernel's line may be the longer
    again = repairs(state, turn.source or "")
    text = moved_on(text, again)
    if not text.startswith(streamed):  # a last-resort fix would unsay what the device already played
        text = streamed  # ponytail: what was heard wins; the guards' streaming holds keep this rare
    record = Row("decided", "kernel", turn_id, {"prompt": asdict(prompt), "output": output, "problem": problem,
                                                **({"model_timing": model_timing} if model_timing else {})})
    if turn.turn_id != turn_id or turn.status != "active":
        return state, (record,)  # stopped or interrupted meanwhile: nothing more is delivered
    if not text:  # the last resort, after `_think`'s retry; itself a "don't know", so right after one it moves on
        resort = needs_words(turn.text, turn.input)
        resort = MOVE_ON_LINE if resort in RESORTS and "unknown_again" in again else resort
        text = speech.whole_sentences(resort, prompt.max_reply)
    rows, opened, recorded = [record], [], []
    for name, value in calls:
        if error := manifest.authorize(name, value, tools):
            recorded.append(Call(name, value, None, error))
            continue
        at = sequence + len(rows)
        rows.append(Row(name, "kernel", turn_id, {"input": value}, target=turn.source))
        max_s = next(tool.get("max_s") for tool in tools if tool["name"] == name)
        opened.append(Open(at, turn.source, turn_id, name, None if max_s is None else now + max_s + GRACE_S))
        recorded.append(Call(name, value, at))
    reply_sequence = None
    if text:
        reply_sequence = sequence + len(rows)
        rows.append(Row("reply", "kernel", turn_id, {"text": text}, target=turn.source))
        opened.append(Open(reply_sequence, turn.source, turn_id, "reply", now + REPLY_TIMEOUT_S))
    rows.append(Row("completed", "kernel", turn_id))
    mine, rest = _own(state.acts, turn.source)
    exchange = Exchange(turn.text, text, reply_sequence, tuple(recorded), turn.source, input=_unheard(turn.input),
                        acts=mine if text else (), output=output, guest=turn.speaker == "guest")  # with no words they wait in `acts`: told once
    idle = 0 if state.topic and _notes_match(state)[1] else state.notes_idle + 1  # judged before the turn is added
    # What the body did on its own is told with the next line Sage speaks to: an unkept exchange leaves it waiting.
    return replace(state, turn=replace(turn, status="completed", text=""), open=state.open + tuple(opened),
                   acts=rest if text else state.acts, exchanges=windowed(state.exchanges, exchange), notes_idle=idle), tuple(rows)


# Memory ------------------------------------------------------------------------------------

def _ranked(facts) -> tuple[list[dict], list[dict]]:
    """Records, most worth saying first, as (core, rest). Core: who they are and what they like (the owner's
    first, their name before the rest of who they are, that before likes; CORE_KNOWN at most), the last day
    line, and what is still open; newest first within each."""
    newest = sorted(facts, key=lambda r: -r["at"])
    known = sorted((r for r in newest if r["kind"] in ("person", "like")),  # a name never loses its place to likes
                   key=lambda r: (r["who"] != "owner", not NAMES.search(r["text"]), r["kind"] != "person"))
    days = [r for r in newest if r["kind"] == "day"]
    held = [r for r in newest if r["kind"] in ("thread", "promise")]
    return known[:CORE_KNOWN] + days[:1] + held, known[CORE_KNOWN:] + days[1:]


def _core(facts) -> list[dict]:
    """The records in the system prompt's block: as much of the core as fits RECALL_CHARS, heads included."""
    used, shown = len(MEMORY) + (len(UNPLACED) + 1 if any(r["who"] != "owner" for r in facts) else 0), []
    for record in _ranked(facts)[0]:
        if used + len(_told(record)) + 3 <= RECALL_CHARS:  # one too long never cuts the shorter ones after it
            used += len(_told(record)) + 3
            shown.append(record)
    return shown


def _told(record: dict) -> str:
    return AS_KIND.get(record["kind"], "{text}").replace("{text}", record["text"])


def recall(facts) -> str:
    """What Sage remembers, for the end of the system prompt: at most RECALL_CHARS. It is a function of the
    records alone, and they change only between conversations (the writer), or when the owner forgets or fixes
    one. What a voice it could not place said is listed apart: it is used, and nobody is greeted by name from it."""
    shown = _core(facts)
    mine = "".join(f"\n- {_told(r)}" for r in shown if r["who"] == "owner")
    other = "".join(f"\n- {_told(r)}" for r in shown if r["who"] != "owner")
    return MEMORY + (mine or ("" if other else "\n" + NOTHING_YET)) + (f"\n{UNPLACED}{other}" if other else "")


def _recalled(state: Companion) -> str:
    """Up to RECALL_EXTRA records that match the newest line and are not in the block, on that line only (like
    notes): however few there are, one the block left out is still found by asking for it. As in the block,
    what a voice it could not place said is listed apart, under the same note."""
    if state.turn.text.startswith("["):
        return ""
    shown = [r["id"] for r in _core(state.facts)]
    left = [r for r in state.facts if r["id"] not in shown]
    found = relevant(tuple(_told(r) for r in left), state.turn.text, RECALL_EXTRA)
    found = [r for r in left if _told(r) in found][:RECALL_EXTRA]
    return "".join("\n\n" + head + "".join(f"\n- {_told(r)}" for r in part)
                   for head, part in ((RECALLED, [r for r in found if r["who"] == "owner"]),
                                      (UNPLACED, [r for r in found if r["who"] != "owner"])) if part)


def _record(facts, who: str, kind: str, text: str, at: float, source: int | None) -> dict:
    # ponytail: the id after the highest in use, so the id of a dropped newest record is given again; a counter
    # in the checkpoint if `memory forget 7` ever hits a record made after the list was read.
    return {"id": max((r["id"] for r in facts), default=0) + 1, "who": who, "kind": kind, "text": text, "at": at,
            "from": source}


def _fresh(facts, now: float | None) -> tuple[dict, ...]:
    """`facts` without day lines and open threads past STALE_S at `now` (None: no clock, all of them). Read past
    here, in every prompt and read-back; dropped for good only where `_kept` runs."""
    return tuple(r for r in facts if now is None or not (r["kind"] in ("day", "thread") and now - r["at"] > STALE_S))


def _kept(facts, record: dict | None, now: float, source: str = "kernel"):
    """`facts` with `record`, if there is one, added or put in place of the one with its id; then without day
    lines and open threads past STALE_S, day lines past the newest DAY_LINES, and whatever is over MAX_RECORDS
    (never who the person is while anything else is left; oldest first). With them the rows to journal: the
    record's `remember`, from `source`, and a `forget` from the kernel for each one dropped, saying why
    ("old", "full"), so nothing leaves memory without a trace."""
    known = record and any(r["id"] == record["id"] for r in facts)
    facts = [record if r["id"] == record["id"] else r for r in facts] if known else [*facts, *([record] if record else ())]
    days = [r["id"] for r in facts if r["kind"] == "day"][:-DAY_LINES]
    kept = [r for r in _fresh(facts, now) if r["id"] not in days]
    old = [r for r in facts if r not in kept]
    while len(kept) > MAX_RECORDS:
        kept.remove(next((r for r in kept if r["kind"] != "person"), kept[0]))
    return tuple(kept), (*([Row("remember", source, None, record)] if record else ()),
                         *(Row("forget", "kernel", None, {"id": r["id"], "why": "old" if r in old else "full"})
                           for r in facts if r not in kept))


def records(facts) -> tuple[dict, ...]:
    """`facts` as records. Before records a fact was its text alone, about nobody in particular and from no
    line (an earlier checkpoint, a bench scenario): each is kept, as the owner's, since only the person's own
    lines were remembered then."""
    return tuple(fact if isinstance(fact, dict) else
                 {"id": n, "who": "owner", "kind": "person", "text": fact, "at": 0.0, "from": None}
                 for n, fact in enumerate(facts, 1))


def _clean(text) -> str:
    return " ".join(text.split())[:MAX_FACT] if isinstance(text, str) else ""


def memorable(body: dict) -> str:
    """Who a journaled `submitted` line may be remembered from: "owner", or "unknown" when the body could not
    tell (no voiceprint enrolled); "" for a line never written to memory: one not said to Sage, a guest's
    (owner, 2026-11-21: a guest lasts one conversation, overheard speech never), the TV's, or one maybe misheard."""
    input = body.get("input") or {}
    if (not body.get("addressed") or body.get("speaker") == "guest" or input.get("media", {}).get("moment")
            or misheard(body["text"], input)):
        return ""
    return "owner" if body.get("speaker") == "owner" else "unknown"


WRITER_FORMAT = {"type": "object", "required": ["operations", "day"], "additionalProperties": False, "properties": {
    "operations": {"type": "array", "maxItems": MAX_OPS, "items": {
        "type": "object", "required": ["op", "from"], "additionalProperties": False, "properties": {
            "op": {"enum": ["add", "update", "drop"]}, "id": {"type": "integer"}, "kind": {"enum": list(KINDS)},
            "text": {"type": "string", "maxLength": MAX_FACT}, "from": {"type": "integer"}}}},
    "day": {"type": "string", "maxLength": MAX_FACT}}}


# What one writer call may hold, so it fits the model's context beside its instructions (num_ctx 5120, about
# 3.5 characters a token): the records most likely to be changed, and each line and reply cut.
# ponytail: guesses; a spoken line is ~60 characters. `evals/memory.py writer` shows answers cut short.
WRITER_KNOWN, WRITER_LINE, WRITER_REPLY = 20, 160, 100


def writer_prompt(facts, lines) -> CompiledPrompt:
    """What the writer is asked after a conversation: the records the block shows and those that match what was
    said (WRITER_KNOWN at most), then `lines`, each (sequence, who, text, Sage's reply): only the lines
    `memorable` let through, so it cannot cite, or see, any other."""
    core = [r["id"] for r in _core(facts)]
    rest = tuple(r for r in facts if r["id"] not in core)
    near = relevant(tuple(r["text"] for r in rest), " ".join(text for _, _, text, _ in lines), WRITER_KNOWN - len(core))
    shown = [r for r in facts if r["id"] in core or r["text"] in near]
    known = "".join(f"\n[{r['id']}] {r['kind']}: {r['text']}" for r in shown) or "\n" + NOTHING_YET
    said = "".join(f"\n[{sequence}] {text[:WRITER_LINE]}"
                   + (f"\n  {WRITER['writer_answered']} {reply[:WRITER_REPLY]}" if reply else "")
                   for sequence, _, text, reply in lines)
    return CompiledPrompt(WRITER["writer"], ({"role": "user", "content": WRITER["writer_known"] + known + "\n\n"
                                              + WRITER["writer_said"] + said},), WRITER_FORMAT)


def written(state: Companion, end: int | None, lines, output: str | None, now: float):
    """Apply what the writer answered for the conversation that closed at journal sequence `end`. `lines` are
    the ones it was shown, (sequence, who, text, reply); `output` None: there were none, and it was not asked.
    An operation is refused unless it cites one of those lines, and a line from a voice the body could not place
    never changes what the owner said. Whatever the answer, the cursor moves past the conversation: it is never
    read again, so a dropped memory cannot grow back."""
    if end is None:  # nothing closed after the cursor: the count was stale (a spoken forget jumps the cursor)
        return replace(state, due=0), ()
    who_said = {sequence: who for sequence, who, _, _ in lines}
    try:
        answer = json.loads(output) if output else {}
    except ValueError:
        answer = {}
    answer = answer if isinstance(answer, dict) else {}
    operations = answer.get("operations") if isinstance(answer.get("operations"), list) else []
    facts, rows, refused = state.facts, [], []
    for n, op in enumerate(operations):
        op = op if isinstance(op, dict) else {}
        who, text = who_said.get(op.get("from")), _clean(op.get("text"))
        old = next((r for r in facts if r["id"] == op.get("id") and type(op.get("id")) is int), None)
        if n >= MAX_OPS:
            why = f"over {MAX_OPS} operations"
        elif who is None:
            why = "cites no line said to Sage in this conversation"
        elif op.get("op") == "add":
            why = ("needs a kind and a text" if op.get("kind") not in KINDS or not text
                   else "already known" if text.lower() in (r["text"].lower() for r in facts) else "")
        elif op.get("op") in ("update", "drop"):
            why = ("no such record" if old is None
                   else "not the owner's voice" if old["who"] == "owner" and who != "owner"
                   else "needs a text" if op["op"] == "update" and not text else "")
        else:
            why = "not add, update or drop"
        if why:
            refused.append({"operation": op, "why": why})
        elif op["op"] == "drop":
            facts = tuple(r for r in facts if r is not old)
            rows.append(Row("forget", "writer", None, {"id": old["id"], "from": op["from"]}))
        else:
            record = ({**old, "who": who if who == "owner" else old["who"], "text": text, "at": now, "from": op["from"]}
                      if op["op"] == "update" else _record(facts, who, op["kind"], text, now, op["from"]))
            facts, kept = _kept(facts, record, now, "writer")
            rows += kept
    if lines and (day := _clean(answer.get("day"))):
        record = _record(facts, "owner" if all(who == "owner" for who in who_said.values()) else "unknown", "day",
                         day, now, lines[0][0])
        facts, kept = _kept(facts, record, now, "writer")
        rows += kept
    facts, kept = _kept(facts, None, now)
    rows += [*kept, Row("written", "kernel", None, {"through": end, "output": output, "refused": refused})]
    return replace(state, facts=facts, cursor=max(state.cursor, end), due=max(0, state.due - 1)), tuple(rows)


def remembering(state: Companion, device: str, head: int, cap: int = manifest.MAX_REPLY, now: float | None = None):
    """The kernel's own answer when `device`'s newest line asks it to forget, or what it remembers: (state,
    rows, words), no model asked; words "" for any other line. Only for a line said to Sage, clearly heard, and
    not by a guest; a forget from any other line changes nothing and is answered that nothing was forgotten, and
    why: the model cannot forget, and may say it did. A guest who asks what it remembers is told it keeps that
    for its person (a short line of the owner's own can score as a guest's, so it asks for it once more). Forgetting drops every record that holds all the words
    named, and lets go of everything said up to now (`head`, the journal's newest row): the writer never reads
    it, so what was just said cannot come back as a memory. ponytail: patterns (the character's), English in the
    default file; "forget it" meaning "never mind" costs this conversation's memories, on the side of forgetting."""
    turn = lane(state, device)
    if turn.text.startswith("["):
        return state, (), ""
    plain = " ".join(re.sub(r"[^\w\s']", " ", _plain(turn.text)).split())
    found = FORGETS.search(plain)
    refused = ("kept_aside" if not turn.addressed else "kept_guest" if turn.speaker == "guest"
               else "kept_unclear" if misheard(turn.text, turn.input) else "")
    if refused:  # a guest asking what it remembers is told why not, in the kernel's words (the prompt holds none either)
        asks = turn.speaker == "guest" and ASKS_MEMORY.search(plain)
        return state, (), _LINES[refused] if found else _LINES["remembers_guest"] if asks else ""
    if found:
        named = set(_terms(" ".join(w for w in (found["what"] or "").split() if w not in FORGET_THAT)))
        gone = [r for r in state.facts if named and named <= set(_terms(r["text"]))]
        rows = tuple(Row("forget", "kernel", turn.turn_id, {"id": r["id"]}) for r in gone) + (
            Row("forget", "kernel", turn.turn_id, {"through": head}),)
        state = replace(state, facts=tuple(r for r in state.facts if r not in gone), cursor=max(state.cursor, head), due=0)
        return state, rows, _NOTES["forgot"].format(count=len(gone)) if gone else _LINES["forgot_none"]
    if not ASKS_MEMORY.search(plain):
        return state, (), ""
    texts = [r["text"] + ("" if r["text"][-1:] in ".!?" else ".")
             for part in _ranked(_fresh(state.facts, now)) for r in part]
    if not texts:
        return state, (), _LINES["remembers_none"]

    def aloud(said) -> str:
        more = len(texts) - len(said)
        return " ".join((_LINES["remembers"], *said, *([_NOTES["remembers_more"].format(count=more)] if more else ())))
    said = []
    for text in texts[:READ_BACK]:
        if len(aloud([*said, text])) > cap:
            break
        said.append(text)
    return state, (), aloud(said)


def amend(state: Companion, op: str, now: float, id: int | None = None, text: str = ""):
    """The owner's own change, from `python -m sage memory`: add a record, edit one's text, or forget one. A
    record the owner edits is the owner's from then on, whoever said it first. ValueError, saying why, when it
    cannot be done."""
    old = next((r for r in state.facts if r["id"] == id), None)
    text = _clean(text)
    if op != "add" and old is None:
        raise ValueError(f"no record {id}")
    if op != "forget" and not text:
        raise ValueError("it needs some text")
    if op == "forget":
        return replace(state, facts=tuple(r for r in state.facts if r is not old)), (Row("forget", "owner", None, {"id": id}),)
    record = ({**old, "who": "owner", "text": text, "at": now} if op == "edit"
              else _record(state.facts, "owner", "person", text, now, None))
    facts, rows = _kept(state.facts, record, now, "owner")
    return replace(state, facts=facts), rows


def failed(state: Companion, turn_id: str, prompt: CompiledPrompt, reason: str):
    row = Row("failed", "kernel", turn_id, {"prompt": asdict(prompt), "reason": reason})
    if state.turn.turn_id != turn_id or state.turn.status != "active":
        return state, (row,)
    return replace(state, turn=replace(state.turn, status="failed", text="")), (row,)


def restarted(state: Companion):
    def cut(turn: Turn) -> Turn:  # never replayed; its own device must acknowledge it
        return replace(turn, status="unknown") if turn.status == "active" else turn
    return (replace(state, turn=cut(state.turn), lanes=tuple(map(cut, state.lanes))),
            (Row("restarted", "kernel", state.turn.turn_id),))


def _others(state: Companion, turn_id: str, *statuses: str) -> bool:
    """Whether another device's lane holds `turn_id` (in one of `statuses`, if given)."""
    return any(t.turn_id == turn_id and (not statuses or t.status in statuses) for t in state.lanes)


def acknowledge(state: Companion, source: str, turn_id: str):
    state = focus(state, source)
    turn = state.turn
    if turn.turn_id is None or turn_id != turn.turn_id:
        if _others(state, turn_id):
            raise Refused("forbidden", "Only the device that started this turn may acknowledge it.")
        raise Refused("invalid", "That is not the current turn. Acknowledge the turn_id the kernel reports.")
    if turn.status != "unknown":
        return state, ()  # already clear: a retry changes nothing
    return replace(state, turn=replace(turn, status="none", text="")), (Row("acknowledged", source, turn_id),)


def stop(state: Companion, source: str, turn_id: str | None, streaming=()):
    """Cancel running turns and send one stop to each acting device. A stop needs no result.

    A bare stop is the owner's global stop: every running turn, every acting body. A stop naming a turn
    reaches only the sender's own turn and effects (v1.2), so one body's "stop" never freezes another.
    `streaming`: the devices with reply text to play; a running turn's device among them is acting too.
    The stop delivered names the turn it was for, so a body that has begun another does not stop that one.
    """
    if turn_id is not None:
        state = focus(state, source)
    mine = turns(state) if turn_id is None else (state.turn,)
    running = [t for t in mine if t.status == "active" and turn_id in (None, t.turn_id)]
    acting = {effect.target for effect in state.open
              if turn_id is None or (effect.target, effect.turn_id) == (source, turn_id)}
    if turn_id is not None and not running and not acting:
        raise Refused("stale", "That turn of yours is neither running nor being acted on; nothing was stopped.")
    acting |= {t.source for t in running if t.source in streaming}

    def cancel(turn: Turn) -> Turn:
        return replace(turn, status="cancelled", text="") if turn in running else turn
    rows = (Row("stop", source, turn_id),) + tuple(
        Row("stop", "kernel", turn_id, target=target) for target in sorted(acting))
    return replace(state, turn=cancel(state.turn), lanes=tuple(map(cancel, state.lanes))), rows


# Results -----------------------------------------------------------------------------------

def _outcome(status: str, text: str, reason: str) -> str:
    return "" if status == "done" else reason or text or status


def _settle(state: Companion, effect: Open, status: str, text: str = "", reason: str = "") -> Companion:
    def update(exchange: Exchange) -> Exchange:
        if exchange.reply_sequence == effect.sequence and status == "interrupted":
            return replace(exchange, reply=text, interrupted=True)  # memory keeps only what the person heard
        done = " ".join(text.split())[:DONE_TEXT] if status == "done" else ""
        return replace(exchange, calls=tuple(
            replace(call, outcome=_outcome(status, text, reason), done=done) if call.sequence == effect.sequence
            else call for call in exchange.calls))
    open = tuple(o for o in state.open if o is not effect)
    return replace(state, open=open, exchanges=_worth(map(update, state.exchanges), open))


def _lasted(state: Companion, turn_id: str, key: str, duration_ms: int | None) -> Companion:
    """An effect's duration_ms, kept in turn.timing.durations_ms under its sequence (or "reply" while streaming)."""
    if duration_ms is None or turn_id != state.turn.turn_id:
        return state
    durations = {**state.turn.timing.get("durations_ms", {}), key: duration_ms}
    return replace(state, turn=replace(state.turn, timing={**state.turn.timing, "durations_ms": durations}))


def _timed(state: Companion, turn_id: str, timing: dict) -> Companion:
    if timing and turn_id == state.turn.turn_id:
        return replace(state, turn=replace(state.turn, timing={**state.turn.timing, **timing}))
    return state


def report(state: Companion, source: str, effect: dict | None, status: str, text: str | None,
           reason: str | None, timing: dict, duration_ms: int | None = None):
    """A device's result for `effect`, the journal's record of a delivered effect (None if there is none)."""
    if effect is None or effect["kind"] == "stop":
        raise Refused("invalid", "That is not a delivered tool or reply effect. Report the effect's sequence.")
    if effect["target"] != source:
        raise Refused("forbidden", "That effect was delivered to another device; only it may report the result.")
    state = focus(state, source)
    waiting = next((o for o in state.open if o.sequence == effect["sequence"]), None)
    if waiting is None:
        return state, ()  # the first result counted; a retry changes nothing
    state = _timed(_settle(state, waiting, status, text or "", reason or ""), waiting.turn_id, timing)
    state = _lasted(state, waiting.turn_id, str(waiting.sequence), duration_ms)
    body = {"sequence": waiting.sequence, "status": status, "reason": reason, "text": text, "timing": timing,
            "duration_ms": duration_ms}
    return state, (Row("result", source, waiting.turn_id, body),)


def report_turn(state: Companion, source: str, turn_id: str, reply: dict | None, status: str, text: str | None,
                reason: str | None, timing: dict, duration_ms: int | None = None):
    """A result naming a turn: it ends a reply still streaming, or reports that turn's delivered `reply`."""
    state = focus(state, source)
    turn = state.turn
    if turn.turn_id == turn_id and turn.status == "active":
        heard = (text or "") if status == "interrupted" else ""
        mine, rest = _own(state.acts, turn.source)
        state = replace(state, turn=replace(turn, status="interrupted", text=""), acts=rest if heard else state.acts,
                        exchanges=windowed(state.exchanges, Exchange(turn.text, heard, device=turn.source,
                                                                     interrupted=status == "interrupted",
                                                                     input=_unheard(turn.input), acts=mine,
                                                                     guest=turn.speaker == "guest")))
        body = {"turn_id": turn_id, "status": status, "reason": reason, "text": text, "timing": timing,
                "duration_ms": duration_ms}
        # A streaming reply has no sequence yet, so its duration is kept under "reply" (lead).
        return _lasted(_timed(state, turn_id, timing), turn_id, "reply", duration_ms), (Row("result", source, turn_id, body),)
    if reply is not None:
        return report(state, source, reply, status, text, reason, timing, duration_ms)
    if turn.turn_id == turn_id and turn.status == "interrupted":
        return state, ()  # already ended by its first result
    if _others(state, turn_id, "active"):
        raise Refused("forbidden", "Only the device that started this turn may report its reply.")
    raise Refused("invalid", "No reply of yours is streaming or delivered for that turn_id.")


def sweep(state: Companion, now: float):
    """Fail every effect still without a result past its deadline: max_s + GRACE_S, or a reply's 30 s."""
    rows = []
    for effect in state.open:
        if effect.deadline is not None and now > effect.deadline:
            state = _settle(state, effect, "failed", reason="timeout")
            rows.append(Row("result", "kernel", effect.turn_id,
                            {"sequence": effect.sequence, "status": "failed", "reason": "timeout"}))
    return state, tuple(rows)


# Checkpoint --------------------------------------------------------------------------------

def encode(state: Companion) -> dict:
    return asdict(state)


def _call(data: dict) -> Call:
    """A call from a checkpoint. Before failure-only reports, outcomes read "<name>: <status>" and kept "done"."""
    outcome = data["outcome"].removeprefix(data["name"] + ": ")
    return Call(**{**data, "outcome": "" if outcome == "done" else outcome})


def decode(data: dict) -> Companion:
    # ponytail: formats are migrated here by shape; add a checkpoint version the next time one changes.
    # Before 0.2.0 an exchange also kept `repeat` (hidden from the model); it is dropped.
    open = tuple(Open(**o) for o in data["open"])
    return Companion(
        turn=Turn(**data["turn"]),
        exchanges=_worth((Exchange(**{**{k: v for k, v in x.items() if k != "repeat"},
                                      "calls": tuple(map(_call, x["calls"])),
                                      "acts": tuple(Act(**a) for a in x.get("acts", ()))}) for x in data["exchanges"]),
                         open),  # once, every turn answered with no words was kept
        open=open, quiet_until=data.get("quiet_until"),
        facts=records(data.get("facts", ())), topic=data.get("topic", ""), notes=tuple(data.get("notes", ())),
        notes_idle=data.get("notes_idle", 0), acts=tuple(Act(**a) for a in data.get("acts", ())),
        lanes=tuple(Turn(**t) for t in data.get("lanes", ())),  # none before v1.2: one lane, `turn`
        spoke_at=data.get("spoke_at"), said=data.get("said", 0), due=data.get("due", 0), cursor=data.get("cursor", 0))
