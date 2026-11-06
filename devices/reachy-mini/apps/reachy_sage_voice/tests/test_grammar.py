"""The device grammar: a fast lane for stop, stop tracking and the owner's commands only."""

import json
from pathlib import Path

import pytest

from reachy_sage_voice.grammar import (
    Command,
    Context,
    Drop,
    Gesture,
    KernelTurn,
    Song,
    plan_for,
    route,
)
from reachy_sage_voice.plan import Step, parse
from reachy_sage_voice.skills import MAX_TURN

SEARCH, TRACK = Step("search_face", until="face_found"), Step("track_face")
STOP, STOP_TRACKING, DANCE = Step("stop"), Step("stop_tracking"), Step("dance", timeout_s=180.0)

COMMANDS = [
    ("Stop.", (STOP,)),
    ("stop!", (STOP,)),
    ("Please stopp.", (STOP,)),
    ("Top!", (STOP,)),  # STT's slip on a lone "stop"
    ("Reachy, stop.", (STOP,)),
    ("Sage, stop.", (STOP,)),
    ("Stop it.", (STOP,)),
    ("Stop moving.", (STOP,)),
    ("Stop dancing.", (STOP,)),
    ("Freeze!", (STOP,)),
    ("Stop tracking me.", (STOP_TRACKING,)),
    ("Reachy, stop tracking.", (STOP_TRACKING,)),
    ("Stop following me.", (STOP_TRACKING,)),
    ("Stop face tracking.", (STOP_TRACKING,)),
    ("Stop looking at me.", (STOP_TRACKING,)),
    # the owner's commands (2026-10-23)
    ("Reachy, look for me.", (SEARCH,)),
    ("Reachy, start face tracking me.", (TRACK,)),
    ("Reachy, dance to the music coming from my speakers.", (DANCE,)),
    ("Find me.", (SEARCH,)),
    ("Sage, find me.", (SEARCH,)),
    ("Look for me.", (SEARCH,)),
    ("Luke for it.", (SEARCH,)),  # STT's slip on the verb
    ("Where am I?", (SEARCH,)),
    ("Track me.", (TRACK,)),
    ("Follow me.", (TRACK,)),
    ("Follow my face.", (TRACK,)),
    ("Can you track me?", (TRACK,)),
    ("Start tracking me.", (TRACK,)),
    ("Start face tracking.", (TRACK,)),
    ("Dance.", (DANCE,)),
    ("Dance to the music.", (DANCE,)),
    ("Dance to this song!", (DANCE,)),
    ("Start dancing.", (DANCE,)),
    ("Search for me and then track me.", (SEARCH, TRACK)),
    ("Hey Reachy, could you find me and follow me?", (SEARCH, TRACK)),
    # the robot's name as the STT may hear it
    ("Richie, find me.", (SEARCH,)),
    ("Ritchie, stop now.", (STOP,)),
    ("Reachie, find me.", (SEARCH,)),
    ("Ricci, dance.", (DANCE,)),
    ("Reechy, follow me.", (TRACK,)),
    # and near misses of "Reachy" that are no English word
    ("Reachi, stop tracking me.", (STOP_TRACKING,)),
    ("Hey Rechy, find me.", (SEARCH,)),
    ("Reache, dance.", (DANCE,)),
    # recorded moves by voice (owner, 2026-10-23)
    ("Sage, show me happy.", (Step("emote", "happy"),)),
    ("Show me you're happy.", (Step("emote", "happy"),)),
    ("Be surprised!", (Step("emote", "surprised"),)),
    ("Look sad.", (Step("emote", "sad"),)),
    ("Act curious.", (Step("emote", "curious"),)),
    ("Reachy, can you look scared?", (Step("emote", "scared"),)),
    ("Make a happy face.", (Step("emote", "happy"),)),
    ("Show me afraid.", (Step("emote", "afraid"),)),
    ("Sage, do your groovy dance.", (Step("dance_move", "groovy"),)),
    ("Do a dance move.", (Step("dance_move", "any"),)),
    ("Show me a dance move!", (Step("dance_move", "any"),)),
    ("Do a little dance.", (Step("dance_move", "any"),)),
    ("Sage, what moves can you do?", (Step("moves"),)),
    ("What emotions do you know?", (Step("moves"),)),
]

AROUND, LOOK_ME = Step("turn", MAX_TURN), Step("look", "me")

#: Loose phrasings and STT slips the fast lane must catch, not leave to the kernel's model.
OWNER = [
    ("Alright, so it's search for me.", (SEARCH,)),
    ("Start fist tracking and look for it.", (SEARCH, TRACK)),  # STT's "face tracking"
    ("Where are you looking now? In the garden shed? Hey, I'm right here. "
     "Turn around.", (AROUND,)),
    ("Hey, I'm right here. Turn around.", (AROUND,)),
    ("Listen to the song I am humming and then accordingly dance to.", (DANCE,)),
    ("Alright, look ahead.", (Step("look", "center"),)),
    ("Alright, turn around.", (AROUND,)),
    ("Now turn around your whole body.", (AROUND,)),
    ("Look for me.", (SEARCH,)),
    ("Reachy, look for me.", (SEARCH,)),
    ("Now rotate your lower body.", (AROUND,)),  # the body, not the head: around
    ("Then you find me, I'm behind you.", (SEARCH,)),
    # turning and looking
    ("Turn around.", (AROUND,)),
    ("Sage, turn around.", (AROUND,)),
    ("Turn left.", (Step("turn", 90),)),
    ("Turn right.", (Step("turn", -90),)),
    ("Turn to the right.", (Step("turn", -90),)),
    ("Turn left 45 degrees.", (Step("turn", 45),)),
    ("Rotate your body to the left.", (Step("turn", 90),)),
    ("Look left.", (Step("look", "left"),)),
    ("Look to the right.", (Step("look", "right"),)),
    ("Look up.", (Step("look", "up"),)),
    ("Look down.", (Step("look", "down"),)),
    ("Look at me.", (LOOK_ME,)),
    ("Stop and look at me.", (STOP, LOOK_ME)),
    ("Turn around and dance.", (AROUND, DANCE)),
    # tracking: "face" as the STT mishears it, only before "track"
    ("Face track me.", (TRACK,)),
    ("Start face tracking.", (TRACK,)),
    ("Start phase tracking now.", (TRACK,)),
    ("Faith track me.", (TRACK,)),
    ("Okay, track me.", (TRACK,)),
    ("Follow me.", (TRACK,)),
    ("Stop fist tracking me.", (STOP_TRACKING,)),
    # searching with filler
    ("So, look for me.", (SEARCH,)),
    ("Let's search for me.", (SEARCH,)),
    ("Okay so find me.", (SEARCH,)),
    ("Look for it.", (SEARCH,)),
    # dancing to the music
    ("Dance with this music.", (DANCE,)),
    ("Dance to the music.", (DANCE,)),
    ("Dance to.", (DANCE,)),  # cut off by the turn's end
    ("Dance accordingly.", (DANCE,)),
    ("Listen to the music and dance.", (DANCE,)),
    ("Listen to this song and then dance to it.", (DANCE,)),
]

#: Chit-chat, or commands too garbled to act on safely.
OWNER_NOT_COMMANDS = [
    "Maybe next week or like in like a month or something.",
    "Is it dance with this rain?",  # maybe "dance with this music", but a question: a miss
    "Switch it on.",
    "I guess I'll give them time.",
    "There's a fear of heights.",
    "Music is okay.",
    "You don't like music?",
    "Sage and you catch the song.",
    "Rotate your body towards the right in the kitchen.",
    "Spent",
]

#: Moves the kernel's model understands and plans with body_plan: not the device's fast lane.
KERNEL = [
    "Look left then right.",
    "Listen to this song.",
    "Listen to the music.",
]

CHAT = [
    "I looked left yesterday.",
    "Don't stop believing.",
    "Don't stop.",
    "Stop talking about the weather.",
    "We should stop by the shop later.",
    "Stop tracking my package.",
    "Look, I think we should talk.",
    "Can you find me a recipe?",
    "Follow the money.",
    "Follow me on Instagram.",
    "That's the top of the hill.",
    "On top of it.",
    "I love to dance.",
    "Do you like dancing?",
    "That was a great dance.",
    "Where am I going to put this?",
    "Hello there.",
    "",
    "Left.",
    # ordinary words a letter or two from "Reachy" are not its name
    "Ready? Find me.",
    "Really, stop.",
    "Reach for me.",
    "Peachy, dance!",
    "Each, stop.",
    "Teach me to dance.",
    "Racy, dance.",
    # not a move by voice
    "I am happy.",
    "Are you happy?",
    "You look happy.",
    "Don't be sad.",
    "Be quiet.",
    "Look angry.",  # a negative move: never offered (owner)
    "Happy birthday!",
    "Be happy for me.",
    "What can you do?",
    "Do you know any dance moves?",
    "Show me the way.",
    # filler and the new verbs stay conservative
    "Okay.",
    "So I love to dance.",
    "Turn left at the light.",
    "Turn around, bright eyes.",
    "Look up the weather.",
    "Look at that.",
    "Look for my keys.",
    "Phase tracking is hard.",
    "We work on phase tracking.",
    "Fist bump.",
    "Track it.",
    "Can you track my package?",
    "I don't want to dance to this music.",
    "You know what I hate? Dancing.",
    "Ready? Turn around.",
    "I told him. Stop.",
    "Is it dancing?",
    "It's a dance song.",
]


@pytest.mark.parametrize("text, plan", COMMANDS)
def test_a_command_maps_to_its_plan(text, plan):
    assert plan_for(text) == plan


@pytest.mark.parametrize("text, plan", OWNER)
def test_the_owners_phrasings_map_to_their_plan(text, plan):
    assert plan_for(text) == plan


@pytest.mark.parametrize("text", KERNEL + CHAT + OWNER_NOT_COMMANDS)
def test_anything_else_is_left_to_the_kernel(text):
    assert plan_for(text) is None


def test_every_emotion_word_resolves_to_a_declared_move():
    from reachy_sage_voice import expressions
    from reachy_sage_voice.manifest import EMOTIONS

    for word in expressions.WORDS:
        assert expressions.emotion(word) in EMOTIONS, word
        assert plan_for(f"show me {word}") == (Step("emote", word),), word


#: The name anchors a command after chit-chat.
NAME_ANCHORED = [
    ("I think you're sleepy sage, find me.", (SEARCH,)),
    ("Ok so, Reachy, start face tracking me.", (TRACK,)),
    ("Hey sage stop tracking me.", (STOP_TRACKING,)),
    ("Um, Sage, find me.", (SEARCH,)),
    ("I can't see you, Reachy, look for me.", (SEARCH,)),
    ("Well, Richie, stop.", (STOP,)),
    ("You're too fast Reachy, stop moving.", (STOP,)),
    ("Alright then, Sage, dance to the music.", (DANCE,)),
    ("This song is great, Reachy, dance!", (DANCE,)),
    ("Where did you go, Sage, find me and follow me.", (SEARCH, TRACK)),
    ("Okay, Ritchie, follow me please.", (TRACK,)),
    ("I'm over here Sage, where am I?", (SEARCH,)),
    ("Hmm, Reachie, stop following me.", (STOP_TRACKING,)),
    ("That's enough, Sage, stop tracking.", (STOP_TRACKING,)),
    ("Nice one, Reachy, show me happy.", (Step("emote", "happy"),)),
    ("So Sage, what moves can you do?", (Step("moves"),)),
    ("Hello Reechy, can you track me?", (TRACK,)),
]

NOT_ANCHORED = [
    "I told Sage to look for me yesterday.",
    "Sage looked for me earlier.",
    "Sage, don't look for me.",
    "Sage, never stop.",
    "I think you're moving sage, look for me later maybe.",
    "Did Sage find me?",
    "I wish Reachy would dance.",
    "I told Reachy, look for me.",
    "My friend said Reachy, stop.",
    "I asked Sage, find me.",
    "Sage, look for my keys.",
    "Sage, I love to dance.",
    "Don't, Reachy, look for me.",
    "The sage in my garden, it stops.",
    "When I say Reachy, dance, it dances.",
    "Sage, stop talking about the weather.",
    "Ready, find me.",
    "I don't want Reachy, look for me.",
]


@pytest.mark.parametrize("text, plan", NAME_ANCHORED)
def test_the_name_anchors_a_command_after_chit_chat(text, plan):
    assert plan_for(text) == plan


@pytest.mark.parametrize("text", NOT_ANCHORED)
def test_a_name_in_a_narrative_or_before_no_command_is_the_kernels(text):
    assert plan_for(text) is None


def test_look_for_me_is_the_search_plan_whatever_the_robot_is_doing():
    # live 2026-10-23: said while face tracking was on; tracking state is no grammar input
    assert plan_for("Reachy, look for me.") == (SEARCH,)


# --- the router: route(heard, context), over the golden set --------------------------------

#: Script block C and not-commands: chat, song requests, and what is heard while a song is listened
#: to. The owner's logged phrasings and the STT's slips on them stay in data/local, never tracked.
DATA = Path(__file__).parent / "data"
LOCAL = DATA / "local" / "commands_real.jsonl"
GOLDEN = [json.loads(line) for path in (DATA / "commands_real.jsonl", LOCAL) if path.exists()
          for line in path.read_text().splitlines()]
_KINDS = {"song": Song, "kernel": KernelTurn, "drop": Drop}


def _routed(row):
    return route(row["text"], Context(listening=row.get("listening", False)))


def _right(row) -> bool:
    got = _routed(row)
    if row["intent"] == "command":
        return got == Command(parse({"steps": row["steps"]}, device=True))
    return isinstance(got, _KINDS[row["intent"]])


def _share(rows) -> float:
    return sum(map(_right, rows)) / len(rows)


def test_tracked_golden_set_holds_no_logged_owner_speech():
    tracked = [json.loads(line) for line in (DATA / "commands_real.jsonl").read_text().splitlines()]
    assert not [r["text"] for r in tracked if r["set"] in ("owner", "misheard")]


def test_i1_logged_owner_commands():
    """I1: the owner's and misheard lines ≥ 90%, clean ≥ 97%, every stop 100%."""
    commands = [r for r in GOLDEN if r["intent"] == "command"]
    owner = [r for r in commands if r["set"] in ("owner", "misheard")]
    clean = [r for r in commands if r["set"] == "clean"]
    stops = [r for r in commands if r["steps"][0]["skill"] in ("stop", "stop_tracking")]
    missed = [r["text"] for r in owner + clean if not _right(r)]
    if owner:  # only where the local file exists
        assert _share(owner) >= 0.90, missed
    assert _share(clean) >= 0.97, missed
    assert _share(stops) == 1.0, [r["text"] for r in stops if not _right(r)]


def test_i2_song_requests_route_to_song_and_talk_moves_nothing():
    """I2: a non-command moves the body at most 1 in 50; song requests ≥ 90% reach the song."""
    talk = [r for r in GOLDEN if r["intent"] != "command"]
    moved = [r["text"] for r in talk if isinstance(_routed(r), Command)]
    assert len(talk) >= 50 and len(moved) / len(talk) <= 1 / 50, moved
    songs = [r for r in GOLDEN if r["intent"] == "song"]
    assert _share(songs) >= 0.90, [r["text"] for r in songs if not _right(r)]


def test_stop_always_wins():
    assert route("Stop.", Context(listening=True)) == Command((STOP,))
    assert route("Stop listening to this song.", Context()) == Command((STOP,))  # no song request


def test_while_a_song_is_listened_to_only_commands_get_through():
    assert route("Reachy, look for me.", Context(listening=True)) == Command((SEARCH,))
    assert isinstance(route("We dance all night", Context(listening=True)), Drop)
    assert isinstance(route("What song is this?", Context(listening=True)), Drop)


def test_without_a_body_or_a_song_listener_it_is_the_kernels():
    assert route("Look for me.", Context(body=False)) == KernelTurn("Look for me.")
    assert route("What song is this?", Context(song=False)) == KernelTurn("What song is this?")
    assert route("Listen to the music and dance.", Context()) == Command((DANCE,))
    assert isinstance(route("", Context()), Drop)


# --- gestures: greetings and praise, answered by the body alone ---------------------------------

FREE = Context(gesture=True)


@pytest.mark.parametrize("text, reaction", [
    ("Hi Sage.", "greet"), ("Hello!", "greet"), ("Richie, good morning", "greet"),
    ("Good girl.", "praised"), ("Good boy, Sage!", "praised"), ("Thank you.", "praised"),
    ("Good job, Reachy", "praised")])
def test_greetings_and_praise_are_gestures(text, reaction):
    assert route(text, FREE) == Gesture(reaction)


@pytest.mark.parametrize("text", ["Hi Sage, what's your favourite colour?", "I said hi to him.",
                                  "Thank you for telling me that story.", "Good."])
def test_a_longer_sentence_with_hi_in_it_stays_a_turn(text):
    assert route(text, FREE) == KernelTurn(text)


def test_commands_still_win_over_a_gesture():
    assert isinstance(route("Hi Sage, stop.", FREE), Command)
    assert isinstance(route("Hi, turn left.", FREE), Command)


def test_no_gesture_unless_the_body_is_free():
    assert route("Hi Sage.", Context()) == KernelTurn("Hi Sage.")
    assert route("Thank you.", Context(gesture=True, listening=True)) == Drop("listening to a song")
