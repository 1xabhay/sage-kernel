"""The body's skills: one table, for ``plan.parse`` and for the kernel's ``body_plan`` schema.

The device owns the skill schema (docs/design/LAYERS.md §0, §2.2). Each skill
says what its ``arg`` is, which ``until`` it may wait for, how long a
``timeout_s`` it takes, and whether it keeps going once started. ``schema()``
turns the kernel's skills into the ``body_plan`` tool's JSON Schema, so the
kernel enforces what ``parse`` accepts: every schema-valid plan parses, except
that a schema cannot say an open-ended step with no ``until`` must come last.
"""

from dataclasses import dataclass

from reachy_sage_voice.limits import LIMITS

LOOKS = ("left", "right", "up", "down", "center", "me")
MAX_STEPS = 4
MAX_TURN = round(LIMITS["body_yaw"][1])  # degrees of body yaw either way
MAX_WAIT_S = 30.0
DANCE_S, MAX_DANCE_S = 180.0, 600.0  # a dance ends at stop, when the music ends, or after this


@dataclass(frozen=True)
class Skill:
    """One skill's rules. ``arg``: ``direction`` (a look), ``degrees`` (a turn), ``name``, None."""

    arg: str | None = None
    until: tuple[str, ...] = ()
    timeout_max: float | None = None  # takes a timeout_s up to this many seconds
    open_ended: bool = False  # keeps going once started: nothing follows it without an until
    device: bool = False  # only the device grammar plans it (recorded moves), not the kernel


SKILLS = {
    "look": Skill(arg="direction"),
    "turn": Skill(arg="degrees"),
    "track_face": Skill(until=("timeout",), timeout_max=MAX_WAIT_S, open_ended=True),
    "search_face": Skill(until=("face_found",), timeout_max=MAX_WAIT_S),
    "listen_song": Skill(until=("timeout",), timeout_max=MAX_WAIT_S, open_ended=True),
    "dance": Skill(timeout_max=MAX_DANCE_S, open_ended=True),
    "stop": Skill(),
    "stop_tracking": Skill(),
    "emote": Skill(arg="name", device=True),
    "dance_move": Skill(arg="name", device=True),
    "moves": Skill(device=True),
}
#: Each arg kind as JSON Schema. ``parse`` also takes a turn in a numeric string.
ARGS = {"direction": {"enum": list(LOOKS)},
        "degrees": {"oneOf": [{"type": "number"}, {"const": "around"}]},
        "name": {"type": "string"}}


def _step_schema(names: list[str], skill: Skill) -> dict:
    properties: dict = {"skill": {"enum": names}}
    if skill.arg:
        properties["arg"] = ARGS[skill.arg]
    if skill.until:
        properties["until"] = {"enum": list(skill.until)}
    if skill.timeout_max:
        properties["timeout_s"] = {"type": "number", "exclusiveMinimum": 0}
    return {"properties": properties, "required": ["skill", *(["arg"] if skill.arg else [])],
            **({"dependentRequired": {"until": ["timeout_s"]}} if "timeout" in skill.until else {}),
            "additionalProperties": False}


def schema() -> dict:
    """Return the kernel's plan as JSON Schema: one step shape per kind of skill."""
    alike: dict[Skill, list[str]] = {}
    for name, skill in SKILLS.items():
        if not skill.device:
            alike.setdefault(skill, []).append(name)
    steps = {"oneOf": [_step_schema(names, skill) for skill, names in alike.items()]}
    return {"type": "object", "properties": {"steps": {
        "type": "array", "minItems": 1, "maxItems": MAX_STEPS, "items": steps}},
        "required": ["steps"], "additionalProperties": False}


#: The kernel tool, described by the body's parts, tersely: it costs prompt on every turn.
BODY_PLAN = {
    "name": "body_plan",
    "description": (
        f"Use your body when asked; up to {MAX_STEPS} steps in order. Watch: search_face (all "
        "round, then follow), track_face (until stop_tracking). Head: look. "
        f"Rotate: turn, degrees (+ left, max {MAX_TURN}) or around. Hear: listen_song, dance. "
        "stop."),
    "inputSchema": schema(),
    "max_s": 180,  # a search all the way round (~80 s on the robot) and three steps of 30 s
    "interruptible": True,
}
