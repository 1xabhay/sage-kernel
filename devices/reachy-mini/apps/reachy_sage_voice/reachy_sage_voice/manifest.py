"""What a Reachy Mini declares to the kernel: its moves, voice tools and body (protocol §2).

The protocol client that sends it is body-agnostic: ``sage_device.kernel``.
"""

import math
import re
from collections.abc import Mapping

from reachy_sage_voice import song as songs
from reachy_sage_voice import voice
from reachy_sage_voice.skills import BODY_PLAN

#: The body language the model may choose, all from the robot's recorded-moves
#: library: the pet's 30 (reachy_mini_pet/live.py), the expressive moves the
#: owner added, and every variant of an offered move. Never a negative one
#: (rage, furious, contempt, disgusted, dying, reprimand, irritated, displeased,
#: frustrated): owner. Copied, not imported: this app never loads the pet.
EMOTIONS = (
    # the pet's 30
    "amazed1", "attentive1", "boredom1", "calming1", "cheerful1", "confused1", "curious1",
    "dance1", "enthusiastic1", "grateful1", "helpful1", "impatient1", "inquiring1",
    "laughing1", "loving1", "no1", "oops1", "proud1", "relief1", "sad1", "scared1",
    "serenity1", "shy1", "success1", "surprised1", "thoughtful1", "tired1",
    "understanding1", "welcoming1", "yes1",
    # expressive moves
    "waiting", "come1", "go_away1", "lonely1", "uncertain1", "electric1", "yes_sad1",
    "no_excited1", "no_sad1",
    # further variants of offered moves
    "attentive2", "boredom2", "dance2", "dance3", "enthusiastic2", "helpful2", "impatient2",
    "inquiring2", "inquiring3", "laughing2", "oops2", "proud2", "proud3", "relief2", "sad2",
    "success2", "surprised2", "thoughtful2", "understanding2", "welcoming2",
)

#: Moves for the coming mood model, not the emote tool: declared so the
#: vocabulary contract keeps them honest, never offered to the model yet.
STATE_MOVES = ("sleep1", "mini-deep-sleep", "wake-mini-up", "toc-toc-toc")

#: Declared moves the library records without a sound; they always play muted.
NO_SOUND = frozenset({"waiting"})

#: One clear line for the moves whose library description is weak: it names
#: another move, or does not say when to use it. The library stays the source
#: for every other move.
DESCRIPTIONS = {
    "attentive2": "Listen closely while someone is talking, to encourage them to go on.",
    "boredom2": "Doze off and snore, when no one has talked to you for a long while.",
    "cheerful1": "A happy little whistle, when someone proposes something that pleases you.",
    "confused1": "Use it when you don't know how to answer a question; it can be funny.",
    "curious1": "Glance around at everyone, in a conversation with several people.",
    "dance3": "Dance energetically and wiggle, when lively music is playing.",
    "enthusiastic2": "Light excitement, for everyday good news.",
    "impatient2": "Show impatience, when you disagree or someone is stalling.",
    "inquiring2": "A light questioning look, when you are thinking and need more details.",
    "laughing2": "A light laugh, for a mildly funny remark.",
    "proud1": "Look around with a satisfied air, when something has gone well.",
    "relief2": "Settle down calmly, when mild annoyance or tension passes.",
    "waiting": "Wait patiently, when you expect someone to answer or act.",
}


#: This body's move for each role the kernel teaches silence with (protocol §2, Moves).
MOVES = {"comfort": "calming1", "agree": "understanding1", "unsure": "confused1"}

#: Longest reply in characters, about 12 spoken words (the pet scorecard's target). The prompt
#: asks for a few words; a small model may not listen. The kernel cuts at the cap back to a
#: sentence, else a word: of 131 replies 2026-11-01..08, 3 were over 60, 2 would end at a word.
#: ponytail: a hard cap on every reply; raise it if real answers get cut.
MAX_CHARS = 60


def load_moves(vocabulary: tuple[str, ...] = EMOTIONS) -> dict:
    """Load the declared moves from the robot's recorded-moves library, by name.

    The library reads the local Hugging Face cache first (the robot daemon
    preloads it) and downloads only if it is missing.
    """
    from reachy_mini.motion.recorded_move import DEFAULT_EMOTIONS_DATASET, RecordedMoves

    library = RecordedMoves(DEFAULT_EMOTIONS_DATASET)
    return {name: library.get(name) for name in vocabulary}


def describe_move(name: str, move) -> str:
    """One line on what a move means: the override, else the library's first sentence."""
    if name in DESCRIPTIONS:
        return DESCRIPTIONS[name]
    return re.split(r"(?<=[.!?])\s", move.description.strip(), maxsplit=1)[0]


#: The body as the model is told it (protocol §2, at most 300 characters): every part, and that the
#: camera only finds and follows faces, so the model never claims to see.
BODY = ("A Reachy Mini: a head that turns and tilts on a rotating body, two antennas, a speaker, "
        "microphones, and a camera only for finding and following faces; "
        "you cannot see through it.")


def manifest(moves: Mapping, song: bool = False, body: bool = False) -> dict:
    """Describe what this body can do, as MCP-shaped tools. Replies are not a tool in v1.

    The tools are emote, then voice_style and vocal (``voice.py``), then
    listen_to_song when ``song`` (a research service is configured, ``song.py``),
    then body_plan (``skills.py``) when ``body`` (the executor moves the body), with
    the body's description.

    ``moves`` maps each declared name to its recorded move (``.duration``,
    ``.description``). ``max_s`` is the longest of them, rounded up, plus 1 s,
    so the kernel never times out a move still playing. Each name's first
    line (``describe_move``) rides beside the enum, which is what the kernel checks.
    """
    names = list(moves)
    meanings = "\n".join(f"{name}: {describe_move(name, moves[name])}" for name in names)
    declared = {"senses": ["speech"], "tools": [{
        "name": "emote",
        "description": "Show a feeling with the body, alone or just before speaking.",
        "inputSchema": {"type": "object",
                        "properties": {"name": {"type": "string", "enum": names,
                                                "description": meanings}},
                        "required": ["name"]},
        "max_s": math.ceil(max(m.duration for m in moves.values())) + 1,
        "interruptible": True,
    }, *voice.tools(moves), *([songs.TOOL] if song else []), *([BODY_PLAN] if body else [])],
        "moves": {role: {"emote": {"name": name}} for role, name in MOVES.items() if name in names},
        "output": {"medium": "speech", "max_chars": MAX_CHARS}}
    if body:
        declared["body"] = BODY
    return declared

