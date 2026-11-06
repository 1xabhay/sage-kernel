"""Hear a beat in the mic: tempo, phase, confidence, and whether music is playing at all.

The music listener's ear, shared by dancing (``dance.py``) and listening to a
song (``song.py``); it knows nothing of either. Plain numpy on 16 kHz mono
int16 PCM, as ``ReachyInputTransport.snippet`` gives it. About 2 ms for 8 s.

- Onsets: spectral flux on 32 ms Hann frames every 10 ms (a 100 Hz envelope),
  log-compressed, so a soft hi-hat counts next to a loud kick.
- Tempo: the envelope's autocorrelation over 60-180 BPM, weighted by a
  one-octave log-normal prior around 120 BPM, which picks 150 over its half,
  75. The period is refined on its first few multiples, which reads it to a
  fraction of a frame.
- Phase: the comb over the envelope at that period that sums highest.
- Confidence: the normalised autocorrelation at the period and its multiples;
  a steady beat keeps it high, speech and noise do not, so it never locks on talk.
- Fade and drop: the last ``TAIL_S`` against the rest of the window. Well
  quieter is music fading out; well louder, after music, is the drop.

The lock gate, the fade and the drop are adapted from binhpham's
reachy-mini-dance-to-music (Apache-2.0, see NOTICE), which reached the same
flux, autocorrelation and comb design independently.
"""

import numpy as np

RATE = 16000
FRAME = 512  # 32 ms
HOP = 160  # 10 ms
FPS = RATE / HOP
MIN_BPM, MAX_BPM = 60, 180
PRIOR_BPM = 120
MIN_SECONDS = 3.0  # less holds too few beats to call a tempo
MIN_CONFIDENCE = 0.3  # ponytail: set on synthetic clicks and babble; tune on real music in the room
MIN_DBFS = -50.0  # quieter than this is not music. ponytail: tune to the room's noise floor
TAIL_S = 1.0  # music_present also wants sound in this last stretch, so a stop shows within ~1 s
FADE_DB = 10.0  # the tail this much quieter than the rest: fading out
DROP_DB = 5.0  # the tail this much louder than the rest: the drop
_WINDOW = np.hanning(FRAME).astype(np.float32)


def _samples(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, "<i2").astype(np.float32) / 32768


def dbfs(pcm: bytes) -> float:
    """Loudness of ``pcm`` in dB below full scale; -inf-ish for silence."""
    x = _samples(pcm)
    return float(10 * np.log10(np.mean(x * x) + 1e-12)) if len(x) else -120.0


def onset_envelope(x: np.ndarray) -> np.ndarray:
    """Half-wave-rectified log spectral flux, one value per 10 ms hop."""
    frames = np.lib.stride_tricks.sliding_window_view(x, FRAME)[::HOP] * _WINDOW
    mag = np.log1p(1000 * np.abs(np.fft.rfft(frames, axis=1)))
    return np.maximum(np.diff(mag, axis=0), 0).sum(axis=1)


def tempo(pcm: bytes) -> tuple[float, float, float] | None:
    """``(bpm, phase, confidence)`` of the beat in ``pcm``, or None if it is under 3 s.

    ``phase`` is how long before the end of ``pcm`` the last beat fell, in
    seconds, so the next one is due ``60 / bpm - phase`` after it.
    """
    x = _samples(pcm)
    if len(x) < MIN_SECONDS * RATE:
        return None
    env = onset_envelope(x)
    e = env - env.mean()
    n = len(e)
    ac = np.fft.irfft(np.abs(np.fft.rfft(e, 2 * n)) ** 2)[:n]
    if ac[0] <= 0:
        return (float(PRIOR_BPM), 0.0, 0.0)
    ac /= ac[0]

    lo, hi = int(FPS * 60 / MAX_BPM), int(np.ceil(FPS * 60 / MIN_BPM))
    lags = np.arange(lo, hi + 1)
    prior = np.exp(-0.5 * np.log2(FPS * 60 / lags / PRIOR_BPM) ** 2)
    coarse = lags[np.argmax(ac[lags] * prior)]

    # Refine on multiples: k periods out, a frame is k times finer.
    fine = np.linspace(coarse - 1, coarse + 1, 81)
    ks = np.arange(1, 5)
    at = fine[:, None] * ks[None, :]
    score = np.where(at < n / 2, np.interp(at, np.arange(n), ac), 0).sum(axis=1)
    period = fine[np.argmax(score)]
    hits = ks[ks * period < n / 2]
    confidence = float(np.clip(np.interp(hits * period, np.arange(n), ac).mean(), 0, 1))

    # Phase: the comb offset that collects the most onset strength.
    offsets = np.arange(int(np.ceil(period)))
    teeth = np.arange(int(n / period) + 1)
    idx = np.round(offsets[:, None] + period * teeth[None, :]).astype(int)
    comb = np.where(idx < n, env[np.minimum(idx, n - 1)], 0).sum(axis=1)
    first = offsets[np.argmax(comb)]
    last = first + period * np.floor((n - 1 - first) / period)
    beat_sample = last * HOP + FRAME  # flux i peaks as an onset enters frame i + 1's tail
    return (float(60 * FPS / period), float((len(x) - beat_sample) / RATE), confidence)


def _tail(pcm: bytes) -> tuple[float, float]:
    # (dBFS of the last TAIL_S, dBFS of everything before it)
    cut = len(pcm) - 2 * int(TAIL_S * RATE)
    return dbfs(pcm[cut:]), dbfs(pcm[:cut])


def music_present(pcm: bytes) -> bool:
    """Whether ``pcm`` holds music: on a beat, loud enough, still sounding and not fading."""
    found = tempo(pcm)
    if found is None or found[2] < MIN_CONFIDENCE:
        return False
    tail, rest = _tail(pcm)
    return dbfs(pcm) > MIN_DBFS and tail > MIN_DBFS and tail > rest - FADE_DB


def drop(pcm: bytes) -> bool:
    """Whether the music in ``pcm`` just got much louder: the drop after a breakdown.

    Music starting after silence is not one. Stateless: the caller keeps any
    warm-up and cool-down.
    """
    if len(pcm) < 2 * MIN_SECONDS * RATE:
        return False
    tail, rest = _tail(pcm)
    return rest > MIN_DBFS and tail > rest + DROP_DB
