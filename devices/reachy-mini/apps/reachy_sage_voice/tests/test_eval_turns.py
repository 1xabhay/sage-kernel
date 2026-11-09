"""eval/turn_taking.py: the owner's live turns, rebuilt from recordings and the bot's log."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

import turn_taking as tt


def log(at, message):
    return f"2026-10-23 01:53:{at:06.3f} | DEBUG    | pipecat.x:y:1 - {message}"


def test_a_recording_is_named_for_the_moment_its_segment_stopped():
    a = tt.stop_time("utt-20261023-015329-077891000.wav")
    b = tt.stop_time("utt-20261023-015331-142068000.wav")
    assert abs((b - a) - 2.064177) < 1e-6


def test_runs_break_where_nobody_spoke_for_long():
    segs = [{"start": 0, "end": 1}, {"start": 3, "end": 4}, {"start": 20, "end": 21}]
    assert [len(r) for r in tt.runs(segs, gap_s=10)] == [2, 1]


def test_the_window_lays_segments_at_their_times_with_silence_between():
    segs = [{"path": "a", "start": 0.0, "end": 0.001}, {"path": "b", "start": 0.002, "end": 0.003}]
    audio = tt.window(segs, 0.0, 0.003, load=lambda p: np.full(16, 1 if p == "a" else 2, np.int16))
    assert audio.tolist() == [1] * 16 + [0] * 16 + [2] * 16


def test_the_log_shows_barge_ins_by_cause_and_fragments_joined_or_replacing():
    lines = [
        log(1.0, "VADProcessor#0: User started speaking"),
        log(2.45, "VADProcessor#0: User stopped speaking"),
        log(2.9, "Transcription: [Tell me about the]"),
        log(3.0, "VADProcessor#0: User started speaking"),
        log(4.45, "VADProcessor#0: User stopped speaking"),
        log(4.8, "Transcription: [weather tomorrow.]"),               # no reply yet: joined
        log(6.0, "Bot started speaking"),
        log(6.9, "received interruption worker frame upstream InterruptionWorkerFrame#0"),
        log(7.0, "Bot stopped speaking"),                        # cut, and nobody spoke: a blip
        log(9.0, "Bot started speaking"),
        log(9.5, "VADProcessor#0: User started speaking"),
        log(10.0, "received interruption worker frame upstream InterruptionWorkerFrame#1"),
        log(10.2, "Bot stopped speaking"),
        log(11.0, "VADProcessor#0: User stopped speaking"),
        log(11.3, "Transcription: [Stop.]"),               # after a cut reply
    ]
    report = tt.log_report(tt.log_events(lines))
    assert report["interruptions_by_cause"] == {"speech": 1, "empty_transcript": 0, "no_speech": 1}
    assert report["transcripts"] == {"joined": 1, "replaced": 0, "after_interruption": 1,
                                     "after_reply": 0}
    assert report["owner_resumed_within_2s"] == 1 and report["replies"] == 2


def test_an_unfinished_transcript_labels_a_continuation_before_any_behaviour():
    assert tt.label("Tell me about", 99.0, 0.0, [1.0], 2.0, 0.0) == (True, "text")
    # Finished text: the owner went on before the reply's audio, or 2 s into it, or after.
    assert tt.label("Hi.", 1.5, 0.0, [2.0], 2.0, 0.0) == (True, "log")
    assert tt.label("Hi.", 3.5, 0.0, [2.0], 2.0, 0.0) == (True, "log")
    assert tt.label("Hi.", 4.5, 0.0, [2.0], 2.0, 0.0) == (False, "log")
    assert tt.label("Hi.", 1.0, 0.0, None, 2.0, 0.0) == (True, "gap")  # no log for it


def test_deciders_split_as_the_pipeline_would():
    assert tt.silence_splits(600, 600) and not tt.silence_splits(599, 600)
    assert not tt.hold_splits(1000, 600, 2000, "and then")
    assert tt.hold_splits(1000, 600, 2000, "Done.")
    assert tt.smart_splits(250, True, 1500) == (True, 200)
    assert tt.smart_splits(1000, False, 1500) == (False, 1700)


def test_rows_count_false_splits_and_the_wait_at_real_ends():
    items = [{"turn": 0, "cont": True, "a": (True, 450), "b": (False, 900)},
             {"turn": 0, "cont": False, "a": (True, 450), "b": (True, 900)},
             {"turn": 1, "cont": False, "a": (True, 450), "b": (True, 900)}]
    a, b = tt.option_rows(items, ["a", "b"], "a", n_boot=50)
    assert (a["false_splits"], b["false_splits"], b["delta_vs_base"]) == (1, 0, -1.0)
    assert (a["wait_p50_ms"], b["wait_p50_ms"]) == (450, 900)
