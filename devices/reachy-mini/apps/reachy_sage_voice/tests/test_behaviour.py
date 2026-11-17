"""What Sage does, at the voice: every ``ci-fake`` voice row of the monorepo's evals/behaviour.json.

The kernel and the body are the suite's fakes. ``@case`` ties a test to its row: a row with no test
fails here, and a row whose status is ``not-built`` or ``bug`` is a strict expected failure. Every
spoken line is invented (the TV's are test_watch's).
"""

import json
from pathlib import Path

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    InterruptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.tests.utils import SleepFrame
from sage_device import floor, media
from test_commands import FakeExecutor, choosing, gesturing
from test_commands import make as with_body
from test_sage_processor import FakeSession, acts, delta, heard, holding, make, run, spoken
from test_watch import COMMENTARY, broadcast, commanding, watching_now

from reachy_sage_voice.sage import UNSURE_CLIP, Playback

TABLE = Path(__file__).resolve().parents[5] / "evals" / "behaviour.json"
if not TABLE.exists():  # the deployed export carries the app alone
    pytest.skip("the behaviour table is in the monorepo's evals/", allow_module_level=True)
CASES = {row["id"]: row for row in json.loads(TABLE.read_text())["cases"]}
DONE = set()


def case(id):
    row = CASES[id]
    assert (row["tier"], row["level"]) == ("ci-fake", "voice"), id

    def mark(test):
        DONE.add(id)
        if row.get("status") in ("not-built", "bug"):
            return pytest.mark.xfail(strict=True, reason=row["why"])(test)
        return test
    return mark


def turns(session):
    return [text for _, text, _ in session.started]


@case("B10")
def test_b10_asked_about_the_tv_the_kernel_is_told_what_is_on():
    session = FakeSession()
    run(make(session, emotes=[]),
        [*broadcast(), heard("Who do you think wins this?"), SleepFrame(0.1)])
    assert turns(session)[-1] == "Who do you think wins this?"
    watching = session.inputs[-1]["media"]
    assert watching["about"] and watching["heard"][-1] == COMMENTARY[-1]


@case("B16")
def test_b16_told_to_look_for_the_person_the_body_searches_at_once_with_no_kernel_turn():
    session, body = FakeSession(), FakeExecutor()
    down, _ = run(with_body(session, body), [heard("Sage, look for me."), SleepFrame(0.05)])
    assert [[step.skill for step in plan] for plan in body.plans] == [["search_face"]]
    assert turns(session) == []
    (line,) = spoken(down)
    assert len(line.split()) <= 4


@case("B17")
def test_b17_stop_always_stops_the_body_the_voice_and_the_kernels_turn():
    session = FakeSession([{"effects": [delta(0, "There was once a snail who")]}])
    body = FakeExecutor()
    down, _ = run(with_body(session, body), [heard("Tell me about the snail."), SleepFrame(0.05),
                                             heard("Stop."), SleepFrame(0.05)])
    assert body.stops == 1 and body.plans == []
    assert turns(session) == ["Tell me about the snail."]  # stop is no turn of its own
    assert session.stops == [session.started[0][0]]
    assert any(isinstance(f, InterruptionFrame) for f in down)
    assert spoken(down) == ["There was once a snail who"]  # and nothing after it
    assert commanding("Stop.")[1] == 1  # even while watching the TV


@case("B19")
def test_b19_command_words_from_the_tv_move_nothing_without_its_name():
    assert commanding("Turn around.", "Look left.") == ([], 0)
    assert commanding("Sage, turn around.")[0] != []


@case("B22")
def test_b22_spoken_over_it_stops_and_tells_the_kernel_only_what_was_heard():
    playback = Playback()
    playback.spoken = ["The snail set off"]
    session = FakeSession([{"effects": [delta(0, "The snail set off at dawn, slowly")]}])
    run(make(session, playback=playback),
        [heard("Tell me about the snail."), SleepFrame(0.1), BotStartedSpeakingFrame(),
         SleepFrame(0.05), InterruptionFrame(), SleepFrame(0.1)])
    (result,) = session.results
    assert (result["status"], result["text"]) == ("interrupted", "The snail set off")
    assert session.stops == [session.started[0][0]]


@case("B23")
def test_b23_a_sentence_stopped_halfway_waits_for_its_rest_and_is_one_turn():
    session = FakeSession()
    run(holding(session, 0.3), [heard("Can you tell my brother that"), SleepFrame(0.1),
                                heard("the bread is in the oven."), SleepFrame(0.4)])
    assert turns(session) == ["Can you tell my brother that the bread is in the oven."]
    assert session.stops == []  # the first half never went alone


@case("B24")
@pytest.mark.parametrize("gap", [True, False], ids=["at a gap", "no gap: dropped"])
def test_b24_watching_a_reply_is_spoken_at_a_gap_in_the_tv_and_never_over_it(monkeypatch, gap):
    monkeypatch.setattr(floor, "GAP_S", 0.2)
    monkeypatch.setattr(floor, "MEDIA_WAIT_S", 0.3)
    session = FakeSession([{"effects": [delta(0, "Good idea.")]}])
    quiet = [VADUserStoppedSpeakingFrame(stop_secs=0.0)] if gap else []
    down, _ = run(watching_now(session), [
        VADUserStartedSpeakingFrame(), heard("I made us some tea."), SleepFrame(0.1), *quiet,
        SleepFrame(0.6)])
    assert turns(session) == ["I made us some tea."]
    assert spoken(down) == (["Good idea."] if gap else [])


@case("B28")
def test_b28_talk_between_other_people_gets_no_turn_no_words_and_no_move():
    emotes, body = [], FakeExecutor()
    session, processor = choosing(emotes, executor=body)
    down, _ = run(processor, [heard("And then her cousin carried the whole lot upstairs."),
                              SleepFrame(0.05)])
    assert (turns(session), spoken(down), emotes, acts(session)) == ([], [], [], [])
    assert "think" not in body.log  # not even the look of an answer coming


@case("B30")
def test_b30_tv_commentary_never_becomes_a_turn_once_it_is_watching():
    session = FakeSession()
    down, _ = run(make(session, emotes=[]), broadcast())
    assert session.watching and spoken(down) == []
    assert not set(turns(session)) & set(COMMENTARY[media.START_LINES - 1:])


@case("B40")
def test_b40_a_command_it_cannot_do_gets_a_tilt_or_a_few_words_never_nothing():
    session, emotes = FakeSession(), []
    down, _ = run(gesturing(session, FakeExecutor(), emotes),
                  [heard("Sage, do a backflip."), SleepFrame(0.05)])
    assert (UNSURE_CLIP, False) in emotes or spoken(down)


def test_every_ci_fake_voice_row_has_its_test():
    assert {id for id, row in CASES.items()
            if row["tier"] == "ci-fake" and row["level"] == "voice"} == DONE
