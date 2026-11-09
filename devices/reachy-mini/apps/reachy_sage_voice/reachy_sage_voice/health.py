"""Per-layer health (LAYERS.md §2.4): each layer says how it is with one ``report`` call.

``start`` writes the table to ``status.MOTION_FILE`` every ``BEAT_S``, for ``make status``.
"""

import dataclasses
import logging
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from reachy_sage_voice import status

logger = logging.getLogger(__name__)

BEAT_S = 2.0


@dataclasses.dataclass(frozen=True)
class Health:
    layer: str
    ok: bool
    since: float  # when it entered this state (ok or not), time.time()
    detail: str = ""


def update(table: Mapping[str, Health], layer: str, ok: bool, detail: str,
           now: float) -> dict[str, Health]:
    """The table with ``layer`` reported; its ``since`` holds while ``ok`` does."""
    old = table.get(layer)
    since = old.since if old is not None and old.ok == ok else now
    return {**table, layer: Health(layer, ok, since, detail)}


#: ponytail: one process-wide table, swapped whole (atomic under the GIL); a bus when §2 lands.
TABLE: dict[str, Health] = {}


def report(layer: str, ok: bool, detail: str = "") -> None:
    global TABLE
    TABLE = update(TABLE, layer, ok, detail, time.time())


def write(path: Path) -> None:
    status.write_heartbeat(path, {name: dataclasses.asdict(h) for name, h in TABLE.items()})


_started = threading.Event()


def start(path: Path = status.MOTION_FILE, every: float = BEAT_S) -> None:
    """Write the heartbeat every ``every`` s from a daemon thread; once per process."""
    if _started.is_set():
        return
    _started.set()

    def beat() -> None:
        while True:
            try:
                write(path)
            except OSError as error:
                logger.warning("could not write the heartbeat (%s)", error)
            time.sleep(every)

    threading.Thread(target=beat, name="heartbeat", daemon=True).start()
