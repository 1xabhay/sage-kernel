"""The skill schema: one table, for both ``parse`` and the kernel's ``body_plan`` schema."""

import itertools
from dataclasses import replace

from reachy_sage_voice.manifest import manifest
from reachy_sage_voice.plan import dump, parse
from reachy_sage_voice.skills import BODY_PLAN, schema

MOVES = {"yes1": type("Move", (), {"duration": 2.0, "description": "Yes."})()}
NUMBERS = (-400, -45, 0.5, 90, 400)
ABSENT = object()


def valid(node, value) -> bool:
    """Check a value against JSON Schema, as much of it as the plan schema uses."""
    if node is False:
        return False
    kinds = node.get("type", ())
    kinds = (kinds,) if isinstance(kinds, str) else kinds
    is_number = isinstance(value, int | float) and not isinstance(value, bool)
    types = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "number": is_number}
    checks = [
        not kinds or any(types[k] for k in kinds),
        "const" not in node or value == node["const"],
        "enum" not in node or value in node["enum"],
        "exclusiveMinimum" not in node or not is_number or value > node["exclusiveMinimum"],
        "oneOf" not in node or sum(valid(n, value) for n in node["oneOf"]) == 1,
    ]
    if isinstance(value, dict):
        properties = node.get("properties", {})
        checks += [all(k in value for k in node.get("required", ())),
                   all(valid(properties[k], v) if k in properties
                       else node.get("additionalProperties", True) is not False
                       for k, v in value.items()),
                   all(all(d in value for d in deps)
                       for k, deps in node.get("dependentRequired", {}).items() if k in value)]
    if isinstance(value, list):
        checks += [node.get("minItems", 0) <= len(value) <= node.get("maxItems", len(value)),
                   all(valid(node.get("items", {}), v) for v in value)]
    return all(checks)


def examples(node) -> list:
    """Instances of one schema node, from the schema alone: each enum, a spread of numbers."""
    if "oneOf" in node:
        return [v for n in node["oneOf"] for v in examples(n)]
    if "const" in node:
        return [node["const"]]
    if "enum" in node:
        return list(node["enum"])
    if node.get("type") == "number":
        return [n for n in NUMBERS if n > node.get("exclusiveMinimum", -1e9)]
    if node.get("type") == "string":
        return ["", "around", "left"]
    properties = node.get("properties", {})
    choices = [[(k, v) for v in examples(n)] + ([] if k in node.get("required", ()) else [ABSENT])
               for k, n in properties.items()]
    return [{k: v for k, v in (c for c in combo if c is not ABSENT)}
            for combo in itertools.product(*choices)]


def plans() -> list[dict]:
    """Every one- and two-step plan of schema-valid steps, and some of four."""
    steps = [s for s in examples(schema()["properties"]["steps"]["items"]) if isinstance(s, dict)]
    fours = [steps[i:i + 4] for i in range(0, len(steps) - 3, 3)]
    return [{"steps": list(p)} for p in [*([s] for s in steps),
                                          *itertools.product(steps, repeat=2), *fours]]


def test_every_schema_valid_plan_parses_and_every_parsed_plan_dumps_back_valid():
    """The one rule a schema cannot say: an open-ended step with no until must come last."""
    cases = [p for p in plans() if valid(schema(), p)]
    assert len(cases) > 500  # the generator covers the schema, not a handful
    for data in cases:
        plan = parse(data)
        if isinstance(plan, str):
            assert "never ends" in plan, (data, plan)
            continue
        assert valid(schema(), dump(plan)), (data, dump(plan))
        assert parse(dump(plan)) == tuple(replace(s, note=None) for s in plan)


def test_a_numeric_turn_is_schema_valid_as_the_kernel_sees_it():
    for arg in (90, -45.5, "around"):
        assert valid(schema(), {"steps": [{"skill": "turn", "arg": arg}]})
    assert not valid(schema(), {"steps": [{"skill": "turn", "arg": "a lot"}]})
    assert not valid(schema(), {"steps": [{"skill": "stop", "arg": "now"}]})


def test_the_manifest_sends_the_generated_schema():
    assert manifest(MOVES, body=True)["tools"][-1] == BODY_PLAN
    assert BODY_PLAN["inputSchema"] == schema()


def test_device_only_skills_stay_out_of_the_kernels_schema():
    steps = examples(schema()["properties"]["steps"]["items"])
    assert {s["skill"] for s in steps} == {"look", "turn", "track_face", "search_face",
                                           "listen_song", "dance", "stop", "stop_tracking"}
