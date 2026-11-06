"""The motion arbiter's pure core: one primary plus additive offsets, clamped (LAYERS.md §1a row 5).

Eval M2 (tick rate >= 48 Hz in sim, >= 40 Hz live, p95 jitter <= 10 ms) is a later live gate,
on the wired 50 Hz writer thread: a pure tick has no rate to measure, so it is not faked here.
"""

import math
import random
from itertools import pairwise

import pytest

from reachy_sage_voice.limits import LIMITS
from reachy_sage_voice.motion import alive
from reachy_sage_voice.motion.arbiter import (
    AMAX,
    MIN_MOVE_S,
    NEUTRAL,
    PEAK_SPEED,
    VMAX,
    Goto,
    Hold,
    Sample,
    State,
    goto,
    settled,
    still_s,
    stopping,
    tick,
)

DT = 0.02  # 50 Hz


def run(seconds, primary_at=lambda t: None, listening_at=lambda t: False, scale=1.0, t0=0.0):
    """Tick at 50 Hz; return the targets. The antenna clock stops while the owner speaks."""
    state, antenna_t, out = State(), 0.0, []
    for i in range(int(seconds / DT)):
        t = t0 + i * DT
        primary = primary_at(t)
        antenna_t += 0 if listening_at(t) else DT
        offsets = alive.offsets(t, still_s(state, t, primary), antenna_t, scale)
        state, target = tick(state, t, primary, offsets)
        out.append(target)
    return out


def within(target):
    for key, (low, high) in LIMITS.items():
        if key in target and not low <= target[key] <= high:
            return False
    return target["body_yaw"] + LIMITS["head_yaw"][0] <= target["yaw"] <= (
        target["body_yaw"] + LIMITS["head_yaw"][1])


def test_a5_no_target_beyond_the_limits_over_a_long_random_run():
    rng = random.Random(5)
    plan = []  # (start, primary) every few seconds, some far past the limits
    t = 0.0
    while t < 600:
        pose = {"body_yaw": rng.uniform(-250, 250), "yaw": rng.uniform(-250, 250),
                "pitch": rng.uniform(-70, 70), "roll": rng.uniform(-70, 70)}
        kind = rng.choice((Goto, Hold, Sample, None))
        primary = (Goto({"body_yaw": 0.0, "yaw": 0.0}, pose, t, rng.uniform(0.2, 3.0))
                   if kind is Goto else Sample(lambda s, p=pose: p, t, rng.uniform(0.2, 3.0))
                   if kind is Sample else kind(pose) if kind else None)
        plan.append((t, primary))
        t += rng.uniform(0.5, 6.0)

    def primary_at(now):
        return [p for start, p in plan if start <= now][-1]

    targets = run(600, primary_at, scale=10.0)  # the knob's top: the widest offsets
    assert all(within(target) for target in targets)


def test_offsets_never_push_a_pose_at_the_limits_past_them():
    for edge in ({"body_yaw": 158.0, "yaw": 171.0, "pitch": 40.0, "roll": 40.0},
                 {"body_yaw": -158.0, "yaw": -155.0, "pitch": -40.0, "roll": -40.0},
                 {"body_yaw": 0.0, "yaw": 53.0}, {"body_yaw": 0.0, "yaw": -53.0}):
        assert all(within(t) for t in run(120, lambda t, e=edge: Hold(e), scale=10.0))


def test_a6_idle_and_awake_never_still_for_more_than_5_s():
    targets = run(600)
    window = int(5 / DT)
    for i in range(len(targets) - window):
        span = targets[i:i + window]
        moved = max(max(t[k] for t in span) - min(t[k] for t in span) for k in ("yaw", "pitch"))
        assert moved > 0.5 or max(t["z"] for t in span) - min(t["z"] for t in span) > 0.002


def test_breathing_runs_under_a_still_primary_such_as_tracking():
    targets = run(20, lambda t: Hold({"body_yaw": 30.0, "yaw": 40.0}))
    zs = [t["z"] for t in targets]
    assert max(zs) - min(zs) > 0.008  # about 5 mm either way


def test_breathing_waits_for_a_move_then_fades_in_over_about_0_3_s():
    def primary_at(t):
        return Goto({"z": 0.0}, {"z": 0.0}, 0.0, 2.0) if t < 2.0 else None

    targets = run(4, primary_at, t0=0.0)
    breath = [abs(t["z"]) for t in targets]
    assert max(breath[:int(2.0 / DT)]) == 0.0  # still moving: no breathing
    assert max(breath[int(2.0 / DT):int(2.04 / DT)]) < 0.001  # just ended: fading in
    # the moving primary also holds back the drift, then it fades in with the breathing
    assert all(t["yaw"] == 0.0 for t in targets[:int(2.0 / DT)])


def test_antennas_freeze_while_the_owner_speaks_and_resume_without_a_snap():
    targets = run(10, listening_at=lambda t: 4.0 <= t < 7.0)
    held = targets[int(4.1 / DT):int(6.9 / DT)]
    assert len({(t["left"], t["right"]) for t in held}) == 1
    steps = [abs(b["left"] - a["left"]) for a, b in pairwise(targets)]
    # 15 degrees at 0.5 Hz moves under 1 degree a tick; the slew limit's gentle start from
    # rest then catches up a little (about 1.25 a tick, well inside VMAX)
    assert max(steps) < 1.4


def test_the_scale_knob_multiplies_every_offset():
    still, one, two = (run(30, scale=s) for s in (0.0, 1.0, 2.0))
    assert all(t == still[0] for t in still)
    for a, b in zip(one[100:], two[100:], strict=True):
        for key in ("yaw", "pitch", "roll", "z", "left", "right"):
            assert abs(b[key] - 2 * a[key]) < 1e-9


def test_a_goto_eases_from_start_to_end():
    goto = Goto({"yaw": 0.0}, {"yaw": 20.0}, 1.0, 2.0)
    assert goto.at(0.5)["yaw"] == 0.0 and goto.at(3.5)["yaw"] == 20.0
    assert goto.at(2.0)["yaw"] == 10.0
    assert goto.at(1.2)["yaw"] < 2.0  # min-jerk: slow out of the start


def test_the_pose_is_held_after_the_primary_ends():
    targets = run(5, lambda t: Hold({"body_yaw": 20.0, "yaw": 20.0}) if t < 1 else None, scale=0)
    assert targets[-1]["body_yaw"] == 20.0 and targets[-1]["yaw"] == 20.0


def test_the_same_inputs_give_the_same_targets():
    assert run(60) == run(60)


def test_goto_lasts_long_enough_for_a_55_degree_glance_under_the_head_cap():
    move = goto({"yaw": 0.0}, {"yaw": 55.0}, 1.0)
    assert move.t0 == 1.0 and move.start == {"yaw": 0.0} and move.end == {"yaw": 55.0}
    assert move.duration >= 55 * 1.875 / PEAK_SPEED["yaw"]


def test_goto_turns_the_body_180_degrees_over_at_least_11_s():
    # owner, 2026-10-27: hard starts and stops jerk the body; low speeds, no full-speed turns
    assert goto({"body_yaw": -90.0}, {"body_yaw": 90.0}, 0.0).duration >= 11


def test_the_slew_starts_and_stops_gently():
    assert VMAX["body_yaw"] <= 30 and AMAX["body_yaw"] <= 60
    assert max(VMAX[k] for k in ("yaw", "pitch", "roll")) <= 60


def test_goto_never_lasts_less_than_the_minimum():
    assert goto({"yaw": 0.0, "z": 0.0}, {"yaw": 0.5, "z": 0.001}, 0.0).duration == MIN_MOVE_S
    assert goto(NEUTRAL, NEUTRAL, 0.0).duration == MIN_MOVE_S


def test_goto_takes_a_key_missing_from_start_from_end():
    move = goto({"yaw": 0.0}, {"yaw": 0.0, "pitch": 30.0}, 0.0)
    assert move.duration == MIN_MOVE_S and move.at(0.1)["pitch"] == 30.0


def test_goto_keeps_every_key_under_its_peak_speed():
    rng = random.Random(7)

    def pose():
        return {k: rng.uniform(0, 0.03) if k == "z" else rng.uniform(-60, 60) for k in NEUTRAL}

    cases = [({"yaw": 0.0}, {"yaw": 55.0, "pitch": -30.0}),
             ({"body_yaw": 160.0, "left": -100.0}, {"body_yaw": -160.0, "left": 100.0}),
             ({"z": 0.0, "roll": 10.0}, {"z": 0.02, "roll": -10.0})]
    cases += [(pose(), pose()) for _ in range(10)]
    dt = 0.001
    for start, end in cases:
        move = goto(start, end, 2.0)
        poses = [move.at(2.0 + i * dt) for i in range(int(move.duration / dt) + 2)]
        for a, b in pairwise(poses):
            for key in end:
                assert abs(b[key] - a[key]) / dt <= PEAK_SPEED[key] * 1.001, (key, start, end)
        assert poses[-1] == pytest.approx(end)


def slew(plan, seconds, t0=0.0):
    """Tick ``plan(t)`` (a primary) with no offsets at 50 Hz; return the states and targets."""
    state, out = State(), []
    for i in range(int(seconds / DT)):
        state, target = tick(state, t0 + i * DT, plan(t0 + i * DT), [])
        out.append((state, target))
    return out


def caps_hold(targets, key):
    v = [(b[key] - a[key]) / DT for a, b in pairwise(targets)]
    acc = [(b - a) / DT for a, b in pairwise([0.0, *v])]
    assert max(map(abs, v)) <= VMAX[key] + 1e-6
    assert max(map(abs, acc)) <= AMAX[key] + 1e-6


def test_the_first_tick_sends_the_target_as_is():
    _, target = tick(State(), 0.0, Hold({"yaw": 40.0}), [])
    assert target["yaw"] == 40.0


def test_a_step_ramps_under_the_velocity_and_acceleration_caps_without_overshoot():
    out = slew(lambda t: Hold({"yaw": 0.0 if t < 0.1 else 40.0}), 3.0)
    targets = [target for _, target in out]
    caps_hold(targets, "yaw")
    yaws = [t["yaw"] for t in targets]
    assert yaws[int(0.12 / DT)] < 1.0  # no jump: it eases out
    assert all(b >= a for a, b in pairwise(yaws)) and max(yaws) <= 40.0
    assert yaws[-1] == 40.0 and out[-1][0].vel["yaw"] == 0.0  # settled, still


def test_every_key_obeys_its_caps_and_a_hold_settles_exactly():
    far = {"body_yaw": 40.0, "yaw": 60.0, "pitch": 30.0, "roll": -30.0, "z": 0.01,
           "left": 90.0, "right": -90.0}
    out = slew(lambda t: Hold(dict(NEUTRAL) if t < 0.1 else far), 5.0)
    targets = [target for _, target in out]
    for key in VMAX:
        caps_hold(targets, key)
        assert targets[-1][key] == far[key] and out[-1][0].vel[key] == 0.0


def test_switching_primaries_mid_move_reverses_smoothly():
    out = slew(lambda t: Hold({"yaw": 0.0}) if t < 0.1 else
               Hold({"yaw": 40.0}) if t < 0.5 else Hold({"yaw": -40.0}), 4.0)
    targets = [target for _, target in out]
    caps_hold(targets, "yaw")
    assert targets[-1]["yaw"] == -40.0


def test_the_runner_keeps_the_slew_across_a_new_primary():
    from reachy_sage_voice.motion import runner

    primaries = {"now": Hold({"yaw": 0.0})}
    step = runner.idle(lambda: primaries["now"], 0.0, scale=0.0)
    step(0.0)
    primaries["now"] = Hold({"yaw": 40.0})  # a glance replaces the hold
    assert step(DT)["yaw"] <= AMAX["yaw"] * DT * DT + 1e-9


def test_a_slewed_target_stays_inside_the_limits():
    out = slew(lambda t: Hold({"body_yaw": 0.0, "yaw": 0.0}) if t < 0.1 else
               Hold({"body_yaw": 300.0, "yaw": 300.0}), 6.0)
    assert all(within(target) for _, target in out)
    assert out[-1][1]["body_yaw"] == LIMITS["body_yaw"][1]


def test_goto_turns_the_body_180_degrees_over_at_least_7_s_and_twice_as_fast_at_speed_2():
    slow = goto({"body_yaw": -90.0}, {"body_yaw": 90.0}, 0.0).duration
    assert slow >= 7.0
    assert goto({"body_yaw": -90.0}, {"body_yaw": 90.0}, 0.0, speed=2.0).duration == slow / 2


def test_goto_skips_keys_with_no_peak_speed_such_as_the_heads_x_and_y():
    assert goto({"x": 0.0, "yaw": 0.0}, {"x": 0.01, "yaw": 0.0}, 0.0).duration == MIN_MOVE_S


def test_the_speed_scales_the_slew_caps_velocity_by_it_and_acceleration_by_its_square():
    state, out = State(), []
    for i in range(int(4.0 / DT)):
        state, target = tick(state, i * DT, Hold({"yaw": 0.0 if i < 5 else 40.0}), [], speed=0.5)
        out.append(target)
    v = [(b["yaw"] - a["yaw"]) / DT for a, b in pairwise(out)]
    acc = [(b - a) / DT for a, b in pairwise([0.0, *v])]
    assert max(map(abs, v)) <= VMAX["yaw"] * 0.5 + 1e-6
    assert max(map(abs, acc)) <= AMAX["yaw"] * 0.25 + 1e-6
    assert out[-1]["yaw"] == 40.0


def test_the_runner_passes_its_speed_to_the_slew():
    from reachy_sage_voice.motion import runner

    primaries = {"now": Hold({"yaw": 0.0})}
    step = runner.idle(lambda: primaries["now"], 0.0, scale=0.0, speed=0.5)
    step(0.0)
    primaries["now"] = Hold({"yaw": 40.0})
    assert step(DT)["yaw"] <= AMAX["yaw"] * 0.25 * DT * DT + 1e-9


def test_settled_once_the_slewed_target_reaches_its_goal_and_rests():
    assert settled(State())  # nothing sent yet: nothing moving
    out = slew(lambda t: Hold({"body_yaw": 0.0 if t < 0.1 else 30.0}), 3.0)
    flags = [settled(state) for state, _ in out]
    arrived = next(i for i, (_, target) in enumerate(out) if target["body_yaw"] == 30.0)
    assert flags[0] and not any(flags[6:arrived]) and all(flags[arrived + 1:])


def test_the_runner_waits_for_settle_before_aliveness_fades_in(monkeypatch):
    from reachy_sage_voice.motion import runner

    monkeypatch.setattr(alive, "FADE_AFTER_S", 0.0)
    move = Goto(dict(NEUTRAL), {**NEUTRAL, "body_yaw": 30.0}, 0.0, 0.2)  # the slew takes ~1.2 s
    step = runner.idle(lambda: move, 0.0)
    targets = [step(i * DT) for i in range(150)]
    arrived = next(i for i, target in enumerate(targets) if target["body_yaw"] == 30.0)
    assert arrived * DT > 1.0 and all(target["z"] == 0.0 for target in targets[:arrived + 1])
    assert any(target["z"] != 0.0 for target in targets[arrived:])  # then it breathes



def test_stopping_is_where_the_slew_brakes_to_from_speed_without_coming_back():
    out = slew(lambda t: Hold({"body_yaw": 0.0 if t < 0.1 else 90.0}), 1.0)  # at full speed
    state = out[-1][0]
    v = state.vel["body_yaw"]
    assert v > 0.6 * VMAX["body_yaw"]
    stop = stopping(state)
    assert stop["body_yaw"] == pytest.approx(
        state.sent["body_yaw"] + v * v / (2 * AMAX["body_yaw"]))
    assert stopping(state, speed=2.0)["body_yaw"] == pytest.approx(
        state.sent["body_yaw"] + v * v / (2 * AMAX["body_yaw"] * 4))
    t0, states = state.t, [state]
    for i in range(1, 60):
        state, _ = tick(state, t0 + i * DT, Hold(stop), [])
        states.append(state)
    yaws = [s.sent["body_yaw"] for s in states]
    vels = [s.vel["body_yaw"] for s in states]
    assert all(abs(b - a) <= AMAX["body_yaw"] * DT + 1e-6 for a, b in pairwise(vels))
    assert all(s.vel["body_yaw"] >= 0.0 for s in states)  # no reversal
    assert max(yaws) <= stop["body_yaw"] + 1e-9 and yaws[-1] == stop["body_yaw"]
    landed = next(i for i, y in enumerate(yaws) if y == stop["body_yaw"])
    assert landed <= math.ceil(v / AMAX["body_yaw"] / DT) + 1  # within one tick of v / AMAX
    assert settled(states[landed + 1])


def test_stopping_at_rest_is_where_it_is_and_stays_inside_the_limits():
    assert stopping(State(sent={"body_yaw": 10.0, "yaw": 5.0, "x": 0.01}, vel={},
                          t=0.0)) == {"body_yaw": 10.0, "yaw": 5.0, "x": 0.01}
    near = State(sent={"body_yaw": LIMITS["body_yaw"][1] - 1.0}, vel={"body_yaw": 45.0}, t=0.0)
    assert stopping(near)["body_yaw"] == LIMITS["body_yaw"][1]


def caps_kept(sends, keys=VMAX):
    """Assert each step of ``sends`` (time, target) keeps the slew caps, from rest after a stall.

    A gap under 0.1 s is a late tick, its caps over the real gap. Past that the body has stopped
    on the last target, so the next step starts from rest and counts as one tick.
    """
    v = dict.fromkeys(keys, 0.0)
    for (t0, a), (t1, b) in pairwise(sends):
        stalled = t1 - t0 > 0.1
        dt = DT if stalled else t1 - t0
        for key in keys:
            now, before = (b[key] - a[key]) / dt, 0.0 if stalled else v[key]
            assert abs(now) <= VMAX[key] + 1e-6, (key, t0, t1, now)
            assert abs(now - before) <= AMAX[key] * dt + 1e-6, (key, t0, t1, now, before)
            v[key] = now


@pytest.mark.parametrize("seed", range(5))
def test_the_sent_target_keeps_the_caps_across_late_ticks_and_stalls(seed):
    rng = random.Random(seed)

    def pose():  # the body and head within reach of each other: reach has its own limit
        return {"body_yaw": rng.uniform(-20, 20), "yaw": rng.uniform(-30, 30),
                "pitch": rng.uniform(-30, 30), "roll": rng.uniform(-30, 30),
                "z": rng.uniform(-0.008, 0.008), "left": rng.uniform(-90, 90),
                "right": rng.uniform(-90, 90)}
    t, sends, state, primary = 0.0, [], State(), Hold(dict(NEUTRAL))
    while t < 120:
        if rng.random() < 0.02:
            end = pose()
            primary = rng.choice((Hold(end), goto(primary.at(t), end, t)))
        state, target = tick(state, t, primary, [])
        sends.append((t, target))
        chance = rng.random()
        t += (rng.uniform(0.2, 12) if chance < 0.01 else
              rng.uniform(0.03, 0.1) if chance < 0.1 else DT)
    caps_kept(sends)


# --- the listening reflex under the real caps, on a fake clock --------------------------------

def reflex_trail(key, **since):
    """Tick the reflex at 50 Hz from a rest: (t, value sent, value wanted) over 1.2 s."""
    state, out = State(), []
    state, _ = tick(state, 0.0, Hold(NEUTRAL), [])
    for i in range(1, 60):
        t = i * 0.02
        wanted = alive.listening(*(t if since.get(k) else None for k in ("perk", "quiet", "nod")))
        state, sent = tick(state, t, Hold(NEUTRAL), [wanted])
        out.append((t, sent[key], wanted.get(key, 0.0)))
    return out


def test_the_antennas_are_visible_within_budget_and_inside_the_caps():
    trail = reflex_trail("left", perk=True)
    assert all(abs(sent - wanted) < 0.5 for _, sent, wanted in trail)  # on its curve: no flick
    assert next(t for t, sent, _ in trail if sent >= 4.0) <= 0.12  # 160-200 ms with the link
    assert trail[-1][1] == pytest.approx(alive.PERK_DEG)


def test_the_nod_is_slow_small_and_inside_the_caps():
    trail = reflex_trail("pitch", nod=True)
    assert all(abs(sent - wanted) < 0.2 for _, sent, wanted in trail)
    assert next(t for t, sent, _ in trail if sent >= 1.5) <= 0.2  # after the 200 ms of silence
    assert max(sent for _, sent, _ in trail) <= alive.NOD_DEG
    speeds = [abs(b[1] - a[1]) / 0.02 for a, b in pairwise(trail)]
    assert max(speeds) < 30  # half the head's cap: slow
    assert trail[-1][1] == pytest.approx(0.0, abs=1e-6)  # and it comes all the way back


def test_a_voice_that_goes_mid_rise_never_jumps():
    wanted = [alive.listening(t * 0.02, max(0.0, t * 0.02 - 0.1) if t * 0.02 >= 0.1 else None, None)
              .get("left", 0.0) for t in range(100)]
    assert max(abs(b - a) for a, b in pairwise(wanted)) < 200 * 0.02
