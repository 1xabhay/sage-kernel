"""What a Reachy Mini declares: its moves, voice tools and body (the wire: sage_device's tests)."""

import asyncio
import json
import re
from typing import NamedTuple

from sage_device.kernel import KernelUnavailableError

from reachy_sage_voice.manifest import EMOTIONS, manifest

# --- manifest -----------------------------------------------------------------


class Move(NamedTuple):
    duration: float
    description: str


#: A stand-in library: the real one is checked in test_vocabulary.py.
MOVES = {name: Move(2.0, f"Look {name}. Use it sparingly.") for name in EMOTIONS}
MOVES["boredom1"] = Move(15.7, "You're about to fall asleep because the conversation is boring. "
                               "Use it when no one talks to you for 2 minutes.")


def test_manifest_offers_emote_as_an_mcp_tool_over_the_declared_moves():
    m = manifest(MOVES)
    assert m["senses"] == ["speech"]
    emote = m["tools"][0]
    assert emote["name"] == "emote"
    assert emote["inputSchema"]["type"] == "object"
    assert emote["inputSchema"]["required"] == ["name"]
    assert emote["inputSchema"]["properties"]["name"]["enum"] == list(EMOTIONS)
    assert len(EMOTIONS) == len(set(EMOTIONS)) and "cheerful1" in EMOTIONS
    assert emote["interruptible"] is True
    assert re.fullmatch(r"[a-z][a-z0-9_]{0,63}", emote["name"])


def test_the_moves_it_declares_are_moves_it_offers_and_replies_stay_short():
    m = manifest(MOVES)
    assert set(m["moves"]) == {"comfort", "agree", "unsure"}
    for move in m["moves"].values():
        assert move["emote"]["name"] in m["tools"][0]["inputSchema"]["properties"]["name"]["enum"]
    assert m["output"] == {"medium": "speech", "max_chars": 60}  # the 12-word target


def test_max_s_fits_the_longest_declared_move_with_a_second_to_spare():
    assert manifest(MOVES)["tools"][0]["max_s"] == 17  # ceil(15.7) + 1
    short = {name: MOVES[name] for name in ("cheerful1", "yes1")}
    assert manifest(short)["tools"][0]["max_s"] == 3  # ceil(2.0) + 1


def test_each_name_carries_the_first_line_of_its_library_description():
    name = manifest(MOVES)["tools"][0]["inputSchema"]["properties"]["name"]
    assert "boredom1: You're about to fall asleep because the conversation is boring." \
        in name["description"]
    assert "2 minutes" not in name["description"]  # one line each: the bench's finding
    assert all(f"{n}:" in name["description"] for n in EMOTIONS)


def test_the_schema_stays_in_the_subset_the_kernel_and_ollama_check():
    """Use enum, which the kernel validates and reads to keep labels unspoken.

    The kernel's checker ignores oneOf/const, so an oneOf vocabulary would let any
    name through. Ollama's format grammar takes enum. Descriptions are plain text.
    """
    name = manifest(MOVES)["tools"][0]["inputSchema"]["properties"]["name"]
    assert set(name) == {"type", "enum", "description"} and name["type"] == "string"
    assert json.loads(json.dumps(manifest(MOVES))) == manifest(MOVES)


def test_a_body_that_moves_declares_its_parts_and_body_plan():
    from reachy_sage_voice.skills import BODY_PLAN

    assert "body" not in manifest(MOVES)
    assert "body_plan" not in [t["name"] for t in manifest(MOVES)["tools"]]
    declared = manifest(MOVES, body=True)
    body = declared["body"]
    assert isinstance(body, str) and len(body) <= 300  # protocol §2
    assert all(part in body for part in ("head", "rotat", "antennas", "speaker", "microphones"))
    assert "camera" in body and "cannot see" in body  # it finds faces; it never sees for the model
    assert declared["tools"][-1] == BODY_PLAN


def test_body_and_body_plan_cost_under_450_tokens_of_the_kernels_3600():
    """The kernel's budget test counts chars/4; a tool reads `name: description Input: schema`.

    450, was 250: the schema generated from skills.py has one step shape per kind of skill
    (~416 tokens, was ~224), so the kernel and its constrained decoding take what parse takes.
    """
    from reachy_sage_voice.skills import BODY_PLAN

    schema = json.dumps(BODY_PLAN["inputSchema"])
    tool = f"{BODY_PLAN['name']}: {BODY_PLAN['description']} Input: {schema}"
    assert (len(manifest(MOVES, body=True)["body"]) + len(tool)) / 4 < 450
    for part in ("Watch:", "Head:", "Rotate:", "Hear:"):  # by component, so the model can compose
        assert part in BODY_PLAN["description"]


def test_a_presence_send_never_takes_the_voice_down():
    from reachy_sage_voice.pipeline import report_presence

    class Down:
        async def presence(self, state, confidence):
            raise KernelUnavailableError("refused")
    asyncio.run(report_presence(Down(), {"kind": "presence", "state": "seen",
                                         "confidence": 1.0}))
