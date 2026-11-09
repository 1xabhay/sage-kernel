"""Is it a pet yet? The owner's table for one session, from its bot log: any body, stdlib only.

A body passes ``report`` or ``main`` a mapping of kind to compiled pattern (``compile_patterns``),
keeping what its own log lines look like in its own package. Each is matched, from its start, on
``<logger>:<message>`` of a stamped line (latency.FORMAT) or on what follows the second ``|`` of a
Pipecat one (``<wall> | LEVEL | <text>``). Anchor them: a transcript can say anything.

The log line contract, by kind:

    session       once at startup; groups build, config. Its presence promises every kind the body
                  declares except arrived and left, so a kind's absence is then a real 0
    transcript    a non-empty STT result; group text
    stt_empty     an empty STT result
    voice_on      the VAD heard a voice start
    voice_off     the VAD heard a voice stop
    bot_on        audio started (Pipecat's line; latency.AUDIO is sage.py's, for latency.py)
    bot_off       audio stopped
    turn          a person's turn began: latency.START, stamped with its turn id; groups lane, ago
    initiated     a turn the kernel started; stamped with its turn id
    say           a clause sent to the voice; group text. Unstamped: joins the turn before it
    emote         a recorded move began; stamped
    reply_empty   the model chose to say nothing; stamped
    reaction      a move answering the person began (nod, cue, glance, emote); stamped
    link_drop     the link to the body was lost
    kernel_down   the kernel did not answer
    voice_silent  a clause or reply went unspoken
    sudden_move   the body made a sudden move
    motion_gates  once a minute from the motion writer; groups ticks, hz, clamped, still (seconds
                  the target held unchanged). Rate from minutes of at least MIN_GATE_TICKS ticks
    overheard     a line it kept out of, as not meant for it
    unsure        a line it did not catch: a tilt or quiet, and no turn
    arrived, left presence edges, when known: a body with no presence never writes them

A kind neither seen nor promised reads "not logged", never 0. Nothing anyone said reaches the
table.

Barge-ins are counted, with no target: an implicit signal is not a reward (a third have no usable
speech, a third come in the first second: echo and turn-end error as much as interest).
"""

import argparse
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sage_device import latency

NOT_LOGGED = "not logged"
#: A Pipecat line, which goes through loguru: wall time, no monotonic time, no turn id.
PIPECAT = re.compile(r"(?P<wall>\S+ \S+) \| \w+ +\| (?P<text>.*)")
FIRST_SECOND_S = 1.0
MIN_GATE_TICKS = 500  # a minute the writer barely ran (10 s at 50 Hz) says nothing of its rate


@dataclass(frozen=True)
class Metric:
    """A row: its label, its value's unit, and its target's bounds."""

    label: str
    unit: str = ""
    low: float | None = None
    high: float | None = None

    @property
    def target(self) -> str:
        """Show the bounds; "-" for a row that only counts."""
        if self.low is not None and self.high is not None:
            return f"{self.low:g}-{self.high:g}{self.unit}"
        if self.high is not None:
            return f"<={self.high:g}{self.unit}"
        return f">={self.low:g}{self.unit}" if self.low is not None else "-"

    def result(self, value: float) -> str:
        """Say "pass" or "fail" against the bounds; "-" for a row that only counts."""
        if self.low is None and self.high is None:
            return "-"
        low, high = self.low, self.high
        passed = (low is None or value >= low) and (high is None or value <= high)
        return "pass" if passed else "fail"


#: Targets: PET-BEHAVIOUR's table, its rule 3 for the reaction, and LAYERS.md E1's gate for speech.
METRICS = {
    "replied": Metric("Replied per transcript", "%", high=50),
    "replied_misheard": Metric("Replied, misheard lines", "%", high=25),
    "question": Metric("Ends in question", "%", high=10),
    "name": Metric("Uses name", "%", high=5),
    "median_words": Metric("Median words", high=4),
    "longest_words": Metric("Longest, words", high=12),
    "longest_s": Metric("Longest, seconds", " s", high=4),
    "initiations": Metric("Initiations", "/h", low=1, high=3),
    "movement_only": Metric("Movement-only turns", "%", low=40),
    "chosen_silence": Metric("Chosen silence", "%", low=20),
    "reaction": Metric("Voice end to reaction, median", " ms", high=300),
    "speech_p50": Metric("Voice end to speech, p50", " s", high=1.5),
    "speech_p95": Metric("Voice end to speech, p95", " s", high=3),
    "talk_over": Metric("Speech begun over a voice", "%", high=5),
    # Watching, a voice may be the TV's or the person's: unknowable today, so apart, no target.
    "talk_over_watching": Metric("Over a voice, watching (TV/person)", "%"),
    "overheard": Metric("Overheard, no words"),
    "unsure": Metric("Unsure: a tilt or quiet, no turn"),
    "sudden_moves": Metric("Sudden moves"),
    "motion_hz": Metric("Motion rate, worst minute", " Hz", low=40),
    "clamped": Metric("Clamped targets", high=0),
    "still_max": Metric("Longest still, awake", " s", high=5),
    "link_drops": Metric("Link drops"),
    "kernel_unreachable": Metric("Kernel unreachable"),
    "voice_silent": Metric("Silent-voice events"),
    "arrived": Metric("Arrivals"),
    "left": Metric("Departures"),
    "barge_usable_first": Metric("Barge-ins: speech, first second"),
    "barge_usable_later": Metric("Barge-ins: speech, later"),
    "barge_empty_first": Metric("Barge-ins: no speech, first second"),
    "barge_empty_later": Metric("Barge-ins: no speech, later"),
}


@dataclass(frozen=True)
class Row:
    """What the table prints for a metric."""

    label: str
    value: str
    target: str
    result: str


@dataclass
class _Turn:
    at: float
    ended: float | None  # the voice's end; None for a turn nobody spoke before
    said: list[str] = field(default_factory=list)
    moved: bool = False
    silent: bool = False


@dataclass
class _Session:
    seen: Counter = field(default_factory=Counter)
    begun: float | None = None
    ended: float | None = None
    build: str | None = None
    config: str | None = None
    heard: list[list] = field(default_factory=list)  # [misheard, its turn]
    turns: list[_Turn] = field(default_factory=list)  # the person's
    spoken: list[_Turn] = field(default_factory=list)  # any turn that has spoken
    reactions: list[float] = field(default_factory=list)
    spans: list[float] = field(default_factory=list)  # seconds of each stretch of speech
    barges: list[list[bool]] = field(default_factory=list)  # [usable, in the first second]
    over_voice: int = 0  # starts of audio with a voice going, not watching
    over_voice_watching: list[bool] = field(default_factory=list)  # each start while watching
    gates: list[tuple[int, float, int, float]] = field(default_factory=list)  # each minute's


def compile_patterns(patterns: dict[str, str]) -> dict[str, re.Pattern]:
    """Make a body's patterns ready for ``report``."""
    return {kind: re.compile(pattern) for kind, pattern in patterns.items()}


def misheard(line: str) -> bool:
    """Copy sage/policy.py's ``misheard``: a device imports no kernel code (test_pet.py checks)."""
    said, end = re.findall(r"[\w']+", line.lower()), line.rstrip()[-1:]
    if not said or " ".join(said) in _COMMON:
        return False
    sentences = (re.findall(r"[\w']+", part) for part in re.split(r"[.!?]", line.lower()))
    repeating = any(w[i:i + n] == w[i + n:i + 2 * n] for w in sentences
                    for n in (1, 2, 3) for i in range(len(w) - 2 * n + 1))
    return end not in ".!?" or (len(said) <= 4 and end != "?") or repeating


_COMMON = {"hi", "hello", "hey", "hi there", "hello there", "okay", "ok", "yes", "no", "yeah",
           "nope", "thanks", "thank you", "bye", "good night", "good morning"}


def _split(line: str) -> tuple[float, str | None, str] | None:
    """Split a line into wall time, turn id (Pipecat's have none) and text; None if no log line."""
    if m := latency.LINE.match(line):
        stamp, turn, text = m["wall"], m["turn"], f"{m['name']}:{m['message']}"
    elif m := PIPECAT.match(line):
        stamp, turn, text = m["wall"], None, m["text"]
    else:
        return None
    try:
        wall = datetime.strptime(stamp.replace(",", "."), "%Y-%m-%d %H:%M:%S.%f")  # noqa: DTZ007
        return wall.timestamp(), turn, text
    except ValueError:
        return None


def _scan(lines: list[str], patterns: dict[str, re.Pattern]) -> _Session:
    """Fold the log's lines, in order, into a session."""
    s = _Session()
    by_id: dict[str, _Turn] = {}
    # The turn in hand, a transcript with no turn yet, a barge-in with no STT, the audio's start.
    current = awaiting = barge = bot_on = None
    voice = watching = False
    for line in lines:
        parts = _split(line)
        if parts is None:
            continue
        t, turn_id, text = parts
        s.begun, s.ended = t if s.begun is None else s.begun, t
        hits = ((k, hit) for k, p in patterns.items() if (hit := p.match(text)))
        kind, m = next(hits, (None, None))
        if kind is None:
            continue
        s.seen[kind] += 1
        here = by_id.get(turn_id, current)
        if kind == "session":
            s.build, s.config = m["build"], m["config"]
        elif kind == "turn":
            ended = None if m["ago"] == "-" else t - int(m["ago"]) / 1000
            current = by_id[turn_id] = _Turn(t, ended)
            s.turns.append(current)
            if awaiting:
                awaiting[1] = current
                awaiting = None
        elif kind == "initiated":
            current = by_id[turn_id] = _Turn(t, None)
        elif kind == "transcript":
            # ponytail: only the last transcript before a turn joins it, so a held fragment reads
            # as unanswered; holds are short and rare. Dropped lines stay unanswered, rightly.
            awaiting = [misheard(m["text"]), None]
            s.heard.append(awaiting)
            if barge:
                barge[0], barge = True, None
        elif kind == "stt_empty":
            barge = None
        elif kind == "voice_off":
            voice = False
        elif kind == "voice_on":
            voice, barge = True, None
            if bot_on is not None:
                barge = [False, t - bot_on < FIRST_SECOND_S]
                s.barges.append(barge)
        elif kind in ("watching", "watched"):
            watching = kind == "watching"
        elif kind == "bot_on":
            bot_on = t
            if watching:
                s.over_voice_watching.append(voice)
            else:
                s.over_voice += voice
        elif kind == "bot_off" and bot_on is not None:
            s.spans.append(t - bot_on)
            bot_on = None
        elif kind == "say" and here is not None:
            if not here.said:
                s.spoken.append(here)
            here.said.append(m["text"])
        elif kind == "emote" and here is not None:
            here.moved = True
            s.reactions.append(t)
        elif kind == "reply_empty" and here is not None:
            here.silent = True
        elif kind == "reaction":
            s.reactions.append(t)
        elif kind == "motion_gates":
            s.gates.append((int(m["ticks"]), float(m["hz"]), int(m["clamped"]), float(m["still"])))
    return s


def _rate(hits: int, of: int) -> tuple[float, str] | None:
    return (round(100 * hits / of), f" ({hits}/{of})") if of else None


def _reactions(s: _Session) -> tuple[float, str] | None:
    """Median from the voice's end to the first move after it, before the next turn began."""
    took = []
    with_end = [turn for turn in s.turns if turn.ended is not None]
    for turn in with_end:
        until = min((other.at for other in s.turns if other.at > turn.at), default=float("inf"))
        first = min((r for r in s.reactions if turn.ended <= r < until), default=None)
        if first is not None:
            took.append(1000 * (first - turn.ended))
    return (round(statistics.median(took)), f" ({len(took)}/{len(with_end)})") if took else None


def _values(lines: list[str], s: _Session, patterns: dict, name: str | None) -> dict:
    """Each metric's (number, note); None where the log cannot say."""
    def known(*kinds: str) -> bool:
        return all(s.seen[k] or (s.seen["session"] and k in patterns) for k in kinds)

    def count(kind: str) -> tuple[float, str] | None:
        return (s.seen[kind], "") if known(kind) else None

    replies = [" ".join(turn.said) for turn in s.spoken]
    words = [len(reply.split()) for reply in replies]
    answered = [bool(turn and turn.said) for _, turn in s.heard]
    heard = known("transcript", "turn")
    speech = latency.summary(latency.turns(lines)).get("kernel", {"n": 0})
    misheards = [turn for was, turn in s.heard if was]
    named = None
    if name:  # its written and spoken forms: the voice's lines hold the spoken one
        named = re.compile(r"\b(" + "|".join(map(re.escape, name.split("|"))) + r")\b", re.I)
    hours = ((s.ended - s.begun) / 3600) if s.seen and s.ended and s.ended > s.begun else None
    barges = Counter((usable, first) for usable, first in s.barges)
    barge_known = known("voice_on", "bot_on", "transcript")
    # A turn with no answer of any kind was replaced: the person spoke on, and the next one answers.
    kept = [turn for turn in s.turns if turn.said or turn.moved or turn.silent]
    moved = [turn for turn in kept if turn.moved and not turn.said]
    silent = [turn for turn in kept if turn.silent and not turn.moved and not turn.said]
    return {
        "replied": heard and _rate(sum(answered), len(answered)),
        "replied_misheard": heard and _rate(sum(bool(t and t.said) for t in misheards),
                                            len(misheards)),
        "question": _rate(sum(reply.rstrip().endswith("?") for reply in replies), len(replies)),
        "name": named and _rate(sum(bool(named.search(reply)) for reply in replies), len(replies)),
        "median_words": (statistics.median(words), "") if words else None,
        "longest_words": (max(words), "") if words else None,
        "longest_s": (round(max(s.spans), 1), "") if s.spans else None,
        "initiations": known("initiated") and hours and (round(s.seen["initiated"] / hours, 1), ""),
        "movement_only": known("emote", "turn") and _rate(len(moved), len(kept)),
        "chosen_silence": known("reply_empty", "turn") and _rate(len(silent), len(kept)),
        "reaction": known("turn") and (known("reaction") or known("emote")) and _reactions(s),
        "speech_p50": (speech["p50"] / 1000, f" ({speech['n']})") if speech["n"] else None,
        "speech_p95": (speech["p95"] / 1000, f" ({speech['n']})") if speech["n"] else None,
        "talk_over": known("voice_on", "voice_off", "bot_on") and _rate(
            s.over_voice, s.seen["bot_on"] - len(s.over_voice_watching)),
        "talk_over_watching": s.over_voice_watching and _rate(sum(s.over_voice_watching),
                                                              len(s.over_voice_watching)),
        "overheard": count("overheard"),
        "unsure": count("unsure"),
        "sudden_moves": count("sudden_move"),
        "motion_hz": min(((hz, "") for ticks, hz, _, _ in s.gates if ticks >= MIN_GATE_TICKS),
                         default=None),
        "clamped": (sum(g[2] for g in s.gates), "") if s.gates else None,
        "still_max": (max(g[3] for g in s.gates), "") if s.gates else None,
        "link_drops": count("link_drop"),
        "kernel_unreachable": count("kernel_down"),
        "voice_silent": count("voice_silent"),
        "arrived": (s.seen["arrived"], "") if s.seen["arrived"] else None,
        "left": (s.seen["left"], "") if s.seen["left"] else None,
        "barge_usable_first": (barges[True, True], "") if barge_known else None,
        "barge_usable_later": (barges[True, False], "") if barge_known else None,
        "barge_empty_first": (barges[False, True], "") if barge_known else None,
        "barge_empty_later": (barges[False, False], "") if barge_known else None,
    }


def report(lines, patterns: dict[str, re.Pattern], name: str | None = None) -> dict[str, Row]:
    """Each metric's row for the session these log lines hold, keyed as in ``METRICS``."""
    lines = list(lines)
    values = _values(lines, _scan(lines, patterns), patterns, name)
    rows = {}
    for key, metric in METRICS.items():
        got = values[key]
        if not got:
            rows[key] = Row(metric.label, NOT_LOGGED, metric.target, "-")
        else:
            value = f"{got[0]:g}{metric.unit}{got[1]}"
            rows[key] = Row(metric.label, value, metric.target, metric.result(got[0]))
    return rows


def main(argv: list[str], patterns: dict[str, re.Pattern]) -> None:
    """Print the table for the bot log named in ``argv``. Reads it only."""
    parser = argparse.ArgumentParser(description="The owner's table for a session.")
    parser.add_argument("log", type=Path)
    parser.add_argument("--name", help="the owner's name, written|spoken; not printed")
    args = parser.parse_args(argv)
    lines = args.log.read_text(errors="replace").splitlines()
    s = _scan(lines, patterns)
    hours = f"{(s.ended - s.begun) / 3600:.1f} h" if s.begun else NOT_LOGGED
    print(f"{args.log.name}: {hours}, build {s.build or NOT_LOGGED}, "
          f"config {s.config or NOT_LOGGED}, "
          f"transcripts {len(s.heard)}, turns {len(s.turns)}")
    print(f"{'Metric':36}{'Value':18}{'Target':10}Result")
    for row in report(lines, patterns, args.name).values():
        print(f"{row.label:36}{row.value:18}{row.target:10}{row.result}")
