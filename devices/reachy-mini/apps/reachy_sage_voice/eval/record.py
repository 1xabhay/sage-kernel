r"""Record the eval set from the robot's mic, raw: both channels, as the robot sends them.

Each clip is ``media.get_audio_sample()`` saved as a 2-channel 16 kHz WAV with a line in
``manifest.jsonl`` that names what each channel carries (``channels``) and the WAV's sha256.
After the robot lane's chip route, L is the conference output (AUDIO_MGR_OP_L=[8,0]) and R the
ASR beam (AUDIO_MGR_OP_R=[7,3]); ``--unrouted`` says R is still the chip's default. Scorers
pick a channel (``score.py ... --channel L|R|mean``), so one session compares the two.
With ``--mac`` and ``sounddevice`` installed, the Mac's own mic records the same window to
``<clip>.mac.wav`` (``mac_wav`` in the manifest). Instructions in ``script.md``.

    REACHY_MINI_HOST=reachy-mini.local uv run python eval/record.py \
        --distance 0.5 --condition quiet [--speaker owner] [--session 2026-10-23a] [--mac]
    ... --speaker other --condition tv --clips 10 --seconds 4   # untranscribed: TV, music
    ... --only T01,T02 --tts       # the robot speaks Kokoro lines over each take
    ... --speaker impostor --condition replay --replay DIR   # unattended: DIR's WAVs from a
                                                             # Mac speaker, the robot's mic recorded

The recordings are the owner's voice, which is biometric: they stay in eval/recordings/
(git-ignored), never committed, never uploaded.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import threading
import time
import urllib.request
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
RATE = 16000  # the robot's mic, as the pipeline reads it
_IDLE_S = 0.005
CHANNEL_MAP = {"L": "conference AUDIO_MGR_OP_L=[8,0]", "R": "ASR beam AUDIO_MGR_OP_R=[7,3]"}
#: What the robot says over a ``--tts`` take, in turn: a reply, a song note, a question.
BOT_LINES = ("Sure, I can do that. Give me a second while I think about it.",
             "That song came out in nineteen eighty two and was produced by Quincy Jones.",
             "I am listening. Tell me more about what you would like to hear today.",
             "Here is something I remember about that band and how they got started.",
             "Do you want me to keep looking around, or should I stay right here with you?")


def channel_map(unrouted: bool) -> dict:
    """What L and R carry: the conference and the ASR beam, unless the beam is not routed yet."""
    return {**CHANNEL_MAP, "R": "unrouted: the chip's default second output"} if unrouted else CHANNEL_MAP


def parse_script(path) -> list[dict]:
    """Return the script's lines: ``S01  Yes.`` -> {"script_id": "S01", "text": "Yes."}."""
    return [{"script_id": m[1], "text": m[2].strip()}
            for m in re.finditer(r"^([A-Z]\d{2}) +(.+)$", Path(path).read_text(), re.M)]


def _write(path: Path, pcm: bytes, channels: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


def _stereo_pcm16(samples: np.ndarray) -> bytes:
    """Mic samples as interleaved 2-channel int16; a mono mic goes to both."""
    samples = samples.reshape(len(samples), -1)
    if samples.shape[1] == 1:
        samples = np.repeat(samples, 2, axis=1)
    return (np.clip(samples[:, :2], -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def record_clip(media, out: Path, line: dict, meta: dict, stop: Callable[[], bool], mac=None) -> dict:
    """Read the mic until ``stop()``; write the WAV and append its manifest line, returned.

    ``media`` is already recording. ``meta`` (speaker, distance_m, condition, session,
    channels, ...) goes into the line as given. ``mac``, if given, has ``start()`` and
    ``stop() -> int16 array``.
    """
    out = Path(out)
    chunks = []
    if mac:
        mac.start()
    while not stop():
        samples = media.get_audio_sample()
        if samples is None:
            time.sleep(_IDLE_S)
        else:
            chunks.append(_stereo_pcm16(samples))
    stem = f"{line['script_id']}-{meta['distance_m']}m-{meta['condition']}-{meta['speaker']}"
    folder = out / meta["session"]
    folder.mkdir(parents=True, exist_ok=True)
    take, first = 1, stem
    while (folder / f"{stem}.wav").exists():
        take += 1
        stem = f"{first}-{take}"
    _write(folder / f"{stem}.wav", b"".join(chunks), 2)
    entry = {"wav": f"{meta['session']}/{stem}.wav", "text": line["text"], **meta,
             "script_id": line["script_id"],
             "sha256": hashlib.sha256((folder / f"{stem}.wav").read_bytes()).hexdigest()}
    if mac:
        _write(folder / f"{stem}.mac.wav", mac.stop().tobytes(), 1)
        entry["mac_wav"] = f"{meta['session']}/{stem}.mac.wav"
    with (out / "manifest.jsonl").open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def play_on_robot(media, audio: np.ndarray, rate: int) -> None:
    """Hand ``audio`` (mono float32) to the robot's speaker at its rate; it plays while we record.

    ponytail: linear resampling, as services/stt does; plenty for overlap audio.
    """
    out_rate = media.get_output_audio_samplerate()
    n = round(len(audio) * out_rate / rate)
    media.start_playing()
    media.push_audio_sample(np.interp(np.arange(n) * rate / out_rate, np.arange(len(audio)),
                                      audio).astype(np.float32))


def tts_take(media, out: Path, line: dict, meta: dict, stop: Callable[[], bool],
             synthesize: Callable[[str], tuple[np.ndarray, int]], bot_line: str, mac=None) -> dict:
    """Record one take while the robot speaks ``bot_line``: the barge-in and echo case."""
    play_on_robot(media, *synthesize(bot_line))
    return record_clip(media, out, line, {**meta, "tts": bot_line}, stop, mac)


def kokoro(url: str, voice: str = "azelma") -> Callable[[str], tuple[np.ndarray, int]]:
    """Text to (float32 audio, 24000) through the local Kokoro's plain /v1/audio/speech."""
    from reachy_sage_voice.tts import RATE as KOKORO_RATE

    def synthesize(text):
        body = json.dumps({"input": text, "voice": voice, "model": "kokoro",
                           "response_format": "pcm"}).encode()
        request = urllib.request.Request(url.rstrip("/").removesuffix("/v1") + "/v1/audio/speech",
                                         body, {"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=60) as response:
            pcm = np.frombuffer(response.read(), np.int16)
        return pcm.astype(np.float32) / 32768, KOKORO_RATE
    return synthesize


def replay(media, out: Path, source: Path, meta: dict, play: Callable[[str], None],
           tail_s: float = 0.5, mac=None, labels: dict[str, dict] | None = None) -> list[dict]:
    """Play each WAV in ``source`` (sorted) with ``play(path)``, recording the robot meanwhile.

    ``play`` blocks until the file has played; recording runs ``tail_s`` past it, for the room's
    echo and the WebRTC lag. Nobody is needed at the keyboard. ``labels`` maps a file name to
    its script_id, text and any other fields for its manifest line (eval/replay.py); a file
    without one is untranscribed, I001 on.
    """
    entries = []
    for i, path in enumerate(sorted(Path(source).glob("*.wav")), 1):
        _drain(media)
        player = threading.Thread(target=play, args=(str(path),), daemon=True)
        ended: list[float] = []

        def stop(player=player, ended=ended):
            if player.is_alive():
                return False
            ended[:] = ended or [time.monotonic()]
            return time.monotonic() - ended[0] >= tail_s
        label = dict((labels or {}).get(path.name) or {"script_id": f"I{i:03d}", "text": ""})
        line = {"script_id": label.pop("script_id"), "text": label.pop("text")}
        player.start()
        entries.append(record_clip(media, out, line, {**meta, "source": path.name, **label},
                                   stop, mac))
    return entries


def mac_player(sounddevice) -> Callable[[str], None]:
    """Play a WAV from the Mac's default output, returning when it has played."""
    def play(path):
        with wave.open(path) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16).reshape(-1, w.getnchannels())
            sounddevice.play(pcm, w.getframerate())
        sounddevice.wait()
    return play


class MacMic:
    """The Mac's default input through sounddevice, mono 16 kHz int16."""

    def __init__(self, sounddevice):
        """Use the already-imported ``sounddevice`` module."""
        self._sd, self._chunks, self._stream = sounddevice, [], None

    def start(self) -> None:
        """Open the stream and keep everything it hands over."""
        self._chunks = []
        self._stream = self._sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                                            callback=lambda data, *_: self._chunks.append(data.copy()))
        self._stream.start()

    def stop(self) -> np.ndarray:
        """Close the stream; return what it heard."""
        self._stream.stop()
        self._stream.close()
        return np.concatenate(self._chunks).ravel() if self._chunks else np.zeros(0, np.int16)


def _drain(media) -> None:
    """Drop what the mic buffered while the prompt waited."""
    while media.get_audio_sample() is not None:
        pass


def main(argv: list[str] | None = None) -> None:
    """Prompt each script line (or count untranscribed clips, or replay files) and record it."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(HERE / "recordings"))
    parser.add_argument("--speaker", choices=("owner", "other", "music", "impostor"), default="owner")
    parser.add_argument("--distance", type=float, choices=(0.5, 1.5), required=True)
    parser.add_argument("--condition", choices=("quiet", "music", "tv", "replay"), required=True)
    parser.add_argument("--session", default=datetime.datetime.now().astimezone().strftime("%Y-%m-%d"))
    parser.add_argument("--only", help="comma-separated script ids, e.g. S01,S07")
    parser.add_argument("--clips", type=int, help="record this many untranscribed clips instead")
    parser.add_argument("--seconds", type=float, help="stop each clip after this long (default: "
                        "Enter; 4 s for --clips)")
    parser.add_argument("--mac", action="store_true", help="also record the Mac's mic")
    parser.add_argument("--unrouted", action="store_true", help="R is not the ASR beam yet")
    parser.add_argument("--tts", action="store_true", help="the robot speaks Kokoro over each take "
                        "(SAGE_VOICE_TTS_URL, default http://127.0.0.1:8782)")
    parser.add_argument("--replay", help="unattended: play this folder's WAVs from the Mac's speaker")
    args = parser.parse_args(argv)
    sd = None
    if args.mac or args.replay:
        try:
            import sounddevice as sd
        except ImportError:
            if args.replay:
                parser.error("--replay needs sounddevice (uv run --with sounddevice ...)")
            print("sounddevice is not installed: recording the robot's mic only")
    mac = MacMic(sd) if args.mac and sd else None
    meta = {"speaker": args.speaker, "distance_m": args.distance, "condition": args.condition,
            "session": args.session, "channels": channel_map(args.unrouted)}
    synthesize = kokoro(os.environ.get("SAGE_VOICE_TTS_URL", "http://127.0.0.1:8782")) if args.tts else None
    seconds = args.seconds or (4.0 if args.clips else None)
    if args.clips:
        lines = [{"script_id": f"F{i:02d}", "text": ""} for i in range(1, args.clips + 1)]
    else:
        lines = parse_script(HERE / "script.md")
        if args.only:
            lines = [line for line in lines if line["script_id"] in args.only.split(",")]

    from reachy_mini import ReachyMini

    with ReachyMini(host=os.environ["REACHY_MINI_HOST"], connection_mode="network") as mini:
        mini.media.start_recording()
        try:
            if args.replay:
                for entry in replay(mini.media, Path(args.out), Path(args.replay), meta,
                                    mac_player(sd), mac=mac):
                    print(f"  saved {entry['wav']} ({entry['source']})")
                return
            for i, line in enumerate(lines):
                print(f"\n{line['script_id']}  {line['text'] or '(untranscribed)'}")
                answer = input("Enter to record, s to skip, q to quit: ").strip().lower()
                if answer == "q":
                    break
                if answer == "s":
                    continue
                _drain(mini.media)
                if seconds:
                    until = time.monotonic() + seconds
                    stop = lambda u=until: time.monotonic() >= u  # noqa: E731
                else:
                    done = threading.Event()
                    threading.Thread(target=lambda d=done: (input("  recording... Enter to stop"),
                                                            d.set()), daemon=True).start()
                    stop = done.is_set
                if synthesize:
                    entry = tts_take(mini.media, Path(args.out), line, meta, stop, synthesize,
                                     BOT_LINES[i % len(BOT_LINES)], mac)
                else:
                    entry = record_clip(mini.media, Path(args.out), line, meta, stop, mac)
                print(f"  saved {entry['wav']}")
        finally:
            mini.media.stop_recording()


if __name__ == "__main__":
    main()
