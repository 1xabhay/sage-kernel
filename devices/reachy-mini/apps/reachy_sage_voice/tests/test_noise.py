"""The noise trace (eval/noise.py): a record per heard segment, a review tool, a CSV. Synthetic."""

import csv
import io
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

import noise

RATE = 16000
T = time.mktime((2026, 11, 7, 11, 6, 0, 0, 0, -1))  # local, as the recorder and the log write


def stamp(t: float, sep: str) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f"{sep}{int(t % 1 * 1000):03d}"


def pipecat(t: float, text: str) -> str:
    return f"{stamp(t, '.')} | DEBUG    | pipecat.x:y:1 - {text}"


def sage(t: float, text: str, turn: str = "reachy-a") -> str:
    return f"{stamp(t, ',')} 1000.000 {turn} INFO:reachy_sage_voice.{text}"


def write_wav(folder: Path, end: float, left: np.ndarray, right: np.ndarray) -> Path:
    ns = round(end % 1 * 1e9)
    path = folder / time.strftime(f"utt-%Y%m%d-%H%M%S-{ns:09d}.wav", time.localtime(end))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.stack([left, right], axis=1) * 32767).astype(np.int16).tobytes())
    return path


def hum(seconds: float, amp: float) -> np.ndarray:  # a voice-like burst after 0.5 s of floor
    rng = np.random.default_rng(1)
    t = np.arange(int(seconds * RATE)) / RATE
    floor = rng.normal(0, 0.002, len(t))
    voice = amp * np.sin(2 * np.pi * 180 * t) * (np.sin(2 * np.pi * 3 * t) > 0) * (t > 0.5)
    return floor + voice


def hiss(seconds: float) -> np.ndarray:
    return np.random.default_rng(2).normal(0, 0.02, int(seconds * RATE))


def session(tmp_path):
    rec = tmp_path / "session-20261107-110600"
    rec.mkdir()
    a = write_wav(rec, T + 2.0, hum(1.5, 0.3), 0.5 * hum(1.5, 0.3))
    b = write_wav(rec, T + 8.0, hiss(1.0), hiss(1.0))
    c = write_wav(rec, T + 12.0, hiss(0.6), hiss(0.6))
    log = [
        pipecat(T + 2.01, "User stopped speaking"),
        pipecat(T + 2.30, "Transcription: [Are you there?]"),
        sage(
            T + 2.31,
            "pipeline:stt: confidence mean 0.95 min 0.80, similarity -, words 0.97 0.80 0.99",
        ),
        sage(T + 2.32, "sage:chose: speak"),
        sage(T + 2.33, "sage:turn start (kernel lane): voice ended 900 ms ago"),
        pipecat(T + 3.00, "Generating TTS [I'm here.]"),
        pipecat(T + 3.20, "Bot started speaking"),
        pipecat(T + 4.00, "Bot stopped speaking"),
        pipecat(T + 8.01, "User stopped speaking"),
        pipecat(T + 8.30, "Transcription: [and then he said]"),
        sage(T + 8.31, "sage:unsure: tilted", "reachy-b"),
        pipecat(T + 12.01, "User stopped speaking"),
        pipecat(T + 12.20, "Received empty transcription from API"),
    ]
    return rec, log, (a, b, c)


def test_each_recorded_segment_is_one_record_joined_to_its_words_and_what_sage_did(tmp_path):
    rec, log, (a, _, _) = session(tmp_path)
    one, two, three = noise.records(log, [rec])
    assert (one["id"], one["wav"], one["text"], one["question"], one["words"]) == (
        a.stem,
        str(a),
        "Are you there?",
        True,
        3,
    )
    assert (one["conf_mean"], one["conf_min"], one["word_conf"]) == (0.95, 0.80, [0.97, 0.80, 0.99])
    assert (one["choice"], one["kernel"], one["reply"], one["hearing"]) == (
        "speak",
        True,
        "I'm here.",
        None,
    )
    assert one["since_sage_spoke_s"] is None and one["sage_asked"] is False
    assert abs(one["duration_s"] - 1.5) < 0.01
    assert (two["text"], two["hearing"], two["reply"], two["kernel"]) == (
        "and then he said",
        "tilted",
        None,
        False,
    )
    assert (
        abs(two["since_sage_spoke_s"] - (T + 7.0 - (T + 4.0))) < 0.01
    )  # its start minus Sage's last word
    assert (three["text"], three["empty"], three["conf_mean"]) == ("", True, None)


def test_audio_features_tell_a_voice_over_a_quiet_floor_from_steady_hiss(tmp_path):
    rec, log, _ = session(tmp_path)
    voice, steady, _ = noise.records(log, [rec])
    assert voice["snr_db"] > 20 > steady["snr_db"]
    assert voice["lr_db"] > 5 and abs(steady["lr_db"]) < 1  # left twice as loud: about 6 dB
    assert voice["flatness"] < steady["flatness"]
    assert 0 <= voice["vad_mean"] <= voice["vad_max"] <= 1 and 0 <= voice["speech_ratio"] <= 1
    assert noise._silero.sample_rate == RATE  # an unset rate makes Silero log an error and say 0


def test_a_line_after_sage_asked_something_says_so_and_echo_overlap_is_flagged(tmp_path):
    rec, log, _ = session(tmp_path)
    log = [line.replace("I'm here.", "Want to play?") for line in log]
    _, after, _ = noise.records(log, [rec])
    assert after["sage_asked"] is True and after["sage_speaking"] is False
    # Sage's audio from 6.8 s to 7.5 s overlaps the segment that starts at 7 s: maybe its own echo.
    log[8:8] = [pipecat(T + 6.8, "Bot started speaking"), pipecat(T + 7.5, "Bot stopped speaking")]
    _, echo, _ = noise.records(log, [rec])
    assert echo["sage_speaking"] is True and echo["since_sage_spoke_s"] == 0.0


def test_review_labels_replied_lines_and_the_csv_carries_the_labels(tmp_path):
    rec, log, (a, _, _) = session(tmp_path)
    rows = noise.records(log, [rec])
    labels = tmp_path / "labels.jsonl"
    played = []
    out = io.StringIO()
    noise.review(rows, labels, keys=iter(["p", "n", "t"]), play=played.append, out=out)
    assert played == [str(a)]  # only the replied-to line is offered by default
    (label,) = [json.loads(line) for line in labels.read_text().splitlines()]
    assert (label["id"], label["label"], label["source"]) == (a.stem, "not_to_me", "tv")
    noise.review(
        rows, labels, keys=iter([]), play=played.append, out=out
    )  # labelled lines are not asked again
    table = tmp_path / "trace.csv"
    noise.export_csv(rows, labels, table)
    got = list(csv.DictReader(table.open()))
    assert [r["label"] for r in got] == ["not_to_me", "", ""] and got[0]["text"] == "Are you there?"


def test_a_segment_whose_transcript_never_came_is_still_a_record(tmp_path):
    rec, log, (a, b, c) = session(tmp_path)
    log = [
        line for line in log if "Transcription" not in line and "empty transcription" not in line
    ]
    log.append(
        pipecat(T + 20.0, "Transcription: [late]")
    )  # 18 s after a's stop: too late to be a's
    got = {r["id"]: r["text"] for r in noise.records(log, [rec], audio=False)}
    assert got == {a.stem: None, b.stem: "late", c.stem: None}


def test_a_log_with_no_recordings_still_traces_its_transcripts():
    log = [pipecat(T + 2.30, "Transcription: [Hello there.]"), sage(T + 2.32, "sage:chose: speak")]
    (only,) = noise.records(log, [])
    assert (only["wav"], only["text"], only["choice"], only["snr_db"]) == (
        None,
        "Hello there.",
        "speak",
        None,
    )


def test_a_record_carries_what_was_heard_at_its_onset_direction_head_and_face(tmp_path):
    rec, log, _ = session(tmp_path)
    log += [  # one line per speech onset, a moment after each segment's start (0.5 s and 7 s)
        sage(
            T + 0.62,
            "executor:heard: DoA 72 deg (speech_detected=True), head +15 deg from the body, "
            "voice +33 deg from the body, face seen at x=+0.12",
        ),
        sage(
            T + 7.10,
            "executor:heard: DoA - deg (speech_detected=None), head -4 deg from the body, "
            "voice - deg from the body, face not seen",
        ),
    ]
    one, two, three = noise.records(log, [rec], audio=False)
    keys = ("doa_deg", "speech_detected", "head_deg", "voice_deg", "face", "face_x")
    assert [one[k] for k in keys] == [72.0, True, 15.0, 33.0, True, 0.12]
    assert [two[k] for k in keys] == [None, None, -4.0, None, False, None]
    assert [three[k] for k in keys] == [None] * 6  # no onset line (Sage was speaking): unknown
    assert one["conf_mean"] == 0.95  # next to the confidence it already keeps


# The gate's labelled set: sample, label, baseline (design 1-gate.md, "Offline scoring").

DAY = 86400.0


def heard_line(t: float, text: str, *did: str, stt: str = "") -> list[str]:
    """One transcript and the lines today's gates leave after it."""
    score = [
        sage(
            t + 0.01, "pipeline:stt: confidence mean 0.50 min 0.40, similarity -, words 0.50" + stt
        )
    ]
    return [
        pipecat(t, f"Transcription: [{text}]"),
        *score,
        *(sage(t + 0.02 + i / 100, d) for i, d in enumerate(did)),
    ]


TURN = ("sage:chose: speak", "sage:turn start (kernel lane): voice ended 900 ms ago")


def test_the_stt_line_is_read_with_and_without_the_face_and_direction_suffix():
    log = [
        *heard_line(T, "the kettle is on", stt=", face 1.5, doa 72"),
        *heard_line(T + 40, "it is raining again", stt=", face none, doa none"),
        *heard_line(T + 80, "where did the cat go"),
    ]
    new, absent, old = noise.records(log, [])
    assert (new["conf_mean"], new["word_conf"], new["face_age_s"], new["stt_doa_deg"]) == (
        0.5,
        [0.5],
        1.5,
        72.0,
    )
    assert (absent["conf_mean"], absent["face_age_s"], absent["stt_doa_deg"]) == (0.5, None, None)
    assert (old["conf_mean"], old["word_conf"], old["face_age_s"], old["stt_doa_deg"]) == (
        0.5,
        [0.5],
        None,
        None,
    )


def test_each_transcript_carries_the_gate_outcome_the_log_recorded():
    log = [
        *heard_line(T, "what time is it", *TURN),
        *heard_line(T + 40, "that is lovely", "sage:chose: move"),
        *heard_line(T + 80, "and then the", "sage:unsure: tilted"),
        *heard_line(T + 120, "mumble the", "sage:unsure: quiet"),
        *heard_line(T + 160, "he told her it was late", "sage:overheard: quiet"),
        *heard_line(T + 200, "nothing came of this one"),
    ]
    assert [noise.outcome(r) for r in noise.records(log, [])] == [
        "turn",
        "move",
        "doubted",
        "doubted",
        "overheard",
        "other",
    ]


def many(days: int = 2, per_day: int = 60) -> list[dict]:
    """Made-up lines over ``days`` days: a chain of close lines each day, every outcome in turn."""
    did = [TURN, ("sage:chose: move",), ("sage:unsure: quiet",), ("sage:overheard: quiet",)]
    log = []
    for d in range(days):
        for i in range(per_day):  # the first 20 lines 5 s apart (one chain), the rest 40 s apart
            t = T + d * DAY + (i * 5 if i < 20 else 100 + i * 40)
            text = "sage come here" if i in (7, 33) else f"made up line {d} {i}"
            log += heard_line(t, text, *did[i % 4])
    return noise.gate_lines(noise.records(log, []))


def test_gate_lines_know_their_day_their_place_in_the_chain_and_three_lines_before():
    lines = many(days=1)
    assert len({r["id"] for r in lines}) == len(lines) == 60  # no recording: an id from the time
    assert [r["chain"] for r in lines[:3]] == [1, 2, 3] and lines[19]["chain"] == 20
    assert lines[20]["chain"] == 1  # 40 s after the last: the window (floor.OPEN_S) had closed
    assert lines[0]["day"] == time.strftime("%Y-%m-%d", time.localtime(T))
    assert lines[0]["context"] == []
    assert [c["text"] for c in lines[5]["context"]] == [f"made up line 0 {i}" for i in (2, 3, 4)]
    assert lines[5]["context"][0]["outcome"] == "doubted" and lines[5]["context"][2]["ago_s"] == 5


def test_the_sample_is_the_same_for_a_seed_covers_every_stratum_and_keeps_every_name_line():
    lines = many()
    picked = noise.sample(lines, n=40, seed=3)
    assert [r["id"] for r in picked] == [r["id"] for r in noise.sample(lines, n=40, seed=3)]
    assert [r["id"] for r in picked] != [r["id"] for r in noise.sample(lines, n=40, seed=4)]
    assert len(picked) == 40 and len({r["id"] for r in picked}) == 40
    assert sum("sage" in r["text"] for r in picked) == 4  # two a day: all of them

    def strata(rows):
        return {(r["day"], r["outcome"], noise.chain_bucket(r["chain"])) for r in rows}

    assert strata(picked) == strata(lines)
    # A line stands for its stratum's lines: the weights add back to every line that was heard.
    assert round(sum(r["weight"] for r in picked)) == len(lines)
    assert len(noise.sample(lines, n=1000, seed=0)) == len(lines)  # fewer lines than asked: all


def test_labelling_shows_three_lines_before_each_and_can_be_stopped_and_resumed(tmp_path):
    picked = noise.sample(many(days=1), n=6, seed=0)
    labels, out = tmp_path / "labels.jsonl", io.StringIO()
    assert noise.review(picked, labels, keys=iter(["y", "q"]), out=out, everything=True) == 1
    shown = sum(len(r["context"]) for r in picked[:2])  # the one labelled and the one it quit on
    assert out.getvalue().count(" s before: ") == shown
    assert max(len(r["context"]) for r in picked) == 3
    for before in picked[1]["context"]:
        assert f"{before['text']!r} -> {before['outcome']}" in out.getvalue()
    again = io.StringIO()
    assert (
        noise.review(
            picked, labels, keys=iter(["n", "o", "s", "k", "q"]), out=again, everything=True
        )
        == 3
    )
    assert "[1/5]" in again.getvalue()  # the one already labelled is not asked again
    got = [json.loads(x) for x in labels.read_text().splitlines()]
    assert [(g["label"], g["source"]) for g in got] == [
        ("to_me", None),
        ("not_to_me", "other_person"),
        ("unsure", None),
        ("noise", None),
    ]
    assert [g["id"] for g in got] == [r["id"] for r in picked[:4]]


def test_the_baseline_counts_missed_address_and_wrong_replies_per_day_without_unsure(tmp_path):
    def row(day, outcome, label, weight=1.0):
        return (
            {
                "id": f"{day}-{len(rows)}",
                "day": day,
                "outcome": outcome,
                "weight": weight,
                "text": "made up",
            },
            label,
        )

    rows = []
    for spec in [
        ("d1", "turn", "to_me"),
        ("d1", "doubted", "to_me"),
        ("d1", "overheard", "to_me"),
        ("d1", "move", "to_me"),
        ("d1", "turn", "not_to_me"),
        ("d1", "turn", "noise"),
        ("d1", "doubted", "not_to_me", 3.0),
        ("d1", "turn", "unsure"),
        ("d2", "turn", "not_to_me"),
        ("d2", "other", "to_me"),
        ("d2", "turn", None),
    ]:
        rows.append(row(*spec))
    labels = {r["id"]: {"id": r["id"], "label": label} for r, label in rows if label}
    days = noise.baseline([r for r, _ in rows], labels)
    d1, d2 = days["d1"], days["d2"]
    assert (d1["to_me"], d1["missed"], d1["moved"]) == (4, 2, 1)
    assert (d1["not_to_me"], d1["wrong_reply"], d1["unsure"]) == (3, 2, 1)
    assert d1["missed_pct"] == 50.0 and d1["wrong_reply_pct"] == 40.0  # weighted: 2 of 1+1+3
    assert (d2["to_me"], d2["missed"], d2["not_to_me"], d2["wrong_reply"]) == (1, 0, 1, 1)
    assert d2["unlabelled"] == 1 and d2["unsure"] == 0
    table = noise.baseline_table(days, frozen={"d2"})
    assert "d1 " in table and "d2 (frozen)" in table


def test_the_gate_commands_sample_once_then_write_a_manifest_a_day_of_which_freezes(
    tmp_path, capsys
):
    import score

    log = tmp_path / "bot-made-up.log"
    lines = []
    for d in range(2):
        for i in range(12):
            lines += heard_line(T + d * DAY + i * 40, f"made up line {d} {i}", *TURN)
    lines.insert(0, pipecat(T - 3 * DAY, "Transcription: [made up, from before the stt line]"))
    log.write_text("\n".join(lines) + "\n")
    args = ["--logs", str(log), "--recordings", str(tmp_path / "rec")]
    noise.main(["sample", *args, "--n", "10", "--seed", "1"])
    assert "24 heard lines in 2 days" in capsys.readouterr().out  # only lines with their signals
    second = time.strftime("%Y-%m-%d", time.localtime(T + DAY))
    noise.main(
        ["sample", "--logs", str(log), "--recordings", str(tmp_path / "b"), "--since", second]
    )
    assert "12 heard lines in 1 days" in capsys.readouterr().out
    gate = tmp_path / "rec" / "gate"
    first = (gate / "sample.jsonl").read_text()
    assert len(first.splitlines()) == 10
    noise.main(["sample", *args, "--n", "5"])  # labels point at the first sample: it is kept
    assert (gate / "sample.jsonl").read_text() == first
    picked = [json.loads(x) for x in first.splitlines()]
    (gate / "labels.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "id": r["id"],
                    "label": "not_to_me",
                    "source": "tv",
                    "at": f"2026-11-21T10:00:{i * 4:02d}+00:00",
                }
            )
            + "\n"
            for i, r in enumerate(picked[:6])
        )
    )
    capsys.readouterr()
    noise.main(["baseline", "--recordings", str(tmp_path / "rec")])
    said = capsys.readouterr().out
    assert "6 of 10 labelled" in said and "4 s a label" in said  # the median gap between labels
    assert "made up" not in said  # counts only: no heard words on the screen or in a paste
    manifest = [json.loads(x) for x in (gate / "manifest.jsonl").read_text().splitlines()]
    assert len(manifest) == 6 and {m["label"] for m in manifest} == {"not_to_me"}
    day = manifest[0]["session"]
    frozen = tmp_path / "FROZEN.md"
    score.freeze(gate, [day], frozen)
    noise.main(["baseline", "--recordings", str(tmp_path / "rec"), "--frozen", str(frozen)])
    assert f"{day} (frozen)" in capsys.readouterr().out


# --- gate.attend: its log lines, its replay over old logs, its score on the labelled set ---


def test_the_new_gates_line_is_read_and_the_old_checks_lines_still_are():
    log = [
        *heard_line(
            T, "can you hear me", "sage:turn start (kernel lane): x", "sage:gate: answer (direct)"
        ),
        *heard_line(T + 40, "that is lovely", "sage:gate: ack (unsure)", "sage:unsure: tilted"),
        *heard_line(
            T + 80, "and then the", "sage:gate: listen (aside, unclear)", "sage:unsure: quiet"
        ),
        *heard_line(
            T + 120,
            "he told her it was late again",
            "sage:gate: overheard (aside)",
            "sage:overheard: quiet",
        ),
        *heard_line(T + 160, "reachy look for me", "sage:gate: command (route)"),
        *heard_line(T + 200, "that is lovely", "sage:chose: move"),  # a log from before the gate
        *heard_line(T + 240, "mumble the", "sage:unsure: quiet"),
    ]
    rows = noise.records(log, [])
    assert [noise.outcome(r) for r in rows] == [
        "turn",
        "ack",
        "listen",
        "overheard",
        "other",
        "move",
        "doubted",
    ]
    assert (rows[1]["gate"], rows[1]["gate_check"]) == ("ack", "unsure")


def replayed(*said, stt=""):
    log = [x for t, text in said for x in heard_line(T + t, text, *TURN, stt=stt)]
    lines = noise.gate_lines(noise.records(log, []))
    return lines, noise.replay(lines)


def test_an_old_log_is_replayed_through_the_gate_from_its_words_times_and_scores():
    tv = "And then the whole team came along to see them."
    lines, did = replayed(
        (0, "The garden looks nice."),
        (5, "I was talking to you."),
        (9, "Is it warm out?"),
        (20, tv),
        (30, tv),
        (40, tv),
        (300, "Hello Sage."),
        (400, tv),
    )
    assert [d["outcome"] for d in did] == [
        "ack",
        "answer",
        "answer",
        "listen",
        "listen",
        "overheard",
        "answer",
        "overheard",
    ]
    assert [d["reason"] for d in did[:3]] == ["unsure", "followed", "direct"]
    assert [d["conversation"] for d in did] == [None, 1, 1, 1, None, None, 2, None]
    assert [d["id"] for d in did] == [r["id"] for r in lines]
    low = noise.replay(lines, low=0.6)  # every made-up line scores 0.50
    assert [d["outcome"] for d in low][:3] == ["listen", "listen", "listen"]
    assert low[6]["outcome"] == "answer"  # its name: never stepped down
    seen = replayed((0, "The garden looks nice."), stt=", face 1.0, doa 40")[1]
    assert (seen[0]["outcome"], seen[0]["reason"]) == ("answer", "face")


def test_the_replay_report_is_counts_and_carries_no_ones_words():
    lines, _ = replayed((0, "The garden looks nice."), (5, "I meant you."), (300, "Hello Sage."))
    report = noise.replay_report([lines])
    assert "3 heard lines in 1 logs" in report and "2 conversations" in report
    assert "median 0.50" in report
    for word in ("garden", "meant", "Hello"):
        assert word not in report


def test_the_gate_is_scored_on_the_labelled_lines_with_looks_counted_apart():
    def row(i, label, named=False):
        return {"id": f"l{i}", "day": "d1", "outcome": "turn", "weight": 1.0, "named": named}, label

    rows = [
        row(0, "to_me"),
        row(1, "to_me"),
        row(2, "to_me", named=True),
        row(3, "to_me"),
        row(4, "not_to_me"),
        row(5, "noise"),
        row(6, "not_to_me"),
        row(7, "not_to_me"),
    ]
    labels = {r["id"]: {"id": r["id"], "label": label} for r, label in rows}
    did = dict(
        zip(
            [r["id"] for r, _ in rows],
            ["answer", "ack", "listen", "overheard", "answer", "ack", "listen", "overheard"],
            strict=True,
        )
    )
    d1 = noise.baseline([r for r, _ in rows], labels, did)["d1"]
    assert (d1["to_me"], d1["missed"], d1["missed_named"], d1["ack_to_me"]) == (4, 2, 1, 1)
    assert (d1["not_to_me"], d1["wrong_reply"], d1["ack_not"]) == (4, 1, 1)
    assert d1["missed_pct"] == 50.0 and d1["wrong_reply_pct"] == 25.0
    old = noise.baseline([r for r, _ in rows], labels)["d1"]  # the old gates gave each a turn
    assert (old["missed"], old["wrong_reply"], old["ack_to_me"]) == (0, 4, 0)
    assert "ack_to_me" in noise.baseline_table({"d1": d1})
