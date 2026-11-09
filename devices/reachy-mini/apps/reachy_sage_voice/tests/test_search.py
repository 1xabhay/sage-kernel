"""The search planner: voice first, never the same place twice, call out, then give up (LAYERS O4).

Simulated time only: the sim calls ``next_step`` once a view has been held for ``pace_s``,
and a call-out takes ``CALL_S`` to say and hear back.
"""

import inspect
import random

from reachy_sage_voice import limits
from reachy_sage_voice.behaviour import search as searches
from reachy_sage_voice.behaviour.search import CallOut, Found, GiveUp, Search, View

LOW, HIGH = limits.LIMITS["yaw"]
CALL_S = 3.0


def run(search, person=None, doa=None, last_seen=None, reply=None):
    """Drive one search; return the views, the (time, action) log and the last action."""
    state, t, fresh, views, log, seen = search.start(doa, last_seen, 0.0), 0.0, None, [], [], False
    for _ in range(200):
        state, act = search.next_step(state, t, seen, fresh)
        log.append((t, act))
        fresh, seen = None, False
        if isinstance(act, Found | GiveUp):
            return views, log, act
        if isinstance(act, View):
            views.append(act)
            t += search.pace_s
            seen = person is not None and abs(act.bearing - person) <= search.radius
        else:  # CallOut: say it and listen; a reply brings a fresh DoA
            t += CALL_S
            fresh = reply
    raise AssertionError("the search never ended")


def coverage(views, half):
    """Return the share of the reachable yaw band some view saw."""
    spans = sorted((max(LOW, v.bearing - half), min(HIGH, v.bearing + half)) for v in views)
    seen, end = 0.0, LOW
    for a, b in spans:
        if b > end:
            seen += b - max(a, end)
            end = b
    return seen / (HIGH - LOW)


def test_with_no_voice_it_covers_the_band_at_live_pace_nearest_unchecked_first():
    s = Search()
    assert s.pace_s == 4.1
    views, log, end = run(s)
    assert [round(v.bearing) for v in views] == [0, 70, 138, -70, -130]
    assert coverage(views, s.radius) > 0.999
    assert isinstance(end, GiveUp) and log[-1][0] <= 60


def test_no_bearing_is_looked_at_twice_within_the_radius():
    rng = random.Random(4)
    for _ in range(300):
        s = Search(pace_s=rng.uniform(1.0, 6.0))  # a view is at least its dwell
        views, _, _ = run(s, doa=rng.choice([None, rng.uniform(-200, 200)]),
                          last_seen=rng.choice([None, rng.uniform(-160, 175)]))
        b = [v.bearing for v in views]
        assert all(abs(x - y) >= s.radius - 1e-9 for i, x in enumerate(b) for y in b[:i])


def test_the_voice_bearing_is_looked_at_first_then_the_last_face():
    views, _, _ = run(Search(), doa=100.0, last_seen=-60.0)
    assert [v.bearing for v in views[:2]] == [100.0, -60.0]
    views, _, _ = run(Search(), doa=180.0)  # behind, past the reach: as far as it goes
    assert views[0].bearing == HIGH


def test_it_calls_out_at_about_15_s_and_the_reply_reseeds_the_search():
    s = Search()
    _, log, _ = run(s, reply=-100.0)
    calls = [t for t, a in log if isinstance(a, CallOut)]
    assert 15 - s.pace_s < calls[0] <= 15
    after = next(a for t, a in log if t > calls[0] and isinstance(a, View))
    assert after.bearing == -100.0  # the reply's direction


def test_a_reply_finds_the_owner_who_moved_behind():
    _, log, end = run(Search(), person=-100.0, doa=60.0, reply=-100.0)
    assert isinstance(end, Found) and log[-1][0] < 25


def test_with_replies_but_nobody_it_still_gives_up_by_60_s():
    for reply in (-100.0, 0.0, 171.0):
        _, log, end = run(Search(), reply=reply)
        assert isinstance(end, GiveUp) and log[-1][0] <= 60


def test_a3_every_search_ends_found_or_given_up_within_its_cap():
    rng = random.Random(3)
    for _ in range(500):
        s = Search(pace_s=rng.uniform(0.6, 8.0))
        state, t = s.start(rng.choice([None, rng.uniform(-300, 300)]), None, 0.0), 0.0
        for _ in range(10_000):  # any call pattern, early calls included
            state, act = s.next_step(state, t, rng.random() < 0.01,
                                     rng.choice([None] * 9 + [rng.uniform(-200, 200)]))
            if isinstance(act, Found | GiveUp):
                break
            t += rng.uniform(0.0, 3.0)
        else:
            raise AssertionError("stuck")
        assert t <= s.cap_s + 3.0


def test_a_face_ends_it_at_once_and_it_stays_ended():
    s = Search()
    state = s.start(None, None, 0.0)
    state, act = s.next_step(state, 0.0, False, None)
    state, act = s.next_step(state, 4.1, True, None)
    assert act == Found()
    assert s.next_step(state, 5.0, False, 50.0)[1] == Found()


def test_it_holds_a_view_for_the_dwell_before_deciding_nobody_is_there():
    s = Search()
    state, first = s.next_step(s.start(None, None, 0.0), 0.0, False, None)
    state, again = s.next_step(state, s.dwell_s / 2, False, None)
    assert again == first
    assert s.next_step(state, s.dwell_s, False, None)[1] != first


def test_nothing_outside_the_limits():
    rng = random.Random(5)
    for _ in range(300):
        views, _, _ = run(Search(pitch=rng.uniform(-90, 90)), doa=rng.uniform(-400, 400),
                          last_seen=rng.uniform(-400, 400), reply=rng.uniform(-400, 400))
        for v in views:
            assert LOW <= v.bearing <= HIGH
            assert limits.LIMITS["pitch"][0] <= v.pitch <= limits.LIMITS["pitch"][1]


def test_a2_time_to_find_by_where_the_owner_stands():
    s = Search()
    quiet = []
    for person in (-150, -105, -60, -15, 30, 75, 120, 165):
        _, log, end = run(s, person=float(person))
        assert isinstance(end, Found)
        quiet.append(log[-1][0])
        _, log, end = run(s, person=float(person), doa=float(person))
        assert isinstance(end, Found) and log[-1][0] <= 5
    quiet.sort()
    assert quiet[int(0.9 * (len(quiet) - 1))] <= 25


def test_simulated_time_only():
    assert "import time" not in inspect.getsource(searches)
