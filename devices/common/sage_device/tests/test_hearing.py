"""Tilt or talk: lines written to the shapes the STT produces, none of them recorded speech."""

import math

import pytest

from sage_device.hearing import COOLDOWN_S, PASS, QUIET, UNSURE, decide, trustworthy


@pytest.mark.parametrize("line", [
    "So the", "And then", "Uh", "Okay, so", "It's", "Yeah, I think",  # cut off, no closing mark
    "Well, he's the.",  # closes on a word no sentence ends on
    "Teapot.", "Zanzibar.",  # one word that answers nothing
    "La la la la", "Tip tip tip tip.", "Hmm, hmm, hmm, hmm.",  # a stuck decoder
])
def test_noise_shapes_are_not_trustworthy(line):
    assert not trustworthy(line)


@pytest.mark.parametrize("line", [
    "Hello", "Yes.", "Thank you", "Okay,", "Yeah, yeah", "Good morning", "Maybe.",  # common replies
    "I think it might rain later today.", "We had soup.", "That sounds good.",  # sentences
    "Juggle!",  # said with force
    "Let's go.", "I know.", "Me too.", "Do it.", "Remind me to call",  # short and whole, or nearly
    "Yes, I went to the shop and then I came back to the house",  # long: cut off, still real
    "So, like, I like it, I like the way it looks, like I like the colour, like really.",  # "like"s
    "No, no, no.", "Don't walk away. Don't walk away.",  # emphasis, not a stuck decoder
])
def test_real_speech_is_trustworthy(line):
    assert trustworthy(line)


@pytest.mark.parametrize("line", [
    "Teapot?", "What", "How can", "Do you", "Are you", "Is it",  # a question, or its start
    "Why is there a", "Where's the",
])
def test_a_question_is_never_swallowed(line):
    assert trustworthy(line)


@pytest.mark.parametrize("line", [
    "Help", "Help me", "Fire.", "Ouch.", "It hurts", "I can't breathe", "I'm scared",
    "Stop stop stop stop", "No no no no", "Emergency.", "I fell", "Call an ambulance",
    "Something wrong",
])
def test_distress_is_never_swallowed(line):
    assert trustworthy(line)


def test_the_name_is_never_swallowed():
    named = {"sage"}.__contains__
    assert not trustworthy("Sage")  # a body's names are its own: this one was told none
    assert trustworthy("Sage", named)
    assert trustworthy("Sage, the", named)


def test_a_line_worth_answering_passes_whatever_the_body_is_doing():
    assert decide("We had soup for dinner.", math.inf) == PASS
    assert decide("We had soup for dinner.", 0.0, busy=True) == PASS


def test_noise_is_unsure_when_the_body_can_show_it():
    assert decide("So the", math.inf) == UNSURE
    assert decide("So the", COOLDOWN_S) == UNSURE


def test_noise_within_the_cooldown_or_on_a_busy_body_is_quiet():
    assert decide("So the", COOLDOWN_S - 0.1) == QUIET
    assert decide("So the", math.inf, busy=True) == QUIET


def test_anything_answers_a_question_just_asked():
    assert decide("Teapot.", math.inf, asked=True) == PASS
    assert decide("So the", math.inf, asked=True) == PASS


def test_nothing_heard_is_not_unsure():
    assert trustworthy("")
    assert trustworthy("  ...  ")
