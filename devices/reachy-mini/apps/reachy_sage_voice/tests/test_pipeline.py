"""The assembled pipeline, the bot-speech wiring, and the Reachy transport, with no robot."""

import asyncio
import random
import threading
import time
import wave

import numpy as np
import pytest
from pipecat.audio.turn.base_turn_analyzer import BaseTurnAnalyzer, EndOfTurnState
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.filters.function_filter import FunctionFilter
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.transports.base_transport import TransportParams
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.utils.text.base_text_aggregator import AggregationType

from reachy_sage_voice import pipeline as voice_pipeline
from reachy_sage_voice import transport as voice_transport
from reachy_sage_voice.clause import ClauseTextAggregator
from reachy_sage_voice.interruption import InterruptionTracker
from reachy_sage_voice.manifest import EMOTIONS
from reachy_sage_voice.pipeline import ConfigError, RobotLinkLostError, build
from reachy_sage_voice.sage import BotSpeech, Playback, SageProcessor
from reachy_sage_voice.transport import (
    PLAYBACK_LEAD_S,
    ReachyInputTransport,
    ReachyOutputTransport,
    to_float32,
    to_pcm16,
)
from reachy_sage_voice.tts import KokoroTTSService

ENV = {
    "SAGE_VOICE_KERNEL_URL": "http://127.0.0.1:8776",
    "SAGE_VOICE_KERNEL_TOKEN": "t" * 32,
    "SAGE_VOICE_STT_URL": "http://127.0.0.1:8001/v1",
    "SAGE_VOICE_TTS_URL": "http://127.0.0.1:8880/v1",
}


class FakeMedia:
    def __init__(self, frames=()):
        self.frames = list(frames)
        self.pushed: list[np.ndarray] = []
        self.calls: list[str] = []

    def start_recording(self):
        self.calls.append("start_recording")

    def stop_recording(self):
        self.calls.append("stop_recording")

    def get_audio_sample(self):
        return self.frames.pop(0) if self.frames else None

    def start_playing(self):
        self.calls.append("start_playing")

    def stop_playing(self):
        self.calls.append("stop_playing")

    def push_audio_sample(self, data):
        self.pushed.append(data)

    def get_output_audio_samplerate(self):
        return 16000

    def play_sound(self, sound_file):
        self.calls.append(sound_file)


class Move:
    duration, description = 4.0, "A move."


MOVES = dict.fromkeys(EMOTIONS, Move())


async def no_emote(name, sound):
    pass


# --- assembly -------------------------------------------------------------------


def test_the_pipeline_runs_mic_to_speaker_through_the_kernel():
    pipeline = build(FakeMedia(), no_emote, MOVES, ENV)
    kinds = [type(p) for p in pipeline.processors[1:-1]]  # inside source and sink
    assert kinds == [ReachyInputTransport, VADProcessor, voice_pipeline.SegmentSTTService,
                     SageProcessor, KokoroTTSService, ReachyOutputTransport, BotSpeech]


def test_with_an_executor_the_kernel_hears_of_the_body_and_the_dance_hears_the_mic():
    from types import SimpleNamespace

    executor = SimpleNamespace(audio=None)
    pipeline = build(FakeMedia(), no_emote, MOVES, ENV, executor=executor)
    sage = next(p for p in pipeline.processors if isinstance(p, SageProcessor))
    mic = next(p for p in pipeline.processors if isinstance(p, ReachyInputTransport))
    assert sage._executor is executor and executor.audio == mic.snippet
    declared = sage._session.manifest
    assert "body" in declared and declared["tools"][-1]["name"] == "body_plan"
    plain = next(p for p in build(FakeMedia(), no_emote, MOVES, ENV).processors
                 if isinstance(p, SageProcessor))
    assert "body" not in plain._session.manifest and plain._executor is None


def test_the_vad_edges_reach_the_body_and_its_own_voice_hushes_them():
    from types import SimpleNamespace

    told = []
    executor = SimpleNamespace(audio=None, perk=lambda hushed: told.append(("perk", hushed)),
                               nod=lambda hushed: told.append(("nod", hushed)),
                               unperk=lambda: told.append(("unperk",)))
    pipeline = build(FakeMedia(), no_emote, MOVES, ENV, executor=executor)
    vad = next(p for p in pipeline.processors if isinstance(p, VADProcessor))._vad_controller
    analyzer = vad._vad_analyzer
    analyzer._on_voice()
    analyzer._on_pause()
    analyzer._on_quiet()
    next(p for p in pipeline.processors if isinstance(p, SageProcessor))._playback.queued = True
    analyzer._on_voice()
    assert told == [("perk", False), ("nod", False), ("unperk",), ("perk", True)]


def test_short_emotes_are_the_ones_that_finish_through_a_new_turn():
    class Short:
        duration, description = 2.1, "A nod."
    sage = next(p for p in build(FakeMedia(), no_emote, {**MOVES, "yes1": Short()}, ENV).processors
                if isinstance(p, SageProcessor))
    assert sage._finish == {"yes1"}  # the others last 4 s, past EMOTE_FINISH_S


def test_tts_is_fed_a_clause_at_a_time_with_a_kokoro_voice():
    pipeline = build(FakeMedia(), no_emote, MOVES, {**ENV, "SAGE_VOICE_TTS_VOICE": "bf_emma"})
    tts = next(p for p in pipeline.processors if isinstance(p, KokoroTTSService))
    assert isinstance(tts._text_aggregator, ClauseTextAggregator)
    assert tts._text_aggregator._aggregation_type == AggregationType.SENTENCE
    assert tts._settings.voice == "bf_emma"


def test_the_vad_reports_onset_for_barge_in():
    pipeline = build(FakeMedia(), no_emote, MOVES, ENV)
    vad = next(p for p in pipeline.processors if isinstance(p, VADProcessor))
    assert vad._vad_controller._vad_analyzer._on_detected is not None
    assert vad._vad_controller._vad_analyzer._on_cleared is not None


def test_only_addressed_speech_reaches_the_kernel_when_asked_for():
    pipeline = build(FakeMedia(), no_emote, MOVES, {**ENV, "SAGE_VOICE_ADDRESSED": "1"},
                     face=lambda: True)
    kinds = [type(p) for p in pipeline.processors[1:-1]]
    assert kinds[2:5] == [voice_pipeline.SegmentSTTService, FunctionFilter, SageProcessor]
    assert pipeline.processors[4]._filter._face() is True


@pytest.mark.parametrize("missing", sorted(ENV))
def test_every_url_and_the_token_must_be_configured(missing):
    env = {k: v for k, v in ENV.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        build(FakeMedia(), no_emote, MOVES, env)


# --- bot speech into the interruption policy -------------------------------------


def test_a_sentence_start_yields_to_someone_still_talking():
    tracker = InterruptionTracker(min_bot_speech_ms=50)
    playback = Playback()
    tracker.user_onset(time.monotonic(), "you")  # started before the bot, still going
    down, _ = asyncio.run(run_test(BotSpeech(tracker, playback), frames_to_send=[
        BotStartedSpeakingFrame(), SleepFrame(0.1),
        TTSTextFrame("Hello there.", aggregated_by=AggregationType.SENTENCE), SleepFrame(0.1)]))
    assert any(isinstance(f, InterruptionFrame) for f in down)
    assert tracker.interruptions == 1


def test_played_sentences_are_recorded_and_the_response_closes_on_stop():
    tracker = InterruptionTracker()
    playback = Playback()
    playback.spoken = ["from the last reply"]
    asyncio.run(run_test(BotSpeech(tracker, playback), frames_to_send=[
        BotStartedSpeakingFrame(),
        TTSTextFrame("Hello there.", aggregated_by=AggregationType.SENTENCE),
        BotStoppedSpeakingFrame(), SleepFrame(0.05)]))
    assert playback.spoken == ["Hello there."]
    assert tracker.user_onset(time.monotonic() + 10, "you") is False  # no response open


# --- the Reachy transport ----------------------------------------------------------


def test_mic_samples_become_mono_pcm16():
    stereo = np.array([[0.5, 0.5], [-1.0, -1.0], [2.0, 2.0]], dtype=np.float32)
    assert np.frombuffer(to_pcm16(stereo), dtype=np.int16).tolist() == [16383, -32767, 32767]


def test_one_mic_channel_can_be_chosen_over_the_mean():
    stereo = np.array([[0.5, -0.5]], dtype=np.float32)
    assert [np.frombuffer(to_pcm16(stereo, ch), dtype=np.int16).tolist()
            for ch in ("mean", "0", "1")] == [[0], [16383], [-16383]]


def test_the_mic_channel_comes_from_the_environment():
    def channel(env):
        return next(p for p in build(FakeMedia(), no_emote, MOVES, env).processors
                    if isinstance(p, ReachyInputTransport))._mic_channel

    assert channel(ENV) == "mean"
    assert channel({**ENV, "SAGE_VOICE_MIC_CHANNEL": "1"}) == "1"
    with pytest.raises(ConfigError, match="SAGE_VOICE_MIC_CHANNEL"):
        build(FakeMedia(), no_emote, MOVES, {**ENV, "SAGE_VOICE_MIC_CHANNEL": "left"})


def test_each_utterance_can_be_recorded_as_raw_stereo(tmp_path):
    chunk = np.tile(np.array([[0.5, -0.25]], dtype=np.float32), (320, 1))
    media = FakeMedia([chunk] * 3)
    params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=16000)
    transport = ReachyInputTransport(media, params, record_dir=str(tmp_path))
    asyncio.run(run_test(transport, frames_to_send=[
        SleepFrame(0.1), VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame(),
        SleepFrame(0.1)]))
    (path,) = tmp_path.glob("*.wav")
    with wave.open(str(path)) as w:
        assert (w.getnchannels(), w.getframerate(), w.getnframes()) == (2, 16000, 960)
        first = np.frombuffer(w.readframes(1), dtype=np.int16).tolist()
    assert first == [16383, -8191]  # both channels, untouched


def test_nothing_is_recorded_unless_asked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    media = FakeMedia([np.zeros((320, 2), dtype=np.float32)])
    params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=16000)
    asyncio.run(run_test(ReachyInputTransport(media, params), frames_to_send=[
        SleepFrame(0.05), VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame()]))
    assert list(tmp_path.iterdir()) == []


def test_tts_pcm16_becomes_float32_for_the_speaker():
    pcm = np.array([16384, -32768], dtype=np.int16).tobytes()
    assert to_float32(pcm).tolist() == [0.5, -1.0]


class FakeClock:
    """Seconds that pass only when slept, at ``scale`` of real time (0: instantly)."""

    def __init__(self, scale=0.0):
        self.t, self.scale = 0.0, scale

    def __call__(self):
        return self.t

    async def sleep(self, seconds):
        self.t += seconds
        await asyncio.sleep(seconds * self.scale)


def chunks(seconds, chunk_s=0.04, rate=16000, kind=OutputAudioRawFrame):
    n = int(rate * chunk_s)
    tone = np.full(n, 1000, dtype=np.int16).tobytes()  # not silence: the transport adds that
    return [kind(audio=tone, sample_rate=rate, num_channels=1)
            for _ in range(round(seconds / chunk_s))]


def pushed_s(media, rate=16000):
    """Seconds of reply handed to the robot, not counting the silence sent at the end."""
    return sum(len(p) for p in media.pushed if np.abs(p).max() > 0) / rate


def test_output_is_paced_to_real_playback_keeping_only_a_small_lead():
    clock, media = FakeClock(), FakeMedia()
    out = ReachyOutputTransport(media, TransportParams(audio_out_enabled=True),
                                clock=clock, sleep=clock.sleep)

    async def go():
        for frame in chunks(1.0):
            await out.write_audio_frame(frame)
    asyncio.run(go())
    assert pushed_s(media) == pytest.approx(1.0)
    # The last write returns (and "bot stopped" can follow) no earlier than the
    # audio's duration less the lead the robot's buffer is allowed to hold.
    assert 1.0 - PLAYBACK_LEAD_S - 1e-9 <= clock.t <= 1.0


def output_pipeline(media, clock, *after):
    from pipecat.pipeline.pipeline import Pipeline

    return Pipeline([ReachyOutputTransport(media, TransportParams(
        audio_out_enabled=True, audio_out_sample_rate=16000), clock=clock, sleep=clock.sleep),
        *after])


def test_an_interruption_mid_reply_pushes_no_more_of_it():
    clock, media = FakeClock(scale=0.1), FakeMedia()
    asyncio.run(run_test(output_pipeline(media, clock), frames_to_send=[
        *chunks(3.0, kind=TTSAudioRawFrame), SleepFrame(0.05), InterruptionFrame(),
        SleepFrame(0.3)]))
    assert 0 < pushed_s(media) < 1.5  # of 3 s: what was due by then, plus the lead


def test_the_voice_is_busy_for_the_whole_reply_not_just_while_it_is_handed_over():
    media, playback, seen = FakeMedia(), Playback(), {}

    class Probe(FakeClock):
        async def sleep(self, seconds):
            await super().sleep(seconds)
            if self.t >= 1.0:  # 1 s into a 2 s reply, by the transport's own clock
                seen.setdefault("mid", playback.busy)

    clock = Probe(scale=0.1)
    # The EndFrame run_test sends last drains the audio queue: all of it has played by then.
    asyncio.run(run_test(output_pipeline(media, clock, BotSpeech(InterruptionTracker(), playback)),
                         frames_to_send=[*chunks(2.0, kind=TTSAudioRawFrame), TTSStoppedFrame()]))
    assert seen["mid"] is True
    assert playback.busy is False  # and free again once it has all played


def test_output_pushes_audio_to_the_robot_speaker():
    media = FakeMedia()
    out = ReachyOutputTransport(media, TransportParams(audio_out_enabled=True))
    frame = OutputAudioRawFrame(audio=np.zeros(320, dtype=np.int16).tobytes(),
                                sample_rate=16000, num_channels=1)
    assert asyncio.run(out.write_audio_frame(frame)) is True
    (pushed,) = media.pushed
    assert pushed.dtype == np.float32 and pushed.shape == (320,)


def test_input_reads_the_robot_mic_into_audio_frames():
    media = FakeMedia([np.zeros((320, 2), dtype=np.float32)] * 3)
    params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=16000)
    down, _ = asyncio.run(run_test(ReachyInputTransport(media, params),
                                   frames_to_send=[SleepFrame(0.1)]))
    audio = [f for f in down if isinstance(f, InputAudioRawFrame)]
    assert len(audio) == 3 and len(audio[0].audio) == 320 * 2
    assert media.calls[0] == "start_recording"


def test_the_mic_is_read_off_the_event_loop():
    # A read blocks up to 20 ms (the SDK's get_sample): a tick of the 50 Hz runner. On a real live
    # appsink beside a 50 Hz task, its lateness was 40 ms at p50 on the loop, 0.3 ms off it.
    threads = set()

    class Where(FakeMedia):
        def get_audio_sample(self):
            threads.add(threading.current_thread())
            return super().get_audio_sample()

    params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=16000)
    asyncio.run(run_test(ReachyInputTransport(Where(), params), frames_to_send=[SleepFrame(0.05)]))
    assert threads and threading.main_thread() not in threads


def test_a_mic_that_always_has_audio_does_not_starve_the_pipeline():
    class Endless(FakeMedia):
        def get_audio_sample(self):
            return np.zeros((320, 2), dtype=np.float32)

    params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=16000)
    asyncio.run(asyncio.wait_for(run_test(ReachyInputTransport(Endless(), params),
                                          frames_to_send=[SleepFrame(0.05)]), 5))


def test_a_mic_that_goes_quiet_reports_the_robot_link_lost(monkeypatch):
    # Live 2026-10-22: the robot's WebRTC stream died and the voice sat deaf for two hours.
    # The robot's mic delivers samples even in a silent room, so none at all is a dead link.
    monkeypatch.setattr(voice_transport, "MIC_SILENT_S", 0.05)
    lost = []
    params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=16000)
    asyncio.run(run_test(ReachyInputTransport(FakeMedia(), params, on_silent=lost.append),
                         frames_to_send=[SleepFrame(0.3)]))
    assert len(lost) == 1 and "mic" in lost[0]


def test_a_mic_that_keeps_delivering_is_never_reported_quiet(monkeypatch):
    class Endless(FakeMedia):
        def get_audio_sample(self):
            return np.zeros((320, 2), dtype=np.float32)

    monkeypatch.setattr(voice_transport, "MIC_SILENT_S", 0.05)
    lost = []
    params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=16000)
    asyncio.run(run_test(ReachyInputTransport(Endless(), params, on_silent=lost.append),
                         frames_to_send=[SleepFrame(0.3)]))
    assert lost == []


def test_a_quiet_mic_ends_the_run_as_robot_link_lost(monkeypatch):
    monkeypatch.setattr(voice_transport, "MIC_SILENT_S", 0.2)
    with pytest.raises(RobotLinkLostError, match="mic"):
        asyncio.run(asyncio.wait_for(voice_pipeline.run(FakeMedia(), no_emote, MOVES, ENV), 15))


# --- mic recovery: a USB drop left the daemon's audio dead (live 2026-10-23) ------------

def quiet_mic(monkeypatch, attempts=3):
    monkeypatch.setattr(voice_transport, "MIC_SILENT_S", 0.05)
    monkeypatch.setattr(voice_transport, "RECOVER_LISTEN_S", 0.1)
    monkeypatch.setattr(voice_transport, "RECOVER_ATTEMPTS", attempts)
    return TransportParams(audio_in_enabled=True, audio_in_sample_rate=16000)


def test_a_quiet_mic_is_recovered_and_the_voice_carries_on(monkeypatch):
    params, media, tried, lost = quiet_mic(monkeypatch, attempts=1), FakeMedia(), [], []

    async def recover(attempt):
        tried.append(attempt)
        media.frames = [np.zeros((320, 2), dtype=np.float32)] * 3

    down, _ = asyncio.run(run_test(
        ReachyInputTransport(media, params, on_silent=lost.append, recover=recover),
        frames_to_send=[SleepFrame(0.4)]))
    assert lost == []
    # Each time audio came back the attempts started over: one allowed, used again and again.
    assert tried[:2] == [1, 1]
    assert any(isinstance(f, InputAudioRawFrame) for f in down)


def test_recovery_gives_up_after_its_attempts_and_says_what_it_tried(monkeypatch):
    params, tried, lost = quiet_mic(monkeypatch, attempts=2), [], []

    async def recover(attempt):
        tried.append(attempt)

    asyncio.run(run_test(
        ReachyInputTransport(FakeMedia(), params, on_silent=lost.append, recover=recover),
        frames_to_send=[SleepFrame(0.6)]))
    assert tried == [1, 2]
    assert len(lost) == 1 and "mic" in lost[0] and "2 media resets" in lost[0]


def test_a_mic_that_raises_counts_as_quiet_rather_than_killing_the_reader(monkeypatch):
    class Broken(FakeMedia):
        def get_audio_sample(self):
            raise RuntimeError("The device has been disconnected.")

    params, lost = quiet_mic(monkeypatch), []
    asyncio.run(run_test(ReachyInputTransport(Broken(), params, on_silent=lost.append),
                         frames_to_send=[SleepFrame(0.3)]))
    assert len(lost) == 1 and "mic" in lost[0]


def test_the_run_tries_recovery_before_ending_as_robot_link_lost(monkeypatch):
    quiet_mic(monkeypatch, attempts=1)
    tried = []

    async def recover(attempt):
        tried.append(attempt)

    with pytest.raises(RobotLinkLostError, match="1 media reset"):
        asyncio.run(asyncio.wait_for(voice_pipeline.run(FakeMedia(), no_emote, MOVES, ENV,
                                                        recover=recover), 15))
    assert tried == [1]


def test_mic_recovery_can_be_switched_off(monkeypatch):
    quiet_mic(monkeypatch)
    tried = []

    async def recover(attempt):
        tried.append(attempt)

    with pytest.raises(RobotLinkLostError, match="mic"):
        asyncio.run(asyncio.wait_for(voice_pipeline.run(
            FakeMedia(), no_emote, MOVES, {**ENV, "SAGE_VOICE_MIC_RECOVERY": "0"},
            recover=recover), 15))
    assert tried == []


def test_a_lost_motion_link_ends_the_run_as_robot_link_lost(monkeypatch):
    # Live 2026-10-22: each emote raised this, was logged as a failed emote, and nothing ended.
    built, original = {}, voice_pipeline.build

    def build(media, play_emote, *args, **kwargs):
        built["play_emote"] = play_emote
        return original(media, play_emote, *args, **kwargs)

    async def lost_emote(name, sound):
        raise ConnectionError("Lost connection with the server.")

    async def go():
        voice = asyncio.ensure_future(voice_pipeline.run(FakeMedia(), lost_emote, MOVES, ENV))
        while "play_emote" not in built:
            await asyncio.sleep(0.01)
        with pytest.raises(ConnectionError):  # the emote still fails as it did
            await built["play_emote"]("cheerful1", True)
        await asyncio.wait_for(voice, 15)

    monkeypatch.setattr(voice_pipeline, "build", build)
    with pytest.raises(RobotLinkLostError, match="Lost connection"):
        asyncio.run(go())


def test_presence_goes_through_the_voices_own_kernel_session(monkeypatch):
    # One Session per body: one hello, one set of features (LAYERS §1a row 7).
    quiet_mic(monkeypatch, attempts=0)
    seen, original = {}, voice_pipeline.build

    def build(*args, **kwargs):
        pipeline = original(*args, **kwargs)
        sage = next(p for p in pipeline.processors if isinstance(p, SageProcessor))
        seen["voice"] = sage._session
        return pipeline

    async def watch(detected, send, muted):
        assert muted() is False  # no media session yet (sage_device.media)
        await send({"kind": "presence", "state": "seen", "confidence": 1.0})

    async def report(session, event):
        seen["presence"] = session

    monkeypatch.setattr(voice_pipeline, "build", build)
    monkeypatch.setattr(voice_pipeline, "watch_presence", watch)
    monkeypatch.setattr(voice_pipeline, "report_presence", report)
    with pytest.raises(RobotLinkLostError):
        asyncio.run(asyncio.wait_for(voice_pipeline.run(FakeMedia(), no_emote, MOVES, ENV,
                                                        presence=lambda: True), 15))
    assert seen["presence"] is seen["voice"]


def test_a_dead_motion_link_seen_by_the_executor_ends_the_run():
    # Live 2026-10-23: idle motion's commands were refused ("Lost connection with the server.").
    class Executor:
        audio = None

        async def breathe(self):
            await asyncio.sleep(0.05)
            self.link_lost("the robot's motion link: no command accepted for 10 s")

    with pytest.raises(RobotLinkLostError, match="motion link"):
        asyncio.run(asyncio.wait_for(voice_pipeline.run(FakeMedia(), no_emote, MOVES, ENV,
                                                        executor=Executor()), 15))


# --- audio boundaries, for body language ------------------------------------------


def test_a_boundary_wait_returns_at_once_when_nothing_is_playing():
    asyncio.run(asyncio.wait_for(Playback().boundary(), 1))


def test_a_played_sentence_is_the_boundary_an_emote_waits_for():
    playback, seen = Playback(), []
    playback.speaking = True  # the emote arrives mid-sentence

    async def emote_after_boundary():
        await playback.boundary()
        seen.append(list(playback.spoken))

    async def go():
        waiter = asyncio.ensure_future(emote_after_boundary())
        await run_test(BotSpeech(InterruptionTracker(), playback), frames_to_send=[
            BotStartedSpeakingFrame(), SleepFrame(0.05),
            TTSTextFrame("Hello there,", aggregated_by=AggregationType.SENTENCE),
            SleepFrame(0.05)])
        await asyncio.wait_for(waiter, 1)
    asyncio.run(go())
    assert seen == [["Hello there,"]]


def test_turn_end_silence_comes_from_calibration():
    def stop_secs(env):
        vad = next(p for p in build(FakeMedia(), no_emote, MOVES, env).processors
                   if isinstance(p, VADProcessor))
        return vad._vad_controller._vad_analyzer.params.stop_secs

    assert stop_secs(ENV) == 0.6  # measured on the owner's live turns
    assert stop_secs({**ENV, "SAGE_VOICE_TURN_END_MS": "620"}) == 0.62


def test_barge_in_needs_speech_that_lasts_and_the_hold_comes_from_the_environment():
    def parts(env):
        processors = build(FakeMedia(), no_emote, MOVES, env).processors
        vad = next(p for p in processors if isinstance(p, VADProcessor))
        sage = next(p for p in processors if isinstance(p, SageProcessor))
        return vad._vad_controller._vad_analyzer.min_onset_ms, sage._hold_s

    assert parts(ENV) == (200, 0.0)  # the hold is off until a live A/B says otherwise
    assert parts({**ENV, "SAGE_VOICE_BARGE_IN_MS": "0", "SAGE_VOICE_HOLD_MS": "2000"}) == (0, 2.0)


def test_the_live_body_chooses_a_move_for_some_statements():
    processors = build(FakeMedia(), no_emote, MOVES, ENV).processors
    assert next(p for p in processors if isinstance(p, SageProcessor))._rng is random


def pcm_wav(values) -> bytes:
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.array(values, np.int16).tobytes())
    return buffer.getvalue()


def test_segments_waiting_for_the_stt_go_with_the_one_it_takes_as_one_request():
    # Pipecat queues every VAD segment for one serial STT task: under load the queue grew
    # without bound (stt_ms up to 52 s). Whatever waits goes along, merged, in one request.
    # Pipecat 1.15 queues (audio, segment number); the merged transcript takes the newest
    # number, so SegmentedSTTService.push_frame finalizes it.
    class Serial:
        def __init__(self):
            self._segment_queue = asyncio.Queue()
            self._transcribing_segment = 1
            self.sent = []

        async def run_stt(self, audio):
            self.sent.append(audio)
            yield "transcript"

    class STT(voice_pipeline.MergeBacklog, Serial):
        pass

    stt = STT()

    async def go():
        for n in (2, 3):
            await stt._segment_queue.put((pcm_wav([n] * 4), n))
        await stt._segment_queue.put(None)  # the stop: it must still end the task
        return [f async for f in stt.run_stt(pcm_wav([1] * 4))]
    assert asyncio.run(go()) == ["transcript"]
    (sent,) = stt.sent
    with wave.open(io_bytes(sent)) as w:
        assert np.frombuffer(w.readframes(w.getnframes()), np.int16).tolist() == [1] * 4 + [2] * 4 + [3] * 4
    assert stt._transcribing_segment == 3
    assert stt._segment_queue.get_nowait() is None and stt._segment_queue.empty()


def test_the_stts_confidence_and_voiceprint_score_are_logged_for_the_noise_trace(caplog):
    # eval/noise.py reads this line; an older STT without the fields logs nothing extra.
    from openai.types.audio import Transcription

    class Serial:
        _segment_queue = asyncio.Queue()

        def __init__(self, result):
            self.result = result

        async def run_stt(self, audio):
            yield TranscriptionFrame("Hi there", "u", "t", result=self.result)

    class STT(voice_pipeline.MergeBacklog, Serial):
        pass

    async def go(result):
        return [f async for f in STT(result).run_stt(pcm_wav([1] * 4))]
    caplog.set_level("INFO", logger="reachy_sage_voice.pipeline")
    asyncio.run(go(Transcription.construct(text="Hi there", similarity=0.712, confidence={
        "mean": 0.95, "min": 0.8, "words": [["Hi", 0.97], ["there", 0.8]]})))
    asyncio.run(go(Transcription.construct(text="Hi there")))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("stt:")]
    assert lines == ["stt: confidence mean 0.95 min 0.80, similarity 0.71, words 0.97 0.80"]


def io_bytes(data):
    import io

    return io.BytesIO(data)


def test_both_stts_merge_their_backlog(monkeypatch):
    def stt(pipeline):
        return next(p for p in pipeline.processors if isinstance(p, OpenAISTTService))
    assert isinstance(stt(build(FakeMedia(), no_emote, MOVES, ENV)), voice_pipeline.MergeBacklog)
    pipeline, _ = smart(monkeypatch)
    assert isinstance(stt(pipeline), voice_pipeline.MergeBacklog)


class FakeTurnAnalyzer(BaseTurnAnalyzer):
    """Smart Turn's interface, with verdicts from a script instead of the model."""

    def __init__(self, verdicts=(), stop_secs=1.5):
        super().__init__()
        self.verdicts = list(verdicts)
        self._params = SmartTurnParams(stop_secs=stop_secs)

    @property
    def speech_triggered(self):
        return False

    @property
    def params(self):
        return self._params

    def append_audio(self, buffer, is_speech):
        return EndOfTurnState.INCOMPLETE

    async def analyze_end_of_turn(self):
        return self.verdicts.pop(0), None

    def clear(self):
        pass


def smart(monkeypatch, env=()):
    made = []

    def analyzer(stop_secs):
        made.append(FakeTurnAnalyzer(stop_secs=stop_secs))
        return made[-1]
    monkeypatch.setattr(voice_pipeline, "smart_turn_analyzer", analyzer)
    pipeline = build(FakeMedia(), no_emote, MOVES, {**ENV, "SAGE_VOICE_TURN": "smart", **dict(env)})
    return pipeline, made


def test_the_stt_pads_a_segment_only_up_to_half_a_second_of_silence(monkeypatch):
    # The segment already ends in the VAD's stop silence; more is compute (~10 ms in Colima).
    stt = build(FakeMedia(), no_emote, MOVES, ENV).processors[3]
    assert stt._trailing_silence_secs == 0  # 0.6 s of stop silence is in the buffer
    pipeline, _ = smart(monkeypatch)
    turn_stt = next(p for p in pipeline.processors if isinstance(p, voice_pipeline.TurnSTTService))
    assert turn_stt._trailing_silence_secs == pytest.approx(0.3)  # 0.2 s stop + 0.3 s pad


def test_silence_ends_the_turn_unless_smart_turn_is_asked_for(monkeypatch):
    kinds = [type(p) for p in build(FakeMedia(), no_emote, MOVES, ENV).processors]
    assert UserTurnProcessor not in kinds
    pipeline, (analyzer,) = smart(monkeypatch)
    kinds = [type(p) for p in pipeline.processors[1:-1]]
    assert kinds == [ReachyInputTransport, VADProcessor, UserTurnProcessor,
                     voice_pipeline.TurnSTTService, SageProcessor, KokoroTTSService,
                     ReachyOutputTransport, BotSpeech]
    vad = pipeline.processors[2]
    assert vad._vad_controller._vad_analyzer.params.stop_secs == 0.2  # Smart Turn's wiring
    assert analyzer.params.stop_secs == 1.5
    assert vad._vad_controller._vad_analyzer._on_detected is not None  # barge-in unchanged
    _, (analyzer,) = smart(monkeypatch, {"SAGE_VOICE_TURN_TIMEOUT_MS": "2000"})
    assert analyzer.params.stop_secs == 2.0
    with pytest.raises(ConfigError, match="SAGE_VOICE_TURN"):
        build(FakeMedia(), no_emote, MOVES, {**ENV, "SAGE_VOICE_TURN": "psychic"})


def test_smart_turn_transcribes_the_whole_turn_once_and_never_interrupts():
    # Each 0.2 s VAD segment alone loses context ("Anthropolog"): the turn goes whole.
    analyzer = FakeTurnAnalyzer([EndOfTurnState.INCOMPLETE, EndOfTurnState.COMPLETE])
    segments = []

    class STT(voice_pipeline.TurnSTTService):
        async def run_stt(self, audio):
            segments.append(len(audio))
            yield TranscriptionFrame(text="What's the pin? Really important.", user_id="you",
                                     timestamp="")

    def chunk():
        return InputAudioRawFrame(audio=bytes(640), sample_rate=16000, num_channels=1)
    stt = STT(api_key="local", base_url="http://127.0.0.1:1/v1")
    down, up = asyncio.run(run_test(Pipeline([voice_pipeline.user_turns(analyzer), stt]),
                                    frames_to_send=[
        chunk(), VADUserStartedSpeakingFrame(), chunk(), chunk(),
        VADUserStoppedSpeakingFrame(stop_secs=0.2),
        *[chunk() for _ in range(60)],  # a 1.2 s pause: Smart Turn waits, the audio stays
        VADUserStartedSpeakingFrame(), chunk(), VADUserStoppedSpeakingFrame(stop_secs=0.2),
        SleepFrame(0.1), chunk(), SleepFrame(0.05)]))
    assert segments == [44 + 64 * 640 + 16000]  # WAV header, the turn with its pre-roll, padding
    order = [f.text if isinstance(f, TranscriptionFrame) else "end" for f in down
             if isinstance(f, (TranscriptionFrame, UserStoppedSpeakingFrame))]
    assert order == ["end", "What's the pin? Really important."]
    assert not any(isinstance(f, InterruptionFrame) for f in down + up)  # barge-in is the onset's


def test_the_voice_is_busy_from_queued_text_until_the_speaker_stops():
    playback = Playback()
    playback.queued = True
    assert playback.busy
    asyncio.run(run_test(BotSpeech(InterruptionTracker(), playback), frames_to_send=[
        BotStartedSpeakingFrame(), BotStoppedSpeakingFrame(), SleepFrame(0.05)]))
    assert not playback.busy
