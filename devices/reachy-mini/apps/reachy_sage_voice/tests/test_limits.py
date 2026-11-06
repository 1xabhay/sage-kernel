"""The joint limits: one dated table, and every target clamped to it (eval A5)."""

from reachy_sage_voice.limits import LIMITS, MEASURED, clamp


def test_each_limit_is_the_sdk_or_the_measured_reach_less_two_degrees():
    assert MEASURED == "2026-10-23"
    assert LIMITS == {"body_yaw": (-158.0, 158.0), "yaw": (-155.0, 171.0),
                      "head_yaw": (-53.0, 53.0), "pitch": (-40.0, 40.0), "roll": (-40.0, 40.0),
                      "z": (-0.010, 0.010)}


def test_the_heads_height_is_clamped_too_so_a_high_idle_scale_cannot_swing_it_far():
    assert clamp({"z": 0.05}) == {"z": 0.010}
    assert clamp({"z": -0.05}) == {"z": -0.010}


def test_a_pose_beyond_the_limits_is_clamped_to_them():
    assert clamp({"body_yaw": 200.0, "yaw": 200.0, "pitch": -50.0, "roll": 50.0}) == {
        "body_yaw": 158.0, "yaw": 171.0, "pitch": -40.0, "roll": 40.0}
    assert clamp({"body_yaw": -200.0, "yaw": -200.0}) == {"body_yaw": -158.0, "yaw": -155.0}


def test_the_head_stays_within_53_degrees_of_the_body():
    assert clamp({"body_yaw": 0.0, "yaw": 90.0}) == {"body_yaw": 0.0, "yaw": 53.0}
    assert clamp({"body_yaw": 100.0, "yaw": 0.0}) == {"body_yaw": 100.0, "yaw": 47.0}
    assert clamp({"yaw": 90.0}) == {"yaw": 90.0}  # the body unknown: only the reach


def test_a_pose_within_the_limits_is_unchanged():
    pose = {"body_yaw": 150.0, "yaw": 170.0, "pitch": 10.0, "roll": -5.0, "z": 0.01}
    assert clamp(pose) == pose
