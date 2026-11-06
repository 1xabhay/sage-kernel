"""Run Reachy's Sage voice on the robot: ``make run`` (``python -m reachy_sage_voice``).

Everything comes from the environment (see pipeline.py, plus REACHY_MINI_HOST),
filled in from a .env (``load_settings``), and every address must be set:
nothing falls back to a network default. The motors are enabled before the
robot wakes; ``wake_up()`` alone leaves the head limp after a reboot
(the pet learned this, live.py ``wake``).

A robot that does not answer is retried forever, at start and after a lost link
(supervise.py): a reboot must not end the voice. A quiet mic first gets media resets
(media_reset.py); one the robot took that still left the mic quiet ends the run with an
ERROR and a nonzero exit, so ``make run`` visibly fails and launchd restarts it
(``MediaResetFailedError``). Disconnecting from a dead robot can hang in GStreamer,
out of reach of Ctrl-C, so it gets ``DISCONNECT_S``.
"""

import asyncio
import contextlib
import functools
import hashlib
import logging
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

HOST = "REACHY_MINI_HOST"
DAEMON_PORT = 8000  # the robot daemon's HTTP API (ReachyMini's default port)
#: reachy_mini warns on every mic poll (5 ms) while its media is down.
MEDIA_DOWN_LOG_EVERY_S = 30.0
#: Longest the robot's disconnect may take before the process exits without it.
DISCONNECT_S = 10.0


class MediaResetFailedError(Exception):
    """The robot took a media reset and its mic stayed quiet: only a restart is left."""


def env_files() -> list[Path]:
    """Name the .env files, first wins: beside the daemon's config, then the app's own.

    Pollen's conversation app reads its instance .env in ``user_config_dir("reachy_mini")``
    too, but then the nearest .env above the CWD; here that walk would reach the monorepo's
    root .env, the kernel's secrets, so only this app's folder counts (``make run``).
    """
    from platformdirs import user_config_dir

    return [Path(user_config_dir("reachy_mini"), "sage_voice", ".env"),
            Path(__file__).resolve().parents[1] / ".env"]


def load_settings() -> None:
    """Fill the settings the environment leaves unset from the .env files; the environment wins."""
    from dotenv import load_dotenv

    for path in env_files():
        if path.is_file():
            load_dotenv(path, override=False)
            logger.info("settings from %s", path)


def unreachable(env: Mapping[str, str], get: Callable = urllib.request.urlopen) -> list[str]:
    """Name the kernel, STT and TTS settings whose URL nothing answers at.

    Any HTTP answer counts: the kernel has no health route and turns a bare GET
    away, and a service still loading its model is up.
    """
    def root(url: str) -> str:
        url = url.rstrip("/")
        return url.removesuffix("/v1")

    urls = {"SAGE_VOICE_KERNEL_URL": env["SAGE_VOICE_KERNEL_URL"].rstrip("/"),
            "SAGE_VOICE_STT_URL": root(env["SAGE_VOICE_STT_URL"]) + "/health",
            "SAGE_VOICE_TTS_URL": root(env["SAGE_VOICE_TTS_URL"]) + "/health"}
    down = []
    for name, url in urls.items():
        try:
            with get(url, timeout=5):
                pass
        except urllib.error.HTTPError:
            pass
        except OSError:  # URLError, refused, timed out
            down.append(name)
    return down


def check(env: Mapping[str, str], required: tuple[str, ...], reach: Callable | None) -> None:
    """Exit with what to fix if a setting is missing or a service is not answering."""
    missing = [name for name in required if not env.get(name)]
    if missing:
        sys.exit("set " + ", ".join(missing) + " (see README.md, 'Settings')")
    down = (reach or unreachable)(env)
    if down:
        sys.exit("cannot reach " + ", ".join(f"{name} ({env[name]})" for name in down)
                 + ": start it (README.md, 'Quick start') or fix the URL")


def main(env: Mapping[str, str] = os.environ, robot: Callable | None = None,
         run: Callable | None = None, moves: Callable | None = None,
         reach: Callable | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
    """Check the settings, connect to the robot, wake it, and run the voice until stopped.

    A robot that does not answer is tried again ``supervise.backoff`` apart, forever.
    """
    from reachy_sage_voice.pipeline import REQUIRED

    check(env, (*REQUIRED, HOST), reach)
    if robot is None:
        from reachy_mini import ReachyMini

        robot = ReachyMini
    if moves is None:
        from reachy_sage_voice.manifest import load_moves as moves

    from reachy_sage_voice import health, supervise
    from reachy_sage_voice.pipeline import RobotLinkLostError

    health.start()

    declared = moves()  # before connecting: a missing library fails fast
    failed = 0
    while True:
        try:
            connected = robot(host=env[HOST], connection_mode="network")
        except Exception as error:  # rebooting, its daemon starting, no Local Network (Errno 65)
            wait = supervise.backoff(failed)
            failed += 1
            health.report("voice.robot", False, f"lost ({error}), trying again in {wait:g} s")
            logger.warning("cannot reach the robot (%s): trying again in %g s", error, wait)
            sleep(wait)
            continue
        failed = 0
        health.report("voice.robot", True, "connected")
        hung = threading.Timer(DISCONNECT_S, _hung)
        hung.daemon = True
        try:
            with connected as mini:
                try:
                    live(mini, declared, env, run)
                    return
                except MediaResetFailedError as error:
                    logger.error("robot link lost (%s), and a media reset the robot took did "
                                 "not bring the mic back. Stopping; check it with `make status`, "
                                 "reset its media with `make -C robot media-reset`", error)
                    sys.exit(1)
                except RobotLinkLostError as error:  # no reset reached it: rebooting, say
                    health.report("voice.robot", False, f"lost ({error}), reconnecting")
                    logger.warning("robot link lost (%s): reconnecting", error)
                finally:
                    hung.start()  # the disconnect, next, gets DISCONNECT_S
        finally:
            hung.cancel()


def _hung() -> None:
    logger.error("the robot did not disconnect within %g s: exiting without it", DISCONNECT_S)
    os._exit(1)


def app(mini, env: Mapping[str, str], stop_event: threading.Event) -> None:
    """Run the voice on a robot the daemon connected, until ``stop_event`` is set."""
    from reachy_sage_voice import health, manifest
    from reachy_sage_voice.pipeline import REQUIRED

    check(env, REQUIRED, None)
    health.start()
    live(mini, manifest.load_moves(), env, None, stop_event)


def live(mini, declared: Mapping, env: Mapping[str, str], run: Callable | None,
         stop_event: threading.Event | None = None) -> None:
    """Enable and wake (and track, for presence), then run the voice and executor until stopped."""
    from reachy_sage_voice import health, media_reset, supervise

    if run is None:
        from reachy_sage_voice import pipeline

        run = pipeline.run
    from reachy_sage_voice.executor import Executor, daemon_detector, daemon_doa, daemon_face
    from reachy_sage_voice.perception.face import Face, face_percept, restamped
    from reachy_sage_voice.robot import Reconnecting

    host = env.get(HOST) or "127.0.0.1"  # run by the daemon (app()), the app is on the robot
    api = f"http://{host}:{DAEMON_PORT}/api"
    # The SDK's command websocket never reconnects by itself, nor shows a detector refusal.
    mini = Reconnecting(mini, detector=daemon_detector(f"{api}/media/tracking/enable"))
    mini.enable_motors()  # a requirement: if it fails, nothing else can move
    try:
        mini.wake_up()
    except Exception as error:  # a gesture, not a requirement
        logger.warning("could not wake up (%s) - carrying on", error)
    # Following a face is a command, not the default (owner, 2026-10-22); presence alone needs the
    # tracker on, since without it the daemon never sees a face.
    tracking = env.get("SAGE_VOICE_PRESENCE", "0") == "1"

    def track(weight: float = 1.0) -> None:
        try:
            mini.start_head_tracking(weight=weight)
        except Exception as error:  # a reflex, not a requirement
            logger.warning("could not start face tracking (%s) - carrying on", error)

    if tracking:
        track()
    # ponytail: SSH as pollen at the voice's own address; a known_hosts entry under another
    # name fails BatchMode (05 §2), which beam() logs. None when the daemon runs the app.
    ssh = f"pollen@{env[HOST]}" if env.get(HOST) else None
    if ssh:
        supervise.beam(ssh)  # a reboot reset it

    took = [False]  # did the latest media reset reach the robot?

    async def recover(attempt: int) -> None:
        took[0] = await media_reset.reset(mini, attempt)
        if took[0] and ssh:  # the robot answers again: rebooted, it lost the beam
            await asyncio.to_thread(supervise.beam, ssh)
        if mini.tracking is not None:  # the tracker reads the camera the reset restarted:
            await asyncio.to_thread(track, mini.tracking)  # the weight last asked for, not 1.0

    restamp = restamped(time.monotonic)  # one stamp stream: the HTTP and the SDK readings
    told = {"detail": None, "at": -math.inf}

    def judged(reading) -> Face:  # the detector runs only at weight > 0: else presence is unknown
        on = bool(mini.tracking)
        face = face_percept(restamp(reading), time.monotonic(), on and not mini.refused)
        detail = ("detector off" if not on else "refused" if mini.refused
                  else "ok" if face.fresh else "stale")
        now = time.monotonic()
        if detail != told["detail"] or now - told["at"] >= health.BEAT_S:  # not every tick
            told.update(detail=detail, at=now)
            health.report("voice.face", face.fresh, detail)
        return face

    def seen() -> Face:
        return judged(mini.get_tracked_face(wait=False))

    read_face = daemon_face(f"{api}/media/tracking/face")

    def looked() -> Face:  # the daemon's reading over HTTP, fresh every read; else the SDK's
        reading = read_face()
        return seen() if reading is None else judged(reading)

    # The executor is the head's only owner: commands, body plans and emotes all go through it.
    executor = Executor(mini, doa=daemon_doa(f"{api}/state/doa"), face=looked,
                        dance=_dancer(), tracking=env.get("SAGE_VOICE_PRESENCE", "0") == "1",
                        idle=env.get("SAGE_VOICE_IDLE", "1") != "0",
                        cues=env.get("SAGE_VOICE_LISTEN_CUES", "1") != "0")

    async def play_emote(name: str, sound: bool) -> None:  # with the arbiter, a Sample
        await executor.overlay(lambda: executor._sample("emotion", name, sound) if executor.arbiter
                               else mini.async_play_move(declared[name], sound=sound))

    executor.play_move, executor.clip = _player(mini, declared)
    if executor.arbiter:  # the SDK's player would be a second writer: the runner samples (M1)
        executor.play_move = None

    executor.on_link = lambda up, stalls: health.report(  # for `make status`
        "voice.motion", up, ("ok" if up else "the SDK refuses commands") + f", {stalls} stalls")

    async def play_sound(name: str) -> None:  # a vocal: the move's own sound alone
        mini.media.play_sound(str(declared[name].sound_path))
        await asyncio.sleep(declared[name].duration)

    def face() -> bool:
        return seen().detected

    presence = None
    if env.get("SAGE_VOICE_PRESENCE", "0") == "1":  # off until the kernel accepts it
        presence = seen

    voice = run(media_reset.LiveMedia(mini), play_emote, declared, env, play_sound=play_sound,
                presence=presence, observers=[], face=face, recover=recover, executor=executor)
    from reachy_sage_voice.pipeline import RobotLinkLostError

    try:
        asyncio.run(voice if stop_event is None else until(stop_event, voice))
    except RobotLinkLostError as error:
        # Only a quiet mic (transport.py: after its resets, audio back starts the count over);
        # a motion link lost long after a reset that worked is the robot going away (live 10-28).
        if took[0] and "mic" in str(error):
            raise MediaResetFailedError(str(error)) from error
        raise


def _player(mini, declared: Mapping) -> tuple[Callable, Callable]:
    """Play a recorded move by voice: a declared emotion, or one of Pollen's recorded dances.

    Returns ``play(kind, name)``, and ``clip(kind, name, sound=True)``: the move, its sound
    started unless muted, for the motion runner to sample (SAGE_VOICE_ARBITER=1).

    ponytail: the dances dataset loads on first use (the HF cache, else a download), and no
    initial goto eases into a dance's first frame: the SDK's goto there blocks the event loop.
    """
    from reachy_sage_voice import expressions

    @functools.cache
    def dances():
        from reachy_mini.motion.recorded_move import RecordedMoves

        return RecordedMoves(expressions.DANCES_DATASET)

    async def load(kind: str, name: str):
        return declared[name] if kind == "emotion" else (await asyncio.to_thread(dances)).get(name)

    async def play(kind: str, name: str) -> None:
        await mini.async_play_move(await load(kind, name), sound=True)

    async def clip(kind: str, name: str, sound: bool = True):
        move = await load(kind, name)
        if sound and move.sound_path is not None:  # stop cuts it (Executor._halt)
            mini.media.play_sound(str(move.sound_path))
        return move
    return play, clip


def _dancer() -> Callable | None:
    """Return the dance lane's ``dance.dance`` once it has landed; None until then."""
    try:
        from reachy_sage_voice.dance import dance
    except ImportError:
        return None
    return dance


async def until(stop_event: threading.Event, voice) -> None:
    """Run ``voice`` until it ends or ``stop_event`` is set, then cancel it."""
    task = asyncio.ensure_future(voice)
    stopped = asyncio.ensure_future(asyncio.to_thread(stop_event.wait))
    await asyncio.wait({task, stopped}, return_when=asyncio.FIRST_COMPLETED)
    stop_event.set()  # frees the waiting thread when the voice ended first
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task  # re-raises the voice's own failure


def configure_logging() -> None:
    """Log INFO and up to stderr, each line with wall time, monotonic time and turn (latency.py)."""
    from sage_device import latency

    logging.basicConfig(level=logging.INFO, format=latency.FORMAT)
    for handler in logging.getLogger().handlers:
        handler.addFilter(latency.stamp)
    logging.getLogger("reachy_mini.media.media_manager").addFilter(
        throttled("Audio system is not initialized.", MEDIA_DOWN_LOG_EVERY_S))


def throttled(text: str, every_s: float) -> Callable[[logging.LogRecord], bool]:
    """Return a logging filter passing ``text`` at most every ``every_s``, and all else."""
    last = -math.inf

    def allow(record: logging.LogRecord) -> bool:
        nonlocal last
        if record.getMessage() != text:
            return True
        now = time.monotonic()
        if now - last < every_s:
            return False
        last = now
        return True
    return allow


def session_line(env: Mapping[str, str]) -> str:
    """Name the build and hash the voice's settings, so sessions compare (sage_device/pet.py)."""
    settings = sorted((k, v) for k, v in env.items() if k.startswith("SAGE_VOICE_")
                      and k not in ("SAGE_VOICE_BUILD", "SAGE_VOICE_KERNEL_TOKEN"))
    config = hashlib.sha256(repr(settings).encode()).hexdigest()[:12]
    return f"session: build {env.get('SAGE_VOICE_BUILD', 'unknown')} config {config}"


if __name__ == "__main__":
    configure_logging()
    load_settings()
    logger.info("%s", session_line(os.environ))
    main()
