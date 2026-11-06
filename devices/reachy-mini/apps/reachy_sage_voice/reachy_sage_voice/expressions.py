"""Recorded moves by voice: emotion intents and Pollen's recorded dances, for the device grammar.

The intent table is Pollen's (reachy_mini_conversation_app, Apache-2.0, c178d4a,
tools/play_emotion.py:126-168; see NOTICE), kept to the moves this body declares
(``kernel.EMOTIONS``: never a negative one, owner), plus a few of ours (curious,
proud, cheerful, shy, tired). Intents with nothing left (angry, disgusted,
dying...) are dropped. The words people say ("happy", "afraid") map onto intents.
"""

import random

from reachy_sage_voice.manifest import EMOTIONS

_POLLEN = {
    "happy": ("laughing2", "laughing1"), "excited": ("dance3", "dance2"),
    "loving": ("loving1",), "grateful": ("grateful1",), "success": ("success1", "success2"),
    "thinking": ("thoughtful1", "thoughtful2"), "attentive": ("attentive1", "attentive2"),
    "confused": ("confused1",), "uncertain": ("uncertain1",),
    "sad": ("sad1", "sad2", "downcast1"), "lonely": ("lonely1",),
    "scared": ("scared1", "fear1", "anxiety1"), "anxious": ("anxiety1", "fear1", "scared1"),
    "surprised": ("surprised1", "surprised2", "amazed1"), "amazed": ("amazed1", "surprised1"),
    "calming": ("calming1",), "relief": ("relief1", "relief2"), "impatient": ("impatient2",),
    "embarrassed": ("shy1",), "bored": ("boredom2", "boredom1"),
    "yes": ("yes1", "understanding2"), "no": ("no1",), "welcoming": ("welcoming2",),
    "helpful": ("helpful1",), "electric": ("electric1",),
}
_OURS = {"curious": ("curious1", "inquiring1", "inquiring2"), "proud": ("proud1", "proud2"),
         "cheerful": ("cheerful1", "enthusiastic1"), "tired": ("tired1",)}
#: Intent -> the declared emotions that show it.
INTENTS = {intent: kept for intent, moves in {**_POLLEN, **_OURS}.items()
           if (kept := tuple(m for m in moves if m in EMOTIONS))}
#: What people say -> intent.
WORDS = {**{intent: intent for intent in INTENTS},
         "joyful": "happy", "glad": "happy", "in love": "loving", "thankful": "grateful",
         "thoughtful": "thinking", "unsure": "uncertain", "afraid": "scared",
         "frightened": "scared", "nervous": "anxious", "calm": "calming",
         "relieved": "relief", "shy": "embarrassed", "sleepy": "tired", "inquisitive": "curious"}
#: Left out as words: "be helpful", "show me yes" are not asks for a move.
WORDS = {word: intent for word, intent in WORDS.items()
         if intent in INTENTS and word not in ("yes", "no", "success", "helpful")}

#: A reaction (``sage_device.reflex``) -> the clips that show it, chosen by running the library
#: through the arbiter's caps at 50 Hz (owner: slow and gentle). Worst head lag behind the clip,
#: then first visible move: welcoming2 6 deg 0.56 s, laughing2 4 deg 0.12 s, dance1 3 deg 0.2 s;
#: grateful1 3 deg 0.62 s, loving1 1 deg 0.56 s, serenity1 0 deg 0.7 s (antennas only). Left out:
#: welcoming1 (22 deg, first moves at 1.3 s), enthusiastic2 and cheerful1 (46, 44 deg).
#: ponytail: the owner's eye decides; these are all under 5.7 s (reflex.GESTURE_S is 6).
REACTIONS = {"greet": ("welcoming2", "laughing2", "dance1"),
             "praised": ("grateful1", "loving1", "serenity1"),
             # For a statement answered by a move alone (``sage_device.choice``): 0-3 deg head lag
             # for comfort, up to 12 for agree (nods); none droops. calming1 is 6.1 s: a body still
             # playing means words next, by ``gesture_phase``.
             "comfort": ("calming1", "serenity1"),
             "agree": ("understanding1", "thoughtful1", "attentive1")}

#: Pollen's recorded dances (the daemon preloads the dataset), less the four that snap
#: (dance.py leaves the same ones out).
DANCES_DATASET = "pollen-robotics/reachy-mini-dances-library"
DANCES = ("groovy_sway_and_roll", "chin_lead", "dizzy_spin", "head_tilt_roll",
          "interwoven_spirals", "neck_recoil", "pendulum_swing", "polyrhythm_combo",
          "sharp_side_tilt", "side_glance_flick", "side_peekaboo", "side_to_side_sway",
          "simple_nod", "stumble_and_recover", "uh_huh_tilt")
DANCE_NAMES = {"groovy": "groovy_sway_and_roll"}

#: Said for "what moves can you do?".
MOVES_LINE = ("I can show feelings, like happy, sad, surprised, curious or shy: say 'show me "
              "happy'. I can do a dance move, or my groovy dance, or dance to your music. And "
              "I can look for you, follow you, look around and turn.")


def emotion(intent: str, rng: random.Random = random) -> str | None:
    """Pick a declared emotion for ``intent`` (or a word for one), at random among its moves."""
    moves = INTENTS.get(WORDS.get(intent, intent))
    return rng.choice(moves) if moves else None


def dance(name: str, rng: random.Random = random) -> str | None:
    """Pick a recorded dance: a named one, or any for ``any``."""
    if name == "any":
        return rng.choice(DANCES)
    name = DANCE_NAMES.get(name, name)
    return name if name in DANCES else None
