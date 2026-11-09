"""The dance skill on a fake clock: beat-locked, inside the limits, stops on time, ends at home."""

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest
from fake_dance_mini import FakeClock, FakeDanceMini, click_track

from reachy_sage_voice import dance

MOVES = {"dance1": SimpleNamespace(duration=3.0), "sad1": SimpleNamespace(duration=3.0)}


@pytest.fixture(autouse=True)
def no_lead(monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_DANCE_LEAD_S", "0")


@pytest.fixture(autouse=True)
def old_path(monkeypatch):
    """M1 allowed: dance.dance writes itself, the flag-off path; with the arbiter, _groove."""
    monkeypatch.setenv("SAGE_VOICE_ARBITER", "0")


def run(audio, clock, mini, stop=None, **kwargs):
    stop = stop or asyncio.Event()
    return asyncio.run(dance.dance(mini, audio, stop, clock=clock, sleep=clock.sleep, **kwargs))


def setup(bpm=120, **track):
    clock = FakeClock()
    return clock, FakeDanceMini(clock), click_track(clock, bpm, **track)


def ends_at_home(mini):
    _, _, head, antennas, _, body_yaw = mini.named("goto_target")[-1]
    assert np.allclose(head, np.eye(4))
    assert np.allclose(antennas, [-0.1745, 0.1745])
    assert body_yaw == 0.0


def tilt_deg(head):
    return np.degrees(np.arccos(np.clip(head[2, 2], -1, 1)))


def yaw_deg(head):
    return np.degrees(np.arctan2(head[1, 0], head[0, 0]))


def test_moves_stay_inside_the_limits():
    clock, mini, audio = setup()
    status, _, _ = run(audio, clock, mini, timeout_s=90, moves=MOVES)
    assert status == "done"
    heads = [t for t in mini.targets if t.head is not None]
    tilts = [tilt_deg(t.head) for t in heads]
    body = [np.degrees(t.body_yaw) for t in heads]
    relative = [(yaw_deg(t.head) - b + 180) % 360 - 180 for t, b in zip(heads, body, strict=True)]
    reach = [1000 * np.linalg.norm(t.head[:3, 3]) for t in heads]
    antennas = np.array([t.antennas for t in heads])
    assert max(tilts) <= 25  # 35° cone, with margin
    assert 15 <= max(np.abs(body)) <= 35  # ±160° (a quiet track: 0.6 of the full sway)
    assert max(np.abs(relative)) <= 46  # ±55°
    assert max(reach) <= 45  # mm off neutral
    assert np.all(np.abs(antennas) <= np.radians(75))
    ends_at_home(mini)


def steps(targets):
    """Return the largest per-tick change of head angle, antenna and body yaw, in degrees."""
    heads = [t for t in targets if t.head is not None]
    rpy = np.unwrap(np.array([[np.arctan2(h.head[2, 1], h.head[2, 2]), -np.arcsin(h.head[2, 0]),
                               np.arctan2(h.head[1, 0], h.head[0, 0])] for h in heads]), axis=0)
    return (np.degrees(np.abs(np.diff(rpy, axis=0))).max(),
            np.degrees(np.abs(np.diff([h.antennas for h in heads], axis=0))).max(),
            np.degrees(np.abs(np.diff([h.body_yaw for h in heads]))).max())


def smooth(targets):
    head, antennas, body = steps(targets)
    return head <= 8 and antennas <= 15 and body <= 2  # per 20 ms, at 150 BPM


def test_motion_never_jumps_even_across_move_switches():
    clock, mini, audio = setup(150)
    run(audio, clock, mini, timeout_s=90)
    assert smooth(mini.targets), steps(mini.targets)


def test_hold_freezes_in_place_and_resumes_smoothly_on_the_beat(monkeypatch):
    clock, mini, audio = setup()
    hold = asyncio.Event()
    clock.at(12.0, hold.set)
    clock.at(15.3, hold.clear)
    status, _, _ = run(audio, clock, mini, timeout_s=25, hold=hold)
    assert status == "done"
    assert not [t for t in mini.targets if 12.03 < t.t < 15.3]  # still while held
    assert [t for t in mini.targets if t.t > 15.3]  # and back after
    assert smooth(mini.targets), steps(mini.targets)  # no jump into or out of the hold


def test_stop_while_held_still_returns_within_a_second():
    clock, mini, audio = setup()
    hold, stop = asyncio.Event(), asyncio.Event()
    clock.at(10.0, hold.set)
    clock.at(12.0, stop.set)
    status, _, _ = run(audio, clock, mini, stop=stop, hold=hold)
    assert status == "interrupted" and clock() - 12.0 < 1.0
    ends_at_home(mini)


def probe(log):
    """Make a library-shaped move that logs the beat time it is asked for, and stays still."""
    def move(t_beats, **_):
        log.append(t_beats)
        return SimpleNamespace(position_offset=np.zeros(3), orientation_offset=np.zeros(3),
                               antennas_offset=np.zeros(2))
    return move


@pytest.mark.parametrize("bpm", [90, 120, 150])
@pytest.mark.parametrize("lead", [0.0, 0.2])
def test_the_moves_run_on_the_heard_beat_within_60_ms(bpm, lead, monkeypatch):
    monkeypatch.setenv("SAGE_VOICE_DANCE_LEAD_S", str(lead))
    clock, mini, audio = setup(bpm)
    log, times = [], []
    fn = probe(log)

    def timed(t_beats, **params):
        times.append(clock())
        return fn(t_beats, **params)

    monkeypatch.setitem(dance.AVAILABLE_MOVES, "probe", (timed, {}, {}))
    monkeypatch.setattr(dance, "ROTATION", ("probe",))
    run(audio, clock, mini, timeout_s=20)
    period, first = 60 / bpm, audio.beats(0, 1)[0]
    errors = [((b - (t + lead - first) / period) + 0.5) % 1 - 0.5
              for t, b in zip(times, log, strict=True) if t > 1.0]
    assert len(errors) > 500
    assert max(np.abs(errors)) * period <= 0.06


def test_it_switches_moves_every_few_bars(monkeypatch):
    clock, mini, audio = setup()
    logs = {"a": [], "b": [], "c": []}
    for name, log in logs.items():
        monkeypatch.setitem(dance.AVAILABLE_MOVES, name, (probe(log), {}, {}))
    monkeypatch.setattr(dance, "ROTATION", tuple(logs))
    run(audio, clock, mini, timeout_s=40)
    assert all(logs.values())


def test_the_library_moves_are_real():
    assert len(dance.ROTATION) >= 10
    assert set(dance.ROTATION) <= set(dance.AVAILABLE_MOVES)
    assert "headbanger_combo" not in dance.ROTATION  # its 30° tilt leaves too little margin
    assert not {"yeah_nod", "chicken_peck", "grid_snap", "jackson_square"} & set(dance.ROTATION)


def test_stop_while_grooving_returns_within_a_second_and_goes_home():
    clock, mini, audio = setup()
    stop = asyncio.Event()
    clock.at(10.0, stop.set)
    status, reason, text = run(audio, clock, mini, stop=stop, moves=MOVES)
    assert (status, reason) == ("interrupted", None) and text
    assert clock() - 10.0 < 1.0
    ends_at_home(mini)


def test_stop_mid_flourish_cancels_it_within_a_second():
    clock, mini, audio = setup(louder=20.0)
    stop = asyncio.Event()
    play = mini.async_play_move

    async def play_then_stop(move, **kwargs):
        clock.at(clock() + 0.5, stop.set)
        await play(move, **kwargs)

    mini.async_play_move = play_then_stop
    status, _, _ = run(audio, clock, mini, stop=stop, moves=MOVES)
    assert mini.named("play_move")
    started = mini.named("play_move")[0][1]
    assert status == "interrupted"
    assert mini.named("cancel_move")
    assert clock() - (started + 0.5) < 1.0
    ends_at_home(mini)


def test_no_music_fails_within_about_8_seconds():
    clock, mini, audio = setup(end=-10.0)
    assert run(audio, clock, mini) == ("failed", "no_music", "I can't hear a beat")
    assert clock() <= 9.0
    assert not mini.named("play_move")


def test_music_stopping_ends_the_dance():
    clock, mini, audio = setup(end=20.0)
    assert run(audio, clock, mini, moves=MOVES) == ("done", None, "danced to ~120 bpm")
    assert 20.0 < clock() < 26.0
    ends_at_home(mini)


def test_the_timeout_ends_the_dance():
    clock, mini, audio = setup()
    status, _, text = run(audio, clock, mini, timeout_s=12)
    assert (status, text) == ("done", "danced to ~120 bpm")
    assert clock() < 13.0


def test_a_drop_plays_one_muted_flourish_and_the_groove_waits_for_it():
    clock, mini, audio = setup(louder=20.0)
    run(audio, clock, mini, timeout_s=40, moves=MOVES)
    played = mini.named("play_move")
    assert len(played) == 1 and 20.0 < played[0][1] < 23.0
    assert all(move is MOVES["dance1"] and sound is False for _, _, move, sound in played)
    assert not [t for t in mini.targets  # the groove leaves the head to the move
                if any(p[1] < t.t < e[1]
                       for p, e in zip(played, mini.named("move_ended"), strict=True))]


def test_no_drop_no_flourish():
    clock, mini, audio = setup()
    run(audio, clock, mini, timeout_s=40, moves=MOVES)
    assert not mini.named("play_move")
