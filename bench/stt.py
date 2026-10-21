"""STT: Parakeet TDT 0.6B v2 int8 (onnx-asr, as an earlier Pipecat voice bot runs it) vs faster-whisper.

Clips are SYNTHETIC: 20 pet-style lines (2-5 s) spoken by four Kokoro voices and
piper jenny, clean and with white noise at 15 dB SNR. Synthetic speech is
cleaner than a room mic, so the WER here is a floor, not what the robot hears.
Latency = wall time of one transcribe call on a 16 kHz clip, warm model, CPU.
RTF = latency / clip duration.

    uv run python stt.py
"""
import json
import os
import re
import statistics
import time

import numpy as np

import tts

os.environ.setdefault("HF_HOME", os.path.join(tts.HERE, ".local/hf"))  # keep weights inside bench/

RATE = 16000
CACHE = os.path.join(tts.HERE, ".local/clips.npz")
LINES = (
    "Good morning, how did you sleep last night?",
    "Can you remind me to call my mum at six this evening?",
    "What's the weather going to be like tomorrow morning?",
    "I have had an absolutely awful day at work today.",
    "Turn around and have a look out of the window for me.",
    "That is brilliant news, thank you so much for telling me.",
    "Do you know what a commutator is, and what it does in a motor?",
    "I'm going to bed now, goodnight, see you in the morning.",
    "Play something quiet in the background while I read my book.",
    "How many minutes are left on the timer you set for me?",
    "Please stop doing that, it's really distracting when I'm working.",
    "I missed you today, little robot, it was very quiet here.",
    "What was the thing I asked you about yesterday afternoon?",
    "Set a timer for twenty five minutes and tell me when it's done.",
    "Is it going to rain this afternoon, or should I go for a walk?",
    "Tell me a short story about a fox who lives in a library.",
    "I can't find my keys anywhere, have you seen them on the desk?",
    "Who wrote Pride and Prejudice, and when was it published?",
    "Let's take a break for five minutes and stretch our legs.",
    "Can you see the blue coffee mug sitting next to my keyboard?",
)
VOICES = ("af_heart", "bf_emma", "am_michael", "bm_george", "piper_jenny")


def resample(audio, rate):
    if rate == RATE:
        return audio.astype(np.float32)
    return np.interp(np.arange(0, len(audio), rate / RATE), np.arange(len(audio)), audio).astype(np.float32)


def clips():
    """[(text, voice, audio16k)], synthesised once and cached."""
    if os.path.exists(CACHE):
        z = np.load(CACHE)
        return [(LINES[i], VOICES[j], z[f"{i}_{j}"]) for i in range(len(LINES)) for j in range(len(VOICES))]
    from piper import PiperVoice

    k, p, out = tts.kokoro(), PiperVoice.load(tts.PIPER), {}
    for i, line in enumerate(LINES):
        for j, v in enumerate(VOICES):
            if v == "piper_jenny":
                chunks = list(p.synthesize(line))
                audio, rate = np.concatenate([c.audio_float_array for c in chunks]), chunks[0].sample_rate
            else:
                audio, rate = k.create(line, voice=v)
            out[f"{i}_{j}"] = resample(audio, rate)
    np.savez(CACHE, **out)
    return clips()


def noisy(audio, snr_db=15, seed=0):
    noise = np.random.default_rng(seed).standard_normal(len(audio)).astype(np.float32)
    noise *= np.sqrt(np.mean(audio**2) / 10 ** (snr_db / 10)) / np.sqrt(np.mean(noise**2))
    return audio + noise


def words(text):
    """Lowercase, strip punctuation, and fold spelling variants that are not mishearings."""
    text = text.lower().replace("twenty five", "25").replace("twenty-five", "25").replace("6", "six")
    text = text.replace("good night", "goodnight").replace("mom", "mum").replace("5 ", "five ")
    return re.sub(r"[^a-z0-9' ]", " ", text).split()


def wer_counts(ref, hyp):
    """(edits, ref words) by Levenshtein over words."""
    r, h = words(ref), words(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
    return d[len(h)], len(r)


def parakeet():
    import onnx_asr

    m = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v2", quantization="int8", providers=["CPUExecutionProvider"])
    return lambda a: m.recognize(a, sample_rate=RATE)


def whisper(size):
    from faster_whisper import WhisperModel

    m = WhisperModel(size, device="cpu", compute_type="int8")

    def run(a):  # the pet app's settings
        segs, _ = m.transcribe(a, beam_size=1, language="en", temperature=0.0, condition_on_previous_text=False,
                               without_timestamps=True, vad_filter=True,
                               vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200})
        return " ".join(s.text.strip() for s in segs)

    return run


def pct(xs, p):
    return sorted(xs)[min(len(xs) - 1, round(p / 100 * (len(xs) - 1)))]


def bench(name, run, data):
    run(data[0][2])  # warm
    lat, rtf, edits, n, misses = [], [], 0, 0, []
    for cond, f in (("clean", lambda a: a), ("noise15dB", noisy)):
        e = m = 0
        for text, voice, audio in data:
            a = f(audio)
            t0 = time.perf_counter()
            hyp = run(a)
            dt = time.perf_counter() - t0
            lat.append(dt)
            rtf.append(dt / (len(a) / RATE))
            de, dn = wer_counts(text, hyp)
            e, m = e + de, m + dn
            if de:
                misses.append((cond, voice, text, hyp))
        yield {"model": name, "cond": cond, "wer": round(e / m, 4)}
        edits, n = edits + e, n + m
    yield {"model": name, "n": len(lat), "p50_s": round(statistics.median(lat), 3), "p95_s": round(pct(lat, 95), 3),
           "rtf_p50": round(statistics.median(rtf), 3), "wer_all": round(edits / n, 4), "misses": misses[:12]}


if __name__ == "__main__":
    assert wer_counts("Call my mum at six.", "call my mom at 6") == (0, 5)
    assert wer_counts("a b c", "a x") == (2, 3)
    data = clips()
    durs = [len(a) / RATE for _, _, a in data]
    print(json.dumps({"clips": len(data), "dur_min": round(min(durs), 2), "dur_p50": round(statistics.median(durs), 2),
                      "dur_max": round(max(durs), 2)}))
    for name, make in (("parakeet-tdt-0.6b-v2 int8", parakeet), ("whisper base.en int8", lambda: whisper("base.en")),
                       ("whisper small.en int8", lambda: whisper("small.en"))):
        for row in bench(name, make(), data):
            print(json.dumps(row))
