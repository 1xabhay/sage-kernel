"""The floor: lines written to the shapes the STT produces, none of them recorded speech."""

import pytest

from sage_device.floor import (
    DISTRESS_HOLD_S,
    DROP,
    GAP_S,
    GRACE_S,
    MAX_HOLD_S,
    MEDIA_WAIT_S,
    MUST_GAP_S,
    MUST_WAIT_S,
    OPEN_S,
    SPEAK,
    WAIT,
    half_heard,
    hold_limit,
    media_floor,
    overheard,
    taken,
)
from sage_device.hearing import PASS, UNSURE, decide


def test_a_voice_going_now_has_the_floor():
    assert taken(now=5.0, began=1.0, voice_on=4.0, voice_off=3.0)
    assert taken(now=5.0, began=4.5, voice_on=4.0, voice_off=3.0)  # it began before the turn did


def test_a_voice_that_stopped_after_the_turn_began_keeps_it_while_its_words_arrive():
    assert taken(now=5.0, began=1.0, voice_on=2.0, voice_off=5.0 - GRACE_S / 2)
    assert not taken(now=5.0, began=1.0, voice_on=2.0, voice_off=5.0 - GRACE_S * 2)


def test_the_speech_this_turn_answers_never_holds_the_reply():
    assert not taken(now=5.0, began=4.9, voice_on=3.0, voice_off=4.8)


def test_a_reply_waits_out_a_long_voice_but_a_tv_cannot_mute_it_for_good():
    # Replay, all logs: 9.1% of voice runs outlast 6 s, 1.6% outlast 15 s
    # (the ones that look like media).
    assert hold_limit("We had soup for dinner.") == MAX_HOLD_S == 15.0


def test_distress_waits_no_longer_than_it_did():
    assert hold_limit("I fell off the") == DISTRESS_HOLD_S == 6.0


def test_nobody_has_spoken():
    assert not taken(now=5.0, began=1.0, voice_on=float("-inf"), voice_off=float("-inf"))


@pytest.mark.parametrize("line", [
    "Somewhat like getting those back for",  # no closing mark
    "that will make the whole thing work because everything",
    "We went over to the place and uh.",  # closes on a word no sentence ends on
])
def test_a_statement_cut_off_is_half_heard(line):
    assert half_heard(line)
    assert decide(line, half=half_heard(line)) == UNSURE


@pytest.mark.parametrize(("line", "why"), [
    ("Is there meant to be a", "question"),
    ("So what did you", "question, after a word that leads one"),
    ("I fell off the", "distress"),
    ("Sage I went to the", "name"),
    ("Remember the thing about the", "teaching"),
    ("Please put that on the", "request"),
])
def test_a_line_that_needs_words_is_never_half_heard(line, why):
    named = lambda word: word == "sage"  # noqa: E731
    assert not half_heard(line, named), why


@pytest.mark.parametrize("line", [
    "Wow,", "Yeah, yeah", "Okay",  # whole replies with no closing mark
    "We had soup.", "That sounds good.",  # finished
    "Yes, I went to the shop and then I came back to the house",  # long: cut off, still real
])
def test_whole_replies_finished_lines_and_long_ones_are_not_half_heard(line):
    assert not half_heard(line)


def test_the_answer_to_its_own_question_passes_however_cut_off():
    assert decide("Somewhat like the", asked=True, half=True) == PASS


def test_talk_that_opens_mid_thought_or_about_others_after_a_quiet_is_overheard():
    for line in ("And then the whole team came along.", "But it never ran.",
                 "They shipped the second model last spring.", "His brother runs that place now."):
        assert overheard(line, OPEN_S), line
        assert overheard(line, float("inf")), line


def test_in_a_running_exchange_nothing_is_overheard():
    assert not overheard("And then the whole team came along.", OPEN_S - 0.1)


@pytest.mark.parametrize("line", [
    "I'm scared and they left.",  # distress
    "And what did they say?",  # a question
    "So Sage, they came back.",  # its name
    "And please tell them.",  # asking for something
    "So remember they called.",  # something to keep
    "And you were right about them.",  # to it, in so many words
    "They left me there.",  # the person's own life
    "So my sister came over.",
    "And I'm so tired.",  # a bad day: not worth the risk of a blank look
    "I miss her.",
    "Yeah.",  # a whole reply
    "So here is the thing.",  # people open with it
    "I went to the shop today.",  # nothing says it wasn't for the body: answer
    "",
])
def test_a_line_that_might_be_for_it_is_never_overheard(line):
    assert not overheard(line, float("inf"), named=lambda word: word == "sage")


def test_whatever_comes_back_after_it_asked_is_for_it():
    assert not overheard("And they left.", float("inf"), asked=True)


# --- watching: the TV never yields, so a reply waits for a gap in it (watch/academic.md rule 2) ---


def test_watching_a_reply_waits_for_a_gap_in_the_tv_not_for_silence():
    assert media_floor(waited=3.0, gap=0.0, must=False) == WAIT  # the commentary is going
    assert media_floor(waited=3.0, gap=GAP_S - 0.1, must=False) == WAIT  # a breath, not a gap
    assert media_floor(waited=3.0, gap=GAP_S, must=False) == SPEAK


def test_watching_with_no_gap_a_reply_that_can_go_unsaid_is_dropped_never_spoken_over_the_tv():
    assert media_floor(waited=MEDIA_WAIT_S - 0.1, gap=0.0, must=False) == WAIT
    assert media_floor(waited=MEDIA_WAIT_S, gap=0.0, must=False) == DROP


def test_watching_a_line_that_needs_words_takes_a_short_gap_and_never_waits_past_its_cap():
    assert media_floor(waited=1.0, gap=MUST_GAP_S, must=True) == SPEAK
    assert media_floor(waited=MUST_WAIT_S - 0.1, gap=0.0, must=True) == WAIT
    assert media_floor(waited=MUST_WAIT_S, gap=0.0, must=True) == SPEAK  # over the TV, not dropped
    assert MUST_WAIT_S <= DISTRESS_HOLD_S  # distress waits no longer than it does today
