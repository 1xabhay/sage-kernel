"""The robot's media rebuilt from scratch, for a mic that went dead (live 2026-10-23).

A USB event dropped the robot's audio device; the daemon logged "The device has
been disconnected." and never rebuilt its media pipeline, so WebRTC connected and
carried no mic audio. ``ReachyMini.release_media()`` then ``acquire_media()``
posts ``/api/media/release`` and ``/api/media/acquire`` to the daemon, which
stops and restarts its media server, and builds the SDK a new ``MediaManager``
(a new WebRTC client, its producer found again). It briefly drops the camera,
and it brings the mic back only once the device has re-appeared on the robot.

By hand, with no voice running: ``make -C robot media-reset``.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

#: Wait between release and acquire on the first attempt; it doubles each attempt after,
#: giving the USB device time to come back.
#: ponytail: 2 s is a guess; measure how long the XVF3800 takes to re-enumerate.
RELEASE_WAIT_S = 2.0


class LiveMedia:
    """``mini.media`` as it is now: an acquire replaces the robot's MediaManager."""

    def __init__(self, mini) -> None:
        """Forward to ``mini.media``, looked up at every call."""
        self._mini = mini

    def __getattr__(self, name: str):
        """Return the current media's attribute."""
        return getattr(self._mini.media, name)


async def reset(mini, attempt: int,
                sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> bool:
    """Release, wait, acquire, and restart recording and playing on the new media.

    Logs each step at WARNING and never raises; True if the new media started (the daemon
    took the reset). Whether the mic came back is for the caller to hear.
    """
    step = f"mic recovery {attempt}:"
    try:
        logger.warning("%s releasing the robot's media (camera and WebRTC drop briefly)", step)
        await asyncio.to_thread(mini.release_media)
        wait = RELEASE_WAIT_S * 2 ** (attempt - 1)
        logger.warning("%s waiting %g s for the audio device", step, wait)
        await sleep(wait)
        logger.warning("%s acquiring the robot's media and reconnecting", step)
        await asyncio.to_thread(mini.acquire_media)
        if mini.media_released:
            logger.warning("%s the daemon did not acquire its media", step)
            return False
        mini.media.start_recording()
        mini.media.start_playing()
        logger.warning("%s media reconnected, listening for the mic", step)
        return True
    except Exception as error:  # a reset that fails is one failed attempt, not a crash
        logger.warning("%s failed: %s", step, error)
        return False
