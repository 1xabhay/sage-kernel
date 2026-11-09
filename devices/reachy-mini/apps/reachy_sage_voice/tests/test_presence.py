"""Presence from the daemon's face tracker: an event on seen and lost edges only.

A dropped frame is not someone leaving: the last sighting is held, then faded
(as the pet's FaceMemory does), and only then is "lost" reported. Pure, with
time passed in; the send sits behind one function.
"""

import asyncio

from reachy_sage_voice.perception.face import Face
from reachy_sage_voice.presence import Presence, watch_presence


def run(presence, frames):
    """frames: (now, detected) pairs; returns the events, with when they fired."""
    return [(now, e) for now, seen in frames if (e := presence.update(seen, now))]


def test_two_sightings_in_a_row_are_seen():
    assert run(Presence(), [(0.0, True), (0.2, True)]) == [
        (0.2, {"kind": "presence", "state": "seen", "confidence": 1.0})]


def test_one_frame_is_not_someone_arriving():
    # Arrivals trigger greetings: a one-frame false positive must not greet, then leave.
    assert run(Presence(), [(0.0, True), *[(t / 5, False) for t in range(1, 60)]]) == []
    assert run(Presence(), [(0.0, True), (0.2, False), (0.4, True), (0.6, False)]) == []


def test_seen_and_lost_sit_either_side_of_the_kernels_half_confidence():
    events = [e for _, e in run(Presence(), [(0.0, True), (0.2, True),
                                             *[(t / 2, False) for t in range(1, 20)]])]
    assert [e["state"] for e in events] == ["seen", "lost"]
    assert events[0]["confidence"] >= 0.5 > events[1]["confidence"]


def test_nothing_more_while_someone_stays():
    assert len(run(Presence(), [(t / 10, True) for t in range(50)])) == 1


def test_a_dropped_frame_is_not_a_loss():
    frames = [(0.0, True), (0.05, True), (0.1, False), (0.2, False), (0.3, True), (1.0, False)]
    assert [e["state"] for _, e in run(Presence(), frames)] == ["seen"]


def test_lost_only_after_the_hold_and_the_fade():
    p = Presence(hold_s=2.0, fade_s=6.0)
    events = run(p, [(-0.2, True), (0.0, True), *[(t / 2, False) for t in range(1, 20)]])
    assert [(now, e["state"], e["confidence"]) for now, e in events] == [
        (0.0, "seen", 1.0), (8.0, "lost", 0.0)]


def test_seen_again_after_being_lost():
    frames = [(0.0, True), (0.2, True), (9.0, False), (10.0, True), (10.2, True)]
    assert [e["state"] for _, e in run(Presence(), frames)] == ["seen", "lost", "seen"]


def test_never_seen_is_never_lost():
    assert run(Presence(), [(t, False) for t in range(20)]) == []


def watch(faces, seconds=20, presence="quick"):
    """Watch ``faces`` (Face percepts, then stale ones) for ``seconds``; return the states sent.

    ``presence`` "quick" is ``Presence()`` (lost after 8 s); None, the watch's own (``LEAVE_S``).
    """
    presence = Presence() if presence == "quick" else presence
    faces = iter(faces)
    clock = {"t": 0.0}
    sent = []

    async def sleep(s):
        clock["t"] += s

    async def send(event):
        sent.append(event["state"])

    asyncio.run(watch_presence(lambda: next(faces, STALE), send, lambda: clock["t"], sleep,
                               period_s=0.25, until=lambda: clock["t"] > seconds,
                               presence=presence))
    return sent


SEEN, EMPTY = Face(True, True, 0.0, 0.0), Face(False, True, None, 0.0)
STALE = Face(False, False, None, 0.0)  # an old stamp, or the detector off: nothing known


def test_the_watch_sends_only_edges_through_one_function():
    assert watch([SEEN, SEEN, EMPTY, SEEN] + [EMPTY] * 100) == ["seen", "lost"]


def test_presence_is_unknown_never_lost_when_the_face_is_not_fresh():
    # The detector runs only at weight > 0 (LAYERS F1, O3 a): a stopped one is no empty room.
    assert watch([SEEN, SEEN]) == ["seen"]  # then stale for 20 s: no lost
    assert watch([STALE] * 100) == []


def test_looking_away_for_a_minute_is_not_leaving_but_two_minutes_unseen_is():
    """The watch's own hold: about two minutes unseen is leaving (LEAVE_S).

    Arrivals greet, so a glance away or a minute out of frame must not make a leaving, then an
    arrival.
    """
    from reachy_sage_voice.presence import LEAVE_S
    assert 100 <= LEAVE_S <= 150
    a_minute = [SEEN, SEEN] + [EMPTY] * 240 + [SEEN] * 4 + [EMPTY] * 40  # 60 s away, then back
    assert watch(a_minute, seconds=80, presence=None) == ["seen"]
    gone = [SEEN, SEEN] + [EMPTY] * int(4 * (LEAVE_S + 5))
    assert watch(gone, seconds=LEAVE_S + 10, presence=None) == ["seen", "lost"]


def test_a_face_read_that_fails_is_unknown_and_the_watch_goes_on(caplog):
    """A watch is a bare task: one exception ended it silently (live 2026-11-08)."""
    reads = iter([SEEN, SEEN, "boom", SEEN] + [EMPTY] * 100)

    def face():
        reading = next(reads, STALE)
        if reading == "boom":
            raise OSError("the daemon did not answer")
        return reading
    assert watch_faces(face) == ["seen", "lost"]
    assert "the daemon did not answer" in caplog.text


def watch_faces(face, seconds=20):
    clock, sent = {"t": 0.0}, []

    async def sleep(s):
        clock["t"] += s

    async def send(event):
        sent.append(event["state"])

    asyncio.run(watch_presence(face, send, lambda: clock["t"], sleep, period_s=0.25,
                               until=lambda: clock["t"] > seconds, presence=Presence()))
    return sent


def test_faces_on_the_screen_during_a_media_session_are_no_arrival():
    """Watching F1 (owner, 2026-11-08): a driver's face on the TV must not greet or nudge."""
    faces, clock, sent = iter([SEEN] * 40), {"t": 0.0}, []

    async def sleep(s):
        clock["t"] += s

    async def send(event):
        sent.append(event["state"])

    asyncio.run(watch_presence(lambda: next(faces, STALE), send, lambda: clock["t"], sleep,
                               period_s=0.25, until=lambda: clock["t"] > 10,
                               presence=Presence(), muted=lambda: True))
    assert sent == []
