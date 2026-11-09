"""eval/replay.py: public labelled speech replayed from a Mac speaker into the robot's mic.

No network, no robot, no speaker: the LibriSpeech tree is a few FLACs written here,
Kokoro is a fake, and the "room" is a fake media whose mic hears what the fake player
played, late and quieter. The live run is the runbook in eval/script.md.
"""

import io
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np
import soundfile
from test_status import ENV, UP, fake

EVAL = Path(__file__).resolve().parent.parent / "eval"
sys.path.insert(0, str(EVAL))

import record  # noqa: E402
import replay  # noqa: E402
import score  # noqa: E402

MIC = 16000
MAC = 48000
SPEAKERS_TXT = """; Some comment
;ID  |SEX| SUBSET           |MINUTES| NAME
{rows}
9999 | F | train-clean-100  | 25.00 | Not In Test
"""


def speech(seconds: float, rate: int, seed: int = 0) -> np.ndarray:
    """Noise bursts with gaps: speech-like enough to correlate, never periodic."""
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(int(seconds * rate)) * 0.1
    envelope = (np.sin(2 * np.pi * 3 * np.arange(len(x)) / rate) > -0.3).astype(float)
    return (x * envelope).astype(np.float32)


def librispeech(root: Path, speakers: int = 24, per_speaker: int = 9) -> Path:
    """Write a miniature LibriSpeech/test-clean: SPEAKERS.TXT, transcripts, 1 s FLACs."""
    base = root / "LibriSpeech"
    rows = []
    for s in range(speakers):
        sid, chapter = str(100 + s), str(5000 + s)
        rows.append(f"{sid} | {'F' if s % 2 else 'M'} | test-clean       | 8.00 | Reader {s}")
        folder = base / "test-clean" / sid / chapter
        folder.mkdir(parents=True)
        lines = []
        for u in range(per_speaker):
            uid = f"{sid}-{chapter}-{u:04d}"
            words = 3 if u == 0 else 8  # the first of each is too short to pick
            lines.append(f"{uid} " + " ".join(["WORD"] * words))
            soundfile.write(
                folder / f"{uid}.flac", speech(1.0, 16000, seed=s * 100 + u) * 0.2, 16000
            )
        (folder / f"{sid}-{chapter}.trans.txt").write_text("\n".join(lines) + "\n")
    (base / "SPEAKERS.TXT").write_text(SPEAKERS_TXT.format(rows="\n".join(rows)))
    return base


def read_wav(path) -> tuple[np.ndarray, int]:
    with wave.open(str(path)) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16).reshape(-1, w.getnchannels())
        return pcm, w.getframerate()


def dbfs(x: np.ndarray) -> float:
    return 20 * np.log10(np.sqrt(np.mean(np.square(x.astype(np.float64)))))


# Fetch --------------------------------------------------------------------------------------


def test_speakers_are_read_for_test_clean_only():
    genders = replay.parse_speakers(
        SPEAKERS_TXT.format(rows="1089 | M | test-clean | 8.0 | A\n121  | F | test-clean | 8.0 | B")
    )
    assert genders == {"1089": "M", "121": "F"}


def test_pick_balances_gender_across_at_least_20_speakers_and_skips_short_lines(tmp_path):
    base = librispeech(tmp_path)
    genders = replay.parse_speakers((base / "SPEAKERS.TXT").read_text())
    utterances = replay.parse_transcripts(base / "test-clean")
    picked = replay.pick(genders, utterances, n=150)
    assert len(picked) == 150 and len({uid for uid, _ in picked}) == 150
    by_speaker = {uid.split("-")[0] for uid, _ in picked}
    assert len(by_speaker) >= 20
    assert sum(genders[uid.split("-")[0]] == "F" for uid, _ in picked) == 75
    assert all(not uid.endswith("-0000") for uid, _ in picked)  # 3 words: too short
    assert picked == replay.pick(genders, utterances, n=150)  # repeatable


def test_fetch_writes_mac_rate_clips_and_a_labelled_manifest_with_synthetic_commands(tmp_path):
    base = librispeech(tmp_path / "corpus")
    spoken = []

    def synthesize(text, voice):
        spoken.append((text, voice))
        return speech(0.8, 24000, seed=len(spoken)), 24000

    entries = replay.fetch(base, tmp_path / "source", MAC, synthesize, n=150, synthetic=30)
    manifest = [
        json.loads(x) for x in (tmp_path / "source" / "manifest.jsonl").read_text().splitlines()
    ]
    assert manifest == entries and len(entries) == 180
    real = [e for e in entries if not e["synthetic"]]
    fake_ = [e for e in entries if e["synthetic"]]
    assert len(real) == 150 and len(fake_) == 30
    assert all(
        e["licence"] == "CC BY 4.0" and e["corpus"] == "librispeech-test-clean" for e in real
    )
    assert {e["gender"] for e in real} == {"F", "M"}
    assert real[0]["text"] == "WORD WORD WORD WORD WORD WORD WORD WORD"
    # The synthetic lines are the script's own command and name lines, in several Kokoro voices.
    script = {line["script_id"]: line["text"] for line in record.parse_script(EVAL / "script.md")}
    assert all(script[e["script_id"]] == e["text"] for e in fake_)
    assert {e["script_id"][0] for e in fake_} == {"C", "N"}
    assert all(e["corpus"] == "kokoro" and e["source_speaker"].startswith("kokoro:") for e in fake_)
    assert len({voice for _, voice in spoken}) >= 6
    assert {e["gender"] for e in fake_} == {"F", "M"}
    for entry in (real[0], fake_[0]):
        pcm, rate = read_wav(tmp_path / "source" / entry["wav"])
        assert rate == MAC and pcm.shape[1] == 1
        assert abs(dbfs(pcm / 32768) - replay.TARGET_DBFS) < 1.0  # every clip at one loudness


# Playback, alignment, levels ----------------------------------------------------------------


def test_prepare_pads_silence_and_mixes_music_at_the_snr():
    voice = speech(2.0, MAC, seed=1)
    music = (np.random.default_rng(2).standard_normal(5 * MAC) * 0.1).astype(np.float32)  # steady
    out = replay.prepare(voice, MAC, lead_s=0.5, tail_s=0.5)
    assert len(out) == len(voice) + MAC and not out[: MAC // 2].any() and not out[-MAC // 2 :].any()
    mixed = replay.prepare(voice, MAC, music=music, snr_db=10.0, lead_s=0.5, tail_s=0.5)
    assert len(mixed) == len(out)
    music_power = np.mean(np.square(mixed[: MAC // 2]))  # the lead: music alone
    voice_power = np.mean(np.square(mixed[MAC // 2 : MAC // 2 + len(voice)])) - music_power
    assert abs(10 * np.log10(voice_power / music_power) - 10.0) < 1.0
    assert np.max(np.abs(mixed)) <= 10 ** (-1 / 20) + 1e-6  # never louder than -1 dBFS


def room(
    played: np.ndarray,
    rate: int,
    delay_s: float,
    gain_l: float = 0.3,
    gain_r: float = 0.2,
    extra_r_s: float = 0.0,
) -> np.ndarray:
    """Return what the robot's mic records of ``played``: at 16 kHz, late, quieter, int16 stereo."""
    mono = replay.resample(played, rate, MIC)  # a real mic is band-limited, as this is
    lead = np.zeros(int(delay_s * MIC))
    left = np.concatenate([lead, mono * gain_l, np.zeros(MIC)])
    right = np.concatenate([lead, np.zeros(int(extra_r_s * MIC)), mono * gain_r, np.zeros(MIC)])[
        : len(left)
    ]
    return (np.column_stack([left, right]) * 32767).astype(np.int16)


def test_align_measures_the_acoustic_delay_of_each_channel():
    played = replay.prepare(speech(2.0, MAC, seed=3), MAC)
    found = replay.align(room(played, MAC, 0.31, extra_r_s=0.02), played, MAC)
    assert abs(found["delay_s"]["L"] - 0.31) < 0.002
    assert abs(found["delay_s"]["R"] - 0.33) < 0.002
    assert found["xcorr"]["L"] > 0.9


def test_levels_flag_clipping_and_speech_outside_minus_6_to_minus_20():
    good = (np.column_stack([np.full(100, 0.2), np.full(100, -0.15)]) * 32767).astype(np.int16)
    found = replay.levels(good)
    assert round(found["peak_dbfs"]["L"], 1) == -14.0 and found["level_ok"]
    quiet = good // 8
    assert not replay.levels(quiet)["level_ok"]
    hot = good.copy()
    hot[3, 1] = 32767
    found = replay.levels(hot)
    assert found["clipped"]["R"] == 1 and not found["level_ok"]


# The run ------------------------------------------------------------------------------------


class RoomMedia:
    """The robot's mic in a room: it hears what ``play`` played, ``delay_s`` late."""

    def __init__(self, delay_s: float, gain: float = 0.3):
        self.delay_s, self.gain, self.queue = delay_s, gain, []

    def get_audio_sample(self):
        return self.queue.pop(0) if self.queue else None

    def play(self, path):
        pcm, rate = read_wav(path)
        heard = room(pcm[:, 0] / 32768, rate, self.delay_s, self.gain, self.gain * 0.8) / 32768
        self.queue.extend(
            heard[i : i + 1600].astype(np.float32) for i in range(0, len(heard), 1600)
        )
        time.sleep(0.01)


def sources(folder: Path, n: int = 3) -> Path:
    folder.mkdir(parents=True)
    lines = []
    for i in range(n):
        name = f"clip{i}.wav"
        soundfile.write(folder / name, speech(1.0, MAC, seed=10 + i) * 2, MAC, subtype="PCM_16")
        lines.append(
            {
                "wav": name,
                "text": f"line {i}",
                "script_id": f"U{i}",
                "corpus": "librispeech-test-clean",
                "source_speaker": "100",
                "gender": "F",
                "synthetic": False,
                "licence": "CC BY 4.0",
            }
        )
    (folder / "manifest.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    return folder


META = {"distance_m": 0.5, "condition": "quiet", "session": "r1", "channels": record.CHANNEL_MAP}


def test_run_records_each_clip_aligns_it_and_writes_a_scoreable_set(tmp_path):
    media = RoomMedia(delay_s=0.25)
    out = tmp_path / "replay"
    entries = replay.run(
        media,
        sources(tmp_path / "source"),
        out,
        META,
        media.play,
        tail_s=0.05,
        playback={"device": "MacBook Speakers", "volume": 50},
    )
    assert [e["text"] for e in entries] == ["line 0", "line 1", "line 2"]
    first = entries[0]
    assert (
        first["speaker"] == "replay"
        and first["source_speaker"] == "100"
        and first["synthetic"] is False
    )
    assert first["channels"] == record.CHANNEL_MAP and first["distance_m"] == 0.5
    assert abs(first["delay_s"]["L"] - 0.25) < 0.005
    assert set(first["peak_dbfs"]) == {"L", "R"} and "level_ok" in first
    pcm, rate = read_wav(out / "set" / first["wav"])
    assert rate == MIC and pcm.shape[1] == 2
    assert len(pcm) < 1.0 * MIC + 2 * replay.PAD_S * MIC + 2  # trimmed to the speech, padded
    assert first["sha256"] == score.file_sha256(out / "set" / first["wav"])
    # The raw take stays, and the delay summary is saved for the dance lead time.
    assert (out / "raw" / "manifest.jsonl").exists()
    summary = json.loads((out / "set" / "r1" / "run.json").read_text())
    assert abs(summary["delay_s"]["L"]["median"] - 0.25) < 0.005 and summary["n"] == 3
    assert summary["playback"] == {"device": "MacBook Speakers", "volume": 50}
    # score.py reads it as any recorded set.
    clips = score.load_manifest(out / "set", "R")
    assert len(clips) == 3 and all(Path(c["path"]).exists() for c in clips)


def test_run_with_music_labels_the_snr_and_a_limit_stops_early(tmp_path):
    media = RoomMedia(delay_s=0.1)
    music = tmp_path / "song.wav"
    soundfile.write(music, speech(3.0, MAC, seed=99), MAC, subtype="PCM_16")
    entries = replay.run(
        media,
        sources(tmp_path / "source"),
        tmp_path / "out",
        {**META, "condition": "music"},
        media.play,
        music=music,
        snr_db=5.0,
        limit=2,
        tail_s=0.05,
    )
    assert len(entries) == 2 and all(
        e["snr_db"] == 5.0 and e["condition"] == "music" for e in entries
    )


def test_record_replay_takes_labels_by_file_name(tmp_path):
    folder = sources(tmp_path / "source", n=1)
    media = RoomMedia(delay_s=0.0)
    lines = record.replay(
        media,
        tmp_path / "out",
        folder,
        {**META, "speaker": "replay"},
        media.play,
        tail_s=0.0,
        labels={"clip0.wav": {"script_id": "U0", "text": "line 0", "gender": "F"}},
    )
    assert (lines[0]["script_id"], lines[0]["text"], lines[0]["gender"]) == ("U0", "line 0", "F")


def test_score_stt_and_paired_take_a_speaker(tmp_path, capsys):
    data = tmp_path / "set"
    data.mkdir()
    rows = []
    for i in range(4):
        path = data / f"{i}.wav"
        soundfile.write(path, np.zeros((1600, 2)), MIC, subtype="PCM_16")
        rows.append(
            {
                "wav": f"{i}.wav",
                "text": "look left",
                "speaker": "replay",
                "session": "r",
                "condition": "quiet",
                "distance_m": 0.5,
            }
        )
    (data / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    hyp = tmp_path / "hyp.jsonl"
    hyp.write_text("".join(json.dumps({"wav": r["wav"], "text": "look left"}) + "\n" for r in rows))
    ledger = tmp_path / "LEDGER.md"
    score.main(
        ["stt", str(data), "--hyp", str(hyp), "--speaker", "replay", "--ledger", str(ledger)]
    )
    assert "| 4 |" in ledger.read_text().splitlines()[-1]
    score.main(
        [
            "paired",
            str(data),
            "--a",
            str(hyp),
            "--b",
            str(hyp),
            "--speaker",
            "replay",
            "--ledger",
            str(ledger),
        ]
    )
    assert "| 4 |" in ledger.read_text().splitlines()[-1]


# Preflight ----------------------------------------------------------------------------------

ROBOT_OK = "journal: persistent\nguard: loaded\nguard: at boot\n"


class FakeSounddevice:
    def __init__(self, name="MacBook Pro Speakers", rate=48000.0):
        self.device = {
            "name": name,
            "default_samplerate": rate,
            "default_high_output_latency": 0.03,
        }

    def query_devices(self, kind=None):
        assert kind == "output"
        return self.device


def runner(
    bot=(1, ""),
    volume=(0, "output volume:50, input volume:60, alert volume:100, output muted:false"),
):
    def run(command):
        if command[0] == "make":
            return 0, ROBOT_OK
        if command[0] == "pgrep":
            return bot
        return volume

    return run


def preflight(answers=UP, run=None, sd=None):
    out = io.StringIO()
    code = replay.preflight(ENV, fake(answers), run or runner(), sd or FakeSounddevice(), out)
    lines = {line.split()[0]: line for line in out.getvalue().splitlines()}
    return code, lines


def test_preflight_passes_and_notes_the_output_device_and_volume():
    code, lines = preflight()
    assert code == 0
    assert set(lines) == {"robot", "beam", "check", "bot", "output"}
    assert (
        "MacBook Pro Speakers" in lines["output"]
        and "48000" in lines["output"]
        and "50" in lines["output"]
    )


def test_preflight_fails_on_an_unrouted_beam_a_running_bot_or_a_muted_mac():
    code, lines = preflight(
        {
            **UP,
            "http://reachy-mini.local:8000/api/audio/config/parameter/AUDIO_MGR_OP_R": {
                "values": [0, 0]
            },
        }
    )
    assert code == 1 and "FAIL" in lines["beam"]
    code, lines = preflight(run=runner(bot=(0, "4242\n")))
    assert code == 1 and "FAIL" in lines["bot"] and "4242" in lines["bot"]
    code, lines = preflight(run=runner(volume=(0, "output volume:40, output muted:true")))
    assert code == 1 and "FAIL" in lines["output"]
