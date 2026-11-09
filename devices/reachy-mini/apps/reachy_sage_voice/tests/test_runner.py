"""The 50 Hz motion runner, and the executor on it behind SAGE_VOICE_ARBITER=1 (L5 steps 1-2).

Step 1, idle; step 2, the gotos (look, turn, home) as Goto primaries of one long-lived runner.
Later steps: M1, the rest (emotes and dance as Samples, search views and tracking);
M2, the live tick rate (>= 40 Hz, p95 jitter <= 10 ms) on the robot.
"""

import asyncio
import logging
import math
import re
import time
from dataclasses import replace
from itertools import pairwise
from types import SimpleNamespace

import numpy as np
import pytest
from fake_mini import FakeMini, head_yaw
from reachy_mini.utils import create_head_pose
from test_arbiter import caps_kept
from test_executor import (  # noqa: F401
    Player,
    alive,
    breathing,
    breaths,
    euler,
    look,
    make,
    plan,
    quick,
    turn,
    until,
    weights,
)

from reachy_sage_voice import executor as module
from reachy_sage_voice.limits import LIMITS
from reachy_sage_voice.motion import arbiter, runner
from reachy_sage_voice.motion.arbiter import NEUTRAL, Goto, Hold
from reachy_sage_voice.robot import Reconnecting


class Fake:
    """A fake clock and sleep; ``stall(at, s)`` makes the sleep at ``at`` last ``s`` longer."""

    def __init__(self):
        self.now, self.stalls = 0.0, {}

    def clock(self):
        return self.now

    async def sleep(self, s):
        late = self.stalls.pop(round(self.now / runner.DT), 0.0)
        self.now += s + late


def go(fake, seconds, step=None, link_down=lambda: False, busy=None):
    sent = []

    async def send(target):
        sent.append((fake.now, target))
    hold = Hold({"yaw": 0.0})
    step = step or runner.idle(lambda: hold, 0.0, scale=10.0)
    busy = busy or (lambda: "done" if fake.now >= seconds else None)
    why = asyncio.run(runner.run(step, send, fake.clock, fake.sleep, busy, link_down))
    return why, sent


def test_idle_ticks_at_50_hz_and_never_beyond_the_limits():
    why, sent = go(Fake(), 60)
    assert why == "done"
    assert abs(len(sent) - 60 * 50) <= 1
    for _, target in sent:
        for key, (low, high) in LIMITS.items():
            assert key not in target or low <= target[key] <= high, (key, target)
    zs = [target["z"] for _, target in sent]
    assert max(zs) - min(zs) > 0.015  # scale 10 still breathes, up to the z limit


def test_late_ticks_are_dropped_never_queued():
    fake = Fake()
    fake.stalls = {10: 0.1}  # one sleep runs 100 ms long: five ticks missed
    _, sent = go(fake, 1)
    times = [t for t, _ in sent]
    assert min(b - a for a, b in pairwise(times)) >= runner.DT - 1e-9  # no burst after
    assert len(sent) <= 50 - 4


def test_while_the_link_is_down_it_stops_sending_and_keeps_the_desired_state():
    fake = Fake()
    down = lambda: 1.0 <= fake.now < 3.0  # noqa: E731
    _, sent = go(fake, 4, link_down=down)
    during = [t for t, _ in sent if 1.0 <= t < 3.0]
    assert len(during) <= 2 / runner.PROBE_S + 1  # only a probe now and then, to see it back
    steps = [t for t, _ in sent]
    assert steps[-1] >= 3.9  # carries on after, where the clock says


def test_it_yields_at_once_when_something_else_has_the_body():
    _, sent = go(Fake(), 1, busy=lambda: "tracking")
    assert sent == []


def test_a_pose_round_trips_through_set_target_arguments():
    from reachy_mini.utils import create_head_pose
    head = create_head_pose(z=0.004, roll=3, pitch=-5, yaw=20)
    pose = runner.pose(head, math.radians(10), [0.1, -0.2])
    assert math.isclose(pose["yaw"], 20) and math.isclose(pose["body_yaw"], 10)
    out = runner.command(pose)
    assert np.allclose(out["head"], head) and np.allclose(out["antennas"], [0.1, -0.2])
    assert math.isclose(out["body_yaw"], math.radians(10))


#: A move at speed from 0.1 s on: the pitch for about 0.75 s, the body for about 2 s.
FAR = {**NEUTRAL, "body_yaw": 50.0, "pitch": 30.0}


def far_move(fake):
    rest, far = Hold(dict(NEUTRAL)), Hold(FAR)
    return runner.idle(lambda: rest if fake.now < 0.1 else far, 0.0, scale=0.0)


@pytest.mark.slew
def test_a_stalled_send_resumes_from_rest_inside_the_caps():
    fake = Fake()
    fake.stalls = {20: 1.15}  # at 0.4 s, a reconnect blocks the writer mid-move
    _, sent = go(fake, 5, far_move(fake))
    assert max(b - a for a, b in pairwise(t for t, _ in sent)) > 1.15
    caps_kept(sent)
    assert sent[-1][1]["body_yaw"] == 50.0 and sent[-1][1]["pitch"] == 30.0


@pytest.mark.slew
def test_the_link_back_resumes_from_what_was_last_sent_inside_the_caps():
    fake = Fake()
    down = lambda: 0.4 <= fake.now < 3.0  # noqa: E731
    _, sent = go(fake, 6, far_move(fake), link_down=down)
    caps_kept(sent)  # the probes too: the link may take any one of them
    assert sent[-1][1]["body_yaw"] == 50.0 and sent[-1][1]["pitch"] == 30.0


# --- the executor, flag on and off ----------------------------------------------------


def calls(monkeypatch):
    ran = []
    real = runner.run

    async def spy(*args):
        ran.append(args)
        return await real(*args)
    monkeypatch.setattr(runner, "run", spy)
    return ran


def test_flag_off_has_no_runner(quick, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "0")
    ran = calls(monkeypatch)

    async def main():
        (await breathing(alive(mini), 0.2)).cancel()
    mini = FakeMini()
    asyncio.run(main())
    assert breaths(mini) and ran == []


def test_the_runner_is_the_default_and_0_rolls_it_back(monkeypatch):
    monkeypatch.delenv("SAGE_VOICE_ARBITER", raising=False)
    assert make(FakeMini()).arbiter
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "0")
    assert not make(FakeMini()).arbiter


def test_once_a_minute_the_runner_logs_its_gates(caplog):
    """M2's rate and jitter, A5's clamps, A6's longest still while it holds: one line a minute."""
    caplog.set_level(logging.INFO, logger="reachy_sage_voice.motion.runner")
    past = Hold({**arbiter.NEUTRAL, "body_yaw": LIMITS["body_yaw"][1] + 5.0})
    held = [past]
    step = runner.idle(lambda: held[0], 0.0, scale=0.0)  # nothing breathes: still once there
    t = 0.0
    while t < 61.0:
        step(t)
        if 20.0 <= t < 20.05:
            held[0] = Hold(dict(arbiter.NEUTRAL))  # back inside: no more clamps
            t += 0.06  # one late tick
        t += runner.DT
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("motion gates:")]
    assert len(lines) == 1, lines
    m = re.match(r"motion gates: (\d+) ticks, ([\d.]+) Hz, late (\d+), jitter p95 (\d+) ms, "
                 r"clamped (\d+) \(body_yaw (\d+) yaw (\d+)\), sudden (\d+), "
                 r"still max ([\d.]+) s$", lines[0])
    assert m, lines[0]
    ticks, hz, late, _, clamped, body, yaw, sudden, still = (float(g) for g in m.groups())
    assert body == yaw == clamped  # the minute line names the axes: the body, and the head with it
    assert 2900 < ticks < 3010 and 49 < hz <= 50.1 and late == 1 and sudden == 0
    assert 900 < clamped < 1100  # the ticks before 20 s: the goal past the limit
    assert 30 < still < 41  # from when the slew landed home, a few s after 20 s, to the line


def test_flag_on_idle_runs_the_runner_within_the_limits(quick, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    ran = calls(monkeypatch)

    async def main():
        (await breathing(alive(mini), 0.5)).cancel()
    mini = FakeMini()
    asyncio.run(main())
    sent = breaths(mini)
    assert len(ran) == 1 and len(sent) > 10
    assert mini.beyond == [] and all(abs(c[2][2, 3]) <= 0.010 for c in sent)


def test_flag_on_tracking_keeps_breathing_under_the_head_the_daemon_steers(quick, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    monkeypatch.setattr(runner.alive, "FADE_AFTER_S", 0.0)
    monkeypatch.setattr(runner.alive, "FADE_S", 0.1)
    mini = FakeMini()
    mini.aim = create_head_pose(yaw=20)  # the daemon follows a face 20 degrees left
    mini.start_head_tracking(1.0)  # SAGE_VOICE_PRESENCE: on before the executor

    def breathed():
        sent = breaths(mini)
        return (len(sent) > 10 and np.ptp([c[4][0] for c in sent]) > 0.05
                and np.ptp([c[2][2, 3] for c in sent]) > 1e-5)

    async def main():
        task = asyncio.ensure_future(alive(mini, tracking=True).breathe())
        await until(breathed)
        task.cancel()
    asyncio.run(main())
    sent = breaths(mini)
    assert len(sent) > 10
    assert np.ptp([c[4][0] for c in sent]) > 0.05  # the antennas breathe
    assert np.ptp([c[2][2, 3] for c in sent]) > 1e-5  # z too, under the tracker's head
    assert math.isclose(deg(mini.head), 20) and mini.dragged == 0  # the daemon kept the face
    assert mini.named("start_head_tracking") == [("start_head_tracking", 1.0)]  # untouched


# --- tracking weight: the runner's (LAYERS.md §1a row 5) ---------------------------------


def emote_while_tracking(mini, during=None, lasts_s=0.4):
    """Play a pipeline emote (as __main__'s play_emote) with a face locked on.

    ``during(executor)`` runs 0.15 s into it. A plan's emote never meets the tracker: the plan
    already took the weight to 0.
    """
    async def main():
        executor = make(mini, play_move=Player(lasts_s=lasts_s), tracking=True,
                        face=lambda: True)
        running = asyncio.ensure_future(
            executor.overlay(lambda: executor._sample("emotion", "sad1")))
        await asyncio.sleep(0.15)
        if during:
            await during(executor)
        await running
    asyncio.run(main())


def test_flag_on_an_emote_drops_the_tracking_weight_while_it_plays_then_restores_it(
        monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    mini = FakeMini()
    mini.aim = create_head_pose(yaw=20)
    mini.weight = 1.0
    emote_while_tracking(mini)
    assert weights(mini) == [0.0, 1.0]
    dropped, restored = (mini.calls.index(("start_head_tracking", w)) for w in (0.0, 1.0))
    nods = [i for i, c in enumerate(mini.calls)
            if c[0] == "set_target" and euler(c[2])[1] > 10]
    assert nods and dropped < nods[0] and nods[-1] < restored  # the clip had the head


class RefusesTheDrop(FakeMini):
    """The motion link stalls just as the tracker is asked to drop its weight."""

    refused = False

    def start_head_tracking(self, weight=1.0):
        if weight == 0.0 and not self.refused:
            self.refused = True
            raise ConnectionError("Lost connection with the server.")
        super().start_head_tracking(weight)


def test_flag_on_the_link_back_mid_emote_reasserts_the_dropped_weight(monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    mini = RefusesTheDrop()
    emote_while_tracking(mini)
    assert weights(mini) == [0.0, 1.0]  # said again as dropped, not as 1 mid-emote


class Dropping(FakeMini):
    """The SDK's command websocket dropped: every command raises until a fresh client."""

    def __init__(self):
        super().__init__()
        self.dead = False
        self.client = SimpleNamespace(host="robot", port=0, disconnect=lambda: None)

    def set_target(self, head=None, antennas=None, body_yaw=None):
        if self.dead:
            raise ConnectionError("Lost connection with the server.")
        super().set_target(head, antennas, body_yaw)

    def enable_motors(self):
        self.calls.append(("enable_motors",))


def test_flag_on_a_reconnect_mid_emote_resends_the_runners_weight(monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    mini = Dropping()

    def connect(host, port):
        mini.dead = False
        return SimpleNamespace(host=host, port=port, disconnect=lambda: None)

    async def drop(executor):
        mini.dead = True
    emote_while_tracking(Reconnecting(mini, connect=connect), drop)
    assert ("enable_motors",) in mini.calls
    assert weights(mini) == [0.0, 0.0, 1.0]  # the reconnect said the dropped weight


# --- step 2: gotos are Goto primaries the one runner plays ------------------------------


def deg(head):
    return math.degrees(head_yaw(head))


def test_flag_on_a_look_is_eased_by_the_runner_at_50_hz_to_the_clamped_target(monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    monkeypatch.setattr(arbiter, "MIN_MOVE_S", 0.4)  # the arbiter's move, as segment_s
    mini = FakeMini()
    mini.yaw, mini.head = math.radians(-40), create_head_pose(yaw=-40)  # the body turned right
    result = asyncio.run(make(mini, segment_s=0.4).run(plan(look("left"))))
    assert result.status == "done" and mini.named("goto_target") == []
    sent = mini.named("set_target")
    assert abs(len(sent) - 0.4 * 50) <= 3
    assert all(math.isclose(math.degrees(c[3]), -40) for c in sent)  # a look keeps the body
    yaws = [deg(c[2]) for c in sent]
    steps = [b - a for a, b in pairwise(yaws)]
    assert min(steps) > -1e-9 and max(steps[:2] + steps[-2:]) < max(steps) / 2  # min-jerk
    assert math.isclose(yaws[-1], -40 + LIMITS["head_yaw"][1], abs_tol=1e-6)  # 30 is past reach
    assert mini.beyond == []


def test_flag_on_a_plan_step_returns_once_its_end_target_is_written(monkeypatch):
    # CI 2026-11-05: the step returned with its last target still on the robot's thread.
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")

    class SlowLink(FakeMini):
        def set_target(self, **kwargs):
            time.sleep(0.05)
            super().set_target(**kwargs)

    mini = SlowLink()
    mini.yaw = math.radians(150)
    result = asyncio.run(make(mini).run(plan(turn(90))))
    assert result.status == "done"
    assert math.isclose(mini.yaw, math.radians(158))


def test_flag_on_a_plan_step_completes_when_its_goto_ends(monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    monkeypatch.setattr(arbiter, "MIN_MOVE_S", 0.2)  # the arbiter's move, as segment_s
    mini = FakeMini()
    start = time.monotonic()
    result = asyncio.run(make(mini, segment_s=0.2).run(plan(turn(80), look("left"))))
    took = time.monotonic() - start
    assert result.status == "done", result
    assert result.text == "turned 80 degrees left; looked left"
    assert mini.named("goto_target") == [] and took >= 3 * 0.2  # two turn segments, one look
    sent = mini.named("set_target")
    turned = next(i for i, c in enumerate(sent) if math.isclose(math.degrees(c[3]), 80))
    assert all(math.isclose(deg(c[2]), math.degrees(c[3]), abs_tol=1e-6) for c in sent[:turned])
    assert math.isclose(deg(sent[-1][2]), 30, abs_tol=1e-6)  # then the look, in the world frame


def test_a_goto_lands_on_its_end_even_when_the_tick_after_it_is_late():
    """Send a goto's end pose on the first tick past its end, however late.

    The flake: under load that tick came more than FADE_AFTER_S late, breathing was already
    on the end pose, and no target reached it.
    """
    goto = Goto(dict(NEUTRAL), {**NEUTRAL, "body_yaw": 80.0}, 0.0, 1.0)
    step = runner.idle(lambda: goto, 0.0)
    step(0.5), step(0.98)
    late = 0.98 + runner.alive.FADE_AFTER_S + 0.4  # and past FADE_AFTER_S from the end too
    assert step(late)["body_yaw"] == 80.0
    assert step(late + runner.alive.FADE_AFTER_S + 0.5)["body_yaw"] != 80.0  # then it breathes


def test_flag_on_breathing_resumes_after_the_goto(quick, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    monkeypatch.setattr(runner.alive, "FADE_AFTER_S", 0.0)
    monkeypatch.setattr(runner.alive, "FADE_S", 0.1)

    async def main():
        task = await breathing(executor, 0.1)
        await executor.run(plan(look("left")))
        ended = time.monotonic()
        await asyncio.sleep(0.5)
        task.cancel()
        return ended
    mini = FakeMini()
    executor = alive(mini, segment_s=0.2)
    ended = asyncio.run(main())
    assert mini.named("goto_target") == []
    after = breaths(mini, ended)
    assert len(after) > 10
    assert all(abs(deg(c[2]) - 30) < 10 for c in after)  # breathing where the look left it
    zs = [c[2][2, 3] for c in after]
    assert max(zs) - min(zs) > 1e-5


def test_flag_on_stop_mid_goto_holds_where_the_body_is_at_once(monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    monkeypatch.setattr(arbiter, "MIN_MOVE_S", 0.4)  # the arbiter's move, as segment_s

    async def main():
        executor = make(mini, segment_s=0.4)
        running = asyncio.ensure_future(executor.run(plan(turn(160))))
        await asyncio.sleep(0.2)
        asked = time.monotonic()
        await executor.stop()
        result = await running
        await asyncio.sleep(0.2)
        return asked, result
    mini = FakeMini()
    asked, result = asyncio.run(main())
    assert result.status == "interrupted" and mini.named("goto_target") == []
    *streamed, freeze = mini.named("set_target")
    assert freeze[1] - asked < 0.1 and streamed and streamed[-1][1] <= asked + runner.DT
    assert 0 < freeze[3] < math.radians(160)  # in place, not home, not the target
    # the pose the runner had reached, and the brake from its speed (caps off here: ~1e-7)
    assert math.isclose(freeze[3], streamed[-1][3], abs_tol=1e-6)


# --- M1 step 4a: search views and the stop's freeze are the runner's ---------------------


def test_flag_on_a_search_sends_no_goto_and_its_views_stay_within_the_limits(monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    monkeypatch.setattr(runner.alive, "FADE_AFTER_S", 0.0)
    monkeypatch.setattr(runner.alive, "FADE_S", 0.05)
    mini = FakeMini()
    executor = make(mini, face=lambda: False)
    executor.search = replace(executor.search, dwell_s=0.3)  # long enough to breathe in
    result = asyncio.run(executor.run(plan({"skill": "search_face"})))
    assert result.reason == "not_found" and mini.named("goto_target") == []
    assert mini.beyond == [] and mini.dragged == 0
    sent = mini.named("set_target")
    yaws = [deg(c[2]) for c in sent]
    assert max(yaws) > 60  # the views went round
    zs = [c[2][2, 3] for c in sent]
    assert max(zs) - min(zs) > 1e-5  # breathing under the dwell's Hold


def test_flag_on_stop_holds_the_pose_last_sent_not_a_stale_read(monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")

    async def main():
        executor = make(mini, segment_s=0.4)
        running = asyncio.ensure_future(executor.run(plan(turn(160))))
        await asyncio.sleep(0.2)
        mini.get_current_head_pose = lambda: np.eye(4)  # the daemon's read lags the target
        mini.get_current_joint_positions = lambda: ([0.0] * 7, [0.0, 0.0])
        asked = time.monotonic()
        await executor.stop()
        await running
        await asyncio.sleep(0.3)  # stopped: nothing moves after, breathing included
        return asked
    mini = FakeMini()
    asked = asyncio.run(main())
    *streamed, freeze = mini.named("set_target")
    assert freeze[1] - asked < 0.1
    assert 0 < freeze[3] == streamed[-1][3]  # the head: plus the brake from its speed
    assert np.allclose(freeze[2], streamed[-1][2], atol=1e-4)


# --- settle, with the real slew caps -----------------------------------


@pytest.mark.slew
def test_flag_on_a_turn_step_is_done_only_once_the_slewed_body_arrives(monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    mini = FakeMini()
    result = asyncio.run(make(mini, segment_s=0.2).run(plan(turn(30))))
    assert result.status == "done", result
    assert math.isclose(math.degrees(mini.named("set_target")[-1][3]), 30, abs_tol=1e-6)



@pytest.mark.slew
def test_flag_on_stop_mid_turn_brakes_the_body_to_rest_within_a_second(monkeypatch):
    """Owner, 2026-10-27: it "rotates hard and stops yanking".

    Stop brakes onto where the slew can stop (arbiter.stopping): no jump in speed, no coming
    back, at rest within a second.
    """
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    states, real = [], runner.tick

    def spy(*args, **kwargs):
        state, target = real(*args, **kwargs)
        states.append(state)
        return state, target
    monkeypatch.setattr(runner, "tick", spy)

    async def main():
        executor = make(mini)
        running = asyncio.ensure_future(executor.run(plan(turn(40))))
        await asyncio.sleep(0.9)  # about the 40-degree goto's peak speed, 45 deg/s
        at = executor._state
        await executor.stop()
        await running
        await asyncio.sleep(1.2)
        return at
    mini = FakeMini()
    at = asyncio.run(main())
    v0, key, amax = at.vel["body_yaw"], "body_yaw", arbiter.AMAX["body_yaw"]
    assert v0 > 20, v0
    after = [s for s in states if s.t >= at.t]
    for a, b in pairwise(after):
        if b.t - a.t > arbiter.STALL_S:  # the real clock, under load: it resumes from rest
            continue
        assert abs(b.vel.get(key, 0.0) - a.vel.get(key, 0.0)) <= amax * (b.t - a.t) + 1e-6
        # no velocity sign flip, no coming back: but for the real clock's tick jitter, which
        # can leave a landing crawl under one tick of braking (some 0.03 degrees)
        assert b.vel.get(key, 0.0) >= -amax * arbiter.SETTLE_DT * CRAWL_TICKS
    rest = next(s for s in after if arbiter.settled(s))  # landed on its goal, at rest
    assert rest.t - at.t <= 1.0
    end = after[-1].sent[key]
    assert at.sent[key] < end <= at.sent[key] + v0 * v0 / (2 * amax) + 1e-6  # a little past
    assert math.isclose(math.degrees(mini.named("set_target")[-1][3]), end, abs_tol=1e-6)


# ponytail: a late tick lets a stop pass its point by about 0.1 degree and crawl back within one
# tick (lane/brakerace; test_arbiter's strict xfail tracks it), so "never comes back" allows two
# ticks of braking, not one. Back to one when a stop lands where it actually stops.
CRAWL_TICKS = 2


def spy_on_ticks(monkeypatch):
    states, real = [], runner.tick

    def spy(*args, **kwargs):
        state, target = real(*args, **kwargs)
        states.append(state)
        return state, target
    monkeypatch.setattr(runner, "tick", spy)
    return states


def brakes_to_rest(states, since, key=None):
    """Assert the ticks from ``since`` keep AMAX and end at rest, ``key`` never coming back."""
    after = states[sum(s.t < since for s in states) - 1:]  # from the last tick before it
    for a, b in pairwise(after):
        for k, amax in arbiter.AMAX.items():
            if k in a.vel and k in b.vel and b.t - a.t <= arbiter.STALL_S:  # else load: a stall
                assert abs(b.vel[k] - a.vel[k]) <= amax * (b.t - a.t) + 1e-6, (k, a, b)
    assert arbiter.settled(after[-1]), after[-1]
    if key:
        sign = math.copysign(1.0, after[0].vel[key])
        assert all(sign * s.vel[key] >= -arbiter.AMAX[key] * arbiter.SETTLE_DT * CRAWL_TICKS
                   for s in after)


async def cue_at_speed(executor):
    """Start a far cue move, wait for its peak speed and return when it is at it."""
    executor._start_cue(executor._cue_move(create_head_pose(yaw=45)), "think", "the test")
    await asyncio.sleep(0.5)
    assert abs(executor._state.vel["yaw"]) > 20, executor._state.vel  # at speed, under the cap
    return time.monotonic()


@pytest.mark.slew
def test_flag_on_a_cue_cancelled_by_another_brakes_before_the_runner_yields(monkeypatch):
    """Owner, 2026-11-05: "accelerating and stopping hard is what is jerky and bad"."""
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    states = spy_on_ticks(monkeypatch)

    async def main():
        executor = alive(FakeMini())
        since = await cue_at_speed(executor)
        executor._start_cue(asyncio.sleep(1.2), "think", "the turn ended again")
        await asyncio.sleep(1.2)
        return since
    brakes_to_rest(states, asyncio.run(main()), "yaw")


@pytest.mark.slew
def test_flag_on_a_cue_ended_with_nothing_after_it_brakes_without_coming_back(monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    states = spy_on_ticks(monkeypatch)

    async def main():
        executor = alive(FakeMini())
        executor.motion.scale = 0.0
        since = await cue_at_speed(executor)
        executor._end_cue("the test")
        await asyncio.sleep(0.6)  # under IDLE_AFTER_S and the fade: only the brake runs
        return since
    brakes_to_rest(states, asyncio.run(main()), "yaw")


@pytest.mark.slew
def test_flag_on_a_plan_cancelled_with_idle_off_brakes_before_the_runner_yields(monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    states = spy_on_ticks(monkeypatch)

    async def main():
        executor = make(FakeMini())
        running = asyncio.ensure_future(executor.run(plan(turn(40))))
        await until(lambda: executor._state and executor._state.vel.get("body_yaw", 0.0) > 15)
        since = time.monotonic()
        running.cancel()
        await until(lambda: arbiter.settled(executor._state) and states[-1].t > since)
        return since
    brakes_to_rest(states, asyncio.run(main()), "body_yaw")


@pytest.mark.slew
def test_flag_on_the_person_speaking_brakes_breathing_before_the_runner_yields(
        quick, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    monkeypatch.setattr(runner.alive, "FADE_AFTER_S", 0.0)
    monkeypatch.setattr(runner.alive, "FADE_S", 0.1)
    states = spy_on_ticks(monkeypatch)

    async def main():
        executor = alive(FakeMini())
        executor.motion.scale = 3.0  # the antennas at 140 deg/s when their clock is at 1 s
        task = await breathing(executor, 1.0)
        assert abs(executor._state.vel["left"]) > 50, executor._state.vel
        since = time.monotonic()
        executor.hold()
        await asyncio.sleep(0.6)
        task.cancel()
        return since
    brakes_to_rest(states, asyncio.run(main()))


def test_flag_on_a_move_that_never_settles_carries_on_after_a_bound(monkeypatch, caplog):
    """The settle wait is bounded: past the move's end plus SETTLE_TIMEOUT_S it warns, goes on."""
    import reachy_sage_voice.executor as module

    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    monkeypatch.setattr(module, "moving", lambda state, primary: True)
    monkeypatch.setattr(module, "SETTLE_TIMEOUT_S", 0.2, raising=False)

    async def main():
        return await asyncio.wait_for(make(FakeMini()).run(plan(turn(30))), 3.0)
    assert asyncio.run(main()).status == "done"
    assert "settle" in caplog.text


# --- tracking hands the head back: the runner's stale target never goes live --------------


class FaceThenGone(FakeMini):
    """The daemon's tracker at +30 degrees; the face goes once a weight is given back."""

    def start_head_tracking(self, weight=1.0):
        if weight:
            self.aim = None  # these tests are of letting go: nothing is acquired after it
        super().start_head_tracking(weight)


async def dance_then_stop(executor):
    await executor.run(plan({"skill": "dance"}))
    await asyncio.sleep(0.2)
    await executor.stop()


async def timed_track_stopped(executor):
    executor._locked = False  # a timed track_face, or a search's: not sticky
    await executor.stop()


LET_GO = {
    "stop tracking": lambda executor: executor.run(plan({"skill": "stop_tracking"})),
    "a look": lambda executor: executor.run(plan(look("right"))),
    "an emote": lambda executor: executor.overlay(lambda: executor._sample("emotion", "sad1")),
    "a dance": dance_then_stop,
    "a search": lambda executor: executor.run(plan({"skill": "search_face"})),
    "a stop": timed_track_stopped,
}


@pytest.mark.slew
@pytest.mark.parametrize("path", LET_GO)
@pytest.mark.parametrize("aim", [30, 60])  # 60: past limits.py's reach, inside the daemon's 65
def test_flag_on_letting_go_of_tracking_slews_from_where_the_head_is(aim, path, quick, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    mini = FaceThenGone()
    mini.weight, mini.aim = 1.0, create_head_pose(yaw=aim)

    async def main():
        executor = alive(mini, tracking=True, face=lambda: True, dance=lambda *a, **k: None,
                         play_move=Player(lasts_s=0.3))
        task = await breathing(executor, 0.3)
        assert math.isclose(deg(mini.head), aim) and abs(deg(mini.target)) < 5  # stale: at 0
        mini.trail.clear()
        await LET_GO[path](executor)
        await asyncio.sleep(0.1)
        task.cancel()
    asyncio.run(main())
    assert not mini.snaps or max(mini.snaps) < 1.0  # no jump as the aim cleared
    assert abs(mini.trail[0][1] - aim) < 2.0  # the first target is from where the head is
    top = arbiter.VMAX["yaw"]
    for (t0, y0), (t1, y1) in pairwise(mini.trail):
        assert abs(y1 - y0) <= top * (t1 - t0) + 1.0, (t1 - t0, y0, y1)


def test_flag_on_a_stop_of_a_timed_track_holds_where_the_tracker_left_the_head(quick, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    mini = FaceThenGone()
    mini.weight, mini.aim = 1.0, create_head_pose(yaw=30)

    async def main():
        executor = alive(mini, tracking=True, face=lambda: True)
        task = await breathing(executor, 0.2)
        await timed_track_stopped(executor)
        task.cancel()
    asyncio.run(main())
    assert math.isclose(deg(mini.head), 30, abs_tol=1.0)


def test_a_reconnect_rearms_tracking_without_moving_the_head(monkeypatch):
    """enable_motors pins the daemon's targets to the head, and the same weight clears no aim."""
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "0")  # the command here is the test's, not M1's
    mini = Dropping()
    mini.enable_motors = lambda: setattr(mini, "target", mini.head)
    mini.weight, mini.aim, mini.head = 1.0, create_head_pose(yaw=30), create_head_pose(yaw=30)
    mini.target = np.eye(4)
    mini.dead = True

    def connect(host, port):
        mini.dead = False
        return SimpleNamespace(host=host, port=port, disconnect=lambda: None)
    pet = Reconnecting(mini, connect=connect)
    pet.tracking = 1.0
    pet.set_target(head=np.eye(4))
    assert not mini.snaps and math.isclose(deg(mini.head), 30)
    assert weights(mini) == [1.0]


# --- acquiring: the weight comes up from here, as the daemon's own speed cannot be limited ---


def test_flag_on_the_tracker_comes_up_over_the_ramp_once_a_face_is_seen(monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    monkeypatch.setattr(module, "TRACK_RAMP_S", 0.3)
    monkeypatch.setattr(module, "RAMP_POLL_S", 0.01)
    mini, seen = FakeMini(), [False]
    mini.aim, mini.target = create_head_pose(yaw=30), np.eye(4)

    async def main():
        executor = make(mini, face=lambda: seen[0])
        await executor.run(plan({"skill": "track_face"}))
        await asyncio.sleep(0.1)
        assert weights(mini) == [module.SEARCH_WEIGHT]  # no face: nothing to acquire too fast
        assert abs(deg(mini.head)) < 2
        seen[0], start = True, time.monotonic()
        while mini.weight < 1.0:
            await asyncio.sleep(0.01)
        return time.monotonic() - start
    assert 0.25 < asyncio.run(main()) < 1.0
    assert mini.weight == 1.0 and weights(mini) == sorted(weights(mini))
    assert max(b - a for a, b in pairwise(weights(mini))) <= 0.1 + 1e-9
    assert max(abs(b[1] - a[1]) for a, b in pairwise(mini.trail)) < 3.5  # of 30 degrees


def test_flag_on_a_search_hands_off_to_tracking_over_the_ramp(monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    monkeypatch.setattr(module, "TRACK_RAMP_S", 0.2)
    mini = FakeMini()

    async def main():
        executor = make(mini, face=lambda: True)
        await executor.run(plan({"skill": "search_face"}))
        assert mini.weight < 1.0  # the hand-off starts low
        mini.aim, mini.trail = create_head_pose(yaw=deg(mini.head) + 30), []
        while mini.weight < 1.0:
            await asyncio.sleep(0.01)
    asyncio.run(main())
    assert mini.trail and max(abs(b[1] - a[1]) for a, b in pairwise(mini.trail)) < 3.5


def test_flag_on_an_emote_gives_the_weight_back_over_the_ramp(monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    monkeypatch.setattr(module, "TRACK_RAMP_S", 0.1)
    mini = FakeMini()
    mini.weight = 1.0

    async def main():
        executor = make(mini, play_move=Player(lasts_s=0.1), tracking=True, face=lambda: True)
        await executor.overlay(lambda: executor._sample("emotion", "sad1"))
        while mini.weight < 1.0:
            await asyncio.sleep(0.01)
    asyncio.run(main())
    assert weights(mini)[:2] == [0.0, module.SEARCH_WEIGHT] and len(weights(mini)) > 5
    assert weights(mini)[-1] == 1.0


def test_flag_on_letting_go_ends_the_ramp(monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    monkeypatch.setattr(module, "TRACK_RAMP_S", 0.2)
    mini = FakeMini()

    async def main():
        executor = make(mini, face=lambda: True)
        await executor.run(plan({"skill": "track_face"}))
        await asyncio.sleep(0.05)
        await executor.run(plan({"skill": "stop_tracking"}))
        told = len(mini.calls)
        await asyncio.sleep(0.25)
        return [c for c in mini.calls[told:] if c[0] == "start_head_tracking"]
    assert asyncio.run(main()) == [] and mini.weight is None


def sudden_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("sudden move")]


@pytest.mark.slew
def test_a_step_past_a_speed_limit_logs_its_axis_and_speed_once_a_second(caplog, monkeypatch):
    caplog.set_level("WARNING", logger="reachy_sage_voice.motion.runner")
    real = runner.tick

    def faulty(state, t, *args):  # what the slew now rules out: the line guards against its return
        state, target = real(state, t, *args)
        snapped = {**state.sent, "yaw": 37.0 * round(t / 0.02 % 2)}
        return replace(state, sent=snapped), {**target, **snapped}
    monkeypatch.setattr(runner, "tick", faulty)
    step = runner.idle(lambda: None, 0.0)
    for i in range(1, 76):  # 1.5 s
        step(i * 0.02)
    lines = sudden_lines(caplog)
    assert len(lines) == 2
    assert lines[0] == "sudden move: yaw 1850/s, limit 60"


@pytest.mark.slew
def test_a_hand_back_past_the_reach_eases_in_at_the_head_speed_limit(caplog):
    caplog.set_level("WARNING", logger="reachy_sage_voice.motion.runner")
    step = runner.idle(lambda: None, 0.0)
    step(0.0)
    step.rebase({**NEUTRAL, "yaw": 60.0}, 0.0)  # the tracker left it past LIMITS' 53 off the body
    sent = [step(i * 0.02) for i in range(1, 76)]
    assert sudden_lines(caplog) == []
    yaws = [60.0, *(target["yaw"] for target in sent)]
    assert all(abs(b - a) <= 60.0 * 0.02 + 1e-6 for a, b in pairwise(yaws))
    assert sent[-1]["yaw"] - sent[-1]["body_yaw"] == pytest.approx(53.0, abs=0.01)  # idle drifts it


@pytest.mark.slew
def test_flag_on_the_first_target_starts_from_where_the_head_is(quick, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "1")
    mini = FakeMini()
    mini.head = create_head_pose(yaw=60)  # past the reach: nobody has sent a target yet

    async def main():
        task = asyncio.ensure_future(alive(mini).breathe())

        async def sent():
            while len(breaths(mini)) < 5:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(sent(), 10)
        task.cancel()
    asyncio.run(main())
    assert math.degrees(head_yaw(breaths(mini)[0][2])) == pytest.approx(60.0, abs=1.3)


@pytest.mark.slew
def test_the_slew_at_full_speed_is_never_a_sudden_move(caplog):
    caplog.set_level("WARNING", logger="reachy_sage_voice.motion.runner")
    far = Goto(dict(NEUTRAL), {**NEUTRAL, "body_yaw": 150.0, "yaw": 170.0}, 0.0, 0.01)  # too fast
    step = runner.idle(lambda: far, 0.0)
    for i in range(500):
        step(i * 0.02)
    assert sudden_lines(caplog) == []
