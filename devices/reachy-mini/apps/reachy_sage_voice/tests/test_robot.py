"""A dropped command websocket heals itself: one fresh client, motors and the asked weight back.

The SDK's WSClient never reconnects: once its keepalive ping times out every
command raises ConnectionError("Lost connection with the server.") for good.
"""

import asyncio
import threading
import time

import pytest

from reachy_sage_voice.robot import Reconnecting


class Client:
    def __init__(self, alive=True):
        self.host, self.port, self.alive, self.closed = "reachy-mini.lan", 8000, alive, False

    def disconnect(self):
        self.closed = True

    def is_connected(self):
        return self.alive


class Mini:
    def __init__(self):
        self.client, self.calls, self.media = Client(alive=False), [], object()

    def _send(self, name):
        if not self.client.alive:
            raise ConnectionError("Lost connection with the server.")
        self.calls.append(name)
        return name

    def goto_target(self):
        return self._send("goto")

    def enable_motors(self):
        self._send("enable_motors")

    def start_head_tracking(self, weight=1.0):
        self._send(("track", weight))

    async def async_play_move(self, move):
        self._send(("move", move))

    def get_tracked_face(self, wait=True):  # the cached status: never raises, may be stale
        return type("Face", (), {"detected": True})()


def robot(now=lambda: 100.0):
    mini, clients = Mini(), []

    def connect(host, port):
        clients.append((host, port))
        return Client()

    return Reconnecting(mini, connect=connect, now=now), mini, clients


def test_a_lost_connection_reconnects_once_rearms_the_robot_and_retries():
    pet, mini, clients = robot()
    old = mini.client
    assert pet.goto_target() == "goto"
    assert clients == [("reachy-mini.lan", 8000)]
    assert mini.calls == ["enable_motors", "goto"]  # following a face is a command (owner)
    deadline = time.monotonic() + 5  # it closes on its own thread
    while not old.closed and time.monotonic() < deadline:
        time.sleep(0.001)
    assert old.closed


def test_a_reconnect_does_not_wait_for_the_dead_client_to_close():
    # Live 2026-10-27: reconnects took 11-12 s, 10 of them in websockets' close_timeout (the robot
    # logs the old socket closing 10 s after the new one opens), the motion runner waiting.
    release = threading.Event()

    class Stuck(Client):
        def disconnect(self):
            release.wait(5)
            self.closed = True

    pet, mini, _ = robot()
    mini.client = stuck = Stuck(alive=False)
    assert pet.goto_target() == "goto"
    assert not stuck.closed and mini.client is not stuck
    release.set()


def test_a_reconnect_sends_the_weight_last_asked_for_not_full_tracking():
    pet, mini, _ = robot()
    mini.client.alive = True
    pet.start_head_tracking(weight=0.3)
    mini.client.alive = False
    assert pet.goto_target() == "goto"
    assert mini.calls == [("track", 0.3), "enable_motors", ("track", 0.3), "goto"]


def test_an_emote_reconnects_then_is_skipped_since_a_late_emote_is_wrong():
    pet, mini, clients = robot()
    assert asyncio.run(pet.async_play_move("cheerful1")) is None
    assert len(clients) == 1 and mini.calls == ["enable_motors"]


def test_a_face_read_on_a_dead_connection_reconnects_and_reports_no_face():
    pet, mini, clients = robot()
    assert pet.get_tracked_face(wait=False).detected is False
    assert len(clients) == 1 and mini.calls == ["enable_motors"]
    assert pet.get_tracked_face(wait=False).detected is True


def test_at_most_one_reconnect_per_ten_seconds():
    clock = [100.0]
    pet, mini, clients = robot(now=lambda: clock[0])
    pet.goto_target()
    mini.client.alive = False
    clock[0] += 5
    with pytest.raises(ConnectionError):
        pet.goto_target()
    clock[0] += 6
    assert pet.goto_target() == "goto" and len(clients) == 2


def test_everything_else_passes_through():
    pet, mini, _ = robot()
    assert pet.media is mini.media


def test_a_detector_that_refuses_is_remembered_and_asked_only_at_a_weight_above_0():
    asked = []
    mini = Mini()
    mini.client.alive = True
    pet = Reconnecting(mini, detector=lambda weight: asked.append(weight) or False)
    pet.start_head_tracking(weight=0.0)
    assert asked == [] and not pet.refused
    pet.start_head_tracking(weight=1.0)
    assert asked == [1.0] and pet.refused


def test_a_rising_weight_is_a_ramp_step_the_detector_already_answered():
    asked = []
    mini = Mini()
    mini.client.alive = True
    pet = Reconnecting(mini, detector=lambda weight: asked.append(weight) or False)
    pet.start_head_tracking(weight=0.05)
    assert pet.refused
    for weight in (0.1, 0.2, 1.0):  # an HTTP read each would stall the runner's one worker
        pet.start_head_tracking(weight=weight)
    assert asked == [0.05] and pet.refused and pet.tracking == 1.0
    pet.start_head_tracking(weight=1.0)  # a media reset says it again: the camera restarted
    assert asked == [0.05, 1.0]
