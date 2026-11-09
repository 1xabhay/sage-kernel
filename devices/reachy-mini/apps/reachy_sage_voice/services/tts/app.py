"""Text to speech: Kyutai's Pocket TTS (100M, PyTorch, CPU) behind OpenAI's speech endpoint.

Pipecat's OpenAITTSService posts JSON to POST /v1/audio/speech with `input`, `model`, `voice`,
`response_format: "pcm"` and optionally `speed` and `instructions`, then reads the body as a
stream of raw 16-bit little-endian mono PCM at 24 kHz. Here the body streams each 80 ms frame as
Pocket decodes it, so the first audio plays while the rest is synthesised.

POST /dev/captioned_speech (Kokoro-FastAPI's path and shape) adds when each word is spoken, so
an interrupted voice knows what was heard. Pocket times no words: each sentence's audio is shared
among its words by their length (words_spoken). Streamed, it also comes frame by frame, so the
words go out with a sentence's first frame, timed over how long the sentence should take.

The voice is azelma (VCTK p303, CC BY 4.0) at temperature 0.6: the owner's choice, 2026-10-23.
"""
import base64
import functools
import json
import os
import re
import threading
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

RATE = 24000
VOICE = "azelma"
TEMPERATURE = 0.6  # Kyutai's evals preferred 0.3; the owner chose 0.6
# How long azelma takes over a sentence, measured: about 0.9 s of pauses plus 21 letters a second.
PAUSE_S, LETTERS_PER_S = 0.9, 21
# Only azelma: Kyutai's card gives its source and licence. cosette (Expresso) and jean (EARS) are
# non-commercial; the rest are unverified. Check tts-voices' card before adding one.
VOICES = (VOICE,)
_model = None


def fetch():
    """Download the weights and the voices into the Hugging Face cache."""
    model = load()
    for v in VOICES:
        model.get_state_for_audio_prompt(v)


def load():
    """Load Pocket's English model at the owner's temperature."""
    from pocket_tts import TTSModel

    # int8 halves the time in the Colima image (real-time factor 0.68 to 0.47); natively, no gain.
    return TTSModel.load_model(temp=TEMPERATURE, quantize=True)


@functools.cache
def state(voice):
    """Return the voice's prompt state, read once from the baked cache; each generation copies it."""
    return _model.get_state_for_audio_prompt(voice)


@asynccontextmanager
async def lifespan(_app):
    global _model
    _model = load()
    _model.generate_audio(state(VOICE), "Ready.")  # the first call pays for setup, not a user
    yield


app = FastAPI(lifespan=lifespan)


class Speech(BaseModel):  # OpenAI's fields
    input: str
    voice: str = VOICE
    model: str = "pocket"  # one model; accepted and ignored
    response_format: str = "pcm"
    # ponytail: Pocket has no speed control, so the voice styles' 0.9-1.1 are ignored; time-stretch
    # the audio (pitch-preserving) if the owner misses them.
    speed: float = 1.0
    instructions: str | None = None  # OpenAI's acting notes; Pocket has none, ignored


class CaptionedSpeech(Speech):
    stream: bool = True  # one JSON line per sentence; false: one object for the whole reply


def sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


@app.get("/health")
def health():
    return {"ready": _model is not None}


@app.get("/v1/audio/voices")
def voices():
    return {"voices": list(VOICES)}


def validated(req: Speech) -> list[str]:
    if req.response_format != "pcm":
        raise HTTPException(400, "only response_format 'pcm' (16-bit mono 24 kHz) is supported")
    if req.voice not in VOICES:
        raise HTTPException(400, f"unknown voice {req.voice!r}; GET /v1/audio/voices lists them")
    if not 0.5 <= req.speed <= 2.0:
        raise HTTPException(400, "speed must be between 0.5 and 2.0")
    parts = sentences(req.input)
    if not parts:
        raise HTTPException(400, "input is empty")
    return parts


def pcm16(audio):
    return (np.clip(audio.numpy(), -1, 1) * 32767).astype("<i2").tobytes()


def frames(text, voice):
    """Pocket's audio frames for the text; a closed stream (the listener cut in) stops generating."""
    stop = threading.Event()
    try:
        yield from _model.generate_audio_stream(state(voice), text, stop=stop)
    finally:
        stop.set()


def words_spoken(sentence, seconds, offset):
    """[{word, start_time, end_time}] for each word of the sentence as written.

    ponytail: Pocket times no words, so each word gets a share of the sentence's audio by its
    length, trailing pause included: a long word or a number ("25") said faster or slower than its
    letters is misplaced by up to a word or two. Forced alignment (or a timed fork of Pocket) if
    interruptions show the wrong words heard."""
    words = sentence.split()
    ends = np.cumsum([len(w) for w in words]) / sum(len(w) for w in words) * seconds
    starts = np.concatenate([[0.0], ends[:-1]])
    return [{"word": w, "start_time": round(offset + s, 3), "end_time": round(offset + e, 3)}
            for w, s, e in zip(words, starts, ends, strict=True)]


@app.post("/v1/audio/speech")
def speech(req: Speech):
    validated(req)
    return StreamingResponse((pcm16(f) for f in frames(req.input, req.voice)), media_type="audio/pcm")


@app.post("/dev/captioned_speech")
def captioned_speech(req: CaptionedSpeech):
    parts = validated(req)

    def whole(part, offset):  # the sentence, then its words over its real length
        audio = b"".join(pcm16(f) for f in frames(part, req.voice))
        yield audio, words_spoken(part, len(audio) / 2 / RATE, offset)

    def streamed(part, offset):
        # ponytail: the sentence's length is a guess when its words go out, off by up to a quarter
        # (numbers and names run long); each next sentence starts from the real audio, so it never
        # adds up. Send the words with the last frame instead if exact beats the 0.5-0.8 s it costs.
        guess = PAUSE_S + len("".join(part.split())) / LETTERS_PER_S
        for i, f in enumerate(frames(part, req.voice)):
            yield pcm16(f), [] if i else words_spoken(part, guess, offset)

    def chunks(each):
        offset = 0.0
        for part in parts:
            for audio, stamps in each(part, offset):
                yield audio, stamps
                offset += len(audio) / 2 / RATE

    def line(audio, stamps):
        return json.dumps({"audio": base64.b64encode(audio).decode(), "audio_format": "audio/pcm",
                           "timestamps": stamps}) + "\n"

    if req.stream:
        return StreamingResponse((line(a, t) for a, t in chunks(streamed)), media_type="application/x-ndjson")
    done = list(chunks(whole))
    return Response(line(b"".join(a for a, _ in done), [w for _, t in done for w in t]), media_type="application/json")


if __name__ == "__main__":
    import sys

    import uvicorn

    if sys.argv[1:] == ["fetch"]:
        fetch()
    else:
        uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
