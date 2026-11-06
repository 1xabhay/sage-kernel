"""F1, face freshness: a reading counts only while the detector runs and its stamp is young.

The daemon's detector runs only at a tracking weight above 0 (weight 0 clears
its face), so "no face" from a stopped detector or an old stamp is not knowledge.
"""

from types import SimpleNamespace

import pytest

from reachy_sage_voice.perception.face import STALE_S, Face, face_percept, restamped


def reading(detected=True, ts=10.0, x=0.25):
    return SimpleNamespace(detected=detected, x=x, ts=ts)


def test_a_young_reading_with_the_detector_on_is_fresh():
    assert face_percept(reading(), 10.5, True) == Face(True, True, 0.25, 10.0)


@pytest.mark.parametrize("age", [0.0, STALE_S])
def test_up_to_the_limit_is_fresh(age):
    assert face_percept(reading(), 10.0 + age, True).fresh


def test_older_than_the_limit_is_stale_and_sees_nobody():
    assert STALE_S == 1.5
    assert face_percept(reading(), 10.0 + STALE_S + 0.01, True) == Face(False, False, None, 10.0)


def test_the_detector_off_is_never_fresh():
    assert face_percept(reading(), 10.0, False) == Face(False, False, None, 10.0)


@pytest.mark.parametrize("ts", [None, 11.0])
def test_no_stamp_or_one_from_the_future_is_not_fresh(ts):
    assert not face_percept(reading(ts=ts), 10.0, True).fresh


def test_no_reading_is_not_fresh():
    assert face_percept(None, 10.0, True) == Face(False, False, None, None)


def test_a_fresh_empty_frame_is_fresh_and_sees_nobody():
    empty = reading(detected=False, x=None)
    assert face_percept(empty, 10.2, True) == Face(False, True, None, 10.0)


def test_a_daemon_stamp_is_put_on_this_clock_when_it_is_seen_to_move():
    # The daemon stamps faces on its own monotonic clock, another machine's.
    clock = iter([5.0, 6.0, 7.0])
    restamp = restamped(lambda: next(clock))
    assert restamp(reading(ts=900.0)).ts is None  # first seen: when it was taken is unknown
    assert restamp(reading(ts=900.1)).ts == 5.0  # moved: just now
    assert restamp(reading(ts=900.1)).ts == 5.0  # stuck: as old as when it last moved
    assert restamp(reading(ts=None)).ts is None  # no reading: start again
    assert restamp(reading(ts=900.2)).ts is None
    moved = restamp(reading(detected=False, x=None, ts=900.3))
    assert (moved.detected, moved.x, moved.ts) == (False, None, 6.0)
