"""Per-layer health (LAYERS.md §2.4): a pure record, one call to report, a heartbeat file."""

import os

from reachy_sage_voice import health, status
from reachy_sage_voice.health import Health, update


def test_a_layer_keeps_its_since_while_its_state_holds():
    table = update({}, "voice.tts", False, "down", now=10.0)
    assert table == {"voice.tts": Health("voice.tts", False, 10.0, "down")}
    table = update(table, "voice.tts", False, "still down", now=20.0)
    assert table["voice.tts"].since == 10.0 and table["voice.tts"].detail == "still down"
    assert update(table, "voice.tts", True, "up", now=30.0)["voice.tts"].since == 30.0


def test_the_heartbeat_round_trips_to_status(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "TABLE", {})
    health.report("voice.robot", True, "connected")
    health.report("voice.stt", False, "down")
    path = tmp_path / "beat.json"
    health.write(path)
    beat = status.read_heartbeat(path)
    assert beat["pid"] == os.getpid()
    ok, lines = status.render(beat, beat["at"], running=True)
    assert not ok
    assert [line.split()[:2] for line in lines] == [["voice.robot", "up"], ["voice.stt", "down"]]
