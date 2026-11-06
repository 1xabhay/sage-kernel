"""SageProcessor: the body's whole conversation with the kernel, inside a real Pipecat pipeline.

The LLM is not in the pipeline. A final transcript goes to the kernel; reply
pieces stream straight to TTS; every delivered effect gets its result; an
interruption reports what was actually played. The session here is a fake that
speaks protocol v1's shapes without HTTP (sage_device's test_kernel.py covers the wire).
"""

import asyncio
import itertools
import time

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.tests.utils import SleepFrame, run_test
from sage_device import latency
from sage_device.hearing import COOLDOWN_S
from sage_device.kernel import KernelClient, KernelUnavailableError, Session

from reachy_sage_voice.sage import UNSURE_CLIP, Playback, SageProcessor
from reachy_sage_voice.voice import VoiceStyleFrame

from conftest import TOKEN

TURN = "<turn>"  # replaced by the turn id the processor chose


class FakeSession:
    """Answers polls from a script, then holds the long poll open, as a quiet kernel does."""

    def __init__(self, polls=(), admit="admitted", idle=False):
        self.polls = list(polls)
        self.admit = admit
        self.idle = idle  # answer polls before any turn: the kernel starts one itself
        self.started: list[tuple[str, str, dict]] = []
        self.results: list[dict] = []
        self.stops: list[str] = []
        self.offsets: list[int] = []
        self.inputs: list[dict | None] = []  # each turn's submitted.input
        self.acts: list[dict] = []

    async def start(self, turn_id, text, timing, input=None):
        if self.admit == "outage":
            raise KernelUnavailableError("refused")
        self.started.append((turn_id, text, timing))
        self.inputs.append(input)
        return self.admit

    async def act(self, event):
        self.acts.append(event)

    async def poll(self, offset, wait):
        while not (self.polls and (self.started or self.idle)):  # nothing to say yet: quiet
            await asyncio.sleep(0.01)
        self.offsets.append(offset)
        body = self.polls.pop(0)
        turn_id = self.started[-1][0] if self.started else None
        for effect in body.get("effects", []):
            if effect.get("turn_id") == TURN:
                effect["turn_id"] = turn_id
        if body.get("turn", {}).get("turn_id") == TURN:
            body["turn"]["turn_id"] = turn_id
        return {"sequence": 0, "effects": [], **body}

    async def result(self, sequence, status, text=None, timing=None, reason=None, turn_id=None,
                     duration_ms=None):
        r = {"sequence": sequence, "status": status, "text": text, "timing": timing,
             "reason": reason, "turn_id": turn_id, "duration_ms": duration_ms}
        self.results.append({k: v for k, v in r.items() if v is not None})

    async def stop(self, turn_id):
        self.stops.append(turn_id)


def acts(session):
    """Return the acted events sent, without their random ids, once each id is checked unique."""
    ids = [a["act_id"] for a in session.acts]
    assert len(set(ids)) == len(ids) and all(i.startswith("act-") for i in ids)
    return [{k: v for k, v in a.items() if k != "act_id"} for a in session.acts]


def aside(line):
    return {"kind": "acted", "origin": "reflex", "what": "aside", "status": "done", "said": line}


def delta(offset, text):
    return {"kind": "reply_delta", "turn_id": TURN, "offset": offset, "text": text}


def reply(sequence, text, stale=False):
    return {"sequence": sequence, "kind": "reply", "turn_id": TURN, "text": text, "stale": stale}


def emote(sequence, name):
    return {"sequence": sequence, "kind": "emote", "turn_id": TURN, "input": {"name": name},
            "stale": False}


def heard(text):
    return TranscriptionFrame(text=text, user_id="you", timestamp="")


def run(processor, frames):
    down, up = asyncio.run(run_test(processor, frames_to_send=frames))
    return down, up


def spoken(down):
    return [f.text for f in down if isinstance(f, (LLMTextFrame, TTSSpeakFrame))]


def make(session, emotes=None, playback=None, fail_emote=False, sounds=None, move_s=0.0,
         now=None, finish=frozenset()):
    """Make a processor whose clock, unless ``now`` is given, moves only while a move plays."""
    clock = [0.0]

    async def play_emote(name, sound):
        if fail_emote:
            raise RuntimeError("motor fault")
        emotes.append(name)
        if sounds is not None:
            sounds.append((name, sound))
        clock[0] += move_s
        await asyncio.sleep(move_s)
    return SageProcessor(session, play_emote, playback or Playback(),
                         now=now or (lambda: clock[0]), finish=finish)


def test_reply_pieces_stream_to_tts_as_they_arrive_then_only_the_rest():
    session = FakeSession([{"effects": [delta(0, "Hel")]},
                           {"effects": [delta(3, "lo there.")]},
                           {"effects": [reply(42, "Hello there. Bye.")]}])
    down, _ = run(make(session), [heard("hi"), SleepFrame(0.1)])
    assert session.started[0][1] == "hi"
    assert session.offsets[:3] == [0, 3, 12]
    assert spoken(down) == ["Hel", "lo there.", " Bye."]
    kinds = [type(f) for f in down if isinstance(
        f, (LLMFullResponseStartFrame, LLMTextFrame, LLMFullResponseEndFrame))]
    assert kinds == [LLMFullResponseStartFrame, LLMTextFrame, LLMTextFrame, LLMTextFrame,
                     LLMFullResponseEndFrame]


def test_a_played_reply_is_reported_done_with_its_timing():
    # The clock moves 1 ms per reading, not with the wall: on a loaded machine the transcript
    # was handled late into the 100 ms before the audio, and the gap between them shrank.
    ticks = itertools.count()
    session = FakeSession([{"effects": [delta(0, "Hello.")]}, {"effects": [reply(42, "Hello.")]}])
    run(make(session, now=lambda: next(ticks) / 1000), [
        VADUserStoppedSpeakingFrame(stop_secs=0.2), SleepFrame(0.05), heard("hi"),
        SleepFrame(0.1), BotStartedSpeakingFrame(), SleepFrame(0.05), BotStoppedSpeakingFrame(),
        SleepFrame(0.05)])
    stt_ms = session.started[0][2]["stt_ms"]
    assert 200 <= stt_ms < 1000  # end of speech is stop_secs before the VAD said so
    (result,) = session.results
    assert result["sequence"] == 42 and result["status"] == "done"
    timing = result["timing"]
    assert 0 <= timing["ttft_ms"] < 200
    assert timing["first_audio_ms"] > stt_ms  # from the same end of speech, not from the transcript


def test_reply_timing_runs_from_the_turns_own_end_of_speech():
    # Live: a later VAD stop (the robot hearing its own voice) moved the end of speech past
    # the first audio, and 168 first_audio_ms clamped to 0.
    ticks = itertools.count()
    session = FakeSession([{"effects": [delta(0, "Hello.")]}, {"effects": [reply(42, "Hello.")]}])
    run(make(session, now=lambda: next(ticks) / 1000), [
        VADUserStoppedSpeakingFrame(stop_secs=0.2), SleepFrame(0.05), heard("hi"), SleepFrame(0.1),
        VADUserStoppedSpeakingFrame(stop_secs=0.0), SleepFrame(0.02), BotStartedSpeakingFrame(),
        SleepFrame(0.05), BotStoppedSpeakingFrame(), SleepFrame(0.05)])
    stt_ms = session.started[0][2]["stt_ms"]
    (result,) = session.results
    assert result["timing"]["first_audio_ms"] > stt_ms


def holding(session, hold_s):
    async def play_emote(name, sound):
        pass
    return SageProcessor(session, play_emote, Playback(), hold_s=hold_s)


def test_an_unfinished_fragment_waits_for_the_rest_of_the_turn():
    session = FakeSession()
    run(holding(session, 0.3), [heard("Remind me to call"), SleepFrame(0.1),
                                heard("my sister tonight."), SleepFrame(0.4)])
    (said,) = [text for _, text, _ in session.started]
    assert said == "Remind me to call my sister tonight."
    assert session.stops == []  # nothing went to the kernel early


def test_an_unfinished_fragment_goes_alone_once_the_hold_is_over():
    session = FakeSession()
    run(holding(session, 0.3), [heard("So I was going to say, uh"), SleepFrame(0.1)])
    assert session.started == []
    session = FakeSession()
    run(holding(session, 0.3), [heard("So I was going to say, uh"), SleepFrame(0.5)])
    assert [text for _, text, _ in session.started] == ["So I was going to say, uh"]


def test_a_finished_sentence_is_never_held():
    session = FakeSession()
    run(holding(session, 5.0), [heard("Turn the lights on."), SleepFrame(0.05)])
    assert [text for _, text, _ in session.started] == ["Turn the lights on."]


def test_a_line_it_is_unsure_of_tilts_the_head_and_asks_no_one(caplog):
    session, emotes = FakeSession(), []
    with caplog.at_level("INFO", logger="reachy_sage_voice.sage"):
        down, _ = run(make(session, emotes), [heard("So the"), SleepFrame(0.05)])
    assert session.started == [] and spoken(down) == []
    assert emotes == [UNSURE_CLIP]
    assert "unsure: tilted" in caplog.text


def test_the_tilt_is_silent():
    session, emotes, sounds = FakeSession(), [], []
    run(make(session, emotes, sounds=sounds), [heard("So the"), SleepFrame(0.05)])
    assert sounds == [(UNSURE_CLIP, False)]


def test_a_second_noise_within_the_cooldown_does_nothing(caplog):
    session, emotes, clock = FakeSession(), [], [0.0]
    frames = [heard("So the"), SleepFrame(0.02), heard("And then"), SleepFrame(0.02)]
    with caplog.at_level("INFO", logger="reachy_sage_voice.sage"):
        run(make(session, emotes, now=lambda: clock[0]), frames)
    assert emotes == [UNSURE_CLIP] and session.started == []
    assert "unsure: quiet" in caplog.text


def test_noise_after_the_cooldown_tilts_again():
    session, emotes, ticks = FakeSession(), [], iter((100.0, 100.0 + COOLDOWN_S))
    run(make(session, emotes, now=lambda: next(ticks, 200.0)),
        [heard("So the"), SleepFrame(0.02), heard("And then"), SleepFrame(0.02)])
    assert emotes == [UNSURE_CLIP, UNSURE_CLIP]


def test_a_failed_tilt_is_only_a_warning():
    session = FakeSession()
    run(make(session, [], fail_emote=True), [heard("So the"), SleepFrame(0.05)])
    assert session.started == []


def test_noise_while_the_voice_is_busy_neither_tilts_nor_cuts_the_reply():
    playback, emotes = Playback(), []
    playback.speaking = True
    session = FakeSession()
    down, _ = run(make(session, emotes, playback=playback), [heard("So the"), SleepFrame(0.05)])
    assert emotes == [] and session.started == []
    assert not [f for f in down if isinstance(f, InterruptionFrame)]


class StillBody:
    """An executor with a say on whether the head is free, and nothing else to do."""

    def __init__(self, why=None):
        self.why, self.thoughts = why, 0

    def busy(self):
        return self.why

    def reflex_busy(self):
        return self.why

    def think(self):
        self.thoughts += 1


def test_noise_while_the_body_is_busy_does_not_tilt():
    session, emotes = FakeSession(), []

    async def play_emote(name, sound):
        emotes.append(name)
    processor = SageProcessor(session, play_emote, Playback(), executor=StillBody("a plan is running"))
    run(processor, [heard("So the"), SleepFrame(0.05)])
    assert emotes == [] and session.started == []


def test_a_real_sentence_still_goes_to_the_kernel_with_no_tilt():
    session, emotes = FakeSession(), []
    run(make(session, emotes), [heard("We had soup for dinner."), SleepFrame(0.05)])
    assert [text for _, text, _ in session.started] == ["We had soup for dinner."]
    assert emotes == []


def test_a_question_distress_and_the_name_go_to_the_kernel():
    for line in ("Teapot?", "What", "Help", "It hurts", "Sage", "Ritchie"):
        session, emotes = FakeSession(), []
        run(make(session, emotes), [heard(line), SleepFrame(0.05)])
        assert [text for _, text, _ in session.started] == [line] and emotes == []


def test_stop_wins_over_the_gate():
    # One word and no closing mark, the shape of noise: the grammar runs first.
    class Stoppable(StillBody):
        stopped = 0

        async def stop(self):
            self.stopped += 1

    session, emotes, body = FakeSession(), [], Stoppable()

    async def play_emote(name, sound):
        emotes.append(name)
    run(SageProcessor(session, play_emote, Playback(), executor=body), [heard("Stop"), SleepFrame(0.05)])
    assert body.stopped == 1 and emotes == [] and session.started == []


def test_a_held_fragment_is_judged_with_the_rest_of_the_turn():
    session, emotes = FakeSession(), []

    async def play_emote(name, sound):
        emotes.append(name)
    run(SageProcessor(session, play_emote, Playback(), hold_s=0.3),
        [heard("So the"), SleepFrame(0.1), heard("soup was cold."), SleepFrame(0.4)])
    assert [text for _, text, _ in session.started] == ["So the soup was cold."]
    assert emotes == []


def test_a_held_fragment_alone_tilts_once_the_hold_is_over():
    session, emotes = FakeSession(), []

    async def play_emote(name, sound):
        emotes.append(name)
    run(SageProcessor(session, play_emote, Playback(), hold_s=0.1),
        [heard("So the"), SleepFrame(0.3)])
    assert session.started == [] and emotes == [UNSURE_CLIP]


def test_a_fragment_after_a_turn_with_no_reply_yet_joins_it():
    # Its words lead the replacement turn, as ever: the second half of a sentence, not noise.
    session, emotes = FakeSession(), []
    run(make(session, emotes), [heard("We had soup for dinner."), SleepFrame(0.05),
                                heard("And then"), SleepFrame(0.05)])
    assert session.started[-1][1] == "We had soup for dinner. And then"
    assert emotes == []


def test_a_short_answer_to_the_question_just_asked_goes_to_the_kernel():
    session, emotes = FakeSession([{"effects": [reply(7, "Tea or soup?")]}]), []
    run(make(session, emotes), [
        heard("We had soup for dinner."), SleepFrame(0.05), BotStartedSpeakingFrame(), SleepFrame(0.02),
        BotStoppedSpeakingFrame(), SleepFrame(0.05), heard("Teapot."), SleepFrame(0.05)])
    assert session.started[-1][1] == "Teapot." and emotes == []


def test_a_short_line_long_after_a_question_is_unsure_again():
    class Finished(Playback):
        busy = False  # no BotSpeech here to say the voice has finished

    hours, emotes = itertools.count(step=3600.0), []  # every reading of the clock is an hour on
    session = FakeSession([{"effects": [reply(7, "Tea or soup?")]}])
    run(make(session, emotes, playback=Finished(), now=lambda: next(hours)), [
        heard("We had soup for dinner."), SleepFrame(0.05), BotStartedSpeakingFrame(), SleepFrame(0.02),
        BotStoppedSpeakingFrame(), SleepFrame(0.05), heard("Teapot."), SleepFrame(0.05)])
    assert emotes == [UNSURE_CLIP]


def test_interrupted_while_playing_reports_what_was_actually_played():
    playback = Playback()
    playback.spoken = ["Hello, I was going to say."]
    session = FakeSession([{"effects": [reply(42, "Hello, I was going to say. Then more.")]}])
    run(make(session, playback=playback),
        [heard("hi"), SleepFrame(0.1), BotStartedSpeakingFrame(), SleepFrame(0.05),
         InterruptionFrame(), SleepFrame(0.1)])
    assert session.results == [{"sequence": 42, "status": "interrupted",
                                "text": "Hello, I was going to say.",
                                "timing": session.results[0]["timing"]}]
    assert session.stops == []  # the turn had completed: there is nothing left to stop


def test_interrupted_while_still_streaming_reports_what_was_played_then_stops():
    playback = Playback()
    playback.spoken = ["Once upon a time,"]
    session = FakeSession([{"effects": [delta(0, "Once upon a time, there was")]}])
    run(make(session, playback=playback),
        [heard("tell me a story"), SleepFrame(0.1), BotStartedSpeakingFrame(),
         SleepFrame(0.05), InterruptionFrame(), SleepFrame(0.1)])
    turn_id = session.started[0][0]
    (result,) = session.results
    assert {k: result[k] for k in ("turn_id", "status", "text")} == {
        "turn_id": turn_id, "status": "interrupted", "text": "Once upon a time,"}
    assert "sequence" not in result
    assert session.stops == [turn_id]


def completed(*effects):
    """A poll in which the turn finished with these effects and no reply (protocol §2 Moves)."""
    return {"effects": list(effects), "turn": {"turn_id": TURN, "run_status": "completed"}}


def test_a_move_with_no_words_plays_says_nothing_and_ends_the_turn():
    emotes, sounds = [], []
    session = FakeSession([completed(emote(7, "calming1"))])
    down, _ = run(make(session, emotes, sounds=sounds, move_s=0.01), [heard("I had a long day."), SleepFrame(0.1)])
    assert emotes == ["calming1"]
    assert sounds == [("calming1", True)]  # nothing to say: the move has its own sound
    assert spoken(down) == []
    assert not [f for f in down if isinstance(f, (LLMFullResponseStartFrame, LLMFullResponseEndFrame))]
    assert [(r["sequence"], r["status"]) for r in session.results] == [(7, "done")]  # no reply to report
    assert session.stops == []


def test_a_line_after_a_silent_turn_is_its_own_turn_and_nothing_is_stopped():
    """Followed for ever, the silent turn looked still running: its words led the next line, as one message."""
    emotes = []
    session = FakeSession([completed(emote(7, "calming1"))])
    run(make(session, emotes, move_s=0.01), [heard("I had a long day."), SleepFrame(0.1),
                                             heard("Anyway, it was fine."), SleepFrame(0.1)])
    assert [text for _, text, _ in session.started] == ["I had a long day.", "Anyway, it was fine."]
    assert session.stops == []
    assert [r["status"] for r in session.results] == ["done"]


def test_a_barge_in_on_a_silent_turn_s_move_reports_only_the_move():
    emotes = []
    session = FakeSession([completed(emote(7, "calming1"))])
    run(make(session, emotes, move_s=5.0), [heard("I had a long day."), SleepFrame(0.1),
                                            InterruptionFrame(), SleepFrame(0.1)])
    assert session.results == [{"sequence": 7, "status": "interrupted"}]
    assert session.stops == []


def test_silence_with_no_move_ends_the_turn_quietly():
    session = FakeSession([completed()])
    down, _ = run(make(session), [heard("I had a long day."), SleepFrame(0.1),
                                  heard("That was fun."), SleepFrame(0.1)])
    assert spoken(down) == [] and session.results == [] and session.stops == []
    assert [text for _, text, _ in session.started] == ["I had a long day.", "That was fun."]


def test_the_kernel_s_own_turn_is_heard_after_a_silent_turn():
    session = FakeSession([completed(),
                           {"effects": [{**reply(9, "Hello again."), "turn_id": "kernel-1"}]}])
    down, _ = run(make(session), [heard("I had a long day."), SleepFrame(0.1)])
    assert spoken(down) == ["Hello again."]


def test_a_voice_style_with_no_words_does_not_reach_the_next_reply():
    style = {"sequence": 6, "kind": "voice_style", "turn_id": TURN, "input": {"style": "slow"}, "stale": False}
    session = FakeSession([completed(style)])
    down, _ = run(make(session), [heard("I had a long day."), SleepFrame(0.1)])
    assert [f.settings for f in down if isinstance(f, VoiceStyleFrame)][-1] == {}


def test_a_silent_turn_ends_its_latency_measure(caplog):
    session = FakeSession([completed()])
    with caplog.at_level("INFO", logger="reachy_sage_voice.sage"):
        run(make(session), [heard("I had a long day."), SleepFrame(0.1)])
    assert latency.SILENT in caplog.messages


def test_a_stop_effect_interrupts_the_body():
    session = FakeSession([{"effects": [reply(42, "A long answer.")]},
                           {"effects": [{"sequence": 43, "kind": "stop", "turn_id": None,
                                         "stale": False}]}])
    down, _ = run(make(session), [heard("hi"), SleepFrame(0.2)])
    assert any(isinstance(f, InterruptionFrame) for f in down)
    assert [(r["sequence"], r["status"]) for r in session.results] == [(42, "interrupted")]


def test_an_emote_effect_plays_on_the_body_and_reports_done():
    emotes = []
    session = FakeSession([{"effects": [emote(41, "cheerful1"), reply(42, "")]}])
    run(make(session, emotes, move_s=0.05), [heard("hi"), SleepFrame(0.2)])
    assert emotes == ["cheerful1"]
    (result,) = [r for r in session.results if r["sequence"] == 41]
    assert result["status"] == "done" and result["duration_ms"] == 50  # how long it took


def test_an_emote_the_body_cannot_perform_reports_failed():
    session = FakeSession([{"effects": [emote(41, "cheerful1"), reply(42, "")]}])
    run(make(session, fail_emote=True), [heard("hi"), SleepFrame(0.1)])
    assert {"sequence": 41, "status": "failed"} in session.results


def test_an_emote_waits_for_the_next_audio_boundary():
    emotes = []
    playback = Playback()
    playback.speaking = True  # mid-sentence: the move must not land on a word
    session = FakeSession([{"effects": [emote(41, "cheerful1")]}])
    run(make(session, emotes, playback=playback), [heard("hi"), SleepFrame(0.1)])
    assert emotes == []


def test_an_emote_with_nothing_playing_goes_at_once():
    emotes = []
    session = FakeSession([{"effects": [emote(41, "cheerful1")]}])  # before any reply text
    run(make(session, emotes), [heard("hi"), SleepFrame(0.1)])
    assert emotes == ["cheerful1"]
    assert session.results == [{"sequence": 41, "status": "done", "duration_ms": 0}]


def test_a_short_emote_finishes_through_a_new_turn_and_is_reported_done():
    # Owner, 2026-10-23: 44 of 108 emotes were cut by the next turn or a barge-in.
    emotes = []
    session = FakeSession([{"effects": [emote(41, "yes1"), reply(42, "")]}])
    run(make(session, emotes, move_s=0.3, finish=frozenset({"yes1"})),
        [heard("hi"), SleepFrame(0.1), InterruptionFrame(), heard("And then we ate."), SleepFrame(0.4)])
    assert emotes == ["yes1"]
    assert [r["status"] for r in session.results if r.get("sequence") == 41] == ["done"]


def test_a_long_emote_is_still_cut_by_a_new_turn():
    session = FakeSession([{"effects": [emote(41, "sad1"), reply(42, "")]}])
    run(make(session, [], move_s=0.3, finish=frozenset({"yes1"})),
        [heard("hi"), SleepFrame(0.1), heard("And then we ate."), SleepFrame(0.4)])
    assert [r["status"] for r in session.results if r.get("sequence") == 41] == ["interrupted"]


def test_with_nothing_to_say_the_move_plays_its_own_sound():
    sounds = []
    session = FakeSession([{"effects": [emote(41, "cheerful1"), reply(42, "")]}])
    run(make(session, [], sounds=sounds), [heard("hi"), SleepFrame(0.1)])
    assert sounds == [("cheerful1", True)]


def test_with_a_real_answer_to_say_the_move_is_muted():
    sounds = []
    session = FakeSession([{"effects": [emote(41, "cheerful1"),
                                        reply(42, "That is wonderful news, well done.")]}])
    run(make(session, [], sounds=sounds), [heard("hi"), SleepFrame(0.1)])
    assert sounds == [("cheerful1", False)]


def test_a_move_decided_before_the_reply_is_muted():
    sounds = []
    session = FakeSession([{"effects": [emote(41, "cheerful1")]}])  # reply not written yet
    run(make(session, [], sounds=sounds), [heard("hi"), SleepFrame(0.1)])
    assert sounds == [("cheerful1", False)]


def test_a_short_reply_is_said_first_then_the_move_sounds_after_the_voice():
    sounds, playback = [], Playback()
    session = FakeSession([{"effects": [emote(41, "yes1"), reply(42, "Yes!")]}])
    run(make(session, [], playback=playback, sounds=sounds),
        [heard("are you there"), SleepFrame(0.1)])
    assert sounds == []  # "Yes!" is queued for the voice: the sound must wait
    assert playback.busy


def test_the_voice_waits_for_a_move_sound_to_finish():
    playback = Playback()
    playback.hold_voice()  # a move is sounding
    session = FakeSession([{"effects": [delta(0, "Hello there.")]}])
    down, _ = run(make(session, playback=playback), [heard("hi"), SleepFrame(0.1)])
    assert spoken(down) == []
    playback.release_voice()


def test_a_stale_reply_is_not_spoken():
    session = FakeSession([{"effects": [reply(42, "Old news.", stale=True)]}])
    down, _ = run(make(session), [heard("hi"), SleepFrame(0.1)])
    assert spoken(down) == []
    assert session.results == [{"sequence": 42, "status": "failed", "reason": "stale"}]


def test_emoji_in_reply_pieces_are_never_spoken():
    session = FakeSession([{"effects": [delta(0, "Lovely 😊 day")]}])
    down, _ = run(make(session), [heard("hi"), SleepFrame(0.1)])
    assert spoken(down) == ["Lovely  day"]


def test_a_busy_kernel_gets_one_moment():
    session = FakeSession(admit="busy")
    down, _ = run(make(session), [heard("hi"), SleepFrame(0.05)])
    assert spoken(down) == ["One moment."]
    assert acts(session) == [aside("One moment.")]  # spoken outside a turn: the kernel hears it


def test_a_turn_says_it_came_in_as_speech_and_its_reply_is_no_act():
    session = FakeSession([{"effects": [reply(42, "Hello.")]}])
    run(make(session), [heard("hi"), SleepFrame(0.1)])
    assert session.inputs == [{"medium": "speech"}]  # the STT gives no confidence: none is sent
    assert session.acts == []


def test_an_unreachable_kernel_says_so_rather_than_nothing():
    down, _ = run(make(FakeSession(admit="outage")), [heard("hi"), SleepFrame(0.05)])
    assert spoken(down) == ["I can't reach my memory right now."]


def test_speech_while_the_kernel_is_thinking_joins_the_turn_it_replaces():
    # The kernel forgets a turn stopped before its reply, so the words go again.
    session = FakeSession()  # thinking: the long poll never answers
    run(make(session), [heard("What's the pin?"), SleepFrame(0.05), heard("Really important."),
                        SleepFrame(0.05), heard("Write it down."), SleepFrame(0.05)])
    assert [text for _, text, _ in session.started] == [
        "What's the pin?", "What's the pin? Really important.",
        "What's the pin? Really important. Write it down."]
    assert session.stops == [turn_id for turn_id, _, _ in session.started[:2]]


def test_a_fragment_before_any_reply_is_heard_adds_to_the_turn():
    # Live 2026-10-22: "So I've been thinking what should we do about", then "Nothing."
    # 0.7 s later, after the reply had begun streaming but before a word played. The
    # reply answered the first half, then was cut off. Nothing was heard, so the
    # half-turn is withdrawn (stopped, no result) and the words go again, joined.
    session = FakeSession([{"effects": [delta(0, "Well, we could")]}])
    down, _ = run(make(session), [heard("So I've been thinking what should we do about"),
                                  SleepFrame(0.7), heard("Nothing."), SleepFrame(0.05)])
    first = session.started[0][0]
    assert [text for _, text, _ in session.started] == [
        "So I've been thinking what should we do about",
        "So I've been thinking what should we do about Nothing."]
    assert session.results == []  # no empty "interrupted" reply left in memory
    assert session.stops == [first]
    assert any(isinstance(f, InterruptionFrame) for f in down)  # its words leave the voice


def test_speech_after_the_reply_was_heard_is_sent_alone_and_the_turn_kept():
    # The person heard part of the reply: the turn stays in memory with what was heard.
    playback = Playback()
    session = FakeSession([{"effects": [delta(0, "Sure, let me")]}])
    run(make(session, playback=playback),
        [heard("hi"), SleepFrame(0.05), BotStartedSpeakingFrame(), SleepFrame(0.02),
         heard("Tell me more."), SleepFrame(0.05)])
    first = session.started[0][0]
    assert [text for _, text, _ in session.started] == ["hi", "Tell me more."]
    assert [(r.get("turn_id"), r["status"]) for r in session.results] == [(first, "interrupted")]


def test_a_turn_replaced_mid_reply_clears_its_words_from_the_voice():
    # Live 10:50: "Oh" streamed, a new transcript replaced the turn, and the TTS
    # still held "Oh" when the next reply began: "OhOh, a messy screen!".
    session = FakeSession([{"effects": [delta(0, "Oh")]}])
    down, _ = run(make(session), [heard("hi"), SleepFrame(0.05), heard("more"), SleepFrame(0.05)])
    kinds = [type(f) for f in down if isinstance(f, (LLMTextFrame, InterruptionFrame))]
    assert kinds == [LLMTextFrame, InterruptionFrame]


def test_a_turn_the_kernel_starts_itself_is_spoken_and_reported():
    # A greeting on a presence "seen": no transcript started it, yet it is this body's to say.
    session = FakeSession([{"effects": [{"sequence": 7, "kind": "reply", "turn_id": "kernel-1",
                                         "text": "Hello again!", "stale": False}]}], idle=True)
    down, _ = run(make(session), [SleepFrame(0.1), BotStartedSpeakingFrame(),
                                  BotStoppedSpeakingFrame(), SleepFrame(0.1)])
    assert spoken(down) == ["Hello again!"]
    assert session.results == [{"sequence": 7, "status": "done", "timing": {}}]
    assert session.started == []


def test_after_an_interruption_the_body_still_listens_for_the_kernel():
    kernel_turn = {"sequence": 7, "kind": "reply", "turn_id": "kernel-1", "text": "Hello again!",
                   "stale": False}
    session = FakeSession([{"effects": [kernel_turn]}])

    async def greet_later():
        await asyncio.sleep(0.3)  # after the barge-in
        session.idle = True

    async def go():
        return (await asyncio.gather(run_test(make(session), frames_to_send=[
            SleepFrame(0.1), InterruptionFrame(), SleepFrame(0.4)]), greet_later()))[0]
    down, _ = asyncio.run(go())
    assert spoken(down) == ["Hello again!"]


def test_interim_transcripts_are_not_turns():
    session = FakeSession()
    run(make(session), [InterimTranscriptionFrame(text="hi", user_id="you", timestamp=""),
                        SleepFrame(0.05)])
    assert session.started == []


def test_reachy_hearing_itself_is_not_sent_to_the_kernel():
    session = FakeSession([{"effects": [reply(42, "I think the weather is lovely today.")]}])
    run(make(session), [heard("what is the weather like"), SleepFrame(0.1),
                        BotStartedSpeakingFrame(), BotStoppedSpeakingFrame(), SleepFrame(0.05),
                        heard("the weather is lovely today"), SleepFrame(0.05)])
    assert [text for _, text, _ in session.started] == ["what is the weather like"]


def test_over_http_a_streamed_turn_ends_with_a_done_result(kernel):
    """The real client and session against the scripted kernel, end to end."""
    def turn_polls(fake):
        """List the long polls since the turn was submitted (before it, the voice listens too)."""
        start = next((i for i, r in enumerate(fake.requests)
                      if (r["body"] or {}).get("kind") == "submitted"), len(fake.requests))
        return [r["query"] for r in fake.requests[start:] if r["method"] == "GET"]

    def effects(fake):
        polls = len(turn_polls(fake))
        if polls in (1, 2):
            turn = fake.events("submitted")[0]["turn_id"]
            return 200, {"sequence": 40 + polls, "effects": [
                {"kind": "reply_delta", "turn_id": turn, "offset": 0, "text": "Hi"} if polls == 1
                else {"sequence": 42, "kind": "reply", "turn_id": turn, "text": "Hi there.",
                      "stale": False}]}
        time.sleep(0.1)  # a quiet long poll
        return 200, {"sequence": 42 if polls else 40, "effects": []}

    kernel.script("POST", "/v1/hello", (200, {"protocol": 1, "sequence": 40}))
    kernel.script("POST", "/v1/events", (200, {"sequence": 41}))
    kernel.script("GET", "/v1/effects", effects)
    session = Session(KernelClient(kernel.url, TOKEN), {"senses": ["speech"], "tools": []})
    down, _ = run(SageProcessor(session, lambda name, sound: None, Playback()),
                  [heard("hello"), SleepFrame(0.2), BotStartedSpeakingFrame(),
                   BotStoppedSpeakingFrame(), SleepFrame(0.2)])
    assert spoken(down) == ["Hi", " there."]
    polls = turn_polls(kernel)
    assert polls[0]["since"] == ["40"] and polls[1]["offset"] == ["2"]
    (result,) = kernel.events("result")
    assert result["sequence"] == 42 and result["status"] == "done"
    assert set(result["timing"]) == {"ttft_ms"}  # no VAD frame here, so no first_audio_ms


def test_its_own_last_words_are_trimmed_from_the_next_turn():
    session = FakeSession([{"effects": [reply(42, "Yes, I'm here! Let's talk.")]}])
    run(make(session), [heard("you up?"), SleepFrame(0.1),
                        BotStartedSpeakingFrame(), BotStoppedSpeakingFrame(), SleepFrame(0.05),
                        heard("Let's talk. Alright, let's see."), SleepFrame(0.05)])
    assert [text for _, text, _ in session.started] == ["you up?", "Alright, let's see."]


def test_a_turn_that_is_only_its_own_echo_is_dropped():
    session = FakeSession([{"effects": [reply(42, "Yes, I'm here! Let's talk.")]}])
    run(make(session), [heard("you up?"), SleepFrame(0.1),
                        BotStartedSpeakingFrame(), BotStoppedSpeakingFrame(), SleepFrame(0.05),
                        heard("Let's talk."), SleepFrame(0.05)])
    assert [text for _, text, _ in session.started] == ["you up?"]


def test_own_tts_heard_back_is_not_a_turn():
    """P3: its own canned lines are its speech too ("One moment." heard back started a turn)."""
    session = FakeSession(admit="busy")
    run(make(session), [heard("hi"), SleepFrame(0.1), heard("One moment."), SleepFrame(0.05)])
    assert [text for _, text, _ in session.started] == ["hi"]


def test_the_words_actually_played_count_as_its_speech_too():
    playback = Playback()
    playback.spoken = ["Once", "upon", "a", "time,"]  # an interrupted reply
    session = FakeSession()
    run(make(session, playback=playback), [heard("a time, tell me another."), SleepFrame(0.05)])
    assert [text for _, text, _ in session.started] == ["tell me another."]


def test_tool_syntax_split_across_reply_pieces_is_never_spoken():
    session = FakeSession([{"effects": [delta(0, "Great news (emo")]},
                           {"effects": [delta(15, "te: cheerful1) indeed.")]},
                           {"effects": [reply(42, "Great news (emote: cheerful1) indeed.")]}])
    down, _ = run(make(session), [heard("hi"), SleepFrame(0.1)])
    said = "".join(spoken(down))
    assert "emote" not in said and "cheerful1" not in said
    assert said.split() == ["Great", "news", "indeed."]


def test_a_name_the_stt_mishears_reaches_the_kernel_corrected():
    """Live 2026-10-23: "Mika" was heard as "Micah", and remembered so."""
    from sage_device import names
    session = FakeSession()
    processor = make(session)
    processor._names = names.parse("Mika:Mee-kah:Micah|Mica")
    run(processor, [heard("My name is Micah."), SleepFrame(0.1)])
    assert session.started[0][1] == "My name is Mika."


def test_a_misheard_name_being_forgotten_reaches_the_kernel_as_it_remembers_it():
    from sage_device import names
    session = FakeSession()
    processor = make(session)
    processor._names = names.parse("Mika:Mee-kah:Micah|Mica")
    run(processor, [heard("Forget Micah, my name is Micah."), SleepFrame(0.1)])
    assert session.started[0][1] == "Forget Micah, my name is Mika."
