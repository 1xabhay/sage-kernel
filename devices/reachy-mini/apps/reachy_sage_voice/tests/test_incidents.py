"""Things that went wrong once at the voice, that no other test would catch again.

Each test names the date and what was seen. One still open is a strict expected failure: when it
is fixed its test turns red until the mark is taken off. The kernel's and the repo's are in the
monorepo's tests/test_incidents.py; an unknown command (2026-11-09) is test_behaviour's B40.
"""

import asyncio
import math
import os
import subprocess
import threading

import pytest
import yaml
from fake_mini import FakeMini, head_yaw
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.tests.utils import SleepFrame, run_test
from reachy_mini.utils import create_head_pose
from test_compose import COMPOSE, FAKE_DOCKER, in_checkout
from test_executor import alive, cue_moves, quick, voice_at  # noqa: F401 - quick is a fixture
from test_run import ENV, Robot
from test_tts import line, ndjson, tts

from reachy_sage_voice import meter
from reachy_sage_voice.__main__ import main
from reachy_sage_voice.manifest import EMOTIONS


def test_a_stale_face_detector_is_turned_off_then_on_not_sent_the_same_weight_again(monkeypatch):
    """2026-11-10: the face detector was dead for three to four hours.

    The camera had restarted, and asking again for the tracking weight it already had did
    nothing on the daemon. Off, then on, revives it.
    """
    from reachy_sage_voice import executor as executors
    from reachy_sage_voice import supervise
    from reachy_sage_voice.perception import face

    weights, back = [], threading.Event()

    def detector(weight):
        weights.append(weight)
        if len(weights) == 3:
            back.set()
        return True

    class Frozen(Robot):  # the daemon's face stamp never moves: a dead detector
        def get_tracked_face(self, wait=True):
            return type("Face", (), {"detected": False, "x": None, "ts": 5.0})()

    async def run(*args, presence=None, **kwargs):
        presence(), presence()  # stale, and still stale once the wait (none here) is over

    every = face.rearmer
    monkeypatch.setattr(face, "rearmer", lambda rearm: every(rearm, every=0.0))
    monkeypatch.setattr(executors, "daemon_detector", lambda url: detector)
    monkeypatch.setattr(executors, "daemon_face", lambda url: lambda: None)  # no HTTP from tests
    monkeypatch.setattr(supervise, "beam", lambda ssh: None, raising=False)  # nor SSH
    main(env=ENV, robot=lambda **kw: Frozen([], **kw), run=run,
         moves=lambda: dict.fromkeys(EMOTIONS, "a move"), reach=lambda env: [])
    assert back.wait(5)
    assert weights == [executors.WATCH_WEIGHT, 0.0, executors.WATCH_WEIGHT]


def test_a_glance_toward_a_voice_starts_from_where_the_head_already_points(quick):  # noqa: F811
    """2026-11-10 (docs review): the glance at a voice ignored where the head was.

    The microphones are at the top of the head and turn with it, so a voice 20 degrees left of a
    head already turned 20 degrees is 40 degrees from the body; the glance went to 20.
    """
    mini = FakeMini()
    mini.head = create_head_pose(yaw=20, degrees=True)

    async def hear():
        alive(mini, doa=voice_at(math.radians(70))).attend()
        await asyncio.sleep(0.1)
    asyncio.run(hear())
    assert math.degrees(head_yaw(cue_moves(mini)[0][2])) == pytest.approx(40, abs=1)


def test_a_clause_the_real_tts_service_speaks_is_one_the_pet_scorecard_counts(http_server):
    """Found 2026-11-13: since Pipecat 1.15 the scorecard counts nothing Sage says.

    Its pattern keys on a Pipecat log line whose function was renamed, test_pet's lines are
    written by hand in the old form, and so `make pet` passed "replied" at 0%.
    """
    from loguru import logger

    http_server.script("POST", "/dev/captioned_speech", ndjson(
        line(0.2, [("Fine", 0.0, 0.1), ("by", 0.1, 0.15), ("me.", 0.15, 0.2)])))
    lines = []
    sink = logger.add(lines.append, level="DEBUG",  # loguru's own layout, as on stderr
                      format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
                             "{name}:{function}:{line} - {message}")
    try:
        asyncio.run(run_test(tts(http_server), frames_to_send=[
            TTSSpeakFrame("Fine by me."), SleepFrame(0.2)]))
    finally:
        logger.remove(sink)
    assert any("Generating TTS [Fine by me.]" in text for text in lines)  # Pipecat still says it
    said = [m["text"] for m in map(meter.PATTERNS["say"].search, lines) if m]
    assert said == ["Fine by me."]


@pytest.mark.skipif(not in_checkout(), reason="deploy archives a commit: needs the git checkout")
def test_a_services_deploy_stops_the_live_stt_before_it_tests_the_new_one(tmp_path):
    """2026-11-09: a second STT beside the live one ran the 4 GiB VM out of memory.

    dockerd died with it, and the owner's voiceprint then dropped the test clip. In what
    docker is really asked: the live STT stops first, and no test run reads the voiceprint.
    """
    bin = tmp_path / "bin"
    bin.mkdir()
    (bin / "docker").write_text(FAKE_DOCKER)
    (bin / "curl").write_text('#!/bin/sh\necho \'{"ready":true,"enrolled":false}\'\n')
    for tool in bin.iterdir():
        tool.chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MAKE", "MFLAGS", "SAGE_", "DEPLOY_")) and k != "SHA"}
    env.update(PATH=f"{bin}:{env['PATH']}", HOME=str(tmp_path), UP_OK="1",
               FAKE_LOG=str(tmp_path / "docker.log"))
    done = subprocess.run(["make", "-s", "deploy", "SHA=HEAD", f"DEPLOY_LOG={tmp_path / 'log'}"],
                          cwd=COMPOSE.parent, env=env, capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stderr
    calls = (tmp_path / "docker.log").read_text().splitlines()
    tests = [n for n, call in enumerate(calls) if "unittest" in call]
    (stopped,) = [n for n, call in enumerate(calls) if call.endswith("compose stop stt")]
    assert tests and stopped < tests[0]
    assert all("-e SAGE_VOICEPRINT=/nonexistent" in calls[n] for n in tests)
    assert next(n for n, call in enumerate(calls) if " up " in call) > tests[-1]


def test_searxng_starts_from_its_own_defaults_so_no_setting_it_reads_is_missing():
    """2026-10-27: SearXNG's /healthz died on KeyError 'default_doi_resolver'; no song research.

    Our settings file names a few keys; every other key the pinned image reads has to come from
    its defaults. Booting the image with the baked file needs Docker and is not tested here.
    """
    settings = yaml.safe_load((COMPOSE.parent / "searxng" / "settings.yml").read_text())
    assert settings["use_default_settings"] is True
