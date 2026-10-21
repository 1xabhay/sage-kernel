"""Phase 2 (run only when the lead says go): expressive local TTS on short pet replies, via MLX.

Engines (weights in bench/expressive/.local/hf, fetched in phase 1):
  dia         mlx-community/Dia-1.6B-fp16        nonverbal "(laughs)"; no streaming
  orpheus     mlx-community/orpheus-3b-0.1-ft-4bit  "<laugh>"; voice "tara"; streams
  turbo       mlx-community/chatterbox-turbo-fp16   "[laugh]", "[whispering]"...; streams
  chatterbox  mlx-community/Chatterbox-TTS-fp16     no tags; style = exaggeration / cfg_weight

Dia, Chatterbox and Turbo clone REF (Kokoro af_heart, so the character's voice stays the same
across engines). Make it first, from bench/:
    uv run python -c "import tts, soundfile as sf; k=tts.kokoro(); a,sr=k.create('Hello, I am your little desk robot, and I am always happy to see you.', voice='af_heart'); sf.write('.local/samples/ref_af_heart.wav', a, sr)"

Then:  uv run python run.py [engine ...]     -> bench/.local/samples/<engine>/*.wav + results.jsonl
TTFA = first audio chunk out (streaming engines, 0.5 s interval) or the whole clip (Dia).
"""
import json
import os
import re
import statistics
import sys
import time

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("HF_HOME", os.path.join(HERE, ".local/hf"))
SAMPLES = os.path.join(HERE, "..", ".local", "samples")
REF = os.path.join(SAMPLES, "ref_af_heart.wav")
REF_TEXT = "Hello, I am your little desk robot, and I am always happy to see you."

# Canonical pet markup: one optional {vocal} and a voice_style per reply.
REPLIES = (
    ("neutral", "Four. Even I can count that high."),
    ("warm", "I missed you too. It's been very quiet without you."),
    ("excited", "That's brilliant news! I knew you could do it."),
    ("soft", "Sleep well. I'll keep an eye on things here."),
    ("whisper", "Shh, the cat is asleep on your keyboard."),
    ("warm", "{laugh} Oh, that's a good one."),
    ("soft", "{sigh} Mondays are hard, aren't they?"),
    ("excited", "{gasp} Is that a new plant on your desk?"),
    ("neutral", "{hmm} Let me think about that for a second."),
)

# vocal -> each engine's tag; None = no tag, drop it. "hmm" is spoken as a word everywhere.
VOCAL = {
    "dia": {"laugh": "(laughs)", "sigh": "(sighs)", "gasp": "(gasps)", "hmm": "Hmm,"},
    "orpheus": {"laugh": "<laugh>", "sigh": "<sigh>", "gasp": "<gasp>", "hmm": "Hmm,"},
    "turbo": {"laugh": "[laugh]", "sigh": "[sigh]", "gasp": "[gasp]", "hmm": "Hmm,"},
    "chatterbox": {"laugh": None, "sigh": None, "gasp": None, "hmm": "Hmm,"},
}
# voice_style -> a leading tag (turbo) or generate kwargs (chatterbox). Others: no control.
STYLE_TAG = {"turbo": {"excited": "[happy]", "whisper": "[whispering]"}}
STYLE_KW = {"chatterbox": {"neutral": {"exaggeration": 0.5, "cfg_weight": 0.5},
                           "warm": {"exaggeration": 0.6, "cfg_weight": 0.4},
                           "excited": {"exaggeration": 0.9, "cfg_weight": 0.3},
                           "soft": {"exaggeration": 0.3, "cfg_weight": 0.6},
                           "whisper": {"exaggeration": 0.25, "cfg_weight": 0.6}}}

ENGINES = {
    "dia": ("mlx-community/Dia-1.6B-fp16", {"ref_audio": REF, "ref_text": "[S1] " + REF_TEXT}, False),
    "orpheus": ("mlx-community/orpheus-3b-0.1-ft-4bit", {"voice": "tara"}, True),
    "turbo": ("mlx-community/chatterbox-turbo-fp16", {"ref_audio": REF}, True),
    "chatterbox": ("mlx-community/Chatterbox-TTS-fp16", {"ref_audio": REF}, True),
}


def render(engine, style, text):
    """Canonical markup -> (engine text, extra kwargs)."""
    def vocal(m):
        return VOCAL[engine][m.group(1)] or ""

    out = re.sub(r"\{(\w+)\}\s*", lambda m: (vocal(m) + " ") if vocal(m) else "", text).strip()
    tag = STYLE_TAG.get(engine, {}).get(style)
    if tag:
        out = f"{tag} {out}"
    if engine == "dia":
        out = "[S1] " + out
    return out, STYLE_KW.get(engine, {}).get(style, {})


def run(engine, reps=3):
    from mlx_audio.tts.utils import load_model

    repo, kw, streams = ENGINES[engine]
    model = load_model(repo)
    out_dir = os.path.join(SAMPLES, engine)
    os.makedirs(out_dir, exist_ok=True)
    list(model.generate(text=render(engine, "neutral", "Warm up.")[0], **kw))
    rows = []
    for i, (style, text) in enumerate(REPLIES):
        said, extra = render(engine, style, text)
        for r in range(reps):
            t0 = time.perf_counter()
            first, chunks, sr = None, [], 24000
            stream_kw = {"stream": True, "streaming_interval": 0.5} if streams else {}
            for res in model.generate(text=said, **kw, **extra, **stream_kw):
                first = first or time.perf_counter() - t0
                chunks.append(np.asarray(res.audio, dtype=np.float32).ravel())
                sr = res.sample_rate
            total = time.perf_counter() - t0
            audio = np.concatenate(chunks)
            if r == 0:
                sf.write(os.path.join(out_dir, f"{i:02d}-{style}.wav"), audio, sr)
            rows.append({"engine": engine, "i": i, "style": style, "text": text, "said": said,
                         "ttfa": first, "total": total, "audio_s": len(audio) / sr})
    with open(os.path.join(SAMPLES, "results.jsonl"), "a") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    ttfa = sorted(r["ttfa"] for r in rows)
    print(json.dumps({"engine": engine, "n": len(rows), "ttfa_p50": round(statistics.median(ttfa), 3),
                      "ttfa_p95": round(ttfa[round(0.95 * (len(ttfa) - 1))], 3),
                      "rtf_p50": round(statistics.median(r["total"] / r["audio_s"] for r in rows), 3)}), flush=True)


if __name__ == "__main__":
    assert render("turbo", "whisper", "{laugh} Shh.") == ("[whispering] [laugh] Shh.", {})
    assert render("chatterbox", "excited", "{laugh} Oh.") == ("Oh.", STYLE_KW["chatterbox"]["excited"])
    assert render("dia", "neutral", "{hmm} Let me.")[0] == "[S1] Hmm, Let me."
    if sys.argv[1:] == ["check"]:
        sys.exit(0)
    assert os.path.exists(REF), "make the reference clip first (see the docstring)"
    for e in sys.argv[1:] or ENGINES:
        run(e)
