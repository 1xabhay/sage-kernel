"""Beat tracking on synthetic signals: click tracks, noise, silence and babble."""

import numpy as np
import pytest

from reachy_sage_voice import beat

RATE = 16000


def clicks(bpm, seconds=8.0, offset=0.1, level=0.5):
    """Make a click track: 10 ms decaying 2 kHz blips, the first at ``offset`` s."""
    x = np.zeros(int(RATE * seconds), np.float32)
    n = np.arange(int(0.01 * RATE))
    blip = level * np.sin(2 * np.pi * 2000 * n / RATE) * np.exp(-n / 40)
    for t in np.arange(offset, seconds, 60 / bpm):
        i = int(t * RATE)
        x[i:i + len(blip)] += blip[:len(x) - i]
    return x


def pcm(x):
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def noise(seconds, level, seed=0):
    return np.random.default_rng(seed).normal(0, level, int(RATE * seconds)).astype(np.float32)


def babble(seconds=8.0, seed=1):
    """Speech-like: noise bursts of syllable length at irregular gaps."""
    rng = np.random.default_rng(seed)
    x = np.zeros(int(RATE * seconds), np.float32)
    t = 0.0
    while t < seconds:
        n = int(rng.uniform(0.08, 0.3) * RATE)
        i = int(t * RATE)
        burst = rng.normal(0, 0.2, n) * np.hanning(n)
        x[i:i + n] += burst[:len(x) - i]
        t += n / RATE + rng.uniform(0.02, 0.4)
    return x


@pytest.mark.parametrize("bpm", [90, 120, 150])
def test_a_click_track_reads_its_tempo(bpm):
    found, _, confidence = beat.tempo(pcm(clicks(bpm)))
    assert abs(found - bpm) <= 3
    assert confidence >= beat.MIN_CONFIDENCE
    assert beat.music_present(pcm(clicks(bpm)))


@pytest.mark.parametrize("bpm", [90, 120, 150])
def test_phase_is_the_time_since_the_last_beat_within_20_ms(bpm):
    x = clicks(bpm, offset=0.137)
    beats = np.arange(0.137, 8.0, 60 / bpm)
    _, phase, _ = beat.tempo(pcm(x))
    assert abs((8.0 - phase) - beats[-1]) <= 0.02


@pytest.mark.parametrize("bpm", [90, 120, 150])
def test_clicks_in_noise_at_10_db_snr(bpm):
    x = clicks(bpm)
    signal = np.mean(x ** 2)
    x = x + noise(8.0, np.sqrt(signal / 10))
    found, _, confidence = beat.tempo(pcm(x))
    assert abs(found - bpm) <= 3
    assert confidence >= beat.MIN_CONFIDENCE


def test_silence_has_no_beat():
    assert not beat.music_present(bytes(2 * RATE * 8))
    assert not beat.music_present(pcm(noise(8.0, 1e-4)))


def test_speech_like_noise_is_low_confidence():
    for seed in range(5):
        _, _, confidence = beat.tempo(pcm(babble(seed=seed)))
        assert confidence < beat.MIN_CONFIDENCE, seed
        assert not beat.music_present(pcm(babble(seed=seed)))


def test_music_that_just_stopped_is_not_present():
    x = clicks(120)
    x[int(6.5 * RATE):] = 0
    assert not beat.music_present(pcm(x))


def test_too_little_audio_is_no_estimate():
    assert beat.tempo(pcm(clicks(120, seconds=1.0))) is None
    assert not beat.music_present(b"")


def voiced(seconds=8.0, seed=2):
    """Speech-like with pitch: harmonic syllables at 110-220 Hz, irregularly spaced."""
    rng = np.random.default_rng(seed)
    x = np.zeros(int(RATE * seconds), np.float32)
    t = 0.0
    while t < seconds:
        n = int(rng.uniform(0.1, 0.35) * RATE)
        i = int(t * RATE)
        f0 = rng.uniform(110, 220)
        k = np.arange(n) / RATE
        syllable = sum(np.sin(2 * np.pi * f0 * h * k) / h for h in range(1, 8)) * np.hanning(n)
        x[i:i + n] += 0.1 * syllable[:len(x) - i]
        t += n / RATE + rng.uniform(0.03, 0.35)
    return x


def test_voiced_speech_does_not_lock():
    for seed in range(5):
        assert beat.tempo(pcm(voiced(seed=seed)))[2] < beat.MIN_CONFIDENCE, seed
        assert not beat.music_present(pcm(voiced(seed=seed)))


def bed(seconds=8.0):
    """Clicks over a steady noise bed at about -30 dBFS: music with a body."""
    return clicks(120, seconds) + noise(seconds, 0.03)


def test_music_fading_out_is_not_present():
    x = bed()
    assert beat.music_present(pcm(x))
    x[-int(beat.TAIL_S * RATE):] *= 0.25  # 12 dB down, still well above the floor
    assert beat.dbfs(pcm(x[-RATE:])) > beat.MIN_DBFS
    assert not beat.music_present(pcm(x))


def test_a_drop_is_the_music_getting_much_louder():
    x = bed()
    assert not beat.drop(pcm(x))
    x[-int(beat.TAIL_S * RATE):] *= 3  # 9.5 dB up
    assert beat.drop(pcm(x))


def test_music_starting_after_silence_is_not_a_drop():
    x = bed()
    x[:-int(beat.TAIL_S * RATE)] = 0
    assert not beat.drop(pcm(x))
