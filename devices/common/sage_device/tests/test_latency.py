"""v2v_ms from the bot's log (LAYERS.md §0), scored per turn and lane from synthetic lines.

The stamps, on a real handler and the processor's own turns, are checked in the Reachy app's
tests/test_latency.py.
"""

from sage_device import latency
from sage_device.latency import LEAD_MS, Turn

SAGE = "INFO:reachy_sage_voice.sage:"
SPOKE = SAGE + "bot started speaking"


def start(lane, ago):
    return f"{SAGE}turn start ({lane} lane): voice ended {ago} ms ago"


def line(mono, turn, message):
    return f"2026-10-25 10:00:00,000 {mono:.3f} {turn} {message}"


def test_a_silent_turn_stays_unmeasured_and_later_audio_is_not_its_own():
    """A greeting's or an aside's voice, after a turn that ended with a move, would read as a minutes-long v2v_ms."""
    found = latency.turns([line(100, "reachy-a", start("kernel", 300)),
                           line(101, "reachy-a", SAGE + latency.SILENT),
                           line(400, "reachy-a", SPOKE)])
    assert found == [Turn("reachy-a", "kernel", None)]


LOG = [
    "uv run python -m reachy_sage_voice",
    line(100, "-", "INFO:reachy_sage_voice.executor:idle action"),
    line(101, "reachy-a", start("kernel", 300)),
    line(102.2, "reachy-a", SPOKE),
    line(102.9, "reachy-a", SPOKE),
    line(104, "reachy-b", start("reflex", 200)),
    line(104.4, "reachy-b", SPOKE),
    line(106, "reachy-c", start("kernel", 100)),
    line(107, "reachy-d", start("kernel", "-")),
    line(108, "reachy-d", SPOKE),
    line(110, "reachy-e", start("kernel", 0)),
    "2026-10-25 10:00:10.500 | DEBUG    | pipecat.transports.base_output - Bot started speaking",
    line(111, "reachy-e", SPOKE),
]


def test_each_turn_runs_from_its_last_voiced_frame_to_its_first_audio_plus_the_lead():
    assert latency.turns(LOG) == [
        Turn("reachy-a", "kernel", 1500 + LEAD_MS),
        Turn("reachy-b", "reflex", 600 + LEAD_MS),
        Turn("reachy-c", "kernel", None),  # replaced before it said anything
        Turn("reachy-d", "kernel", None),  # no end of speech to time from
        Turn("reachy-e", "kernel", 1000 + LEAD_MS)]


def test_the_summary_is_p50_and_p95_per_lane():
    assert latency.summary(latency.turns(LOG)) == {
        "kernel": {"n": 2, "unmeasured": 2, "p50": 1000 + LEAD_MS, "p95": 1500 + LEAD_MS},
        "reflex": {"n": 1, "unmeasured": 0, "p50": 600 + LEAD_MS, "p95": 600 + LEAD_MS}}


def test_the_cli_prints_each_lane(tmp_path, capsys):
    path = tmp_path / "bot.log"
    path.write_text("\n".join(LOG))
    latency.main([str(path)])
    out = capsys.readouterr().out
    assert "kernel" in out and "reflex" in out and str(1500 + LEAD_MS) in out
