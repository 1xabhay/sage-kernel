"""One line of the robot's health every INTERVAL_S, to the journal (sage-health.service).

The robot dropped off the network on 2026-10-22 and nothing on record said why. Each line names
the usual Raspberry Pi breaking points: undervoltage and throttling (vcgencmd get_throttled), heat,
Wi-Fi signal, power saving, bit rates and error counters, load, free memory, the Reachy daemon's
state, its CPU, and its control loop: rate, worst gap between ticks and motor errors over the last
second (the daemon logs "Control loop took too long" only at debug level, so its stats stand in). The
journal is persistent (50-sage-persistent.conf), so the seconds before a drop survive a reboot.

Standard library only; it runs on the robot's own Python. A probe that fails costs its fields,
never the line.
"""
import json
import os
import re
import subprocess
import time
import urllib.request

INTERVAL_S = 10
# get_throttled bits (Raspberry Pi documentation, "vcgencmd").
FLAGS = {"undervolt_now": 0, "capped_now": 1, "throttled_now": 2, "undervolt_since_boot": 16,
         "throttled_since_boot": 18}
ORDER = ("throttled", "undervolt_now", "capped_now", "throttled_now", "undervolt_since_boot",
         "throttled_since_boot", "temp_c", "wifi", "signal_dbm", "rx_mbit", "tx_mbit", "tx_retries",
         "tx_failed", "beacon_loss", "power_save", "load1", "mem_avail_mb", "daemon", "backend_ready",
         "daemon_cpu", "loop_hz", "loop_max_ms", "loop_errors")
DAEMON = "http://127.0.0.1:8000/api/daemon/status"
DAEMON_MODULE = b"reachy_mini.daemon.app.main"  # reachy-mini-daemon.service's python, under launcher.sh


def throttled(text: str) -> dict:
    match = re.search(r"throttled=(0x[0-9a-fA-F]+)", text)
    if not match:
        return {}
    value = int(match.group(1), 16)
    return {"throttled": match.group(1), **{name: value >> bit & 1 for name, bit in FLAGS.items()}}


def temperature(text: str) -> dict:
    match = re.search(r"temp=([\d.]+)", text)
    return {"temp_c": float(match.group(1))} if match else {}


def _number(pattern: str, text: str, kind=float):
    match = re.search(pattern, text)
    return kind(match.group(1)) if match else None


def wifi(link: str, station: str) -> dict:
    """`iw dev wlan0 link` and `iw dev wlan0 station dump`, as fields."""
    if not link.startswith("Connected"):
        return {"wifi": "down"}
    found = {"wifi": "up", "signal_dbm": _number(r"signal:\s*(-?\d+)", link, int),
             "rx_mbit": _number(r"rx bitrate:\s*([\d.]+)", link), "tx_mbit": _number(r"tx bitrate:\s*([\d.]+)", link),
             "tx_retries": _number(r"tx retries:\s*(\d+)", station, int),
             "tx_failed": _number(r"tx failed:\s*(\d+)", station, int),
             "beacon_loss": _number(r"beacon loss:\s*(\d+)", station, int)}
    return {key: value for key, value in found.items() if value is not None}


def power_save(text: str) -> dict:
    """`iw dev wlan0 get power_save`."""
    match = re.search(r"Power save:\s*(\w+)", text)
    return {"power_save": match.group(1)} if match else {}


def daemon_pid(proc: str = "/proc") -> int | None:
    """The daemon's pid, from /proc/*/cmdline: the sealed service cannot ask systemd."""
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc, name, "cmdline"), "rb") as f:
                if DAEMON_MODULE in f.read().split(b"\0"):
                    return int(name)
        except OSError:  # gone, or not ours to read
            continue
    return None


def cpu_ticks(stat: str) -> int:
    """utime + stime from /proc/<pid>/stat (fields 14 and 15, counted after the name's last ')')."""
    fields = stat[stat.rindex(")") + 2:].split()
    return int(fields[11]) + int(fields[12])


def daemon_cpu(proc: str, last: dict, now: float, hz: int) -> dict:
    """The daemon's CPU % since the previous call (100 = one core); `last` keeps pid, ticks and time."""
    try:
        pid = daemon_pid(proc)
        with open(os.path.join(proc, str(pid), "stat")) as f:
            ticks = cpu_ticks(f.read())
    except (OSError, ValueError, IndexError):
        last.clear()
        return {}
    before = dict(last)
    last.update(pid=pid, ticks=ticks, now=now)
    if before.get("pid") != pid or now <= before["now"]:
        return {}
    return {"daemon_cpu": round((ticks - before["ticks"]) / hz / (now - before["now"]) * 100, 1)}


def _status() -> str:
    with urllib.request.urlopen(DAEMON, timeout=3) as response:
        return response.read().decode()


def daemon(status=_status) -> dict:
    """The daemon's own answer, so a hung daemon shows, not just a running process."""
    try:
        answer = json.loads(status())
    except (OSError, ValueError):
        return {"daemon": "unreachable"}
    backend = answer.get("backend_status") or {}
    loop = backend.get("control_loop_stats") or {}
    fields = {"daemon": answer.get("state", "unknown"), "backend_ready": int(bool(backend.get("ready")))}
    if "mean_control_loop_frequency" in loop:
        fields.update(loop_hz=round(loop["mean_control_loop_frequency"], 1),
                      loop_max_ms=round(loop["max_control_loop_interval"] * 1000), loop_errors=loop.get("nb_error"))
    return fields


def line(fields: dict) -> str:
    return "sage-health " + " ".join(f"{key}={fields[key]}" for key in ORDER if key in fields)


def _run(argv) -> str:
    return subprocess.run(argv, capture_output=True, text=True, timeout=5).stdout


_LAST: dict = {}


def sample(run=_run, loadavg: str | None = None, meminfo: str | None = None, status=_status, proc="/proc",
           last=_LAST) -> dict:
    """Every probe once; `run(argv) -> stdout`."""
    def probe(argv) -> str:
        try:
            return run(argv)
        except (OSError, subprocess.SubprocessError):
            return ""
    loadavg = loadavg if loadavg is not None else open("/proc/loadavg").read()
    meminfo = meminfo if meminfo is not None else open("/proc/meminfo").read()
    fields = {**throttled(probe(["vcgencmd", "get_throttled"])), **temperature(probe(["vcgencmd", "measure_temp"])),
              **wifi(probe(["iw", "dev", "wlan0", "link"]), probe(["iw", "dev", "wlan0", "station", "dump"])),
              **power_save(probe(["iw", "dev", "wlan0", "get", "power_save"])),
              "load1": float(loadavg.split()[0])}
    available = _number(r"MemAvailable:\s*(\d+)", meminfo, int)
    if available is not None:
        fields["mem_avail_mb"] = available // 1024
    return {**fields, **daemon(status), **daemon_cpu(proc, last, time.monotonic(), os.sysconf("SC_CLK_TCK"))}


if __name__ == "__main__":
    while True:
        print(line(sample()), flush=True)
        time.sleep(INTERVAL_S)
