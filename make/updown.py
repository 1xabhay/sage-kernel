"""make sage-up and make sage-down: the whole of Sage, on or off.

Down turns each launchd job off with `launchctl disable`, which outlives a reboot, so a Sage that
was shut down stays down. Up turns them on again; the login job (ai.sage.up) then brings Sage back
after a reboot. Containers are started and stopped, never removed: the journal stays in its volume.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

KERNEL = "sage-kernel-kernel-1"
SERVICES = ("reachy-sage-voice-searxng-1", "reachy-sage-voice-research-1", "reachy-sage-voice-stt-1")
JOBS = ("ai.sage.reachy-pocket", "ai.sage.reachy-voice")  # the TTS first: the voice speaks through it
LOGIN = "ai.sage.up"
DOCKER = ("docker", "--context", "colima")  # named: the default context has fallen back before (2026-10-20)
HEALTHY_WITHIN_S = 120


def up(run, agents: Path, uid: str, sleep=time.sleep) -> None:
    """Start the VM, the kernel and the services, wait until they are healthy, then load the jobs."""
    for job in JOBS:
        if not (agents / f"{job}.plist").exists():
            sys.exit(f"sage-up: {job} was never installed. Run the voice app's make deploy (and its "
                     "services' make pocket-plist) first.")
    if run(["colima", "status"], check=False).returncode:
        run(["colima", "start"])
    for name in (KERNEL, *SERVICES):
        if run([*DOCKER, "start", name], check=False).returncode:
            sys.exit(f"sage-up: there is no container {name}. Run make deploy (the kernel) and the voice "
                     "services' make deploy first.")
    for name in (KERNEL, *SERVICES):
        for _ in range(HEALTHY_WITHIN_S):
            if run([*DOCKER, "inspect", "-f", "{{.State.Health.Status}}", name], check=False).stdout.strip() == "healthy":
                break
            sleep(1)
        else:
            sys.exit(f"sage-up: {name} is not healthy after {HEALTHY_WITHIN_S} s. See: docker logs {name}")
    run(["launchctl", "enable", f"gui/{uid}/{LOGIN}"])  # loaded by launchd itself at the next login
    for job in JOBS:
        run(["launchctl", "enable", f"gui/{uid}/{job}"])
        if run(["launchctl", "print", f"gui/{uid}/{job}"], check=False).returncode:  # not loaded yet
            run(["launchctl", "bootstrap", f"gui/{uid}", str(agents / f"{job}.plist")])
    print("sage: up. It comes back by itself after a reboot.")


def down(run, uid: str, model: str = "") -> None:
    """Back up the journal, turn the jobs off for good, stop the containers, and free the model."""
    alive = run([*DOCKER, "inspect", "-f", "{{.State.Running}}", KERNEL], check=False)
    running = alive.returncode == 0 and alive.stdout.strip() == "true"
    if running and run(["make", "backup"], check=False).returncode:
        print("sage-down: the journal backup failed; the journal itself stays in its volume.", file=sys.stderr)
    for job in (LOGIN, *reversed(JOBS)):
        run(["launchctl", "disable", f"gui/{uid}/{job}"])
    for job in reversed(JOBS):
        run(["launchctl", "bootout", f"gui/{uid}/{job}"], check=False)  # not loaded: nothing to do
    for name in (*SERVICES, KERNEL):
        run([*DOCKER, "stop", name], check=False)
    if model:
        run(["ollama", "stop", model], check=False)
    print("sage: down. It stays down across a reboot, until make sage-up.")


def _run(command: list[str], check: bool = True) -> subprocess.CompletedProcess:
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if check and done.returncode:
        sys.exit(f"{' '.join(command)}: {done.stderr.strip() or done.returncode}")
    return done


def _model() -> str:
    """SAGE_MODEL from the checkout's .env, the one the kernel was started with; "" when it names none."""
    try:
        lines = Path(".env").read_text().splitlines()
    except OSError:
        return ""
    return next((line.partition("=")[2].strip() for line in lines if line.startswith("SAGE_MODEL=")), "")


if __name__ == "__main__":
    home, user = Path.home() / "Library" / "LaunchAgents", str(os.getuid())
    if sys.argv[1:] == ["up"]:
        up(_run, home, user)
    elif sys.argv[1:] == ["down"]:
        down(_run, user, _model())
    else:
        sys.exit("usage: updown.py up | down")
