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
