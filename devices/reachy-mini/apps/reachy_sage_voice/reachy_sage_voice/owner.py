"""Only the owner barges in, once the STT has their voiceprint (services/stt, ``make -C services enroll``).

With a voiceprint enrolled, the STT drops every transcript in another voice, so
music, TV and other people never start a turn. Barge-in comes earlier than any
transcript, on the VAD's onset, and music trips the VAD too. So an onset waits
``VERIFY_S``, sends that audio (with ``BEFORE_S`` before it) to the STT's
speaker check, and interrupts only for the owner: barge-in takes ~0.8 s
instead of ~0.2 s, the price of not being cut off by a song. With no
voiceprint, nothing here runs.
"""

import asyncio
import json
import logging
import urllib.request
from collections.abc import Awaitable, Callable

from reachy_sage_voice.song import form_wav, wav

logger = logging.getLogger(__name__)

#: Speech after the onset that the check hears. ponytail: a guess; measure barge-in on the robot.
VERIFY_S = 0.8
#: Audio before the onset: the VAD commits ~0.2 s after speech starts.
BEFORE_S = 0.2


class OwnerCheck:
    """The STT's speaker check over stdlib HTTP."""

    def __init__(self, stt_url: str, timeout_s: float = 2.0):
        """Take the STT URL, with or without ``/v1``."""
        self.url, self.timeout_s = stt_url.rstrip("/").removesuffix("/v1"), timeout_s

    def enrolled(self) -> bool:
        """Whether the STT holds a voiceprint."""
        with urllib.request.urlopen(self.url + "/health", timeout=self.timeout_s) as response:
            return bool(json.loads(response.read()).get("enrolled"))

    def is_owner(self, pcm: bytes) -> bool:
        """Whether 16 kHz mono int16 ``pcm`` is the owner's voice."""
        body, kind = form_wav(wav(pcm), "onset.wav")
        request = urllib.request.Request(self.url + "/v1/speaker/verify", body, {"Content-Type": kind},
                                         method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            return bool(json.loads(response.read())["owner"])


def verified_onset(on_onset: Callable[[], Awaitable], check, snippet: Callable[[float], bytes],
                   sleep: Callable[[float], Awaitable] = asyncio.sleep) -> Callable[[], None]:
    """An onset handler that interrupts only for the owner, without holding up the audio path."""
    pending: set[asyncio.Task] = set()

    async def verify() -> None:
        await sleep(VERIFY_S)
        try:
            owner = await asyncio.to_thread(check.is_owner, snippet(VERIFY_S + BEFORE_S))
        except (OSError, ValueError, KeyError) as error:
            logger.warning("could not check the voice, barging in: %s", error)
            owner = True  # being able to stop the robot matters more than a song
        if owner:
            await on_onset()
        else:
            logger.info("not the owner's voice: no barge-in")

    def handler() -> None:
        task = asyncio.ensure_future(verify())
        pending.add(task)
        task.add_done_callback(pending.discard)
    return handler
