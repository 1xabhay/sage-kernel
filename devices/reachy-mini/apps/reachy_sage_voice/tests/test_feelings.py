"""The model names a feeling; the body picks the clip, never the same one twice running."""

import random

from pipecat.tests.utils import SleepFrame
from test_sage_processor import FakeSession, emote, heard, make, reply, run

from reachy_sage_voice import expressions
from reachy_sage_voice.manifest import EMOTIONS


def test_a_feeling_with_several_clips_never_gets_the_last_one_again():
    rng, last = random.Random(7), None
    for _ in range(50):
        clip = expressions.emotion("curious", rng, last=last)
        assert clip in expressions.INTENTS["curious"] and clip != last
        last = clip


def test_a_feeling_with_one_clip_plays_it_again_and_an_unknown_one_plays_nothing():
    (only,) = expressions.INTENTS["calming"]
    assert expressions.emotion("calming", last=only) == only
    assert expressions.emotion("furious") is None and expressions.emotion(None) is None


def test_emote_effects_naming_a_feeling_play_its_clips_never_the_same_twice_running():
    emotes = []
    feelings = [emote(sequence, "curious") for sequence in (41, 42, 43)]
    session = FakeSession([{"effects": [*feelings, reply(44, "")]}])
    run(make(session, emotes, move_s=0.01), [heard("hi"), SleepFrame(0.3)])
    assert len(emotes) == 3 and set(emotes) <= set(expressions.INTENTS["curious"])
    assert emotes[0] != emotes[1] != emotes[2]
    assert [r["status"] for r in session.results if r["sequence"] in (41, 42, 43)] == ["done"] * 3


def test_a_feeling_the_body_has_no_clip_for_reports_failed():
    session = FakeSession([{"effects": [emote(41, "furious"), reply(42, "")]}])
    run(make(session), [heard("hi"), SleepFrame(0.1)])
    assert {"sequence": 41, "status": "failed"} in session.results
    assert "furious" not in EMOTIONS
