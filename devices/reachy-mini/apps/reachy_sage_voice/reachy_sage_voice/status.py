"""``make status``: is everything the voice needs up? One line each, with why.

    robot     the daemon at REACHY_MINI_HOST:8000: running, backend ready
    beam      the mic chip's ASR beam routed to the right channel (chip.py)
    kernel    SAGE_VOICE_KERNEL_URL /v1/health with SAGE_VOICE_KERNEL_TOKEN (v1.1): its protocol
              and features; else, as for a v1 kernel or no token, any HTTP answer at the root
    stt       SAGE_VOICE_STT_URL /health: ready, and whether a voiceprint is enrolled
    tts       SAGE_VOICE_TTS_URL /health: ready
    research  SAGE_VOICE_RESEARCH_URL /health: ready
    ollama    127.0.0.1:11434 /api/ps: which model is loaded
    voice.*   the running voice's own view, one line per layer (robot link, beam, kernel,
              stt, tts, motion) from its heartbeat (``MOTION_FILE``, health.py), with how
              long each has been so; a stale heartbeat says the voice is not running

Unset service URLs mean this machine's usual ports. Exits 1 if any is down.
Stdlib only, so it answers even when the app's environment is broken.
"""

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO

TIMEOUT_S = 3.0
LOCAL = {"SAGE_VOICE_KERNEL_URL": "http://127.0.0.1:8776",
         "SAGE_VOICE_STT_URL": "http://127.0.0.1:8781",
         "SAGE_VOICE_TTS_URL": "http://127.0.0.1:8782",
         "SAGE_VOICE_RESEARCH_URL": "http://127.0.0.1:8783"}
OLLAMA = "http://127.0.0.1:11434/api/ps"
#: The running voice's heartbeat: each layer's health (health.py), every 2 s.
MOTION_FILE = Path(tempfile.gettempdir()) / "reachy-sage-voice-motion.json"
#: A heartbeat older than this is a voice that stopped (or hung).
STALE_S = 10.0


def write_heartbeat(path: Path, layers: Mapping[str, dict]) -> None:
    """Record each layer's health for ``make status``, as this process; never half a file."""
    part = path.with_suffix(".part")
    part.write_text(json.dumps({"pid": os.getpid(), "at": time.time(), "layers": layers}))
    os.replace(part, path)


def read_heartbeat(path: Path) -> dict | None:
    """Read the heartbeat; None with none written."""
    try:
        beat = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return beat if isinstance(beat, dict) else None


def _running(pid) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, TypeError):
        return False
    return True


def _ago(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} s"
    return f"{seconds / 60:.0f} min" if seconds < 3600 else f"{seconds / 3600:.0f} h"


def render(beat: dict, now: float, running: bool) -> tuple[bool, list[str]]:
    """One line per layer and how long it has been so; False if any is down.

    A stale heartbeat is one line saying the voice is not running, and not a failure.
    """
    at = beat.get("at", 0)
    if not running or now - at > STALE_S:
        return True, [f"{'voice':<12} {'stale':<5} last heartbeat {_ago(now - at)} ago: "
                      "the voice is not running"]
    layers = [beat["layers"][name] for name in sorted(beat.get("layers", {}))]
    return all(layer["ok"] for layer in layers), [
        f"{layer['layer']:<12} {'up' if layer['ok'] else 'down':<5} {layer['detail']}, "
        f"for {_ago(now - layer['since'])}" for layer in layers]


def _root(url: str) -> str:
    return url.rstrip("/").removesuffix("/v1")


def _robot(answer: dict) -> tuple[bool, str]:
    state, error = answer.get("state"), answer.get("error")
    why = f": {error}" if error else ""
    if state != "running":
        return False, f"daemon {state}{why}"
    if not (answer.get("backend_status") or {}).get("ready"):
        return False, f"daemon running, backend not ready{why}"
    return True, "daemon running, backend ready"


def _beam(answer: dict) -> tuple[bool, str]:
    """The ASR beam on the right channel (7,3), for recordings and STT: make -C robot beam-on."""
    if answer["values"] == [7, 3]:
        return True, "mic beam: ASR on R"
    return False, f"mic beam: not routed (R {answer['values']}; make -C robot beam-on)"


def _ready(answer: dict) -> tuple[bool, str]:
    return (True, "ready") if answer.get("ready") else (False, "loading its model")


def _research(answer: dict) -> tuple[bool, str]:
    return (True, "ready") if answer.get("ready") else (False, f"searxng: {answer.get('searxng')}")


def _stt(answer: dict) -> tuple[bool, str]:
    up, why = _ready(answer)
    voice = "owner enrolled" if answer.get("enrolled") else "owner not enrolled: anyone barges in"
    return up, f"{why}, {voice}" if up else why


def _kernel(answer: dict) -> tuple[bool, str]:
    features = ", ".join(answer.get("features", [])) or "none"
    return bool(answer.get("ok")), f"protocol {answer.get('protocol')}, features: {features}"


def _ollama(answer: dict) -> tuple[bool, str]:
    names = [model["name"] for model in answer.get("models", [])]
    return True, "loaded: " + ", ".join(names) if names else "up, no model loaded"


def check(url: str, read: Callable[[dict], tuple[bool, str]] | None,
          get: Callable) -> tuple[bool, str]:
    """GET ``url`` and judge its JSON with ``read``; with no ``read``, any HTTP answer is up."""
    try:
        with get(url, timeout=TIMEOUT_S) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        return (True, f"answering (HTTP {error.code})") if read is None else (
            False, f"HTTP {error.code}")
    except urllib.error.URLError as error:
        return False, f"no answer: {error.reason}"
    except OSError as error:  # timed out, reset
        return False, f"no answer: {error}"
    if read is None:
        return True, "answering"
    try:
        return read(json.loads(body))
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return False, f"unexpected answer: {error}"


def main(env: Mapping[str, str] = os.environ, get: Callable = urllib.request.urlopen,
         out: TextIO = sys.stdout, motion: Path = MOTION_FILE) -> int:
    """Print each check; return 1 if any is down."""
    def url(name: str) -> str:
        return env.get(name) or LOCAL[name]

    host = env.get("REACHY_MINI_HOST")
    kernel = _root(url("SAGE_VOICE_KERNEL_URL"))
    health = urllib.request.Request(kernel + "/v1/health", headers={
        "Authorization": f"Bearer {env.get('SAGE_VOICE_KERNEL_TOKEN', '')}"})
    checks = [
        ("stt", _root(url("SAGE_VOICE_STT_URL")) + "/health", _stt),
        ("tts", _root(url("SAGE_VOICE_TTS_URL")) + "/health", _ready),
        ("research", _root(url("SAGE_VOICE_RESEARCH_URL")) + "/health", _research),
        ("ollama", OLLAMA, _ollama),
    ]
    results = [("robot", "REACHY_MINI_HOST", (False, "REACHY_MINI_HOST not set"))]
    if host:
        daemon = f"http://{host}:8000/api/daemon/status"
        beam = f"http://{host}:8000/api/audio/config/parameter/AUDIO_MGR_OP_R"
        results = [("robot", daemon, check(daemon, _robot, get)), ("beam", beam, check(beam, _beam, get))]
    if (judged := check(health, _kernel, get))[0]:
        results.append(("kernel", health.full_url, judged))
    else:  # a v1 kernel has no health route, and without the token it answers 401
        results.append(("kernel", kernel, check(kernel, None, get)))
    results += [(name, where, check(where, read, get)) for name, where, read in checks]
    for name, where, (up, why) in results:
        print(f"{name:<12} {'up' if up else 'down':<5} {why}  ({where})", file=out)
    voice = True
    if (beat := read_heartbeat(motion)) is not None:
        voice, lines = render(beat, time.time(), _running(beat.get("pid")))
        print("\n".join(lines), file=out)
    return 0 if voice and all(up for _, _, (up, _) in results) else 1


if __name__ == "__main__":
    sys.exit(main())
