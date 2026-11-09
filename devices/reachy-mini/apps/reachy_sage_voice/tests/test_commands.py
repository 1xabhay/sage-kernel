"""Spoken commands end to end in SageProcessor: the device's fast lane, and the kernel's body_plan.

The executor is a fake (test_executor.py covers the real one): what is checked is
the routing. A fast-lane command never starts a kernel turn and is acknowledged
in a canned line; stop reaches the executor first and cuts the voice too;
anything else, commands included, goes to the kernel, whose body_plan effects
run on the executor and are reported.
"""

import asyncio
import logging
from itertools import pairwise

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    InterruptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.tests.utils import SleepFrame, run_test
from sage_device import choice, names
from test_sage_processor import TURN, FakeSession, acts, aside, delta, heard, run, spoken

from reachy_sage_voice.executor import Result
from reachy_sage_voice.expressions import MOVES_LINE, REACTIONS
from reachy_sage_voice.manifest import EMOTIONS
from reachy_sage_voice.plan import Step, dump
from reachy_sage_voice.sage import (
    ACKS,
    NOT_FOUND_EMOTE,
    NOT_FOUND_LINE,
    Playback,
    SageProcessor,
)

TRACKING = Result("done", text="now tracking you")


class FakeExecutor:
    def __init__(self, result=TRACKING):
        self.result, self.plans, self.stops, self.log = result, [], 0, []
        self.reflex_block, self.ended_thought = None, False

    async def run(self, plan):
        self.plans.append(plan)
        self.log.append("run")
        return self.result

    async def stop(self):
        self.stops += 1
        self.log.append("stop")

    def busy(self):
        return None

    def hold(self):
        self.log.append("hold")

    def resume(self):
        self.log.append("resume")

    def attend(self):
        self.log.append("attend")

    def think(self):
        self.log.append("think")

    def replying(self):
        self.log.append("replying")

    def thought_over(self):
        self.ended_thought = True

    def reflex_busy(self):
        return self.reflex_block

    async def home(self):
        self.log.append("home")


def make(session, executor):
    return SageProcessor(session, lambda name, sound: None, Playback(), executor=executor)


def body_plan(sequence, steps, stale=False):
    return {"sequence": sequence, "kind": "body_plan", "turn_id": TURN, "input": {"steps": steps},
            "stale": stale}


def test_the_owners_commands_run_on_the_body_without_a_kernel_turn():
    for said, skill, line in [("Reachy, look for me.", "search_face", "Looking for you."),
                              ("Reachy, start face tracking me.", "track_face", "Following you."),
                              ("Reachy, dance to the music coming from my speakers.", "dance",
                               "Dancing!")]:
        session, executor = FakeSession(), FakeExecutor()
        down, _ = run(make(session, executor), [heard(said), SleepFrame(0.05)])
        assert session.started == [], said
        assert [s.skill for s in executor.plans[0]] == [skill]
        assert spoken(down) == [line] == [ACKS[skill]]


def test_stop_reaches_the_executor_first_cuts_the_voice_and_says_nothing():
    session, executor = FakeSession([{"effects": [delta(0, "Once upon a time")]}]), FakeExecutor()
    down, _ = run(make(session, executor), [heard("Tell me a story."), SleepFrame(0.05),
                                            heard("Stop."), SleepFrame(0.05)])
    assert executor.stops == 1 and executor.plans == []
    assert [t for _, t, _ in session.started] == ["Tell me a story."]  # stop is no turn
    assert session.stops == [session.started[0][0]]  # the kernel turn is stopped too
    assert any(isinstance(f, InterruptionFrame) for f in down)
    assert spoken(down) == ["Once upon a time"]


def test_the_body_glances_at_the_voice_holds_still_while_they_speak_then_thinks():
    session, executor = FakeSession(), FakeExecutor()
    run(make(session, executor), [VADUserStartedSpeakingFrame(), SleepFrame(0.02),
                                  VADUserStoppedSpeakingFrame(stop_secs=0.2), SleepFrame(0.02),
                                  heard("Tell me a story."), SleepFrame(0.02),
                                  BotStartedSpeakingFrame(), SleepFrame(0.02)])
    assert executor.log == ["hold", "attend", "resume", "think", "replying"]
    assert [t for _, t, _ in session.started] == ["Tell me a story."]


def test_noise_with_no_words_gets_no_thinking():
    executor = FakeExecutor()
    run(make(FakeSession(), executor), [VADUserStartedSpeakingFrame(), SleepFrame(0.02),
                                        VADUserStoppedSpeakingFrame(stop_secs=0.2),
                                        SleepFrame(0.02)])
    assert executor.log == ["hold", "attend", "resume"]


def test_words_that_start_no_reply_get_no_thinking():
    executor = FakeExecutor()
    run(make(FakeSession(), executor), [heard("Stop tracking me."), SleepFrame(0.02)])
    assert "think" not in executor.log  # a command: the plan has the head


def test_an_unfinished_fragment_thinks_only_once_it_goes_to_the_kernel():
    session, executor = FakeSession(), FakeExecutor()
    processor = SageProcessor(session, lambda name, sound: None, Playback(), executor=executor,
                              hold_s=0.2)
    run(processor, [heard("Remind me to call"), SleepFrame(0.05),
                    BotStartedSpeakingFrame(), SleepFrame(0.3)])
    assert executor.log == ["replying", "think"] and len(session.started) == 1


def test_its_own_voice_in_the_mic_gets_no_glance_and_no_thinking():
    executor, playback = FakeExecutor(), Playback()
    playback.speaking = True
    run(SageProcessor(FakeSession(), lambda name, sound: None, playback, executor=executor),
        [VADUserStartedSpeakingFrame(), SleepFrame(0.02),
         VADUserStoppedSpeakingFrame(stop_secs=0.2), SleepFrame(0.02),
         heard("Tell me a story."), SleepFrame(0.02)])
    assert executor.log == ["resume"]  # nor a hold: its own voice must not pause a search


def test_a_search_calls_out_the_owners_name():
    class Calling(FakeExecutor):
        async def run(self, plan):
            await self.call_out()
            return await super().run(plan)

    executor = Calling()
    processor = SageProcessor(FakeSession(), lambda name, sound: None, Playback(),
                              executor=executor, names=names.parse("Mika:Mee-kah:Micah"))
    down, _ = run(processor, [heard("Find me."), SleepFrame(0.05)])
    assert "Mika, where are you?" in spoken(down)


def test_what_moves_can_you_do_is_answered_without_moving():
    session, executor = FakeSession(), FakeExecutor()
    down, _ = run(make(session, executor), [heard("Sage, what moves can you do?"),
                                            SleepFrame(0.05)])
    assert spoken(down) == [MOVES_LINE] and executor.plans == [] and session.started == []


def test_a_move_by_voice_runs_on_the_body_without_a_kernel_turn():
    session, executor = FakeSession(), FakeExecutor()
    run(make(session, executor), [heard("Show me happy."), SleepFrame(0.05)])
    assert executor.plans == [(Step("emote", "happy"),)] and session.started == []


def test_a_glance_skipped_because_reachy_speaks_is_logged(caplog):
    import logging

    executor, playback = FakeExecutor(), Playback()
    playback.speaking = True
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.sage"):
        run(SageProcessor(FakeSession(), lambda name, sound: None, playback, executor=executor),
            [VADUserStartedSpeakingFrame(), SleepFrame(0.02)])
    assert any("glance skipped" in r.getMessage() for r in caplog.records)


def test_stop_tracking_is_a_plan_not_a_stop():
    executor = FakeExecutor(Result("done", text="stopped tracking"))
    run(make(FakeSession(), executor), [heard("Stop tracking me."), SleepFrame(0.05)])
    assert executor.stops == 0 and executor.plans == [(Step("stop_tracking"),)]


def test_a_search_that_finds_no_one_complains_then_stays_at_the_default_pose():
    executor, emotes = FakeExecutor(Result("failed", "not_found", "no face in any view")), []
    processor = SageProcessor(FakeSession(), lambda name, sound: emotes.append((name, sound)),
                              Playback(), executor=executor)
    down, _ = run(processor, [heard("Find me."), SleepFrame(0.05)])
    assert spoken(down) == [ACKS["search_face"], NOT_FOUND_LINE]
    assert NOT_FOUND_LINE == "Aw, I looked everywhere and couldn't find you!"  # owner, 2026-10-23
    assert emotes == [(NOT_FOUND_EMOTE, False)]  # muted: the line says it
    assert executor.log[-1] == "home"  # the emote ends, the head goes back to the default pose


def test_commands_the_fast_lane_does_not_know_go_to_the_kernel():
    session, executor = FakeSession(), FakeExecutor()
    run(make(session, executor), [heard("Look left then right."), SleepFrame(0.05)])
    assert [t for _, t, _ in session.started] == ["Look left then right."]
    assert executor.plans == [] and executor.stops == 0


def test_without_an_executor_everything_goes_to_the_kernel():
    session = FakeSession()
    run(make(session, None), [heard("Stop."), SleepFrame(0.05)])
    assert [t for _, t, _ in session.started] == ["Stop."]


def test_a_body_plan_from_the_kernel_runs_and_is_reported_with_its_text():
    session, executor = FakeSession([{"effects": [body_plan(41, [{"skill": "track_face"}])]}]), \
        FakeExecutor()
    run(make(session, executor), [heard("Keep an eye on me, would you?"), SleepFrame(0.1)])
    assert executor.plans == [(Step("track_face"),)]
    assert {"sequence": 41, "status": "done", "text": "now tracking you"} in session.results


def test_a_failed_or_interrupted_plan_is_reported_so():
    for result, expected in [
            (Result("failed", "not_found", "no face found in 15 s"),
             {"sequence": 41, "status": "failed", "reason": "not_found",
              "text": "no face found in 15 s"}),
            (Result("interrupted", text="turned 40 degrees left"),
             {"sequence": 41, "status": "interrupted", "text": "turned 40 degrees left"})]:
        session = FakeSession([{"effects": [body_plan(41, [{"skill": "search_face"}])]}])
        run(make(session, FakeExecutor(result)), [heard("Where are you looking?"), SleepFrame(0.1)])
        assert expected in session.results


def test_a_body_plan_the_body_cannot_parse_fails_with_the_reason():
    session, executor = FakeSession([{"effects": [body_plan(41, [{"skill": "juggle"}])]}]), \
        FakeExecutor()
    run(make(session, executor), [heard("Juggle!"), SleepFrame(0.1)])
    assert executor.plans == []
    (result,) = [r for r in session.results if r.get("sequence") == 41]
    assert (result["status"], result["reason"]) == ("failed", "invalid")
    assert "juggle" in result["text"]


def test_a_stale_body_plan_does_not_move():
    session, executor = FakeSession([{"effects": [body_plan(41, [{"skill": "dance"}], True)]}]), \
        FakeExecutor()
    run(make(session, executor), [heard("Shall we have a party?"), SleepFrame(0.1)])
    assert executor.plans == []
    assert {"sequence": 41, "status": "failed", "reason": "stale"} in session.results


def test_a_stop_from_the_kernel_stops_the_body_too():
    session, executor = FakeSession([{"effects": [{"sequence": 43, "kind": "stop", "turn_id": None,
                                                   "stale": False}]}]), FakeExecutor()
    run(make(session, executor), [heard("Tell me more."), SleepFrame(0.1)])
    assert executor.stops == 1


def test_a_long_report_text_is_cut_to_what_the_kernel_tells():
    executor = FakeExecutor(Result("done", text="x" * 300))
    session = FakeSession([{"effects": [body_plan(41, [{"skill": "track_face"}])]}])
    run(make(session, executor), [heard("Follow me around."), SleepFrame(0.1)])
    (result,) = [r for r in session.results if r.get("sequence") == 41]
    assert len(result["text"]) == 120


def test_listen_song_on_the_body_uses_the_song_listener():
    listened = []

    class Listener:
        busy = False

        async def listen(self, say, notes):
            listened.append(True)
            return "done", None

    async def main():
        from fake_mini import FakeMini

        from reachy_sage_voice.executor import Executor
        executor, session = Executor(FakeMini(), segment_s=0.02), FakeSession()
        session.notes = None
        processor = SageProcessor(session, lambda n, s: None, Playback(), song=Listener(),
                                  executor=executor)
        assert processor is not None and executor.listen is not None
        await executor.listen()
    asyncio.run(main())
    assert listened == [True]



class SlowListener:
    """A song listen that lasts until stopped: ``busy`` meanwhile, as ``song.SongListener``."""

    busy = False

    async def listen(self, say, notes):
        self.busy = True
        try:
            await asyncio.sleep(10)
            return "done", None
        finally:
            self.busy = False


def listening(session, executor):
    session.notes = None
    playback = Playback()

    async def idle():  # its "Okay, I'm listening." has played
        pass

    playback.idle = idle
    return SageProcessor(session, lambda n, s: None, playback, song=SlowListener(),
                         executor=executor)


def test_commands_still_run_while_a_song_is_listened_to_and_the_rest_is_the_music():
    executor, session = FakeExecutor(), FakeSession()
    run(listening(session, executor), [
        heard("Listen to this song."), SleepFrame(0.05),
        heard("We dance all night under the lights"), SleepFrame(0.05),
        heard("Sage, look at me."), SleepFrame(0.05)])
    assert executor.plans == [(Step("look", "me"),)]
    assert session.started == []  # the lyrics never reached the kernel


def test_stop_ends_the_song_listen_too():
    executor, session = FakeExecutor(), FakeSession()
    run(listening(session, executor), [heard("Listen to this song."), SleepFrame(0.05),
                                       heard("Stop."), SleepFrame(0.05),
                                       heard("Hello there."), SleepFrame(0.05)])
    assert executor.stops == 1
    assert [text for _, text, _ in session.started] == ["Hello there."]  # no longer the music


# --- acts: what the body did on its own reaches the kernel (protocol v1.1) ---------


def plan_act(plan, status, reason=None, said=None):
    act = {"kind": "acted", "origin": "reflex", "what": "body_plan", "input": dump(plan),
           "status": status, "reason": reason, "said": said}
    return {k: v for k, v in act.items() if v is not None}


def test_a_reflex_plan_that_ends_tells_the_kernel_what_it_did_and_said():
    session, executor = FakeSession(), FakeExecutor()
    run(make(session, executor), [heard("Reachy, start face tracking me."), SleepFrame(0.05)])
    assert acts(session) == [plan_act(executor.plans[0], "done", said="Following you.")]


def test_a_failed_reflex_search_reports_why_and_every_line_it_said_in_one_act():
    session, executor = FakeSession(), FakeExecutor(Result("failed", "not_found", "no face"))
    run(make(session, executor), [heard("Find me."), SleepFrame(0.05)])
    assert acts(session) == [plan_act(executor.plans[0], "failed", "not_found",
                                      f"{ACKS['search_face']} {NOT_FOUND_LINE}")]


def test_an_interrupted_reflex_plan_says_so():
    session, executor = FakeSession(), FakeExecutor(Result("interrupted", text="turned 40"))
    run(make(session, executor), [heard("Show me happy."), SleepFrame(0.05)])
    assert acts(session) == [plan_act(executor.plans[0], "interrupted")]


def test_a_stop_by_voice_is_an_act_too():
    session = FakeSession()
    run(make(session, FakeExecutor()), [heard("Stop."), SleepFrame(0.05)])
    assert acts(session) == [plan_act((Step("stop"),), "done")]


def test_a_line_said_outside_a_turn_is_an_aside():
    session = FakeSession()
    run(make(session, FakeExecutor()), [heard("Sage, what moves can you do?"), SleepFrame(0.05)])
    assert acts(session) == [aside(MOVES_LINE)]


def test_a_kernels_plan_keeps_its_result_and_only_its_complaint_is_an_aside():
    session = FakeSession([{"effects": [body_plan(41, [{"skill": "search_face"}])]}])
    executor = FakeExecutor(Result("failed", "not_found", "no face"))
    run(make(session, executor), [heard("Where are you looking?"), SleepFrame(0.1)])
    assert {"sequence": 41, "status": "failed", "reason": "not_found", "text": "no face"} \
        in session.results
    assert acts(session) == [aside(NOT_FOUND_LINE)]


# --- gestures ------------------------------------------------------------------------------------

def gesturing(session, executor, emotes, now=None, **kwargs):
    async def play_emote(name, sound):
        emotes.append((name, sound))
    return SageProcessor(session, play_emote, kwargs.pop("playback", Playback()), executor=executor,
                         **({"now": now} if now else {}), **kwargs)


def test_a_greeting_moves_the_body_at_once_with_no_kernel_turn_and_no_words():
    session, executor, emotes = FakeSession(), FakeExecutor(), []
    down, _ = run(gesturing(session, executor, emotes), [heard("Hi Sage."), SleepFrame(0.05)])
    assert session.started == [] and spoken(down) == []
    ((clip, sound),) = emotes
    assert clip in REACTIONS["greet"] and sound is False  # muted: its sound would trip the VAD
    assert "think" not in executor.log


def test_a_gesture_is_told_to_the_kernel_as_what_the_body_did_and_what_it_answered():
    """The kernel never saw the words: it must not read the next line as following nothing."""
    session, emotes = FakeSession(), []
    run(gesturing(session, FakeExecutor(), emotes), [heard("Thank you."), SleepFrame(0.05)])
    assert acts(session) == [{"kind": "acted", "origin": "reflex", "what": "body_plan",
                              "status": "done", "reason": "heard: Thank you.",
                              "input": {"steps": [{"skill": "emote", "arg": emotes[0][0]}]}}]


def test_a_gesture_never_repeats_its_clip_and_a_quick_repeat_moves_nothing():
    session, emotes, clock = FakeSession(), [], [1000.0]
    processor = gesturing(session, FakeExecutor(), emotes, now=lambda: clock[0])

    async def main():
        frames = [frame for _ in range(6) for frame in (heard("Hi Sage."), SleepFrame(0.02))]
        original = processor._submit

        async def stepped(said, hold=True):
            await original(said, hold)
            clock[0] += 7.0  # past the cooldown, one turn on
        processor._submit = stepped
        await run_test(processor, frames_to_send=frames)
    asyncio.run(main())
    clips = [clip for clip, _ in emotes]
    assert len(clips) == 6 and all(a != b for a, b in pairwise(clips))
    assert session.started == []

    session, emotes = FakeSession(), []
    run(gesturing(session, FakeExecutor(), emotes, now=lambda: 1000.0),
        [heard("Thank you."), SleepFrame(0.02), heard("Thank you."), SleepFrame(0.02)])
    assert len(emotes) == 1 and session.started == []  # swallowed: no clip, no turn


def test_no_gesture_while_the_body_is_busy_or_a_voice_is_speaking_or_a_reply_is_coming():
    executor = FakeExecutor()
    executor.reflex_block = "dancing"
    session, emotes = FakeSession(), []
    run(gesturing(session, executor, emotes), [heard("Good girl."), SleepFrame(0.02)])
    assert emotes == [] and [t for _, t, _ in session.started] == ["Good girl."]

    playback = Playback()
    playback.speaking = True
    session, emotes = FakeSession(), []
    run(gesturing(session, FakeExecutor(), emotes, playback=playback),
        [heard("Thank you."), SleepFrame(0.02)])
    assert emotes == [] and [t for _, t, _ in session.started] == ["Thank you."]

    session, emotes = FakeSession(), []  # the kernel is on it: the words join that turn
    run(gesturing(session, FakeExecutor(), emotes),
        [heard("Tell me a story."), SleepFrame(0.02), heard("Thank you."), SleepFrame(0.02)])
    assert emotes == []
    assert [t for _, t, _ in session.started][-1] == "Tell me a story. Thank you."


def test_commands_and_longer_lines_never_gesture():
    session, executor, emotes = FakeSession(), FakeExecutor(), []
    run(gesturing(session, executor, emotes), [heard("Hi Sage, stop."), SleepFrame(0.02)])
    assert executor.stops == 1 and emotes == []
    session, emotes = FakeSession(), []
    run(gesturing(session, FakeExecutor(), emotes),
        [heard("Hi, how was your day?"), SleepFrame(0.02)])
    assert emotes == [] and len(session.started) == 1


# --- a move for an answer (sage_device.choice) ----------------------------------------------

STATEMENT = "The sun is out today."


class Heads:
    """A chooser that always draws a move."""

    @staticmethod
    def random():
        return 0.0


def choosing(emotes, features=("acted",), executor=None, **kwargs):
    session = FakeSession()
    session.features = frozenset(features)
    return session, gesturing(session, executor or FakeExecutor(), emotes, rng=Heads, **kwargs)


def lately(*frames):
    """It spoke a moment ago, then ``frames``: the owner is not waiting on words."""
    return [BotStartedSpeakingFrame(), SleepFrame(0.01), *frames]


def test_a_statement_can_be_answered_by_a_muted_move_with_no_kernel_turn_and_no_words():
    emotes, executor = [], FakeExecutor()
    session, processor = choosing(emotes, executor=executor)
    down, _ = run(processor, lately(heard(STATEMENT), SleepFrame(0.05)))
    assert session.started == [] and spoken(down) == []
    ((clip, sound),) = emotes
    assert clip in REACTIONS["agree"] and sound is False
    assert "think" not in executor.log  # no reply is coming, so no thought to show


def test_overheard_talk_gets_no_turn_no_words_no_move_and_the_kernel_is_never_told(caplog):
    emotes, executor = [], FakeExecutor()
    session, processor = choosing(emotes, executor=executor)
    with caplog.at_level("INFO", logger="reachy_sage_voice.sage"):
        down, _ = run(processor, [heard("And then the whole team came along."), SleepFrame(0.05)])
    assert session.started == [] and spoken(down) == [] and emotes == [] and acts(session) == []
    assert "think" not in executor.log
    assert "overheard: quiet" in caplog.text


def test_the_same_words_inside_an_exchange_are_not_overheard(caplog):
    session, processor = choosing([])
    with caplog.at_level("INFO", logger="reachy_sage_voice.sage"):
        run(processor, lately(heard("And then the whole team came along."), SleepFrame(0.05)))
    assert "overheard" not in caplog.text and "chose: move" in caplog.text


def test_a_cold_statement_may_get_a_move_after_a_long_quiet():
    emotes = []
    session, processor = choosing(emotes)
    processor._spoke_at = processor._now() - 100.0
    run(processor, [heard(STATEMENT), SleepFrame(0.05)])
    assert session.started == [] and len(emotes) == 1


def test_a_chosen_move_tells_the_kernel_the_words_and_says_none_itself():
    emotes = []
    session, processor = choosing(emotes)
    run(processor, lately(heard(STATEMENT), SleepFrame(0.05)))
    assert acts(session) == [{"kind": "acted", "origin": "reflex", "what": "body_plan",
                              "status": "done", "reason": f"heard: {STATEMENT}",
                              "input": {"steps": [{"skill": "emote", "arg": emotes[0][0]}]}}]


def test_the_kernel_hears_a_chosen_move_while_the_clip_plays_so_before_the_next_line():
    session, seen = FakeSession(), []
    session.features = frozenset(("acted",))

    async def play_emote(name, sound):
        await asyncio.sleep(0.02)
        seen.append(len(session.acts))
    processor = SageProcessor(session, play_emote, Playback(), executor=FakeExecutor(), rng=Heads)
    run(processor, lately(heard(STATEMENT), SleepFrame(0.05)))
    assert seen == [1]


def test_after_two_moves_in_a_row_it_speaks_and_words_begin_the_count_again():
    emotes, clock = [], [1000.0]
    session, processor = choosing(emotes, now=lambda: clock[0])
    session.polls.append({"turn": {"turn_id": TURN, "run_status": "completed"}})  # the third's

    async def main():
        original = processor._submit

        async def stepped(said, hold=True):
            await original(said, hold)
            clock[0] += 7.0  # past the cooldown, within the minute
        processor._submit = stepped
        await run_test(processor, frames_to_send=lately(
            *[frame for _ in range(3) for frame in (heard(STATEMENT), SleepFrame(0.05))],
            BotStartedSpeakingFrame(), SleepFrame(0.02), heard(STATEMENT), SleepFrame(0.02)))
    asyncio.run(main())
    assert len(emotes) == 3 and [t for _, t, _ in session.started] == [STATEMENT]


def test_a_gesture_counts_as_a_move_in_a_row():
    emotes, clock = [], [1000.0]
    session, processor = choosing(emotes, now=lambda: clock[0])

    async def main():
        original = processor._submit

        async def stepped(said, hold=True):
            await original(said, hold)
            clock[0] += 7.0
        processor._submit = stepped
        await run_test(processor, frames_to_send=lately(
            heard("Hi Sage."), SleepFrame(0.02), heard(STATEMENT), SleepFrame(0.02),
            heard(STATEMENT), SleepFrame(0.02)))
    asyncio.run(main())
    assert len(emotes) == 2 and [t for _, t, _ in session.started] == [STATEMENT]


def test_words_when_the_move_could_not_show_or_the_kernel_could_not_be_told():
    busy = FakeExecutor()
    busy.reflex_block = "dancing"
    speaking = Playback()
    speaking.speaking = True
    for kwargs in ({"executor": busy}, {"playback": speaking}, {"features": ()}):
        emotes = []
        session, processor = choosing(emotes, **kwargs)
        run(processor, lately(heard(STATEMENT), SleepFrame(0.02)))
        assert emotes == [] and [t for _, t, _ in session.started] == [STATEMENT], kwargs

    emotes = []  # it has not spoken yet: the owner hears it first
    session, processor = choosing(emotes)
    run(processor, [heard(STATEMENT), SleepFrame(0.02)])
    assert emotes == [] and len(session.started) == 1

    emotes = []  # no chooser: a body that never chooses
    session = FakeSession()
    session.features = frozenset(("acted",))
    run(gesturing(session, FakeExecutor(), emotes), lately(heard(STATEMENT), SleepFrame(0.02)))
    assert emotes == [] and len(session.started) == 1


def test_a_cooling_gesture_means_words_not_a_second_clip():
    emotes = []
    session, processor = choosing(emotes, now=lambda: 1000.0)
    run(processor, lately(heard("Hi Sage."), SleepFrame(0.02),
                          heard(STATEMENT), SleepFrame(0.02)))
    assert len(emotes) == 1 and [t for _, t, _ in session.started] == [STATEMENT]


def test_questions_and_distress_reach_the_kernel_on_a_body_that_chooses():
    for line in ("Is it raining.", "I can't breathe.", "My name is Lena."):
        emotes = []
        session, processor = choosing(emotes)
        run(processor, lately(heard(line), SleepFrame(0.02)))
        assert emotes == [] and [t for _, t, _ in session.started] == [line]


def test_a_reply_to_its_own_question_gets_words():
    emotes = []
    session, processor = choosing(emotes)
    processor._recent.append("What did you do today?")
    run(processor, lately(heard(STATEMENT), SleepFrame(0.02)))
    assert emotes == [] and [t for _, t, _ in session.started] == [STATEMENT]


def test_the_choice_is_logged_with_none_of_the_words(caplog):
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.sage"):
        run(choosing([])[1], lately(heard(STATEMENT), SleepFrame(0.02)))
        run(choosing([])[1], lately(heard("Is it raining."), SleepFrame(0.02)))
    logged = [r.getMessage() for r in caplog.records if r.getMessage().startswith("chose: ")]
    assert logged == ["chose: move", "chose: speak"]


def test_a_statement_that_could_not_be_a_move_says_why_not(caplog):
    """The log tells words forced by a body not free from words the odds chose (live 2026-11-07)."""
    executor = FakeExecutor()
    executor.reflex_block = "emote"
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.sage"):
        run(choosing([], executor=executor)[1], lately(heard(STATEMENT), SleepFrame(0.02)))
    assert "chose: speak (body not free)" in caplog.messages


def test_the_clips_for_a_chosen_move_are_declared_and_none_of_them_droops():
    """L4, L5: an answer to a statement or a goodbye never reads as hurt, bored or sent away."""
    droops = {"lonely1", "sad1", "sad2", "downcast1", "impatient1", "impatient2", "boredom1",
              "boredom2", "tired1", "no_sad1", "yes_sad1", "go_away1", "uncertain1"}
    for reaction in (choice.COMFORT, choice.AGREE):
        assert set(REACTIONS[reaction]) <= set(EMOTIONS)
        assert not set(REACTIONS[reaction]) & droops


def test_a_clip_is_commanded_as_the_transcript_lands_on_the_fake_clock(caplog):
    """Default 600 ms stop, 280 ms STT (p50, 622 logged turns): the clip goes at 880 ms."""
    times, played = iter([0.6]), []
    clock = lambda: next(times, 0.88)  # noqa: E731  the VAD stop reads 0.6, all else 0.88

    async def play_emote(name, sound):
        played.append(clock())
    processor = SageProcessor(FakeSession(), play_emote, Playback(), executor=FakeExecutor(),
                              now=clock)
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.sage"):
        run(processor, [VADUserStoppedSpeakingFrame(stop_secs=0.6), SleepFrame(0.01),
                        heard("Good job."), SleepFrame(0.02)])
    assert played == [0.88]
    assert any("reflex lane): voice ended 880 ms ago" in r.getMessage() for r in caplog.records)


def test_a_turn_that_ends_without_audio_ends_the_thought():
    session = FakeSession([{"turn": {"turn_id": TURN, "run_status": "failed"}}])
    executor = FakeExecutor()
    run(make(session, executor), [heard("Tell me a story."), SleepFrame(0.05)])
    assert executor.ended_thought
