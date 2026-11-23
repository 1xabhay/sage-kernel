"""What a Reachy Mini declares to the kernel: its moves, voice tools and body (protocol §2).

The protocol client that sends it is body-agnostic: ``sage_device.kernel``.
"""

import math
from collections.abc import Mapping

from reachy_sage_voice import song as songs
from reachy_sage_voice import voice
from reachy_sage_voice.skills import BODY_PLAN

#: The clips this body plays, all from the robot's recorded-moves
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

def load_moves(vocabulary: tuple[str, ...] = EMOTIONS) -> dict:
    """Load the declared moves from the robot's recorded-moves library, by name.

    The library reads the local Hugging Face cache first (the robot daemon
    preloads it) and downloads only if it is missing.
    """
    from reachy_mini.motion.recorded_move import DEFAULT_EMOTIONS_DATASET, RecordedMoves

    library = RecordedMoves(DEFAULT_EMOTIONS_DATASET)
    return {name: library.get(name) for name in vocabulary}


#: The body as the model is told it (protocol §2, at most 300 characters): every part, and that the
#: camera only finds and follows faces, so the model never claims to see.
_PARTS = ("A Reachy Mini: a head that turns and tilts on a rotating body, two antennas, a speaker, "
          "microphones, and a camera ")
BODY = _PARTS + "only for finding and following faces; you cannot see through it."
#: With sight (a vision model is named, ``pipeline.sight``): what it sees, when, and what never.
SEEING = (_PARTS + "that finds and follows faces. You see through it only in words: you are told "
          "what it shows when asked what you see, or when the scene changes. "
          "A person is only a person, never who.")


def manifest(moves: Mapping, song: bool = False, body: bool = False,
             sight: bool = False) -> dict:
    """Describe what this body can do, as MCP-shaped tools. Replies are not a tool in v1.

    The tools are emote, then voice_style and vocal (``voice.py``), then
    listen_to_song when ``song`` (a research service is configured, ``song.py``),
    then body_plan (``skills.py``) when ``body`` (the executor moves the body), with
    the body's description. ``sight``: the camera's frames reach the model as words
    (``sage_device.vision``), so the body declares the sense and describes it.

    ``moves`` maps each clip to its recorded move (``.duration``). ``max_s`` is the
    longest of them, rounded up, plus 1 s, so the kernel never times out a move still
    playing. The model names a feeling (``expressions.INTENTS``, those with a clip in
    ``moves``); the body picks the clip (``expressions.emotion``). The 59 clip names and
    a line on each were 4.7k of the system prompt's 8.7k characters, and two clips were
    72% of what it played (journal 11-20).

    The reply's length is the kernel's default (160 characters, ``sage.manifest.MAX_REPLY``,
    about 10 s aloud). This body used to declare 60, about 12 words: too short for a
    companion's two sentences, and 30 of 668 spoken replies ran into it.
    """
    from reachy_sage_voice.expressions import INTENTS  # it reads EMOTIONS from here

    feelings = [feeling for feeling, clips in INTENTS.items() if any(c in moves for c in clips)]
    declared = {"senses": ["speech", *(["vision"] if sight else [])], "tools": [{
        "name": "emote",
        "description": "Show a feeling with the body as you speak.",
        "inputSchema": {"type": "object",
                        "properties": {"name": {"type": "string", "enum": feelings}},
                        "required": ["name"]},
        "max_s": math.ceil(max(m.duration for m in moves.values())) + 1,
        "interruptible": True,
    }, *voice.tools(moves), *([songs.TOOL] if song else []), *([BODY_PLAN] if body else [])]}
    if body:
        declared["body"] = SEEING if sight else BODY
    return declared

