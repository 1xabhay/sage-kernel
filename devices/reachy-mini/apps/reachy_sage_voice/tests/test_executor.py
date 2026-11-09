"""The executor, the head's one owner: plans in order, a new plan or stop cuts the old one."""

import asyncio
import itertools
import logging
import math
import os
import random
import threading
import time
from dataclasses import replace

import numpy as np
import pytest
from fake_dance_mini import click_track
from fake_mini import FakeMini, head_yaw
from reachy_mini.utils import create_head_pose
from scipy.spatial.transform import Rotation

from reachy_sage_voice import dance
from reachy_sage_voice import executor as executors
from reachy_sage_voice.behaviour.search import Search
from reachy_sage_voice.executor import (
    H_FOV,
    SEARCH_WEIGHT,
    V_FOV,
    Executor,
    HeadBusyError,
    Result,
    SearchTiming,
    daemon_detector,
    daemon_doa,
    daemon_face,
)
from reachy_sage_voice.limits import LIMITS
from reachy_sage_voice.motion import alive as alive_motion
from reachy_sage_voice.motion import arbiter, runner
from reachy_sage_voice.motion.arbiter import Sample
from reachy_sage_voice.perception.face import Face
from reachy_sage_voice.plan import parse

pytestmark = pytest.mark.usefixtures("arbiter")  # every test with the arbiter off and on

RIGHT, LEFT = LIMITS["yaw"]  # the furthest the head looks each way


def plan(*steps):
    result = parse({"steps": list(steps)})
    assert not isinstance(result, str), result
    return result


def look(arg):
    return {"skill": "look", "arg": arg}


def turn(arg):
    return {"skill": "turn", "arg": arg}


TRACK, STOP_TRACKING = {"skill": "track_face"}, {"skill": "stop_tracking"}
DANCE = {"skill": "dance"}


#: The search's timings, shrunk for tests: same steps, milliseconds instead of seconds.
FAST = SearchTiming(settle_s=0.0, dwell_s=0.02, poll_s=0.005, confirm_s=0.02)
#: The planner at the live pace (4.1 s a view, 04 A1), a hundredth of the time: 41 ms a view.
LIVE = Search(pace_s=0.041, dwell_s=0.0075, call_out_s=0.15, listen_s=0.03, cap_s=0.6)
SCALE = LIVE.pace_s / Search().pace_s
#: LIVE with the live 60 s cap, out of reach: a test of the path, not the cap. The fake's views
#: cost more than LIVE's 41 ms pace (segments, runner ticks, polls), more under load, so 0.6 s
#: of wall clock cut short paths that live, at 4.1 s a view, fit the cap many times over.
UNCAPPED = replace(LIVE, cap_s=Search().cap_s)


def make(mini, **kwargs):
    kwargs.setdefault("timing", FAST)
    if callable(face := kwargs.get("face")):  # a scripted bool: a fresh reading of it
        kwargs["face"] = lambda: None if (seen := face()) is None else Face(seen, True, None, 0.0)
    executor = Executor(mini, segment_s=kwargs.pop("segment_s", 0.02), **kwargs)
    if isinstance(player := kwargs.get("play_move"), Player):
        executor.clip = player.clip  # as __main__ sets it
    executor.search, executor.hold_max_s = LIVE, 8.0 * SCALE
    mini.gone, goto = [], executor._goto

    async def recorded(*args, **target):
        mini.gone.append((time.monotonic(), target.get("head"), target.get("body_yaw")))
        return await goto(*args, **target)
    executor._goto = recorded
    return executor


def calls(mini):
    """Return the robot calls in order, a run of motion commands (one move) as "move"."""
    out = []
    for c in mini.calls:
        call = "move" if c[0] in ("goto_target", "set_target") else c
        if not (call == "move" and out and out[-1] == "move"):
            out.append(call)
    return out


def landed(mini):
    """Return the last motion command's (head, body_yaw, antennas): where the body was sent."""
    return mini.moves()[-1][1:]


def views(mini):
    """Return each move's (time, head, body_yaw) as asked.

    Its goto_target, or with the arbiter the target given to ``_goto`` (``make`` records it),
    which the runner eases to.
    """
    if os.environ.get("SAGE_VOICE_ARBITER") == "0":
        return [(c[1], c[3], c[2]) for c in mini.named("goto_target")]
    return mini.gone


def weights(mini):
    return [c[1] for c in mini.named("start_head_tracking")]


# --- C3: plans in order; a new plan interrupts the old -------------------------------

def test_stop_tracking_leaves_the_head_where_the_tracker_had_it():
    mini = FakeMini()
    mini.weight, mini.aim, mini.head = 1.0, create_head_pose(yaw=30), create_head_pose(yaw=30)
    mini.target = np.eye(4)  # the daemon's stored target: stale, while the tracker steered
    assert asyncio.run(make(mini, tracking=True).run(plan(STOP_TRACKING))).status == "done"
    assert mini.snaps and max(mini.snaps) < 1.0



def test_steps_run_in_order_and_a_look_keeps_the_body_where_it_is():
    mini = FakeMini()
    result = asyncio.run(make(mini).run(plan(turn(90), look("left"))))
    assert result.status == "done"
    moves = mini.moves()
    look_at = next(i for i, (_, head, yaw, _) in enumerate(moves)  # the head off the body
                   if yaw is None or abs(head_yaw(head) - yaw) > 1e-3)
    turned = [m[2] for m in moves[:look_at]]
    assert turned == sorted(turned) and len(turned) >= 3  # in order, in segments
    for _, _, yaw, _ in moves[look_at:]:  # the look moved the head only
        assert yaw is None or math.isclose(yaw, math.radians(90), abs_tol=1e-6)
    assert math.isclose(mini.yaw, math.radians(90), abs_tol=1e-6)


def test_a_turn_carries_the_head_so_the_body_is_never_dragged_back():
    mini = FakeMini()
    asyncio.run(make(mini).run(plan(turn(150))))
    assert mini.dragged == 0 and math.isclose(mini.yaw, math.radians(150))


def test_a_new_plan_reports_the_old_one_interrupted():
    async def main():
        executor = make(FakeMini(), segment_s=0.05)
        old = asyncio.ensure_future(executor.run(plan(turn(160))))
        await asyncio.sleep(0.08)
        new = await executor.run(plan(look("center")))
        return await old, new
    old, new = asyncio.run(main())
    assert old.status == "interrupted" and new.status == "done"


def test_a_turn_past_the_body_goes_to_the_limit_and_says_so():
    mini = FakeMini()
    mini.yaw = math.radians(150)
    result = asyncio.run(make(mini).run(plan(turn(90))))
    assert result.status == "done" and "out_of_range" in result.text
    assert math.isclose(mini.yaw, math.radians(158))


def test_no_target_goes_beyond_the_limits():
    """A5: the daemon silently drops a target past a limit, which reads as a freeze."""
    mini = FakeMini()
    executor = make(mini, face=lambda: False, doa=lambda: 0.0)
    for steps in ([turn(400)], [look("left"), look("up")], [turn(-400)], [look("me")],
                  [search()], [turn("around")]):
        asyncio.run(executor.run(plan(*steps)))
    mini.yaw, mini.head = math.radians(158), create_head_pose(yaw=175, pitch=-50)
    asyncio.run(executor.run(plan({"skill": "stop"})))  # it freezes where it reads it is
    assert mini.moves() and mini.beyond == []


def test_a_turn_already_at_the_limit_fails_out_of_range():
    mini = FakeMini()
    mini.yaw = math.radians(158)
    result = asyncio.run(make(mini).run(plan(turn("around"))))
    assert (result.status, result.reason) == ("failed", "out_of_range")
    assert mini.named("goto_target") == []


def test_a_plan_cut_to_the_limit_says_so_too():
    result = asyncio.run(make(FakeMini()).run(plan(turn(180))))
    assert result.status == "done" and "out_of_range" in result.text


def test_tracking_is_done_once_it_starts_and_a_look_pauses_it_then_gives_it_back():
    async def main():
        executor = make(mini)
        started = await executor.run(plan(TRACK))
        moved = await executor.run(plan(look("up")))
        return started, moved
    mini = FakeMini()
    started, moved = asyncio.run(main())
    assert started == Result("done", text="now tracking you")
    assert moved.status == "done"
    assert ("stop_head_tracking",) not in mini.calls  # tracking is sticky
    assert calls(mini) == [("start_head_tracking", 1.0), "move",  # handed back, then let go
                           ("start_head_tracking", 0.0), "move",
                           ("start_head_tracking", 1.0)]


def test_tracking_for_a_while_then_the_next_step():
    mini = FakeMini()
    result = asyncio.run(make(mini).run(plan(
        {"skill": "track_face", "until": "timeout", "timeout_s": 0.05}, look("center"))))
    assert result.status == "done"
    assert [c if c == "move" else c[0] for c in calls(mini)] == [
        "start_head_tracking", "move", "stop_head_tracking", "move"]  # handed back, let go


def test_listening_needs_a_listener():
    result = asyncio.run(make(FakeMini()).run(plan({"skill": "listen_song"})))
    assert (result.status, result.reason) == ("failed", "unsupported")


def test_listening_is_done_once_it_starts():
    heard = []

    async def listen():
        heard.append("song")
        await asyncio.sleep(10)

    async def main():
        result = await make(FakeMini(), listen=listen).run(plan({"skill": "listen_song"}))
        await asyncio.sleep(0)
        return result
    assert asyncio.run(main()) == Result("done", text="now listening")
    assert heard == ["song"]


def test_a_move_that_times_out_fails_the_plan_and_skips_the_rest():
    mini = FakeMini()

    def stuck(**kwargs):
        raise TimeoutError("task did not complete")
    mini.goto_target = mini.set_target = stuck  # a goto, or the motion runner's targets
    result = asyncio.run(make(mini).run(plan(look("left"), look("right"))))
    assert (result.status, result.reason) == ("failed", "timeout")


def test_a_move_the_robot_refuses_fails_the_plan_and_skips_the_rest():
    mini = FakeMini()

    def refused(**kwargs):
        raise ConnectionError("Lost connection with the server.")
    mini.goto_target = mini.set_target = refused  # the motion link down
    result = asyncio.run(make(mini).run(plan(look("left"), look("right"))))
    assert (result.status, result.reason, result.text) == ("failed", "error", "look failed")


# --- C4: stop wins over every motion but tracking -------------------------------------

def test_stop_mid_turn_freezes_where_the_body_is_within_a_second(monkeypatch):
    monkeypatch.setattr(arbiter, "MIN_MOVE_S", 0.4)  # the arbiter's move, as segment_s
    async def main():
        executor = make(mini, segment_s=0.4)
        running = asyncio.ensure_future(executor.run(plan(turn(160))))
        await asyncio.sleep(0.2)
        asked = time.monotonic()
        await executor.stop()
        return asked, await running
    mini = FakeMini()
    asked, result = asyncio.run(main())
    assert result.status == "interrupted"
    if not Executor(mini).arbiter:  # with it, the runner is the only writer: nothing to cancel
        assert ("cancel_move",) in mini.calls  # an SDK-side move (an emote)
        assert ("send_command", "StopMoveCmd") in mini.calls  # a daemon-run one, its sound
    *before, (at, head, yaw, _) = mini.moves()  # the freeze is the last command
    assert at - asked < 1.0
    assert 0 < yaw < math.radians(160) and math.isclose(yaw, mini.yaw)  # in place, not home
    assert head is mini.head
    assert before and all(m[0] < at for m in before)  # nothing moved after


def test_a_stop_while_a_move_reads_where_the_body_is_reports_the_plan_interrupted():
    class Slow(FakeMini):
        def get_current_head_pose(self):
            time.sleep(0.1)
            return super().get_current_head_pose()

    async def main():
        executor = make(mini, segment_s=0.3)
        running = asyncio.ensure_future(executor.run(plan(look("left"))))
        await asyncio.sleep(0.05)
        await executor.stop()
        return await running
    mini = Slow()
    assert asyncio.run(main()).status == "interrupted"


def test_stop_does_not_end_tracking():
    async def main():
        executor = make(mini)
        await executor.run(plan(TRACK))
        await executor.stop()
    mini = FakeMini()
    asyncio.run(main())
    assert ("stop_head_tracking",) not in mini.calls
    assert weights(mini)[-1] == 1.0


def test_stop_tracking_ends_it_and_goes_home():
    async def main():
        executor = make(mini)
        await executor.run(plan(TRACK))
        mini.yaw = math.radians(120)
        return await executor.run(plan(STOP_TRACKING))
    mini = FakeMini()
    result = asyncio.run(main())
    assert result == Result("done", text="stopped tracking")
    assert ("stop_head_tracking",) in mini.calls
    yaws = [m[2] for m in mini.moves()]
    assert len(yaws) >= 3 and yaws == sorted(yaws, reverse=True)  # home in segments, no swing back
    head, yaw, antennas = landed(mini)
    assert yaw == 0.0 and np.allclose(head, np.eye(4)) and antennas is not None
    assert mini.dragged == 0


def test_stop_ends_a_search_and_its_tracking():
    async def main():
        executor = make(mini, segment_s=0.05)
        running = asyncio.ensure_future(executor.run(plan(search(5))))
        while not weights(mini):  # the search's tracking is on (a fixed 0.1 s raced load)
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)  # mid-search, a move in flight
        await executor.stop()
        return await running
    mini = FakeMini()
    assert asyncio.run(main()).status == "interrupted"
    assert mini.calls[-2:][0] == ("stop_head_tracking",)


def test_with_presence_watched_a_search_that_ends_leaves_the_detector_watching():
    """Presence without following (SAGE_VOICE_WATCH): a search's or a track's end watches again.

    The detector goes back to the watch weight, which never moves the head, rather than off.
    """
    async def main():
        executor = make(mini, segment_s=0.05, watch=executors.WATCH_WEIGHT)
        running = asyncio.ensure_future(executor.run(plan(search(5))))
        while not weights(mini):
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)
        await executor.stop()
        await running
        await executor.run(plan(TRACK))
        await executor.run(plan(STOP_TRACKING))
    mini = FakeMini()
    asyncio.run(main())
    assert ("stop_head_tracking",) not in mini.calls
    assert weights(mini)[-1] == executors.WATCH_WEIGHT
    assert 0 < executors.WATCH_WEIGHT < executors.SEARCH_WEIGHT  # never steers (_steered)


def test_a_stop_step_freezes_too():
    mini = FakeMini()
    result = asyncio.run(make(mini).run(plan({"skill": "stop"})))
    assert result.status == "done" and mini.named("set_target")


# --- C5: search_face, voice first, stop and look --------------------------------

def search(timeout_s=None):
    return {"skill": "search_face"} | ({"timeout_s": timeout_s} if timeout_s else {})


def angle_off(a, b):
    return abs((a - b + 180) % 360 - 180)


def aim(head):
    """(yaw, elevation) of a head pose in degrees; elevation is up (the SDK's pitch is down)."""
    _, pitch, yaw = Rotation.from_matrix(np.asarray(head)[:3, :3]).as_euler("xyz", degrees=True)
    return yaw, -pitch


def face_at(mini, azimuth, elevation):
    """Place a face the camera sees once the head aims within the detection box of it."""
    def seen():
        yaw, elev = aim(mini.head)
        return angle_off(yaw, azimuth) <= H_FOV / 2 and abs(elev - elevation) <= V_FOV / 2
    return seen


def test_body_and_head_turn_the_same_way_so_their_angles_add():
    # Live 2026-10-23: the path came up short with body and head turning opposite ways.
    mini = FakeMini()
    asyncio.run(make(mini, face=lambda: False).run(plan(search())))
    gotos = views(mini)
    reached = [aim(c[1])[0] for c in gotos]
    assert max(reached) + H_FOV / 2 >= LEFT and min(reached) - H_FOV / 2 <= RIGHT  # all of it
    for _, head, body in gotos:
        body, yaw = math.degrees(body), aim(head)[0]
        relative = yaw - body
        assert abs(body) <= 158 + 1e-6 and RIGHT - 1e-6 <= yaw <= LEFT + 1e-6
        assert abs(relative) < 1e-6 or relative * body > 0  # never counter-rotating
    assert mini.dragged == 0


def test_with_no_voice_it_looks_straight_ahead_first():
    mini = FakeMini()
    asyncio.run(make(mini, face=lambda: False).run(plan(search())))
    first = views(mini)[0]
    assert aim(first[1]) == pytest.approx((0, -LIVE.pitch))


# The planner sees the reach, one tilt row: faces 20 degrees below level to 40 above.
@pytest.mark.parametrize("azimuth, elevation", [(0, -10), (120, -15), (-160, 40), (-60, 35),
                                                (170, 0), (-100, -15)])
def test_a_face_anywhere_is_found_and_locked_on(azimuth, elevation, caplog):
    mini = FakeMini()
    executor = make(mini, face=face_at(mini, azimuth, elevation))
    executor.search = UNCAPPED
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.executor"):
        result = asyncio.run(executor.run(plan(search())))
    assert result == Result("done", text="found you; now tracking you")
    views = sum(r.getMessage().startswith("search view") for r in caplog.records)
    live = Search()
    assert views * live.pace_s + live.listen_s <= live.cap_s  # in time live, a call-out too
    assert weights(mini)[0] == SEARCH_WEIGHT and weights(mini)[-1] == 1.0
    assert mini.dragged == 0


def test_a_face_seen_mid_path_stops_the_search_there():
    mini = FakeMini()
    seen = face_at(mini, 120, 30)  # high on the left: the second view
    asyncio.run(make(mini, face=seen).run(plan(search())))
    assert seen() and aim(mini.head)[1] == pytest.approx(-LIVE.pitch)  # stopped there
    assert len(mini.named("goto_target")) <= 5  # the third view, seen on the way


def test_a_glimpse_that_is_not_confirmed_does_not_end_the_search():
    mini = FakeMini()
    reads = iter([True] + [False] * 10_000)
    result = asyncio.run(make(mini, face=lambda: next(reads)).run(plan(search())))
    assert result.reason == "not_found"


def test_no_face_means_the_whole_band_then_home_then_not_found(caplog):
    mini = FakeMini()
    executor = make(mini, face=lambda: False)
    executor.search = UNCAPPED
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.executor"):
        result = asyncio.run(executor.run(plan(search())))
    assert result == Result("failed", "not_found", "no face in 5 views in 60 s")
    head, body, antennas = landed(mini)
    assert body == 0.0 and np.allclose(head, np.eye(4)) and antennas is not None  # default
    assert mini.calls[-1] == ("stop_head_tracking",) or ("stop_head_tracking",) in mini.calls
    views = [r.getMessage() for r in caplog.records if r.getMessage().startswith("search view")]
    assert [v.split("yaw ")[1] for v in views] == ["0", "70", "138", "-70", "-130"]
    assert "face read" in caplog.text and "search outcome: not_found" in caplog.text


def test_a_search_with_a_time_limit_gives_up_then():
    mini = FakeMini()
    result = asyncio.run(make(mini, face=lambda: False).run(plan(search(0.05))))
    assert (result.status, result.reason) == ("failed", "timeout")
    assert ("stop_head_tracking",) in mini.calls


def test_a_search_while_locked_on_that_finds_nothing_keeps_tracking():
    async def main():
        executor = make(mini, face=lambda: False)
        await executor.run(plan(TRACK))
        return await executor.run(plan(search()))
    mini = FakeMini()
    assert asyncio.run(main()).reason == "not_found"
    assert ("stop_head_tracking",) not in mini.calls and weights(mini)[-1] == 1.0


def test_speech_mid_search_holds_it_still_then_it_carries_on():
    reads, counts = [0], {}

    def speak():
        executor.hold()
        counts["held"] = len(mini.named("goto_target"))
        asyncio.get_running_loop().call_later(0.2, done_speaking)

    def done_speaking():
        counts["resumed"] = len(mini.named("goto_target"))
        executor.resume()

    def face():
        reads[0] += 1
        if reads[0] == 3:
            counts["loop"].call_soon_threadsafe(speak)
        return False

    async def main():
        counts["loop"] = asyncio.get_running_loop()
        return await executor.run(plan(search()))
    mini = FakeMini()
    executor = make(mini, face=face)
    executor.hold_max_s = 1.0  # longer than this sentence
    assert asyncio.run(main()).reason == "not_found"  # and the whole band still looked at
    assert counts["resumed"] - counts["held"] <= 1  # held: at most the segment in flight lands


def doa_at(bearing):
    """Return the daemon's DoA for a voice ``bearing`` degrees off the body (0 left, pi/2 front)."""
    return math.radians(90 - bearing)


def test_the_voice_is_looked_at_first():
    mini = FakeMini()
    executor = make(mini, face=lambda: False, doa=lambda: doa_at(60))
    asyncio.run(executor.run(plan(search())))
    assert aim(views(mini)[0][1])[0] == pytest.approx(60)


def test_the_owners_long_speech_pauses_the_search_at_most_8_s():
    async def main():
        running = asyncio.ensure_future(executor.run(plan(search())))
        await asyncio.sleep(0.03)
        executor.hold()  # and never a stop: a long sentence, or a stop the VAD missed
        return await asyncio.wait_for(running, 3)
    mini = FakeMini()
    executor = make(mini, face=lambda: False)
    assert executors.HOLD_MAX_S == 8.0  # owner, 2026-10-25
    assert asyncio.run(main()).reason == "not_found"
    starts = [c[0] for c in mini.moves()]  # a goto each, or the runner's ticks
    pause = max(b - a for a, b in itertools.pairwise(starts))
    resume = executors.IDLE_TICK_S if executor.arbiter else 0.0  # the runner wakes to move
    assert executor.hold_max_s - 0.01 <= pause <= executor.hold_max_s + 0.06 + resume


def test_it_gives_up_by_60_s_even_while_the_owner_keeps_talking(caplog):
    async def main():
        running = asyncio.ensure_future(executor.run(plan(search())))
        while not running.done():  # held time no longer extends the cap
            executor.hold()
            await asyncio.sleep(0.06)
            executor.resume()
            await asyncio.sleep(0.02)
        return await running
    mini = FakeMini()
    executor = make(mini, face=lambda: False)
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.executor"):
        result = asyncio.run(main())
    assert Search().cap_s == 60.0
    # By the search's own clock, the cap is when the last view may begin: held time counts.
    # How long the views in flight then take is the machine's pace, so that is not timed.
    logged = [(r.created, r.getMessage()) for r in caplog.records]
    began = next(t for t, m in logged if m.startswith("search: voice"))
    views = [t - began for t, m in logged if m.startswith("search view")]
    gave_up = next(t for t, m in logged if m.startswith("search outcome")) - began
    assert result.reason == "not_found" and views
    assert max(views) <= LIVE.cap_s - LIVE.pace_s + 0.01 <= gave_up + 0.01  # by the cap, not before


def test_it_calls_out_once_a_round_and_a_reply_is_looked_at_next(caplog):
    called, replied = [], []

    async def call_out():
        called.append(True)
        if len(called) == 1:  # the owner answers the first one, from 60 degrees right of the body
            replied.append(max(RIGHT, min(LEFT, round(math.degrees(mini.yaw)) - 60)))
            executor.hold()
            asyncio.get_running_loop().call_later(0.02, executor.resume)

    mini = FakeMini()
    executor = make(mini, face=lambda: False, doa=lambda: doa_at(-60) if replied else None)
    executor.call_out = call_out
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.executor"):
        assert asyncio.run(executor.run(plan(search()))).reason == "not_found"
    assert 1 <= len(called) <= 2  # one per round: the reply started the second
    logged = [r.getMessage() for r in caplog.records]
    after = logged[logged.index("search: calling out"):]
    assert next(m for m in after if m.startswith("search view")).endswith(f"yaw {replied[0]}")


def test_with_no_reply_it_calls_out_once_then_gives_up():
    called = []

    async def call_out():
        called.append(time.monotonic())

    mini = FakeMini()
    executor = make(mini, face=lambda: False)
    executor.call_out = call_out
    start = time.monotonic()
    assert asyncio.run(executor.run(plan(search()))).reason == "not_found"
    assert len(called) == 1 and called[0] - start <= LIVE.call_out_s + 5 * SCALE


def test_face_reads_come_fresh_from_the_daemon(http_server):
    """The live daemon (1.10.0) nests the reading: {"status": "ok", "face_target": {"detected": ...}}.
    Read flat, every read was False, so the search never found anyone (live, 2026-10-23)."""
    http_server.script("GET", "/api/media/tracking/face",
                       (200, {"status": "ok", "face_target": {"detected": True, "x": 0.1, "y": 0.0,
                                                              "roll": 0.0, "ts": 1.0}}),
                       (200, {"status": "ok", "face_target": {"detected": False, "x": None}}), (500, {}))
    read = daemon_face(http_server.url + "/api/media/tracking/face")
    seen, empty, failed = read(), read(), read()
    assert (seen.detected, seen.x, seen.ts) == (True, 0.1, 1.0)
    assert (empty.detected, empty.ts, failed) == (False, None, None)


def test_a_detector_refusal_is_a_warning_not_silence(http_server, caplog):
    # The SDK's websocket drops the daemon's reply; with no camera it refuses (abstract.py:682).
    http_server.script("POST", "/api/media/tracking/enable",
                       (200, {"status": "ok", "enabled": True}),
                       (200, {"status": "unavailable", "enabled": False}), (500, {}))
    enable = daemon_detector(http_server.url + "/api/media/tracking/enable")
    with caplog.at_level(logging.WARNING, logger="reachy_sage_voice.executor"):
        assert (enable(1.0), enable(0.5), enable(1.0)) == (True, False, None)
    assert http_server.requests[1]["body"] == {"weight": 0.5}
    refused = [r for r in caplog.records if "refused" in r.getMessage()]
    assert len(refused) == 1 and refused[0].levelno == logging.WARNING


def test_without_a_daemon_reading_the_sdks_status_is_used():
    mini = FakeMini(faces=[True, True])  # seen, and seen again: confirmed
    executor = make(mini, face=lambda: None)
    assert asyncio.run(executor.run(plan(search()))).status == "done"
    assert mini.face_checks == 2


def test_a_stale_face_reading_is_not_found():
    mini = FakeMini(faces=[True] * 10_000, age=5.0)  # the daemon's last face, 5 s old
    assert asyncio.run(make(mini).run(plan(search()))).reason == "not_found"




# --- C6: overlays borrow the head and give it back --------------------------------

def test_an_emote_during_tracking_gives_the_tracking_weight_back(monkeypatch):
    """Without the arbiter; with it the runner drops the weight for a Sample (test_runner.py)."""
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "0")
    played = []

    async def emote():
        played.append(weights(mini))

    async def main():
        executor = make(mini)
        await executor.run(plan(TRACK))
        await executor.overlay(emote)
    mini = FakeMini(faces=[True])
    asyncio.run(main())
    assert played == [[1.0, 0.0]]  # the head was free while the emote played
    assert mini.calls[-1] == ("start_head_tracking", 1.0)


def test_an_emote_while_no_face_is_seen_leaves_the_tracker_searching():
    async def main():
        executor = make(mini)
        await executor.run(plan(TRACK))
        await executor.overlay(lambda: None)
    mini = FakeMini(faces=[False])
    asyncio.run(main())
    assert weights(mini) == [1.0]  # pausing would block acquiring a face (Pollen's rule)


def test_an_emote_with_nothing_running_touches_no_tracking():
    mini = FakeMini()
    asyncio.run(make(mini).overlay(lambda: None))
    assert mini.calls == []


def test_stop_cuts_an_emote_and_tracking_goes_on():
    async def emote():
        await asyncio.sleep(10)

    async def main():
        executor = make(mini)
        await executor.run(plan(TRACK))
        playing = asyncio.ensure_future(executor.overlay(emote))
        await asyncio.sleep(0.05)
        await executor.stop()
        await asyncio.wait({playing}, timeout=1)
        return playing
    mini = FakeMini(faces=[True])
    playing = asyncio.run(main())
    assert playing.cancelled()
    assert (("cancel_move",) in mini.calls) != Executor(mini).arbiter
    assert weights(mini)[-1] == 1.0


# --- dance: the head is the dance's until it ends -------------------------------------

class Dancer:
    def __init__(self, ignores_event=False, lasts_s=10.0):
        self.calls, self.ignores_event, self.lasts_s = [], ignores_event, lasts_s
        self.ended = None

    async def __call__(self, mini, audio, stop_event, hold=None):
        self.calls.append((mini, audio(1.0), stop_event))
        self.hold = hold
        start = time.monotonic()
        try:
            while self.ignores_event or not stop_event.is_set():
                if time.monotonic() - start > self.lasts_s:
                    break
                await asyncio.sleep(0.01)
        finally:
            self.ended = time.monotonic()
        return Result("done", text="danced")


@pytest.fixture
def sdk_dancer():
    """Skip with the arbiter: it dances itself, so the injected dancer's contract is moot."""
    if os.environ.get("SAGE_VOICE_ARBITER") != "0":
        pytest.skip("the injected dancer; with the arbiter the runner dances")


def test_dancing_needs_a_dancer():
    result = asyncio.run(make(FakeMini()).run(plan(DANCE)))
    assert (result.status, result.reason) == ("failed", "unsupported")


def test_a_dance_is_done_once_it_starts_and_hears_the_mic(sdk_dancer):
    dancer, mini = Dancer(), FakeMini()

    async def main():
        result = await make(mini, dance=dancer, audio=lambda s: b"pcm").run(plan(DANCE))
        await asyncio.sleep(0.02)
        return result
    assert asyncio.run(main()) == Result("done", text="now dancing")
    ((given, pcm, _),) = dancer.calls
    assert given is mini and pcm == b"pcm"


def test_stop_ends_a_dance_within_a_second_even_one_that_ignores_its_event(sdk_dancer):
    for dancer in (Dancer(), Dancer(ignores_event=True)):
        async def main(dancer=dancer):
            executor = make(FakeMini(), dance=dancer)
            await executor.run(plan(DANCE))
            await asyncio.sleep(0.05)
            asked = time.monotonic()
            await executor.stop()
            await asyncio.sleep(0.1)
            return asked
        asked = asyncio.run(main())
        assert dancer.calls[0][2].is_set()
        assert dancer.ended is not None and dancer.ended - asked < 1.0


def test_no_emote_during_a_dance():
    async def main():
        executor = make(FakeMini(), dance=Dancer())
        await executor.run(plan(DANCE))
        try:
            await executor.overlay(lambda: None)
        except HeadBusyError:
            return "refused"
    assert asyncio.run(main()) == "refused"


def test_a_dance_that_ends_by_itself_goes_home():
    async def main():
        await make(mini, dance=Dancer(lasts_s=0.05)).run(plan(DANCE))
        await asyncio.sleep(0.3)
    mini = FakeMini()
    mini.yaw = math.radians(40)
    asyncio.run(main())
    assert landed(mini)[1] == 0.0


def test_a_dance_while_tracking_pauses_it_then_tracking_resumes():
    async def main():
        executor = make(mini, dance=Dancer(lasts_s=0.05))
        await executor.run(plan(TRACK))
        await executor.run(plan({"skill": "dance", "timeout_s": 0.05}))
        paused = weights(mini)[-1]
        await asyncio.sleep(0.3)
        return paused
    mini = FakeMini()
    assert asyncio.run(main()) == 0.0
    assert weights(mini)[-1] == 1.0 and mini.named("goto_target") == []


def test_a_new_plan_ends_the_dance(sdk_dancer):
    dancer = Dancer()

    async def main():
        executor = make(FakeMini(), dance=dancer)
        await executor.run(plan(DANCE))
        await executor.run(plan(look("center")))
        await asyncio.sleep(0.05)
    asyncio.run(main())
    assert dancer.calls[0][2].is_set() and dancer.ended is not None


# --- holding still while the person speaks (motors ruin the STT) ----------------------

def test_held_while_the_person_speaks_the_body_moves_no_more_then_carries_on():
    async def main():
        executor = make(mini, segment_s=0.05)
        executor.hold_max_s = 1.0  # longer than this sentence
        running = asyncio.ensure_future(executor.run(plan(turn(160))))
        await asyncio.sleep(0.07)
        executor.hold()
        await asyncio.sleep(0.06)  # the segment in flight lands
        held = len(mini.moves())
        await asyncio.sleep(0.3)
        still = len(mini.moves())
        executor.resume()
        return held, still, await running
    mini = FakeMini()
    held, still, result = asyncio.run(main())
    assert held == still  # no new targets while held
    assert result.status == "done" and math.isclose(mini.yaw, math.radians(158))  # where it was
    yaws = [m[2] for m in mini.moves()]
    assert yaws == sorted(yaws)  # it carried on from where it was held, nothing gone back over


def test_stop_still_wins_while_held():
    async def main():
        executor = make(mini, segment_s=0.05)
        running = asyncio.ensure_future(executor.run(plan(turn(160))))
        await asyncio.sleep(0.07)
        executor.hold()
        await executor.stop()
        return await asyncio.wait_for(running, 1)
    mini = FakeMini()
    assert asyncio.run(main()).status == "interrupted"
    assert mini.named("set_target")


def test_tracking_is_not_held():
    async def main():
        executor = make(mini)
        executor.hold()
        return await asyncio.wait_for(executor.run(plan(TRACK)), 1)
    mini = FakeMini()
    assert asyncio.run(main()).status == "done" and weights(mini) == [1.0]


def test_the_dance_is_handed_the_hold(sdk_dancer):
    dancer = Dancer()

    async def main():
        executor = make(FakeMini(), dance=dancer)
        await executor.run(plan(DANCE))
        await asyncio.sleep(0.02)
        executor.hold()
        held = dancer.hold.is_set()
        executor.resume()
        return held, dancer.hold.is_set()
    assert asyncio.run(main()) == (True, False)


# --- the dance as the motion runner's primary (SAGE_VOICE_ARBITER=1) -------------------

@pytest.fixture
def grooving(monkeypatch):
    """Dance with the arbiter, to a 120 BPM click track played on the wall clock."""
    if os.environ.get("SAGE_VOICE_ARBITER") == "0":
        pytest.skip("the arbiter's dance; off, the dance is the injected dancer's")
    monkeypatch.setenv("SAGE_VOICE_DANCE_LEAD_S", "0")
    return click_track(time.monotonic, 120)


def dance_targets(mini, after=-math.inf, before=math.inf):
    return [(t, head, body) for t, head, body, _ in mini.moves() if after < t < before]


def frozen(targets):
    return all(np.allclose(h, targets[0][1]) and math.isclose(b, targets[0][2])
               for _, h, b in targets)


def test_with_the_arbiter_a_dance_streams_its_moves_on_the_beat_within_the_limits(
        grooving, monkeypatch):
    heard = []

    def logged(name, phase, level):
        heard.append((time.monotonic(), phase))
        return offsets(name, phase, level)
    offsets = dance.offsets
    monkeypatch.setattr(dance, "offsets", logged)

    async def main():
        await make(mini, dance=dance.dance, audio=grooving).run(plan(DANCE))
        await asyncio.sleep(1.5)
    mini = FakeMini()
    asyncio.run(main())
    assert mini.named("goto_target") == [] and mini.beyond == []  # the runner's targets only
    started = heard[0][0]
    assert len(dance_targets(mini, started, started + 1.0)) >= 40  # 50 Hz
    beat_s, first = 0.5, grooving.beats(started, started + 1)[0]
    errors = [((phase - (t - first) / beat_s) + 0.5) % 1 - 0.5 for t, phase in heard]
    assert max(np.abs(errors)) * beat_s <= 0.06


def test_with_the_arbiter_the_person_speaking_pauses_the_dance_then_it_resumes(grooving):
    async def main():
        executor = make(mini, dance=dance.dance, audio=grooving)
        executor.hold_max_s = 0.6
        await executor.run(plan(DANCE))
        await asyncio.sleep(0.5)
        executor.hold()
        held = time.monotonic()
        await asyncio.sleep(0.9)  # past the cap: it dances on
        executor.resume()
        executor.hold()
        again = time.monotonic()
        await asyncio.sleep(0.3)
        executor.resume()
        await asyncio.sleep(0.3)
        return held, again
    mini = FakeMini()
    held, again = asyncio.run(main())
    assert frozen(dance_targets(mini, held + 0.05, held + 0.55))
    assert not frozen(dance_targets(mini, held + 0.65, held + 0.9))
    assert frozen(dance_targets(mini, again + 0.05, again + 0.3))
    assert not frozen(dance_targets(mini, again + 0.35))


def test_with_the_arbiter_stop_freezes_the_dance_within_a_tenth_of_a_second(grooving):
    async def main():
        executor = make(mini, dance=dance.dance, audio=grooving)
        await executor.run(plan(DANCE))
        await asyncio.sleep(0.5)
        asked = time.monotonic()
        await executor.stop()
        await asyncio.sleep(0.5)
        return asked
    mini = FakeMini()
    asked = asyncio.run(main())
    assert len(dance_targets(mini, asked - 0.3, asked)) >= 10  # it was dancing
    after = dance_targets(mini, asked + 0.1)
    assert frozen(dance_targets(mini)[-len(after) - 1:])  # nothing moves after the first 0.1 s
    assert mini.named("goto_target") == []


def test_with_the_arbiter_a_dance_that_ends_goes_home(grooving):
    async def main():
        await make(mini, dance=dance.dance, audio=grooving).run(
            plan({"skill": "dance", "timeout_s": 0.8}))
        await asyncio.sleep(1.2)
    mini = FakeMini()
    asyncio.run(main())
    _, head, body = dance_targets(mini)[-1]
    assert mini.named("goto_target") == []  # home through the runner
    assert np.allclose(head, np.eye(4), atol=1e-6) and math.isclose(body, 0.0, abs_tol=1e-9)


# --- W4: the voice's direction from the robot daemon ---------------------------------

def test_the_direction_of_arrival_is_read_from_the_daemon(http_server):
    http_server.script("GET", "/api/state/doa", (200, {"angle": 1.2, "speech_detected": True}),
                       (200, None), (200, {"angle": 2.27, "speech_detected": False}),
                       (200, None), (200, None), (500, {}))
    read = daemon_doa(http_server.url + "/api/state/doa", retry_s=0.01)
    assert read() == 1.2
    assert read() == 2.27  # the first read after idle is null: read once more
    assert read() is None  # still nothing
    assert read() is None  # a daemon error is no direction, not a crash
    assert daemon_doa("http://127.0.0.1:9/api/state/doa", timeout_s=0.2, retry_s=0)() is None


# --- aliveness: breathing, a glance toward the voice, a thinking cue -------------------

@pytest.fixture
def quick(monkeypatch):
    """Aliveness timings shrunk to test speed; the shapes stay the robot's."""
    for name, value in {"IDLE_AFTER_S": 0.05, "IDLE_TICK_S": 0.005, "IDLE_EASE_S": 0.05,
                        "THINK_S": 0.05, "CUE_MOVE_S": 0.01,
                        "MOTION_DEAD_S": 0.2}.items():
        monkeypatch.setattr(executors, name, value)
    monkeypatch.setattr(runner, "PROBE_S", 0.01)  # the runner's probe of a link that is down


def alive(mini, **kwargs):
    kwargs.setdefault("idle", True)
    kwargs.setdefault("cues", True)
    return make(mini, **kwargs)


def voice_at(angle, speech_detected=True):
    """Return a daemon_doa: the direction ``angle``, its reading of whether that was speech."""
    def read():
        return angle
    read.speech_detected = speech_detected
    return read


async def until(done, bound_s=10.0):
    """Wait for ``done()``, however loaded the machine; past the bound the test fails."""
    async def poll():
        while not done():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(poll(), bound_s)


def breaths(mini, after=0.0):
    return [c for c in mini.named("set_target") if c[1] >= after]


async def breathing(executor, seconds):
    task = asyncio.ensure_future(executor.breathe())
    await asyncio.sleep(seconds)
    return task


def fast(factor=40.0):
    """Return a clock ``factor`` times the wall's: seconds of idle motion in a moment."""
    start = time.monotonic()
    return lambda: start + (time.monotonic() - start) * factor


def cue_moves(mini):
    """Return each cue move as (start time, body_yaw, head, antennas); body_yaw None: kept.

    A goto_target; with the arbiter, the streamed targets from one rest to the next: at rest
    the next target is the same (no offsets for ``alive.FADE_AFTER_S`` after a move), or the
    runner yielded.
    """
    if os.environ.get("SAGE_VOICE_ARBITER") == "0":
        return [c[1:] for c in mini.named("goto_target")]
    sent = mini.named("set_target")

    def rests(i):
        if i + 1 == len(sent) or sent[i + 1][1] - sent[i][1] > 2 * runner.DT:
            return True
        return np.array_equal(sent[i][2], sent[i + 1][2]) and sent[i][4] == sent[i + 1][4]
    out, start = [], None
    for i, (_, _, head, body, antennas) in enumerate(sent):
        if (i == 0 or rests(i - 1)) and not rests(i):
            start = sent[i][1]  # a move's first target
        if start is not None and rests(i):
            out.append((start, None if body == sent[0][3] else body, head, antennas))
            start = None
    return out


def euler(head):
    """Roll, pitch, yaw in degrees of a head pose (xyz, as create_head_pose builds it)."""
    return Rotation.from_matrix(head[:3, :3]).as_euler("xyz", degrees=True)


def test_idle_motion_is_organic_head_turns_rises_and_antennas_move_apart(quick):
    async def main():
        task = await breathing(alive(mini, clock=fast()), 1.0)
        task.cancel()
    mini = FakeMini()
    asyncio.run(main())
    sent = breaths(mini)
    assert len(sent) > 10
    angles = np.array([euler(c[2]) for c in sent])
    heights = np.array([c[2][2, 3] for c in sent])
    left, right = np.array([c[4][0] for c in sent]), np.array([c[4][1] for c in sent])
    assert (np.ptp(angles, axis=0) > 1.0).all()  # roll, pitch and yaw all wander
    assert np.ptp(heights) > 0.004  # a visible breath, millimetres
    assert np.ptp(left) > math.radians(10) and np.ptp(right) > math.radians(10)
    assert abs(heights[0]) < 0.002  # it eases in from where it was


def test_idle_motion_sways_the_body_and_the_head_follows_it_the_same_way(quick):
    async def main():
        task = await breathing(alive(mini, clock=fast()), 1.0)
        task.cancel()
    mini = FakeMini()
    asyncio.run(main())
    sent = breaths(mini)
    body = np.degrees([c[3] for c in sent])
    relative = np.array([math.degrees(head_yaw(c[2])) for c in sent]) - body
    assert np.ptp(body) > 4  # the body sways: no 2D poster (owner, 2026-10-23)
    assert (relative * body >= -1e-6).all()  # never counter-rotating


def test_idle_motion_is_visible_within_its_limits_and_the_body_never_counter_rotates():
    # Owner, 2026-10-23: "a pet that stands still and only moves its antennas looks like a 2D
    # poster". Head yaw about 12 degrees, pitch 6, roll 4, the body 8, all within the limits.
    motion = executors.IdleMotion(random.Random(3))
    a = np.array([motion.at(t) for t in np.arange(0, 600, 0.05)])
    roll, pitch, yaw, _, _, _, body = np.abs(a).max(axis=0)
    assert roll >= 3 and pitch >= 4.5 and yaw >= 9 and body >= 6
    assert (np.abs(a) <= np.array(executors.IDLE_LIMITS) + 1e-9).all()
    assert (a[:, 2] * a[:, 6] >= -1e-9).all()  # head and body turn the same way
    assert executors.IDLE_ACTION_EVERY_S == (4.0, 10.0)


def test_one_setting_scales_every_idle_amplitude(monkeypatch):
    def peaks(scale):
        motion = executors.IdleMotion(random.Random(5), scale)
        return np.abs([motion.at(t) for t in np.arange(0, 120, 0.05)]).max(axis=0)
    assert not peaks(0.0).any()
    assert peaks(0.5) == pytest.approx(peaks(1.0) / 2, rel=0.25)
    monkeypatch.setenv("SAGE_VOICE_IDLE_SCALE", "0.5")
    assert make(FakeMini()).motion.scale == 0.5
    for bad in ("loud", "-1", "nan"):
        monkeypatch.setenv("SAGE_VOICE_IDLE_SCALE", bad)
        assert make(FakeMini()).motion.scale == 1.0
    monkeypatch.delenv("SAGE_VOICE_IDLE_SCALE")
    assert make(FakeMini()).motion.scale == 1.0


def test_idle_motion_logs_its_start_stop_and_actions(quick, caplog):
    async def main():
        executor = alive(mini, clock=fast())
        task = await breathing(executor, 0.6)
        await executor.run(plan(look("left")))
        await asyncio.sleep(0.05)
        task.cancel()
    mini = FakeMini()
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.executor"):
        asyncio.run(main())
    said = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    if executors.Executor(mini).arbiter:  # one runner: the plan's move is its primary, no stop
        assert any(m.startswith("motion runner started") for m in said)
        return
    assert any(m.startswith("idle motion started") for m in said)
    assert any(m.startswith("idle motion stopped: ") and "plan" in m for m in said)
    assert any(m.startswith("idle action: ") for m in said)


def test_idle_motion_is_not_a_metronome_and_stays_within_limits():
    one, two = executors.IdleMotion(random.Random(1)), executors.IdleMotion(random.Random(2))
    times = np.arange(0, 600, 0.05)
    a = np.array([one.at(t) for t in times])
    b = np.array([two.at(t) for t in times])
    assert not np.allclose(a, b)  # two robots, two ways of being still
    limits = np.array(executors.IDLE_LIMITS)
    assert (np.abs(a) <= limits + 1e-9).all() and (np.abs(b) <= limits + 1e-9).all()
    steps = np.abs(np.diff(a, axis=0)).max(axis=0)
    assert (steps <= limits * 0.2).all()  # smooth: no jumps between 50 ms ticks


def test_idle_actions_come_at_irregular_intervals_and_vary():
    motion = executors.IdleMotion(random.Random(7))
    for t in np.arange(0, 900, 0.5):
        motion.at(t)
    starts = [start for start, _ in motion.history]
    kinds = {kind for _, kind in motion.history}
    gaps = np.diff(starts)
    low, high = executors.IDLE_ACTION_EVERY_S
    assert len(starts) >= 900 / high and (gaps >= low - 0.5).all() and (gaps <= high + 0.5).all()
    assert np.std(gaps) > 1.0  # irregular
    assert kinds == set(executors.IDLE_ACTIONS)


def test_a_plan_takes_over_at_once_and_idle_motion_eases_back_in_after(quick):
    async def main():
        executor = alive(mini)
        task = await breathing(executor, 0.2)
        await executor.run(plan(look("left")))
        ended = time.monotonic()
        await asyncio.sleep(0.6)
        task.cancel()
        return ended
    mini = FakeMini()
    ended = asyncio.run(main())
    moves = mini.moves()
    looked = [m for m in moves if m[0] < ended][-1][1]
    assert math.isclose(math.degrees(head_yaw(looked)), 30, abs_tol=1e-6)  # no idle mid-plan
    later = [m for m in moves if m[0] >= ended and not np.allclose(looked, m[1], rtol=0, atol=1e-9)]
    assert later and later[0][0] - ended >= executors.IDLE_AFTER_S * 0.8  # eases back in after
    reach = executors.IDLE_LIMITS[2] + executors.IDLE_LIMITS[6]  # the head's own, the body's
    assert abs(math.degrees(head_yaw(later[-1][1])) - 30) < reach  # around where it looks


def test_no_idle_motion_while_tracking_held_or_after_a_stop_until_spoken_to(quick):
    async def main():
        tracked = alive(FakeMini(), tracking=True)
        task = await breathing(tracked, 0.2)
        task.cancel()
        held = alive(mini)
        held.hold()
        task = await breathing(held, 0.2)
        none_held = len(breaths(mini))
        held.resume()
        await asyncio.sleep(0.15)
        await held.stop()
        stopped = time.monotonic()
        await asyncio.sleep(0.2)
        none_stopped = len(breaths(mini, stopped + 0.01))
        held.hold()
        held.resume()  # spoken to again
        await asyncio.sleep(0.2)
        task.cancel()
        return tracked.mini, none_held, none_stopped, stopped
    mini = FakeMini()
    tracked_mini, none_held, none_stopped, stopped = asyncio.run(main())
    # With the arbiter the runner breathes under tracking (test_runner.py).
    assert (breaths(tracked_mini) == []) != Executor(tracked_mini).arbiter
    assert none_held == 0 and none_stopped == 0
    assert breaths(mini, stopped + 0.2)  # alive again


def test_idle_off_never_moves(quick):
    async def main():
        task = await breathing(alive(mini, idle=False), 0.2)
        task.cancel()
    mini = FakeMini()
    asyncio.run(main())
    assert mini.calls == []


class Stalling(FakeMini):
    """The SDK's liveness flag flapping: set_target raises while ``down`` (live 2026-10-23)."""

    down = False

    def set_target(self, head=None, antennas=None, body_yaw=None):
        if self.down:
            raise ConnectionError("Lost connection with the server.")
        super().set_target(head, antennas, body_yaw)


def test_a_motion_link_stall_pauses_idle_motion_without_restarting_it_and_logs_once(
        quick, caplog):
    lost = []

    async def main():
        executor = alive(mini)
        executor.link_lost = lost.append
        task = await breathing(executor, 0.15)
        mini.down = True
        await asyncio.sleep(0.1)  # a stall shorter than MOTION_DEAD_S
        mini.down = False
        back = time.monotonic()
        await asyncio.sleep(0.1)
        task.cancel()
        return back
    mini = Stalling()
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.executor"):
        back = asyncio.run(main())
    stalled = [r for r in caplog.records if "motion link" in r.getMessage()]
    assert [r.levelno for r in stalled] == [logging.WARNING, logging.INFO]  # down once, back once
    assert breaths(mini, back)  # carries on
    assert lost == []


def test_a_dead_motion_link_is_reported_once_and_shown(quick):
    shown, lost = [], []

    async def main():
        executor = alive(mini)
        executor.link_lost = lost.append
        executor.on_link = lambda up, stalls: shown.append(up)
        task = await breathing(executor, 0.5)
        task.cancel()
    mini = Stalling()
    mini.down = True
    asyncio.run(main())
    assert len(lost) == 1 and "motion link" in lost[0]
    assert shown[:1] == [False]


class Refusing(Stalling):
    """A stalled link refusing the tracker's weight too."""

    def start_head_tracking(self, weight=1.0):
        if self.down:
            raise ConnectionError("Lost connection with the server.")
        super().start_head_tracking(weight)


def test_a_weight_refused_while_the_link_was_down_is_sent_again_once_it_is_back():
    # The cached weight skipped the re-send: the daemon stayed at 0, its detector off (LAYERS F1).
    async def main():
        executor = make(mini, tracking=True)
        await executor._weight(0.0)  # a plan paused the tracker
        mini.down = True
        with pytest.raises(ConnectionError):
            await executor._weight(1.0)  # and gave it back over a stalled link
        mini.down = False
        await executor.stop()  # the link is back
        await asyncio.sleep(0.05)
    mini = Refusing()
    asyncio.run(main())
    assert weights(mini)[-1] == 1.0


def test_the_glance_logs_the_direction_heard_and_the_yaw_chosen(caplog):
    def read():
        return math.radians(60)
    read.speech_detected = True

    async def main():
        executor = alive(FakeMini(), doa=read)
        executor.attend()
        await asyncio.sleep(0.1)
        executor.attend()  # within the cooldown
        await asyncio.sleep(0.05)
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.executor"):
        asyncio.run(main())
    said = [r.getMessage() for r in caplog.records]
    assert any("DoA 60" in m and "speech_detected=True" in m and "+30" in m for m in said)
    assert any("glance skipped" in m and "cooldown" in m for m in said)


def test_a_glance_while_tracking_logs_why_it_was_skipped(caplog):
    async def main():
        alive(FakeMini(), doa=lambda: 1.0, tracking=True).attend()
        await asyncio.sleep(0.05)
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.executor"):
        asyncio.run(main())
    assert any("glance skipped" in r.getMessage() and "tracking" in r.getMessage()
               for r in caplog.records)


def test_the_thinking_cue_is_visible_moves_the_antennas_and_logs(quick, caplog):
    mini = FakeMini()

    async def main():
        executor = alive(mini)
        executor.think()
        await executor._cue  # the first cue there and back (a fixed 0.2 s raced load)
        executor.think()
        executor.replying()  # before it fires
        await asyncio.sleep(0.05)
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.executor"):
        asyncio.run(main())
    away, back = cue_moves(mini)
    _, pitch, yaw = euler(away[2])
    assert max(abs(yaw), abs(pitch)) >= 5  # past the idle's "2D poster" (owner, 2026-10-23)
    assert away[3] is not None and back[3] is not None  # the antennas too, and back
    assert not np.allclose(away[3], back[3])
    said = [r.getMessage() for r in caplog.records]
    assert any("thinking cue" in m and "fired" in m for m in said)
    assert any("thinking cue" in m and "cancelled" in m for m in said)


def test_the_thinking_cue_fires_at_once_it_is_late_enough_already(caplog):
    async def main():
        alive(FakeMini()).think()  # the transcript took its time: no more on top
        await asyncio.sleep(0.08)
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.executor"):
        asyncio.run(main())
    assert any("thinking cue fired" in r.getMessage() for r in caplog.records)


@pytest.mark.slew
def test_a_thinking_cue_keeps_its_curve_under_the_acceleration_cap():
    """Min-jerk peaks at 5.77 d / T^2: past AMAX the slew cuts it, and it reads as a flick."""
    move = arbiter.goto({"yaw": 0.0, "pitch": 0.0},
                        {"yaw": executors.THINK_YAW, "pitch": executors.THINK_PITCH}, 0.0)
    assert all(5.77 * abs(d) / move.duration ** 2 <= arbiter.AMAX[k] for k, d in move.end.items())


def test_no_thinking_cue_right_after_a_move_nor_while_they_speak_again(quick, caplog):
    mini = FakeMini()

    async def main():
        executor = alive(mini)
        executor._moved_at = executor.clock()  # a glance just landed
        executor.think()
        await asyncio.sleep(0.05)
        executor._moved_at = -math.inf
        executor.hold()  # the transcript came after they started again
        executor.think()
        await asyncio.sleep(0.05)
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.executor"):
        asyncio.run(main())
    assert cue_moves(mini) == []
    said = [r.getMessage() for r in caplog.records if "thinking cue skipped" in r.getMessage()]
    assert "cooldown" in said[0] and "speaking" in said[1]


# --- recorded moves by voice --------------------------------------------------------

class Clip:
    """A recorded move as the SDK's: a 20 degree nod over ``duration`` s, antennas apart."""

    def __init__(self, duration):
        self.duration = duration

    def evaluate(self, s):
        assert 0 <= s < self.duration, "the SDK raises past the end"
        u = math.sin(math.pi * s / self.duration)
        return create_head_pose(pitch=20 * u), np.radians([30 * u, -30 * u]), 0.0


class Player:
    def __init__(self, lasts_s=0.1):
        self.played, self.lasts_s, self.sampled = [], lasts_s, False

    async def __call__(self, kind, name):
        self.played.append((kind, name))
        await asyncio.sleep(self.lasts_s)

    async def clip(self, kind, name, sound=True):
        """Return the clip itself, for the runner to sample (the arbiter's way)."""
        self.played.append((kind, name))
        self.sampled = True
        return Clip(self.lasts_s)


class Raised(Clip):
    """A recorded emote as Pollen's are: the head 20 mm up, with a 5 mm bob, past the z cap."""

    def evaluate(self, s):
        head, antennas, body = super().evaluate(s)
        head[2, 3] = 0.020 - 0.005 * math.sin(math.pi * s / self.duration)
        return head, antennas, body


def test_with_the_arbiter_a_raised_emote_plays_inside_the_limits_with_its_bob(quick):
    player = Player(lasts_s=0.4)
    player.clip = lambda kind, name, sound=True: asyncio.sleep(0, Raised(player.lasts_s))

    async def main():
        executor = alive(mini, play_move=player)
        started = time.monotonic()
        result = await executor.run(device({"skill": "emote", "arg": "happy"}))
        return executor.arbiter, result, started, time.monotonic()
    mini = FakeMini()
    arbiter, result, started, ended = asyncio.run(main())
    if not arbiter:
        return
    assert result.status == "done"
    zs = [head[2, 3] for t, head, _, _ in mini.moves() if started + 0.15 <= t <= ended]
    assert max(zs) <= LIMITS["z"][1] + 1e-9 and mini.beyond == []
    assert max(zs) - min(zs) >= 0.003  # the bob is still there: not flattened at the cap


def device(*steps):
    result = parse({"steps": list(steps)}, device=True)
    assert not isinstance(result, str), result
    return result


def test_an_emotion_by_voice_plays_a_recorded_emotion():
    player = Player()
    result = asyncio.run(make(FakeMini(), play_move=player).run(
        device({"skill": "emote", "arg": "happy"})))
    assert result.status == "done"
    ((kind, name),) = player.played
    assert kind == "emotion" and name in ("laughing1", "laughing2")


def test_a_dance_move_by_voice_plays_a_recorded_dance():
    player = Player()
    asyncio.run(make(FakeMini(), play_move=player).run(
        device({"skill": "dance_move", "arg": "groovy"})))
    assert player.played == [("dance", "groovy_sway_and_roll")]
    player = Player()
    asyncio.run(make(FakeMini(), play_move=player).run(
        device({"skill": "dance_move", "arg": "any"})))
    assert player.played[0][0] == "dance"


def test_recorded_moves_need_a_player_and_stop_cuts_them():
    result = asyncio.run(make(FakeMini()).run(device({"skill": "emote", "arg": "happy"})))
    assert (result.status, result.reason) == ("failed", "unsupported")

    async def main():
        executor = make(mini, play_move=Player(lasts_s=10))
        running = asyncio.ensure_future(executor.run(device({"skill": "emote", "arg": "sad"})))
        await asyncio.sleep(0.05)
        await executor.stop()
        return await asyncio.wait_for(running, 1)
    mini = FakeMini()
    assert asyncio.run(main()).status == "interrupted"
    assert (("cancel_move",) in mini.calls) != Executor(mini).arbiter
    # the emote's sound: the SDK's cancel_move stops it, else stop does
    assert (("stop_playing",) in mini.calls) == Executor(mini).arbiter


def test_with_the_arbiter_an_emote_is_the_runners_samples_on_time_then_breathing(quick):
    player = Player(lasts_s=0.4)

    async def main():
        executor = alive(mini, play_move=player)
        started = time.monotonic()
        result = await executor.run(device({"skill": "emote", "arg": "happy"}))
        ended = time.monotonic()
        await asyncio.sleep(0.6)
        return executor.arbiter, result, started, ended
    mini = FakeMini()
    arbiter, result, started, ended = asyncio.run(main())
    assert result.status == "done" and 0.4 <= ended - started < 0.5  # on time
    assert player.sampled == arbiter
    if not arbiter:
        return
    played = [m for m in mini.moves() if m[0] <= ended]
    pitches = [euler(head)[1] for _, head, _, _ in played]
    assert 19 <= max(pitches) <= 20.01 and mini.beyond == []  # it follows the clip, in limits
    assert max(np.degrees(antennas[0]) for *_, antennas in played) >= 29
    after = [m[1][2, 3] for m in mini.moves() if m[0] > ended + 0.4]
    assert after and np.ptp(after) > 0  # breathing again


def test_stop_mid_emote_freezes_within_0_1_s():
    async def main():
        executor = make(mini, play_move=Player(lasts_s=10))
        running = asyncio.ensure_future(executor.run(device({"skill": "emote", "arg": "sad"})))
        await asyncio.sleep(0.2)
        asked = time.monotonic()
        await executor.stop()
        return asked, await asyncio.wait_for(running, 1)
    mini = FakeMini()
    asked, result = asyncio.run(main())
    assert result.status == "interrupted"
    *before, (at, *_) = mini.moves()  # the freeze is the last command
    assert at - asked < 0.1 and all(m[0] < at for m in before)


def test_speech_onset_glances_toward_the_voice_head_only(quick):
    def glance(doa):
        mini = FakeMini()

        async def main():
            executor = alive(mini, doa=voice_at(doa))
            executor.hold()
            executor.attend()
            await asyncio.sleep(0.1)
        asyncio.run(main())
        ((_, body_yaw, head, _),) = cue_moves(mini)
        assert body_yaw is None
        assert bool(mini.named("goto_target")) != executors.Executor(mini).arbiter
        return math.degrees(head_yaw(head)), head
    yaw, head = glance(math.radians(60))  # 0 is left: 30 degrees left of front
    assert math.isclose(yaw, 30, abs_tol=1e-6)
    pitch = math.degrees(math.asin(-head[2][0]))  # small: a glance, not a nod
    assert abs(pitch) <= 10
    reach = (LIMITS["head_yaw"][1] - 1e-6, executors.GLANCE_MAX_YAW + 1e-6)  # arbiter: clamped
    assert reach[0] <= glance(0.0)[0] <= reach[1]  # hard left: the head's reach
    assert reach[0] <= -glance(math.pi)[0] <= reach[1]


@pytest.mark.parametrize("speech_detected", [False, None])
def test_no_glance_unless_the_daemon_heard_speech_there(quick, caplog, speech_detected):
    mini = FakeMini()

    async def main():
        executor = alive(mini, doa=voice_at(math.radians(35), speech_detected))
        executor.hold()
        executor.attend()
        await asyncio.sleep(0.1)
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.executor"):
        asyncio.run(main())
    assert cue_moves(mini) == []
    assert any("glance skipped" in m and f"speech_detected={speech_detected}" in m
               for m in (r.getMessage() for r in caplog.records))


def test_a_glance_is_relative_to_where_the_body_faces(quick):
    mini = FakeMini()
    mini.yaw = math.radians(90)

    async def main():
        alive(mini, doa=voice_at(math.radians(60))).attend()
        await asyncio.sleep(0.1)
    asyncio.run(main())
    assert math.isclose(head_yaw(cue_moves(mini)[0][2]), math.radians(120))


def test_no_second_glance_within_the_cooldown_after_a_move(quick, caplog):
    now = [0.0]
    mini = FakeMini()

    async def main():
        executor = alive(mini, doa=voice_at(1.0), clock=lambda: now[0])
        executor.attend()
        await asyncio.sleep(0.05)
        now[0] += executors.GLANCE_COOLDOWN_S / 2  # its own motor noise
        executor.attend()
        await asyncio.sleep(0.05)
        now[0] += executors.GLANCE_COOLDOWN_S
        executor.attend()
        await asyncio.sleep(0.05)
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.executor"):
        asyncio.run(main())  # a frozen clock: with the arbiter, the first glance never lands
    assert sum(r.getMessage().startswith("glance: DoA") for r in caplog.records) == 2
    assert 1.5 <= executors.GLANCE_COOLDOWN_S <= 2.0


def test_no_glance_while_tracking_during_a_plan_or_a_dance_or_with_idle_off(caplog):
    async def tracked():
        executor = alive(mini, doa=lambda: 1.0, tracking=True)
        executor.attend()
        await asyncio.sleep(0.05)

    async def planning():
        executor = alive(mini, doa=lambda: 1.0, segment_s=0.1)
        running = asyncio.ensure_future(executor.run(plan(turn(160))))
        await asyncio.sleep(0.02)
        executor.attend()
        await running

    async def dancing():  # with the arbiter, to music: attended mid-Sample, not while listening
        executor = alive(mini, doa=lambda: 1.0, dance=Dancer(),
                         audio=click_track(time.monotonic, 120))
        await executor.run(plan(DANCE))
        await asyncio.sleep(0.3)
        assert executor._dancing is not None
        assert not executor.arbiter or isinstance(executor._primary, Sample)
        executor.attend()
        await asyncio.sleep(0.1)
        await executor.stop()

    async def off():
        alive(mini, doa=lambda: 1.0, idle=False).attend()
        await asyncio.sleep(0.05)

    def glances(mini):  # a cue move that keeps the body; with the arbiter, streamed targets
        return [m for m in cue_moves(mini) if m[1] is None]

    mini = FakeMini()
    asyncio.run(tracked())
    asyncio.run(off())
    assert mini.moves() == []
    asyncio.run(planning())
    assert cue_moves(mini) and glances(mini) == []  # only the turn's moves
    mini = FakeMini()
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.executor"):
        asyncio.run(dancing())  # the dance's own home move keeps the body too: read the log
    said = [r.getMessage() for r in caplog.records if r.getMessage().startswith("glance")]
    assert said[-1] in ("glance skipped: dancing", "glance skipped: an activity is running")
    assert not any(m.startswith("glance: DoA") for m in said)


def test_after_the_turn_ends_it_thinks_then_looks_back(quick):
    mini = FakeMini()

    async def main():
        alive(mini).think()
        await asyncio.sleep(0.2)
    asyncio.run(main())
    away, back = cue_moves(mini)
    assert away[1] is None and back[1] is None
    assert not np.allclose(away[2], np.eye(4)) and np.allclose(back[2], np.eye(4))
    assert back[0] - away[0] >= executors.THINK_S


def test_reply_audio_brings_the_head_back_at_once(quick, monkeypatch):
    monkeypatch.setattr(executors, "THINK_S", 0.5)
    mini = FakeMini()

    async def main():
        executor = alive(mini)
        executor.think()
        await asyncio.sleep(0.15)  # glanced away, thinking
        replied = time.monotonic()
        executor.replying()
        await asyncio.sleep(0.06)  # with the arbiter, it lands a tick or two later
        return replied
    replied = asyncio.run(main())
    _away, back = cue_moves(mini)
    assert np.allclose(back[2], np.eye(4)) and back[0] - replied < 0.015


def test_a_reply_before_the_cue_or_a_plan_cancels_it(quick):
    mini = FakeMini()

    async def replied():
        executor = alive(mini)
        executor.think()
        executor.replying()
        await asyncio.sleep(0.1)

    async def planned():
        executor = alive(mini)
        executor.think()
        await executor.run(plan({"skill": "stop"}))
        await asyncio.sleep(0.1)
    asyncio.run(replied())
    asyncio.run(planned())
    assert cue_moves(mini) == []


def test_thinking_cues_off_think_nothing(quick):
    mini = FakeMini()

    async def main():
        alive(mini, cues=False).think()
        await asyncio.sleep(0.1)
    asyncio.run(main())
    assert mini.calls == []


def test_new_speech_mid_thought_brings_the_head_back(quick, monkeypatch):
    monkeypatch.setattr(executors, "THINK_S", 0.5)
    mini = FakeMini()

    async def main():
        executor = alive(mini)  # no voice direction: no glance, so back to the face
        executor.think()
        await asyncio.sleep(0.15)  # glanced away, thinking
        executor.hold()
        executor.attend()
        await asyncio.sleep(0.05)
    asyncio.run(main())
    _away, back = cue_moves(mini)
    assert np.allclose(back[2], np.eye(4))


def test_with_the_arbiter_a_whole_session_has_one_writer_inside_the_limits(grooving):
    """M1 and A5 end to end: one runner writes every target, none past ``limits.py``.

    It idles, looks, emotes, searches, dances and stops (conftest checks M1 too).
    """
    from fake_mini import OFF_RUNNER

    async def main():
        executor = alive(mini, play_move=player, face=lambda: False, doa=lambda: 0.0,
                         dance=dance.dance, audio=grooving)
        idling = asyncio.ensure_future(executor.breathe())
        await asyncio.sleep(0.6)  # quiet past IDLE_AFTER_S: it breathes
        done = [await executor.run(device(step)) for step in (
            look("left"), {"skill": "emote", "arg": "happy"}, search(), DANCE)]
        await asyncio.sleep(1.0)  # dancing
        done.append(await executor.run(device({"skill": "stop"})))
        idling.cancel()
        return done
    mini, player = FakeMini(), Player(lasts_s=0.3)
    looked, emoted, searched, danced, stopped = asyncio.run(main())
    assert [r.status for r in (looked, emoted, danced, stopped)] == ["done"] * 4
    assert searched.reason == "not_found" and player.sampled
    assert len(mini.named("set_target")) > 100 and mini.named("goto_target") == []
    assert OFF_RUNNER == [] and mini.beyond == []  # M1, A5


# --- motion speed (owner, 2026-10-27: too fast, abrupt) --------------------------------

def played_moves(executor):
    """With the arbiter, record each primary ``_play`` is given instead of playing it."""
    if not executor.arbiter:
        pytest.skip("the arbiter path only: the old path is deleted after live check 2")
    moves = []

    async def spy(move):
        moves.append(move)
    executor._play = spy
    return moves


def looked_left():
    executor = make(FakeMini())
    moves = played_moves(executor)
    asyncio.run(executor.run(plan(look("left"))))
    (move,) = moves
    return move


def test_a_look_lasts_as_its_distance_needs_not_a_fixed_segment(real_speed):
    move = looked_left()
    assert move.duration == arbiter.goto(move.start, move.end, 0.0).duration > 0.3


def test_a_55_degree_glance_no_longer_takes_0_3_s(real_speed):
    executor = make(FakeMini())
    moves = played_moves(executor)
    asyncio.run(executor._cue_move(create_head_pose(yaw=55, degrees=True)))
    (move,) = moves
    assert move.duration >= 55 * 1.875 / arbiter.PEAK_SPEED["yaw"] > executors.CUE_MOVE_S


def test_the_motion_speed_knob_halves_a_moves_duration_at_2(monkeypatch, real_speed):
    slow = looked_left().duration
    monkeypatch.setenv("SAGE_VOICE_MOTION_SPEED", "2")
    assert looked_left().duration == pytest.approx(slow / 2)


@pytest.mark.parametrize(("said", "speed"), [(None, 1.0), ("0.5", 0.5), ("2", 2.0), ("0.25", 0.25),
                                             ("0.1", 1.0), ("3", 1.0), ("fast", 1.0), ("nan", 1.0)])
def test_the_motion_speed_knob_is_validated(said, speed):
    env = {} if said is None else {"SAGE_VOICE_MOTION_SPEED": said}
    assert executors.motion_speed(env) == speed


# --- the listening reflex, and the held thought ------------------------------------------------

@pytest.fixture
def arbitrated(monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")


def antennas_sent(mini):
    return [c[4] for c in mini.named("set_target")]


def test_the_antennas_lift_at_the_voice_and_ease_down_when_gone(quick, arbitrated, monkeypatch):
    monkeypatch.setattr(alive_motion, "PERK_RISE_S", 0.05)
    monkeypatch.setattr(alive_motion, "PERK_FALL_S", 0.05)
    mini = FakeMini()

    async def main():
        executor = alive(mini, idle=False)
        executor.perk()
        await until(lambda: antennas_sent(mini) and math.isclose(
            math.degrees(antennas_sent(mini)[-1][0]), alive_motion.PERK_DEG, abs_tol=0.5))
        up = antennas_sent(mini)[-1]
        executor.unperk()
        await until(lambda: abs(antennas_sent(mini)[-1][0]) <= 0.01)
        return up, antennas_sent(mini)[-1]
    up, down = asyncio.run(main())
    assert math.degrees(up[0]) == pytest.approx(alive_motion.PERK_DEG, abs=0.5)  # left +, right -
    assert math.degrees(up[1]) == pytest.approx(-alive_motion.PERK_DEG, abs=0.5)
    assert down == pytest.approx([0.0, 0.0], abs=0.01)  # nothing baked in


def test_the_lift_stays_up_with_nothing_sent_while_they_speak(quick, arbitrated, monkeypatch):
    """Motors ruin the STT: once risen, the runner yields as it does for any speech."""
    monkeypatch.setattr(alive_motion, "PERK_RISE_S", 0.05)
    mini = FakeMini()

    async def main():
        executor = alive(mini)
        breathing = asyncio.ensure_future(executor.breathe())
        executor.perk()
        executor.hold()
        await asyncio.sleep(0.15)
        count = len(mini.named("set_target"))
        await asyncio.sleep(0.1)
        breathing.cancel()
        return count, len(mini.named("set_target"))
    before, after = asyncio.run(main())
    assert before == after


def test_a_small_nod_goes_and_comes_all_the_way_back(quick, arbitrated, monkeypatch):
    monkeypatch.setattr(alive_motion, "NOD_S", 0.3)  # wide enough that a loaded tick meets its top
    mini = FakeMini()

    def nodded():
        return [euler(c[2])[1] for c in mini.named("set_target")]

    def back():
        return (nodded() and max(nodded()) > 0.5 * alive_motion.NOD_DEG
                and nodded()[-1] == pytest.approx(0.0, abs=0.01))

    async def main():
        executor = alive(mini, idle=False)
        executor.nod()
        await until(back)
    asyncio.run(main())
    pitches = nodded()
    assert 0.5 * alive_motion.NOD_DEG < max(pitches) <= alive_motion.NOD_DEG + 0.01
    assert pitches[-1] == pytest.approx(0.0, abs=0.01)


def test_no_listening_reflex_while_speaking_dancing_searching_stopped_or_cooling(quick, arbitrated):
    mini = FakeMini()

    async def main():
        executor = alive(mini)
        executor.perk(speaking=True)
        executor.nod(speaking=True)
        executor._dancing = threading.Event()  # a dance
        executor.perk()
        executor.nod()
        executor._dancing = None
        executor._plan = asyncio.ensure_future(asyncio.sleep(0.5))  # a search
        executor.perk()
        executor.nod()
        executor._plan.cancel()
        executor._plan = None
        executor._still = True  # stopped
        executor.perk()
        executor._still = False
        assert executor._perk_at is None and executor._nod_at is None
        executor.perk()
        first = executor._perk_at
        executor.unperk()
        executor.perk()  # a flutter: still in its cooldown
        assert executor._perk_at == first
    asyncio.run(main())


def test_without_the_arbiter_or_cues_there_is_no_listening_reflex(quick, monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "0")
    mini = FakeMini()

    async def main():
        executor = alive(mini)
        executor.perk()
        executor.nod()
        await asyncio.sleep(0.05)
        assert executor._perk_at is None
    asyncio.run(main())
    assert mini.moves() == []


def test_the_reflex_logs_when_its_first_target_went_out(quick, arbitrated, caplog):
    mini = FakeMini()

    async def main():
        executor = alive(mini, idle=False)
        executor.perk()
        await asyncio.sleep(0.1)
    with caplog.at_level(logging.INFO, logger="reachy_sage_voice.executor"):
        asyncio.run(main())
    assert any("reflex listening: first target" in r.getMessage() for r in caplog.records)


def test_a_move_starts_from_under_the_reflex_so_nothing_is_baked_in(quick, arbitrated, monkeypatch):
    monkeypatch.setattr(alive_motion, "PERK_RISE_S", 0.01)
    monkeypatch.setattr(alive_motion, "PERK_FALL_S", 0.01)
    mini = FakeMini()

    async def main():
        executor = alive(mini, idle=False)
        executor.perk()
        await asyncio.sleep(0.08)
        _, _, antennas = await executor._pose()
        assert antennas == pytest.approx([0.0, 0.0], abs=0.02)
        await executor._cue_move(mini.head)
        executor.unperk()
        await asyncio.sleep(0.08)
    asyncio.run(main())
    assert antennas_sent(mini)[-1] == pytest.approx([0.0, 0.0], abs=0.02)


def test_the_thought_holds_until_the_reply_with_no_second_move(quick, monkeypatch):
    monkeypatch.setattr(executors, "THINK_S", 5.0)
    mini = FakeMini()

    async def main():
        executor = alive(mini)
        executor.think()
        await asyncio.sleep(0.2)
        moves = len(cue_moves(mini))
        assert not executor._cue.done()  # still thinking, long past the old second
        assert executor._busy() is None  # and breathing may go on under the held pose
        executor.replying()
        await asyncio.sleep(0.15)
        return moves
    assert asyncio.run(main()) == 1
    assert len(cue_moves(mini)) == 2


def test_the_thought_gives_up_quietly_at_its_timeout(quick):
    mini = FakeMini()

    async def main():
        executor = alive(mini)
        executor.think()
        await asyncio.sleep(0.3)  # THINK_S is 0.05 under ``quick``
    asyncio.run(main())
    assert len(cue_moves(mini)) == 2  # there and back, no third


def test_a_turn_ending_with_no_audio_ends_the_thought_but_leaves_a_glance_alone(quick, monkeypatch):
    monkeypatch.setattr(executors, "THINK_S", 5.0)
    mini = FakeMini()

    async def main():
        executor = alive(mini)
        executor.think()
        await asyncio.sleep(0.1)
        executor.thought_over()
        await asyncio.sleep(0.1)
        assert executor._away is None
        glance = executor._cue = asyncio.ensure_future(asyncio.sleep(0.2))
        executor._cue_kind = "glance"
        executor.thought_over()
        assert executor._cue is glance and not glance.cancelled()
        glance.cancel()
    asyncio.run(main())
    assert len(cue_moves(mini)) == 2


def test_the_thought_lasts_past_the_median_reply():
    assert executors.THINK_S >= 5.0  # replies: 3.4 s median, measured 2026-11-05
