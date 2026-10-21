"""TTS: Kokoro 82M ONNX vs piper jenny_dioco, on short pet replies.

Time to first audio = text in -> first audio chunk out (Kokoro: first chunk of
create_stream; piper: first sentence chunk of synthesize). RTF = synthesis time
/ audio duration (lower is faster). CPU only, one thread pool, warm model.

    uv run python tts.py
"""
import asyncio
import glob
import json
import os
import re
import statistics
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
KOKORO = os.path.join(HERE, ".local/kokoro")
PIPER = os.path.expanduser("~/.reachy_pet/voices/en_GB-jenny_dioco-medium.onnx")  # read-only

REPLIES = (  # 1-2 sentence pet replies, the shape the live turn produces
    "Good morning! I was just watching the light move across your desk.",
    "Oh no, that sounds like a rotten day. Do you want to tell me about it?",
    "Four. Even I can count that high.",
    "Sleep well. I'll keep an eye on things here.",
    "Sorry, I didn't quite catch that. Could you say it again?",
    "That's brilliant news! I knew you could do it.",
    "I missed you too. It's been very quiet without you.",
    "Okay, I'll stop.",
)


def kokoro(model="model_quantized.onnx"):
    """Kokoro with the onnx-community voices packed into the npz kokoro-onnx expects."""
    from kokoro_onnx import Kokoro

    npz = os.path.join(KOKORO, "voices.npz")
    if not os.path.exists(npz):
        voices = {os.path.basename(p)[:-4]: np.fromfile(p, dtype=np.float32).reshape(-1, 1, 256)
                  for p in glob.glob(os.path.join(KOKORO, "voices/*.bin"))}
        np.savez(npz, **voices)
    return Kokoro(os.path.join(KOKORO, "onnx", model), npz)


def sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s]


def bench_kokoro(model, voice="af_heart", per_sentence=False):
    """per_sentence: synthesise each sentence on its own, as a first-clause pipeline would."""
    k = kokoro(model)
    k.create("Warm up.", voice=voice)

    async def one(text):
        t0 = time.perf_counter()
        first, n, sr = None, 0, 24000
        for part in sentences(text) if per_sentence else [text]:
            async for audio, sr in k.create_stream(part, voice=voice):
                first = first or time.perf_counter() - t0
                n += len(audio)
        return first, time.perf_counter() - t0, n / sr

    return [asyncio.run(one(t)) for t in REPLIES for _ in range(3)]


def bench_piper():
    from piper import PiperVoice

    v = PiperVoice.load(PIPER)
    list(v.synthesize("Warm up."))

    def one(text):
        t0 = time.perf_counter()
        first, n, sr = None, 0, 22050
        for chunk in v.synthesize(text):
            first = first or time.perf_counter() - t0
            n += len(chunk.audio_float_array)
            sr = chunk.sample_rate
        return first, time.perf_counter() - t0, n / sr

    return [one(t) for t in REPLIES for _ in range(3)]


def pct(xs, p):
    return sorted(xs)[min(len(xs) - 1, round(p / 100 * (len(xs) - 1)))]


def summary(name, runs):
    ttfa = [r[0] for r in runs]
    rtf = [r[1] / r[2] for r in runs]
    return {"engine": name, "n": len(runs), "ttfa_p50": statistics.median(ttfa), "ttfa_p95": pct(ttfa, 95),
            "rtf_p50": statistics.median(rtf), "rtf_p95": pct(rtf, 95),
            "audio_s_p50": statistics.median(r[2] for r in runs)}


if __name__ == "__main__":
    rows = [summary("kokoro int8 (model_quantized)", bench_kokoro("model_quantized.onnx")),
            summary("kokoro int8, per sentence", bench_kokoro("model_quantized.onnx", per_sentence=True)),
            summary("kokoro fp32 (model)", bench_kokoro("model.onnx")),
            summary("piper jenny_dioco medium", bench_piper())]
    for r in rows:
        print(json.dumps({k: round(v, 3) if isinstance(v, float) else v for k, v in r.items()}))
