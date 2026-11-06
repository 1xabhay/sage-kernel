"""Turn-taking on the owner's live conversation: premature turn ends, barge-ins, and replay.

The bot's recorder saves each VAD segment (``SAGE_VOICE_RECORD_DIR``), named by the moment
the VAD called its stop, with up to 1 s of pre-roll. From those names and durations this
rebuilds the conversation, labels every pause between segments as a continuation of the
owner's turn or a real turn end (from the transcripts and from the bot's log: did the owner
go on before, or just after, the bot started its reply), and replays the rebuilt audio
through each end-of-turn decider. The audio and transcripts are the owner's: they are read
in place, and only metrics leave this script.

    uv run python eval/turn_taking.py <recordings dir>
        [--logs '~/Library/Logs/reachy-sage/bot-*.log']
        [--stt-url http://127.0.0.1:8781] [--cache transcripts.json] [--until 20261023-022000]
        [--option NAME]

Transcripts come from the log where it covers a segment, else from the STT (one request at
a time), cached in ``--cache`` (keep it outside the repo).
"""

import argparse
import datetime
import glob
import hashlib
import itertools
import json
import os
import re
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import score
from sage_device.hearing import unfinished

from reachy_sage_voice.turn_end import SAMPLE_RATE, load_pcm16

#: Silence ends tried, ms; 450 was the live default when the recordings were made.
SILENCE_MS = (450, 600, 800, 1000, 1200)
#: Smart Turn timeouts tried, ms, after its 200 ms VAD stop.
SMART_TIMEOUTS_MS = (1000, 1500)
SMART_VAD_STOP_MS = 200
#: Semantic holds tried: (silence end ms, hold ms while the transcript looks unfinished).
HOLDS_MS = ((450, 1500), (450, 2000), (600, 1500), (600, 2000), (800, 2000))
#: A pause this long ends any turn under every decider: the conversation is cut into runs there.
RUN_GAP_S = 10.0
#: The owner speaking within this long of the bot's first audio is going on with their turn.
BARGE_S = 2.0
#: The live VAD's start_secs: speech shorter than this never opens a segment.
MIN_SPEECH_S = 0.2


# Timeline ------------------------------------------------------------------------------------


def stop_time(path: str) -> float:
    """Return when the VAD stopped this segment (epoch s), from ``utt-YYYYmmdd-HHMMSS-<ns>.wav``."""
    day, clock, ns = Path(path).stem.removeprefix("utt-").split("-")
    return _local(day + clock, "%Y%m%d%H%M%S") + int(ns) / 1e9


def _local(text: str, fmt: str) -> float:
    """Return epoch seconds of a local wall-clock time, as the recorder and the log write them."""
    return datetime.datetime.strptime(text, fmt).timestamp()  # noqa: DTZ007 local, as written


def segments(paths: list[str]) -> list[dict]:
    """Each recording as {path, start, end} in epoch seconds, in time order."""
    found = []
    for path in paths:
        with wave.open(path) as w:
            duration = w.getnframes() / w.getframerate()
        end = stop_time(path)
        found.append({"path": path, "start": end - duration, "end": end})
    return sorted(found, key=lambda s: s["end"])


def runs(segs: list[dict], gap_s: float = RUN_GAP_S) -> list[list[dict]]:
    """Consecutive segments split where the gap between them reaches ``gap_s``."""
    out: list[list[dict]] = []
    for seg in segs:
        if out and seg["start"] - out[-1][-1]["end"] < gap_s:
            out[-1].append(seg)
        else:
            out.append([seg])
    return out


def window(segs: list[dict], t_from: float, t_to: float, load=load_pcm16) -> np.ndarray:
    """Return the audio from ``t_from`` to ``t_to`` (epoch s): its segments, silence between."""
    out = np.zeros(max(0, round((t_to - t_from) * SAMPLE_RATE)), np.int16)
    for seg in segs:
        if seg["end"] <= t_from or seg["start"] >= t_to:
            continue
        pcm, at = load(seg["path"]), round((seg["start"] - t_from) * SAMPLE_RATE)
        lo, hi = max(0, at), min(len(out), at + len(pcm))
        out[lo:hi] = pcm[lo - at:hi - at]
    return out


# The bot's log -------------------------------------------------------------------------------

_LOG = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) \|.*? - (.*)$")
_KINDS = (("User started speaking", "user_started"), ("User stopped speaking", "user_stopped"),
          ("Transcription: [", "transcript"), ("empty transcription", "transcript"),
          ("_bot_started_speaking", "bot_started"), ("Bot started speaking", "bot_started"),
          ("Bot stopped speaking based", None), ("Bot stopped speaking", "bot_stopped"),
          ("InterruptionWorkerFrame", "interruption"), ("Generating TTS", "tts"))


def log_events(lines) -> list[tuple[float, str, str]]:
    """(epoch s, kind, text) from the bot's log lines; kinds as in ``_KINDS``."""
    events = []
    for line in lines:
        m = _LOG.match(line)
        if not m:
            continue
        for needle, kind in _KINDS:
            if needle in line:
                if kind:
                    bracketed = kind in ("transcript", "tts") and "[" in line
                    text = line.split("[", 1)[1].rsplit("]", 1)[0] if bracketed else ""
                    events.append((_local(m[1], "%Y-%m-%d %H:%M:%S.%f"), kind, text))
                break
    return sorted(events)


def log_texts(segs: list[dict], events, tolerance_s: float = 0.25) -> dict[str, str]:
    """Return the transcript the bot got for each segment its log saw stop."""
    stops = [t for t, k, _ in events if k == "user_stopped"]
    texts = {}
    for seg in segs:
        near = [t for t in stops if abs(t - seg["end"]) <= tolerance_s]
        if not near:
            continue
        after = [x for t, k, x in events if k == "transcript" and t >= near[0]]
        if after:
            texts[seg["path"]] = after[0]
    return texts


def user_segments(events, stop_tail_s: float = 0.45) -> list[dict]:
    """Return the owner's VAD segments in the log: {start, end of speech, stop, text}."""
    out, start = [], None
    for t, kind, _ in events:
        if kind == "user_started":
            start = t
        elif kind == "user_stopped" and start is not None:
            text = next((x for u, k, x in events if k == "transcript" and u >= t), "")
            out.append({"start": start, "end": t - stop_tail_s, "stop": t, "text": text})
            start = None
    return out


def responses(events) -> list[dict]:
    """Return the bot's replies as played: {start, end, interrupted}, one per start/stop pair."""
    out, start, cut = [], None, False
    for t, kind, _ in events:
        if kind == "bot_started" and start is None:
            start, cut = t, False
        elif kind == "interruption" and start is not None:
            cut = True
        elif kind == "bot_stopped" and start is not None:
            out.append({"start": start, "end": t, "interrupted": cut})
            start = None
    return out


def log_report(events, barge_s: float = BARGE_S) -> dict:
    """Count replies begun over the owner, barge-ins and their cause, fragments joined.

    A transcript is joined (sage.py C7) when no reply audio and no interruption came since the
    one before; it replaces a reply still playing; else it follows a finished or cut reply.
    """
    segs, replies = user_segments(events), responses(events)
    over = [r for r in replies if any(s["start"] <= r["start"] < s["end"] for s in segs)]
    soon = [r for r in replies if r not in over
            and any(r["start"] < s["start"] <= r["start"] + barge_s for s in segs)]
    cuts = {"speech": 0, "empty_transcript": 0, "no_speech": 0}
    into = []
    for t in (t for t, k, _ in events if k == "interruption"):
        near = [s for s in segs if s["start"] - 0.5 <= t <= s["stop"]]
        cuts["no_speech" if not near else "empty_transcript" if not near[0]["text"].strip()
             else "speech"] += 1
        began = [r["start"] for r in replies if r["start"] <= t]
        if began:
            into.append(t - began[-1])
    joins = {"joined": 0, "replaced": 0, "after_interruption": 0, "after_reply": 0}
    seen, audio, cut, playing = False, False, False, False
    for _t, kind, text in events:
        if kind == "bot_started":
            audio = playing = True
        elif kind == "bot_stopped":
            playing = False
        elif kind == "interruption":
            cut = True
        elif kind == "transcript" and text.strip():
            if seen:
                joins["replaced" if playing and not cut else "after_interruption" if cut
                      else "after_reply" if audio else "joined"] += 1
            seen, audio, cut = True, False, False
    return {"segments": len(segs), "empty_segments": sum(not s["text"].strip() for s in segs),
            "replies": len(replies), "replies_interrupted": sum(r["interrupted"] for r in replies),
            "replies_begun_over_owner": len(over),
            "begun_over_owner_interrupted": sum(r["interrupted"] for r in over),
            "owner_resumed_within_2s": len(soon),
            "resumed_within_2s_interrupted": sum(r["interrupted"] for r in soon),
            "interruptions": sum(cuts.values()), "interruptions_by_cause": cuts,
            "interruption_s_into_reply_p50": float(np.percentile(into, 50)) if into else None,
            "interruptions_within_2s_of_floor": sum(x <= barge_s + 0.6 for x in into),
            "transcripts": joins}


def bot_starts(events) -> list[float]:
    """Return when each stretch of bot audio began."""
    return [t for t, k, _ in events if k == "bot_started"]


# Labels --------------------------------------------------------------------------------------


def confirmed(runs_s: list[tuple[float, float]], min_s: float = MIN_SPEECH_S) -> list:
    """Keep the speech runs the live VAD would have started on: at least its start_secs long."""
    return [(a, b) for a, b in runs_s if b - a >= min_s]


def label(prev_text: str, resumed_at: float, stop_at: float, starts: list[float] | None,
          latency_floor_s: float, speech_end: float, barge_s: float = BARGE_S) -> tuple[bool, str]:
    """Tell whether the owner goes on with their turn after a segment: (yes/no, the evidence).

    Text: the segment before looks unfinished (``turn_end.unfinished``). Behaviour, from the
    log: the owner went on before the bot's reply audio, or within ``barge_s`` of its start. Without
    a log (``starts`` None): they went on sooner than the bot ever replies (``latency_floor_s``
    after their speech ended).
    """
    if unfinished(prev_text):
        return True, "text"
    if starts is None:
        return resumed_at - speech_end < latency_floor_s, "gap"
    first = next((s for s in starts if s > stop_at), None)
    return first is None or resumed_at < first + barge_s, "log"


# Deciders ------------------------------------------------------------------------------------


def silence_splits(pause_ms: float, end_ms: float) -> bool:
    """Decide as the live VAD does: a pause of ``end_ms`` ends the turn."""
    return pause_ms >= end_ms


def hold_splits(pause_ms: float, end_ms: float, hold_ms: float, text: str) -> bool:
    """Decide by silence, but wait ``hold_ms`` in all while the transcript looks unfinished."""
    return pause_ms >= (hold_ms if unfinished(text) else end_ms)


def smart_splits(pause_ms: float, complete: bool, timeout_ms: float,
                 vad_stop_ms: float = SMART_VAD_STOP_MS) -> tuple[bool, float]:
    """Decide as Pipecat runs Smart Turn: (ends the turn, wait ms from the end of speech).

    At the 200 ms VAD stop the model ends the turn if it calls it complete; else the silence
    must reach the timeout.
    """
    wait = vad_stop_ms if complete else vad_stop_ms + timeout_ms
    return pause_ms >= wait, wait


def smart_turn_predict():
    """Return Smart Turn v3, the weights Pipecat bundles, on CPU: float32 16 kHz audio -> 0/1."""
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

    model = LocalSmartTurnAnalyzerV3(sample_rate=SAMPLE_RATE)
    # ponytail: Pipecat's private per-segment call (1.12, pinned); the public path needs a clock.
    return lambda audio: model._predict_endpoint(audio)["prediction"]


# Scoring -------------------------------------------------------------------------------------


def option_rows(items: list[dict], options: list[str], baseline: str, n_boot: int = 2000) -> list:
    """Per option: false splits of continuation pauses, and the wait at real turn ends.

    ``items`` are pauses {turn, cont, <option>: (splits, wait_ms)}; CIs resample turns (each
    real turn with the pauses inside it), paired against ``baseline``.
    """
    turns = sorted({i["turn"] for i in items})
    index = {t: k for k, t in enumerate(turns)}
    ends = [i for i in items if not i["cont"]]

    def per_turn(name: str) -> np.ndarray:
        out = np.zeros(len(turns))
        for i in items:
            if i["cont"]:
                out[index[i["turn"]]] += i[name][0] if name else 1
        return out
    count, base = per_turn(""), per_turn(baseline)
    rows = []
    for name in options:
        split = per_turn(name)
        waits = np.array([i[name][1] for i in ends])
        rows.append({
            "option": name, "cont_pauses": int(count.sum()), "false_splits": int(split.sum()),
            "false_split_rate": split.sum() / max(1, count.sum()),
            "rate_ci": score.bootstrap(len(turns), lambda ix, x=split: x[ix].sum()
                                       / max(1, count[ix].sum()), n_boot),
            "delta_vs_base": (split.sum() - base.sum()) / max(1, count.sum()),
            "delta_ci": score.bootstrap(len(turns), lambda ix, x=split: (
                x[ix].sum() - base[ix].sum()) / max(1, count[ix].sum()), n_boot),
            "turns_cut": int((split > 0).sum()), "real_ends": len(ends),
            "missed_ends": int(sum(not i[name][0] for i in ends)),
            "wait_p50_ms": float(np.percentile(waits, 50)) if len(waits) else None,
            "wait_p95_ms": float(np.percentile(waits, 95)) if len(waits) else None})
    return rows


#: Bands of continuation pauses reported, ms.
BANDS_MS = (450, 600, 800, 1000, 1200, 2000, 5000)


def pause_summary(items: list[dict]) -> dict:
    """Summarise pauses between segments inside a turn (count, percentiles, bands) and ends."""
    cont = np.array([i["ms"] for i in items if i["cont"] and i["boundary"]])
    ends = [i for i in items if not i["cont"]]
    edges = (0, *BANDS_MS, float("inf"))
    return {"continuations": len(cont), "real_ends": len(ends),
            "real_ends_followed": sum(i["ms"] != float("inf") for i in ends),
            "continuation_ms_p50_p90_p95": np.percentile(cont, [50, 90, 95]).round().tolist()
            if len(cont) else None,
            "continuation_bands_ms": {f"{lo:g}-{hi:g}": int(((cont >= lo) & (cont < hi)).sum())
                                      for lo, hi in itertools.pairwise(edges)},
            "unfinished_text_at_real_end": sum(i["unfinished"] for i in ends)}


def transcripts(segs, events, stt_url: str | None, cache: Path | None) -> dict[str, str]:
    """Return text per segment: the log's, else the cache's, else the STT's (cached)."""
    texts = json.loads(cache.read_text()) if cache and cache.exists() else {}
    texts.update(log_texts(segs, events))
    missing = [s["path"] for s in segs if s["path"] not in texts]
    if missing and stt_url:
        transcribe = score.stt_pcm(stt_url)
        for path in missing:
            texts[path] = transcribe(load_pcm16(path))
            time.sleep(0.2)  # the live bot shares this STT
    if cache:
        cache.write_text(json.dumps(texts))
    return texts


def analyse(segs, events, texts, predict, silero=score.silero_runs, load=load_pcm16) -> dict:
    """Label every pause in the owner's speech and put it to every decider; count the log.

    Segments with no words (breath, noise, the robot's own voice) are left out: they start no
    turn. A pause inside a segment is inside a turn; a pause between segments is labeled by
    ``label``; the silence after the last segment before a 10 s gap is a real turn end.
    Pauses come from the pipeline's Silero on each recording alone, kept to runs the live VAD
    would have started on. Smart Turn sees the turn so far (up to 8 s) at each pause.
    """
    logged = set(log_texts(segs, events))
    starts = bot_starts(events)
    replies = [r - s["end"] for s in user_segments(events) if s["text"].strip()
               for r in [next((b for b in starts if b > s["stop"]), None)]
               if r is not None and r - s["end"] < RUN_GAP_S]
    floor = float(np.percentile(replies, 5)) if replies else 1.5
    voiced = []
    for seg in segs:
        speech = confirmed(silero(load(seg["path"]), SAMPLE_RATE))
        if texts.get(seg["path"], "").strip() and speech:
            voiced.append({**seg, "speech": [(seg["start"] + a, seg["start"] + b)
                                             for a, b in speech]})
    items, evidence, turn = [], {"text": 0, "log": 0, "gap": 0}, 0
    for run in runs(voiced):
        turn_from = {t: run[0]["speech"][0][0] for t in SMART_TIMEOUTS_MS}
        for k, seg in enumerate(run):
            text = texts[seg["path"]]
            nxt = run[k + 1] if k + 1 < len(run) else None
            gaps = [(a[1], b[0], None) for a, b in itertools.pairwise(seg["speech"])]
            gaps.append((seg["speech"][-1][1], nxt["speech"][0][0] if nxt else float("inf"), k))
            for end, resume, boundary in gaps:
                ms = (resume - end) * 1000
                if ms < SMART_VAD_STOP_MS:
                    continue
                if boundary is None:
                    cont = True
                elif nxt is None:
                    cont = False
                else:
                    cont, why = label(text, resume, seg["end"],
                                      starts if seg["path"] in logged else None, floor, end)
                    evidence[why] += cont
                item = {"turn": turn, "cont": cont, "boundary": boundary is not None, "ms": ms,
                        "unfinished": boundary is not None and unfinished(text)}
                for x in SILENCE_MS:
                    item[f"silence{x}"] = (silence_splits(ms, x), x)
                for x, h in HOLDS_MS:
                    held = item["unfinished"]
                    item[f"hold{x}/{h}"] = (hold_splits(ms, x, h, text if held else "."),
                                            h if held else x)
                for t in SMART_TIMEOUTS_MS:
                    lo = max(turn_from[t] - 0.7, end + SMART_VAD_STOP_MS / 1000 - 8.0)
                    audio = window(run, lo, end + SMART_VAD_STOP_MS / 1000, load)
                    complete = predict(audio.astype(np.float32) / 32768.0) == 1
                    item[f"smart{t}"] = smart_splits(ms, complete, t)
                    if item[f"smart{t}"][0] and resume != float("inf"):
                        turn_from[t] = resume
                items.append(item)
                turn += not cont
    return {"items": items, "evidence": evidence, "reply_floor_s": floor,
            "segments": len(segs), "voiced": len(voiced), "log": log_report(events)}


def main(argv=None) -> None:
    """Print the metrics; append a LEDGER row per decider (metrics only)."""
    ap = argparse.ArgumentParser()
    ap.add_argument("recordings")
    ap.add_argument("--logs", default="~/Library/Logs/reachy-sage/bot-*.log")
    ap.add_argument("--stt-url")
    ap.add_argument("--cache", type=Path)
    ap.add_argument("--until", help="ignore recordings stopped after this (YYYYmmdd-HHMMSS)")
    ap.add_argument("--option", default="owner-live")
    ap.add_argument("--no-ledger", action="store_true")
    args = ap.parse_args(argv)
    paths = sorted(glob.glob(os.path.join(args.recordings, "utt-*.wav")))
    if args.until:
        paths = [p for p in paths if Path(p).stem.removeprefix("utt-")[:15] <= args.until]
    segs = segments(paths)
    lines = [line for f in sorted(glob.glob(os.path.expanduser(args.logs)))
             for line in Path(f).read_text(errors="replace").splitlines()]
    events = log_events(lines)
    if paths:  # the log runs on past the last recording kept
        events = [e for e in events if e[0] <= stop_time(paths[-1]) + RUN_GAP_S]
    texts = transcripts(segs, events, args.stt_url, args.cache)
    result = analyse(segs, events, texts, smart_turn_predict())
    print(json.dumps({k: v for k, v in result.items() if k != "items"}, indent=1))
    print(json.dumps(pause_summary(result["items"]), indent=1))
    names = [k for k in result["items"][0]
             if k not in ("turn", "cont", "boundary", "ms", "unfinished")]
    rows = option_rows(result["items"], names, "silence450")
    print(score.table([{k: v for k, v in r.items() if not k.endswith("_ci")} for r in rows]))
    for r in rows:
        print(r["option"], "rate CI", np.round(r["rate_ci"], 3),
              "delta CI", np.round(r["delta_ci"], 3))
    if not args.no_ledger:
        sha = hashlib.sha256("\n".join(Path(p).name for p in paths).encode()).hexdigest()
        for r in rows:
            low, high = r["delta_ci"]
            metrics = (f"false_split {r['false_split_rate']:.3f} delta {r['delta_vs_base']:+.3f} "
                       f"[{low:+.3f}, {high:+.3f}] wait p50 {r['wait_p50_ms']:.0f} "
                       f"p95 {r['wait_p95_ms']:.0f} ms")
            score.ledger(score.LEDGER, "turn-taking", f"{args.option} {r['option']}",
                         Path(args.recordings).name, metrics, n=r["cont_pauses"], ci=r["rate_ci"],
                         sha256=sha)


if __name__ == "__main__":
    main()
