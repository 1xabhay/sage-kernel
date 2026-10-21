"""The voice's STT and TTS servers over HTTP: in Docker (Colima) vs native, same code.

Servers: the robot-side voice services (stt/app.py, tts/app.py), kept with the Reachy device.
STT: the 100 clean synthetic clips from stt.py, posted as 16 kHz WAV the way Pipecat does.
TTS: tts.py's 8 replies x 3, response_format pcm; TTFB = first body byte (one sentence streamed).

    uv run python services.py docker http://127.0.0.1:8781 http://127.0.0.1:8782
    uv run python services.py captioned native http://127.0.0.1:8792
"""
import io
import json
import statistics
import sys
import time
import urllib.request
import uuid

import soundfile as sf

import stt
import tts


def post_wav(url, audio):
    buf = io.BytesIO()
    sf.write(buf, audio, stt.RATE, format="WAV", subtype="PCM_16")
    b = uuid.uuid4().hex
    body = (f'--{b}\r\nContent-Disposition: form-data; name="model"\r\n\r\nparakeet-tdt-0.6b-v2\r\n'
            f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
            f"Content-Type: audio/wav\r\n\r\n").encode() + buf.getvalue() + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(url + "/v1/audio/transcriptions", body,
                                 {"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)["text"]


def speak(url, text):
    """(ttfb, total, audio seconds)."""
    req = urllib.request.Request(url + "/v1/audio/speech", json.dumps(
        {"model": "kokoro", "voice": "af_heart", "response_format": "pcm", "input": text}).encode(),
        {"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req) as r:
        first = r.read1(65536)
        ttfb = time.perf_counter() - t0
        n = len(first) + len(r.read())
    return ttfb, time.perf_counter() - t0, n / 2 / 24000


def first_caption(url, text):
    """Time to the first streamed captioned line (first sentence's audio + its word times)."""
    req = urllib.request.Request(url + "/dev/captioned_speech", json.dumps(
        {"voice": "af_heart", "response_format": "pcm", "input": text, "stream": True}).encode(),
        {"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req) as r:
        line = r.readline()
        dt = time.perf_counter() - t0
        r.read()
    assert json.loads(line)["timestamps"]
    return dt


def captioned(label, tts_url):
    """Plain vs captioned time to first audio, interleaved so load noise hits both alike."""
    first_caption(tts_url, "Warm up.")
    plain, capt = [], []
    for _ in range(5):
        for t in tts.REPLIES:
            plain.append(speak(tts_url, t)[0])
            capt.append(first_caption(tts_url, t))
    diff = [c - p for p, c in zip(plain, capt)]
    print(json.dumps({"where": label, "stage": "captioned", "n": len(diff),
                      "plain_ttfb_p50": round(statistics.median(plain), 3),
                      "captioned_first_line_p50": round(statistics.median(capt), 3),
                      "paired_diff_p50_ms": round(statistics.median(diff) * 1000, 1),
                      "paired_diff_p95_ms": round(stt.pct(diff, 95) * 1000, 1)}))


def main(label, stt_url, tts_url):
    clips = [c for c in stt.clips()]
    post_wav(stt_url, clips[0][2])
    lat, edits, words = [], 0, 0
    for text, _, audio in clips:
        t0 = time.perf_counter()
        hyp = post_wav(stt_url, audio)
        lat.append(time.perf_counter() - t0)
        e, n = stt.wer_counts(text, hyp)
        edits, words = edits + e, words + n
    dur = statistics.median(len(a) / stt.RATE for _, _, a in clips)
    print(json.dumps({"where": label, "stage": "stt", "n": len(lat), "p50": round(statistics.median(lat), 3),
                      "p95": round(stt.pct(lat, 95), 3), "rtf_p50": round(statistics.median(lat) / dur, 3),
                      "wer": round(edits / words, 4)}))
    speak(tts_url, "Warm up.")
    runs = [speak(tts_url, t) for t in tts.REPLIES for _ in range(3)]
    print(json.dumps({"where": label, "stage": "tts", "n": len(runs),
                      "ttfb_p50": round(statistics.median(r[0] for r in runs), 3),
                      "ttfb_p95": round(stt.pct([r[0] for r in runs], 95), 3),
                      "total_p50": round(statistics.median(r[1] for r in runs), 3),
                      "rtf_p50": round(statistics.median(r[1] / r[2] for r in runs), 3)}))


if __name__ == "__main__":
    if sys.argv[1] == "captioned":
        captioned(sys.argv[2], sys.argv[3])
    else:
        main(*sys.argv[1:4])
