"""Phase 2 tag adherence, the part a program can check: transcribe each expressive sample with
Parakeet and flag a tag that was *read out* ("laugh", "whispering") instead of performed, plus the
WER of the words themselves. Whether a laugh actually laughs is for the ear (the WAVs).

    uv run python adherence.py      # reads .local/samples/results.jsonl from expressive/run.py
"""
import json
import os
import re

import soundfile as sf

import stt

SAMPLES = os.path.join(stt.tts.HERE, ".local/samples")
TAG_WORDS = re.compile(r"\b(laughs?|sighs?|gasps?|whisper(ing)?|happy|chuckles?)\b", re.I)


if __name__ == "__main__":
    asr = stt.parakeet()
    seen = set()
    for line in open(os.path.join(SAMPLES, "results.jsonl")):
        row = json.loads(line)
        key = (row["engine"], row["i"])
        if key in seen:
            continue
        seen.add(key)
        audio, rate = sf.read(os.path.join(SAMPLES, row["engine"], f"{row['i']:02d}-{row['style']}.wav"),
                              dtype="float32", always_2d=True)
        heard = asr(stt.resample(audio.mean(axis=1), rate))
        words = re.sub(r"\{\w+\}", "", row["text"].replace("{hmm}", "Hmm,"))
        spoken_tag = bool(TAG_WORDS.search(heard)) and not TAG_WORDS.search(words)
        e, n = stt.wer_counts(words, heard)
        print(json.dumps({**{k: row[k] for k in ("engine", "i", "style", "text")}, "heard": heard,
                          "tag_read_aloud": spoken_tag, "wer": round(e / n, 3)}))
