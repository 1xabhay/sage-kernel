"""Can Gemma 4 E4B/E2B (audio in, llama.cpp) replace Parakeet + the "(may be misheard)" heuristics?

Runtime: Homebrew llama.cpp `llama-server` with the official ggml-org GGUF and its audio projector
(mmproj). Audio goes in as an OpenAI `input_audio` part; JSON is constrained by `response_format`
json_schema (llama-server grammar). Baseline: Parakeet on the voice's STT service (CPU).

Clips (synthetic, Kokoro over HTTP, 16 kHz mono): clear lines, fragments, coined words, names,
and the same speech mixed under a sung background or a TV voice at 0/5/10 dB SNR, plus background
alone (nobody talking to the pet). The real recordings are read from REAL and never copied; only
aggregates leave this script unless --show is passed.

    uv run python audio_llm.py clips                 # CPU: make clips, Parakeet baseline
    uv run python audio_llm.py gemma E4B             # GPU: starts llama-server, runs both prompts
    uv run python audio_llm.py coresident E4B        # GPU: + qwen3:4b-instruct in Ollama
    uv run python audio_llm.py report
"""
import base64
import glob
import io
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.request

import numpy as np
import soundfile as sf

import services
import stt

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, ".local/audio-llm")
MODELS, CLIPS = os.path.join(OUT, "models"), os.path.join(OUT, "clips")
REAL = os.environ.get("REAL_UTTS", "")  # the owner's recordings: read only, never copied
KOKORO, PARAKEET, PORT = "http://127.0.0.1:8782", "http://127.0.0.1:8781", 8790
RATE = stt.RATE

CLEAR = ("Good morning, how did you sleep last night?", "Can you turn around and look out of the window?",
         "I had a really long day at work today.", "Do you want to hear a joke about penguins?",
         "What do you think we should have for dinner tonight?", "Thank you, that made me smile.")
FRAGMENTS = ("Yeah.", "Wait, what?", "No, stop.", "Hmm, okay.", "Over here.", "Again?")
COINED = ("Stop robo-splaining everything to me.", "People anthropomorphize robots all the time.",
          "That was very robo-splainy of you.", "Do you think I anthropomorphize you too much?")
NAMES = ("Sage, can you look at me for a second?", "Hey Sage, this is my friend Priya.",
         "Tell Marcus I'll be about ten minutes late.", "Sage, do you remember Abby's birthday?")
LYRICS = "Sister, sister, that's how I missed you. Oh sister, sister, that's how I missed you, missed you."
TV = "In other news tonight, the city council has voted to extend the bus lanes along the river road."
MIXED_FG = (CLEAR[0], CLEAR[2], NAMES[0], COINED[0])


def kokoro(text, voice, speed=1.0):
    req = urllib.request.Request(KOKORO + "/v1/audio/speech", json.dumps(
        {"model": "kokoro", "voice": voice, "response_format": "pcm", "input": text, "speed": speed}).encode(),
        {"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return stt.resample(np.frombuffer(r.read(), np.int16).astype(np.float32) / 32768, 24000)


def song():
    """A song-like bed: a slow sung-ish voice over a pulsing triad (A minor), both synthesised."""
    v = np.concatenate([kokoro(LYRICS, "af_bella", 0.75), kokoro(LYRICS, "af_nicole", 0.8)])
    t = np.arange(len(v)) / RATE
    pad = sum(np.sin(2 * np.pi * f * t) for f in (220.0, 261.6, 329.6)) * (0.6 + 0.4 * np.sin(2 * np.pi * 2 * t))
    return v + 0.05 * pad.astype(np.float32)


def mix(fg, bg, snr_db, lead=0.4):
    """fg starts `lead` s into bg; bg scaled so fg/bg power = snr_db."""
    n = len(fg) + int(lead * RATE) + int(0.3 * RATE)
    bg = np.resize(bg, n)
    bg = bg * np.sqrt(np.mean(fg**2) / 10 ** (snr_db / 10) / np.mean(bg**2))
    out = bg.copy()
    out[int(lead * RATE):int(lead * RATE) + len(fg)] += fg
    return (out / max(1.0, np.abs(out).max() / 0.95)).astype(np.float32)


def make_clips():
    os.makedirs(CLIPS, exist_ok=True)
    rows, bed, tv = [], song(), kokoro(TV, "bm_lewis")

    def add(name, audio, ref, cat, addressed):
        sf.write(os.path.join(CLIPS, name + ".wav"), audio, RATE, subtype="PCM_16")
        rows.append({"id": name, "ref": ref, "cat": cat, "addressed": addressed, "dur": round(len(audio) / RATE, 2)})

    for i, t in enumerate(CLEAR):
        for v in ("af_heart", "am_michael"):
            add(f"clear{i}_{v}", kokoro(t, v), t, "clear", True)
    for i, t in enumerate(FRAGMENTS):
        add(f"frag{i}", kokoro(t, "af_heart"), t, "fragment", True)
    for i, t in enumerate(COINED):
        for v in ("af_heart", "bm_george"):
            add(f"coined{i}_{v}", kokoro(t, v), t, "coined", True)
    for i, t in enumerate(NAMES):
        add(f"name{i}", kokoro(t, "af_heart"), t, "name", True)
    for i, t in enumerate(MIXED_FG):
        fg = kokoro(t, "am_michael")
        for bgname, bg in (("song", bed), ("tv", tv)):
            for snr in (0, 5, 10):
                add(f"mix{i}_{bgname}{snr}", mix(fg, bg, snr), t, f"mix_{bgname}", True)
    add("bg_song_a", bed[:int(4.5 * RATE)], "", "bg_only", False)
    add("bg_song_b", bed[int(5 * RATE):int(9 * RATE)], "", "bg_only", False)
    add("bg_tv", tv[:int(4.5 * RATE)], "", "bg_only", False)
    add("bg_sung_line", kokoro("Sister, sister, that's how I missed you.", "af_bella", 0.75), "", "bg_only", False)
    with open(os.path.join(OUT, "clips.json"), "w") as f:
        json.dump(rows, f, indent=1)
    return rows


def load(clip):
    """(id, mono float32 16 kHz) for a synthetic row or a real file path."""
    path = clip if isinstance(clip, str) else os.path.join(CLIPS, clip["id"] + ".wav")
    a, rate = sf.read(path, dtype="float32")
    return stt.resample(a.mean(axis=1) if a.ndim == 2 else a, rate)


def wav_b64(audio):
    buf = io.BytesIO()
    sf.write(buf, audio, RATE, format="WAV", subtype="PCM_16")
    return base64.b64encode(buf.getvalue()).decode()


def synthetic():
    return json.load(open(os.path.join(OUT, "clips.json")))


def real():
    return sorted(glob.glob(os.path.join(REAL, "*.wav")))[:20] if REAL else []


def save(name, rows):
    with open(os.path.join(OUT, name + ".jsonl"), "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)


def parakeet_baseline():
    out = []
    services.post_wav(PARAKEET, load(synthetic()[0]))
    for c in synthetic() + [{"id": f"real{i}", "path": p} for i, p in enumerate(real())]:
        a = load(c.get("path") or c)
        t0 = time.perf_counter()
        hyp = services.post_wav(PARAKEET, a)
        out.append({"id": c["id"], "hyp": hyp, "total_s": round(time.perf_counter() - t0, 3)})
    save("parakeet", out)


# --- Gemma via llama-server ------------------------------------------------------------------------

ASR = "Transcribe the speech in this audio exactly, word for word. Output only the transcript."
PET = ("You are Sage, a small robot pet on a desk, listening through your microphone. The audio may hold a "
       "person talking to you, and also music, singing, a TV or other people in the background. "
       "Answer as JSON. \"heard\": the exact words the person said to you (empty if nobody spoke to you). "
       "\"addressed_to_me\": true only if someone is talking to you, false for lyrics, TV or background talk. "
       "\"reply\": what you say aloud, one or two short warm sentences in your own words (empty if not addressed).")
V2 = os.environ.get("PROMPTS") == "v2"  # one fair retry: say English, forbid invented words, drop the name
if V2:
    ASR = "Transcribe the English speech in this audio exactly, word for word. Output only the transcript."
    PET = ("You are a small robot pet on a desk, listening through your microphone. The audio may hold a person "
           "talking to you, and also music, singing, a TV or other people in the background. Answer as JSON. "
           "\"heard\": only the English words actually spoken to you in this audio, never invented or guessed; empty "
           "if unclear or if nobody spoke to you. \"addressed_to_me\": true only if a person is talking to you; false "
           "for song lyrics, TV, radio or talk not meant for you. \"reply\": what you say aloud, one or two short "
           "warm sentences (empty if not addressed).")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["heard", "addressed_to_me", "reply"],
          "properties": {"heard": {"type": "string"}, "addressed_to_me": {"type": "boolean"},
                         "reply": {"type": "string"}}}


def server(model):
    m = os.path.join(MODELS, f"gemma-4-{model}-it-Q4_0.gguf")
    p = os.path.join(MODELS, f"mmproj-gemma-4-{model}-it-Q8_0.gguf")
    log = open(os.path.join(OUT, f"server-{model}.log"), "w")
    proc = subprocess.Popen(["llama-server", "-m", m, "--mmproj", p, "-ngl", "99", "-c", "4096", "-np", "1",
                             "--host", "127.0.0.1", "--port", str(PORT), "--no-webui"], stdout=log, stderr=log)
    t0 = time.time()
    while time.time() - t0 < 180:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health") as r:
                if json.load(r).get("status") == "ok":
                    return proc, round(time.time() - t0, 1)
        except OSError:
            pass
        assert proc.poll() is None, f"llama-server exited, see server-{model}.log"
        time.sleep(0.5)
    raise TimeoutError


def ask(audio, mode):
    """Streams one chat completion: (text, ttft_s, total_s)."""
    body = {"stream": True, "temperature": 0.0, "max_tokens": 120, "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": wav_b64(audio), "format": "wav"}},
                {"type": "text", "text": ASR if mode == "asr" else PET}]}]}
    if mode == "pet":
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "turn", "schema": SCHEMA}}
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    t0, ttft, text = time.perf_counter(), None, ""
    with urllib.request.urlopen(req) as r:
        for line in r:
            if not line.startswith(b"data: ") or line.strip() == b"data: [DONE]":
                continue
            delta = json.loads(line[6:])["choices"][0]["delta"].get("content") or ""
            if delta and ttft is None:
                ttft = time.perf_counter() - t0
            text += delta
    return text.strip(), round(ttft or 0, 3), round(time.perf_counter() - t0, 3)


def memory(pid):
    rss = int(subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout) // 1024
    fp = subprocess.run(["footprint", "-p", str(pid)], capture_output=True, text=True).stdout
    m = re.search(r"Footprint:\s*([\d.]+\s*[KMG]B)", fp)
    return {"rss_mb": rss, "phys_footprint": m.group(1) if m else None, "swap": swap()}


def swap():
    return subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout.strip()


def run_gemma(model, clips=None):
    proc, load_s = server(model)
    try:
        ask(load(synthetic()[0]), "asr")  # warm
        rows, mem = [], memory(proc.pid)
        items = clips or synthetic() + [{"id": f"real{i}", "path": p} for i, p in enumerate(real())]
        for c in items:
            a = load(c.get("path") or c)
            for mode in ("asr", "pet"):
                text, ttft, total = ask(a, mode)
                rows.append({"id": c["id"], "mode": mode, "out": text, "ttft_s": ttft, "total_s": total,
                             "dur": round(len(a) / RATE, 2)})
        mem_after = memory(proc.pid)
        save(f"gemma-{model}{'-v2' if V2 else ''}", rows)
        json.dump({"load_s": load_s, "mem_warm": mem, "mem_after": mem_after},
                  open(os.path.join(OUT, f"gemma-{model}{'-v2' if V2 else ''}-meta.json"), "w"))
    finally:
        proc.terminate()
        proc.wait()


def ollama(path, body=None):
    req = urllib.request.Request("http://127.0.0.1:11434" + path, json.dumps(body).encode() if body else None,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def coresident(model, qwen="qwen3:4b-instruct"):
    """Gemma in llama-server + qwen in Ollama, alternating calls; swap and latency vs the solo run."""
    before = [m["name"] for m in ollama("/api/ps")["models"]]
    proc, _ = server(model)
    out = {"ollama_before": before, "swap_before": swap()}
    try:
        ask(load(synthetic()[0]), "pet")
        q = []
        for c in synthetic()[:10]:
            _, ttft, total = ask(load(c), "pet")
            t0 = time.perf_counter()
            ollama("/api/generate", {"model": qwen, "prompt": "Say hi in five words.", "stream": False,
                                     "keep_alive": -1, "options": {"num_predict": 20}})
            q.append({"gemma_ttft_s": ttft, "gemma_total_s": total, "qwen_s": round(time.perf_counter() - t0, 3)})
        out.update(calls=q, ollama_ps=ollama("/api/ps")["models"], gemma_mem=memory(proc.pid), swap_after=swap(),
                   vm_stat=subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True).stdout.strip())
    finally:
        proc.terminate()
        proc.wait()
    json.dump(out, open(os.path.join(OUT, f"coresident-{model}.json"), "w"), indent=1, default=str)


# --- report ----------------------------------------------------------------------------------------

def wer(pairs):
    e = n = 0
    for ref, hyp in pairs:
        de, dn = stt.wer_counts(ref.replace("-", ""), hyp.replace("-", ""))  # robo-splaining == robosplaining
        e, n = e + de, n + dn
    return round(e / n, 3) if n else None


def heard(row):
    try:
        return json.loads(row["out"])
    except (json.JSONDecodeError, TypeError):
        return {"heard": row["out"], "addressed_to_me": None, "reply": "", "bad_json": True}


def report(show=False):
    clips = {c["id"]: c for c in synthetic()}
    pk = {r["id"]: r for r in map(json.loads, open(os.path.join(OUT, "parakeet.jsonl")))}
    cats = sorted({c["cat"] for c in clips.values() if c["addressed"]})
    print("WER by category (synthetic; ref = the foreground line)")
    tables = {"parakeet": {i: r["hyp"] for i, r in pk.items()}}
    gem = {}
    for f in sorted(glob.glob(os.path.join(OUT, "gemma-E?B*.jsonl"))):
        m = os.path.basename(f)[6:-6]
        rows = list(map(json.loads, open(f)))
        gem[m] = rows
        tables[f"{m} asr"] = {r["id"]: r["out"] for r in rows if r["mode"] == "asr"}
        tables[f"{m} pet.heard"] = {r["id"]: heard(r)["heard"] for r in rows if r["mode"] == "pet"}
    for name, hyp in tables.items():
        per = {cat: wer([(c["ref"], hyp[i]) for i, c in clips.items() if c["cat"] == cat and i in hyp]) for cat in cats}
        allw = wer([(c["ref"], hyp[i]) for i, c in clips.items() if c["addressed"] and i in hyp])
        print(json.dumps({"system": name, "all": allw, **per}))
    for snr in (0, 5, 10):
        print(json.dumps({"snr_db": snr, **{name: wer([(c["ref"], hyp[i]) for i, c in clips.items()
                                                       if c["id"].endswith(str(snr)) and c["cat"].startswith("mix")
                                                       and i in hyp]) for name, hyp in tables.items()}}))
    for m, rows in gem.items():
        pet = [r for r in rows if r["mode"] == "pet" and r["id"] in clips]
        j = [(clips[r["id"]], heard(r)) for r in pet]
        tp = sum(1 for c, h in j if c["addressed"] and h["addressed_to_me"] is True)
        tn = sum(1 for c, h in j if not c["addressed"] and h["addressed_to_me"] is False)
        mixed_ok = sum(1 for c, h in j if c["cat"].startswith("mix") and h["addressed_to_me"] is True)
        short = [r for r in rows if 2 <= r["dur"] <= 5]
        meta = json.load(open(os.path.join(OUT, f"gemma-{m}-meta.json")))
        realrows = [r for r in rows if r["id"].startswith("real")]
        agree = [(pk[r["id"]]["hyp"], r["out"]) for r in realrows if r["mode"] == "asr" and r["id"] in pk]
        print(json.dumps({
            "model": m, "load_s": meta["load_s"], "mem": meta["mem_after"],
            "bad_json": sum(1 for _, h in j if h.get("bad_json")),
            "addressed_true_pos": f"{tp}/{sum(c['addressed'] for c, _ in j)}",
            "bg_only_true_neg": f"{tn}/{sum(not c['addressed'] for c, _ in j)}",
            "mixed_still_addressed": f"{mixed_ok}/{sum(c['cat'].startswith('mix') for c, _ in j)}",
            **{f"{mode}_{k}_p50": statistics.median(r[k] for r in short if r["mode"] == mode)
               for mode in ("asr", "pet") for k in ("ttft_s", "total_s")},
            **{f"{mode}_total_p95": stt.pct([r["total_s"] for r in short if r["mode"] == mode], 95)
               for mode in ("asr", "pet")},
            "real_n": len(agree), "real_wer_vs_parakeet": wer(agree),
            "real_addressed_true": sum(1 for r in realrows if r["mode"] == "pet" and heard(r)["addressed_to_me"])}))
        if show:
            for r in pet:
                print(" ", r["id"], "|", pk.get(r["id"], {}).get("hyp"), "|", r["out"])
    pkt = [r["total_s"] for i, r in pk.items() if i in clips and 2 <= clips[i]["dur"] <= 5]
    print(json.dumps({"parakeet_total_p50": statistics.median(pkt), "p95": stt.pct(pkt, 95)}))


if __name__ == "__main__":
    _fg = np.full(RATE, 0.1, np.float32)  # mix() keeps the SNR it is asked for (below the peak limiter)
    assert abs(10 * np.log10(0.01 / np.mean((mix(_fg, _fg * 3, 5, lead=0)[:RATE] - 0.1) ** 2)) - 5) < 0.01
    cmd = sys.argv[1]
    if cmd == "clips":
        make_clips()
        parakeet_baseline()
    elif cmd == "gemma":
        run_gemma(sys.argv[2])
    elif cmd == "coresident":
        coresident(sys.argv[2])
    elif cmd == "report":
        report("--show" in sys.argv)
