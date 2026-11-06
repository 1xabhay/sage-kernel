"""``make pet`` on the lines Reachy really writes; the meter's own tests are in sage_device."""
# ruff: noqa: E501

import asyncio
import logging
import subprocess
import sys

from pipecat.frames.frames import BotStartedSpeakingFrame, VADUserStoppedSpeakingFrame
from pipecat.tests.utils import SleepFrame
from sage_device import latency, pet
from test_commands import choosing
from test_latency import stamped  # noqa: F401
from test_sage_processor import FakeSession, delta, emote, heard, make, reply, run

from reachy_sage_voice import meter
from reachy_sage_voice.__main__ import session_line
from reachy_sage_voice.perception.face import Face
from reachy_sage_voice.presence import watch_presence


def table(lines, name=None):
    return {key: row.value for key, row in pet.report(lines, meter.PATTERNS, name).items()}


def said(stream):
    """Each line as the meter classifies it: logger and message."""
    return [f"{m['name']}:{m['message']}" for m in map(latency.LINE.match, stream.getvalue().splitlines())]


def test_pipecats_own_lines_and_reachys_are_both_read():
    lines = [
        "2026-11-05 15:40:00.789 | DEBUG    | pipecat.processors.audio.vad_processor:on_speech_started:73 - VADProcessor#0: User started speaking",
        "2026-11-05 15:40:01.753 | DEBUG    | pipecat.services.whisper.base_stt:run_stt:287 - Transcription: [Yeah, kernel unreachable: robot connection lost.]",
        "2026-11-05 15:40:01,800 900.000 reachy-a INFO:reachy_sage_voice.sage:turn start (kernel lane): voice ended 1000 ms ago",
        "2026-11-05 15:40:02.708 | DEBUG    | pipecat.services.tts_service:_push_tts_frames:1419 - KokoroTTSService#0: Generating TTS [Yep!]",
        "2026-11-05 15:40:03.062 | DEBUG    | pipecat.transports.base_output:_bot_started_speaking:712 - Bot started speaking",
        "2026-11-05 15:40:03,100 901.300 reachy-a INFO:reachy_sage_voice.sage:bot started speaking",
        "2026-11-05 15:40:04.975 | DEBUG    | pipecat.transports.base_output:_handle_frame:883 - Bot stopped speaking based on TTSStoppedFrame",
        "2026-11-05 15:40:05.062 | DEBUG    | pipecat.transports.base_output:_bot_stopped_speaking:817 - Bot stopped speaking",
        "2026-11-05 15:40:06,000 904.000 - WARNING:reachy_sage_voice.robot:robot connection lost - reconnecting",
        "2026-11-05 15:40:07,000 905.000 - WARNING:reachy_sage_voice.sage:kernel unreachable between turns: GET x",
        "2026-11-05 15:40:08.000 | WARNING  | pipecat.services.tts_service:_record_context_audio_outcome:1934 - KokoroTTSService#0 audio context x completed with no audio",
        "2026-11-05 15:40:08,500 906.000 - ERROR:reachy_sage_voice.tts:Kokoro keeps failing: Sage is not speaking; every reply still tries it",
        "2026-11-05 15:40:09,000 907.000 reachy-a INFO:reachy_sage_voice.executor:thinking cue fired",
    ]
    got = table(lines)
    assert got["replied"] == "100% (1/1)"
    assert got["longest_s"] == "2 s"  # from Bot started to Bot stopped, not the "based on" line
    assert got["link_drops"] == "1"  # not the owner saying so, in the transcript
    assert got["kernel_unreachable"] == "1"
    assert got["voice_silent"] == "2"
    assert got["speech_p50"] == f"{(2300 + latency.LEAD_MS) / 1000:g} s (1)"


def test_a_turn_that_only_moves_logs_its_emote_and_empty_reply_and_no_speech(caplog, stamped):  # noqa: F811
    caplog.set_level(logging.INFO, logger="reachy_sage_voice.sage")
    _, stream = stamped("reachy_sage_voice.sage")
    session = FakeSession([{"effects": [emote(1, "cheerful1"), reply(2, "")]}])
    run(make(session), [VADUserStoppedSpeakingFrame(stop_secs=0.2), SleepFrame(0.05),
                        heard("I had such a long day today."), SleepFrame(0.2)])
    lines = stream.getvalue().splitlines()
    got = table(lines)
    assert got["movement_only"] == "100% (1/1)"
    assert got["reaction"].endswith("(1/1)")
    assert not any("long day" in line for line in lines)


def test_a_chosen_move_is_a_movement_only_turn_and_its_words_stay_out_of_the_log(caplog, stamped):  # noqa: F811
    caplog.set_level(logging.INFO, logger="reachy_sage_voice.sage")
    _, stream = stamped("reachy_sage_voice.sage")
    emotes = []
    session, processor = choosing(emotes)
    run(processor, [BotStartedSpeakingFrame(), SleepFrame(0.01),
                    VADUserStoppedSpeakingFrame(stop_secs=0.2), SleepFrame(0.01),
                    heard("I had a big lunch."), SleepFrame(0.05)])
    lines = stream.getvalue().splitlines()
    got = table(lines)
    assert len(emotes) == 1 and session.started == []
    assert got["movement_only"] == "100% (1/1)"
    assert got["reaction"].endswith("(1/1)")
    assert any("chose: move" in line for line in lines)
    assert not any("big lunch" in line for line in lines)


def test_a_greeting_answered_by_the_body_is_movement_only_too(caplog, stamped):  # noqa: F811
    caplog.set_level(logging.INFO, logger="reachy_sage_voice.sage")
    _, stream = stamped("reachy_sage_voice.sage")
    run(choosing([])[1], [heard("Hi Sage."), SleepFrame(0.05)])
    assert table(stream.getvalue().splitlines())["movement_only"] == "100% (1/1)"


def test_a_reply_with_words_is_not_a_chosen_silence(caplog, stamped):  # noqa: F811
    caplog.set_level(logging.INFO, logger="reachy_sage_voice.sage")
    _, stream = stamped("reachy_sage_voice.sage")
    session = FakeSession([{"effects": [delta(0, "Hi."), reply(1, "Hi.")]}])
    run(make(session), [heard("Good evening to you, my friend."), SleepFrame(0.05),
                        BotStartedSpeakingFrame(), SleepFrame(0.1)])
    assert not any(meter.PATTERNS["reply_empty"].match(line) for line in said(stream))


def test_a_turn_the_kernel_starts_is_logged_as_an_initiation(caplog, stamped):  # noqa: F811
    caplog.set_level(logging.INFO, logger="reachy_sage_voice.sage")
    _, stream = stamped("reachy_sage_voice.sage")
    session = FakeSession([{"effects": [{"sequence": 7, "kind": "reply", "turn_id": "kernel-1",
                                         "text": "Hello again!", "stale": False}]}], idle=True)
    run(make(session), [SleepFrame(0.1)])
    assert any(meter.PATTERNS["initiated"].match(line) for line in said(stream))


def test_presence_edges_are_logged_as_arrived_and_left(caplog):
    caplog.set_level(logging.INFO, logger="reachy_sage_voice.presence")
    readings = iter([Face(True, True, 0.0, 0.0), Face(True, True, 0.0, 0.2), Face(False, True, None, 100.0)])
    clock = iter([0.0, 0.2, 100.0])
    ended = iter([False, False, False, True])

    async def sink(_event):
        pass

    async def still(_s):
        pass

    asyncio.run(watch_presence(lambda: next(readings), sink, lambda: next(clock), still,
                               until=lambda: next(ended)))
    lines = [f"{r.name}:{r.getMessage()}" for r in caplog.records]
    assert sum(bool(meter.PATTERNS["arrived"].match(line)) for line in lines) == 1
    assert sum(bool(meter.PATTERNS["left"].match(line)) for line in lines) == 1


def test_the_session_line_names_the_build_and_hashes_the_settings_without_secrets():
    env = {"SAGE_VOICE_BUILD": "abc1234", "SAGE_VOICE_ARBITER": "1", "SAGE_VOICE_KERNEL_TOKEN": "tok", "HOME": "/x"}
    line = session_line(env)
    match = meter.PATTERNS["session"].match("__main__:" + line)
    assert match["build"] == "abc1234" and len(match["config"]) == 12
    assert "tok" not in line
    assert session_line({**env, "SAGE_VOICE_KERNEL_TOKEN": "other", "HOME": "/y"}) == line
    assert session_line({**env, "SAGE_VOICE_BUILD": "def5678"}).split("config")[1] == line.split("config")[1]
    assert session_line({**env, "SAGE_VOICE_ARBITER": "0"}) != line


def test_the_meter_runs_with_the_voice_unimported(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text("nothing yet\n")
    code = ("import sys, runpy; sys.argv = ['meter', sys.argv[1]]; "
            "runpy.run_module('reachy_sage_voice.meter', run_name='__main__'); "
            "assert 'pipecat' not in sys.modules")
    out = subprocess.run([sys.executable, "-c", code, str(log)], capture_output=True, text=True, check=True)
    assert "not logged" in out.stdout
