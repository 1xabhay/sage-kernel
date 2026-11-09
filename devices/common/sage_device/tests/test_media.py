"""Watching together: a broadcast is noticed from what is heard, and its commentary is never a turn.

Big moments get a move, and now and then a few words.
"""

import math

from sage_device import media

COMMENTARY = [
    "And the leader is pulling away now with a gap of almost eight seconds to the car behind.",
    "Into turn one side by side, neither driver willing to give up the inside line here.",
    "The tyres are starting to go off, you can see him sliding wide on the exit of the corner.",
    "Strategy will be key in the second half of this race with rain expected in twenty minutes.",
    "He has made a superb start from fourth on the grid and is up into second place already.",
]


def watching(lines=COMMENTARY, start=0.0, every=8.0):
    watch = media.Watch()
    for i, line in enumerate(lines):
        watch, _ = media.heard(watch, start + i * every, line)
    return watch


def test_sustained_commentary_starts_a_session_with_no_command():
    watch = media.Watch()
    kinds = []
    for i, line in enumerate(COMMENTARY):
        watch, kind = media.heard(watch, i * 8.0, line)
        kinds.append(kind)
    assert watch.on
    assert kinds[-1] == media.COMMENTARY  # once it is on, a long broadcast line is the TV's
    assert media.context(watch)["about"] == "a sports broadcast"


def test_one_long_line_from_the_person_starts_nothing():
    line = "I spent the whole afternoon cleaning out the garage and it was awful."
    watch, kind = media.heard(media.Watch(), 0.0, line)
    assert not watch.on and kind == media.PERSON


def test_saying_so_starts_it_at_once():
    watch, kind = media.heard(media.Watch(), 0.0, "I'm watching the race.")
    assert watch.on and kind == media.PERSON


def test_the_person_is_still_heard_during_the_broadcast():
    watch = watching()
    for line in ("Yeah.", "That was crazy, buddy.", "Oh no!", "Sage, who do you think wins?",
                 "Do you think he can hold on to it?",
                 "I'm so tired, I need help with dinner later tonight please."):
        kind = media.heard(watch, 50.0, line, named=lambda word: word == "sage")[1]
        assert kind == media.PERSON, line


def test_the_person_gets_what_is_on_and_a_profile_from_its_words():
    lap = "Lap thirty of fifty seven here at the Grand Prix, and the pit wall is busy."
    watch = watching([*COMMENTARY, lap])
    about = media.context(watch)
    assert about["about"] == "a Formula 1 race"
    assert about["heard"][-1].startswith("Lap thirty")
    assert len(about["heard"]) <= media.HEARD
    assert all(len(line) <= media.HEARD_CHARS for line in about["heard"])


def test_it_ends_after_a_quiet_spell():
    watch = watching()
    watch, kind = media.heard(watch, 40.0 + media.END_S + 1, "So, what do you want for dinner?")
    assert not watch.on and kind == media.PERSON
    assert media.context(watch) is None


def test_big_moments_get_a_move_and_now_and_then_a_few_words():
    watch = watching()
    reactions = []
    for i in range(20):  # a big moment every 30 s for ten minutes
        line = "And he's through, what an overtake into the lead!"
        watch, react = media.moment(watch, 60.0 + i * 30.0, line)
        reactions.append(react)
    assert reactions[0] == media.SPEAK
    assert reactions.count(media.SPEAK) == math.ceil(600 / media.SPOKEN_GAP_S)
    assert set(reactions) == {media.SPEAK, media.MOVE}


def test_ordinary_commentary_is_no_moment():
    assert media.moment(watching(), 60.0, COMMENTARY[3])[1] is None


def test_a_film_or_video_keeps_quiet_company():
    film = ["I never thought I would see this town again after all these years away at sea.",
            "We have to leave tonight, before they find out what we took from the house.",
            "She looked out over the water and said nothing for a long time, waiting for the boat.",
            "If you go through that door you can never come back here, do you understand me now?"]
    assert not watching(film).on  # no broadcast cue in the words: it waits to be told
    watch, _ = media.heard(media.Watch(), 0.0, "We're watching a film.")
    for i, line in enumerate(film):
        watch, kind = media.heard(watch, 1.0 + i * 8.0, line)
    assert kind == media.COMMENTARY and media.context(watch)["about"] == "a show or video"
    crash = "Oh no, what a crash, he's out of it!"
    assert media.moment(watch, 60.0, crash)[1] is None  # no spoken cheering in a film


def test_a_meeting_or_call_on_the_laptop_is_never_a_session():
    meeting = ["Okay so the admin page lets each user join the session from the dashboard now.",
               "We should manage the platform tools and the guides before the teachers log in.",
               "I think the second option is better, but let's check numbers with finance first.",
               "Can everyone see my screen, I'm sharing the slide with the quarterly targets.",
               "Right, so next week we move the review to Thursday and keep the same agenda."]
    watch = watching(meeting)
    assert not watch.on  # overheard, not something to watch together (watch/behaviour.md H7)


def test_odds_and_betting_never_reach_the_kernel():
    bet = "The bookmakers have him at two to one now, odds shortening by the lap here."
    kept = media.context(watching([*COMMENTARY, bet]))["heard"]
    assert not any("odds" in line or "bookmaker" in line for line in kept)


def test_a_long_merged_broadcast_stays_the_tvs_even_with_a_worried_word_in_it():
    """Live F1, 11-08: 70 merged words of commentary, "an unsafe rejoin" among them, reached it."""
    long = ("He's not sure about the state of the car and he got out of the way like a back marker "
            "as the leader came through, and I imagine he's thinking about an unsafe rejoin there "
            "into the path of the leader, which the stewards will look at after the race tonight.")
    assert media.heard(watching(), 50.0, long)[1] == media.COMMENTARY
    assert media.heard(watching(), 50.0, "I feel unsafe, something's wrong.")[1] == media.PERSON


def f1_watching():
    lap = "Lap thirty of fifty seven here at the Grand Prix, and the pit wall is busy."
    return watching([*COMMENTARY, lap])


def test_short_commentary_in_the_races_own_words_is_the_tvs():
    watch = f1_watching()
    for line in ("Norris on the soft tyres.", "Box, box.", "Safety car deployed.",
                 "Purple sector for Leclerc.", "P3 for Hamilton."):
        assert media.heard(watch, 50.0, line)[1] == media.COMMENTARY, line


def test_the_persons_short_lines_stay_theirs_while_watching():
    watch = f1_watching()
    for line in ("Yeah.", "That was crazy.", "Who's leading?", "Oh no!", "Is Norris winning?",
                 "Sage, look at that.", "I'm hungry.", "Wow."):
        assert media.heard(watch, 50.0, line, named=lambda w: w == "sage")[1] == media.PERSON, line


def test_short_lines_are_never_the_tvs_outside_a_session():
    assert media.heard(media.Watch(), 0.0, "Norris on the soft tyres.")[1] == media.PERSON


def test_a_session_keeps_its_first_confident_profile_until_it_ends():
    watch = f1_watching()
    plain = "The weather has been lovely here all weekend and the crowd is in good spirits now."
    for i in range(media.HEARD + 1):  # the lap line leaves the last few lines the kernel is shown
        watch, _ = media.heard(watch, 60.0 + i, plain)
    assert media.context(watch)["about"] == "a Formula 1 race"
    gone, _ = media.heard(watch, 60.0 + media.END_S + 10, "Yeah.")
    assert not gone.on
