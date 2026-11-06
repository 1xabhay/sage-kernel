"""v2v_ms from the bot's log (LAYERS.md §0): every line stamped, then scored per turn and lane.

The scorer's own tests, on synthetic lines, are in devices/common/sage_device; here the stamps
are checked on a real handler, on the processor's own turns, and on ``make run``'s logging.
"""

import contextvars
import io
import logging
import subprocess
import sys
import time

import pytest
from pipecat.frames.frames import BotStartedSpeakingFrame, VADUserStoppedSpeakingFrame
from pipecat.tests.utils import SleepFrame
from sage_device import latency
from sage_device.latency import LEAD_MS
from test_commands import FakeExecutor, make
from test_sage_processor import FakeSession, delta, heard, run


@pytest.fixture
def stamped():
    """Stamp one logger's lines into a string, as ``make run`` stamps them on stderr."""
    added = []

    def stamp(name: str) -> tuple[logging.Logger, io.StringIO]:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter(latency.FORMAT))
        handler.addFilter(latency.stamp)
        log = logging.getLogger(name)
        log.addHandler(handler)
        added.append((log, handler))
        return log, stream
    yield stamp
    for log, handler in added:
        log.removeHandler(handler)


def test_every_line_has_wall_time_monotonic_time_and_the_turn(caplog, stamped):
    caplog.set_level(logging.INFO, logger="test_latency")
    log, stream = stamped("test_latency")
    before = time.monotonic()
    log.info("between turns")

    def during():
        latency.TURN_ID.set("reachy-x")
        log.info("in a turn")

    contextvars.copy_context().run(during)
    lines = [latency.LINE.match(line) for line in stream.getvalue().splitlines()]
    assert [m["turn"] for m in lines] == ["-", "reachy-x"]
    assert all(before - 0.001 <= float(m["mono"]) <= time.monotonic() + 0.001 for m in lines)
    assert lines[0]["wall"].startswith(time.strftime("%Y-%m-%d"))
    assert lines[1]["message"] == "in a turn"


def test_the_processors_turns_are_stamped_and_scored_per_lane(caplog, stamped):
    caplog.set_level(logging.INFO, logger="reachy_sage_voice.sage")
    _, stream = stamped("reachy_sage_voice.sage")
    session = FakeSession([{"effects": [delta(0, "Once upon a time")]}])
    voice_end = [VADUserStoppedSpeakingFrame(stop_secs=0.2), SleepFrame(0.05)]
    run(make(session, FakeExecutor()),
        [*voice_end, heard("Tell me a story."), SleepFrame(0.05), BotStartedSpeakingFrame(),
         SleepFrame(0.05), *voice_end, heard("Reachy, look for me."), SleepFrame(0.05),
         BotStartedSpeakingFrame(), SleepFrame(0.05)])
    lines = stream.getvalue().splitlines()
    kernel, reflex = latency.turns(lines)
    assert kernel.turn_id == session.started[0][0] and kernel.lane == "kernel"
    assert reflex.lane == "reflex" and reflex.turn_id != kernel.turn_id
    for turn in (kernel, reflex):  # 0.2 s of stop_secs, 0.05 s of STT, 0.05 s to the voice
        assert 300 + LEAD_MS <= turn.v2v_ms < 1000 + LEAD_MS
    ids = (kernel.turn_id, reflex.turn_id)
    assert all(latency.LINE.match(line)["turn"] in ids for line in lines)


def test_make_run_stamps_every_line():
    code = ("from reachy_sage_voice.__main__ import configure_logging; configure_logging(); "
            "import logging; logging.getLogger('reachy_sage_voice.x').info('hello')")
    err = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stderr
    match = latency.LINE.match(err.strip())
    assert match and match["turn"] == "-" and match["message"] == "hello"


def test_the_media_managers_polled_warning_is_throttled_and_its_others_are_not():
    """The mic polls every 5 ms: 29k of these lines in one log on 2026-10-27."""
    code = ("from reachy_sage_voice.__main__ import configure_logging; configure_logging(); "
            "import logging; log = logging.getLogger('reachy_mini.media.media_manager'); "
            "[log.warning('Audio system is not initialized.') for _ in range(100)]; "
            "[log.warning('something else') for _ in range(2)]")
    err = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stderr
    assert err.count("Audio system is not initialized.") == 1
    assert err.count("something else") == 2
