"""Plans: a few flat body steps, checked before anything moves."""

import pytest

from reachy_sage_voice.plan import Step, parse
from reachy_sage_voice.skills import BODY_PLAN, SKILLS


def steps(*items):
    return {"steps": list(items)}


def test_a_chain_parses_into_steps():
    assert parse(steps({"skill": "look", "arg": "left"}, {"skill": "look", "arg": "right"})) == (
        Step("look", "left"), Step("look", "right"))


def test_search_then_track_leaves_the_search_its_whole_path():
    assert parse(steps({"skill": "search_face"}, {"skill": "track_face"})) == (
        Step("search_face", until="face_found"), Step("track_face"))


@pytest.mark.parametrize("timeout, expected, note", [
    (None, 180.0, None), (60, 60.0, None), (900, 600.0, "out_of_range")])
def test_a_dance_lasts_three_minutes_unless_told_up_to_ten(timeout, expected, note):
    item = {"skill": "dance"} | ({"timeout_s": timeout} if timeout else {})
    (step,) = parse(steps(item))
    assert (step.skill, step.until, step.timeout_s, step.note) == ("dance", None, expected, note)


def test_stop_tracking_is_a_step_that_others_can_follow():
    assert parse(steps({"skill": "stop_tracking"}, {"skill": "turn", "arg": 90})) == (
        Step("stop_tracking"), Step("turn", 90))


@pytest.mark.parametrize("arg, degrees, note", [
    ("around", 158, None), (90, 90, None), ("-45", -45, None), (180, 158, "out_of_range"),
    (-200, -158, "out_of_range"),
])
def test_turn_degrees_clamp_to_the_body_with_a_note(arg, degrees, note):
    (step,) = parse(steps({"skill": "turn", "arg": arg}))
    assert (step.arg, step.note) == (degrees, note)


def test_a_search_longer_than_30_s_is_cut_to_30_with_a_note():
    (step,) = parse(steps({"skill": "search_face", "timeout_s": 60}))
    assert (step.timeout_s, step.note) == (30.0, "out_of_range")


def test_tracking_for_a_while_can_lead_more_steps():
    plan = parse(steps({"skill": "track_face", "until": "timeout", "timeout_s": 5},
                       {"skill": "look", "arg": "center"}))
    assert plan[0] == Step("track_face", until="timeout", timeout_s=5.0)


@pytest.mark.parametrize("data", [
    {"steps": []},
    {"steps": [{"skill": "look", "arg": "left"}] * 5},
    {"steps": [{"skill": "juggle"}]},
    {"steps": [{"skill": "dance"}, {"skill": "look", "arg": "left"}]},  # never reached
    {"steps": [{"skill": "dance", "until": "timeout", "timeout_s": 5}]},
    {"steps": [{"skill": "stop_tracking", "timeout_s": 5}]},
    {"steps": [{"skill": "look", "arg": "sideways"}]},
    {"steps": [{"skill": "look"}]},
    {"steps": [{"skill": "turn", "arg": "a lot"}]},
    {"steps": [{"skill": "turn", "arg": True}]},
    {"steps": [{"skill": "stop", "arg": "now"}]},
    {"steps": [{"skill": "look", "arg": "left", "until": "face_found"}]},
    {"steps": [{"skill": "search_face", "until": "forever"}]},
    {"steps": [{"skill": "search_face", "timeout_s": 0}]},
    {"steps": [{"skill": "track_face", "until": "timeout"}]},  # no timeout_s
    {"steps": [{"skill": "track_face"}, {"skill": "look", "arg": "left"}]},  # never reached
    {"steps": [{"skill": "look", "arg": "left", "speed": 3}]},
    {"steps": "look left"},
    {},
    "look left",
])
def test_a_bad_plan_is_refused_with_a_reason(data):
    reason = parse(data)
    assert isinstance(reason, str) and reason


def test_the_kernel_tool_offers_the_kernels_skills_in_at_most_four_steps():
    steps = BODY_PLAN["inputSchema"]["properties"]["steps"]
    assert BODY_PLAN["name"] == "body_plan"
    assert (steps["minItems"], steps["maxItems"]) == (1, 4)
    assert [s for s, skill in SKILLS.items() if not skill.device] == [
        "look", "turn", "track_face", "search_face", "listen_song", "dance", "stop",
        "stop_tracking"]
