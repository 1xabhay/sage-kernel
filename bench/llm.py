"""LLM on Ollama's native /api/chat: latency and constrained-output reliability.

Modes, each over the same 8 pet cases x REPS:
  prose   the pet's current prompt ("EMOTION: <name>" line, then speech), streamed
  tools   one tool emote(name: enum[30], reply: str); valid = exactly one emote call
  format  `format` = JSON schema {emotion: enum[30], reply: str}, streamed
format_rf / format_gloss / format_best are harness variants (see
comments by their schemas). qwen3 models run with think=false. Router: a yes/no "addressed to the robot?"
helper on 20 SYNTHETIC labelled lines, via `format`.

ttft = first streamed chunk carrying content or a tool call. speak = when the
first speakable reply character exists (prose: first char after the EMOTION
line; format: first char inside "reply"; tools: the call only arrives whole, so
speak = total). Stdlib only; talks to localhost:11434.

    uv run python llm.py [model ...]
"""
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request

OLLAMA = "http://localhost:11434/api/chat"
MODELS = ("qwen2.5:7b", "qwen3:1.7b", "gemma3:1b", "qwen3:4b")
REPS = 3

# Copied from the Reachy Mini pet app's EMOTIONS
EMOTIONS = ("amazed1", "attentive1", "boredom1", "calming1", "cheerful1", "confused1", "curious1", "dance1",
            "enthusiastic1", "grateful1", "helpful1", "impatient1", "inquiring1", "laughing1", "loving1", "no1",
            "oops1", "proud1", "relief1", "sad1", "scared1", "serenity1", "shy1", "success1", "surprised1",
            "thoughtful1", "tired1", "understanding1", "welcoming1", "yes1")
assert len(EMOTIONS) == 30

# Copied from the pet app's bench: what a pet gets said to, and the fair feelings.
CASES = (
    ("good morning", {"welcoming1", "cheerful1", "attentive1", "enthusiastic1"}),
    ("I have had an absolutely awful day", {"sad1", "calming1", "understanding1"}),
    ("that is brilliant news", {"cheerful1", "enthusiastic1", "amazed1", "proud1"}),
    ("what's two plus two", {"attentive1", "helpful1", "thoughtful1", "cheerful1"}),
    ("asdkjh sdkjh garbled nonsense", {"confused1", "inquiring1", "curious1"}),
    ("I'm going to bed, goodnight", {"serenity1", "tired1", "calming1", "loving1"}),
    ("can you stop doing that", {"oops1", "understanding1", "sad1", "shy1"}),
    ("I missed you today", {"loving1", "grateful1", "welcoming1", "shy1"}),
)

PERSONA = ("You are a small desk robot, someone's pet and everyday assistant. You hear them through your own "
           "microphone, so what you receive is speech-to-text and may be misheard - if it is garbled, say so "
           "rather than inventing an answer. Speak the way a fond, unhurried friend talks out loud: one or two "
           "short sentences, no lists, no markdown, no emoji. It goes through a speech synthesiser.")
# The pet's current free-text contract (live.py SYSTEM, abridged to the same rules).
PROSE = (PERSONA + "\n\nAnswer in exactly this shape:\nEMOTION: <one name from the list>\n<what to say out loud>\n"
         "Choose from: " + ", ".join(EMOTIONS) + ". Never say the emotion name out loud.")
TOOLS_SYS = PERSONA + "\n\nAlways answer by calling the emote tool exactly once: your body language and what you say."
FORMAT_SYS = PERSONA + "\n\nAnswer as JSON: the emotion your body shows, and the reply you say out loud."

TOOL = {"type": "function", "function": {
    "name": "emote", "description": "Move your body to show a feeling, and say your reply out loud.",
    "parameters": {"type": "object", "required": ["name", "reply"], "properties": {
        "name": {"type": "string", "enum": list(EMOTIONS), "description": "the body-language move"},
        "reply": {"type": "string", "description": "one or two short spoken sentences"}}}}}
# Harness variants, each one change against the plain mode above:
# tools2: the argument is called `emotion`, not `name`. Qwen's tool-call JSON is
#   {"name": <function>, "arguments": ...}, so an argument called `name` makes the model put the
#   emotion where the function name goes, and Ollama silently drops calls to unknown functions.
TOOL2 = json.loads(json.dumps(TOOL).replace('"name"]', '"emotion"]').replace('"name": {', '"emotion": {'))
# format_gloss: one plain-words gloss per move, so a small model knows what each label is for.
GLOSS = {"amazed1": "something astonishing", "attentive1": "listening closely", "boredom1": "nothing happening",
         "calming1": "soothing someone upset", "cheerful1": "light happy chat", "confused1": "you didn't understand",
         "curious1": "something interesting", "dance1": "music, celebration", "enthusiastic1": "exciting plans",
         "grateful1": "being thanked or given something", "helpful1": "doing a task for them",
         "impatient1": "waiting too long", "inquiring1": "asking them a question", "laughing1": "a joke",
         "loving1": "affection", "no1": "refusing", "oops1": "you made a mistake", "proud1": "their achievement",
         "relief1": "a worry is over", "sad1": "bad news", "scared1": "something alarming",
         "serenity1": "calm, bedtime", "shy1": "a compliment", "success1": "a task done", "surprised1": "unexpected",
         "thoughtful1": "a question needing thought", "tired1": "late, sleepy", "understanding1": "empathy, apology accepted",
         "welcoming1": "greeting, hello", "yes1": "agreeing"}
assert set(GLOSS) == set(EMOTIONS)
GLOSS_SYS = FORMAT_SYS + " Emotions: " + "; ".join(f"{k} = {v}" for k, v in GLOSS.items()) + "."

SCHEMA = {"type": "object", "required": ["emotion", "reply"], "properties": {
    "emotion": {"type": "string", "enum": list(EMOTIONS)}, "reply": {"type": "string"}}}
# format_rf: reply first, so speech can start before the emotion is chosen (emotion lands ~5 tokens later).
SCHEMA_RF = {"type": "object", "required": ["reply", "emotion"], "properties": {
    "reply": {"type": "string"}, "emotion": {"type": "string", "enum": list(EMOTIONS)}}}
# format_best: reply first + glosses + a grammar `pattern` on the reply that admits plain spoken words only (no digits, so
# no "loving1" label; numbers come out as words, which TTS wants anyway),
# so emoji, markdown and JSON cannot be decoded at all (Ollama honours `pattern` in `format`).
SCHEMA_BEST = json.loads(json.dumps(SCHEMA_RF))
SCHEMA_BEST["properties"]["reply"]["pattern"] = "^[A-Za-z ,.!?'-]+$"

EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
LABEL = re.compile(r"\b(" + "|".join(EMOTIONS) + r")\b|EMOTION\s*:|\*[^*]+\*|\{|\}", re.I)


def leaks(reply):
    """Spoken text that must not be spoken: a label, an emoji, a stage direction, JSON."""
    return bool(EMOJI.search(reply) or LABEL.search(reply))


def call(body):
    req = urllib.request.Request(OLLAMA, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=180)


def base(model, system, user, keep):
    b = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
         "keep_alive": keep, "options": {"temperature": 0.7, "num_predict": 200}}
    if model.startswith("qwen3"):
        b["think"] = False
    return b


def stream(body):
    """(ttft, speak, total, content, tool_calls, thinking, clause) from a streamed /api/chat.

    clause = when the first clause of the reply is complete (a , . ! ? or the reply's end): what a
    first-clause TTS can start synthesising."""
    body["stream"] = True
    t0 = time.perf_counter()
    ttft = speak = clause = None
    content, calls, thinking = "", [], ""
    with call(body) as r:
        for line in r:
            m = json.loads(line).get("message", {})
            content += m.get("content", "")
            thinking += m.get("thinking", "") or ""
            calls += m.get("tool_calls", []) or []
            now = time.perf_counter() - t0
            if ttft is None and (content or calls):
                ttft = now
            if speak is None and speakable(body, content):
                speak = now
            if clause is None and clause_done(body, content):
                clause = now
    total = time.perf_counter() - t0
    return ttft or total, speak or total, total, content, calls, thinking, clause or total


def clause_done(body, content):
    if "tools" in body:
        return False
    if "format" in body:
        return bool(re.search(r'"reply"\s*:\s*"[^"]*?([,.!?]|")', content))
    return bool(re.search(r"EMOTION\s*:[^\n]*\n[^\n]*?[,.!?]", content))


def speakable(body, content):
    if "tools" in body:
        return False  # Ollama emits a tool call whole, at the end
    if "format" in body:
        m = re.search(r'"reply"\s*:\s*"(.)', content)
        return bool(m)
    return bool(re.search(r"EMOTION\s*:[^\n]*\n\s*\S", content))


def run_case(model, mode, user, keep):
    if mode == "prose":
        b = base(model, PROSE, user, keep)
    elif mode == "tools":
        b = base(model, TOOLS_SYS, user, keep) | {"tools": [TOOL]}
    elif mode == "tools2":
        b = base(model, TOOLS_SYS, user, keep) | {"tools": [TOOL2]}
    elif mode == "format_rf":
        b = base(model, FORMAT_SYS, user, keep) | {"format": SCHEMA_RF}
    elif mode == "format_best":
        b = base(model, GLOSS_SYS, user, keep) | {"format": SCHEMA_BEST}
    elif mode == "format_gloss":
        b = base(model, GLOSS_SYS, user, keep) | {"format": SCHEMA}
    else:
        b = base(model, FORMAT_SYS, user, keep) | {"format": SCHEMA}
    ttft, speak, total, content, calls, thinking, clause = stream(b)
    emotion, reply, valid = None, "", False
    if mode == "prose":
        m = re.match(r"\s*EMOTION\s*:\s*(\S+)\s*\n(.*)", content, re.S)
        if m and m.group(1).strip("*.,") in EMOTIONS:
            emotion, reply, valid = m.group(1).strip("*.,"), m.group(2).strip(), True
        else:
            reply = content
    elif mode.startswith("tools"):
        if len(calls) == 1 and calls[0]["function"]["name"] == "emote":
            args = calls[0]["function"].get("arguments") or {}
            emotion, reply = args.get("name", args.get("emotion")), args.get("reply") or ""
            valid = emotion in EMOTIONS and isinstance(reply, str) and bool(reply.strip())
        reply = reply or content
    else:
        try:
            obj = json.loads(content)
            emotion, reply = obj.get("emotion"), obj.get("reply") or ""
            valid = emotion in EMOTIONS and isinstance(reply, str) and bool(reply.strip())
        except (json.JSONDecodeError, AttributeError):
            reply = content
    return {"ttft": ttft, "speak": speak, "clause": clause, "total": total, "valid": valid, "emotion": emotion,
            "reply": reply, "leak": leaks(reply),
            "thinking": len(thinking)}


ROUTER_SYS = ("You are a router for a desk robot that hears everything said in a room. Decide if the utterance "
              "is addressed to the robot (yes) or is someone talking to another person, on the phone, to "
              "themselves, or background media (no). Answer JSON.")
# router_fewshot: the same prompt plus six worked examples (none of them in the test lines).
ROUTER_SHOTS = (("What's the capital of France?", "yes"), ("Hey, can you nod for me?", "yes"),
                ("Dad, where's the charger?", "no"), ("I'll call you back after lunch, bye.", "no"),
                ("Are you listening?", "yes"), ("Why is this spreadsheet so slow.", "no"))
ROUTER_FEWSHOT_SYS = (ROUTER_SYS + " A request or question with no other person named is for the robot. "
                      "Examples: " + " ".join(f'"{u}" -> {a}.' for u, a in ROUTER_SHOTS))
ROUTER_SCHEMA = {"type": "object", "required": ["addressed"], "properties": {"addressed": {"enum": ["yes", "no"]}}}
# SYNTHETIC: written for this bench, not recorded. Label = addressed to the robot?
ROUTER_LINES = (
    ("Hey robot, what time is it?", "yes"), ("Can you look over here for a second?", "yes"),
    ("Little guy, are you awake?", "yes"), ("Set a timer for ten minutes please.", "yes"),
    ("What do you think of my new lamp, robot?", "yes"), ("Good morning, buddy, did you miss me?", "yes"),
    ("Robot, stop moving, I'm on a call.", "yes"), ("Tell me a joke.", "yes"),
    ("Do you remember what I asked you yesterday?", "yes"), ("Look at me when I'm talking to you, little robot.", "yes"),
    ("Mum, I'll be down for dinner in five minutes.", "no"), ("Yeah, I can send you the slides after the meeting.", "no"),
    ("Where on earth did I put my keys.", "no"), ("And now, the weather for the south east.", "no"),
    ("Honey, did you feed the cat?", "no"), ("Okay team, let's go round and do updates.", "no"),
    ("Ugh, this code still doesn't compile.", "no"), ("No, I said the blue one, not the green one.", "no"),
    ("Sorry, can you repeat that? The line is bad.", "no"), ("Hmm, I think I'll have pasta tonight.", "no"),
)


def router(model, keep, system=ROUTER_SYS):
    out = []
    for _ in range(2):
        for line, label in ROUTER_LINES:
            b = base(model, system, line, keep) | {"format": ROUTER_SCHEMA, "stream": False}
            b["options"] = {"temperature": 0, "num_predict": 20}
            t0 = time.perf_counter()
            with call(b) as r:
                content = json.load(r)["message"]["content"]
            dt = time.perf_counter() - t0
            try:
                got = json.loads(content).get("addressed")
            except json.JSONDecodeError:
                got = None
            out.append((got == label, dt, line, label, got))
    return out


def pct(xs, p):
    return sorted(xs)[min(len(xs) - 1, round(p / 100 * (len(xs) - 1)))]


def bench(model):
    # qwen2.5:7b is the running pet's model: keep its 1 h keep-alive, so the bench never shortens it.
    keep = "1h" if model == "qwen2.5:7b" else "3m"
    t0 = time.perf_counter()
    with call(base(model, "hi", "hi", keep) | {"stream": False}) as r:
        json.load(r)
    yield {"model": model, "warm_s": round(time.perf_counter() - t0, 2)}
    for mode in os.environ.get("BENCH_MODES", "prose tools tools2 format format_rf format_gloss format_best").split():
        try:
            rs = [run_case(model, mode, u, keep) | {"ok": e} for u, e in CASES for _ in range(REPS)]
        except urllib.error.HTTPError as err:
            yield {"model": model, "mode": mode, "error": f"{err.code} {err.read().decode()[:200]}"}
            continue
        yield {"model": model, "mode": mode, "n": len(rs), "valid": sum(r["valid"] for r in rs),
               "leaks": sum(r["leak"] for r in rs),
               "fair_emotion": sum(r["valid"] and r["emotion"] in r["ok"] for r in rs),
               "ttft_p50": round(statistics.median(r["ttft"] for r in rs), 3),
               "speak_p50": round(statistics.median(r["speak"] for r in rs), 3),
               "speak_p95": round(pct([r["speak"] for r in rs], 95), 3),
               "clause_p50": round(statistics.median(r["clause"] for r in rs), 3),
               "clause_p95": round(pct([r["clause"] for r in rs], 95), 3),
               "total_p50": round(statistics.median(r["total"] for r in rs), 3),
               "total_p95": round(pct([r["total"] for r in rs], 95), 3),
               "thinking_chars": sum(r["thinking"] for r in rs),
               "words_p50": statistics.median(len(r["reply"].split()) for r in rs),
               "samples": [(r["emotion"], r["reply"][:90]) for r in rs[::REPS]],
               "bad": [(r["emotion"], r["reply"][:120]) for r in rs if r["leak"] or not r["valid"]][:5]}
    for name, system in (("router", ROUTER_SYS), ("router_fewshot", ROUTER_FEWSHOT_SYS)):
        if os.environ.get("BENCH_MODES") and name not in os.environ["BENCH_MODES"]:
            continue
        yield router_row(model, name, router(model, keep, system))


def router_row(model, name, rt):
    return {"model": model, "mode": name, "n": len(rt), "correct": sum(r[0] for r in rt),
           "p50": round(statistics.median(r[1] for r in rt), 3), "p95": round(pct([r[1] for r in rt], 95), 3),
           "wrong": sorted({(r[2], r[3], r[4]) for r in rt if not r[0]})}


if __name__ == "__main__":
    assert leaks("EMOTION: cheerful1 hi") and leaks("hi \U0001F600") and leaks("*wiggles*") and not leaks("Hello there.")
    for m in sys.argv[1:] or MODELS:
        for row in bench(m):
            print(json.dumps(row), flush=True)
