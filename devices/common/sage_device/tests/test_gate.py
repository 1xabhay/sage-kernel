"""The gate's fourteen scenarios (design 1-gate): made-up lines, none of them recorded speech."""

from dataclasses import replace

from sage_device.gate import (
    ACK,
    ANSWER,
    CLOSE_S,
    FOLLOW_S,
    LISTEN,
    LOW_CONFIDENCE,
    OVERHEARD,
    Attention,
    Signals,
    attend,
    is_open,
    opened,
)

CLOSED, OPEN = Attention(), Attention(open=True, at=100.0)
TV = "And then the whole team came along to see them."


def named(word):
    return word == "sage"


def at(now=101.0, **signals):
    return Signals(now, named=named, **signals)


def did(line, state=CLOSED, **signals):
    return attend(line, at(**signals), state)[0]


def test_1_its_name_anywhere_any_time_is_answered_and_opens_a_conversation():
    outcome, reason, state = attend(
        "So they asked Sage about them and left.", at(commentary=False), CLOSED
    )
    assert (outcome, reason, state.open) == (ANSWER, "name", True)
    assert did("Sage.", confidence=0.05) == ANSWER  # never stepped down


def test_2_distress_is_always_answered():
    assert did("I fell and it hurts.") == ANSWER
    assert did("I can't breathe.", OPEN, confidence=0.05, similarity=0.1) == ANSWER
    assert did("Help.") == ANSWER and did("I feel so alone tonight.") == ANSWER


def test_an_everyday_no_or_dont_is_no_cry_and_is_judged_as_any_other_line():
    """Real replay: 427 of 5,197 lines were distress only for "don't", "no", "stop", "wait"..."""
    assert attend("No, I don't think so.", at(), CLOSED)[:2] == (ACK, "unsure")
    assert attend("Wait, that is wrong.", at(), OPEN)[:2] == (ANSWER, "open")
    assert did("No, don't, that hurts.") == ANSWER  # with a cry in it, it is one


def test_3_after_its_own_question_whatever_comes_back_is_answered():
    assert did("And they left.", asked=True) == ANSWER
    assert did("Blue.", asked=True, confidence=0.05) == ANSWER


def test_4_in_a_conversation_a_question_a_request_or_a_line_about_you_or_me_is_answered():
    for line in (
        "Is it raining.",
        "Play something slow.",
        "That was kind of you.",
        "They left me there.",
    ):
        assert attend(line, at(confidence=0.05), OPEN)[:2] == (ANSWER, "direct")


def test_5_in_a_conversation_a_plain_statement_is_answered_and_keeps_it_open():
    outcome, reason, state = attend("The garden looks nice today.", at(now=110.0), OPEN)
    assert (outcome, reason, state.open, state.at) == (ANSWER, "open", True, 100.0)
    # Answered, and its own reply holds the conversation open; unanswered plain talk does not.
    assert attend("I'd plant roses there.", at(now=125.0), state)[:2] == (ACK, "unsure")
    assert did("I'd plant roses there.", state, now=125.0, spoke_at=112.0) == ANSWER
    assert attend("Is it raining.", at(now=110.0), OPEN)[2].at == 110.0  # a line to it does


def test_6_in_a_conversation_talk_about_others_is_listened_to_and_two_in_a_row_close_it():
    outcome, reason, state = attend(TV, at(), OPEN)
    assert (outcome, reason, state.open, state.at) == (
        LISTEN,
        "aside",
        True,
        100.0,
    )  # not refreshed
    outcome, _, state = attend("But she never told them why.", at(now=102.0), state)
    assert (outcome, state.open) == (LISTEN, False)
    again = attend(TV, at(), OPEN)[2]
    assert (
        attend("The garden looks nice today.", at(now=102.0), again)[2].asides == 0
    )  # not in a row


def test_7_with_a_face_in_view_any_clear_line_is_answered_and_opens():
    outcome, reason, state = attend("The garden looks nice today.", at(face=True), CLOSED)
    assert (outcome, reason, state.open) == (ANSWER, "face", True)
    assert did("Garden", face=True) == ACK  # not a clear line: a look


def test_8_with_no_face_a_greeting_a_you_question_or_a_request_opens_from_anywhere():
    for line in (
        "Good morning.",
        "Hello there.",
        "Can you hear the rain?",
        "Tell me a story.",
        "Call me Lena.",
    ):
        for face in (False, None):  # no face seen, or no camera at all
            assert attend(line, at(face=face), CLOSED)[:2] == (ANSWER, "direct"), line


def test_9_with_no_face_another_question_or_statement_gets_a_look_then_the_next_line_an_answer():
    for line in ("Is it raining?", "The garden looks nice."):
        outcome, reason, state = attend(line, at(), CLOSED)
        assert (outcome, reason, state.open) == (ACK, "unsure", False)
    turned = "I meant you."  # they carry on to it: a plain line from any voice is not that
    assert attend(turned, at(now=101.0 + FOLLOW_S), state)[:2] == (ANSWER, "followed")
    assert did(turned, state, now=102.0 + FOLLOW_S) == ACK  # too late: another look
    assert attend("The rain, outside.", at(now=103.0), state)[:2] == (ACK, "unsure")


def test_10_with_no_face_long_talk_about_others_is_overheard():
    outcome, reason, state = attend(TV, at(), CLOSED)
    assert (outcome, reason, state) == (OVERHEARD, "aside", CLOSED)
    assert (
        did(TV, replace(CLOSED, acked_at=100.0)) == OVERHEARD
    )  # a look just before changes nothing
    assert did("She left.") == ACK  # short: it cannot tell


def test_11_the_tvs_commentary_is_overheard_while_a_short_you_question_gets_through():
    assert attend("He takes the corner wide and holds the lead.", at(commentary=True), OPEN)[
        :2
    ] == (OVERHEARD, "media")
    assert did("Did you see that?", commentary=False) == ANSWER  # media.heard calls it the person's


def test_12_low_confidence_steps_down_one_unless_rules_1_to_4_decided():
    low = LOW_CONFIDENCE - 0.01
    assert attend("The garden looks nice today.", at(confidence=low), OPEN)[:2] == (
        ACK,
        "open, low confidence",
    )
    assert did("The garden looks nice today.", OPEN, confidence=LOW_CONFIDENCE) == ANSWER
    assert (
        did("The garden looks nice.", confidence=low) == LISTEN
    )  # a look steps down to nothing shown
    assert did("Good morning.", confidence=low) == ACK  # rule 8 is not one of 1-4
    assert (
        did("The garden looks nice today.", OPEN, confidence=0.5, low=0.6) == ACK
    )  # the body's own floor
    stepped = attend("The garden looks nice today.", at(confidence=low), OPEN)[2]
    assert stepped.at == 100.0  # a look does not keep the conversation open


def test_13_another_voice_than_the_one_that_opened_it_steps_down_unless_it_says_the_name():
    owner = attend("Good morning.", at(similarity=0.8, doa=10.0), CLOSED)[2]
    assert (owner.similarity, owner.doa) == (0.8, 10.0)
    assert attend("Is it raining.", at(similarity=0.2), owner)[:2] == (ACK, "direct, other voice")
    assert did("Is it raining.", owner, similarity=0.7) == ANSWER
    assert did("Sage, is it raining.", owner, similarity=0.2) == ANSWER
    turned = attend("Good morning.", at(doa=10.0), CLOSED)[2]  # no voiceprint: direction alone
    assert did("Is it raining.", turned, doa=100.0) == ACK
    assert did("Is it raining.", turned, doa=350.0) == ANSWER  # 20 degrees round the back
    assert did("Is it raining.", turned) == ANSWER  # a body with neither: no step
    kept = attend("Is it raining.", at(similarity=0.7, doa=200.0), owner)[2]
    assert (kept.similarity, kept.doa) == (0.8, 10.0)  # the opener's, to the end


def test_14_a_goodbye_a_dismissal_or_a_long_quiet_closes_it():
    outcome, reason, state = attend("Okay, bye.", at(), OPEN)
    assert (outcome, reason, state) == (ANSWER, "open, closed", CLOSED)  # the goodbye is answered
    assert attend("No, not you.", at(), OPEN)[1:] == ("dismissed", CLOSED)
    assert attend("Not talking to you.", at(), OPEN)[0] == OVERHEARD
    assert attend("Stop.", at(), OPEN)[2] == CLOSED
    assert is_open(OPEN, 100.0 + CLOSE_S) and not is_open(OPEN, 100.1 + CLOSE_S)
    assert is_open(OPEN, 130.0, spoke_at=115.0)  # counted from its last word
    assert attend("The garden looks nice.", at(now=121.0), OPEN)[:2] == (ACK, "unsure")
    assert attend("The garden looks nice.", at(now=121.0, spoke_at=104.0), OPEN)[:2] == (
        ANSWER,
        "open",
    )


def test_a_tv_alone_does_not_hold_a_conversation_open():
    """The design's one change: only an ANSWER refreshes it (243 lines in one chain, live)."""
    state, outcomes = OPEN, []
    for i in range(40):  # a line every 10 s, as a broadcast runs
        outcome, _, state = attend(TV, at(now=101.0 + 10 * i), state)
        outcomes.append(outcome)
    assert ANSWER not in outcomes and not state.open
    assert outcomes[:3] == [LISTEN, LISTEN, OVERHEARD]


def test_its_own_first_words_open_a_conversation():
    state = opened(CLOSED, 100.0)
    assert attend("The garden looks nice.", at(), state)[:2] == (ANSWER, "open")


def test_a_mic_only_body_runs_the_same_rules_with_time_alone():
    outcome, _, state = attend("Hey, can you hear me?", Signals(5.0), CLOSED)
    assert (outcome, state.open) == (ANSWER, True)
    assert attend("The garden looks nice.", Signals(9.0), state)[0] == ANSWER


TALK = (  # two people, to each other: plain lines and questions that name no one
    "The garden looks nice.",
    "It really does.",
    "Is it warm out?",
    "A little, by the wall.",
    "The roses came back.",
    "Is that the red one?",
)


def test_two_people_talking_with_no_face_in_view_are_never_answered():
    """Review finding 4: a look, then any line from any voice, opened a conversation of theirs."""
    state, outcomes = CLOSED, set()
    for i in range(20):  # over a minute, a line every 4 s: each inside the last look's window
        outcome, _, state = attend(TALK[i % len(TALK)], at(now=100.0 + 4 * i), state)
        outcomes.add(outcome)
        assert not state.open
    assert outcomes == {ACK}
    for theirs, mine in ((0.8, 0.2), (0.2, 0.8), (0.2, 0.2)):  # voices told apart, or both guests
        looked = attend(TALK[0], at(similarity=theirs), CLOSED)[2]
        assert did(TALK[1], looked, now=104.0, similarity=mine) == ACK


def test_after_a_look_the_person_who_turns_to_it_is_answered():
    looked = attend(TALK[0], at(), CLOSED)[2]
    for line, reason in (
        ("I was talking to you.", "followed"),
        ("Tell me if it rains.", "direct"),
        ("Sage, the garden.", "name"),
    ):
        assert attend(line, at(now=104.0), looked)[:2] == (ANSWER, reason)
    assert attend(TALK[1], at(now=104.0, face=True), looked)[:2] == (ANSWER, "face")
    owner = attend(TALK[0], at(similarity=0.8), CLOSED)[2]  # a voiceprint: the same voice goes on
    assert attend(TALK[4], at(now=104.0, similarity=0.7), owner)[:2] == (ANSWER, "followed")
