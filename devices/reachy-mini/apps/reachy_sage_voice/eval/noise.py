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

The gate's labelled set ("is this for me?"), in ``<recordings>/gate``:

    uv run python eval/noise.py sample [--logs GLOB] [--since DAY] [--n 400] [--seed 0]   once
    uv run python eval/noise.py label                    label them; q stops, the next run resumes
    uv run python eval/noise.py baseline            how the old gates and ``gate.attend`` did, a day
    uv run python eval/noise.py replay [--logs GLOB] [--since DAY] [--low 0.37]
        ``gate.attend`` over every logged line, no labels needed: counts of what it would have done
        against what the old gates did, and how long its conversations stay open.
    uv run python eval/score.py freeze <recordings>/gate --session <day>      hold a day out

``--recordings`` is the folder of ``session-*`` folders: every session whose recordings fall in the
log's time span is joined. Labels: y to me, n not to me (then t TV, m music, o another person,
e self-talk, Enter skip), k noise, s unsure, p play, q quit.
"""

import argparse
import csv
import glob
import itertools
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import turn_taking as tt
from sage_device import gate, latency, media

from reachy_sage_voice.grammar import is_name
from reachy_sage_voice.meter import HEARD
from reachy_sage_voice.turn_end import SAMPLE_RATE, load_pcm16

#: A chain is lines each within this of the one before: the old gates' open window, kept as the
#: sampler's strata were cut with it.
OPEN_S = 30.0
#: Replay only: how long after an answered line Sage's last word falls (first audio a median 3.0 s,
#: then a reply of at most 60 characters). ponytail: a guess; the old logs' own speech times answer
#: lines the new gate would not, so they cannot be used.
REPLY_S = 5.0
ASKED_S = 20.0  # as sage.ASKED_S
FACE_S = 3.0  # as pipeline.FACE_S
JOIN_S = 0.3  # a recording's name is its VAD stop; the log's stop line lands within this
STALE_S = 15.0  # a VAD stop with no transcript this long after it never got one
FRAME = 512  # 32 ms at 16 kHz, Silero's frame
LABELS = {"y": "to_me", "n": "not_to_me", "k": "noise", "s": "unsure"}
SOURCES = {"t": "tv", "m": "music", "o": "other_person", "e": "self_talk"}
_SAGE = [  # (kind, pattern on "<logger>:<message>") for the voice's own lines
    (
        "stt",
        re.compile(
            r"pipeline:stt: confidence mean (\S+) min (\S+), similarity (\S+), words ?([^,]*)"
            r"(?:, face (\S+), doa (\S+))?$"  # the suffix is newer than four days of logs
        ),
    ),
    ("merged", re.compile(r"pipeline:STT behind: (\d+) segments merged")),
    (
        "gate",
        re.compile(r"sage:gate: (\w+) \(([^)]*)\)$"),
    ),  # from step 1 on; the lines below, before
    ("hearing", re.compile(r"sage:unsure: (tilted|quiet)$")),
    ("overheard", re.compile(r"sage:overheard: quiet$")),
    ("choice", re.compile(r"sage:chose: (\w+)")),
    ("kernel", re.compile(r"sage:turn start ")),
    ("silent", re.compile(r"sage:reply empty$")),
    ("floor", re.compile(r"sage:floor: holding")),
    ("emote", re.compile(r"(?:sage:(?:emote|gesture)|executor:recorded \w+): (\S+)")),
    ("doa", re.compile(r"executor:glance(: | skipped: no direction of arrival)")),
    ("heard", re.compile(HEARD)),  # groups: DoA, speech_detected, head, voice, face, its x
]


def _num(text: str) -> float | None:
    return None if text in ("-", "", "none", None) else float(text)


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
    heard = [(t, v) for t, k, v in evs if k == "heard"]
    starts = sorted(r["start"] for r in rows if r["start"] is not None)
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
            face_age_s=None,  # the stt line's own: seconds since a face was seen, and its direction
            stt_doa_deg=None,
            gate=None,  # the gate line's result, and its check or rule
            gate_check=None,
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
            doa_deg=None,
            speech_detected=None,
            head_deg=None,
            voice_deg=None,
            face_x=None,
        )
        start, end = row["start"], row["end"]
        # The onset's "heard:" line: the first from this segment's start to the next one's.
        later = min([s for s in starts if start is not None and s > start] + [math.inf])
        for t, (doa, speech, head, voice, face, x) in heard:
            if start is not None and start - JOIN_S <= t < min(later - JOIN_S, start + STALE_S):
                row.update(
                    doa_deg=_num(doa),
                    speech_detected={"True": True, "False": False}.get(speech),
                    head_deg=_num(head),
                    voice_deg=_num(voice),
                    face=None if face == "-" else face != "not seen",
                    face_x=_num(x or "-"),
                )
                break
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
                row["face_age_s"], row["stt_doa_deg"] = _num(value[4]), _num(value[5])
            elif kind == "gate":  # the last one: a held fragment's outcome is the next gate line's
                row["gate"], row["gate_check"] = value
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
        for before in row.get("context") or ():  # the gate set's lines carry what came before
            print(
                f"  {before['ago_s']} s before: {before['text']!r} -> {before['outcome']}"
                + (f", reply {before['reply']!r}" if before["reply"] else ""),
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


#: The gate line's results (sage._gate) as outcomes: ``gate.attend``'s four, and the old checks'.
_GATED = {
    "turn": "turn",
    "answer": "turn",
    "move": "move",
    "doubted": "doubted",
    "ack": "ack",
    "listen": "listen",
    "overheard": "overheard",
}


def outcome(row: dict) -> str:
    """Return what the gate did with a transcript, from a log old or new.

    Old checks: overheard, doubted, move, turn. ``gate.attend``: turn (its ANSWER), ack, listen,
    overheard. Other is a line none of them judged: a command, a repeat, an echo, a kernel down.
    """
    if row.get("gate") in _GATED:
        return _GATED[row["gate"]]
    if row.get("overheard"):
        return "overheard"
    if row.get("hearing"):
        return "doubted"
    if row.get("choice") == "move":
        return "move"
    return "turn" if row.get("kernel") or row.get("choice") else "other"


def replay(lines: list[dict], low: float = gate.LOW_CONFIDENCE) -> list[dict]:
    """Return what ``gate.attend`` would do with each of one log's gate lines, in time order.

    Each: id, outcome, reason, and ``conversation`` (a count, None when none is open after it).
    From what an old log carries: the words, the time, the STT's confidence and voiceprint score,
    and the face's age where logged. No direction. Sage is taken to speak ``REPLY_S`` after every
    line answered, and to have asked a question only when the line before was answered.
    """
    state, watch, out = gate.Attention(), media.Watch(), []
    spoke_at, answered, conversation = -math.inf, False, 0
    for row in sorted(lines, key=lambda r: r["at"]):
        at, was_on = row["at"], watch.on
        watch, whose = media.heard(watch, at, row["text"], is_name)
        if watch.on and not was_on:
            state = gate.Attention()  # as sage._watched: a broadcast starting closes it
        since = row.get("since_sage_spoke_s")
        was_open = gate.is_open(state, at, spoke_at)
        did, reason, state = gate.attend(
            row["text"],
            gate.Signals(
                at,
                spoke_at=spoke_at,
                low=low,
                named=is_name,
                confidence=row.get("conf_mean"),
                asked=bool(
                    answered and row.get("sage_asked") and since is not None and since < ASKED_S
                ),
                face=None if row.get("face_age_s") is None else row["face_age_s"] <= FACE_S,
                similarity=row.get("similarity"),
                commentary=whose == media.COMMENTARY,
            ),
            state,
        )
        answered = did == gate.ANSWER
        if answered:
            spoke_at = at + REPLY_S
            conversation += not was_open
        out.append(
            {
                "id": row["id"],
                "outcome": did,
                "reason": reason,
                "open": was_open,
                "conversation": conversation if state.open else None,
                "at": at,
            }
        )
    return out


def replay_report(logs: list[list[dict]], low: float = gate.LOW_CONFIDENCE) -> str:
    """Return counts, no words: old outcome by new, why, the conversations, and the confidence."""
    cross, why, runs, confidence, total = {}, {}, [], [], 0
    for lines in logs:
        did = {r["id"]: r for r in replay(lines, low)}
        open_run: dict[int, list[float]] = {}
        for row in lines:
            new = did[row["id"]]
            total += 1
            cross[row["outcome"], new["outcome"]] = (
                cross.get((row["outcome"], new["outcome"]), 0) + 1
            )
            rule = new["reason"].split(",")[0], new["outcome"], new["reason"].partition(", ")[2]
            why[rule] = why.get(rule, 0) + 1
            if row.get("conf_mean") is not None:
                confidence.append(row["conf_mean"])
            if new["conversation"] is not None:
                open_run.setdefault(new["conversation"], []).append(new["at"])
        runs += [(len(ats), max(ats) - min(ats)) for ats in open_run.values()]
    news = (gate.ANSWER, gate.ACK, gate.LISTEN, gate.OVERHEARD)
    olds = sorted({old for old, _ in cross})
    out = [
        f"{total} heard lines in {len(logs)} logs",
        "",
        "old gates \\ gate.attend  " + "  ".join(f"{n:>9}" for n in (*news, "all")),
    ]
    for old in olds:
        cells = [cross.get((old, n), 0) for n in news]
        out.append(f"{old:<24}" + "  ".join(f"{c:>9}" for c in (*cells, sum(cells))))
    sums = [sum(cross.get((old, n), 0) for old in olds) for n in news]
    out.append(f"{'all':<24}" + "  ".join(f"{c:>9}" for c in (*sums, sum(sums))))
    out += ["", "rule, outcome, step down: lines"]
    out += [
        f"  {rule:<10} {did:<9} {step or '-':<15} {n}"
        for (rule, did, step), n in sorted(why.items(), key=lambda x: -x[1])
    ]
    if runs:
        most, longest = max(n for n, _ in runs), max(s for _, s in runs)
        out += [
            "",
            f"{len(runs)} conversations; lines heard in one while open: median "
            f"{statistics.median(n for n, _ in runs):.0f}, most {most}; longest {longest:.0f} s "
            f"first line to last; {sum(n >= 20 for n, _ in runs)} of 20 lines or more",
        ]
    if confidence:
        cuts = statistics.quantiles(confidence, n=20)
        out += [
            "",
            f"confidence, {len(confidence)} lines: p10 {cuts[1]:.2f}, p25 {cuts[4]:.2f}, "
            f"median {cuts[9]:.2f}, p75 {cuts[14]:.2f}; under {low}: "
            f"{sum(c < low for c in confidence)}, under 0.5: {sum(c < 0.5 for c in confidence)}",
        ]
    return "\n".join(out)


def chain_bucket(chain: int) -> str:
    """Return the sampler's bucket for a line's place in its chain. The edges are guesses."""
    return "1" if chain < 2 else "2-5" if chain < 6 else "6-20" if chain < 21 else "21+"


def gate_lines(rows) -> list[dict]:
    """Return the transcripts with words, each with an id, day, outcome, chain and context.

    ``chain`` counts the unbroken lines up to this one: each within the open window
    (``OPEN_S``, the old gates' window) of the one before. Rows of one log: no chain crosses logs.
    """
    out, chain, last = [], 0, -math.inf
    for row in sorted((r for r in rows if r.get("text")), key=lambda r: r["at"]):
        at = row["at"]
        chain, last = (chain + 1 if at - last < OPEN_S else 1), at
        when = datetime.fromtimestamp(at)  # noqa: DTZ006  local, as the log writes it
        out.append(
            {
                **row,
                "id": row["id"] or f"at-{when:%Y%m%d-%H%M%S}-{int(at % 1 * 1000):03d}",
                "day": f"{when:%Y-%m-%d}",
                "outcome": outcome(row),
                "chain": chain,
                "named": any(map(is_name, re.findall(r"[\w']+", row["text"].lower()))),
                "context": [
                    {
                        "text": b["text"],
                        "outcome": b["outcome"],
                        "reply": b["reply"],
                        "ago_s": round(at - b["at"]),
                    }
                    for b in out[-3:]
                ],
            }
        )
    return out


def sample(lines, n: int = 400, seed: int = 0) -> list[dict]:
    """Pick ``n`` gate lines to label: every name line, then the strata in turn; in time order.

    A stratum is a day, an outcome and a chain bucket. Taking from each in turn fills the rare
    ones (overheard, long chains) first, so each picked line carries ``weight``: how many heard
    lines it stands for. The same lines for the same ``seed`` and input.
    """
    rng, strata, picked = random.Random(seed), {}, []
    for row in lines:
        if row["named"]:
            picked.append({**row, "weight": 1.0})
        else:
            strata.setdefault((row["day"], row["outcome"], chain_bucket(row["chain"])), []).append(
                row
            )
    for key in sorted(strata):
        rng.shuffle(strata[key])
    take, left = dict.fromkeys(strata, 0), n - len(picked)
    while left > 0 and any(take[k] < len(strata[k]) for k in strata):
        for key in sorted(strata):
            if left > 0 and take[key] < len(strata[key]):
                take[key] += 1
                left -= 1
    for key, rows in strata.items():
        picked += [{**r, "weight": len(rows) / take[key]} for r in rows[: take[key]]]
    return sorted(picked, key=lambda r: r["at"])


def baseline(rows, labels: dict[str, dict], did: dict[str, str] | None = None) -> dict[str, dict]:
    """Per day, how a gate did on the labelled lines: the old checks, or ``gate.attend``.

    ``did``: each line's outcome from ``replay`` (None: what the log recorded, ``outcome``).
    Missed: a to-me line doubted, listened to or overheard (``missed_named``: one with its name).
    Wrong reply: a not-to-me line (TV, music, another person, self-talk, noise) given a turn.
    A look (ack) is neither: counted apart, for to-me and not. Unsure lines are counted and left
    out. The percentages weigh each line by the heard lines it stands for (``sample``).
    ponytail: no interval on the weighted rates; bootstrap over strata if a decision hangs on one.
    """
    days: dict[str, dict] = {}
    for row in rows:
        d = days.setdefault(
            row["day"],
            dict.fromkeys(
                (
                    "to_me",
                    "missed",
                    "moved",
                    "missed_named",
                    "ack_to_me",
                    "not_to_me",
                    "wrong_reply",
                    "ack_not",
                    "unsure",
                    "unlabelled",
                    "_to_me",
                    "_missed",
                    "_not",
                    "_wrong",
                ),
                0,
            ),
        )
        label, w = (labels.get(row["id"]) or {}).get("label"), row["weight"]
        act = row["outcome"] if did is None else did.get(row["id"], "other")
        if label is None:
            d["unlabelled"] += 1
        elif label == "unsure":
            d["unsure"] += 1
        elif label == "to_me":
            missed = act in ("doubted", "listen", "overheard")
            d["to_me"], d["missed"], d["moved"] = (
                d["to_me"] + 1,
                d["missed"] + missed,
                d["moved"] + (act == "move"),
            )
            d["missed_named"] += missed and bool(row.get("named"))
            d["ack_to_me"] += act == "ack"
            d["_to_me"], d["_missed"] = d["_to_me"] + w, d["_missed"] + w * missed
        else:
            wrong = act in ("turn", "answer")
            d["not_to_me"], d["wrong_reply"] = d["not_to_me"] + 1, d["wrong_reply"] + wrong
            d["ack_not"] += act == "ack"
            d["_not"], d["_wrong"] = d["_not"] + w, d["_wrong"] + w * wrong
    for d in days.values():
        to_me, missed, not_to_me, wrong = (
            d.pop(k) for k in ("_to_me", "_missed", "_not", "_wrong")
        )
        d["missed_pct"] = round(100 * missed / to_me, 1) if to_me else None
        d["wrong_reply_pct"] = round(100 * wrong / not_to_me, 1) if not_to_me else None
    return dict(sorted(days.items()))


def baseline_table(days: dict[str, dict], frozen=()) -> str:
    """Return the baseline as a table: a row a day, the frozen ones marked."""
    keys = (
        "to_me",
        "missed",
        "missed_pct",
        "moved",
        "missed_named",
        "ack_to_me",
        "not_to_me",
        "wrong_reply",
        "wrong_reply_pct",
        "ack_not",
        "unsure",
        "unlabelled",
    )
    rows = [("day", *keys)] + [
        (
            day + (" (frozen)" if day in frozen else ""),
            *("-" if d[k] is None else str(d[k]) for k in keys),
        )
        for day, d in days.items()
    ]
    wide = [max(map(len, col)) for col in zip(*rows, strict=True)]
    return "\n".join(
        "  ".join(c.ljust(w) for c, w in zip(r, wide, strict=True)).rstrip() for r in rows
    )


def _frozen_days(frozen: Path, dataset: str) -> set[str]:
    """Return the sessions ``score.py freeze`` wrote for this dataset (its FROZEN.md table)."""
    if not frozen.exists():
        return set()
    cells = [[c.strip() for c in x.strip("|").split("|")] for x in frozen.read_text().splitlines()]
    return {day for c in cells if len(c) == 5 and c[1] == dataset for day in c[2].split(",")}


def _gate(a, p) -> None:
    """Run sample, label or baseline on the gate set in ``<recordings>/gate``."""
    home = Path(a.recordings) / "gate"
    picked, labels = home / "sample.jsonl", home / "labels.jsonl"
    if a.command == "sample" and not picked.exists():
        root, lines = Path(a.recordings), []
        for log in sorted(glob.glob(a.logs)):
            text = Path(log).read_text(errors="replace").splitlines()
            lines += gate_lines(records(text, sessions_for(text, root), audio=False))
        # A line in two logs is one line. Only lines with the stt line: the gate's signals.
        lines = [
            r
            for r in {r["id"]: r for r in lines}.values()
            if r["conf_mean"] is not None and r["day"] >= a.since
        ]
        home.mkdir(parents=True, exist_ok=True)
        write_trace(sample(lines, a.n, a.seed), picked)
        print(f"{len(lines)} heard lines in {len({r['day'] for r in lines})} days")
    elif a.command == "sample":
        print(
            f"kept {picked}: its labels point at these lines. Move the folder away to sample anew."
        )
    if not picked.exists():
        p.error("no sample yet: run eval/noise.py sample")
    rows = [json.loads(x) for x in picked.read_text().splitlines()]
    if a.command == "sample":
        count: dict[tuple, int] = {}
        for r in rows:
            count[r["day"], r["outcome"]] = count.get((r["day"], r["outcome"]), 0) + 1
        for (day, did), k in sorted(count.items()):
            print(f"  {day} {did:<9} {k}")
        print(
            f"{len(rows)} lines to label, {sum(r['named'] for r in rows)} with the name -> {picked}"
        )
    elif a.command == "label":
        print(f"{review(rows, labels, everything=True)} labelled -> {labels}")
    else:
        done = _labels(labels)
        write_trace(
            [
                {
                    **r,
                    "session": r["day"],
                    "label": done[r["id"]]["label"],
                    "source": done[r["id"]].get("source"),
                }
                for r in rows
                if r["id"] in done
            ],
            home / "manifest.jsonl",
        )  # what score.py freeze hashes: the lines and their labels
        frozen = _frozen_days(Path(a.frozen), home.name)
        print("the old gates, as logged:")
        print(baseline_table(baseline(rows, done), frozen))
        # gate.attend is stateful: every line of every log is replayed, the labelled ones scored.
        did, runs = {}, {}
        for n, log in enumerate(sorted(glob.glob(a.logs))):
            text = Path(log).read_text(errors="replace").splitlines()
            lines = gate_lines(records(text, sessions_for(text, Path(a.recordings)), audio=False))
            for d in replay(lines, a.low):
                did[d["id"]] = d["outcome"]
                wrong = (done.get(d["id"]) or {}).get("label") in ("not_to_me", "noise")
                if d["conversation"] is not None and wrong and d["outcome"] == gate.ANSWER:
                    runs[n, d["conversation"]] = runs.get((n, d["conversation"]), 0) + 1
        print(f"\ngate.attend, replayed (confidence floor {a.low}):")
        print(baseline_table(baseline(rows, done, did), frozen))
        print(
            f"{sum(r['id'] not in did for r in rows)} sampled lines not found in --logs; most "
            f"not-to-me lines answered in one conversation: {max(runs.values(), default=0)}"
        )
        print(
            "pass bar (frozen day): missed_pct <= 5, missed_named 0, wrong_reply_pct <= 15, "
            "and under 5 not-to-me lines answered in any one conversation"
        )
        at = sorted(datetime.fromisoformat(d["at"]) for d in done.values() if d.get("at"))
        gaps = [(b - c).total_seconds() for c, b in itertools.pairwise(at)]
        pace = f", {statistics.median(gaps):.0f} s a label (median gap)" if gaps else ""
        print(f"{sum(r['id'] in done for r in rows)} of {len(rows)} labelled{pace}")


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
    """Command line: trace, review, csv, backfill, and the gate set (module docstring)."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "command",
        choices=("trace", "review", "csv", "backfill", "sample", "label", "baseline", "replay"),
    )
    p.add_argument("log", nargs="?")
    p.add_argument("--recordings", default=os.path.expanduser("~/sage-kernel/.deploy/recordings"))
    p.add_argument("--logs", default=os.path.expanduser("~/Library/Logs/reachy-sage/bot-*.log"))
    p.add_argument("--out")
    p.add_argument(
        "--all", action="store_true", help="review every heard line, not only answered ones"
    )
    p.add_argument("--n", type=int, default=400, help="sample: how many lines to label")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--since", default="", help="sample: only lines from this day (2026-11-16) on")
    p.add_argument("--low", type=float, default=gate.LOW_CONFIDENCE, help="the confidence floor")
    p.add_argument("--frozen", default=str(Path(__file__).resolve().parent / "FROZEN.md"))
    a = p.parse_args(argv)
    root = Path(a.recordings)
    if a.command in ("sample", "label", "baseline"):
        return _gate(a, p)
    if a.command == "replay":
        logs = []
        for log in sorted(glob.glob(a.logs)):
            text = Path(log).read_text(errors="replace").splitlines()
            lines = gate_lines(records(text, sessions_for(text, root), audio=False))
            logs.append([r for r in lines if r["conf_mean"] is not None and r["day"] >= a.since])
        print(replay_report([lines for lines in logs if lines], a.low))
        return
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
