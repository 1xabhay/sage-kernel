"""Speaker verification scoring: VAD-trimmed onset windows, leave-one-session-out, top-k enrollment.

Voices are tones: the fake embedding is the tone's pitch as an angle, so cosine similarity
falls as pitches part. The fake VAD calls anything louder than silence speech.
"""

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


def voice(pitch: float, seconds: float, lead_s: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return np.concatenate([np.zeros(int(lead_s * RATE)), 0.3 * np.sin(2 * np.pi * pitch * t),
                           np.zeros(int(0.3 * RATE))])


def pitch_embed(audio: np.ndarray) -> np.ndarray:
    pitch = np.argmax(np.abs(np.fft.rfft(audio))) * RATE / len(audio)
    return np.array([np.cos(pitch / 100), np.sin(pitch / 100)])


def loud_vad(pcm: np.ndarray, rate: int) -> list[tuple[float, float]]:
    loud = np.flatnonzero(np.abs(pcm) > 100)
    return [(loud[0] / rate, (loud[-1] + 1) / rate)] if loud.size else []


def clips_of(tmp_path, spec) -> list[dict]:
    """spec: (session, speaker, pitch, seconds) per clip."""
    return [{"path": write_wav(tmp_path / f"{i}.wav", voice(pitch, seconds)), "wav": f"{i}.wav",
             "session": session, "speaker": speaker} for i, (session, speaker, pitch, seconds) in enumerate(spec)]


OWNER = 220.0
SET = [("s1", "owner", OWNER, 2.0), ("s1", "owner", OWNER + 5, 2.0), ("s2", "owner", OWNER + 10, 2.0),
       ("s2", "owner", OWNER + 15, 0.8), ("imp", "impostor", 520.0, 2.0), ("imp", "impostor", 600.0, 1.2),
       ("imp", "music", 700.0, 2.0)]


def test_windows_are_cut_at_the_speech_onset(tmp_path):
    clips = clips_of(tmp_path, SET)
    seen = []

    def embed(audio):
        seen.append(len(audio))
        assert np.abs(audio[: RATE // 100]).max() > 0.1  # starts at the onset, not in the lead silence
        return pitch_embed(audio)

    score.speaker_report(clips, embed, loud_vad)
    assert {RATE, int(1.5 * RATE), 2 * RATE, int(0.8 * RATE), int(1.2 * RATE)} == set(seen)


def test_leave_one_session_out_never_enrolls_the_session_scored(tmp_path):
    clips = clips_of(tmp_path, SET)
    report = score.speaker_report(clips, pitch_embed, loud_vad, k=1)
    targets = [t for t in report["trials"] if t["target"]]
    assert targets and all(t["score"] < 0.9999 for t in targets)  # own session would score 1.0
    rows = {r["window"]: r for r in report["rows"]}
    assert rows["full"]["targets"] == 4 and rows["full"]["impostors"] == 3
    assert rows["1.0s"]["targets"] == 3  # the 0.8 s turn has no 1 s window
    assert rows["1.5s"]["impostors"] == 2
    assert rows["full"]["eer"] == 0 and rows["full"]["frr@far1%"] == 0
    assert rows["full"]["far_bound"] == pytest.approx(3 / 3)  # rule of 3: 3 impostors bound FAR at 1.0
    lo, hi = rows["full"]["frr_ci"]
    assert lo == 0 and 0 < hi < 1


def test_top_k_scores_the_mean_of_the_k_closest_enrollment_clips():
    enrolled = np.array([[1.0, 0.0], [np.cos(1.0), np.sin(1.0)], [np.cos(1.5), np.sin(1.5)]])
    probe = np.array([2.0, 0.0])  # not unit: cosine, not dot
    assert score.top_k_score(probe, enrolled, 1) == pytest.approx(1.0)
    assert score.top_k_score(probe, enrolled, 3) == pytest.approx((1 + np.cos(1.0) + np.cos(1.5)) / 3)
    assert score.top_k_score(probe, enrolled, 10) == pytest.approx((1 + np.cos(1.0) + np.cos(1.5)) / 3)


def test_refuses_one_session_or_no_impostors(tmp_path):
    one_session = clips_of(tmp_path, [s for s in SET if s[0] != "s2"])
    with pytest.raises(ValueError, match="session"):
        score.speaker_report(one_session, pitch_embed, loud_vad)
    no_impostors = clips_of(tmp_path, [s for s in SET if s[1] == "owner"])
    with pytest.raises(ValueError, match="impostor"):
        score.speaker_report(no_impostors, pitch_embed, loud_vad)


def test_a_clip_with_no_speech_is_counted_not_scored(tmp_path):
    clips = clips_of(tmp_path, SET)
    clips.append({"path": write_wav(tmp_path / "quiet.wav", np.zeros(RATE)), "wav": "quiet.wav",
                  "session": "imp", "speaker": "music"})
    report = score.speaker_report(clips, pitch_embed, loud_vad)
    assert report["no_speech"] == 1
    assert {r["window"]: r for r in report["rows"]}["full"]["impostors"] == 3


def test_speaker_cli_prints_the_rule_of_three_and_writes_a_full_row(tmp_path, monkeypatch, capsys):
    data = tmp_path / "set"
    data.mkdir()
    clips = clips_of(data, SET)
    (data / "manifest.jsonl").write_text("".join(
        json.dumps({k: c[k] for k in ("wav", "session", "speaker")}) + "\n" for c in clips))
    (tmp_path / "fake_embed.py").write_text(
        "import numpy as np\n"
        "def embed(audio):\n"
        "    pitch = np.argmax(np.abs(np.fft.rfft(audio))) * 16000 / len(audio)\n"
        "    return np.array([np.cos(pitch / 100), np.sin(pitch / 100)])\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(score, "silero_runs", loud_vad)
    ledger = tmp_path / "LEDGER.md"
    score.main(["speaker", str(data), "--embed", "fake_embed:embed", "--ledger", str(ledger)])
    out = capsys.readouterr().out
    assert "rule of 3" in out and "300" in out
    row = ledger.read_text().splitlines()[-1]
    assert "| speaker |" in row and "| 4 |" in row and score.manifest_sha256(data) in row


def test_speaker_cli_refuses_a_single_session(tmp_path, monkeypatch):
    data = tmp_path / "set"
    data.mkdir()
    clips = clips_of(data, [s for s in SET if s[0] != "s2"])
    (data / "manifest.jsonl").write_text("".join(
        json.dumps({k: c[k] for k in ("wav", "session", "speaker")}) + "\n" for c in clips))
    monkeypatch.setattr(score, "silero_runs", loud_vad)
    with pytest.raises(SystemExit, match="session"):
        score.main(["speaker", str(data), "--embed", "numpy:ones", "--ledger", str(tmp_path / "L.md")])
