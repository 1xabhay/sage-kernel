"""The pet meter on synthetic lines of a made-up body: it needs only the contract, not Reachy.

Reachy's own patterns, on the lines its processor really writes, are in the app's tests/test_pet.py.
"""

import ast
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from sage_device import latency, pet

PATTERNS = pet.compile_patterns({
    "session": r"body:session: build (?P<build>\S+) config (?P<config>\S+)",
    "transcript": r"stt - heard \[(?P<text>.*)\]$",
    "stt_empty": r"stt - nothing$",
    "voice_on": r"vad - on$",
    "voice_off": r"vad - off$",
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
    "overheard": r"body\.sage:overheard: quiet",
    "watching": r"body\.sage:media: watching ",
    "watched": r"body\.sage:media: ended",
})
#: A body that also moves and tilts: its motion writer's lines and the lines it did not catch.
MOVING = PATTERNS | pet.compile_patterns({
    "sudden_move": r"body\.motion:sudden move",
    "motion_gates": (r"body\.motion:gates: (?P<ticks>\d+) ticks, (?P<hz>[\d.]+) Hz, "
                     r"clamped (?P<clamped>\d+), still (?P<still>[\d.]+) s"),
    "unsure": r"body\.sage:unsure: ",
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


def rows(lines, name=None, patterns=PATTERNS):
    return {key: (row.value, row.target, row.result)
            for key, row in pet.report(lines, patterns, name).items()}


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


def test_the_name_is_matched_written_or_spoken():
    assert rows(SESSION_LOG, name="Nobody|Ada")["name"][0] == "50% (1/2)"
    assert rows(SESSION_LOG, name="Nobody")["name"][0] == "0% (0/2)"


def test_a_turn_replaced_before_it_answered_is_no_turn_in_the_shares():
    """The person spoke on before it answered: their next turn answers both (floor.py)."""
    lines = [own(0, "-", SESSION), start(1, "reachy-a"), start(3, "reachy-b"),
             own(3.5, "reachy-b", "body.sage:emote: cheerful1")]
    table = rows(lines)
    assert table["movement_only"][0] == "100% (1/1)"
    assert table["chosen_silence"][0] == "0% (0/1)"


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


def test_a_line_whose_time_cannot_be_read_is_skipped():
    lost = own(40, "-", "body.link:lost it")
    assert rows([own(0, "-", SESSION), lost])["link_drops"][0] == "1"
    assert rows([own(0, "-", SESSION), lost.replace(" 10:00:40,", " 10:61:40,")])[
        "link_drops"][0] == "0"


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


#: A body whose link says when it went down and when it came back.
LINKED = PATTERNS | pet.compile_patterns({
    "link_down": r"body\.link:down",
    "link_back": r"body\.link:back",
})


def linked(drops):
    """Two hours with ``drops`` lost links; down 30.5 s, 60 s to a restart, 200 s to the end."""
    return [
        own(0, "-", SESSION),
        *(own(50 + i, "-", "body.link:lost it") for i in range(drops)),
        own(100, "-", "body.link:down"),
        own(110, "-", "body.link:down"),  # said twice: still the one stretch
        own(130.5, "-", "body.link:back"),
        own(140, "-", "body.link:back"),  # back with nothing down: nothing
        own(200, "-", "body.link:down"),
        own(260, "-", SESSION),  # a restart ends the stretch
        own(7000, "-", "body.link:down"),
        own(7200, "-", "body.motion:idle"),
    ]


def test_link_drops_per_hour_and_the_seconds_the_link_was_down():
    table = rows(linked(3), patterns=LINKED)
    assert table["link_drops"] == ("3", "-", "-")
    assert table["link_drops_per_hour"] == ("1.5/h", "-", "-")
    assert table["link_down_s"] == ("290.5 s (3)", "-", "-")  # in 3 stretches
    quiet = rows([own(0, "-", SESSION), own(3600, "-", "body.motion:idle")], patterns=LINKED)
    assert quiet["link_drops_per_hour"][0] == "0/h"
    assert quiet["link_down_s"][0] == "0 s (0)"


def test_link_rows_the_log_cannot_give_are_not_logged():
    assert rows(linked(3))["link_down_s"][0] == "not logged"  # a body with no such lines
    assert rows([own(0, "-", SESSION)], patterns=LINKED)["link_drops_per_hour"][0] == "not logged"


def compared(capsys, tmp_path, before, after, patterns=LINKED):
    for name, lines in (("before.log", before), ("after.log", after)):
        (tmp_path / name).write_text("\n".join(lines))
    pet.main([str(tmp_path / "after.log"), "--baseline", str(tmp_path / "before.log")], patterns)
    return capsys.readouterr().out.splitlines()


def test_a_baseline_is_compared_by_link_drops_per_hour(capsys, tmp_path):
    out = compared(capsys, tmp_path, linked(3), linked(1))
    assert out[0].startswith("after.log: 2.0 h")  # the table is the log's, as ever
    assert out[-3:] == [
        "before.log: 2.0 h, link drops 3, link drops per hour 1.5/h, link down 290.5 s (3)",
        "after.log: 2.0 h, link drops 1, link drops per hour 0.5/h, link down 290.5 s (3)",
        "link drops per hour: fell",
    ]
    assert compared(capsys, tmp_path, linked(1), linked(1))[-1] == (
        "link drops per hour: did not fall")
    assert compared(capsys, tmp_path, linked(1), linked(3))[-1] == (
        "link drops per hour: did not fall")


def test_a_baseline_that_cannot_say_is_not_a_fall(capsys, tmp_path):
    out = compared(capsys, tmp_path, ["nothing here at all"], linked(1))
    assert out[-3].startswith("before.log: not logged, link drops not logged")
    assert out[-1] == "link drops per hour: cannot say"


NAMES = ("COMMON", "TO_SAGE", "NEAR_SAGE", "REQUESTS", "HANGING")


def test_misheard_is_the_kernels():
    """The meter holds a copy (a device imports no kernel code): kernel changes must reach it."""
    policy = Path(__file__).resolve().parents[4] / "sage" / "policy.py"
    picked = [node for node in ast.parse(policy.read_text()).body
              if getattr(node, "name", None) in ("misheard", "said_to_sage")
              or (isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in NAMES)]
    kernel = {"re": re, "SURE": 1.0}
    exec(compile(ast.Module(picked, []), str(policy), "exec"), kernel)
    copies = [pet._COMMON, pet._TO_SAGE, pet._NEAR_SAGE, pet._REQUESTS, pet._HANGING]
    assert [kernel[name] for name in NAMES] == copies
    for line in ("I'm sad.", "I'm just gonna.", "Sing a song.", "Only the kettle.", "", "Hello.",
                 "hi there", "What is it?", "the fridge banana", "Short one.", "Age.", "Hey age.",
                 "Age, come here.", "Come here age.", "Old age home.", "Saige!", "Stage left.",
                 "This one is a long and whole sentence.", "la la la la and then we sang it all.",
                 "Is this a question without", "A much longer line that ends fine, doesn't it?"):
        assert pet.misheard(line) == kernel["misheard"](line), line


def test_speech_begun_over_a_voice_is_scored_once_per_start_of_audio():
    table = rows([
        own(0, "-", SESSION),
        theirs(1, "out - on"), theirs(2, "out - off"),  # nobody talking
        theirs(3, "vad - on"), theirs(4, "out - on"), theirs(5, "out - off"),  # over them
        theirs(6, "vad - off"), theirs(7, "out - on"), theirs(8, "out - off"),
    ])
    assert table["talk_over"][0].startswith("33%")
    assert table["talk_over"][2] == "fail"


@pytest.mark.parametrize("kind", ["overheard", "unsure"])
def test_lines_it_kept_out_of_or_did_not_catch_are_counted_and_absent_is_not_zero(kind):
    kept = own(1, "-", f"body.sage:{kind}: quiet")
    assert rows([own(0, "-", SESSION), kept, kept], patterns=MOVING)[kind] == ("2", "-", "-")
    assert rows(["nothing here at all"], patterns=MOVING)[kind][0] == "not logged"


def test_sudden_moves_are_counted_and_a_session_with_none_says_zero():
    sudden = own(1, "-", "body.motion:sudden move: yaw too fast")
    assert rows([own(0, "-", SESSION)], patterns=MOVING)["sudden_moves"][0] == "0"
    assert rows([own(0, "-", SESSION), sudden, sudden], patterns=MOVING)["sudden_moves"][0] == "2"


def test_the_motion_rows_read_the_worst_full_minute_and_score_it():
    """At least 40 Hz, no clamped target, still at most 5 s: from the writer's line each minute."""
    def gates(minute, ticks, hz, clamped, still):
        return own(60 * minute, "-", f"body.motion:gates: {ticks} ticks, {hz} Hz, "
                                     f"clamped {clamped}, still {still} s")
    full = pet.MIN_GATE_TICKS
    table = rows([own(0, "-", SESSION), gates(1, 2990, 49.8, 1, 1.2), gates(2, full, 41.5, 2, 6.5),
                  gates(3, full - 1, 12.0, 0, 0.4)],  # a minute it barely ran: nothing of its rate
                 patterns=MOVING)
    assert table["motion_hz"] == ("41.5 Hz", ">=40 Hz", "pass")
    assert table["clamped"] == ("3", "<=0", "fail")  # every minute's, added up
    assert table["still_max"] == ("6.5 s", "<=5 s", "fail")
    slow = rows([own(0, "-", SESSION), gates(1, 2990, 39.9, 0, 5.0)], patterns=MOVING)
    assert [slow[key][2] for key in ("motion_hz", "clamped", "still_max")] == [
        "fail", "pass", "pass"]
    none = rows([own(0, "-", SESSION)], patterns=MOVING)  # no minute line yet: unmeasured
    assert {none[key][0] for key in ("motion_hz", "clamped", "still_max")} == {"not logged"}


def test_speech_over_a_voice_while_watching_is_its_own_row_the_tv_or_the_person():
    """Watching, any voice may be the TV's: unknowable today, so counted apart, not hidden."""
    table = rows([
        own(0, "-", SESSION),
        theirs(1, "vad - on"), theirs(2, "out - on"), theirs(3, "out - off"),  # over them
        own(4, "-", "body.sage:media: watching a Formula 1 race"),
        theirs(5, "out - on"), theirs(6, "out - off"),  # the voice still going: the TV, maybe
        theirs(7, "vad - off"), theirs(8, "out - on"), theirs(9, "out - off"),
        own(10, "-", "body.sage:media: ended"),
    ])
    assert table["talk_over"][0].startswith("100% (1/1)")
    assert table["talk_over_watching"][0].startswith("50% (1/2)")
