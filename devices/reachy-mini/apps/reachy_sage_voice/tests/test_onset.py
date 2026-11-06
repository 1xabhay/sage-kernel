"""Barge-in once speech has lasted ``min_onset_ms``; with 0, on the VAD's first voiced frame.

The analyzer here is Pipecat's own VADAnalyzer state machine with a scripted
confidence, so the edge under test is the real one, not a stand-in.
"""

import asyncio

from pipecat.audio.vad.vad_analyzer import VADAnalyzer, VADParams, VADState
from pipecat.frames.frames import InterruptionWorkerFrame
from pipecat.processors.frame_processor import FrameDirection

from reachy_sage_voice.interruption import InterruptionTracker
from reachy_sage_voice.onset import (
    SpeechOnsetMixin,
    build_speech_onset_silero,
    make_speech_onset_handler,
)

FRAME = 320  # samples per Reachy mic frame at 16 kHz
LOUD = (b"\xff\x3f" * FRAME)  # int16 ~0.5 full scale, over min_volume


class ScriptedVAD(VADAnalyzer):
    def __init__(self, confidences):
        super().__init__(sample_rate=16000, params=VADParams(start_secs=0.2, min_volume=0.0))
        self.set_sample_rate(16000)
        self._confidences = list(confidences)

    def num_frames_required(self) -> int:
        return FRAME

    def voice_confidence(self, buffer: bytes) -> float:
        return self._confidences.pop(0) if self._confidences else 0.0


class OnsetVAD(SpeechOnsetMixin, ScriptedVAD):
    min_onset_ms = 0  # the first voiced frame, as STARTING; each test sets it where it matters


def run_frames(vad, n):
    async def go():
        return [await vad.analyze_audio(LOUD) for _ in range(n)]
    return asyncio.run(go())


def test_barge_in_waits_until_speech_has_lasted_min_onset_ms():
    # Live 2026-10-23: 20 of 43 barge-ins had no speech the VAD confirmed (a breath, a knock,
    # the robot's own voice), 7 more no words. Speech must last before the robot stops.
    vad = OnsetVAD([1.0] * 20)
    vad.min_onset_ms = 200
    fired_at: list[int] = []
    frames: list[int] = []
    vad.set_onset_handlers(on_detected=lambda: fired_at.append(len(frames)))

    async def go():
        for _ in range(20):
            await vad.analyze_audio(LOUD)
            frames.append(1)
    asyncio.run(go())
    assert fired_at == [9]  # the tenth 20 ms frame: 200 ms of speech


def test_a_blip_shorter_than_min_onset_ms_never_barges_in():
    vad = OnsetVAD([1.0] * 5 + [0.0] * 5 + [1.0] * 5)
    vad.min_onset_ms = 200
    seen: list[str] = []
    vad.set_onset_handlers(on_detected=lambda: seen.append("on"),
                           on_cleared=lambda: seen.append("off"))
    run_frames(vad, 15)
    assert seen == []


def test_the_default_minimum_is_the_vads_own_start():
    assert SpeechOnsetMixin.min_onset_ms == 200


def test_with_no_minimum_onset_fires_on_starting_well_before_speaking():
    vad = OnsetVAD([1.0] * 20)
    fired_at: list[int] = []
    states: list[VADState] = []
    vad.set_onset_handlers(on_detected=lambda: fired_at.append(len(states)))

    async def go():
        for _ in range(20):
            states.append(await vad.analyze_audio(LOUD))
    asyncio.run(go())

    first_speaking = states.index(VADState.SPEAKING)
    assert fired_at == [0]  # once, on the very first voiced frame
    assert first_speaking * FRAME / 16 >= 160  # SPEAKING is >= 160 ms later


def test_observing_does_not_change_the_states_the_stt_reads():
    script = [1.0] * 15 + [0.0] * 20
    plain, observed = ScriptedVAD(script), OnsetVAD(script)
    observed.set_onset_handlers(on_detected=lambda: None, on_cleared=lambda: None)
    assert run_frames(plain, 35) == run_frames(observed, 35)


def test_a_false_start_is_retracted():
    vad = OnsetVAD([1.0, 1.0, 0.0])
    seen: list[str] = []
    vad.set_onset_handlers(on_detected=lambda: seen.append("on"),
                           on_cleared=lambda: seen.append("off"))
    run_frames(vad, 3)
    assert seen == ["on", "off"]


def test_a_failing_handler_costs_an_interruption_not_the_audio_path():
    vad = OnsetVAD([1.0])

    def boom():
        raise RuntimeError("bad handler")
    vad.set_onset_handlers(on_detected=boom)
    assert run_frames(vad, 1) == [VADState.STARTING]


def test_onset_while_the_bot_talks_pushes_one_interruption_upstream():
    tracker = InterruptionTracker()
    tracker.bot_started(10.0)
    pushed = []

    async def enqueue(frame, direction):
        pushed.append((type(frame), direction))

    clock = iter([11.0, 11.5])
    handler = make_speech_onset_handler(tracker, enqueue, now=lambda: next(clock))
    asyncio.run(handler("you"))
    asyncio.run(handler("you"))
    assert pushed == [(InterruptionWorkerFrame, FrameDirection.UPSTREAM)]


def test_onset_with_the_bot_silent_interrupts_nothing():
    pushed = []

    async def enqueue(frame, direction):
        pushed.append(frame)

    handler = make_speech_onset_handler(InterruptionTracker(), enqueue, now=lambda: 1.0)
    asyncio.run(handler("you"))
    assert pushed == []


def test_a_broken_enqueue_never_raises_into_the_audio_path():
    tracker = InterruptionTracker()
    tracker.bot_started(0.0)

    async def enqueue(frame, direction):
        raise RuntimeError("pipeline gone")

    asyncio.run(make_speech_onset_handler(tracker, enqueue, now=lambda: 1.0)("you"))


def test_real_silero_runs_on_the_overridden_onnxruntime():
    """The onnxruntime override (1.27, Pipecat asks ~=1.24) must not break Silero."""
    import onnxruntime

    vad = build_speech_onset_silero(VADParams())
    vad.set_sample_rate(16000)
    states = run_frames(vad, 50)  # one second of 20 ms frames
    assert onnxruntime.__version__ == "1.27.0"
    assert set(states) <= set(VADState)


def reflexes(confidences):
    """Run the frames; return what fired, as (frame index, what)."""
    vad, frames, fired = OnsetVAD(confidences), [], []
    vad.set_params(VADParams(start_secs=0.2, stop_secs=0.6, min_volume=0.0))

    def note(what):
        return lambda: fired.append((len(frames), what))
    vad.set_reflex_handlers(on_voice=note("voice"), on_pause=note("pause"), on_quiet=note("quiet"))

    async def go():
        for _ in confidences:
            await vad.analyze_audio(LOUD)
            frames.append(1)
    asyncio.run(go())
    return fired


def test_the_first_voiced_frame_is_the_voice_and_it_fires_once():
    assert reflexes([1.0] * 20) == [(0, "voice")]


def test_two_hundred_ms_of_silence_is_a_pause_long_before_the_turn_ends():
    fired = reflexes([1.0] * 20 + [0.0] * 60)  # the default stop_secs, 0.6: 30 frames
    assert fired[1] == (20 + 9, "pause")  # the tenth 20 ms frame of silence: 200 ms
    assert fired[2][1] == "quiet" and fired[2][0] > 29


def test_a_pause_that_goes_on_speaking_can_pause_again():
    fired = reflexes([1.0] * 20 + [0.0] * 15 + [1.0] * 5 + [0.0] * 15)
    assert [what for _, what in fired] == ["voice", "pause", "pause"]


def test_a_false_start_never_nods_and_goes_quiet():
    fired = reflexes([1.0] * 3 + [0.0] * 5)
    assert [what for _, what in fired] == ["voice", "quiet"]


def test_a_vad_stopping_at_the_pause_itself_still_pauses():
    vad, fired = OnsetVAD([1.0] * 20 + [0.0] * 20), []
    vad.set_params(VADParams(start_secs=0.2, stop_secs=0.2, min_volume=0.0))
    vad.set_reflex_handlers(on_pause=lambda: fired.append("pause"))
    run_frames(vad, 40)
    assert fired == ["pause"]  # Smart Turn's 0.2 s VAD stop


def test_a_failing_reflex_handler_costs_the_reflex_not_the_audio_path():
    vad = OnsetVAD([1.0] * 3)

    def boom():
        raise RuntimeError("motor")
    vad.set_reflex_handlers(on_voice=boom)
    assert len(run_frames(vad, 3)) == 3
