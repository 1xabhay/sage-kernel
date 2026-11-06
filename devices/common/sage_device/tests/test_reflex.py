"""What a body does at once, before any words: decided here with no body in sight."""

import random

import pytest

from sage_device import reflex


def named(word):
    return word in ("sage", "reachy")


@pytest.mark.parametrize("said", ["Hi Sage.", "hello", "Hey, Sage!", "Good morning, Reachy.",
                                  "Sage, hi there", "here's age"])
def test_a_greeting_alone_is_a_greeting(said):
    assert reflex.reaction_to(said, named) == reflex.GREET


@pytest.mark.parametrize("said", ["Good girl!", "good boy, Sage", "Thank you.", "Thanks, Sage",
                                  "Good job.", "Sage, well done", "thank you so much"])
def test_praise_alone_is_praise(said):
    assert reflex.reaction_to(said, named) == reflex.PRAISED


@pytest.mark.parametrize("said", ["Hi, what's the weather like?", "I said hi to her.",
                                  "Thank you for the music, what's next?", "good", "Good.",
                                  "Sage", "", "good job on that", "hi hi hi hi hi",
                                  "I want to die, thank you"])
def test_a_longer_or_other_line_is_a_turn(said):
    assert reflex.reaction_to(said, named) is None


def test_a_gesture_passes_to_a_turn_while_the_body_is_busy_or_speaking():
    phase = reflex.gesture_phase
    assert phase(100.0, -1e9, speaking=False, busy=None) == reflex.FIRE
    assert phase(100.0, -1e9, speaking=True, busy=None) == reflex.PASS
    assert phase(100.0, -1e9, speaking=False, busy="dancing") == reflex.PASS


def test_a_repeat_within_the_cooldown_moves_nothing_and_goes_nowhere():
    just = 100.0 - reflex.COOLDOWN_S[reflex.GREET] + 1
    assert reflex.gesture_phase(100.0, just, speaking=False, busy="an emote") == reflex.SWALLOW
    assert reflex.gesture_phase(100.0, just, speaking=True, busy=None) == reflex.PASS


def test_a_reflex_fires_only_when_free_and_cooled():
    for reaction in (reflex.LISTENING, reflex.PAUSE):
        wait = reflex.COOLDOWN_S[reaction]
        assert reflex.fires(reaction, 100.0, 100.0 - wait, speaking=False, busy=None)
        assert not reflex.fires(reaction, 100.0, 100.0 - wait + 0.01, speaking=False, busy=None)
        assert not reflex.fires(reaction, 100.0, -1e9, speaking=True, busy=None)
        assert not reflex.fires(reaction, 100.0, -1e9, speaking=False, busy="searching")


def test_never_the_same_clip_twice_in_a_row_and_every_clip_gets_a_turn():
    rng, options, last, seen = random.Random(1), ("a", "b", "c"), None, set()
    for _ in range(100):
        pick = reflex.pick(options, last, rng)
        assert pick != last
        seen.add(pick)
        last = pick
    assert seen == set(options)
    assert reflex.pick(("only",), "only", rng) == "only"  # one clip: it still answers
