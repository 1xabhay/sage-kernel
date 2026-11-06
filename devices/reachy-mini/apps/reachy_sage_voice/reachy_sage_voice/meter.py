"""What Reachy's log lines look like, for the pet meter: ``make pet LOG=<bot log>``.

The metrics, their targets and the log line contract are in sage_device/pet.py. Reachy declares no
``sudden_move``: nothing logs one yet, so that row reads "not logged".
"""

import sys

from sage_device import latency, pet

VOICE = r"reachy_sage_voice\."
PATTERNS = pet.compile_patterns({
    "session": r"__main__:session: build (?P<build>\S+) config (?P<config>\S+)",
    "transcript": r"pipecat\.services\.\S+:run_stt:\d+ - Transcription: \[(?P<text>.*)\]$",
    "stt_empty": r"pipecat\.services\.\S+:run_stt:\d+ - Received empty transcription",
    "voice_on": r"pipecat\.\S+:on_speech_started:\d+ - \S+ User started speaking$",
    "bot_on": r"pipecat\.\S+:_bot_started_speaking:\d+ - Bot started speaking$",
    "bot_off": r"pipecat\.\S+:_bot_stopped_speaking:\d+ - Bot stopped speaking$",
    "turn": VOICE + r"sage:" + latency.START.pattern,
    "initiated": VOICE + r"sage:initiated$",
    "say": r"pipecat\.services\.\S+:_push_tts_frames:\d+ - \S+ Generating TTS \[(?P<text>.*)\]$",
    "emote": VOICE + r"(sage:(emote|gesture): |executor:recorded (emotion|dance): )",
    "reply_empty": VOICE + r"sage:reply empty$",
    "reaction": VOICE + r"executor:(thinking cue fired|glance: )",
    "link_drop": VOICE + r"robot:robot connection lost",
    "kernel_down": VOICE + r"sage:kernel unreachable",
    "voice_silent": (r"(pipecat\.services\.\S+:_record_context_audio_outcome:\d+ - .* no audio"
                     "|" + VOICE + r"tts:Kokoro keeps failing)"),
    "arrived": VOICE + r"presence:arrived$",
    "left": VOICE + r"presence:left$",
})

if __name__ == "__main__":
    pet.main(sys.argv[1:], PATTERNS)
