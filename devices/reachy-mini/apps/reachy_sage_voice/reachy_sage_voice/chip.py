"""The mic chip's ASR beam (XMOS XVF3800): route it to the right channel, check, put it back.

    python3 -I reachy_sage_voice/chip.py on --robot pollen@HOST --gain 1.0   (make -C robot beam-on)
    python3 -I reachy_sage_voice/chip.py check --robot pollen@HOST           (make -C robot beam-check)
    python3 -I reachy_sage_voice/chip.py off --robot pollen@HOST             (make -C robot beam-off)

``on`` reads AUDIO_MGR_OP_L, AUDIO_MGR_OP_R and AEC_ASROUTGAIN, keeps them in a record file, then
writes L = 8,0 (the conference output, as today), R = 7,3 (the ASR beam: beamformed, no noise
suppression or AGC; XMOS ASR guide) and the beam's gain, and reads each back. A value that does not
read back puts all three back as read. ``off`` writes the record back and deletes it. Nothing is
saved on the chip (no SAVE_CONFIGURATION), so a power cycle of the robot also undoes it.

Reads go through the daemon (GET /api/audio/config/parameter/<name>). Writes go through the chip's
own CLI over SSH, as pollen (the udev rule opens the USB device to all): in reachy_mini 1.10.0 the
daemon's POST /api/audio/config/apply turns every value into a float, which the integer parameters
(L and R) cannot be written with, so it answers applied=false.

Stdlib only, like status.py.
"""

import argparse
import json
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

L, R, GAIN = "AUDIO_MGR_OP_L", "AUDIO_MGR_OP_R", "AEC_ASROUTGAIN"
CONFERENCE, ASR_BEAM = [8, 0], [7, 3]
TIMEOUT_S = 5.0
RECORD = Path(__file__).resolve().parent.parent / "robot" / ".beam-before.json"
CHIP_CLI = ["/venvs/mini_daemon/bin/python", "-m", "reachy_mini.media.audio_control_utils"]


def _host(robot: str) -> str:
    return robot.rpartition("@")[2]


def read(robot: str, name: str, get: Callable) -> list[float]:
    url = f"http://{_host(robot)}:8000/api/audio/config/parameter/{name}"
    with get(url, timeout=TIMEOUT_S) as response:
        return json.loads(response.read())["values"]


def _same(a: list[float], b: list[float]) -> bool:
    return len(a) == len(b) and all(abs(x - y) <= 1e-3 for x, y in zip(a, b))


def ssh_command(robot: str, name: str, values: list[float]) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", robot, *CHIP_CLI, name,
            "--values", *(f"{value:g}" for value in values)]


# ponytail: SSH for writes; back to POST /api/audio/config/apply once the daemon keeps integers.
def ssh_write(robot: str, name: str, values: list[float]) -> None:
    """The CLI prints an error and still exits 0, so the caller reads every write back."""
    subprocess.run(ssh_command(robot, name, values), check=True, capture_output=True, timeout=30)


def _apply(robot: str, values: dict, get: Callable, write: Callable) -> list[str]:
    """Write each value and read it back; the names that do not match."""
    for name, value in values.items():
        write(robot, name, value)
    return [name for name, value in values.items() if not _same(read(robot, name, get), value)]


def _now(robot: str, get: Callable) -> dict:
    return {name: read(robot, name, get) for name in (L, R, GAIN)}


def on(robot: str, gain: float, record: Path, get: Callable = urllib.request.urlopen,
       write: Callable = ssh_write) -> tuple[bool, str]:
    try:
        now = _now(robot, get)
    except (OSError, ValueError, KeyError) as error:
        return False, f"cannot read the chip, nothing written: {error}"
    first = not record.exists()
    if first:  # a second `on` keeps the values from before the first
        record.write_text(json.dumps(now))
    before = json.loads(record.read_text())
    try:
        wrong = _apply(robot, {L: CONFERENCE, R: ASR_BEAM, GAIN: [gain]}, get, write)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        wrong = [f"error: {error}"]
    if not wrong:
        return True, f"routed: L {CONFERENCE}, R {ASR_BEAM} (ASR beam), gain {gain}; before {before}"
    back = _apply(robot, now, get, write)
    if first and not back:
        record.unlink()
    return False, f"{', '.join(wrong)} did not read back; put back as read{'' if not back else f', except {back}'}"


def off(robot: str, record: Path, get: Callable = urllib.request.urlopen,
        write: Callable = ssh_write) -> tuple[bool, str]:
    if not record.exists():
        return False, f"no record at {record}: nothing to put back (was beam-on run from here?)"
    before = json.loads(record.read_text())
    wrong = _apply(robot, before, get, write)
    if wrong:
        return False, f"{', '.join(wrong)} did not read back; record kept at {record}"
    record.unlink()
    return True, f"put back: {before}"


def _show(values: list[float]) -> str:
    return " ".join(f"{value:g}" for value in values)


def check(robot: str, get: Callable = urllib.request.urlopen) -> tuple[bool, str]:
    now = _now(robot, get)
    routed = _same(now[R], ASR_BEAM)
    where = f"L {_show(now[L])}, R {_show(now[R])}, gain {_show(now[GAIN])}"
    return routed, f"mic beam: {'ASR on R' if routed else 'not routed'} ({where})"


def main(argv: list[str] | None = None, get: Callable = urllib.request.urlopen,
         write: Callable = ssh_write, out: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("on", "off", "check"))
    parser.add_argument("--robot", required=True, help="user@host, as for ssh")
    parser.add_argument("--gain", type=float, default=1.0, help="AEC_ASROUTGAIN (on only)")
    parser.add_argument("--record", type=Path, default=RECORD)
    args = parser.parse_args(argv)
    if args.action == "on":
        ok, why = on(args.robot, args.gain, args.record, get, write)
    elif args.action == "off":
        ok, why = off(args.robot, args.record, get, write)
    else:
        ok, why = check(args.robot, get)
    print(why, file=out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
