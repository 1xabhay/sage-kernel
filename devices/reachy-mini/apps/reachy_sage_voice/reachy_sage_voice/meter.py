"""What Reachy's log lines look like, for the pet meter: ``make pet LOG=<bot log>``.

The metrics, their targets and the log line contract are in sage_device/pet.py.
"""

import sys

from sage_device import latency, pet

VOICE = r"reachy_sage_voice\."
PATTERNS = pet.compile_patterns({
    "session": r"__main__:session: build (?P<build>\S+) config (?P<config>\S+)",
    "transcript": r"pipecat\.services\.\S+:run_stt:\d+ - Transcription: \[(?P<text>.*)\]$",
    "stt_empty": r"pipecat\.services\.\S+:run_stt:\d+ - Received empty transcription",
    "voice_on": r"pipecat\.\S+:on_speech_started:\d+ - \S+ User started speaking$",
    "voice_off": r"pipecat\.\S+:on_speech_stopped:\d+ - \S+ User stopped speaking$",
    "bot_on": r"pipecat\.\S+:_bot_started_speaking:\d+ - Bot started speaking$",
    "bot_off": r"pipecat\.\S+:_bot_stopped_speaking:\d+ - Bot stopped speaking$",
    "turn": VOICE + r"sage:" + latency.START.pattern,
    "initiated": VOICE + r"sage:initiated$",
    "say": r"pipecat\.services\.\S+:_push_tts_frames:\d+ - \S+ Generating TTS \[(?P<text>.*)\]$",
    "overheard": VOICE + r"sage:overheard: quiet$",
    "unsure": VOICE + r"sage:unsure: (tilted|quiet)$",
    "emote": VOICE + r"(sage:(emote|gesture): |executor:recorded (emotion|dance): )",
    "reply_empty": VOICE + r"sage:reply empty$",
    # The nod at a pause, not the antennas at a voice's start: that one comes before the voice ends.
    "reaction": VOICE + r"executor:(thinking cue fired|glance: |reflex pause: )",
    "sudden_move": VOICE + r"motion\.runner:sudden move: ",
    "motion_gates": (VOICE + r"motion\.runner:motion gates: (?P<ticks>\d+) ticks, "
                     r"(?P<hz>[\d.]+) Hz, .*clamped (?P<clamped>\d+)(?: \([^)]*\))?, "
                     r".*still max (?P<still>[\d.]+) s$"),
    "link_drop": VOICE + r"robot:robot connection lost",
    "kernel_down": VOICE + r"sage:kernel unreachable",
    "voice_silent": (r"(pipecat\.services\.\S+:_record_context_audio_outcome:\d+ - .* no audio"
                     "|" + VOICE + r"tts:Kokoro keeps failing)"),
    "watching": VOICE + r"sage:media: watching ",
    "watched": VOICE + r"sage:media: ended",
    "arrived": VOICE + r"presence:arrived$",
    "left": VOICE + r"presence:left$",
})

if __name__ == "__main__":
    pet.main(sys.argv[1:], PATTERNS)
