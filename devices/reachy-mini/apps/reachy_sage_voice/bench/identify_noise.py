"""Song naming under robot-mic-like damage: condition x snippet length -> found rate and latency.

Fetches five 30 s iTunes previews (Search API ``previewUrl``) into bench/.audio (git-ignored,
never committed), decodes them with ffmpeg to 16 kHz mono, damages a window of each, and posts
every variant to a running research service's ``/identify``, one per GAP_S (3 s; 95 requests).
Babble and Sage's voice tail are the STT fixture's speech. No robot: room, level and talkers
are simulated, so a real mic can still do worse.

    make bench-identify               # SAGE_VOICE_RESEARCH_URL, 127.0.0.1:8783 by default
"""

import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
AUDIO = HERE / ".audio"
SPEECH = HERE.parent / "services/stt/fixtures/good-morning-little-robot.wav"
RATE, START_S = 16000, 8.0
GAP_S = float(os.environ.get("GAP_S", "3"))  # Shazam answers 429 after a dozen quick requests
SONGS = [("Blinding Lights", "The Weeknd"), ("Billie Jean", "Michael Jackson"),
         ("Smells Like Teen Spirit", "Nirvana"), ("Take Five", "The Dave Brubeck Quartet"),
         ("Lose Yourself", "Eminem")]
rng = np.random.default_rng(7)


def fetch(title: str, artist: str) -> np.ndarray:
    """The song's preview as 16 kHz mono float, downloaded once."""
    path = AUDIO / f"{title}.wav"
    if not path.exists():
        AUDIO.mkdir(exist_ok=True)
        q = urllib.parse.urlencode({"term": f"{title} {artist}", "entity": "song", "limit": 5})
        hits = json.load(urllib.request.urlopen(f"https://itunes.apple.com/search?{q}", timeout=20))["results"]
        hit = next(h for h in hits if h["trackName"].casefold().startswith(title.casefold()))
        m4a = urllib.request.urlopen(hit["previewUrl"], timeout=30).read()
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", "pipe:", "-ac", "1", "-ar", str(RATE), str(path)],
                       input=m4a, check=True)
    return read(path)


def read(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float64) / 32768


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x ** 2))) or 1e-9


def at_snr(music: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    return music + noise * rms(music) / rms(noise) / 10 ** (snr_db / 20)


def pink(n: int) -> np.ndarray:
    spectrum = np.fft.rfft(rng.standard_normal(n))
    return np.fft.irfft(spectrum / np.sqrt(np.maximum(np.arange(len(spectrum)), 1)), n)


def babble(n: int, speech: np.ndarray) -> np.ndarray:
    """Four talkers: the fixture at four speeds, each looped from its own offset."""
    out = np.zeros(n)
    for speed in (0.85, 1.0, 1.15, 1.3):
        voice = np.interp(np.arange(0, len(speech), speed), np.arange(len(speech)), speech)
        out += np.roll(np.tile(voice, n // len(voice) + 1)[:n], rng.integers(n))
    return out


def reverb(x: np.ndarray, rt60_s: float = 0.6) -> np.ndarray:
    """A room: the direct sound plus an exponentially decaying noise tail, as loud as the direct."""
    t = np.arange(int(rt60_s * RATE)) / RATE
    ir = rng.standard_normal(len(t)) * np.exp(-6.9 * t / rt60_s)
    ir *= 1 / np.sqrt(np.sum(ir ** 2))
    ir[0] += 1
    return np.convolve(x, ir)[:len(x)]


def voice_tail(x: np.ndarray, speech: np.ndarray, seconds: float = 1.5) -> np.ndarray:
    """Sage still talking at the start: the speaker is on the robot, so 6 dB over the music."""
    n = int(seconds * RATE)
    loudest = max(range(0, len(speech) - n, RATE // 20), key=lambda i: rms(speech[i:i + n]))
    out = x.copy()
    out[:n] += speech[loudest:loudest + n] * rms(x) / rms(speech) * 2
    return out


def room(x: np.ndarray, speech: np.ndarray) -> np.ndarray:
    """All of it: a reverberant room, a fan at 10 dB, people at 10 dB, Sage's tail, 20 dB down."""
    wet = reverb(x)
    noisy = at_snr(at_snr(wet, pink(len(x)), 10), babble(len(x), speech), 10)
    return voice_tail(noisy, speech) * 0.1


def conditions(speech: np.ndarray) -> list:
    """(name, damage, lengths): every damage at 10 s; clean and the whole room at every length."""
    every = (5, 8, 10, 12)
    out = [("clean", lambda x: x, every)]
    out += [(f"pink {snr} dB", lambda x, s=snr: at_snr(x, pink(len(x)), s), (10,)) for snr in (20, 10, 5, 0)]
    out += [(f"babble {snr} dB", lambda x, s=snr: at_snr(x, babble(len(x), speech), s), (10,)) for snr in (20, 10, 5, 0)]
    out += [("level -20 dB", lambda x: x * 0.1, (10,)), ("reverb 0.6 s", reverb, (10,)),
            ("voice tail 1.5 s", lambda x: voice_tail(x, speech), (10,)), ("room (all)", lambda x: room(x, speech), every)]
    return out


def wav(x: np.ndarray) -> bytes:
    peak = np.max(np.abs(x))
    if peak > 1:  # loud mixes clip on a real mic too; scale instead so only the SNR changes
        x = x / peak
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((x * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def identify(url: str, data: bytes) -> tuple[dict, float]:
    boundary = "benchboundary"
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="s.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(url + "/identify", body,
                                     {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=40) as r:
            return json.loads(r.read()), time.monotonic() - start
    except (urllib.error.HTTPError, TimeoutError) as e:  # 502: Shazam did not answer
        print(f"  error: {e!r}", file=sys.stderr)
        return {"found": False}, time.monotonic() - start


def main(url: str) -> None:
    speech = read(SPEECH)
    songs = [(title, fetch(title, artist)) for title, artist in SONGS]
    lengths = (5, 8, 10, 12)
    print(f"{'condition':<18}" + "".join(f"{f'{s} s':>14}" for s in lengths) + "   (found/songs, median s)")
    for name, damage, used in conditions(speech):
        cells = []
        for seconds in lengths:
            if seconds not in used:
                cells.append(f"{'-':>14}")
                continue
            hits, times = 0, []
            for title, audio in songs:
                clip = damage(audio[int(START_S * RATE):int((START_S + seconds) * RATE)])
                out, took = identify(url, wav(clip))
                right = out.get("found") and title.casefold() in out["title"].casefold()
                hits, times = hits + bool(right), times + [took]
                if out.get("found") and not right:
                    print(f"  wrong: {title} heard as {out['title']} ({name}, {seconds} s)", file=sys.stderr)
                time.sleep(GAP_S)
            cells.append(f"{f'{hits}/{len(songs)}  {np.median(times):.1f}':>14}")
        print(f"{name:<18}" + "".join(cells), flush=True)


if __name__ == "__main__":
    main(os.environ.get("SAGE_VOICE_RESEARCH_URL", "http://127.0.0.1:8783").rstrip("/"))
