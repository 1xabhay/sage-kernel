"""The robot's media rebuilt from scratch when its mic goes dead, with a fake robot."""

import asyncio
import logging

from reachy_sage_voice import media_reset
from reachy_sage_voice.media_reset import LiveMedia, reset


class Media:
    def __init__(self, calls, name):
        self.calls, self.name = calls, name

    def start_recording(self):
        self.calls.append(("start_recording", self.name))

    def start_playing(self):
        self.calls.append(("start_playing", self.name))


class Mini:
    """ReachyMini's release/acquire: acquire builds a fresh MediaManager (a new WebRTC client)."""

    def __init__(self, acquires=True, release_error=None):
        self.calls, self.acquires, self.release_error = [], acquires, release_error
        self.media, self.media_released, self._made = Media(self.calls, "old"), False, 0

    def release_media(self):
        self.calls.append(("release",))
        if self.release_error:
            raise self.release_error
        self.media_released = True

    def acquire_media(self):
        self.calls.append(("acquire",))
        if self.acquires:
            self._made += 1
            self.media, self.media_released = Media(self.calls, f"new{self._made}"), False


def go(mini, attempt):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        mini.calls.append(("wait", seconds))

    asyncio.run(reset(mini, attempt, sleep=sleep))
    return waits


def test_a_reset_releases_waits_acquires_then_restarts_the_new_media(caplog):
    mini = Mini()
    with caplog.at_level(logging.WARNING):
        go(mini, 1)
    assert mini.calls == [("release",), ("wait", media_reset.RELEASE_WAIT_S), ("acquire",),
                          ("start_recording", "new1"), ("start_playing", "new1")]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) >= 3 and all("mic recovery 1" in r.getMessage() for r in warnings)


def test_each_attempt_waits_longer_before_acquiring():
    assert go(Mini(), 1) == [media_reset.RELEASE_WAIT_S]
    assert go(Mini(), 3) == [4 * media_reset.RELEASE_WAIT_S]


def test_a_refused_acquire_restarts_nothing(caplog):
    mini = Mini(acquires=False)
    with caplog.at_level(logging.WARNING):
        go(mini, 1)
    assert not [c for c in mini.calls if c[0].startswith("start")]
    assert "acquire" in caplog.text


def test_a_failing_reset_is_logged_not_raised(caplog):
    mini = Mini(release_error=OSError("connection refused"))
    with caplog.at_level(logging.WARNING):
        go(mini, 2)
    assert "connection refused" in caplog.text


def test_live_media_always_reaches_the_robots_current_media():
    mini = Mini()
    media = LiveMedia(mini)
    assert media.name == "old"
    go(mini, 1)
    assert media.name == "new1"


def test_a_reset_says_whether_the_media_came_back():
    # Only a reset that came back counts as one tried: a quiet mic after it ends the run (R1).
    async def no_wait(seconds):
        pass

    assert asyncio.run(reset(Mini(), 1, sleep=no_wait)) is True
    assert asyncio.run(reset(Mini(acquires=False), 1, sleep=no_wait)) is False
    assert asyncio.run(reset(Mini(release_error=OSError("no route")), 1, sleep=no_wait)) is False
