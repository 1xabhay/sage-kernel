"""Supervision (LAYERS.md §1a row 8): the voice stays up while the robot reboots.

Connecting is retried forever, ``backoff`` apart, at start and after a lost link
(``__main__.main``). The one exit is a media reset that reached the robot and still left
the mic quiet (``__main__.MediaResetFailedError``): then launchd restarts the voice.

The mic chip saves nothing, so a reboot also puts the ASR beam back off R: ``beam`` checks
it at every connect and after every media reset the robot took, and routes it again
(``make -C robot beam-on``, chip.py) when it is off (owner decision O5).
"""

import logging
import urllib.request
from collections.abc import Callable
from pathlib import Path

from reachy_sage_voice import chip, health

logger = logging.getLogger(__name__)

#: The first wait between connection attempts; it doubles up to ``MAX_BACKOFF_S``.
BACKOFF_S, MAX_BACKOFF_S = 1.0, 10.0


def backoff(attempt: int) -> float:
    """Seconds to wait after ``attempt`` + 1 failed connections in a row."""
    return min(BACKOFF_S * 2 ** attempt, MAX_BACKOFF_S)


def beam(robot: str, get: Callable = urllib.request.urlopen, write: Callable = chip.ssh_write,
         record: Path = chip.RECORD, gain: float = 1.0) -> None:
    """Route the ASR beam on ``robot`` (user@host, as for ssh) if it is off; never raises.

    ponytail: one try; a chip the daemon cannot read yet waits for the next connect or reset.
    """
    try:
        routed, why = chip.check(robot, get)
    except (OSError, ValueError, KeyError) as error:
        logger.warning("could not read the mic beam (%s)", error)
        health.report("voice.beam", False, f"could not read it ({error})")
        return
    if routed:
        logger.info("%s", why)
        health.report("voice.beam", True, why)
        return
    logger.warning("%s: routing it again (beam-on)", why)
    ok, why = chip.on(robot, gain, record, get, write)
    health.report("voice.beam", ok, f"beam-on: {why}")
    (logger.info if ok else logger.warning)("beam-on: %s", why)
