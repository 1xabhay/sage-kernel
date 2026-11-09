"""The XVF3800 route for the ASR beam: apply, verify, revert. No network, no robot.

The daemon's GET route is a fake ``urlopen`` over a dict that stands for the chip; writes go to
the same dict, the way the chip's own CLI over SSH would change it.
"""

import io
import json

from reachy_sage_voice import chip

ROBOT = "pollen@reachy.test"
BEFORE = {"AUDIO_MGR_OP_L": [8, 0], "AUDIO_MGR_OP_R": [8, 0], "AEC_ASROUTGAIN": [1.0]}
PREFIX = "http://reachy.test:8000/api/audio/config/parameter/"


def fake(values=None, stuck=()):
    """``get`` and ``write`` over one chip; names in ``stuck`` ignore writes."""
    state = {name: list(v) for name, v in (values or BEFORE).items()}
    written = []

    def get(url, timeout):
        assert url.startswith(PREFIX)
        name = url.removeprefix(PREFIX)
        return io.BytesIO(json.dumps({"name": name, "values": state[name]}).encode())

    def write(robot, name, values):
        assert robot == ROBOT
        written.append(name)
        if name not in stuck:
            state[name] = list(values)
    return state, written, get, write


def test_on_routes_the_asr_beam_to_r_keeps_l_sets_the_gain_and_remembers_before(tmp_path):
    state, written, get, write = fake()
    record = tmp_path / "before.json"
    ok, why = chip.on(ROBOT, 0.5, record, get, write)
    assert ok, why
    assert state == {"AUDIO_MGR_OP_L": [8, 0], "AUDIO_MGR_OP_R": [7, 3], "AEC_ASROUTGAIN": [0.5]}
    assert json.loads(record.read_text()) == BEFORE


def test_on_that_does_not_read_back_reverts_and_fails(tmp_path):
    state, _, get, write = fake(stuck={"AUDIO_MGR_OP_R"})
    record = tmp_path / "before.json"
    ok, why = chip.on(ROBOT, 0.5, record, get, write)
    assert not ok and "AUDIO_MGR_OP_R" in why
    assert state == BEFORE
    assert not record.exists()


def test_on_twice_keeps_the_first_before(tmp_path):
    state, _, get, write = fake()
    record = tmp_path / "before.json"
    chip.on(ROBOT, 0.5, record, get, write)
    ok, _ = chip.on(ROBOT, 0.25, record, get, write)
    assert ok and state["AEC_ASROUTGAIN"] == [0.25]
    assert json.loads(record.read_text()) == BEFORE


def test_off_restores_what_was_read_before_and_forgets_it(tmp_path):
    state, _, get, write = fake()
    record = tmp_path / "before.json"
    chip.on(ROBOT, 0.5, record, get, write)
    ok, why = chip.off(ROBOT, record, get, write)
    assert ok, why
    assert state == BEFORE
    assert not record.exists()


def test_off_without_a_record_refuses_and_writes_nothing(tmp_path):
    _, written, get, write = fake()
    ok, why = chip.off(ROBOT, tmp_path / "before.json", get, write)
    assert not ok and "beam-on" in why
    assert written == []


def test_an_unreadable_chip_is_never_written(tmp_path):
    written = []

    def get(url, timeout):
        raise OSError("refused")
    ok, why = chip.on(ROBOT, 0.5, tmp_path / "before.json", get,
                      lambda robot, name, values: written.append(name))
    assert not ok and "refused" in why
    assert written == [] and not (tmp_path / "before.json").exists()


def test_only_the_three_parameters_are_written_and_nothing_is_saved(tmp_path):
    _, written, get, write = fake()
    record = tmp_path / "before.json"
    chip.on(ROBOT, 0.5, record, get, write)
    chip.off(ROBOT, record, get, write)
    assert set(written) == {"AUDIO_MGR_OP_L", "AUDIO_MGR_OP_R", "AEC_ASROUTGAIN"}


def test_check_says_whether_r_carries_the_asr_beam():
    _, _, get, _ = fake({**BEFORE, "AUDIO_MGR_OP_R": [7, 3]})
    assert chip.check(ROBOT, get) == (True, "mic beam: ASR on R (L 8 0, R 7 3, gain 1)")
    _, _, get, _ = fake()
    assert chip.check(ROBOT, get) == (False, "mic beam: not routed (L 8 0, R 8 0, gain 1)")


def test_main_check_exits_by_the_route(tmp_path):
    _, _, get, write = fake()
    out = io.StringIO()
    assert chip.main(["check", "--robot", ROBOT], get, write, out) == 1
    assert "not routed" in out.getvalue()
    assert chip.main(["on", "--robot", ROBOT, "--gain", "0.5", "--record", str(tmp_path / "b.json")],
                     get, write, out) == 0
    assert chip.main(["check", "--robot", ROBOT], get, write, out) == 0


def test_writes_go_through_the_chips_own_cli_on_the_robot():
    assert chip.ssh_command(ROBOT, "AUDIO_MGR_OP_R", [7, 3]) == [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", ROBOT,
        "/venvs/mini_daemon/bin/python", "-m", "reachy_mini.media.audio_control_utils",
        "AUDIO_MGR_OP_R", "--values", "7", "3"]
    assert chip.ssh_command(ROBOT, "AEC_ASROUTGAIN", [0.125])[-2:] == ["--values", "0.125"]
