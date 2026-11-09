"""Body plans: up to four flat steps, from the device grammar or the kernel's ``body_plan`` tool.

A step is a skill, maybe an argument, and maybe ``until`` (``face_found`` or
``timeout``) with ``timeout_s``. A search lasts up to the planner's cap
(``behaviour/search.py``) and a dance ``DANCE_S`` unless given a ``timeout_s`` (a
dance's up to ``MAX_DANCE_S``).
``parse`` checks a plan before anything moves: it returns the steps, or a short
reason it was refused. A turn past the body's reach, or a wait longer than
allowed, is cut to the limit and carries the note ``out_of_range``, which the
executor reports with the result.
"""

from dataclasses import asdict, dataclass

from reachy_sage_voice.skills import DANCE_S, LOOKS, MAX_STEPS, MAX_TURN, SKILLS

#: Skills that keep going once started; without a timeout nothing can follow them.
OPEN_ENDED = tuple(name for name, skill in SKILLS.items() if skill.open_ended)
OUT_OF_RANGE = "out_of_range"


@dataclass(frozen=True)
class Step:
    """One skill to perform. ``arg`` is a look direction or degrees (positive is left)."""

    skill: str
    arg: str | int | None = None
    until: str | None = None
    timeout_s: float | None = None
    note: str | None = None  # out_of_range: the request was cut to the limit


Plan = tuple[Step, ...]


class _RefusedError(ValueError):
    """Why a plan cannot be performed."""


def parse(data, device: bool = False) -> Plan | str:
    """Check ``{"steps": [...]}`` into a Plan; return the reason instead if it is refused.

    ``device``: the device grammar's plan, which may also use the device-only skills.
    """
    try:
        if not (isinstance(data, dict) and set(data) == {"steps"}
                and isinstance(data["steps"], list)):
            raise _RefusedError('a plan is {"steps": [...]}')
        items = data["steps"]
        if not 1 <= len(items) <= MAX_STEPS:
            raise _RefusedError(f"a plan has 1 to {MAX_STEPS} steps")
        skills = {n: s for n, s in SKILLS.items() if device or not s.device}
        plan = tuple(_step(item, skills) for item in items)
    except _RefusedError as error:
        return str(error)
    for step in plan[:-1]:
        if step.skill in OPEN_ENDED and step.until is None:
            return f"{step.skill} never ends, so nothing can follow it; give it a timeout"
    return plan


def _step(item, skills: dict) -> Step:
    if not isinstance(item, dict) or not set(item) <= {"skill", "arg", "until", "timeout_s"}:
        raise _RefusedError("a step has skill, and maybe arg, until and timeout_s")
    skill, arg, until, timeout = (item.get(k) for k in ("skill", "arg", "until", "timeout_s"))
    if skill not in skills:
        raise _RefusedError(f"unknown skill {skill!r}")
    note = None
    rules = skills[skill]
    kind = rules.arg
    if kind == "name" and not isinstance(arg, str):
        raise _RefusedError(f"{skill} takes a name")
    if kind == "direction" and arg not in LOOKS:
        raise _RefusedError(f"look takes one of {', '.join(LOOKS)}")
    if kind == "degrees":
        arg = _degrees(arg)
        if abs(arg) > MAX_TURN:
            arg, note = max(-MAX_TURN, min(MAX_TURN, arg)), OUT_OF_RANGE
    if kind is None and arg is not None:
        raise _RefusedError(f"{skill} takes no arg")
    if until is not None and until not in rules.until:
        raise _RefusedError(f"{skill} cannot wait until {until!r}")
    if timeout is not None:
        if rules.timeout_max is None:
            raise _RefusedError(f"{skill} takes no timeout")
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
            raise _RefusedError("timeout_s is a number of seconds above 0")
        timeout = float(timeout)
        if timeout > rules.timeout_max:
            timeout, note = rules.timeout_max, OUT_OF_RANGE
    if until == "timeout" and timeout is None:
        raise _RefusedError(f"{skill} until timeout needs timeout_s")
    if skill == "search_face":
        until = "face_found"  # no timeout: the planner caps it
    if skill == "dance":
        timeout = timeout or DANCE_S
    return Step(skill, arg, until, timeout, note)


def _degrees(arg) -> int:
    if arg == "around":
        return MAX_TURN
    if isinstance(arg, bool):
        raise _RefusedError("turn takes degrees, or around")
    try:
        return round(float(arg))
    except (TypeError, ValueError):
        raise _RefusedError("turn takes degrees, or around") from None


def dump(plan: Plan) -> dict:
    """Return the plan as ``{"steps": [...]}``, as ``parse`` reads it (without the notes)."""
    return {"steps": [{k: v for k, v in asdict(step).items() if v is not None and k != "note"}
                      for step in plan]}
