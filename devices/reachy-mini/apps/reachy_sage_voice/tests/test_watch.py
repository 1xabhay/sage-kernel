"""Watching together, wired (owner, 2026-11-08, Formula 1).

Commentary never reaches the kernel as the person's line, the person's own line carries what is on,
and a big moment gets a cheer.
"""

from pipecat.tests.utils import SleepFrame
from sage_device import media
from test_sage_processor import FakeSession, heard, make, run

COMMENTARY = [
    "And the leader is pulling away now with a gap of almost eight seconds to the car behind.",
    "Into turn one side by side, neither driver willing to give up the inside line here.",
    "The tyres are starting to go off, he is sliding wide on the exit of the corner again.",
    "Strategy will be key in the second half of this race with rain expected in twenty minutes.",
    "Lap thirty of fifty seven here at the Grand Prix, and the pit wall is getting busy now.",
]


def broadcast(*lines):
    frames = []
    for line in (*COMMENTARY, *lines):
        frames += [heard(line), SleepFrame(0.05)]
    return frames


def test_commentary_starts_a_session_and_never_becomes_a_turn():
    session = FakeSession()
    run(make(session, emotes=[]), broadcast())
    starts = [text for _, text, _ in session.started]
    assert not set(starts) & set(COMMENTARY[media.START_LINES - 1:])  # once it is on, none of it
    assert len(starts) < media.START_LINES  # and before it was on, at most the first few


def test_the_persons_line_carries_what_is_on():
    session = FakeSession()
    run(make(session, emotes=[]), [*broadcast(), heard("Who's leading?"), SleepFrame(0.1)])
    assert session.started[-1][1] == "Who's leading?"
    watching = session.inputs[-1]["media"]
    assert watching["about"] == "a Formula 1 race"
    assert watching["heard"][-1].startswith("Lap thirty")


def test_a_big_moment_gets_words_first_then_moves():
    session, emotes = FakeSession(), []
    moments = ["And he's through, what an overtake into the lead at turn four!",
               "What a move, he takes second place with a brilliant pass into turn nine!"]
    run(make(session, emotes=emotes), [*broadcast(*moments), SleepFrame(0.2)])
    told = [(text, i) for (_, text, _), i in zip(session.started, session.inputs, strict=True)
            if (i or {}).get("media", {}).get("moment")]
    assert [text for text, _ in told] == moments[:1]  # a few words, once: the TV's line, marked so
    assert any(e in media_cheers() for e in emotes)  # the next one is a move alone


def media_cheers():
    from reachy_sage_voice.expressions import REACTIONS
    return REACTIONS["cheer"]


def test_a_media_session_mutes_presence_for_faces_on_the_screen():
    session = FakeSession()
    run(make(session, emotes=[]), broadcast())
    assert session.watching


class Body:
    """An executor that says yes to everything and counts stops."""

    stopped = 0

    def busy(self):
        return None

    def reflex_busy(self):
        return None

    def __getattr__(self, name):  # hold, attend, think, resume...: nothing to do
        return lambda *args, **kwargs: None

    async def stop(self):
        self.stopped += 1


def commanding(*lines):
    """Run ``lines`` after a broadcast through a processor with a body; return plans run, stops."""
    from reachy_sage_voice.sage import Playback, SageProcessor

    async def play_emote(name, sound):
        pass
    body, plans = Body(), []
    processor = SageProcessor(FakeSession(), play_emote, Playback(), executor=body)
    processor._run_plan = lambda plan, sequence=None, said=None: plans.append(plan[0].skill)
    frames = broadcast()
    for line in lines:
        frames += [heard(line), SleepFrame(0.05)]
    run(processor, frames)
    return plans, body.stopped


def test_no_body_command_from_the_tv_while_watching():
    """S0: "turn" and "look" are commentary words; only the name makes them a command."""
    assert commanding("Turn around.", "Look left.") == ([], 0)
    assert commanding("Sage, turn around.")[0] != []


def test_stop_always_stops_even_while_watching():
    assert commanding("Stop.")[1] == 1  # a false stop costs nothing; a missed one does


# --- the floor while watching: the TV never yields, so a reply waits for a gap in it ------------


def watching_now(session):
    """Make a processor already watching, on the real clock, that always answers in words."""
    import time

    from test_sage_processor import real_clock
    processor = real_clock(session)
    processor._watch = media.Watch(on=True, since=time.monotonic())
    processor._rng = type("Words", (), {"random": lambda self: 1.0})()
    return processor


def test_watching_a_reply_with_no_gap_in_the_tv_is_dropped_not_spoken_over_it(monkeypatch, caplog):
    import logging

    from pipecat.frames.frames import VADUserStartedSpeakingFrame
    from sage_device import floor
    from test_sage_processor import delta, spoken
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(floor, "MEDIA_WAIT_S", 0.3)
    session = FakeSession([{"effects": [delta(0, "Sure is.")]}])
    down, _ = run(watching_now(session), [
        VADUserStartedSpeakingFrame(), heard("We had soup."), SleepFrame(0.7)])
    assert session.started and spoken(down) == []
    assert "media: reply dropped" in caplog.text


def test_watching_a_reply_goes_at_a_gap_in_the_tv(monkeypatch):
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame
    from sage_device import floor
    from test_sage_processor import delta, spoken
    monkeypatch.setattr(floor, "GAP_S", 0.2)
    session = FakeSession([{"effects": [delta(0, "Sure is.")]}])
    down, _ = run(watching_now(session), [
        VADUserStartedSpeakingFrame(), heard("We had soup."), SleepFrame(0.1),
        VADUserStoppedSpeakingFrame(stop_secs=0.0), SleepFrame(0.5)])
    assert spoken(down) == ["Sure is."]


def test_watching_a_question_to_it_is_answered_by_its_cap_even_over_the_tv(monkeypatch):
    from pipecat.frames.frames import VADUserStartedSpeakingFrame
    from sage_device import floor
    from test_sage_processor import delta, spoken
    monkeypatch.setattr(floor, "MUST_WAIT_S", 0.2)
    session = FakeSession([{"effects": [delta(0, "Norris, it sounds like.")]}])
    down, _ = run(watching_now(session), [
        VADUserStartedSpeakingFrame(), heard("Who's leading?"), SleepFrame(0.6)])
    assert spoken(down) == ["Norris, it sounds like."]
