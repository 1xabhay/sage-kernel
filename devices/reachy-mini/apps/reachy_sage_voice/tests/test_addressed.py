"""Reply only when addressed: a face in view, the robot's name, or an exchange in progress."""

import asyncio

from pipecat.frames.frames import BotSpeakingFrame, BotStoppedSpeakingFrame, TranscriptionFrame
from pipecat.processors.filters.function_filter import FunctionFilter

from reachy_sage_voice.addressed import Addressed, addressed_filter


def heard(text):
    return TranscriptionFrame(text, "you", "now")


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def passes(gate, text):
    return asyncio.run(gate(heard(text)))


def test_lyrics_with_no_face_no_name_and_no_exchange_are_not_answered():
    # Live: "Sister, sister, that's how I missed you" got "Sister, I missed you too!".
    assert not passes(Addressed(face=lambda: False), "Sister, sister, that's how I missed you")


def test_saying_the_name_or_an_alias_is_addressing_it():
    def gate():  # a fresh one each time: a turn that passes opens the conversation
        return Addressed(aliases=("Reachy",), face=lambda: False)
    assert passes(gate(), "Sage, what time is it?")
    assert passes(gate(), "hey SAGE")
    assert passes(gate(), "Reachy, look here")
    assert not passes(gate(), "a sagebrush in the desert")


def test_a_tracked_face_is_someone_talking_to_it():
    assert passes(Addressed(face=lambda: True), "what's the weather")


def test_no_face_reader_means_no_face():
    assert not passes(Addressed(), "what's the weather")


def test_the_exchange_stays_open_for_a_while_after_the_pet_spoke():
    clock = Clock()
    gate = Addressed(face=lambda: False, exchange_s=8.0, now=clock)
    asyncio.run(gate(BotSpeakingFrame()))
    asyncio.run(gate(BotStoppedSpeakingFrame()))
    clock.t += 7.9
    assert passes(gate, "and tomorrow?")
    clock.t += 8.1  # the transcript above refreshed nothing: only the pet's own speech does
    assert not passes(gate, "and the day after?")


def test_other_frames_always_pass():
    assert asyncio.run(Addressed(face=lambda: False)(BotSpeakingFrame()))


def test_the_filter_is_configured_from_the_environment():
    assert addressed_filter({}, None) is None  # off by default
    gate = addressed_filter({"SAGE_VOICE_ADDRESSED": "1", "SAGE_VOICE_ALIASES": "Reachy, Mini",
                             "SAGE_VOICE_EXCHANGE_S": "5"}, lambda: False)
    assert isinstance(gate, FunctionFilter)
    addressed = gate._filter
    assert addressed.exchange_s == 5.0
    assert passes(addressed, "mini, hello") and passes(addressed, "sage hello")
    assert addressed_filter({"SAGE_VOICE_ADDRESSED": "1"}, None)._filter.exchange_s == 8.0


def started(gate):
    from pipecat.frames.frames import VADUserStartedSpeakingFrame
    return asyncio.run(gate(VADUserStartedSpeakingFrame()))


def test_a_long_reply_begun_inside_the_exchange_is_still_in_it():
    """Live: the owner began "What should I do? I have a football match..." 4 s after the pet spoke;
    it took 5 s to say, so its transcript came at 9 s and was dropped."""
    clock = Clock()
    gate = Addressed(face=lambda: False, exchange_s=8.0, now=clock)
    asyncio.run(gate(BotStoppedSpeakingFrame()))
    clock.t += 4
    started(gate)
    clock.t += 5
    assert passes(gate, "What should I do? I have a football match in half an hour.")


def test_each_turn_that_passes_keeps_the_conversation_open():
    """Live: "You were gonna say something?" came 13 s after the pet's last words, mid-conversation."""
    clock = Clock()
    gate = Addressed(face=lambda: False, exchange_s=8.0, now=clock)
    assert passes(gate, "Sage, guess what.")
    clock.t += 7
    started(gate)
    clock.t += 2
    assert passes(gate, "I won the match.")
    clock.t += 7
    started(gate)
    clock.t += 3
    assert passes(gate, "You were gonna say something?")
    clock.t += 30
    started(gate)
    assert not passes(gate, "lyrics on the radio")


def test_inside_a_pipeline_the_filter_sees_the_pet_speak_and_the_owner_start():
    """FunctionFilter passes system frames, and frames moving upstream, without asking the predicate.
    The pet's speaking frames reach it upstream (from the output transport, after it), and the VAD's
    start is a system frame, so the exchange window never opened live."""
    from pipecat.frames.frames import VADUserStartedSpeakingFrame
    from pipecat.processors.frame_processor import FrameDirection

    gate = addressed_filter({"SAGE_VOICE_ADDRESSED": "1"}, lambda: False)
    assert not gate._should_passthrough_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
    assert not gate._should_passthrough_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
