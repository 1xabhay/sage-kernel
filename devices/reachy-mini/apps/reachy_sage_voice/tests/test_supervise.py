"""Supervision (LAYERS.md §1a row 8): reconnect pacing and the mic beam after a reboot."""

import logging
import urllib.error

from test_chip import BEFORE, ROBOT, fake

from reachy_sage_voice import supervise


def test_reconnects_back_off_doubling_to_a_cap():
    assert [supervise.backoff(n) for n in range(6)] == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]


def test_a_beam_a_reboot_reset_is_routed_again(tmp_path):
    # The chip saves nothing: a robot reboot puts the ASR beam back off R (R2, owner O5).
    state, written, get, write = fake()
    supervise.beam(ROBOT, get, write, tmp_path / "before.json")
    assert state["AUDIO_MGR_OP_R"] == [7, 3] and written


def test_a_routed_beam_is_left_alone(tmp_path):
    _, written, get, write = fake({**BEFORE, "AUDIO_MGR_OP_R": [7, 3]})
    supervise.beam(ROBOT, get, write, tmp_path / "before.json")
    assert written == []


def test_a_chip_that_cannot_be_read_is_logged_not_raised(tmp_path, caplog):
    def get(url, timeout):
        raise urllib.error.URLError("no route to host")

    def write(robot, name, values):
        raise AssertionError("nothing is written blind")

    with caplog.at_level(logging.WARNING):
        supervise.beam(ROBOT, get, write, tmp_path / "before.json")
    assert "no route to host" in caplog.text
