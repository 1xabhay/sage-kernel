"""`python -m sage`: the kernel, its device listener and its model, configured by environment."""

import os
import sys
import threading
from pathlib import Path

from sage import disposition
from sage.adapters import http, model
from sage.journal import Journal
from sage.kernel import Kernel


def main() -> None:
    try:
        devices = http.parse_devices(os.environ.get("SAGE_DEVICES", ""))
        if not devices:
            raise ValueError("Set SAGE_DEVICES=name=token,... to start the device listener")
        # SAGE_BACKGROUND_MODEL is configured for background work and not used yet.
        url, name = os.environ.get("SAGE_MODEL_URL", ""), os.environ.get("SAGE_MODEL", "")
        decide, warm = model.provider(url, name), model.provider(url, name, num_predict=1)
        fresh_s = float(os.environ.get("SAGE_FRESH_SECONDS", "10"))
        timings = disposition.Timings(float(os.environ.get("SAGE_GREET_AFTER_ABSENT_S", disposition.GREET_AFTER_S)),
                                      float(os.environ.get("SAGE_NUDGE_AFTER_IDLE_S", disposition.RESTLESS_S)),
                                      float(os.environ.get("SAGE_QUIET_S", disposition.QUIET_S)),
                                      float(os.environ.get("SAGE_NUDGE_GAP_S", disposition.NUDGE_COOLDOWN_S)),
                                      int(os.environ.get("SAGE_NUDGES_PER_HOUR", disposition.NUDGE_BUDGET)),
                                      os.environ.get("SAGE_GREET_ON_START", "1") not in ("0", "false", "off"),
                                      float(os.environ.get("SAGE_ENGAGED_S", disposition.ENGAGED_S)))
        host, port = os.environ.get("SAGE_DEVICE_BIND", "127.0.0.1"), int(os.environ.get("SAGE_DEVICE_PORT", "8766"))
    except ValueError as error:
        sys.exit(f"sage: {error}")
    journal = Journal(Path(os.environ.get("SAGE_STATE_DIR", ".local/state")) / "kernel.sqlite3")
    kernel = Kernel(journal, decide, fresh_s, timings=timings, warm=warm)
    threading.Thread(target=kernel.ticking, daemon=True).start()
    server = http.serve(kernel, devices, host, port)
    print(f"sage: devices on {host}:{port}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
