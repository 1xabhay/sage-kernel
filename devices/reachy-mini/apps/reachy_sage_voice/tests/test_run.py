"""`make run`: one command from the environment to a live pipeline, with no robot here.

The robot and the pipeline runner are fakes; what is checked is the order of
operations and that nothing falls back to a network default.
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
import textwrap
import threading
from types import SimpleNamespace
from unittest import mock

import pytest
from fake_mini import m1

from reachy_sage_voice.__main__ import main
from reachy_sage_voice.executor import Executor
from reachy_sage_voice.manifest import EMOTIONS
from reachy_sage_voice.pipeline import RobotLinkLostError

ENV = {
    "SAGE_VOICE_KERNEL_URL": "http://127.0.0.1:8776",
    "SAGE_VOICE_KERNEL_TOKEN": "t" * 32,
    "SAGE_VOICE_STT_URL": "http://127.0.0.1:8781",
    "SAGE_VOICE_TTS_URL": "http://127.0.0.1:8782",
    "REACHY_MINI_HOST": "reachy-mini.lan",
}


class Robot:
    def __init__(self, calls, fail_wake=False, fail_track=False, **kwargs):
        self.calls, self.fail_wake, self.fail_track = calls, fail_wake, fail_track
        calls.append(("connect", kwargs))
        self.media = object()
        self.client = type("Client", (), {"is_connected": lambda self: True})()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append(("disconnect",))

    def enable_motors(self):
        self.calls.append(("enable_motors",))

    def wake_up(self):
        self.calls.append(("wake_up",))
        if self.fail_wake:
            raise TimeoutError("still settling")

    media_released = False

    def release_media(self):
        self.calls.append(("release",))

    def acquire_media(self):
        self.calls.append(("acquire",))
        self.media = type("Media", (), {"start_recording": lambda s: self.calls.append(("rec",)),
                                        "start_playing": lambda s: self.calls.append(("play",))})()

    def start_head_tracking(self, weight=1.0):
        self.calls.append(("track", weight))
        if self.fail_track:
            raise TimeoutError("tracker not ready")

    ts = 0.0  # the daemon's stamp, moving on with every frame

    def get_tracked_face(self, wait=True):
        self.calls.append(("face", wait))
        self.ts += 0.1
        return type("Face", (), {"detected": True, "x": 0.0, "ts": self.ts})()

    async def async_play_move(self, move, sound):
        m1("async_play_move")
        self.calls.append(("move", move, sound))


def start(env, fail_wake=False, fail_track=False):
    calls, ran = [], {}

    async def run(media, play_emote, moves, env, play_sound=None, presence=None, observers=(),
            face=None, recover=None, executor=None):
        ran.update(media=media, moves=moves, env=env, play_sound=play_sound, presence=presence,
                   observers=observers, face=face, recover=recover, executor=executor)
        await play_emote("cheerful1", True)
        if recover:
            await recover(1)

    async def sample(executor, kind, name, sound=True):  # the arbiter's player, noted
        calls.append(("sample", kind, name, sound))
        if executor._tracking:  # as its _play does: is a face locked on, to drop the weight?
            await executor._face()

    with mock.patch.object(Executor, "_sample", sample):
        main(env=env, robot=lambda **kw: Robot(calls, fail_wake, fail_track, **kw), run=run,
             moves=lambda: dict.fromkeys(EMOTIONS, "a move"), reach=lambda env: [])
    return calls, ran


def test_it_connects_to_the_named_host_only_then_enables_before_waking():
    calls, ran = start(ENV)
    assert calls[0] == ("connect", {"host": "reachy-mini.lan", "connection_mode": "network"})
    assert [c[0] for c in calls[1:3]] == ["enable_motors", "wake_up"]
    assert ran["env"] is ENV and set(ran["moves"]) == set(EMOTIONS)


def test_an_emote_plays_the_recorded_move_muted_or_not(arbiter):
    """With the arbiter an expression is a Sample the motion runner plays, not the SDK's move."""
    calls, _ = start(ENV)
    on = os.environ["SAGE_VOICE_ARBITER"] == "1"
    assert (("move", "a move", True) in calls) != on
    assert (("sample", "emotion", "cheerful1", True) in calls) == on


def test_a_muted_clip_starts_no_sound():
    from reachy_sage_voice.__main__ import _player

    played = []
    mini = SimpleNamespace(media=SimpleNamespace(play_sound=played.append))
    _, clip = _player(mini, {"sigh": SimpleNamespace(sound_path="sigh.wav")})
    asyncio.run(clip("emotion", "sigh", False))
    assert played == []
    asyncio.run(clip("emotion", "sigh"))
    assert played == ["sigh.wav"]


def test_face_tracking_is_off_unless_presence_is_reported():
    """Owner (2026-10-22): following a face is a command, not the default. Presence still needs the
    tracker on (live run 5: the daemon reported no face ever, because nothing switched it on)."""
    calls, _ = start(ENV)
    assert "track" not in [c[0] for c in calls]
    calls, _ = start({**ENV, "SAGE_VOICE_PRESENCE": "1"})
    names = [c[0] for c in calls]
    assert ("track", 1.0) in calls and names.index("track") > names.index("wake_up")


def test_the_executor_owns_the_head_and_hears_the_voice_from_the_robot_daemon(monkeypatch):
    from reachy_sage_voice import executor as executors

    urls = []
    monkeypatch.setattr(executors, "daemon_doa", lambda url: urls.append(url) or (lambda: None))
    monkeypatch.setattr(executors, "daemon_face", lambda url: urls.append(url) or (lambda: None))
    calls, ran = start(ENV)
    executor = ran["executor"]
    assert isinstance(executor, executors.Executor) and executor.mini.calls is calls
    assert sorted(urls) == ["http://reachy-mini.lan:8000/api/media/tracking/face",  # fresh, not 1 Hz
                            "http://reachy-mini.lan:8000/api/state/doa"]
    assert not executor._locked  # following a face is a command
    played = (("move", "a move", True), ("sample", "emotion", "cheerful1", True))
    assert any(c in calls for c in played)  # emotes play, through the executor


def test_recorded_moves_by_voice_play_on_the_robot(arbiter):
    """With the arbiter the SDK's player is not wired at all: the runner is the one writer (M1)."""
    calls, ran = start(ENV)
    if os.environ["SAGE_VOICE_ARBITER"] == "1":
        assert ran["executor"].play_move is None
        return
    asyncio.run(ran["executor"].play_move("emotion", "cheerful1"))
    assert calls[-1] == ("move", "a move", True)


def test_with_presence_on_the_executor_knows_the_tracker_is_on():
    _, ran = start({**ENV, "SAGE_VOICE_PRESENCE": "1"})
    assert ran["executor"]._locked


def test_a_tracker_that_will_not_start_does_not_stop_the_voice():
    _, ran = start({**ENV, "SAGE_VOICE_PRESENCE": "1"}, fail_track=True)
    assert ran


def test_a_slow_wake_up_does_not_stop_the_voice():
    _, ran = start(ENV, fail_wake=True)
    assert ran  # the pipeline still ran


@pytest.mark.parametrize("missing", sorted(ENV))
def test_every_address_and_the_token_must_be_set_before_touching_the_robot(missing):
    env = {k: v for k, v in ENV.items() if k != missing}
    calls = []
    with pytest.raises(SystemExit, match=missing):
        main(env=env, robot=lambda **kw: Robot(calls, **kw), run=None, moves=dict)
    assert calls == []


def test_the_stt_url_may_be_given_with_or_without_v1():
    from reachy_sage_voice.pipeline import openai_base

    assert openai_base("http://127.0.0.1:8781") == "http://127.0.0.1:8781/v1"
    assert openai_base("http://127.0.0.1:8781/v1/") == "http://127.0.0.1:8781/v1"


def test_presence_is_off_unless_asked_for():
    _, ran = start(ENV)
    assert ran["presence"] is None  # the deployed kernel refuses presence events


def test_presence_on_reads_the_daemons_face_tracker_without_waiting():
    calls, ran = start({**ENV, "SAGE_VOICE_PRESENCE": "1"})  # the emote read the first stamp
    face = ran["presence"]()
    assert face.fresh and face.detected
    assert ("face", False) in calls


def test_presence_is_unknown_when_the_detector_refused(monkeypatch):
    from reachy_sage_voice import executor as executors

    urls = []
    monkeypatch.setattr(executors, "daemon_detector",
                        lambda url: urls.append(url) or (lambda weight: False))
    _, ran = start({**ENV, "SAGE_VOICE_PRESENCE": "1"})
    assert urls == ["http://reachy-mini.lan:8000/api/media/tracking/enable"]
    ran["presence"](), ran["presence"]()
    assert not ran["presence"]().fresh


def test_presence_is_unknown_while_the_tracker_is_paused():
    _, ran = start({**ENV, "SAGE_VOICE_PRESENCE": "1"})
    ran["presence"](), ran["presence"]()
    ran["executor"].mini.start_head_tracking(weight=0.0)  # weight 0 stops the detector
    assert not ran["presence"]().fresh


def test_the_face_tracker_is_handed_to_the_voice_for_addressing():
    calls, ran = start({**ENV, "SAGE_VOICE_PRESENCE": "1"})
    assert ran["face"]() is True
    assert ("face", False) in calls


def test_a_face_with_the_detector_off_does_not_address():
    _, ran = start(ENV)  # not tracking: the detector is off
    assert [ran["face"]() for _ in range(3)] == [False] * 3


def test_the_executor_counts_a_face_only_while_the_detector_runs(monkeypatch):
    from reachy_sage_voice import executor as executors

    stamps = iter(range(1, 100))
    monkeypatch.setattr(executors, "daemon_face", lambda url: lambda: SimpleNamespace(
        detected=True, x=0.0, ts=float(next(stamps))))
    _, ran = start(ENV)
    assert not any(ran["executor"].face().detected for _ in range(3))
    _, ran = start({**ENV, "SAGE_VOICE_PRESENCE": "1"})
    ran["executor"].face()
    assert ran["executor"].face().detected


def test_face_health_is_down_when_the_detector_refused(monkeypatch):
    from reachy_sage_voice import executor as executors
    from reachy_sage_voice import health

    monkeypatch.setattr(health, "TABLE", {})
    monkeypatch.setattr(executors, "daemon_detector", lambda url: lambda weight: False)
    start({**ENV, "SAGE_VOICE_PRESENCE": "1"})
    assert not health.TABLE["voice.face"].ok and "refused" in health.TABLE["voice.face"].detail


def test_face_health_is_down_while_stale_up_once_fresh_and_not_reported_every_tick(monkeypatch):
    from reachy_sage_voice import health

    reports = []
    monkeypatch.setattr(health, "report", lambda *args: reports.append(args))
    _, ran = start({**ENV, "SAGE_VOICE_PRESENCE": "1"})  # the emote reads the first stamp
    for _ in range(10):
        ran["presence"]()
    assert [r for r in reports if r[0] == "voice.face"] == [("voice.face", False, "stale"), ("voice.face", True, "ok")]


@pytest.fixture(autouse=True)
def detector(monkeypatch):
    """The robot daemon's face detector, running: no HTTP from tests."""
    from reachy_sage_voice import executor as executors

    monkeypatch.setattr(executors, "daemon_detector", lambda url: lambda weight: True)


@pytest.fixture(autouse=True)
def beams(monkeypatch):
    """Record each robot whose mic beam was checked (and routed if off): no SSH from tests."""
    from reachy_sage_voice import supervise

    checked = []
    monkeypatch.setattr(supervise, "beam", checked.append, raising=False)
    return checked


def test_the_mic_beam_is_checked_at_connect_and_after_a_media_reset(beams):
    # A reboot resets the beam (R2): after it the voice connects again, or resets the media.
    start(ENV)  # it connects once, and its fake voice resets the media once
    assert beams == ["pollen@reachy-mini.lan"] * 2


@pytest.fixture(autouse=True)
def no_reset_wait(monkeypatch):
    from reachy_sage_voice import media_reset

    monkeypatch.setattr(media_reset, "RELEASE_WAIT_S", 0)


def test_a_quiet_mic_resets_the_robots_media_and_the_voice_reads_the_new_one():
    calls, ran = start(ENV)
    names = [c[0] for c in calls]
    assert names.index("release") < names.index("acquire") < names.index("rec")
    assert "track" not in names
    ran["media"].start_recording()  # the voice's media follows mini.media, now the new one
    assert calls[-1] == ("rec",)


def test_a_media_reset_restarts_face_tracking_when_presence_had_it_on():
    calls, _ = start({**ENV, "SAGE_VOICE_PRESENCE": "1"})
    names = [c[0] for c in calls]
    assert "track" in names[names.index("acquire"):]


def test_a_media_reset_sends_the_tracker_the_weight_it_had_not_full_tracking():
    calls = []

    async def run(*args, recover=None, executor=None, **kwargs):
        executor.mini.start_head_tracking(weight=0.0)  # a plan paused the tracker
        await recover(1)

    main(env={**ENV, "SAGE_VOICE_PRESENCE": "1"}, robot=lambda **kw: Robot(calls, **kw), run=run,
         moves=lambda: dict.fromkeys(EMOTIONS, "a move"), reach=lambda env: [])
    assert [c for c in calls if c[0] == "track"][-1] == ("track", 0.0)


def test_breathing_glances_and_thinking_cues_are_on_unless_switched_off():
    _, ran = start(ENV)
    assert ran["executor"].idle and ran["executor"].cues and ran["observers"] == []
    _, ran = start({**ENV, "SAGE_VOICE_IDLE": "0", "SAGE_VOICE_LISTEN_CUES": "0"})
    assert not ran["executor"].idle and not ran["executor"].cues


def test_a_lost_robot_link_after_a_media_reset_that_reached_the_robot_exits_nonzero(caplog):
    # Live 2026-10-22: deaf for two hours with no log; `make run` must visibly fail instead.
    # A reset the daemon took that still left the mic quiet is the one exit (LAYERS.md §1b row 8).
    calls = []

    async def run(*args, recover=None, **kwargs):
        await recover(1)
        raise RobotLinkLostError("no audio from the robot's mic for 5 s")

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as stopped:
        main(env=ENV, robot=lambda **kw: Robot(calls, **kw), run=run,
             moves=lambda: dict.fromkeys(EMOTIONS, "a move"), reach=lambda env: [])
    assert stopped.value.code not in (0, None)
    assert "robot link lost" in caplog.text and "mic" in caplog.text
    assert calls[-1] == ("disconnect",)


def test_a_motion_link_lost_long_after_a_reset_that_worked_reconnects(caplog):
    # Live 2026-10-28: the mic came back at 10:30, the robot shut down at 10:45, and the voice
    # exited saying a media reset "did not bring the mic back". Only a quiet mic is that.
    calls, runs = [], []

    async def run(*args, recover=None, **kwargs):
        runs.append(1)
        if len(runs) == 1:
            await recover(1)
            raise RobotLinkLostError("the robot's motion link: Lost connection with the server.")

    with caplog.at_level(logging.WARNING):
        main(env=ENV, robot=lambda **kw: Robot(calls, **kw), run=run,
             moves=lambda: dict.fromkeys(EMOTIONS, "a move"), reach=lambda env: [],
             sleep=lambda seconds: None)
    assert len(runs) == 2 and "reconnecting" in caplog.text


def test_tracking_sent_again_after_a_media_reset_stays_off_the_event_loop():
    # It can reconnect and asks the daemon over HTTP: the voice must not wait on either.
    threads = []

    class Where(Robot):
        def start_head_tracking(self, weight=1.0):
            threads.append(threading.current_thread())
            super().start_head_tracking(weight)

    async def run(*args, recover=None, **kwargs):
        await recover(1)

    main(env={**ENV, "SAGE_VOICE_PRESENCE": "1"}, robot=lambda **kw: Where([], **kw), run=run,
         moves=lambda: dict.fromkeys(EMOTIONS, "a move"), reach=lambda env: [])
    assert threads[-1] is not threading.main_thread()


def test_unreachable_robot_at_start_retries_not_exits():
    # Live: "No route to host" at start crash-looped the bot 26 times under launchd (R1).
    calls, tries, waits = [], [], []

    def robot(**kw):
        tries.append(kw)
        if len(tries) <= 3:
            raise OSError(65, "No route to host")
        return Robot(calls, **kw)

    async def run(*args, **kwargs):
        pass

    main(env=ENV, robot=robot, run=run, moves=lambda: dict.fromkeys(EMOTIONS, "a move"),
         reach=lambda env: [], sleep=waits.append)
    assert waits == [1.0, 2.0, 4.0]
    assert calls[0] == ("connect", {"host": "reachy-mini.lan", "connection_mode": "network"})


def test_a_link_lost_with_no_media_reset_reaching_the_robot_reconnects(caplog):
    # A robot reboot: the daemon is gone, so no reset reaches it; the voice must not exit (R1).
    calls, runs = [], []

    async def run(*args, **kwargs):
        runs.append(1)
        if len(runs) == 1:
            raise RobotLinkLostError("no audio from the robot's mic for 5 s")

    with caplog.at_level(logging.WARNING):
        main(env=ENV, robot=lambda **kw: Robot(calls, **kw), run=run,
             moves=lambda: dict.fromkeys(EMOTIONS, "a move"), reach=lambda env: [],
             sleep=lambda seconds: None)
    assert len(runs) == 2
    assert [c[0] for c in calls].count("connect") == 2
    assert "reconnecting" in caplog.text


#: The voice with a mic that stays live and a speaker that hangs when stopped, as a
#: robot call stuck on a dead link would; it says "ready" once the pipeline has started.
HUNG_STOP = textwrap.dedent("""
    import asyncio, time
    import numpy as np
    from reachy_sage_voice import pipeline
    from reachy_sage_voice.manifest import EMOTIONS

    class Media:
        def start_recording(self): print("ready", flush=True)
        def stop_recording(self): pass
        def get_audio_sample(self): return np.zeros((320, 2), dtype=np.float32)
        def start_playing(self): pass
        def stop_playing(self): time.sleep(60)
        def push_audio_sample(self, data): pass
        def get_output_audio_samplerate(self): return 16000
        def play_sound(self, path): pass

    class Move:
        duration, description = 3.0, "A move."

    async def emote(name, sound): pass

    env = {"SAGE_VOICE_KERNEL_URL": "http://127.0.0.1:9", "SAGE_VOICE_KERNEL_TOKEN": "t" * 32,
           "SAGE_VOICE_STT_URL": "http://127.0.0.1:9", "SAGE_VOICE_TTS_URL": "http://127.0.0.1:9"}
    asyncio.run(pipeline.run(Media(), emote, dict.fromkeys(EMOTIONS, Move()), env))
""")


def test_ctrl_c_stops_the_voice_even_when_shutting_down_hangs():
    # Live 2026-10-22: SIGINT did nothing; it took SIGTERM. Pipecat's runner took SIGINT
    # for a graceful cancel and swallowed every Ctrl-C after it, so a stuck cancel was final.
    child = subprocess.Popen([sys.executable, "-c", HUNG_STOP], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        child.send_signal(signal.SIGINT)
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.send_signal(signal.SIGINT)  # a second Ctrl-C must always stop it
        child.wait(timeout=10)
    finally:
        child.kill()


#: ``make run`` against a robot whose disconnect never returns, after the link is lost.
HUNG_DISCONNECT = textwrap.dedent("""
    import time
    from reachy_sage_voice import __main__ as voice
    from reachy_sage_voice.pipeline import RobotLinkLostError

    class Robot:
        media = None

        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *exc): time.sleep(60)
        def enable_motors(self): pass
        def wake_up(self): pass
        def start_head_tracking(self, weight): pass

    async def run(*args, **kwargs):
        raise RobotLinkLostError("no audio from the robot's mic for 5 s")

    voice.DISCONNECT_S = 0.5
    env = {"SAGE_VOICE_KERNEL_URL": "u", "SAGE_VOICE_KERNEL_TOKEN": "t",
           "SAGE_VOICE_STT_URL": "u", "SAGE_VOICE_TTS_URL": "u", "REACHY_MINI_HOST": "h"}
    voice.main(env=env, robot=Robot, run=run, moves=dict, reach=lambda env: [])
""")


def test_a_robot_that_will_not_disconnect_cannot_keep_the_voice_alive():
    done = subprocess.run([sys.executable, "-c", HUNG_DISCONNECT], capture_output=True,
                          text=True, timeout=20)
    assert done.returncode != 0
    assert "robot link lost" in done.stderr
