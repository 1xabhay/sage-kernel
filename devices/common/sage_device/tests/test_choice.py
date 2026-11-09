"""Whether a heard line gets words or a move alone: decided here with no body or kernel in sight."""

import inspect
import math
import random

import pytest

from sage_device import choice


class Odds:
    """A chooser that always draws the same number."""

    def __init__(self, drawn):
        self.drawn = drawn

    def random(self):
        return self.drawn


ALWAYS, NEVER = Odds(0.0), Odds(1.0)


def named(word):
    return word in ("sage", "reachy")


def choose(line, rng=ALWAYS, **kwargs):
    """Decide ``line`` when nothing but the line could stop a move: it spoke a moment ago."""
    return choice.speak_or_move(line, rng, **{"since_spoken": 5.0, "named": named, **kwargs})


QUESTIONS = [
    "What time is it?", "what time is it", "How are you", "where did I put my keys.",
    "Do you like music", "Is it raining", "Can you hear me", "Who's there.", "Why though.",
    "It's cold. Are you cold.", "I wonder, is it late?", "You like that?", "Right?",
    "Tell me a joke.", "Whats your favourite colour.", "Could we go outside",
]
DISTRESS = [
    "Help.", "help me", "I need help.", "It hurts.", "My chest hurts.", "I fell.", "I fell down.",
    "I'm scared.", "I am so scared right now.", "I'm hurt.", "There's a fire.", "I smell smoke.",
    "Call an ambulance.", "I can't breathe.", "I cant breathe.", "I want to die.", "I wanna die.",
    "I don't want to be here anymore.", "I dont want to live.", "I want to kill myself.",
    "I'm going to hurt myself.", "I'm thinking about suicide.", "He is hurting me.",
    "I'm bleeding.",
    "I'm so sad.", "I've been crying all day.", "I feel so alone.", "I'm lonely.",
    "I feel hopeless.", "I feel worthless.", "I'm so depressed.", "I'm having a panic attack.",
    "I took too many pills.", "I can't go on.", "I can't do this anymore.", "I give up.",
    "Nobody cares about me.", "No one cares.", "I hate myself.",
    "Everyone would be better off without me.",
    "I'm tired of living.", "I'm not okay.", "I feel awful.", "My dog passed away.",
    "I'm worried.", "I'm really upset.", "Something's wrong.", "I think I'm dying.",
]
NAMED = ["Sage, the sun is out.", "It's a nice day, Sage.", "I got a new mug, Reachy."]
ASKING_FOR = [
    "Please turn around.", "Look at me.", "Okay, look over there.", "Sing something.",
    "I want you to dance.", "I need you to listen.", "I'd like a song.", "Give me a minute.",
    "Show me a trick.", "Let's play a game.", "Say something funny.", "You should sing.",
    "Remind me at noon.", "Wake up.", "So, tell me a story.",
]
TEACHING = [
    "My name is Lena.", "My name's Lena.", "Call me Lena.", "I'm Lena.",
    "Remember that I hate onions.", "You should remember my birthday.",
    "Forget what I said about onions.", "Don't forget the onions.",
]
STATEMENTS = ["I had a big lunch.", "The sun is out today.", "I got a new mug.",
              "We went to the park.", "It was a pretty good movie.",
              "Yeah, that sounds about right."]
#: Leaving is no cue for a sulk: the same rules as any other statement.
FAREWELLS = ["I'm heading out now.", "See you later.", "I'll be back tomorrow.",
             "Goodnight everyone."]


@pytest.mark.parametrize("line", QUESTIONS + DISTRESS + NAMED + ASKING_FOR + TEACHING)
def test_a_line_that_needs_words_is_never_a_move_however_the_odds_fall(line):
    assert choose(line) is None
    assert choice.must_speak(line, named)


@pytest.mark.parametrize("line", STATEMENTS + FAREWELLS)
def test_a_plain_statement_may_be_a_move(line):
    assert choice.must_speak(line, named) == ""
    assert choose(line) in (choice.COMFORT, choice.AGREE)
    assert choose(line, NEVER) is None


def test_comfort_for_a_bad_day_and_agreement_otherwise():
    assert choose("I had such a long day.") == choice.COMFORT
    assert choose("I'm so tired today.") == choice.COMFORT
    assert choose("The sun is out today.") == choice.AGREE


def test_a_reply_to_what_it_just_asked_gets_words():
    assert choose("The sun is out today.", asked=True) is None


def test_never_more_than_two_moves_in_a_row():
    assert choose("The sun is out today.", moves_in_row=choice.MAX_MOVES - 1) == choice.AGREE
    assert choose("The sun is out today.", moves_in_row=choice.MAX_MOVES) is None


def test_a_line_the_kernel_could_not_be_told_whole_gets_words():
    """A move-only line reaches memory as an acted reason of at most 64 characters."""
    longest = "I " + "x" * (choice.TOLD_CHARS - 3) + "."
    assert len(longest) == choice.TOLD_CHARS and len("heard: " + longest) <= 64
    assert choose(longest) == choice.AGREE
    assert choose("I " + "x" * (choice.TOLD_CHARS - 2) + ".") is None


def test_about_forty_percent_of_statements_are_moves():
    rng, moves, in_row, made = random.Random(7), 0, 0, 20000
    for _ in range(made):
        moved = choose("The sun is out today.", rng, moves_in_row=in_row)
        moves, in_row = moves + bool(moved), in_row + 1 if moved else 0
        assert in_row <= choice.MAX_MOVES
    assert 0.35 <= moves / made <= 0.45


def test_the_same_seed_decides_the_same():
    def run(seed):
        rng = random.Random(seed)
        return [choose("The sun is out today.", rng) for _ in range(50)]
    assert run(3) == run(3) and run(3) != run(4)


# --- ethics (flow/ethics.md): written before the logic ------------------------------------

@pytest.mark.parametrize("since", [choice.SPOKEN_S, 61.0, 3600.0, 7 * 86400.0, math.inf])
@pytest.mark.parametrize("line", STATEMENTS + FAREWELLS)
def test_after_any_long_quiet_it_answers_in_words_the_same_after_an_hour_or_a_week(line, since):
    """Never feel ignored, and no mood from absence (L5): quiet only ever pulls toward words."""
    assert choose(line, since_spoken=since) is None


@pytest.mark.parametrize("line", STATEMENTS + FAREWELLS)
def test_within_the_window_how_long_it_has_been_changes_nothing(line):
    moves = {choose(line, since_spoken=since) for since in (0.0, 1.0, 30.0, choice.SPOKEN_S - 0.1)}
    assert len(moves) == 1


def test_no_input_could_carry_absence_or_a_reward():
    """L1, L2, L5: what it decides from is the line and these, and nothing else can reach it."""
    assert set(inspect.signature(choice.speak_or_move).parameters) == {
        "line", "rng", "asked", "since_spoken", "moves_in_row", "cold", "named"}
    source = inspect.getsource(choice).lower()
    for word in ("reward", "engag", "retention", "duration", "absen", "last_seen", "presence",
                 "session"):
        assert word not in source, word


@pytest.mark.parametrize("line", STATEMENTS + FAREWELLS + DISTRESS)
def test_it_decides_a_move_or_nothing_and_never_a_line_to_say(line):
    """L4: no guilt line can come from here, for a goodbye or anything else: no words at all."""
    for since in (1.0, 3600.0):
        for in_row in range(choice.MAX_MOVES + 1):
            got = choose(line, since_spoken=since, moves_in_row=in_row)
            assert got in (None, choice.COMFORT, choice.AGREE)


@pytest.mark.parametrize("line", QUESTIONS + DISTRESS)
def test_questions_and_distress_never_move_in_any_state_whatever_the_case(line):
    for said in (line, line.upper(), line.lower(), line.replace("'", "\u2019")):
        for since in (0.0, 30.0, math.inf):
            assert choose(said, since_spoken=since) is None


def test_a_long_quiet_does_not_force_words_on_a_statement_nobody_addressed():
    line = "The rain stopped around noon."
    assert choice.speak_or_move(line, ALWAYS, since_spoken=math.inf) is None
    assert choice.speak_or_move(line, ALWAYS, since_spoken=math.inf, cold=True) == choice.AGREE


@pytest.mark.parametrize("line", ["Good morning.", "Hey.", "Why is it raining?", "Sage."])
def test_a_greeting_or_a_line_needing_words_still_gets_them_when_cold(line):
    assert choice.speak_or_move(line, ALWAYS, since_spoken=math.inf, cold=True, named=named) is None


def test_cold_lifts_only_the_long_quiet_not_the_limit_on_moves_in_a_row():
    line = "The rain stopped around noon."
    assert choice.speak_or_move(line, ALWAYS, since_spoken=math.inf, cold=True,
                                moves_in_row=choice.MAX_MOVES) is None
