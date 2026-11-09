"""`make status`: up or down, with a reason, for everything the voice needs. No network.

The HTTP layer is a fake ``urlopen``: each URL answers JSON, an HTTP error, or
nothing at all.
"""

import io
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from reachy_sage_voice.status import main, render

NO_VOICE = Path("/nonexistent/reachy-sage-voice-motion.json")

ENV = {"REACHY_MINI_HOST": "reachy-mini.local"}
DAEMON = "http://reachy-mini.local:8000/api/daemon/status"
BEAM = "http://reachy-mini.local:8000/api/audio/config/parameter/AUDIO_MGR_OP_R"
UP = {
    DAEMON: {"state": "running", "backend_status": {"ready": True}, "error": None},
    BEAM: {"name": "AUDIO_MGR_OP_R", "values": [7, 3]},
    "http://127.0.0.1:8776": 404,  # a v1 kernel: no health route (and none in this table)
    "http://127.0.0.1:8781/health": {"ready": True, "enrolled": True},
    "http://127.0.0.1:8782/health": {"ready": True},
    "http://127.0.0.1:8783/health": {"ready": True},
    "http://127.0.0.1:11434/api/ps": {"models": [{"name": "qwen3:4b-instruct"}]},
}


def fake(answers, sent=None):
    """``urlopen`` from a table; ``sent`` collects each request's headers, by URL."""
    def get(url, timeout):
        if isinstance(url, urllib.request.Request):
            if sent is not None:
                sent[url.full_url] = dict(url.header_items())
            url = url.full_url
        answer = answers.get(url, urllib.error.URLError(ConnectionRefusedError(61, "refused")))
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            raise urllib.error.HTTPError(url, answer, "error", {}, io.BytesIO(b""))
        return io.BytesIO(json.dumps(answer).encode())
    return get


def status(answers, env=ENV, motion=NO_VOICE, sent=None):
    out = io.StringIO()
    code = main(env, fake(answers, sent), out, motion)
    lines = {line.split()[0]: line for line in out.getvalue().splitlines()}
    return code, lines


def test_all_up_exits_zero_and_says_what_each_is_doing():
    code, lines = status(UP)
    assert code == 0
    assert set(lines) == {"robot", "beam", "kernel", "stt", "tts", "research", "ollama"}
    assert all(line.split()[1] == "up" for line in lines.values())
    assert "mic beam: ASR on R" in lines["beam"]
    assert "enrolled" in lines["stt"]
    assert "qwen3:4b-instruct" in lines["ollama"]


def test_a_daemon_whose_backend_is_not_ready_is_down():
    code, lines = status({**UP, DAEMON: {"state": "running", "backend_status": {"ready": False},
                                         "error": None}})
    assert code == 1
    assert lines["robot"].split()[1] == "down" and "backend not ready" in lines["robot"]


def test_a_mic_whose_asr_beam_is_not_routed_is_down():
    # Recordings and STT need the ASR beam on R: make -C robot beam-on.
    code, lines = status({**UP, BEAM: {"name": "AUDIO_MGR_OP_R", "values": [8, 0]}})
    assert code == 1
    assert lines["beam"].split()[1] == "down" and "mic beam: not routed" in lines["beam"]


def test_a_daemon_in_error_is_down_with_its_error():
    code, lines = status({**UP, DAEMON: {"state": "error", "backend_status": None,
                                         "error": "motor 12 not found"}})
    assert code == 1 and "error" in lines["robot"] and "motor 12 not found" in lines["robot"]


def test_a_service_that_does_not_answer_is_down_with_why():
    answers = {k: v for k, v in UP.items() if ":8782" not in k}
    code, lines = status(answers)
    assert code == 1
    assert lines["tts"].split()[1] == "down" and "refused" in lines["tts"]


def test_a_service_still_loading_its_model_is_down():
    code, lines = status({**UP, "http://127.0.0.1:8781/health": {"ready": False}})
    assert code == 1 and "loading" in lines["stt"]


def test_research_without_its_searxng_is_a_warning_and_says_why():
    code, lines = status({**UP, "http://127.0.0.1:8783/health": {
        "ready": False, "searxng": "ConnectError('connection refused')"}})
    assert code == 0 and lines["research"].split()[1] == "warn"  # optional: songs only
    assert "searxng" in lines["research"] and "connection refused" in lines["research"]
    assert "loading" not in lines["research"]


def test_an_stt_without_a_voiceprint_is_up_and_says_so():
    code, lines = status({**UP, "http://127.0.0.1:8781/health": {"ready": True, "enrolled": False}})
    assert code == 0 and "not enrolled" in lines["stt"]


def test_ollama_up_with_no_model_loaded_says_so():
    code, lines = status({**UP, "http://127.0.0.1:11434/api/ps": {"models": []}})
    assert code == 0 and "no model loaded" in lines["ollama"]


def test_without_the_robots_address_the_robot_is_down():
    code, lines = status(UP, env={})
    assert code == 1 and "REACHY_MINI_HOST" in lines["robot"]


def test_the_voices_own_urls_are_checked_when_set():
    env = {**ENV, "SAGE_VOICE_STT_URL": "http://127.0.0.1:9781/v1"}
    code, lines = status({**UP, "http://127.0.0.1:9781/health": {"ready": True, "enrolled": True}},
                         env)
    assert code == 0 and "9781" in lines["stt"]


def heartbeat(path, layers, at=None, pid=None):
    path.write_text(json.dumps({"pid": pid or os.getpid(), "at": at or time.time(),
                                "layers": layers}))
    return path


def test_render_says_each_layer_and_how_long_it_has_been_so():
    beat = {"at": 100.0, "layers": {
        "voice.tts": {"layer": "voice.tts", "ok": False, "since": 90.0, "detail": "down"},
        "voice.robot": {"layer": "voice.robot", "ok": True, "since": 100.0 - 300,
                        "detail": "connected"}}}
    ok, lines = render(beat, now=101.0, running=True)
    assert not ok
    assert [line.split()[:2] for line in lines] == [["voice.robot", "up"], ["voice.tts", "down"]]
    assert lines[0].endswith("connected, for 5 min") and lines[1].endswith("down, for 11 s")


def test_render_says_a_stale_heartbeat_is_a_voice_not_running():
    ok, lines = render({"at": 100.0, "layers": {}}, now=160.0, running=True)
    assert ok and lines == ["voice        stale last heartbeat 1 min ago: the voice is not running"]
    ok, lines = render({"at": 100.0, "layers": {}}, now=101.0, running=False)
    assert ok and "not running" in lines[0]


def test_a_running_voice_with_one_layer_down_exits_one(tmp_path):
    # Live 2026-10-23: the SDK refused every command ("Lost connection with the server.").
    layers = {"voice.motion": {"layer": "voice.motion", "ok": False, "since": time.time(),
                               "detail": "the SDK refuses commands, 3 stalls"}}
    code, lines = status(UP, motion=heartbeat(tmp_path / "beat.json", layers))
    assert code == 1
    assert lines["voice.motion"].split()[1] == "down" and "3 stalls" in lines["voice.motion"]
    layers["voice.motion"]["ok"] = True
    code, lines = status(UP, motion=heartbeat(tmp_path / "beat.json", layers))
    assert code == 0 and lines["voice.motion"].split()[1] == "up"


def test_a_stale_heartbeat_says_so_and_is_not_a_failure(tmp_path):
    layers = {"voice.tts": {"layer": "voice.tts", "ok": False, "since": 0, "detail": "down"}}
    code, lines = status(UP, motion=heartbeat(tmp_path / "beat.json", layers, pid=2 ** 22 + 12345))
    assert code == 0 and "voice.tts" not in lines
    assert lines["voice"].split()[1] == "stale" and "not running" in lines["voice"]


HEALTH = "http://127.0.0.1:8776/v1/health"


def test_a_v11_kernel_is_judged_by_its_health_route_with_the_token():
    sent = {}
    health = {"ok": True, "protocol": 1, "features": ["acted", "health"]}
    code, lines = status({**UP, HEALTH: health}, {**ENV, "SAGE_VOICE_KERNEL_TOKEN": "t" * 32},
                         sent=sent)
    assert code == 0 and lines["kernel"].split()[1] == "up"
    assert "protocol 1" in lines["kernel"] and "acted, health" in lines["kernel"]
    assert sent[HEALTH]["Authorization"] == "Bearer " + "t" * 32


def test_a_kernel_without_health_falls_back_to_any_answer():
    for health in (404, 401):  # a v1 kernel; a token this check does not hold
        code, lines = status({**UP, HEALTH: health})
        assert code == 0 and "answering (HTTP 404)" in lines["kernel"]


def test_a_kernel_that_does_not_answer_is_down():
    code, lines = status({k: v for k, v in UP.items() if ":8776" not in k})
    assert code == 1 and lines["kernel"].split()[1] == "down"


def test_an_optional_layer_down_is_a_warning_and_a_required_one_fails(tmp_path):
    """Deploy runs status: an optional layer down warns, a required one fails.

    The face is optional (tracking and watch off, or nobody in view); robot, beam, stt, tts and
    kernel are required.
    """
    layers = {"voice.face": {"layer": "voice.face", "ok": False, "since": time.time(),
                             "detail": "stale"}}
    code, lines = status(UP, motion=heartbeat(tmp_path / "beat.json", layers))
    assert code == 0 and lines["voice.face"].split()[1] == "warn"
    layers["voice.kernel"] = {"layer": "voice.kernel", "ok": False, "since": time.time(),
                              "detail": "no answer"}
    code, lines = status(UP, motion=heartbeat(tmp_path / "beat.json", layers))
    assert code == 1 and lines["voice.kernel"].split()[1] == "down"
