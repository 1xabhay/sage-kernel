"""Score a labeled set of robot-mic recordings, one stage at a time, with standard metrics.

A dataset is a folder with ``manifest.jsonl`` (``record.py`` writes one; README in
``script.md``). Each scorer is a pure function over the clips and a backend callable,
so tests use fakes; the CLI picks the backend, and calls a live service (127.0.0.1:878x)
only when a ``--*-url`` flag names it. Every run prints a table and appends a dated row
to ``eval/LEDGER.md`` with its n, the 95% CI of its first metric and the sha256 of the
manifest lines scored; a row missing any of them is refused. Dataset scorers take
``--channel L|R|mean`` (stereo recordings: L conference, R ASR beam) and ``--session S``
(repeatable: dev or test).

    uv run python eval/score.py freeze <set> --session S
        Hash session S's manifest lines into eval/FROZEN.md: the test set, fixed before
        anything is scored on it. A changed manifest under a frozen name is refused.
    uv run python eval/score.py paired <set> --a A --b B [--stt-url URL]
        Two options on the same owner utterances (each a jsonl of {wav, text}, or a channel
        transcribed live): WER delta A-B with its paired bootstrap 95% CI, and McNemar's exact
        test on which utterances each got wrong.
    uv run python eval/score.py stt <set> --hyp hyp.jsonl | --stt-url http://127.0.0.1:8781
        WER and CER (corpus level, after ``normalize``), per condition and distance, and
        how often "Sage" and "Reachy" come out right. Owner clips (--speaker replay: the
        replayed public set, eval/replay.py).
    uv run --with onnx-asr python eval/score.py speaker <set> [--embed wespeaker|mod:fn] [-k 3]
        FRR at FAR=1% (Wilson CI, rule-of-3 FAR bound) and EER on Silero-trimmed windows
        from the speech onset (1.0 s, 1.5 s, the full turn), leave-one-session-out, each clip
        scored against its top-k closest owner enrollment clips. Refuses fewer than 2 owner
        sessions or no impostor clip.
    uv run python eval/score.py addressed <set> --hyp hyp.jsonl | --stt-url URL [--aliases Reachy]
        Name hits (clips whose line names the robot that the app's Addressed filter passes)
        with false wakes per hour (every other clip it passes, per hour of that audio).
    uv run python eval/score.py pipeline <set> --stt-url URL [--turn-end-ms 450]
        WER of the app's path (Silero segments with pre-roll and stop tail, each to the STT)
        beside the oracle WER of the whole clip: what segmentation costs.
    uv run python eval/score.py turns <set> [--turn-end-ms 450]
        False-split and merge rates of Silero (CPU, offline) plus the turn-end silence,
        against the script lines.
    uv run python eval/score.py latency <latency.txt | kernel.sqlite3> [--kernel PATH]
        p50/p95 of stt_ms, ttft_ms and first_audio_ms.
    uv run python eval/score.py song-qa [--passages p.json | --research-url URL] [--replies r.jsonl]
        Recall@k of the passages the kernel would show, and answer accuracy by keyword.
"""

import argparse
import datetime
import hashlib
import importlib
import itertools
import json
import math
import os
import re
import subprocess
import sys
import urllib.request
import wave
from collections.abc import Callable, Iterable
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent


def kernel_dir() -> str:
    """Find the Sage kernel's checkout: SAGE_KERNEL_DIR, else the root of this file's repo."""
    git = ["git", "rev-parse", "--show-toplevel"]
    return os.environ.get("SAGE_KERNEL_DIR") or subprocess.run(
        git, cwd=HERE, capture_output=True, text=True).stdout.strip()
LEDGER = HERE / "LEDGER.md"
FROZEN = HERE / "FROZEN.md"
NAMES = ("sage", "reachy")
DEVICE = ("stt_ms", "ttft_ms", "first_audio_ms")

# Normalization ------------------------------------------------------------------------------
# Whisper's EnglishTextNormalizer is not a dependency here (openai-whisper / transformers), so
# this is the documented simple one: lowercase, drop apostrophes, every other non-word character
# to a space, digits spelled out as people say them (years in pairs), whitespace collapsed.
# ponytail: no ordinals ("1st"), currency, or British spellings; add them when a WER diff shows
# them. Both sides go through the same function, so a spelled-out reference still matches.

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
         "nineteen"]
_TENS = ["_", "_", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def number_words(n: int, year: bool = True) -> str:
    """A cardinal in words; 1100-1999 and 2010-2099 as years: 'nineteen eighty two'."""
    if year and (1100 <= n <= 1999 or 2010 <= n <= 2099):
        high, low = divmod(n, 100)
        return number_words(high) + (" hundred" if low == 0 else " oh " + _ONES[low] if low < 10
                                     else " " + number_words(low))
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    for size, name in ((10**9, "billion"), (10**6, "million"), (1000, "thousand"), (100, "hundred")):
        if n >= size:
            high, low = divmod(n, size)
            return f"{number_words(high, False)} {name}" + ("" if low == 0 else " " + number_words(low, False))
    return str(n)


def normalize(text: str) -> str:
    """Text for WER: lowercase, no punctuation, numbers as words, single spaces."""
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text.lower())
    text = re.sub(r"\d+", lambda m: f" {number_words(int(m.group()))} ", text)
    text = re.sub(r"[\W_]+", " ", text.replace("'", "").replace(chr(0x2019), ""))
    return " ".join(text.split())


# STT ----------------------------------------------------------------------------------------


def edit_distance(ref, hyp) -> int:
    """Levenshtein distance between two sequences (words or characters)."""
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        diagonal, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            diagonal, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, diagonal + (r != h))
    return row[-1]


def wer(refs: Iterable[str], hyps: Iterable[str]) -> float:
    """Word error rate over a corpus: all edits over all reference words, after ``normalize``."""
    pairs = [(normalize(r).split(), normalize(h).split()) for r, h in zip(refs, hyps, strict=True)]
    return sum(edit_distance(r, h) for r, h in pairs) / max(1, sum(len(r) for r, _ in pairs))


def cer(refs: Iterable[str], hyps: Iterable[str]) -> float:
    """Character error rate over a corpus, spaces included, after ``normalize``."""
    pairs = [(normalize(r), normalize(h)) for r, h in zip(refs, hyps, strict=True)]
    return sum(edit_distance(r, h) for r, h in pairs) / max(1, sum(len(r) for r, _ in pairs))


def _groups(clips: list[dict]) -> list[tuple[str, list[dict]]]:
    """'all', then each condition, then each distance, in first-seen order."""
    out = [("all", clips)]
    for key, label in (("condition", "{}"), ("distance_m", "{}m")):
        for value in dict.fromkeys(c.get(key) for c in clips):
            out.append((label.format(value), [c for c in clips if c.get(key) == value]))
    return out


def stt_report(clips: list[dict], transcribe: Callable[[str], str], speaker: str = "owner") -> dict:
    """WER/CER per group and each robot name's hit rate, on ``speaker``'s transcribed clips."""
    clips = [c for c in clips if c.get("speaker") == speaker and c.get("text")]
    for clip in clips:
        clip["hyp"] = transcribe(clip["path"])
    rows = [{"group": g, "n": len(cs), "wer": wer([c["text"] for c in cs], [c["hyp"] for c in cs]),
             "cer": cer([c["text"] for c in cs], [c["hyp"] for c in cs])} for g, cs in _groups(clips)]
    names = {}
    for name in NAMES:
        said = heard = 0
        for clip in clips:
            ref, hyp = normalize(clip["text"]).split(), normalize(clip["hyp"]).split()
            said += ref.count(name)
            heard += min(ref.count(name), hyp.count(name))
        names[name] = heard / said if said else None
    ci = wer_ci([c["text"] for c in clips], [c["hyp"] for c in clips]) if clips else None
    return {"rows": rows, "names": names, "ci": ci}


def _post_wav(url: str, path: str) -> dict:
    return _post_bytes(url, Path(path).read_bytes(), Path(path).name)


def _post_bytes(url: str, data: bytes, name: str) -> dict:
    from reachy_sage_voice.song import form_wav

    body, kind = form_wav(data, name)
    request = urllib.request.Request(url, body, {"Content-Type": kind}, method="POST")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def _root(url: str) -> str:
    return url.rstrip("/").removesuffix("/v1")


# Speaker verification -----------------------------------------------------------------------


def _rates(trials, threshold) -> tuple[float, float]:
    targets = [s for s, t in trials if t]
    impostors = [s for s, t in trials if not t]
    far = sum(s >= threshold for s in impostors) / max(1, len(impostors))
    frr = sum(s < threshold for s in targets) / max(1, len(targets))
    return far, frr


def _thresholds(trials) -> list[float]:
    return [*sorted({s for s, _ in trials}), math.inf]


def eer(trials: list[tuple[float, bool]]) -> float:
    """Equal error rate: (FAR+FRR)/2 at the threshold where they are closest. Accept: score >= t."""
    far, frr = min((_rates(trials, t) for t in _thresholds(trials)), key=lambda r: abs(r[0] - r[1]))
    return (far + frr) / 2


def frr_at_far(trials: list[tuple[float, bool]], far: float = 0.01) -> float:
    """The lowest FRR at any threshold whose FAR is at most ``far``."""
    return min(frr for f, frr in (_rates(trials, t) for t in _thresholds(trials)) if f <= far)


def duration_s(path: str) -> float:
    """A WAV's length in seconds."""
    with wave.open(path) as w:
        return w.getnframes() / w.getframerate()


#: Windows scored, from the speech onset: what the gate has heard by barge-in time, and the turn.
WINDOWS = (("1.0s", 1.0), ("1.5s", 1.5), ("full", None))
SPEAKER_MODEL = "Wespeaker/wespeaker-voxceleb-resnet34-LM"  # services/stt's


def _unit(vectors: np.ndarray) -> np.ndarray:
    return vectors / np.maximum(np.linalg.norm(vectors, axis=-1, keepdims=True), 1e-12)


def top_k_score(probe: np.ndarray, enrolled: np.ndarray, k: int) -> float:
    """Return the mean cosine of ``probe`` to its ``k`` closest enrollment embeddings.

    Closer to how a voice varies than one mean voiceprint: a turn is judged against the
    enrollment clips it most resembles (same distance, same loudness), not their average.
    """
    cosines = _unit(enrolled) @ _unit(probe)
    return float(np.sort(cosines)[-k:].mean())


def speaker_report(clips: list[dict], embed: Callable[[np.ndarray], np.ndarray],
                   vad: Callable[[np.ndarray, int], list[tuple[float, float]]], k: int = 3) -> dict:
    """Return FRR@FAR1% and EER per onset window, leave-one-session-out.

    Each clip is trimmed by ``vad(pcm, rate)`` to its first speech onset through its last
    speech end; windows take the first 1.0 s and 1.5 s of that (clips shorter are left out of
    the window) and the whole. ``embed(float32 audio)`` gives a voice embedding. A clip is
    scored against the owner's full-turn embeddings from every other session (``top_k_score``),
    so no session enrolls itself. Target: speaker "owner"; impostors: everyone and everything
    else. Refused (ValueError) with fewer than 2 owner sessions or no impostor clip: a threshold
    tuned and scored on one sitting, or never tried on another voice, says nothing.
    """
    from reachy_sage_voice.turn_end import SAMPLE_RATE, load_pcm16

    speech, no_speech = [], 0
    for clip in clips:
        pcm = load_pcm16(clip["path"])
        runs = vad(pcm, SAMPLE_RATE)
        if not runs:
            no_speech += 1
            continue
        start, end = int(runs[0][0] * SAMPLE_RATE), int(runs[-1][1] * SAMPLE_RATE)
        speech.append((clip, pcm[start:end].astype(np.float32) / 32768))
    owner_sessions = {c["session"] for c, _ in speech if c["speaker"] == "owner"}
    if len(owner_sessions) < 2:
        raise ValueError(f"speaker needs the owner in 2 sessions or more, not {len(owner_sessions)}: "
                         "enrollment and test must come from different sittings")
    if all(c["speaker"] == "owner" for c, _ in speech):
        raise ValueError("speaker needs impostor clips (record.py --replay): FAR is unmeasured without")
    full = {id(c): embed(audio) for c, audio in speech}
    trials = []
    for clip, audio in speech:
        enrolled = np.array([full[id(c)] for c, _ in speech
                             if c["speaker"] == "owner" and c["session"] != clip["session"]])
        for window, seconds in WINDOWS:
            if seconds is None:
                vector = full[id(clip)]
            elif len(audio) >= seconds * SAMPLE_RATE:
                vector = embed(audio[:int(seconds * SAMPLE_RATE)])
            else:
                continue
            trials.append({"window": window, "wav": clip.get("wav"), "target": clip["speaker"] == "owner",
                           "score": top_k_score(vector, enrolled, k)})
    rows = []
    for window, _ in WINDOWS:
        found = [(t["score"], t["target"]) for t in trials if t["window"] == window]
        targets = sum(t for _, t in found)
        impostors = len(found) - targets
        frr = frr_at_far(found) if targets and impostors else None
        rows.append({"window": window, "targets": targets, "impostors": impostors,
                     "frr@far1%": frr, "frr_ci": None if frr is None else wilson(round(frr * targets), targets),
                     "eer": eer(found) if targets and impostors else None,
                     "far_bound": 3 / impostors if impostors else None})
    return {"rows": rows, "trials": trials, "no_speech": no_speech}


def load_embed(spec: str) -> Callable[[np.ndarray], np.ndarray]:
    """Return an embedding callable: "wespeaker" (services/stt's model, through onnx-asr) or module:name."""
    if spec != "wespeaker":
        module, _, name = spec.partition(":")
        return getattr(importlib.import_module(module), name)
    try:
        from onnx_asr.loader import Manager
    except ImportError:
        sys.exit("wespeaker needs onnx-asr: uv run --with onnx-asr python eval/score.py speaker ...")
    return Manager(providers=["CPUExecutionProvider"]).create_se(SPEAKER_MODEL).embedding


# Turn segmentation --------------------------------------------------------------------------


def turn_errors(lines: list[tuple[float, float]], turns: list[tuple[float, float]]) -> dict:
    """Per script line (a reference span): split into 2+ turns, merged with another, or missed.

    A turn belongs to every line it overlaps in time.
    """
    touching = [[j for j, (ts, te) in enumerate(turns) if ts < le and te > ls] for ls, le in lines]
    owners = [sum(j in t for t in touching) for j in range(len(turns))]
    return {"lines": len(lines), "split": sum(len(t) > 1 for t in touching),
            "merged": sum(any(owners[j] > 1 for j in t) for t in touching),
            "missed": sum(not t for t in touching)}


def turns_report(clips: list[dict], segment: Callable) -> list[dict]:
    """Split and merge rates: each session/condition/distance's clips end to end, in manifest order.

    ``segment(pcm_int16, rate)`` returns turns as (start_s, end_s). The pause between two lines is
    whatever silence the clips have at their ends, as when the owner reads them one after another.
    """
    from reachy_sage_voice.turn_end import SAMPLE_RATE, load_pcm16

    clips = [c for c in clips if c.get("speaker") == "owner" and c.get("text")]
    keys = dict.fromkeys((c.get("session"), c.get("condition"), c.get("distance_m")) for c in clips)
    counted = []
    for key in keys:
        run = [c for c in clips if (c.get("session"), c.get("condition"), c.get("distance_m")) == key]
        audio = [load_pcm16(c["path"]) for c in run]
        edges = np.cumsum([0] + [len(a) for a in audio]) / SAMPLE_RATE
        errors = turn_errors(list(itertools.pairwise(edges)),
                             segment(np.concatenate(audio), SAMPLE_RATE))
        counted.append((run[0], errors))
    rows = []
    for group, members in _groups([c for c, _ in counted]):
        totals = [e for c, e in counted if any(c is m for m in members)]
        n = sum(e["lines"] for e in totals)
        rows.append({"group": group, "lines": n, **{name: sum(e[k] for e in totals) / max(1, n) for k, name in
                                                   (("split", "split_rate"), ("merged", "merge_rate"),
                                                    ("missed", "missed_rate"))}})
    return rows


def silero_runs(pcm: np.ndarray, rate: int) -> list[tuple[float, float]]:
    """Return the pipeline's Silero VAD (CPU) speech runs in 16 kHz int16 ``pcm``, raw."""
    import asyncio

    from reachy_sage_voice.turn_end import CHUNK, _states, speech_runs

    return speech_runs(asyncio.run(_states(pcm)), CHUNK / rate)


def silero_segmenter(turn_end_ms: int) -> Callable:
    """The pipeline's Silero VAD (CPU) as raw speech runs, joined across pauses under the turn end.

    ponytail: models the plain VAD turn end only; with SAGE_VOICE_SMART_TURN the smart-turn model
    decides too, which this does not run.
    """
    def segment(pcm, rate):
        runs = silero_runs(pcm, rate)
        turns = []
        for start, end in runs:
            if turns and (start - turns[-1][1]) * 1000 < turn_end_ms:
                turns[-1] = (turns[-1][0], end)
            else:
                turns.append((start, end))
        return turns
    return segment


# Addressed ----------------------------------------------------------------------------------


def addressed_report(clips: list[dict], transcribe: Callable[[str], str],
                     aliases: tuple[str, ...] = ("Reachy",)) -> dict:
    """Return name hits and false wakes per hour of the app's own ``Addressed`` filter.

    Each transcript goes through a fresh ``Addressed(aliases)``, no face and no exchange window:
    the name alone decides, as for a first turn. A clip whose reference says "Sage" or "Reachy"
    should pass (a name hit); any other clip, the owner's or TV, music, impostors, should not
    (a false wake), counted per hour of that audio. Paired metrics: neither means much alone.
    """
    import asyncio

    from pipecat.frames.frames import TranscriptionFrame

    from reachy_sage_voice.addressed import Addressed

    for clip in clips:
        clip["named"] = bool(set(normalize(clip.get("text") or "").split()) & set(NAMES))
        frame = TranscriptionFrame(transcribe(clip["path"]), "eval", "")
        clip["woke"] = asyncio.run(Addressed(aliases)(frame))
        clip["seconds"] = duration_s(clip["path"])
    rows = []
    for group, members in _groups(clips):
        named = [c for c in members if c["named"]]
        others = [c for c in members if not c["named"]]
        hits, wakes = sum(c["woke"] for c in named), sum(c["woke"] for c in others)
        hours = sum(c["seconds"] for c in others) / 3600
        rows.append({"group": group, "named": len(named), "name_hits": hits,
                     "name_hit_rate": hits / len(named) if named else None, "negatives": len(others),
                     "hours": hours, "false_wakes": wakes, "false_wakes_per_hour": wakes / hours if hours else None})
    return {"rows": rows, "ci": wilson(rows[0]["name_hits"], rows[0]["named"])}


# Pipeline path ------------------------------------------------------------------------------


def pipeline_report(clips: list[dict], transcribe_pcm: Callable[[np.ndarray], str], segment: Callable,
                    pre_roll_s: float | None = None, stop_s: float | None = None) -> dict:
    """Return WER of the app's path (VAD segments, each to the STT) and of the whole clip (oracle).

    ``segment(pcm, rate)`` gives speech turns; each goes to ``transcribe_pcm`` as Pipecat's
    segmented STT sends it: from ``pre_roll_s`` before its start (never back into audio already
    sent) to ``stop_s`` after its end, when the VAD declares the stop. The texts are joined.
    The gap between the two WERs is what segmentation costs. Owner transcribed clips only.
    """
    from reachy_sage_voice.transport import PRE_ROLL_S
    from reachy_sage_voice.turn_end import DEFAULT_MS, SAMPLE_RATE, load_pcm16

    pre_roll_s = PRE_ROLL_S if pre_roll_s is None else pre_roll_s
    stop_s = DEFAULT_MS / 1000 if stop_s is None else stop_s
    clips = [c for c in clips if c.get("speaker") == "owner" and c.get("text")]
    refs, piped, oracle, segments = [c["text"] for c in clips], [], [], 0
    for clip in clips:
        pcm, sent, texts = load_pcm16(clip["path"]), 0, []
        for start, end in segment(pcm, SAMPLE_RATE):
            low = max(sent, int((start - pre_roll_s) * SAMPLE_RATE))
            high = min(len(pcm), int((end + stop_s) * SAMPLE_RATE))
            if high > low:
                texts.append(transcribe_pcm(pcm[low:high]))
                sent, segments = high, segments + 1
        piped.append(" ".join(texts))
        oracle.append(transcribe_pcm(pcm))
    return {"n": len(clips), "segments": segments, "wer_pipeline": wer(refs, piped),
            "wer_oracle": wer(refs, oracle), "ci": wer_ci(refs, piped) if clips else None}


def stt_pcm(url: str) -> Callable[[np.ndarray], str]:
    """Return the live STT as a callable on 16 kHz int16 audio."""
    import io

    def transcribe(pcm):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm.astype(np.int16).tobytes())
        return _post_bytes(_root(url) + "/v1/audio/transcriptions", buffer.getvalue(), "segment.wav")["text"]
    return transcribe


# Latency ------------------------------------------------------------------------------------


def pct(values, p: float):
    """Nearest-rank percentile, as sage-kernel bench/latency.py computes it."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def latency_from_text(text: str) -> list[dict]:
    """Per-turn timings from ``bench/latency.py`` output lines ('stt 120 ttft 400 first_audio 700')."""
    turns = []
    for line in text.splitlines():
        found = dict(re.findall(r"\b(stt|ttft|first_audio) (\d+|-)(?=\s|$)", line))
        if found:
            turns.append({f"{k}_ms": None if found.get(k, "-") == "-" else int(found[k])
                          for k in ("stt", "ttft", "first_audio")})
    return turns


def latency_from_journal(path: str, kernel: str) -> list[dict]:
    """Per-turn timings from a copy of the kernel's journal, through its own bench/latency.py."""
    sys.path.insert(0, str(Path(kernel) / "bench"))
    return importlib.import_module("latency").turns(path)


def latency_summary(turns: list[dict]) -> dict:
    """n, p50 and p95 of each device timing that was reported."""
    out = {}
    for key in DEVICE:
        values = [t[key] for t in turns if t.get(key) is not None]
        if values:
            out[key] = {"n": len(values), "p50": pct(values, 50), "p95": pct(values, 95)}
    return out


# Song Q&A -----------------------------------------------------------------------------------


def has_keywords(text: str, keywords: list[str]) -> bool:
    """Whether every keyword is in ``text`` as whole words, after ``normalize``; 'a|b' is either."""
    text = f" {normalize(text)} "
    return all(any(f" {normalize(alt)} " in text for alt in k.split("|")) for k in keywords)


def kernel_relevant(kernel: str) -> tuple[Callable, int]:
    """The kernel's own retriever (sage.policy.relevant) and how many it shows, imported read-only."""
    sys.path.insert(0, str(Path(kernel)))
    policy = importlib.import_module("sage.policy")
    return policy.relevant, policy.NOTES_SHOWN


def song_qa_report(items: list[dict], passages_for: Callable[[str], list[str]], relevant: Callable,
                   k: int, reply: Callable[[str], str | None] | None = None) -> list[dict]:
    """Retrieval recall@k and answer accuracy, overall and for time-sensitive questions apart.

    Shown is what the kernel's prompt shows (policy._notes): the first passage, the song's card,
    plus ``relevant(rest, question, k)``. Answerable: some passage has the keywords at all;
    recall is over answerable questions. Accuracy needs ``reply(question)``.
    """
    for item in items:
        found = list(passages_for(item["song"]))
        shown = found[:1] + list(relevant(tuple(found[1:]), item["question"], k))
        item["answerable"] = any(has_keywords(p, item["answer_keywords"]) for p in found)
        item["shown"] = any(has_keywords(p, item["answer_keywords"]) for p in shown)
        said = reply(item["question"]) if reply else None
        item["correct"] = None if said is None else has_keywords(said, item["answer_keywords"])
    rows = []
    for group, members in (("all", items), ("time-sensitive", [i for i in items if i.get("time_sensitive")]),
                           *((s, [i for i in items if i["song"] == s]) for s in dict.fromkeys(i["song"] for i in items))):
        answerable = [i for i in members if i["answerable"]]
        replied = [i for i in members if i["correct"] is not None]
        rows.append({"group": group, "n": len(members), "answerable": len(answerable),
                     "recall": sum(i["shown"] for i in answerable) / len(answerable) if answerable else None,
                     "accuracy": sum(i["correct"] for i in replied) / len(replied) if replied else None})
    return rows


# Statistics ---------------------------------------------------------------------------------
# Paired, because both options hear the same utterances: the per-utterance difference cancels
# what the utterances share (speaker, phrase, distance), so a smaller set shows a real change.


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Return the Wilson 95% interval of a proportion k/n (sound at 0 and n), None if n is 0."""
    if n == 0:
        return None
    p, z2 = k / n, z * z
    middle, half = (p + z2 / (2 * n)) / (1 + z2 / n), z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return max(0.0, middle - half), min(1.0, middle + half)


def bootstrap(n: int, stat: Callable[[np.ndarray], float], n_boot: int = 2000,
              seed: int = 0) -> tuple[float, float]:
    """Return the percentile 95% interval of ``stat(indices)`` over resamples of n items."""
    rng = np.random.default_rng(seed)
    values = [stat(rng.integers(0, n, n)) for _ in range(n_boot)]
    return float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5))


def _edits(refs, hyps) -> tuple[np.ndarray, np.ndarray]:
    pairs = [(normalize(r).split(), normalize(h).split()) for r, h in zip(refs, hyps, strict=True)]
    return np.array([edit_distance(r, h) for r, h in pairs]), np.array([len(r) for r, _ in pairs])


def wer_ci(refs: list[str], hyps: list[str], n_boot: int = 2000, seed: int = 0) -> tuple[float, float]:
    """Return the bootstrap 95% interval of corpus WER, resampling utterances."""
    edits, words = _edits(refs, hyps)
    return bootstrap(len(refs), lambda i: edits[i].sum() / max(1, words[i].sum()), n_boot, seed)


def mcnemar(a_wrong: list[bool], b_wrong: list[bool]) -> dict:
    """Return McNemar's exact test on paired per-utterance errors: discordant counts and p.

    Two-sided binomial on the pairs where exactly one option erred; p = 1 when they tie.
    """
    a_only = sum(a and not b for a, b in zip(a_wrong, b_wrong, strict=True))
    b_only = sum(b and not a for a, b in zip(a_wrong, b_wrong, strict=True))
    n, low = a_only + b_only, min(a_only, b_only)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(low + 1)) / 2 ** n) if n else 1.0
    return {"a_only": a_only, "b_only": b_only, "p": p}


def paired_wer(refs: list[str], hyps_a: list[str], hyps_b: list[str], n_boot: int = 2000,
               seed: int = 0) -> dict:
    """Return WER of A and B on the same utterances, delta = A - B with its paired bootstrap CI,
    and McNemar on which utterances each got wrong."""
    edits_a, words = _edits(refs, hyps_a)
    edits_b, _ = _edits(refs, hyps_b)
    total = max(1, words.sum())
    ci = bootstrap(len(refs), lambda i: (edits_a[i].sum() - edits_b[i].sum()) / max(1, words[i].sum()),
                   n_boot, seed)
    return {"n": len(refs), "wer_a": edits_a.sum() / total, "wer_b": edits_b.sum() / total,
            "delta": (edits_a.sum() - edits_b.sum()) / total, "ci": ci,
            "mcnemar": mcnemar(list(edits_a > 0), list(edits_b > 0))}


# Output -------------------------------------------------------------------------------------


def load_jsonl(path) -> list[dict]:
    """One JSON object per non-empty line."""
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


CHANNELS = ("mean", "L", "R")


def channel_wav(folder: Path, wav: str, channel: str) -> str:
    """Return a mono WAV of one channel (or the mean) of ``folder/wav``, cached in ``.channels``.

    A mono recording is returned as it is. The cache sits inside the dataset (git-ignored,
    the owner's voice); it is rewritten every call, so a re-recorded take is never stale.
    """
    path = folder / wav
    with wave.open(str(path)) as w:
        channels, rate = w.getnchannels(), w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16).reshape(-1, channels)
    if channels == 1:
        return str(path)
    mono = pcm.mean(axis=1) if channel == "mean" else pcm[:, "LR".index(channel)]
    cached = folder / ".channels" / channel / wav
    cached.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(cached), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(mono.astype(np.int16).tobytes())
    return str(cached)


def _manifest_lines(folder, sessions: Iterable[str] | None = None) -> list[str]:
    lines = [line for line in (Path(folder) / "manifest.jsonl").read_text().splitlines() if line.strip()]
    return lines if sessions is None else [x for x in lines if json.loads(x).get("session") in set(sessions)]


def manifest_sha256(folder, sessions: Iterable[str] | None = None) -> str:
    """Return the sha256 of the manifest lines scored (each with its newline).

    Each line carries its WAV's own sha256 (record.py), so this hash pins the audio and labels.
    """
    return hashlib.sha256("".join(x + "\n" for x in _manifest_lines(folder, sessions)).encode()).hexdigest()


def load_manifest(folder, channel: str = "mean", sessions: Iterable[str] | None = None) -> list[dict]:
    """Return the dataset's clips (of ``sessions``), each with ``path``: a mono WAV of ``channel``."""
    folder = Path(folder)
    return [{**clip, "path": channel_wav(folder, clip["wav"], channel)}
            for clip in map(json.loads, _manifest_lines(folder, sessions))]


def freeze(folder, sessions: list[str] | None, frozen=FROZEN) -> str:
    """Write the hash of these sessions' manifest lines to FROZEN.md; return it.

    Freezing the same lines again changes nothing; different lines under a name already frozen
    are refused: a test set, once frozen, is not relabeled or re-recorded.
    """
    frozen, sha = Path(frozen), manifest_sha256(folder, sessions)
    name = ",".join(sessions) if sessions else "all"
    if not frozen.exists():
        frozen.write_text("# Frozen test sets\n\nWritten by `eval/score.py freeze`. A LEDGER row reports "
                          "on a frozen set when its sha256 is here.\n\n| date | dataset | sessions | clips "
                          "| sha256 |\n|---|---|---|---|---|\n")
    for row in frozen.read_text().splitlines():
        cells = [c.strip() for c in row.strip("|").split("|")]
        if len(cells) == 5 and cells[2] == name:
            if cells[4] != sha:
                raise ValueError(f"{name} is frozen with sha256 {cells[4]}; its manifest now hashes to {sha}")
            return sha
    with frozen.open("a") as f:
        f.write(f"| {_today()} | {Path(folder).name} | {name} | {len(_manifest_lines(folder, sessions))} "
                f"| {sha} |\n")
    return sha


def _cell(value) -> str:
    return "-" if value is None else f"{value:.3f}" if isinstance(value, float) else str(value)


def table(rows: list[dict]) -> str:
    """Rows as an aligned plain-text table."""
    keys = list(rows[0])
    cells = [keys] + [[_cell(r[k]) for k in keys] for r in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(keys))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)) for row in cells)


def _today() -> str:
    return datetime.datetime.now().astimezone().date().isoformat()


def ledger(path, stage: str, option: str, dataset: str, metrics: str, *, n: int | None,
           ci: tuple[float, float] | None, sha256: str | None) -> None:
    """Append a dated row; write the header first if the ledger is new.

    Refused (ValueError) without its sample size ``n``, the 95% CI of its first metric, and the
    sha256 of what was scored: a number nobody can bound or trace back is not a result.
    """
    if not isinstance(n, int) or n < 1:
        raise ValueError(f"n must be a positive count, not {n!r}")
    if ci is None or len(ci) != 2 or not ci[0] <= ci[1]:
        raise ValueError(f"ci must be (low, high), not {ci!r}")
    if not (isinstance(sha256, str) and re.fullmatch(r"[0-9a-f]{64}", sha256)):
        raise ValueError(f"sha256 of the manifest scored is required, not {sha256!r}")
    path = Path(path)
    if not path.exists() or not path.read_text().strip():
        path.write_text("# Voice eval ledger\n\nOne row per scorer run (eval/score.py). Compare rows "
                        "with the same stage and sha256. The CI is the first metric's.\n\n"
                        "| date | stage | option | dataset | n | 95% CI | metrics | sha256 |\n"
                        "|---|---|---|---|---|---|---|---|\n")
    with path.open("a") as f:
        f.write(f"| {_today()} | {stage} | {option} | {dataset} | {n} | [{ci[0]:.3f}, {ci[1]:.3f}] "
                f"| {metrics} | {sha256} |\n")


def file_sha256(path) -> str:
    """Return the sha256 of a file's bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _summary(row: dict, keys: Iterable[str]) -> str:
    return " ".join(f"{k} {_cell(row[k])}" for k in keys)


def _transcriber(args, clips: list[dict], parser) -> Callable[[str], str]:
    """``--hyp`` transcripts by WAV, or the live ``--stt-url``; the parser errors without either."""
    if args.hyp:
        heard = {h["wav"]: h["text"] for h in load_jsonl(args.hyp)}
        wavs = {c["path"]: c["wav"] for c in clips}
        return lambda path: heard[wavs[path]]
    if args.stt_url:
        return lambda path: _post_wav(_root(args.stt_url) + "/v1/audio/transcriptions", path)["text"]
    parser.error(f"{args.stage} needs --hyp or --stt-url")


def main(argv: list[str] | None = None) -> None:
    """Run one scorer: print its table, append its ledger row (n, 95% CI, sha256 of the input)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="stage", required=True)
    stt, speaker, turns = sub.add_parser("stt"), sub.add_parser("speaker"), sub.add_parser("turns")
    latency, song, paired = sub.add_parser("latency"), sub.add_parser("song-qa"), sub.add_parser("paired")
    addressed, pipeline = sub.add_parser("addressed"), sub.add_parser("pipeline")
    frozen = sub.add_parser("freeze", help="hash a session's manifest lines into eval/FROZEN.md")
    audio = (stt, speaker, turns, addressed, pipeline)
    for p in (*audio, paired, frozen):
        p.add_argument("dataset")
        p.add_argument("--session", action="append", help="only these sessions (repeatable)")
    for p in audio:
        p.add_argument("--channel", choices=CHANNELS, default="mean",
                       help="which channel of a stereo recording: L conference, R ASR beam")
    for p in (*audio, latency, song, paired):
        p.add_argument("--option", default="default", help="what is being compared, for the ledger")
        p.add_argument("--ledger", default=str(LEDGER))
    frozen.add_argument("--frozen", default=str(FROZEN))
    for p in (stt, addressed):
        p.add_argument("--hyp", help="jsonl of {wav, text}: transcripts made elsewhere")
    for p in (stt, paired, addressed):
        p.add_argument("--stt-url", help="transcribe with this live STT (e.g. http://127.0.0.1:8781)")
    for p in (stt, paired):
        p.add_argument("--speaker", default="owner",
                       help="whose clips: owner, or replay (eval/replay.py)")
    pipeline.add_argument("--stt-url", required=True, help="the live STT each segment goes to")
    addressed.add_argument("--aliases", default="Reachy", help="as SAGE_VOICE_ALIASES: comma-separated")
    pipeline.add_argument("--turn-end-ms", type=int, default=None)
    paired.add_argument("--a", required=True, help="option A: a jsonl of {wav, text}, or a channel")
    paired.add_argument("--b", required=True, help="option B: a jsonl of {wav, text}, or a channel")
    speaker.add_argument("--embed", default="wespeaker", help="wespeaker, or module:callable taking "
                         "16 kHz float32 audio and returning an embedding")
    speaker.add_argument("-k", type=int, default=3, help="enrollment clips each score averages")
    turns.add_argument("--turn-end-ms", type=int, default=None)
    latency.add_argument("source", help="bench/latency.py output, or a copy of kernel.sqlite3")
    latency.add_argument("--kernel", default=kernel_dir())
    song.add_argument("--qa", default=str(HERE / "song_qa.jsonl"))
    song.add_argument("--passages", help="json of {song: [passages]}")
    song.add_argument("--research-url", help="fetch passages from this live research service")
    song.add_argument("--replies", help="jsonl of {question, reply}")
    song.add_argument("--kernel", default=kernel_dir())
    song.add_argument("-k", type=int, default=None, help="passages shown (default: the kernel's)")
    args = parser.parse_args(argv)

    if args.stage == "freeze":
        try:
            sha = freeze(args.dataset, args.session, args.frozen)
        except ValueError as error:
            sys.exit(str(error))
        print(f"frozen {','.join(args.session or ['all'])}: sha256 {sha} ({args.frozen})")
        return
    clips, sha = [], None
    if hasattr(args, "dataset"):
        sha = manifest_sha256(args.dataset, args.session)
    if hasattr(args, "channel"):
        clips = load_manifest(args.dataset, args.channel, args.session)
        if args.channel != "mean":
            args.option += f" ch={args.channel}"
    if args.stage == "addressed":
        aliases = tuple(filter(None, (a.strip() for a in args.aliases.split(","))))
        report = addressed_report(clips, _transcriber(args, clips, parser), aliases)
        print(table(report["rows"]))
        if args.option.startswith("default"):
            args.option = args.option.replace("default", f"aliases={','.join(aliases) or '-'}")
        metrics = _summary(report["rows"][0], ("name_hit_rate", "false_wakes_per_hour", "false_wakes",
                                               "hours"))
        n, ci = report["rows"][0]["named"], report["ci"]
    elif args.stage == "pipeline":
        from reachy_sage_voice.turn_end import DEFAULT_MS

        ms = args.turn_end_ms or DEFAULT_MS
        found = pipeline_report(clips, stt_pcm(args.stt_url), silero_segmenter(ms), stop_s=ms / 1000)
        print(table([{k: v for k, v in found.items() if k != "ci"}]))
        if args.option.startswith("default"):
            args.option = args.option.replace("default", f"silero turn_end={ms}")
        metrics = _summary(found, ("wer_pipeline", "wer_oracle", "segments"))
        n, ci = found["n"], found["ci"]
    elif args.stage == "stt":
        report = stt_report(clips, _transcriber(args, clips, parser), args.speaker)
        print(table(report["rows"]))
        print("names: " + " ".join(f"{k} {_cell(v)}" for k, v in report["names"].items()))
        metrics = _summary(report["rows"][0], ("wer", "cer")) + " " + " ".join(
            f"{k} {_cell(v)}" for k, v in report["names"].items())
        n, ci = report["rows"][0]["n"], report["ci"]
    elif args.stage == "paired":
        lines = [c for c in map(json.loads, _manifest_lines(args.dataset, args.session))
                 if c.get("speaker") == args.speaker and c.get("text")]

        def hyps(spec):
            if spec in CHANNELS:
                if not args.stt_url:
                    parser.error(f"channel {spec} needs --stt-url")
                paths = {c["wav"]: c["path"] for c in load_manifest(args.dataset, spec, args.session)}
                return [_post_wav(_root(args.stt_url) + "/v1/audio/transcriptions", paths[c["wav"]])["text"]
                        for c in lines]
            heard = {h["wav"]: h["text"] for h in load_jsonl(spec)}
            return [heard[c["wav"]] for c in lines]
        found = paired_wer([c["text"] for c in lines], hyps(args.a), hyps(args.b))
        test = found["mcnemar"]
        print(table([{"n": found["n"], "wer_a": found["wer_a"], "wer_b": found["wer_b"], "delta": found["delta"],
                      "ci_low": found["ci"][0], "ci_high": found["ci"][1], "a_only": test["a_only"],
                      "b_only": test["b_only"], "mcnemar_p": test["p"]}]))
        if args.option == "default":
            args.option = f"A {Path(args.a).name} vs B {Path(args.b).name}"
        metrics = (f"delta {_cell(found['delta'])} wer_a {_cell(found['wer_a'])} wer_b {_cell(found['wer_b'])} "
                   f"mcnemar p {test['p']:.4f} (a_only {test['a_only']} b_only {test['b_only']})")
        n, ci = found["n"], found["ci"]
    elif args.stage == "speaker":
        try:
            report = speaker_report(clips, load_embed(args.embed), silero_runs, args.k)
        except ValueError as error:
            sys.exit(str(error))
        rows = report["rows"]
        print(table([{**r, "frr_ci": "-" if r["frr_ci"] is None else "{:.3f}-{:.3f}".format(*r["frr_ci"])}
                     for r in rows]))
        print(f"{report['no_speech']} clips without speech left out")
        for r in rows:
            if r["impostors"]:
                print(f"{r['window']}: rule of 3: with {r['impostors']} impostors and none accepted, FAR is "
                      f"below {r['far_bound']:.3f} at 95%; FAR=1% needs 300 impostors or more")
        if args.option == "default":
            args.option = f"{args.embed} top-{args.k}"
        metrics = "; ".join(f"{r['window']} " + _summary(r, ("frr@far1%", "eer", "far_bound"))
                            for r in reversed(rows))
        n, ci = rows[-1]["targets"], rows[-1]["frr_ci"]
    elif args.stage == "turns":
        from reachy_sage_voice.turn_end import DEFAULT_MS

        ms = args.turn_end_ms or DEFAULT_MS
        args.option = args.option if not args.option.startswith("default") else f"silero turn_end={ms}"
        rows = turns_report(clips, silero_segmenter(ms))
        print(table(rows))
        metrics = _summary(rows[0], ("split_rate", "merge_rate", "missed_rate"))
        n = rows[0]["lines"]
        ci = wilson(round(rows[0]["split_rate"] * n), n)
    elif args.stage == "latency":
        source = Path(args.source)
        found = (latency_from_journal(str(source), args.kernel) if source.suffix in (".sqlite3", ".db")
                 else latency_from_text(source.read_text()))
        summary = latency_summary(found)
        rows = [{"timing": k, **v} for k, v in summary.items()]
        if not rows:
            sys.exit("no device timings found")
        print(table(rows))
        first = "first_audio_ms" if "first_audio_ms" in summary else rows[0]["timing"]
        values = np.array([t[first] for t in found if t.get(first) is not None])
        metrics = f"{first} p50 first; " + " ".join(
            f"{r['timing']} p50 {r['p50']} p95 {r['p95']} (n={r['n']})" for r in rows)
        n, ci = len(values), bootstrap(len(values), lambda i: pct(values[i], 50))
        args.dataset, sha = source.name, file_sha256(source)
    else:
        items = load_jsonl(args.qa)
        if args.passages:
            passages_for = json.loads(Path(args.passages).read_text()).__getitem__
        elif args.research_url:
            from reachy_sage_voice.song import ResearchClient

            client, artists = ResearchClient(args.research_url), {i["song"]: i["artist"] for i in items}
            cache: dict[str, list[str]] = {}

            def passages_for(song):
                if song not in cache:
                    cache[song] = client.research(song, artists[song])["passages"]
                return cache[song]
        else:
            parser.error("song-qa needs --passages or --research-url")
        replies = {r["question"]: r["reply"] for r in load_jsonl(args.replies)} if args.replies else {}
        relevant, shown = kernel_relevant(args.kernel)
        rows = song_qa_report(items, passages_for, relevant, args.k or shown, replies.get)
        print(table(rows))
        metrics = _summary(rows[0], ("recall", "accuracy", "n")) + f" k {args.k or shown}"
        n, recall = rows[0]["answerable"], rows[0]["recall"]
        ci = None if recall is None else wilson(round(recall * n), n)
        args.dataset, sha = Path(args.qa).name, file_sha256(args.qa)
    try:
        ledger(args.ledger, args.stage, args.option, Path(args.dataset).name, metrics, n=n, ci=ci, sha256=sha)
    except ValueError as error:
        sys.exit(f"no ledger row: {error}")


if __name__ == "__main__":
    main()
