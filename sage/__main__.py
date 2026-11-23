"""`python -m sage`: the kernel, its device listener and its model, configured by environment.
`python -m sage memory list | forget ID | edit ID "text" | add "text"`: what it remembers, for its owner to see and fix."""

import os
import sys
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

try:  # the character file is read as the kernel is imported: a bad one stops the start here, in one line
    from sage import disposition, policy
    from sage.adapters import http, model, search
    from sage.journal import Journal
    from sage.kernel import Kernel
except ValueError as error:
    sys.exit(f"sage: {error}")


USAGE = 'usage: python -m sage memory list | forget ID | edit ID "text" | add "text"'


def memory(args: list[str], state_dir: str, out=print) -> int:
    """The owner's view of memory, on the journal in `state_dir` (SAGE_STATE_DIR), while the kernel runs or
    not: each record with the line it came from; forget, edit and add are journaled as the owner's. Never
    creates a journal: a wrong folder is an error, not an empty memory."""
    path = Path(state_dir) / "kernel.sqlite3"
    if not path.is_file():
        out(f"sage: no journal at {path}. Set SAGE_STATE_DIR to the kernel's state folder.")
        return 1
    journal = Journal(path)
    try:
        command, rest = args[0], args[1:]
        if command == "list" and not rest:
            for r in journal.state.facts:
                day = time.strftime("%Y-%m-%d", time.localtime(r["at"])) if r["at"] else "earlier"
                line = journal.line(r["from"])
                out(f"{r['id']:>4}  {r['who']:<8} {r['kind']:<8} {day}  {r['text']}"
                    + (f'    <- line {r["from"]}: "{line}"' if line else ""))
            out(f"{len(journal.state.facts)} remembered")
            return 0
        op, id, text = {"forget": lambda: ("forget", int(rest[0]), ""), "edit": lambda: ("edit", int(rest[0]), rest[1]),
                        "add": lambda: ("add", None, rest[0])}[command]()
        if len(rest) != (1 if command != "edit" else 2):
            raise IndexError
        journal.amend(lambda state: policy.amend(state, op, time.time(), id, text), time.time())
    except (IndexError, KeyError):
        out(USAGE)
        return 2
    except ValueError as error:
        out(f"sage: {error}")
        return 1
    out({"forget": f"forgot {id}", "edit": f"changed {id}", "add": "added"}[op])
    return 0


def main() -> None:
    if sys.argv[1:2] == ["memory"]:
        sys.exit(memory(sys.argv[2:], os.environ.get("SAGE_STATE_DIR", ".local/state")))
    try:
        devices = http.Devices(os.environ.get("SAGE_DEVICES", ""), os.environ.get("SAGE_DEVICES_FILE", ""))
        if not devices.tokens:
            raise ValueError("Set SAGE_DEVICES=name=token,... (or SAGE_DEVICES_FILE) to start the device listener")
        # SAGE_BACKGROUND_MODEL is configured for background work and not used yet.
        url, name = os.environ.get("SAGE_MODEL_URL", ""), os.environ.get("SAGE_MODEL", "")
        decide, warm = model.provider(url, name), model.provider(url, name, num_predict=1)
        # Memory is written between conversations by the model already loaded, unless SAGE_WRITER_MODEL names
        # another on the same Ollama; never by the hosted one. "off": conversations still end, nothing is written.
        writes = os.environ.get("SAGE_WRITER_MODEL") or name
        write = None if writes == "off" else model.writer(url, writes)
        idle_s = float(os.environ.get("SAGE_SESSION_IDLE_S") or policy.SESSION_IDLE_S)
        # SAGE_HOSTED: a hosted model for the lines a device marks as addressed; unset, nothing leaves (SECURITY.md).
        decide, addressed = model.from_env(os.environ, decide)
        # The web, through the voice's research service; empty: never (the query is the one line that leaves).
        search_url = os.environ.get("SAGE_SEARCH_URL", "")
        look = search.provider(search_url) if search_url else None
        fresh_s = float(os.environ.get("SAGE_FRESH_SECONDS", "10"))
        timings = disposition.Timings(float(os.environ.get("SAGE_GREET_AFTER_ABSENT_S", disposition.GREET_AFTER_S)),
                                      float(os.environ.get("SAGE_NUDGE_AFTER_IDLE_S", disposition.RESTLESS_S)),
                                      float(os.environ.get("SAGE_QUIET_S", disposition.QUIET_S)),
                                      float(os.environ.get("SAGE_NUDGE_GAP_S", disposition.NUDGE_COOLDOWN_S)),
                                      int(os.environ.get("SAGE_NUDGES_PER_HOUR", disposition.NUDGE_BUDGET)),
                                      os.environ.get("SAGE_GREET_ON_START", "1") not in ("0", "false", "off"),
                                      float(os.environ.get("SAGE_ENGAGED_S", disposition.ENGAGED_S)))
        zone = os.environ.get("SAGE_TZ", "")  # an IANA name, such as Europe/Paris: the person's local time
        tz = ZoneInfo(zone) if zone else None
        if not zone:
            print("sage: SAGE_TZ is not set: times are this machine's, UTC in a container", file=sys.stderr)
        host, port = os.environ.get("SAGE_DEVICE_BIND", "127.0.0.1"), int(os.environ.get("SAGE_DEVICE_PORT", "8766"))
        origins = http.parse_origins(os.environ.get("SAGE_ORIGINS", ""))  # pages whose browser may call; empty: none
    except (OSError, ValueError, KeyError) as error:  # KeyError: an unknown SAGE_TZ
        sys.exit(f"sage: {error}")
    journal = Journal(Path(os.environ.get("SAGE_STATE_DIR", ".local/state")) / "kernel.sqlite3")
    kernel = Kernel(journal, decide, fresh_s, timings=timings, warm=warm, tz=tz,
                    place=os.environ.get("SAGE_PLACE", ""), search=look, write=write, idle_s=idle_s)
    threading.Thread(target=kernel.ticking, daemon=True).start()
    server = http.serve(kernel, devices, host, port, origins, addressed)
    print(f"sage: devices on {host}:{port}", file=sys.stderr)
    if addressed:
        print("sage: addressed lines go to the hosted model; all others stay on this machine", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
