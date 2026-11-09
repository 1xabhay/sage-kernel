r"""Replay public labelled speech from a Mac speaker into the robot's mic, with reference texts.

A robot-mic set made without the owner's time. Runbook: script.md, "Replay set".

    uv run python eval/replay.py fetch [--rate 48000] [--skip-synthetic]
        LibriSpeech test-clean (CC BY 4.0, openslr.org/12, md5-checked): 150 utterances, 75
        female and 75 male across 20 or more speakers, plus 30 of script.md's command and name
        lines read by Kokoro voices (synthetic, never the owner's), each at one loudness and
        resampled to the Mac's output rate, into eval/replay/source/ with manifest.jsonl.
    uv run --with sounddevice python eval/replay.py preflight
        Beam routed, robot up and checked, the voice not running, the Mac's output noted.
    uv run --with sounddevice python eval/replay.py run --distance 0.5 [--music song.wav --snr 10]
        Each source clip, padded with silence (and music under it at --snr dB), plays from the
        Mac while record.py's replay records the robot (raw/); each take is then aligned to what
        played by cross-correlation, trimmed and level-checked into set/, in record.py's manifest
        format with speaker "replay". set/<session>/run.json keeps the acoustic delay, which the
        dance lead time needs. --limit 5 is a level check into check/, replaced each time.

Everything lands in eval/replay/ (git-ignored). Nothing here is the owner's voice.
"""

import argparse
import datetime
import hashlib
import json
import math
import re
import shutil
import sys
import tarfile
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO

import numpy as np
import record
import soundfile
from scipy.signal import correlate, resample_poly

HERE = Path(__file__).resolve().parent
OUT = HERE / "replay"
MIC = record.RATE
URL = "https://www.openslr.org/resources/12/test-clean.tar.gz"
MD5 = "32fa31d27d2e1cad72775fee3f4849a9"  # openslr.org/resources/12/md5sum.txt
CORPUS, LICENCE = "librispeech-test-clean", "CC BY 4.0"
#: Kokoro voices, female and male in turn; af_heart, the robot's own voice, is left out.
VOICES = (
    "af_bella",
    "am_adam",
    "bf_emma",
    "bm_george",
    "af_nicole",
    "am_michael",
    "bf_isabella",
    "bm_lewis",
    "af_sarah",
    "am_eric",
    "bf_alice",
    "bm_daniel",
)
TARGET_DBFS = -23.0  # every source clip's RMS, so the Mac's volume alone sets the level
CAP = 10 ** (-1 / 20)  # no playback file peaks above -1 dBFS
PAD_S = 0.3  # kept around the speech when a take is trimmed
LEVEL_DBFS = (-20.0, -6.0)  # a take's peak, each channel


# Fetch --------------------------------------------------------------------------------------


def parse_speakers(text: str) -> dict[str, str]:
    """SPEAKERS.TXT's test-clean readers: {id: "F" | "M"}."""
    rows = (
        line.split("|") for line in text.splitlines() if line.strip() and not line.startswith(";")
    )
    return {
        r[0].strip(): r[1].strip() for r in rows if len(r) >= 3 and r[2].strip() == "test-clean"
    }


def parse_transcripts(root: Path) -> list[tuple[str, str]]:
    """Every (utterance id, text) of the ``*.trans.txt`` files under ``root``, sorted."""
    return sorted(
        tuple(line.split(" ", 1))
        for path in Path(root).glob("*/*/*.trans.txt")
        for line in path.read_text().splitlines()
        if " " in line
    )


def pick(
    genders: Mapping[str, str],
    utterances: list[tuple[str, str]],
    n: int = 150,
    words: tuple[int, int] = (6, 20),
    seed: int = 0,
) -> list[tuple[str, str]]:
    """``n`` utterances, half female, half male, dealt round the readers of each in turn.

    Lines of ``words`` words (about 2 to 7 s) only; each reader's are shuffled by ``seed``,
    so the picks are repeatable and not all a chapter's opening.
    """
    rng = np.random.default_rng(seed)
    by_reader: dict[str, list] = {}
    for uid, text in utterances:
        if words[0] <= len(text.split()) <= words[1] and uid.split("-")[0] in genders:
            by_reader.setdefault(uid.split("-")[0], []).append((uid, text))
    picked = []
    for gender, want in (("F", n // 2), ("M", n - n // 2)):
        queues = [
            list(rng.permutation(len(by_reader[r])))
            for r in sorted(by_reader)
            if genders[r] == gender
        ]
        readers = [r for r in sorted(by_reader) if genders[r] == gender]
        got = 0
        while got < want and any(queues):
            for reader, queue in zip(readers, queues, strict=True):
                if queue and got < want:
                    picked.append(by_reader[reader][queue.pop(0)])
                    got += 1
    return picked


def _level(audio: np.ndarray) -> np.ndarray:
    """``audio`` at TARGET_DBFS RMS, turned down if that would peak over CAP."""
    rms = math.sqrt(float(np.mean(np.square(audio, dtype=np.float64)))) or 1.0
    audio = audio * (10 ** (TARGET_DBFS / 20) / rms)
    return audio * min(1.0, CAP / max(float(np.max(np.abs(audio))), 1e-9))


def resample(audio: np.ndarray, rate: int, to: int) -> np.ndarray:
    """Polyphase resampling (scipy), float32."""
    g = math.gcd(rate, to)
    return (audio if rate == to else resample_poly(audio, to // g, rate // g)).astype(np.float32)


def _mono(path) -> tuple[np.ndarray, int]:
    audio, rate = soundfile.read(str(path), dtype="float32")
    return (audio.mean(axis=1) if audio.ndim == 2 else audio), rate


def fetch(
    base: Path,
    out: Path,
    rate: int,
    synthesize: Callable[[str, str], tuple[np.ndarray, int]] | None,
    n: int = 150,
    synthetic: int = 30,
) -> list[dict]:
    """Write the picked LibriSpeech clips and ``synthetic`` Kokoro lines; return the manifest.

    ``base`` is the unpacked LibriSpeech folder (SPEAKERS.TXT, test-clean/). ``synthesize(text,
    voice)`` gives (float32 audio, its rate); None leaves the synthetic lines out.
    """
    base, out = Path(base), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    genders = parse_speakers((base / "SPEAKERS.TXT").read_text())
    entries = []
    for uid, text in pick(genders, parse_transcripts(base / "test-clean"), n):
        reader, chapter, _ = uid.split("-")
        audio, source_rate = _mono(base / "test-clean" / reader / chapter / f"{uid}.flac")
        entries.append(
            {
                "wav": f"ls-{uid}.wav",
                "text": text,
                "script_id": uid,
                "corpus": CORPUS,
                "source_speaker": reader,
                "gender": genders[reader],
                "synthetic": False,
                "licence": LICENCE,
            }
        )
        soundfile.write(
            out / entries[-1]["wav"],
            _level(resample(audio, source_rate, rate)),
            rate,
            subtype="PCM_16",
        )
    if synthesize and synthetic:
        script = record.parse_script(HERE / "script.md")
        commands = [x for x in script if x["script_id"][0] == "C"][: synthetic * 2 // 3]
        names = [x for x in script if x["script_id"][0] == "N"][: synthetic - len(commands)]
        for i, line in enumerate(commands + names):
            voice = VOICES[i % len(VOICES)]
            audio, source_rate = synthesize(line["text"], voice)
            entries.append(
                {
                    "wav": f"kokoro-{line['script_id']}-{voice}.wav",
                    "text": line["text"],
                    "script_id": line["script_id"],
                    "corpus": "kokoro",
                    "source_speaker": f"kokoro:{voice}",
                    "gender": "F" if voice[1] == "f" else "M",
                    "synthetic": True,
                    "licence": "Apache-2.0 (Kokoro 82M); text from script.md",
                }
            )
            soundfile.write(
                out / entries[-1]["wav"],
                _level(resample(audio, source_rate, rate)),
                rate,
                subtype="PCM_16",
            )
    (out / "manifest.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
    return entries


def download(cache: Path) -> Path:
    """test-clean.tar.gz into ``cache`` (md5-checked, once), unpacked there; return LibriSpeech/."""
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / "test-clean.tar.gz"
    if not archive.exists():
        print(f"downloading {URL} (346 MB)")
        urllib.request.urlretrieve(URL, cache / "test-clean.tar.gz.part")
        (cache / "test-clean.tar.gz.part").rename(archive)
    digest = hashlib.md5(archive.read_bytes()).hexdigest()
    if digest != MD5:
        archive.unlink()
        sys.exit(f"test-clean.tar.gz md5 {digest}, expected {MD5}: deleted, run again")
    if not (cache / "LibriSpeech" / "SPEAKERS.TXT").exists():
        with tarfile.open(archive) as tar:
            tar.extractall(cache, filter="data")
    return cache / "LibriSpeech"


# Playback, alignment, levels ----------------------------------------------------------------


def prepare(
    voice: np.ndarray,
    rate: int,
    music: np.ndarray | None = None,
    snr_db: float | None = None,
    lead_s: float = 0.5,
    tail_s: float = 0.5,
    offset: int = 0,
) -> np.ndarray:
    """Return what plays: silence, ``voice``, silence; ``music`` looped under all at ``snr_db``.

    The SNR is the voice's power over the music's. ponytail: music and speech leave the same
    speaker, so the beam cannot steer between them; a second output device for the music is
    the upgrade if the paired comparison needs spatial noise.
    """
    out = np.concatenate([np.zeros(int(lead_s * rate)), voice, np.zeros(int(tail_s * rate))])
    if music is not None:
        bed = np.resize(np.roll(music, -offset), len(out))
        gain = math.sqrt(np.mean(np.square(voice)) / max(np.mean(np.square(bed)), 1e-12)) / 10 ** (
            snr_db / 20
        )
        out = out + bed * gain
    return (out * min(1.0, CAP / max(float(np.max(np.abs(out))), 1e-9))).astype(np.float32)


def align(pcm: np.ndarray, played: np.ndarray, rate: int) -> dict:
    """Where ``played`` starts in each channel of the robot's take: delay_s and normalized xcorr.

    The delay runs from ``play()`` to the robot's mic: the Mac's output latency, the room, the
    chip and WebRTC. Only lags of zero or more are searched (the take starts before playback).
    """
    ref = resample(played, rate, MIC).astype(np.float64)
    found = {"delay_s": {}, "xcorr": {}}
    for i, channel in enumerate("LR"):
        x = pcm[:, i].astype(np.float64)
        c = correlate(x, ref, mode="full", method="fft")[len(ref) - 1 :]
        lag = int(np.argmax(np.abs(c)))
        norm = np.linalg.norm(ref) * np.linalg.norm(x[lag : lag + len(ref)])
        found["delay_s"][channel] = round(lag / MIC, 5)
        found["xcorr"][channel] = round(float(abs(c[lag]) / norm), 3) if norm else 0.0
    return found


def levels(pcm: np.ndarray) -> dict:
    """Return each channel's peak dBFS and clipped samples; level_ok: none clipped, in range."""
    peak = {
        ch: 20 * math.log10(max(int(np.max(np.abs(pcm[:, i].astype(np.int32)))), 1) / 32768)
        for i, ch in enumerate("LR")
    }
    clipped = {
        ch: int(np.sum(np.abs(pcm[:, i].astype(np.int32)) >= 32767)) for i, ch in enumerate("LR")
    }
    ok = not any(clipped.values()) and all(
        LEVEL_DBFS[0] <= p <= LEVEL_DBFS[1] for p in peak.values()
    )
    return {
        "peak_dbfs": {ch: round(p, 1) for ch, p in peak.items()},
        "clipped": clipped,
        "level_ok": ok,
    }


def _read(path) -> np.ndarray:
    audio, _ = soundfile.read(str(path), dtype="int16", always_2d=True)
    return audio


def run(
    media,
    source: Path,
    out: Path,
    meta: dict,
    play: Callable[[str], None],
    music: Path | None = None,
    snr_db: float | None = None,
    limit: int | None = None,
    tail_s: float = 0.5,
    lead_s: float = 0.5,
    playback: dict | None = None,
) -> list[dict]:
    """Play ``source``'s clips (its manifest) while ``media`` records; write the aligned set.

    ``out``/play/<session> holds what played, ``out``/raw record.py's untrimmed takes, and
    ``out``/set the trimmed takes, their manifest.jsonl and <session>/run.json.
    """
    source, out, session = Path(source), Path(out), meta["session"]
    clips = [
        json.loads(x) for x in (source / "manifest.jsonl").read_text().splitlines() if x.strip()
    ][:limit]
    bed = None
    folder = out / "play" / session
    shutil.rmtree(folder, ignore_errors=True)  # a stale file there would play too
    folder.mkdir(parents=True)
    labels = {}
    for i, clip in enumerate(clips, 1):
        voice, rate = _mono(source / clip["wav"])
        if music and bed is None:
            bed = resample(*_mono(music), rate)
        name = f"{i:03d}-{Path(clip['wav']).stem}.wav"
        soundfile.write(
            folder / name,
            prepare(
                voice,
                rate,
                bed,
                snr_db,
                lead_s,
                lead_s,
                offset=(i * 7 * rate) % len(bed) if bed is not None else 0,
            ),
            rate,
            subtype="PCM_16",
        )
        labels[name] = {k: v for k, v in clip.items() if k != "wav"} | (
            {"snr_db": snr_db} if music else {}
        )
    raw = record.replay(
        media, out / "raw", folder, {**meta, "speaker": "replay"}, play, tail_s, labels=labels
    )
    (out / "set" / session).mkdir(parents=True, exist_ok=True)
    entries = []
    for take in raw:
        pcm = _read(out / "raw" / take["wav"])
        played, rate = _mono(folder / take["source"])
        found = align(pcm, played, rate)
        speech_s = len(played) / rate - 2 * lead_s
        delays = found["delay_s"].values()
        start = max(0, int((min(delays) + lead_s - PAD_S) * MIC))
        trimmed = np.ascontiguousarray(
            pcm[start : max(start, int((max(delays) + lead_s + speech_s + PAD_S) * MIC))]
        )
        wav = f"{session}/{Path(take['wav']).name}"
        record._write(out / "set" / wav, trimmed.tobytes(), 2)
        entries.append(
            {
                **take,
                "wav": wav,
                "raw_wav": take["wav"],
                **found,
                **levels(trimmed),
                "sha256": hashlib.sha256((out / "set" / wav).read_bytes()).hexdigest(),
            }
        )
        with (out / "set" / "manifest.jsonl").open("a") as f:
            f.write(json.dumps(entries[-1]) + "\n")
    summary = {
        "session": session,
        "n": len(entries),
        "distance_m": meta.get("distance_m"),
        "condition": meta.get("condition"),
        "snr_db": snr_db if music else None,
        "delay_s": {
            ch: {
                "median": round(float(np.median(d)), 4),
                "p10": round(float(np.percentile(d, 10)), 4),
                "p90": round(float(np.percentile(d, 90)), 4),
            }
            for ch in "LR"
            if (d := [e["delay_s"][ch] for e in entries])
        },
        "level_ok": sum(e["level_ok"] for e in entries),
        "clipped_takes": sum(any(e["clipped"].values()) for e in entries),
        "playback": playback or {},
        "note": "delay_s runs from play() to the robot's mic; less playback.latency_s it is the "
        "room-to-robot lead the dance needs",
    }
    (out / "set" / session / "run.json").write_text(json.dumps(summary, indent=2) + "\n")
    return entries


# Preflight ----------------------------------------------------------------------------------


def output_info(sounddevice, run: Callable) -> tuple[bool, str, dict]:
    """Return the Mac's default output device, its rate and latency, and the volume (osascript)."""
    device = sounddevice.query_devices(kind="output")
    _, said = run(["osascript", "-e", "get volume settings"])
    volume = re.search(r"output volume:(\d+)", said)
    muted = "output muted:true" in said
    info = {
        "device": device["name"],
        "rate": int(device["default_samplerate"]),
        "latency_s": device.get("default_high_output_latency"),
        "volume": int(volume[1]) if volume else None,
        "muted": muted,
    }
    why = (
        f"{info['device']}, {info['rate']} Hz, volume {info['volume']}%{' MUTED' if muted else ''}"
    )
    return (
        bool(volume) and int(volume[1]) > 0 and not muted,
        why + " (keep it for every condition)",
        info,
    )


def preflight(
    env: Mapping[str, str], get: Callable, run: Callable, sounddevice, out: TextIO = sys.stdout
) -> int:
    """Print each check for the live replay; return 1 if any fails."""
    from reachy_sage_voice import preflight as app_preflight
    from reachy_sage_voice import status

    host = env.get("REACHY_MINI_HOST")
    unset = (False, "REACHY_MINI_HOST not set")
    api = f"http://{host}:8000/api"
    code, pids = run(["pgrep", "-f", "[-]m reachy_sage_voice( |$)"])
    ok, why, _ = output_info(sounddevice, run)
    results = [
        ("robot", status.check(f"{api}/daemon/status", status._robot, get) if host else unset),
        (
            "beam",
            status.check(f"{api}/audio/config/parameter/AUDIO_MGR_OP_R", status._beam, get)
            if host
            else unset,
        ),
        ("check", app_preflight._robot(run)),
        (
            "bot",
            (
                False,
                f"the voice is running (pid {' '.join(pids.split())}): stop it, the recorder needs "
                "the mic",
            )
            if code == 0
            else (True, "the voice is not running"),
        ),
        ("output", (ok, why)),
    ]
    for name, (good, reason) in results:
        print(f"{name:<7} {'ok' if good else 'FAIL':<5} {reason}", file=out)
    return 0 if all(good for _, (good, _) in results) else 1


def main(argv: list[str] | None = None) -> None:
    """fetch, preflight or run."""
    import os

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="step", required=True)
    pull = sub.add_parser("fetch")
    pull.add_argument(
        "--rate", type=int, help="the Mac's output rate (default: its default device's, or 48000)"
    )
    pull.add_argument("--out", default=str(OUT / "source"))
    pull.add_argument("--cache", default=str(OUT / ".cache"))
    pull.add_argument("-n", type=int, default=150)
    pull.add_argument("--synthetic", type=int, default=30)
    pull.add_argument(
        "--skip-synthetic", action="store_true", help="LibriSpeech only (no Kokoro needed)"
    )
    pull.add_argument(
        "--tts-url", default=os.environ.get("SAGE_VOICE_TTS_URL", "http://127.0.0.1:8782")
    )
    sub.add_parser("preflight")
    go = sub.add_parser("run")
    go.add_argument("--distance", type=float, required=True, help="speaker to robot, metres")
    go.add_argument("--music", help="a WAV/FLAC looped under the speech")
    go.add_argument("--snr", type=float, default=10.0, help="speech over music, dB (with --music)")
    go.add_argument("--session")
    go.add_argument("--limit", type=int, help="only the first N clips, into check/: a level check")
    go.add_argument("--unrouted", action="store_true", help="R is not the ASR beam yet")
    go.add_argument("--source", default=str(OUT / "source"))
    go.add_argument("--out", default=str(OUT))
    args = parser.parse_args(argv)
    sd = None
    try:
        import sounddevice as sd
    except ImportError:
        if args.step != "fetch":
            parser.error(f"{args.step} needs sounddevice (uv run --with sounddevice ...)")
    from reachy_sage_voice.preflight import run_command

    if args.step == "fetch":
        rate = args.rate or (
            int(sd.query_devices(kind="output")["default_samplerate"]) if sd else 48000
        )
        synthesize = (
            None
            if args.skip_synthetic
            else lambda text, voice: record.kokoro(args.tts_url, voice)(text)
        )
        entries = fetch(
            download(Path(args.cache)), Path(args.out), rate, synthesize, args.n, args.synthetic
        )
        print(f"{len(entries)} clips at {rate} Hz in {args.out}")
        return
    if args.step == "preflight":
        sys.exit(preflight(os.environ, urllib.request.urlopen, run_command, sd))
    _, why, playback = output_info(sd, run_command)
    print(f"output: {why}")
    first = json.loads((Path(args.source) / "manifest.jsonl").read_text().splitlines()[0])
    if soundfile.info(str(Path(args.source) / first["wav"])).samplerate != playback["rate"]:
        print(
            f"warning: the clips are not at {playback['rate']} Hz; CoreAudio resamples "
            "(fetch again to match)"
        )
    condition = "music" if args.music else "quiet"
    session = args.session or (
        f"{datetime.datetime.now().astimezone().date()}-replay-{args.distance}m-{condition}"
        + (f"-snr{args.snr:g}" if args.music else "")
    )
    out = Path(args.out)
    if args.limit:
        out = out / "check"
        shutil.rmtree(out, ignore_errors=True)
    meta = {
        "distance_m": args.distance,
        "condition": condition,
        "session": session,
        "channels": record.channel_map(args.unrouted),
    }
    from reachy_mini import ReachyMini

    with ReachyMini(host=os.environ["REACHY_MINI_HOST"], connection_mode="network") as mini:
        mini.media.start_recording()
        try:
            entries = run(
                mini.media,
                Path(args.source),
                out,
                meta,
                record.mac_player(sd),
                Path(args.music) if args.music else None,
                args.snr,
                args.limit,
                playback=playback,
            )
        finally:
            mini.media.stop_recording()
    summary = json.loads((out / "set" / session / "run.json").read_text())
    for entry in entries:
        if not entry["level_ok"]:
            print(f"  level: {entry['wav']} peaks {entry['peak_dbfs']} clipped {entry['clipped']}")
    print(
        f"{summary['n']} takes, {summary['level_ok']} at a good level, "
        f"{summary['clipped_takes']} clipped; "
        f"delay {summary['delay_s']}; {out / 'set' / session / 'run.json'}"
    )


if __name__ == "__main__":
    main()
