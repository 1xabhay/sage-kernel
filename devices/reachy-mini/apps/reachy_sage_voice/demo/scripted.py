"""`make demo`: a scripted showcase on the real robot, for filming.

`make demo-dry` is the same with no robot.

The body is the real one: the robot's mic, speech to text, voice and recorded moves. The kernel is
the real one too, on a throwaway journal, so nothing said here enters the pet's memory. Only the
model is scripted. Both beats are turns the kernel begins itself: BEATS[0] is its greeting when it
first sees someone, BEATS[1] its nudge a few seconds after the greeting's move ends. Nothing has
to be said; a spoken "hi" gets the voice's own greeting dance on top.

The live voice job is stopped while this runs and started again when it ends (Ctrl-C).
ponytail: one emote per turn is all the answer format carries; a longer show needs more turns.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("SAGE_KERNEL_DIR") or APP.parents[3])  # the monorepo, with the kernel
LABEL = "ai.sage.reachy-voice"
#: What Sage says and the feeling it shows (the body picks the clip). The kernel cuts a reply
#: longer than the voice's cap at a sentence.
MAX_CHARS = 160
BEATS = [
    (
        "Hi! You're home! I missed the sound of you.",
        {"emote": {"name": "excited"}, "voice_style": {"style": "excited"}},
    ),
    (
        "One thing: Maya's coming Saturday. Shall I remind you?",
        {"emote": {"name": "attentive"}, "voice_style": {"style": "warm"}},
    ),
]
assert all(len(line) <= MAX_CHARS for line, _ in BEATS), "a line is over the voice's cap"
SAME_TURN_S = 2.0  # the kernel's retries of one turn come at once; the nudge comes later
# Idle seconds after the greeting before the reminder; the kernel also waits out the dance.
NUDGE_AFTER_S = "4"
LOG = Path.home() / "Library" / "Logs" / "reachy-sage" / "demo.log"  # the last run, to time it


def scripted():
    """Return the fake model's `answer(messages)`: the next beat per turn, then silence."""
    state = {"beat": -1, "last": float("-inf")}

    def answer(messages) -> tuple[str, dict]:
        now = time.monotonic()
        if now - state["last"] > SAME_TURN_S:
            state["beat"] += 1
        state["last"] = now
        return BEATS[state["beat"]] if state["beat"] < len(BEATS) else ("", {})

    return answer


def kernel(state: str):
    """Start a real kernel on a throwaway journal, its model scripted: (kernel, provider, token)."""
    sys.path.insert(0, str(ROOT))
    from conformance import test_conformance as suite

    suite.answer = scripted()
    provider = suite.Provider()
    process = suite.KernelProcess(
        state,
        provider,
        SAGE_GREET_ON_START="1",
        SAGE_NUDGE_AFTER_IDLE_S=NUDGE_AFTER_S,
        SAGE_ENGAGED_S="1",
        SAGE_NUDGE_GAP_S="0",
    ).start()
    return process, provider, suite.TOKENS["reachy"]


async def dry(url: str, token: str) -> list[dict]:
    """Play the body's part with no robot: come into view, acknowledge all; the effects."""
    from sage_device.kernel import KernelClient, Session

    manifest = json.loads((ROOT / "tests" / "voice_manifest.json").read_text())
    session, seen, shown = Session(KernelClient(url, token), manifest), [], 0
    await session.hello()
    await session.presence("seen", 0.9)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and sum(e["kind"] == "reply" for e in seen) < len(BEATS):
        for effect in (await session.poll(shown, 2))["effects"]:
            if effect["kind"] == "reply_delta":
                shown += len(effect["text"])
                continue
            shown = 0
            seen.append(effect)
            print(
                time.strftime("%H:%M:%S"),
                effect["kind"] + ":",
                effect.get("text") or effect.get("input"),
            )
            await session.result(effect["sequence"], "done")
    return seen


def main() -> None:
    """Run the demo on the robot, or with `dry` check it with none."""
    with tempfile.TemporaryDirectory() as state:
        process, provider, token = kernel(state)
        url = f"http://127.0.0.1:{process.device_port}"
        try:
            if sys.argv[1:] == ["dry"]:
                seen = asyncio.run(dry(url, token))
                said = [e["text"] for e in seen if e["kind"] == "reply"]
                moved = [e["input"]["name"] for e in seen if e["kind"] == "emote"]
                assert said == [beat[0] for beat in BEATS], said
                assert moved == [beat[1]["emote"]["name"] for beat in BEATS], moved
                print("demo-dry: ok. Both beats came in order, the second begun by the kernel.")
                return
            uid = os.getuid()
            plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}/{LABEL}"], check=False
            )  # the robot has one driver
            print(
                "demo: the live voice is stopped. Stay OUT of the robot's view until the line\n"
                "demo: `mic beam` appears. Then start filming and step into view: it greets you.\n"
                "demo: Ctrl-C ends it and starts the live voice again."
            )
            try:
                LOG.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    f'uv run python -m reachy_sage_voice 2>&1 | tee "{LOG}"',
                    shell=True,
                    cwd=APP,
                    check=False,
                    env={
                        **os.environ,
                        "SAGE_VOICE_KERNEL_URL": url,
                        "SAGE_VOICE_KERNEL_TOKEN": token,
                    },
                )
            except KeyboardInterrupt:
                pass
            finally:
                subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], check=False)
                print("demo: the live voice is starting again.")
        finally:
            process.stop()
            provider.server.shutdown()


if __name__ == "__main__":
    main()
