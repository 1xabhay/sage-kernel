"""The noise trace: a record per heard segment, to study replies to noise, TV and other people.

Built after the fact from the bot's log and the session's recordings (``SAGE_VOICE_RECORD_DIR``),
so the live audio path does nothing extra: it only logs one ``stt:`` line per transcript (the STT's
word confidence and voiceprint score). Fields follow the research note's trace list: audio, context,
STT, speaker, space, decision, and the owner's label. The audio and words are the owner's: the
trace, labels and CSV go next to the recordings (git-ignored), never into the repo.

    uv run python eval/noise.py trace  <log> [--recordings DIR] [--out trace.jsonl]
    uv run python eval/noise.py review <log> [--recordings DIR] [--all]
    uv run python eval/noise.py csv    <log> [--recordings DIR] --out trace.csv
    uv run python eval/noise.py backfill [--logs GLOB] [--recordings DIR]

``--recordings`` is the folder of ``session-*`` folders: every session whose recordings fall in the
log's time span is joined. Labels: y to me, n not to me (then t TV, m music, o another person,
e self-talk, Enter skip), k noise, s unsure, p play, q quit.
"""

import argparse
import csv
import glob
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import turn_taking as tt
from sage_device import latency

from reachy_sage_voice.turn_end import SAMPLE_RATE, load_pcm16

JOIN_S = 0.3  # a recording's name is its VAD stop; the log's stop line lands within this
STALE_S = 15.0  # a VAD stop with no transcript this long after it never got one
FRAME = 512  # 32 ms at 16 kHz, Silero's frame
LABELS = {"y": "to_me", "n": "not_to_me", "k": "noise", "s": "unsure"}
SOURCES = {"t": "tv", "m": "music", "o": "other_person", "e": "self_talk"}
_SAGE = [  # (kind, pattern on "<logger>:<message>") for the voice's own lines
    (
        "stt",
        re.compile(
            r"pipeline:stt: confidence mean (\S+) min (\S+), similarity (\S+), words ?(.*)$"
        ),
    ),
    ("merged", re.compile(r"pipeline:STT behind: (\d+) segments merged")),
    ("hearing", re.compile(r"sage:unsure: (tilted|quiet)$")),
    ("overheard", re.compile(r"sage:overheard: quiet$")),
    ("choice", re.compile(r"sage:chose: (\w+)")),
    ("kernel", re.compile(r"sage:turn start ")),
    ("silent", re.compile(r"sage:reply empty$")),
    ("floor", re.compile(r"sage:floor: holding")),
    ("emote", re.compile(r"(?:sage:(?:emote|gesture)|executor:recorded \w+): (\S+)")),
    ("doa", re.compile(r"executor:glance(: | skipped: no direction of arrival)")),
]


def _num(text: str) -> float | None:
    return None if text in ("-", "") else float(text)


def events(lines) -> list[tuple[float, str, object]]:
    """(epoch s, kind, value) from both of the log's formats, in time order."""
    out = list(tt.log_events(lines))
    for line in lines:
        if not (m := latency.LINE.match(line)):
            continue
        said = f"{m['name'].removeprefix('reachy_sage_voice.')}:{m['message']}"
        for kind, pattern in _SAGE:
            if hit := pattern.search(said):
                wall = m["wall"].replace(",", ".")  # local, as the log writes it
                t = datetime.strptime(wall, "%Y-%m-%d %H:%M:%S.%f").timestamp()  # noqa: DTZ007
                out.append((t, kind, hit.groups()))
                break
    return sorted(out, key=lambda e: e[0])


def _features(path: str) -> dict:
    """Level, floor, SNR, stereo balance, flatness and Silero stats of one recording."""
    import wave

    with wave.open(path) as w:
        raw = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
        raw = raw.reshape(-1, w.getnchannels())

    def db(x):
        return round(20 * math.log10(max(float(x), 1e-9)), 1)

    mono = raw.mean(axis=1)
    frames = mono[: len(mono) // FRAME * FRAME].reshape(-1, FRAME)
    rms = np.sqrt((frames**2).mean(axis=1)) if len(frames) else np.zeros(1)
    out = {
        "rms_db": db(np.sqrt((mono**2).mean())) if len(mono) else None,
        "peak_db": db(np.abs(mono).max()) if len(mono) else None,
        "floor_db": db(np.percentile(rms, 10)),
        "snr_db": round(db(np.percentile(rms, 95)) - db(np.percentile(rms, 10)), 1),
    }
    if raw.shape[1] == 2 and len(raw):
        left, right = raw[:, 0], raw[:, 1]
        out["lr_db"] = round(db(np.sqrt((left**2).mean())) - db(np.sqrt((right**2).mean())), 1)
        out["lr_corr"] = (
            round(float(np.corrcoef(left, right)[0, 1]), 3) if left.std() and right.std() else None
        )
    loud = frames[rms >= np.percentile(rms, 50)] if len(frames) else frames
    if len(loud):  # spectral flatness of the louder half: a tone or a voice is peaky, hiss is flat
        power = np.abs(np.fft.rfft(loud, axis=1)) ** 2 + 1e-12
        out["flatness"] = round(
            float((np.exp(np.log(power).mean(axis=1)) / power.mean(axis=1)).mean()), 4
        )
    out.update(_vad(load_pcm16(path)))
    return out


_silero = None


def _vad(pcm: np.ndarray) -> dict:
    """Return Silero's speech probability over a segment: mean, max, share at 0.5 or over."""
    global _silero
    if _silero is None:
        from pipecat.audio.vad.silero import SileroVADAnalyzer

        _silero = SileroVADAnalyzer()
        _silero.set_sample_rate(
            SAMPLE_RATE
        )  # as turn_end does: the constructor's rate is only a default
    if hasattr(_silero._model, "reset_states"):
        _silero._model.reset_states()
    probs = [
        float(np.ravel(_silero.voice_confidence(pcm[i : i + FRAME].tobytes()))[0])
        for i in range(0, len(pcm) - FRAME + 1, FRAME)
    ]
    if not probs:
        return {"vad_mean": None, "vad_max": None, "speech_ratio": None}
    return {
        "vad_mean": round(float(np.mean(probs)), 3),
        "vad_max": round(float(np.max(probs)), 3),
        "speech_ratio": round(sum(p >= 0.5 for p in probs) / len(probs), 3),
    }


def _blank(seg: dict | None) -> dict:
    keys = (
        "rms_db",
        "peak_db",
        "floor_db",
        "snr_db",
        "lr_db",
        "lr_corr",
        "flatness",
        "vad_mean",
        "vad_max",
        "speech_ratio",
    )
    return {
        "id": Path(seg["path"]).stem if seg else None,
        "session": Path(seg["path"]).parent.name if seg else None,
        "wav": seg["path"] if seg else None,
        "start": seg["start"] if seg else None,
        "end": seg["end"] if seg else None,
        "duration_s": round(seg["end"] - seg["start"], 3) if seg else None,
        **dict.fromkeys(keys),
    }


def records(lines, sessions, audio: bool = True) -> list[dict]:
    """Return a record per VAD segment (its WAV if recorded), or per lone transcript."""
    evs = events(lines)
    segs = tt.segments(
        sorted(str(p) for folder in sessions for p in Path(folder).glob("utt-*.wav"))
    )
    used: set[str] = set()

    def wav_for(stop: float) -> dict | None:
        near = [s for s in segs if abs(s["end"] - stop) <= JOIN_S and s["path"] not in used]
        return min(near, key=lambda s: abs(s["end"] - stop)) if near else None

    rows, pending, merge, started = [], [], 1, None
    for t, kind, value in evs:
        if kind == "user_started":
            started = t
        elif kind == "user_stopped":
            seg = wav_for(t)
            if seg:
                used.add(seg["path"])
            row = _blank(seg)
            row.update(start=row["start"] or started, end=row["end"] or t)
            pending.append((t, row))
            started = None
        elif kind == "merged":
            merge = int(value[0])
        elif kind == "transcript":
            rows.extend(
                r for s, r in pending if t - s > STALE_S
            )  # never transcribed: kept, text None
            pending = [(s, r) for s, r in pending if t - s <= STALE_S]
            take, pending = pending[:merge], pending[merge:]
            if not take:
                row = _blank(None)
                row.update(start=t, end=t)
                take = [(t, row)]
            for _, r in take[:-1]:
                r.update(text=None, merged=True, at=t)
            take[-1][1].update(text=value, merged=False, at=t)
            rows.extend(r for _, r in take)
            merge = 1
    rows.extend(r for _, r in pending)  # stops whose transcript never came
    for seg in segs:  # recordings the log never saw stop (a log from another session, a cut log)
        if seg["path"] not in used:
            rows.append(_blank(seg))
    rows.sort(key=lambda r: r["start"] or 0)
    _decide(rows, evs)
    if audio:
        for row in rows:
            if row["wav"]:
                row.update(_features(row["wav"]))
    return rows


def _decide(rows: list[dict], evs) -> None:
    """Words, context and what Sage did, from the events after each transcript until the next."""
    said = [(t, v) for t, k, v in evs if k == "tts"]
    played = tt.responses(
        [(t, k, v) for t, k, v in evs if k in ("bot_started", "bot_stopped", "interruption")]
    )
    marks = sorted(r["at"] for r in rows if r.get("at") is not None)
    for row in rows:
        text, at = row.get("text"), row.get("at")
        row.update(
            text=text,
            empty=text == "" if text is not None else None,
            words=len(re.findall(r"[\w']+", text)) if text else 0 if text == "" else None,
            question=text.rstrip().endswith("?") if text else False if text == "" else None,
            conf_mean=None,
            conf_min=None,
            word_conf=None,
            similarity=None,
            hearing=None,
            overheard=False,
            choice=None,
            kernel=False,
            reply=None,
            silent=False,
            floor_held=False,
            emote=None,
            doa=None,
            face=None,
        )
        start, end = row["start"], row["end"]
        before = [p for p in played if p["start"] < end]
        row["sage_speaking"] = any(p["end"] > start for p in before) if start is not None else None
        ended = [p["end"] for p in before if p["end"] <= start] if start is not None else []
        row["since_sage_spoke_s"] = (
            0.0 if row["sage_speaking"] else round(start - max(ended), 3) if ended else None
        )
        last = [v for t, v in said if start is not None and t < start]
        row["sage_asked"] = bool(last) and last[-1].rstrip().endswith("?")
        if at is None:
            continue
        until = min([m for m in marks if m > at] + [at + STALE_S])
        reply = []
        for t, kind, value in evs:
            if not at - 0.05 <= t < until:
                continue
            if kind == "stt" and row["conf_mean"] is None and row["similarity"] is None:
                row["conf_mean"], row["conf_min"], row["similarity"] = (
                    _num(value[0]),
                    _num(value[1]),
                    _num(value[2]),
                )
                row["word_conf"] = [_num(p) for p in value[3].split()] or None
            elif kind == "hearing" and not row["hearing"]:
                row["hearing"] = value[0]
            elif kind == "overheard":
                row["overheard"] = True
            elif kind == "choice" and not row["choice"]:
                row["choice"] = value[0]
            elif kind in ("kernel", "silent", "floor"):
                row[{"floor": "floor_held"}.get(kind, kind)] = True
            elif kind == "emote" and not row["emote"]:
                row["emote"] = value[0]
            elif kind == "doa" and row["doa"] is None:
                row["doa"] = value[0] == ": "  # a glance needs a direction; "no direction" is False
            elif kind == "tts" and value:
                reply.append(value)
        row["reply"] = " ".join(reply) or None


def answered(row: dict) -> bool:
    """Sage did something for this line: words, a kernel turn or a move."""
    return bool(
        row.get("reply") or row.get("kernel") or row.get("emote") or row.get("choice") == "move"
    )


def _labels(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    return {d["id"]: d for d in map(json.loads, path.read_text().splitlines()) if d.get("id")}


def review(
    rows, labels: Path, keys=None, play=None, out=sys.stdout, everything: bool = False
) -> int:
    """Ask the owner about each replied-to line not yet labelled; append answers; return a count."""
    keys = keys if keys is not None else iter(lambda: input("> ").strip().lower(), None)
    play = play or (lambda wav: subprocess.run(["afplay", wav], check=False))
    done, count = _labels(labels), 0
    todo = [
        r
        for r in rows
        if r["id"] and r["id"] not in done and (everything or answered(r)) and r.get("text")
    ]
    for i, row in enumerate(todo, 1):
        print(
            f"\n[{i}/{len(todo)}] {row['id']}  heard: {row['text']!r}  reply: {row['reply']!r}",
            file=out,
        )
        print(
            "  "
            + ", ".join(
                f"{k} {row[k]}"
                for k in (
                    "duration_s",
                    "snr_db",
                    "vad_mean",
                    "conf_mean",
                    "similarity",
                    "since_sage_spoke_s",
                    "sage_asked",
                    "hearing",
                    "choice",
                )
                if row.get(k) is not None
            ),
            file=out,
        )
        print("  y to me, n not to me, k noise, s unsure, p play, q quit", file=out)
        for key in keys:
            if key == "p" and row["wav"]:
                play(row["wav"])
            elif key == "q":
                return count
            elif key in LABELS:
                source = None
                if key == "n":
                    print(
                        "  source? t TV, m music, o another person, e self-talk, Enter skip",
                        file=out,
                    )
                    source = SOURCES.get(next(keys, ""))
                with labels.open("a") as f:
                    f.write(
                        json.dumps(
                            {
                                "id": row["id"],
                                "label": LABELS[key],
                                "source": source,
                                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            }
                        )
                        + "\n"
                    )
                count += 1
                break
        else:
            return count
    return count


def export_csv(rows, labels: Path, out: Path) -> None:
    """Every record, its label and source if any, as one CSV for analysis."""
    done = _labels(labels)
    fields = [*(rows[0] if rows else []), "label", "source"]
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            got = done.get(row["id"], {})
            w.writerow(
                {
                    **{k: json.dumps(v) if isinstance(v, list) else v for k, v in row.items()},
                    "label": got.get("label", ""),
                    "source": got.get("source") or "",
                }
            )


def sessions_for(log_lines, root: Path) -> list[Path]:
    """Return the ``session-*`` folders under ``root`` with a recording in the log's time span."""
    times = [t for t, _, _ in tt.log_events(log_lines)]
    if not times or not root.is_dir():
        return []
    lo, hi = min(times) - 60, max(times) + 60
    return [
        d
        for d in sorted(root.glob("session-*"))
        if any(lo <= tt.stop_time(str(p)) <= hi for p in d.glob("utt-*.wav"))
    ]


def _where(log: str, root: Path) -> tuple[list[dict], Path]:
    lines = Path(log).read_text(errors="replace").splitlines()
    folders = sessions_for(lines, root)
    rows = records(lines, folders)
    home = folders[-1] if folders else root / f"trace-{Path(log).stem}"
    home.mkdir(parents=True, exist_ok=True)
    return rows, home


def write_trace(rows, path: Path) -> None:
    """Write the records as JSON lines."""
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def main(argv=None) -> None:
    """Command line: trace, review, csv, backfill (module docstring)."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("command", choices=("trace", "review", "csv", "backfill"))
    p.add_argument("log", nargs="?")
    p.add_argument("--recordings", default=os.path.expanduser("~/sage-kernel/.deploy/recordings"))
    p.add_argument("--logs", default=os.path.expanduser("~/Library/Logs/reachy-sage/bot-*.log"))
    p.add_argument("--out")
    p.add_argument(
        "--all", action="store_true", help="review every heard line, not only answered ones"
    )
    a = p.parse_args(argv)
    root = Path(a.recordings)
    if a.command == "backfill":
        for log in sorted(glob.glob(a.logs)):
            rows, home = _where(log, root)
            write_trace(rows, home / f"trace-{Path(log).stem}.jsonl")
            print(
                f"{Path(log).name}: {len(rows)} records, "
                f"{sum(bool(r['wav']) for r in rows)} with audio, "
                f"{sum(answered(r) for r in rows)} answered -> {home}"
            )
        return
    if not a.log:
        p.error("give the bot's log")
    rows, home = _where(a.log, root)
    trace = home / f"trace-{Path(a.log).stem}.jsonl"
    write_trace(rows, trace)
    labels = home / "labels.jsonl"
    if a.command == "trace":
        print(f"{len(rows)} records -> {trace}")
    elif a.command == "review":
        print(f"{review(rows, labels, everything=a.all)} labelled -> {labels}")
    else:
        out = Path(a.out or home / f"trace-{Path(a.log).stem}.csv")
        export_csv(rows, labels, out)
        print(f"{len(rows)} rows -> {out}")


if __name__ == "__main__":
    main()
