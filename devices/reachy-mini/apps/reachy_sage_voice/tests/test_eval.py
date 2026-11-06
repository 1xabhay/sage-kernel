"""The offline eval harness (eval/): standard metrics per stage, on synthetic fixtures.

No real voice anywhere: audio is numpy tones, backends are fakes. The live
services and the kernel's journal are only touched by the CLIs, behind flags.
"""

import hashlib
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np
import pytest

EVAL = Path(__file__).resolve().parent.parent / "eval"
sys.path.insert(0, str(EVAL))

import record  # noqa: E402
import score  # noqa: E402

KERNEL = Path(score.kernel_dir())
RATE = 16000


def write_wav(path: Path, samples: np.ndarray) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return str(path)


def tone(seconds: float) -> np.ndarray:
    return 0.3 * np.sin(2 * np.pi * 220 * np.arange(int(seconds * RATE)) / RATE)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * RATE))


# STT


def test_normalize_lowercases_strips_punctuation_and_spells_numbers():
    assert score.normalize("Hello, Sage! It's 3 o'clock.") == "hello sage its three oclock"
    assert score.normalize("Released in 1982, 21 weeks at #1") == (
        "released in nineteen eighty two twenty one weeks at one")
    assert score.normalize("2005 and 2019 and 1900") == (
        "two thousand five and twenty nineteen and nineteen hundred")
    assert score.normalize("1,000 fans, 100 shows") == "one thousand fans one hundred shows"
    assert score.normalize("nineteen eighty-two") == score.normalize("1982")


def test_edit_distance_counts_substitutions_insertions_and_deletions():
    assert score.edit_distance("kitten", "sitting") == 3
    assert score.edit_distance([], ["a"]) == 1
    assert score.edit_distance(["a", "b"], ["a", "b"]) == 0


def test_wer_and_cer_are_corpus_level_against_known_examples():
    ref = "the cat sat on the mat"
    assert score.wer([ref], ["the cat sat on mat"]) == pytest.approx(1 / 6)
    assert score.wer([ref], ["a cat sat on the mat today"]) == pytest.approx(2 / 6)
    assert score.wer([ref, "yes"], ["the cat sat on the mat", "no"]) == pytest.approx(1 / 7)
    assert score.cer(["abc"], ["abd"]) == pytest.approx(1 / 3)


def test_stt_report_breaks_down_by_condition_and_scores_names():
    clips = [
        {"path": "a", "text": "Sage, stop.", "speaker": "owner", "condition": "quiet", "distance_m": 0.5},
        {"path": "b", "text": "Hey Reachy, play it", "speaker": "owner", "condition": "music",
         "distance_m": 1.5},
        {"path": "c", "text": "whatever the TV said", "speaker": "other", "condition": "tv",
         "distance_m": 1.5},
    ]
    heard = {"a": "sage stop", "b": "hey richie play it"}
    report = score.stt_report(clips, heard.__getitem__)
    rows = {row["group"]: row for row in report["rows"]}
    assert rows["all"]["n"] == 2  # owner clips only
    assert rows["all"]["wer"] == pytest.approx(1 / 6)
    assert rows["quiet"]["wer"] == 0 and rows["music"]["wer"] == pytest.approx(1 / 4)
    assert rows["1.5m"]["n"] == 1
    assert report["names"] == {"sage": 1.0, "reachy": 0.0}


def test_stt_cli_prints_a_table_and_appends_a_ledger_row(tmp_path, capsys):
    data = tmp_path / "set"
    data.mkdir()
    write_wav(data / "s01.wav", tone(0.5))
    (data / "manifest.jsonl").write_text(json.dumps(
        {"wav": "s01.wav", "text": "Yes", "speaker": "owner", "distance_m": 0.5,
         "condition": "quiet", "script_id": "S01", "session": "x"}) + "\n")
    hyp = tmp_path / "hyp.jsonl"
    hyp.write_text(json.dumps({"wav": "s01.wav", "text": "yes."}) + "\n")
    ledger = tmp_path / "LEDGER.md"
    score.main(["stt", str(data), "--hyp", str(hyp), "--option", "parakeet", "--ledger", str(ledger)])
    assert "wer" in capsys.readouterr().out
    rows = ledger.read_text().splitlines()
    assert rows[-1].startswith("| ") and "| stt | parakeet | set |" in rows[-1]
    assert "wer 0.000" in rows[-1]
    assert "| 1 | [0.000, 0.000] |" in rows[-1] and score.manifest_sha256(data) in rows[-1]


def test_stt_cli_refuses_to_guess_a_backend(tmp_path):
    (tmp_path / "manifest.jsonl").write_text("")
    with pytest.raises(SystemExit):
        score.main(["stt", str(tmp_path), "--ledger", str(tmp_path / "L.md")])


# Speaker verification


def test_eer_and_frr_at_far_on_known_scores():
    trials = [(0.9, True), (0.8, True), (0.4, True), (0.5, False), (0.3, False), (0.2, False)]
    assert score.eer(trials) == pytest.approx(1 / 3)
    assert score.frr_at_far(trials, 0.01) == pytest.approx(1 / 3)
    separated = [(0.9, True), (0.7, True), (0.2, False)]
    assert score.eer(separated) == 0 and score.frr_at_far(separated, 0.01) == 0


# Turn segmentation


def test_turn_errors_count_false_splits_and_merges():
    lines = [(0, 2), (2, 4), (4, 6)]
    turns = [(0.1, 0.8), (1.0, 1.9), (2.1, 5.5)]
    assert score.turn_errors(lines, turns) == {"lines": 3, "split": 1, "merged": 2, "missed": 0}
    assert score.turn_errors(lines, [(0.1, 1.9)])["missed"] == 2


def test_turns_report_concatenates_each_session_in_order(tmp_path):
    a = write_wav(tmp_path / "a.wav", np.concatenate([silence(0.3), tone(0.5), silence(0.3)]))
    b = write_wav(tmp_path / "b.wav", np.concatenate(  # one line, a long mid-sentence pause
        [silence(0.3), tone(0.4), silence(0.8), tone(0.4), silence(0.3)]))
    clips = [{"path": p, "text": "line", "speaker": "owner", "condition": "quiet", "distance_m": 0.5,
              "session": "s"} for p in (a, b)]

    def energy_turns(pcm, rate):  # a stand-in VAD: runs of loud 10 ms frames, gaps < 0.5 s merged
        loud = np.abs(pcm[: len(pcm) // 160 * 160].reshape(-1, 160)).mean(axis=1) > 1000
        runs, start = [], None
        for i, on in enumerate([*loud, False]):
            if on and start is None:
                start = i
            elif not on and start is not None:
                runs.append([start / 100, i / 100])
                start = None
        merged = [runs[0]]
        for s, e in runs[1:]:
            if s - merged[-1][1] < 0.5:
                merged[-1][1] = e
            else:
                merged.append([s, e])
        return [tuple(r) for r in merged]

    rows = {row["group"]: row for row in score.turns_report(clips, energy_turns)}
    assert rows["all"]["lines"] == 2
    assert rows["all"]["split_rate"] == 0.5 and rows["all"]["merge_rate"] == 0


# Latency


def test_latency_summary_reads_the_bench_output():
    text = ("t1 'hi' decided 1.2s retries 0 prompt - notes no stt 120 ttft 400 first_audio 700\n"
            "t2 'yo' decided 1.0s retries 0 prompt - notes no stt 80 ttft - first_audio 900\n"
            "t3 'ok' decided 1.0s retries 0 prompt - notes no stt 100 ttft 300 first_audio 800\n")
    summary = score.latency_summary(score.latency_from_text(text))
    assert summary["stt_ms"] == {"n": 3, "p50": 100, "p95": 120}
    assert summary["ttft_ms"] == {"n": 2, "p50": 300, "p95": 400}
    assert summary["first_audio_ms"]["p50"] == 800


# Song Q&A


def test_song_qa_set_covers_three_songs_ten_questions_each():
    items = score.load_jsonl(EVAL / "song_qa.jsonl")
    assert len(items) == 30
    songs = {i["song"] for i in items}
    assert len(songs) == 3
    for item in items:
        assert item["question"] and item["answer_keywords"] and item["artist"]
        assert isinstance(item.get("time_sensitive", False), bool)
    assert any(i.get("time_sensitive") for i in items)


def test_keywords_match_normalized_with_alternatives():
    assert score.has_keywords("It came out on January 2, 1983.", ["1983"])
    assert score.has_keywords("Quincy Jones produced it", ["quincy jones|q"])
    assert score.has_keywords("released nineteen eighty-three", ["1983"])
    assert not score.has_keywords("Quincy produced it", ["quincy jones"])
    assert not score.has_keywords("in 19830", ["1983"])


def test_song_qa_scores_recall_at_k_and_answers_with_backends():
    items = [{"song": "S", "artist": "A", "question": "who produced it", "answer_keywords": ["quincy"]},
             {"song": "S", "artist": "A", "question": "when did it come out", "answer_keywords": ["1983"]},
             {"song": "S", "artist": "A", "question": "tour dates", "answer_keywords": ["arena"],
              "time_sensitive": True}]
    passages = {"S": ["S by A: a card", "Quincy Jones produced it", "Other trivia", "Out in 1983"]}

    def first_only(found, _query, k):  # a retriever that shows only the first k
        return list(found[:k])

    report = score.song_qa_report(items, passages.__getitem__, first_only, k=1,
                                  reply={"who produced it": "Quincy Jones", "when did it come out": "1982",
                                         "tour dates": "an arena"}.get)
    rows = {row["group"]: row for row in report}
    assert rows["all"]["answerable"] == 2 and rows["all"]["recall"] == 0.5  # card + 1 shown
    assert rows["all"]["accuracy"] == pytest.approx(2 / 3)
    assert rows["time-sensitive"]["n"] == 1


POLICY = KERNEL / "sage/policy.py"


# The retriever lands with the kernel's notes (kernel/notes, PLAN.md B1); until it merges, the
# kernel at hand has a policy.py without it, so the test waits for exactly that.
@pytest.mark.skipif(not POLICY.exists(), reason="no sage-kernel checkout")
@pytest.mark.skipif(POLICY.exists() and "def relevant(" not in POLICY.read_text(),
                    reason="this kernel has no sage.policy.relevant yet (kernel/notes, B1)")
def test_kernel_relevant_imports_read_only_from_a_path():
    relevant, k = score.kernel_relevant(str(KERNEL))
    assert k >= 1
    assert relevant(("a", "Quincy Jones produced it", "c"), "who produced it", k) == ["Quincy Jones produced it"]


# Recording


class FakeMedia:
    """Stereo chunks of 0.1 s, as get_audio_sample() hands them; None between some."""

    def __init__(self, chunks: int, left: float = 0.25, right: float = -0.5):
        self.left, self.recording, self.playing, self.pushed = chunks, False, False, []
        self.gaps = False
        self.chunk = np.column_stack([np.full(1600, left), np.full(1600, right)]).astype(np.float32)

    def start_recording(self):
        self.recording = True

    def stop_recording(self):
        self.recording = False

    def get_audio_sample(self):
        if not self.recording or self.left == 0:
            return None
        self.left -= 1
        if self.gaps and self.left % 2:  # a live mic: nothing ready between chunks
            return None
        return self.chunk

    def start_playing(self):
        self.playing = True

    def get_output_audio_samplerate(self):
        return 16000

    def push_audio_sample(self, data):
        assert self.playing
        self.pushed.append(data)


META = {"speaker": "owner", "distance_m": 0.5, "condition": "quiet", "session": "x",
        "channels": {"L": "conference AUDIO_MGR_OP_L=[8,0]", "R": "ASR beam AUDIO_MGR_OP_R=[7,3]"}}


def test_the_channel_map_names_the_conference_left_and_the_asr_beam_right():
    assert record.channel_map(unrouted=False) == META["channels"]
    assert record.channel_map(unrouted=True)["R"].startswith("unrouted")


def read_stereo(path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert (w.getnchannels(), w.getframerate()) == (2, 16000)
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).reshape(-1, 2)


def test_record_clip_writes_raw_stereo_and_the_channel_map(tmp_path):
    media = FakeMedia(chunks=5)
    media.start_recording()
    line = record.record_clip(media, tmp_path, {"script_id": "S01", "text": "Yes."}, META,
                              stop=lambda: media.left == 0)
    wav = tmp_path / "x/S01-0.5m-quiet-owner.wav"
    assert line == {"wav": "x/S01-0.5m-quiet-owner.wav", "text": "Yes.", "speaker": "owner",
                    "distance_m": 0.5, "condition": "quiet", "session": "x",
                    "channels": {"L": "conference AUDIO_MGR_OP_L=[8,0]", "R": "ASR beam AUDIO_MGR_OP_R=[7,3]"},
                    "script_id": "S01", "sha256": hashlib.sha256(wav.read_bytes()).hexdigest()}
    pcm = read_stereo(wav)
    assert len(pcm) == 8000
    assert np.all(pcm[:, 0] == int(0.25 * 32767)) and np.all(pcm[:, 1] == int(-0.5 * 32767))
    assert [json.loads(x) for x in (tmp_path / "manifest.jsonl").read_text().splitlines()] == [line]


def test_record_clip_copies_a_mono_mic_to_both_channels(tmp_path):
    media = FakeMedia(chunks=1)
    media.chunk = np.full(1600, 0.25, np.float32)
    media.start_recording()
    line = record.record_clip(media, tmp_path, {"script_id": "S01", "text": "Yes."}, META,
                              stop=lambda: media.left == 0)
    pcm = read_stereo(tmp_path / line["wav"])
    assert np.array_equal(pcm[:, 0], pcm[:, 1])


def test_record_clip_keeps_a_second_take_apart(tmp_path):
    for _ in range(2):
        media = FakeMedia(chunks=1)
        media.start_recording()
        last = record.record_clip(media, tmp_path, {"script_id": "S01", "text": "Yes."}, META,
                                  stop=lambda m=media: m.left == 0)
    assert last["wav"] == "x/S01-0.5m-quiet-owner-2.wav"
    assert len((tmp_path / "manifest.jsonl").read_text().splitlines()) == 2


def test_tts_take_plays_kokoro_on_the_robot_while_recording(tmp_path):
    media = FakeMedia(chunks=3)
    media.start_recording()
    spoken = []

    def synthesize(text):  # Kokoro's 24 kHz, 0.5 s
        spoken.append(text)
        return np.full(12000, 0.1, np.float32), 24000

    line = record.tts_take(media, tmp_path, {"script_id": "T01", "text": "Stop."}, META,
                           stop=lambda: media.left == 0, synthesize=synthesize, bot_line="Here is a song.")
    assert spoken == ["Here is a song."]
    assert len(media.pushed) == 1 and media.pushed[0].ndim == 1 and len(media.pushed[0]) == 8000
    assert media.pushed[0].dtype == np.float32
    assert line["tts"] == "Here is a song." and line["text"] == "Stop."
    assert len(read_stereo(tmp_path / line["wav"])) == 4800


def test_replay_plays_each_file_and_records_the_robot_unattended(tmp_path):
    source = tmp_path / "impostors"
    source.mkdir()
    for name in ("b.wav", "a.wav"):
        write_wav(source / name, tone(0.2))
    (source / "notes.txt").write_text("not audio")
    media = FakeMedia(chunks=100000)
    media.gaps = True
    media.start_recording()
    played = []

    def play(path):  # a local speaker: returns when the file has played
        played.append(Path(path).name)
        time.sleep(0.05)

    meta = {**META, "speaker": "impostor", "condition": "replay"}
    lines = record.replay(media, tmp_path / "out", source, meta, play, tail_s=0.0)
    assert played == ["a.wav", "b.wav"]
    assert [(e["script_id"], e["source"], e["text"], e["speaker"]) for e in lines] == [
        ("I001", "a.wav", "", "impostor"), ("I002", "b.wav", "", "impostor")]
    for entry in lines:
        assert len(read_stereo(tmp_path / "out" / entry["wav"])) > 0


def blocks() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for line in record.parse_script(EVAL / "script.md"):
        found.setdefault(line["script_id"][0], []).append(line["text"])
    return found


def test_script_lines_are_unique():
    lines = record.parse_script(EVAL / "script.md")
    assert len({line["script_id"] for line in lines}) == len(lines)


def test_script_has_a_name_dense_block_enough_for_200_name_tokens():
    names = blocks()["W"]
    assert 28 <= len(names) <= 32
    said = [score.normalize(t).split() for t in names]
    assert all("sage" in w or "reachy" in w for w in said)
    assert any(w[0] in score.NAMES for w in said) and any(w[-1] in score.NAMES for w in said)
    assert any(set(w[1:-1]) & set(score.NAMES) for w in said)  # mid-sentence too
    tokens = sum(w.count(n) for w in said for n in score.NAMES)
    tokens += sum(score.normalize(t).split().count(n) for t in blocks()["N"] for n in score.NAMES)
    assert tokens * 8 >= 200  # 4 conditions x 2 sessions (script.md, Targets)


def test_script_has_commands_enough_for_150_per_condition_and_negatives():
    commands, negatives = blocks()["C"], blocks()["X"]
    assert len(commands) * 4 >= 150  # 2 distances x 2 sessions per condition
    text = " ".join(score.normalize(c) for c in commands)
    for phrase in ("look left", "look right", "look up", "look down", "turn around", "search for me",
                   "track me", "stop", "listen to this song", " then "):
        assert phrase in f" {text} ", phrase
    assert len(negatives) >= 6
    assert any("looked left" in score.normalize(x) for x in negatives)


def test_script_has_tts_overlap_takes_and_song_clips_of_the_qa_songs():
    assert len(blocks()["T"]) >= 10
    songs = {i["song"] for i in score.load_jsonl(EVAL / "song_qa.jsonl")}
    clips = blocks()["M"]
    assert len(clips) >= 6 and set(clips) == songs


# Channels


def test_load_manifest_picks_a_channel_of_a_stereo_recording(tmp_path):
    from reachy_sage_voice.turn_end import load_pcm16

    media = FakeMedia(chunks=2)
    media.start_recording()
    record.record_clip(media, tmp_path, {"script_id": "S01", "text": "Yes."}, META,
                       stop=lambda: media.left == 0)
    mono = tmp_path / "old.wav"
    write_wav(mono, tone(0.1))
    with (tmp_path / "manifest.jsonl").open("a") as f:
        f.write(json.dumps({"wav": "old.wav", "text": "", "speaker": "music"}) + "\n")
    for channel, value in (("L", 0.25), ("R", -0.5), ("mean", -0.125)):
        clips = score.load_manifest(tmp_path, channel)
        with wave.open(clips[0]["path"]) as w:
            assert w.getnchannels() == 1
        pcm = load_pcm16(clips[0]["path"])
        assert len(pcm) == 3200 and np.all(np.abs(pcm - value * 32767) <= 1), channel
        assert clips[0]["wav"] == "x/S01-0.5m-quiet-owner.wav"
        assert clips[1]["path"] == str(mono)  # a mono recording is its own every channel
