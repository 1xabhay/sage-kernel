"""Whether a heard line needs words: decided from the words alone, no body or kernel in sight."""

import ast
import importlib
import inspect
import sys
from pathlib import Path

import pytest

from sage_device import choice


def named(word):
    return word in ("sage", "reachy")


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
    "So look at me.",  # an order after a word that leads one in, no comma between
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
def test_a_line_that_needs_words_says_why(line):
    assert choice.must_speak(line, named)


@pytest.mark.parametrize("line", STATEMENTS + FAREWELLS)
def test_a_plain_statement_needs_none(line):
    assert choice.must_speak(line, named) == ""


def test_a_line_the_kernel_could_not_be_told_whole_is_too_long():
    """A move-only line reaches memory as an acted reason of at most 64 characters."""
    longest = "I " + "x" * (choice.TOLD_CHARS - 3) + "."
    assert len(longest) == choice.TOLD_CHARS and len("heard: " + longest) <= 64
    assert choice.must_speak(longest) == ""
    assert choice.must_speak("I " + "x" * (choice.TOLD_CHARS - 2) + ".") == "too long"


def test_no_name_in_the_code_could_carry_absence_or_a_reward():
    """L1, L2, L5 (flow/ethics.md): it decides from the line, and nothing else can reach it."""
    assert set(inspect.signature(choice.must_speak).parameters) == {"line", "named"}
    tree = ast.parse(inspect.getsource(choice))  # what the code names, not what its comments say
    names = {str(getattr(node, key, "")).lower() for node in ast.walk(tree)
             for key in ("id", "arg", "attr", "name")}
    for word in ("reward", "engag", "retention", "duration", "absen", "last_seen", "presence",
                 "session"):
        assert not [name for name in names if word in name], word


def test_the_coin_that_swapped_words_for_a_nod_is_gone():
    """Design 1-gate: whether a line is for it is the gate's; words or a nod, the kernel's."""
    assert not hasattr(choice, "speak_or_move") and not hasattr(choice, "MOVE_ODDS")


@pytest.mark.parametrize("line", QUESTIONS + DISTRESS)
def test_questions_and_distress_need_words_in_any_case_and_with_a_curly_apostrophe(line):
    for said in (line.upper(), line.lower(), line.replace("'", "\u2019")):
        assert choice.must_speak(said, named), said


def test_the_distress_and_question_patterns_are_the_kernels():
    """The device holds copies, importing no kernel code: what the kernel adds must stop a move."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))  # its words are in a file it loads
    kernel = vars(importlib.import_module("sage.policy"))
    assert choice._KERNEL_DISTRESS.pattern == kernel["DISTRESS"].pattern
    assert choice._KERNEL_QUESTION.pattern == kernel["QUESTION"].pattern
