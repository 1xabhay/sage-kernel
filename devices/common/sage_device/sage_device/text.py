"""The smallest body: a person's lines in on stdin, Sage's replies out on stdout.

``SAGE_KERNEL_URL`` and ``SAGE_TOKEN`` name the kernel and this body's token. It has no senses
and one tool, a gesture written between stars, and tells the kernel its output is text. The
device conformance suite (conformance/test_device.py) runs it by default.
"""

import asyncio
import os
import sys
import uuid

from sage_device.kernel import KernelClient, KernelUnavailableError, Session

GESTURE = {"name": "gesture",
           "description": "Show a small gesture in the text, with or without words.",
           "inputSchema": {"type": "object", "required": ["name"],
                           "properties": {"name": {"enum": ["smile", "nod", "shrug"]}}}}
MANIFEST = {"senses": [], "tools": [GESTURE], "output": {"medium": "text"}}


async def turn(session: Session, line: str, show) -> None:
    """Submit one line and show its reply as it streams; every reply shown gets its result."""
    turn_id = f"text-{uuid.uuid4().hex}"
    if await session.start(turn_id, line, {}, {"medium": "text"}) != "admitted":
        show("(Sage is busy.)\n")
        return
    shown = 0
    while True:
        body = await session.poll(shown, 20)
        for effect in body["effects"]:
            mine = effect.get("turn_id") == turn_id
            if effect["kind"] == "reply_delta" and mine:
                show(effect["text"])
                shown += len(effect["text"])
            elif effect["kind"] == "reply":  # this turn's, or one Sage began itself
                show(effect["text"][shown if mine else 0:] + "\n")
                await session.result(effect["sequence"], "done")
                if mine:
                    return
            elif effect["kind"] == "gesture":
                show(f"*{effect['input']['name']}*\n")
                await session.result(effect["sequence"], "done")
        ended = body.get("turn", {})
        if ended.get("turn_id") == turn_id and ended.get("run_status") != "active":
            return  # silence is an answer too


async def main() -> None:
    """Read lines until stdin ends."""
    client = KernelClient(os.environ["SAGE_KERNEL_URL"], os.environ["SAGE_TOKEN"])
    session = Session(client, MANIFEST)

    def show(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    while line := (await asyncio.to_thread(sys.stdin.readline)).strip():
        try:
            await turn(session, line, show)
        except KernelUnavailableError:
            show("(Sage's kernel is not answering.)\n")


if __name__ == "__main__":
    asyncio.run(main())
