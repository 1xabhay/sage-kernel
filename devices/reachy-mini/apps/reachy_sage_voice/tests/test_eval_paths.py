"""Scoring the app's own paths offline: the addressed filter, and VAD segments into the STT."""

import json
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

import score  # noqa: E402

RATE = 16000


def write_wav(path: Path, samples: np.ndarray) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((samples * 32767).astype(np.int16).tobytes())
    return str(path)


def tone(pitch: float, seconds: float) -> np.ndarray:
    return 0.3 * np.sin(2 * np.pi * pitch * np.arange(int(seconds * RATE)) / RATE)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * RATE))


# Addressed


ADDRESSED = [  # (wav, text, speaker, condition, seconds, heard)
    ("a.wav", "Sage, stop.", "owner", "quiet", 0.5, "sage stop"),
    ("b.wav", "Hey Reachy.", "owner", "music", 0.5, "hey beachy"),
    ("c.wav", "Thanks, Reachy.", "owner", "quiet", 0.5, "thanks reachy"),
    ("d.wav", "", "other", "tv", 0.9, "the sage said nothing"),
    ("e.wav", "", "music", "music", 0.9, ""),
    ("f.wav", "Stop.", "owner", "quiet", 0.2, "stop"),
]


def addressed_set(folder: Path) -> list[dict]:
    folder.mkdir(exist_ok=True)
    rows = [{"wav": w, "text": t, "speaker": s, "condition": c, "session": "1"}
            for w, t, s, c, _, _ in ADDRESSED]
    for w, *_, seconds, _ in ADDRESSED:
        write_wav(folder / w, tone(220, seconds))
    (folder / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return rows


def test_addressed_report_counts_name_hits_and_false_wakes_per_hour(tmp_path):
    addressed_set(tmp_path)
    heard = {w: h for w, *_, h in ADDRESSED}
    clips = score.load_manifest(tmp_path)
    paths = {c["path"]: c["wav"] for c in clips}
    report = score.addressed_report(clips, lambda p: heard[paths[p]], aliases=("Reachy",))
    rows = {r["group"]: r for r in report["rows"]}
    assert rows["all"]["named"] == 3 and rows["all"]["name_hits"] == 2
    assert rows["all"]["name_hit_rate"] == pytest.approx(2 / 3)
    assert rows["all"]["false_wakes"] == 1  # the TV said "sage"
    assert rows["all"]["hours"] == pytest.approx(2.0 / 3600)
    assert rows["all"]["false_wakes_per_hour"] == pytest.approx(1800)
    assert rows["music"]["named"] == 1 and rows["music"]["name_hit_rate"] == 0
    assert report["ci"] == score.wilson(2, 3)


def test_addressed_report_uses_the_filters_aliases(tmp_path):
    addressed_set(tmp_path)
    heard = {w: h for w, *_, h in ADDRESSED}
    clips = score.load_manifest(tmp_path)
    paths = {c["path"]: c["wav"] for c in clips}
    report = score.addressed_report(clips, lambda p: heard[paths[p]], aliases=())
    assert report["rows"][0]["name_hits"] == 1  # "thanks reachy" no longer wakes it


def test_addressed_cli_writes_a_full_row(tmp_path, capsys):
    data = tmp_path / "set"
    addressed_set(data)
    hyp = tmp_path / "hyp.jsonl"
    hyp.write_text("".join(json.dumps({"wav": w, "text": h}) + "\n" for w, *_, h in ADDRESSED))
    ledger = tmp_path / "LEDGER.md"
    score.main(["addressed", str(data), "--hyp", str(hyp), "--ledger", str(ledger)])
    assert "false_wakes_per_hour" in capsys.readouterr().out
    row = ledger.read_text().splitlines()[-1]
    assert "| addressed |" in row and "| 3 |" in row and score.manifest_sha256(data) in row
    assert "name_hit_rate 0.667" in row


# Pipeline path


def two_words() -> np.ndarray:  # "look" at 300 Hz, a pause, "left" at 600 Hz: 2.4 s
    return np.concatenate([silence(0.3), tone(300, 0.5), silence(0.8), tone(600, 0.5), silence(0.3)])


def tone_stt(heard: list):
    def transcribe(pcm):
        heard.append(len(pcm))
        spectrum = np.abs(np.fft.rfft(pcm.astype(float)))
        energy = {w: spectrum[int(f * len(pcm) / RATE)] for w, f in (("look", 300), ("left", 600))}
        return " ".join(w for w, e in energy.items() if e > 1e5)
    return transcribe


def test_pipeline_report_crops_segments_with_pre_roll_and_the_stop_tail(tmp_path):
    clip = {"path": write_wav(tmp_path / "a.wav", two_words()), "text": "Look left.", "speaker": "owner"}
    heard: list[int] = []
    report = score.pipeline_report([clip], tone_stt(heard), lambda pcm, rate: [(0.3, 0.8), (1.6, 2.1)],
                                   pre_roll_s=1.0, stop_s=0.45)
    # segment 1: 0 (pre-roll clipped at the start) to 0.8 + 0.45; segment 2 from there (the
    # pre-roll never reaches back into a segment already sent) to the end of the clip
    assert heard == [int(1.25 * RATE), int(2.4 * RATE) - int(1.25 * RATE), int(2.4 * RATE)]
    assert report["n"] == 1 and report["segments"] == 2
    assert report["wer_pipeline"] == 0 and report["wer_oracle"] == 0


def test_pipeline_report_shows_what_segmentation_loses_against_the_oracle(tmp_path):
    clip = {"path": write_wav(tmp_path / "a.wav", two_words()), "text": "Look left.", "speaker": "owner"}
    report = score.pipeline_report([clip], tone_stt([]), lambda pcm, rate: [(0.3, 0.8)],
                                   pre_roll_s=1.0, stop_s=0.1)
    assert report["wer_pipeline"] == pytest.approx(0.5) and report["wer_oracle"] == 0
    lo, hi = report["ci"]
    assert lo <= 0.5 <= hi


def test_pipeline_cli_writes_a_full_row(tmp_path, monkeypatch, capsys):
    data = tmp_path / "set"
    data.mkdir()
    write_wav(data / "a.wav", two_words())
    (data / "manifest.jsonl").write_text(json.dumps(
        {"wav": "a.wav", "text": "Look left.", "speaker": "owner", "session": "1"}) + "\n")
    monkeypatch.setattr(score, "stt_pcm", lambda url: tone_stt([]))
    monkeypatch.setattr(score, "silero_segmenter", lambda ms: lambda pcm, rate: [(0.3, 0.8), (1.6, 2.1)])
    ledger = tmp_path / "LEDGER.md"
    score.main(["pipeline", str(data), "--stt-url", "http://127.0.0.1:1", "--ledger", str(ledger)])
    assert "wer_oracle" in capsys.readouterr().out
    row = ledger.read_text().splitlines()[-1]
    assert "| pipeline |" in row and "wer_pipeline 0.000 wer_oracle 0.000" in row
