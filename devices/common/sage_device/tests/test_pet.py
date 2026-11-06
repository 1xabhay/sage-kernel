"""The pet meter on synthetic lines of a made-up body: it needs only the contract, not Reachy.

Reachy's own patterns, on the lines its processor really writes, are in the app's tests/test_pet.py.
"""

import ast
import re
from datetime import datetime, timedelta
from pathlib import Path

from sage_device import latency, pet

PATTERNS = pet.compile_patterns({
    "session": r"body:session: build (?P<build>\S+) config (?P<config>\S+)",
    "transcript": r"stt - heard \[(?P<text>.*)\]$",
    "stt_empty": r"stt - nothing$",
    "voice_on": r"vad - on$",
    "bot_on": r"out - on$",
    "bot_off": r"out - off$",
    "turn": r"body\.sage:" + latency.START.pattern,
    "initiated": r"body\.sage:initiated$",
    "say": r"tts - say \[(?P<text>.*)\]$",
    "emote": r"body\.sage:emote: ",
    "reply_empty": r"body\.sage:reply empty$",
    "reaction": r"body\.motion:(cue|glance)",
    "link_drop": r"body\.link:lost",
    "kernel_down": r"body\.sage:kernel unreachable",
    "voice_silent": r"tts - no audio",
    "arrived": r"body\.presence:arrived",
    "left": r"body\.presence:left",
})
SESSION = "body:session: build abc1234 config 0123456789ab"


def at(s):
    """Give the wall time ``s`` seconds into the session."""
    begun = datetime(2026, 11, 5, 10)  # noqa: DTZ001
    return (begun + timedelta(seconds=s)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def own(s, turn, text):
    return f"{at(s).replace('.', ',')} {1000 + s:.3f} {turn} INFO:{text}"


def theirs(s, text):
    return f"{at(s)} | DEBUG    | {text}"


def start(s, turn, ago=500, lane="kernel"):
    return own(s, turn, f"body.sage:turn start ({lane} lane): voice ended {ago} ms ago")


def spoke(s, turn):
    return own(s, turn, "body.sage:" + latency.AUDIO)


def rows(lines, name=None):
    return {key: (row.value, row.target, row.result)
            for key, row in pet.report(lines, PATTERNS, name).items()}


SECRET = "my bank code is nine"
#: One hour: four transcripts; one turn speaks, one only moves, one stays silent; one initiation.
SESSION_LOG = [
    own(0, "-", SESSION),
    theirs(1, f"stt - heard [{SECRET}.]"),  # clean
    start(2, "reachy-a"),
    own(2.1, "reachy-a", "body.motion:cue fired"),
    theirs(3, "tts - say [Hello Ada,]"),
    theirs(3.1, "tts - say [how are you?]"),
    theirs(4, "out - on"),
    spoke(4, "reachy-a"),
    theirs(7, "out - off"),
    theirs(10, "stt - heard [fridge banana]"),  # misheard: no end mark
    start(11, "reachy-b", 1000),
    own(11.5, "reachy-b", "body.sage:emote: cheerful1"),
    own(11.6, "reachy-b", "body.sage:reply empty"),
    theirs(20, "stt - heard [never reached a turn]"),  # misheard, and dropped
    theirs(30, "stt - heard [what time is it now, please?]"),  # clean
    start(31, "reachy-c"),
    own(31.2, "reachy-c", "body.sage:reply empty"),
    own(40, "-", "body.link:lost it"),
    own(41, "-", "body.sage:kernel unreachable between turns: x"),
    theirs(42, "tts - no audio"),
    own(43, "-", "body.presence:arrived"),
    own(44, "kernel-1", "body.sage:initiated"),
    theirs(44.5, "tts - say [One two three four five six seven.]"),
    own(45, "-", "body.presence:left"),
    own(3600, "-", "body.motion:idle"),
]


def test_the_owners_table_for_a_session():
    table = rows(SESSION_LOG, name="Ada")
    assert table["replied"] == ("25% (1/4)", "<=50%", "pass")
    assert table["replied_misheard"] == ("0% (0/2)", "<=25%", "pass")
    assert table["question"] == ("50% (1/2)", "<=10%", "fail")
    assert table["name"] == ("50% (1/2)", "<=5%", "fail")
    assert rows(SESSION_LOG, name="Nobody|Ada")["name"][0] == "50% (1/2)"  # written|spoken
    assert table["median_words"] == ("6", "<=4", "fail")  # of 5 and 7
    assert table["longest_words"] == ("7", "<=12", "pass")
    assert table["longest_s"] == ("3 s", "<=4 s", "pass")
    assert table["initiations"] == ("1/h", "1-3/h", "pass")
    assert table["movement_only"] == ("33% (1/3)", ">=40%", "fail")
    assert table["chosen_silence"] == ("33% (1/3)", ">=20%", "pass")
    assert table["link_drops"] == ("1", "-", "-")
    assert table["kernel_unreachable"] == ("1", "-", "-")
    assert table["voice_silent"] == ("1", "-", "-")
    assert table["arrived"][0] == table["left"][0] == "1"


def test_voice_end_to_reaction_and_speech():
    table = rows(SESSION_LOG)
    # a: ended 1.5, cue 2.1 = 600 ms. b: ended 10, emote 11.5 = 1500 ms. c: nothing moved.
    assert table["reaction"] == ("1050 ms (2/3)", "<=300 ms", "fail")
    # latency.py's own number: a's voice ended at 1.5 and spoke at 4, plus its lead.
    assert table["speech_p50"] == (f"{(2500 + latency.LEAD_MS) / 1000:g} s (1)", "<=1.5 s", "fail")
    assert table["speech_p95"][1:] == ("<=3 s", "pass")


def test_a_missing_line_is_not_logged_never_zero():
    gone = (SESSION, "body.link", "emote", "reply empty", "initiated")
    table = rows([line for line in SESSION_LOG if not any(g in line for g in gone)])
    for key in ("link_drops", "movement_only", "chosen_silence", "initiations", "name",
                "sudden_moves"):
        assert table[key] == ("not logged", table[key][1], "-"), key
    assert rows(["nothing here at all"])["replied"][0] == "not logged"
    assert rows([])["barge_usable_first"][0] == "not logged"


def test_the_session_line_promises_the_bodys_lines_so_their_absence_is_a_real_zero():
    table = rows([own(0, "-", SESSION), own(3600, "-", "body.motion:idle")])
    assert table["link_drops"][0] == "0"
    assert table["initiations"] == ("0/h", "1-3/h", "fail")
    assert table["arrived"][0] == "not logged"  # presence may be off: known only once seen
    assert table["sudden_moves"][0] == "not logged"  # a pattern this body does not declare


def test_lines_join_by_turn_id_when_they_land_after_the_next_turn_began():
    table = rows([
        own(0, "-", SESSION),
        theirs(1, "stt - heard [one two three four five.]"),
        start(2, "reachy-a"),
        start(3, "reachy-b"),
        own(4, "reachy-a", "body.sage:emote: late"),
        theirs(5, "tts - say [Hi.]"),
    ])
    assert table["movement_only"][0] == "50% (1/2)"


def test_barge_ins_split_by_usable_speech_and_first_second_are_counted_not_scored():
    table = rows([
        own(0, "-", SESSION),
        theirs(10, "vad - on"),  # the bot is quiet: no barge-in
        theirs(11, "stt - heard [Hello there, my friend.]"),
        theirs(20, "out - on"),
        theirs(20.5, "vad - on"),  # first second, usable
        theirs(21, "stt - heard [Wait, hold on please.]"),
        theirs(22, "vad - on"),  # later, nothing usable
        theirs(23, "stt - nothing"),
        theirs(24, "vad - on"),  # later, usable
        theirs(25, "stt - heard [No, the other one please.]"),
        theirs(26, "out - off"),
        theirs(30, "out - on"),
        theirs(30.2, "vad - on"),  # first second, and no STT result before the next onset
        theirs(31, "out - off"),
        theirs(32, "vad - on"),  # the bot is quiet
    ])
    assert table["barge_usable_first"] == ("1", "-", "-")
    assert table["barge_usable_later"] == ("1", "-", "-")
    assert table["barge_empty_first"] == ("1", "-", "-")
    assert table["barge_empty_later"] == ("1", "-", "-")


def test_the_printed_table_has_counts_and_no_ones_speech(capsys, tmp_path):
    log = tmp_path / "bot.log"
    log.write_text("\n".join(SESSION_LOG))
    pet.main([str(log), "--name", "Ada"], PATTERNS)
    out = capsys.readouterr().out
    assert "abc1234" in out and "0123456789ab" in out  # sessions compare across builds
    assert "Replied per transcript" in out and "not logged" in out
    assert re.search(r"Ends in question\s+50% \(1/2\)\s+<=10%\s+fail", out)
    for spoken in (SECRET, "fridge", "banana", "how are you", "Ada"):
        assert spoken not in out


def test_misheard_is_the_kernels():
    """The meter holds a copy (a device imports no kernel code): kernel changes must reach it."""
    policy = Path(__file__).resolve().parents[4] / "sage" / "policy.py"
    picked = [node for node in ast.parse(policy.read_text()).body
              if getattr(node, "name", None) == "misheard"
              or (isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "COMMON")]
    kernel = {"re": re, "SURE": 1.0}
    exec(compile(ast.Module(picked, []), str(policy), "exec"), kernel)
    for line in ("", "Hello.", "hi there", "What is it?", "the fridge banana", "Short one.",
                 "This one is a long and whole sentence.", "la la la la and then we sang it all.",
                 "Is this a question without", "A much longer line that ends fine, doesn't it?"):
        assert pet.misheard(line) == kernel["misheard"](line), line
