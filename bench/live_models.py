"""Live-turn model comparison: the kernel's own eval (evals/test_live_conversation.py), per model.

KERNEL is an exported commit of the kernel (git archive), so a work in progress is never measured.
The eval is run natively (not `make eval`'s container), against Ollama on 127.0.0.1:11434, twice per
model. A cold load (load_duration, from unloaded) is measured once. Each model is unloaded afterwards,
so the next one's runs don't share RAM with it.

    uv run python live_models.py /path/to/kernel-export model ...

The brain comparison (design 4-brain): every candidate, local or hosted, answers the same made-up lines
(brain_scenarios.json: questions, half-heard lines, things it cannot know, follow-ups) through the kernel's own
prompt and provider code, and is scored the same way. A hosted candidate is named anthropic:<model id>, needs
ANTHROPIC_API_KEY, never touches Ollama and so runs at any time; a local one waits for quiet as above.

    uv run python live_models.py --brain /path/to/kernel-export candidate ...
    uv run python live_models.py --verdict
"""
import glob
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".local/live_models.jsonl")
# The live bot's log: a run waits for its quiet and stops on a turn, so the owner never waits on a bench.
BOT_LOGS = os.path.expanduser("~/Library/Logs/reachy-sage/bot-*.log")
QUIET_S = int(os.environ.get("SAGE_BENCH_QUIET_S", 900))  # longer than one eval run: in practice, the owner away
KERNEL_MODEL = os.environ.get("SAGE_KERNEL_LIVE_MODEL", "qwen3:4b-instruct")  # reloaded after each model
SCENARIOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brain_scenarios.json")
BRAIN_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".local/brain.jsonl")
# ponytail: all four are the design's guesses, to fix before the first run is read. Hosted is adopted only if it
# beats the best local candidate by ADOPT_MARGIN points of answered-correctly, with no answer off the schema and
# its first piece inside both bars. Measure: run --brain twice per candidate; if two runs of one candidate differ
# by more than a third of the margin, raise RUNS before trusting a verdict.
ADOPT_MARGIN = 15
FIRST_P50_S, FIRST_P95_S = 1.0, 2.5
RUNS = int(os.environ.get("SAGE_BENCH_RUNS", "3"))  # answers per scenario: the local model samples at 0.7
# $ per million tokens (input, output), prompts up to 100k tokens: platform.claude.com/docs/en/about-claude/pricing,
# read 2026-11-22.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00)}
# ponytail: a word list, not a judge. Read the replies in .local/brain.jsonl before trusting a close call.
DONT_KNOW = re.compile(r"\b(i (do not|don't|dont) know|(i'm|i am) not sure|no idea|not sure)\b", re.IGNORECASE)
TURN = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\S* .*turn start")


def stamp(text):
    return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))


def turns(lines):
    return [stamp(m.group(1)) for m in map(TURN.match, lines) if m]


def quiet(lines, now, seconds):
    """No turn in the last ``seconds``."""
    return all(t <= now - seconds for t in turns(lines))


def turned_since(lines, since):
    return any(t > since for t in turns(lines))


def bot_log():
    logs = glob.glob(BOT_LOGS)
    if not logs:
        return []
    with open(max(logs, key=os.path.getmtime), errors="replace") as f:
        return f.readlines()[-4000:]


def ollama(path, body):
    req = urllib.request.Request(OLLAMA + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read(), strict=False)


def unload_all():
    with urllib.request.urlopen(OLLAMA + "/api/ps") as r:
        for m in json.load(r)["models"]:
            ollama("/api/generate", {"model": m["name"], "keep_alive": 0})


def cold_load(model):
    unload_all()
    return ollama("/api/generate", {"model": model, "prompt": "hi", "stream": False, "keep_alive": -1,
                                    "options": {"num_predict": 1, "num_ctx": 5120}})["load_duration"] / 1e9


def restore():
    """Only the kernel's model loaded again, warm, at the kernel's context."""
    unload_all()
    cold_load(KERNEL_MODEL)


def stop(p):
    """End ``p`` and everything it started: the eval runs its own kernel, which would go on asking."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    p.communicate()


def wait_quiet():
    while not quiet(bot_log(), time.time(), QUIET_S):
        time.sleep(15)


def parse(log):
    """Per check pass/fail, the eval's own summary line, and each turn's first-piece time."""
    checks = {}
    for name, result in re.findall(r"^(test_\w+) \(.*?\)\s*(?:\n.*?)?\.\.\. (ok|FAIL|ERROR)", log, re.M):
        checks[name] = result == "ok"
    for name in re.findall(r"^(?:FAIL|ERROR): (test_\w+)", log, re.M):  # a failed subtest marks its check
        checks[name] = False
    m = re.search(r"emote chosen (\d+)/(\d+)", log)
    firsts = [float(x) for x in re.findall(r"\(([\d.]+) s\)(?:  RESTATES THE LINE)?$", log, re.M)]
    restating = re.search(r"restating: (\d+)/", log)
    return checks, (int(m.group(1)), int(m.group(2))) if m else None, firsts, restating and int(restating.group(1))


def run(kernel, model):
    """The eval's log, or None if the owner spoke during it (it is stopped at once)."""
    env = {**os.environ, "SAGE_MODEL": model, "SAGE_EVAL_MODEL_URL": OLLAMA}
    began = time.time()
    p = subprocess.Popen(["uv", "run", "--no-project", "--python", "3.12", "python", "-m", "unittest", "discover",
                          "-s", "evals", "-v"], cwd=kernel, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, start_new_session=True)
    while p.poll() is None:
        if turned_since(bot_log(), began) or time.time() - began > 900:
            stop(p)
            return None
        time.sleep(2)
    return p.communicate()[0]


def score(scenario, output):
    """One answer against its scenario: on the schema, an "I don't know", and correct. A thing it cannot know is
    answered correctly by saying so; anything else by a reply one of the scenario's patterns matches."""
    try:
        reply = json.loads(output)["reply"]
        if not isinstance(reply, str):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        return {"schema": False, "dont_know": False, "correct": False}
    dont_know = bool(DONT_KNOW.search(reply))
    matched = any(re.search(pattern, reply, re.IGNORECASE) for pattern in scenario["expect"])
    return {"schema": True, "dont_know": dont_know,
            "correct": matched or (dont_know and scenario["kind"] == "unknowable")}


def answer_all(kernel, decide, scenarios, runs=RUNS, interrupted=lambda: False):
    """Each scenario, `runs` times, as the prompt the kernel at `kernel` compiles for the deployed voice body,
    answered by `decide`; None if `interrupted()` turned true (the owner spoke)."""
    sys.path.insert(0, os.path.abspath(kernel))
    from sage import manifest, policy
    with open(os.path.join(kernel, "tests", "voice_manifest.json")) as f:
        declared = manifest.check_manifest(json.load(f))
    results = []
    for scenario in scenarios:
        before = tuple(policy.Exchange(said, reply, device="bench") for said, reply in scenario.get("before", ()))
        state = policy.Companion(turn=policy.Turn("bench", "bench", "active", scenario["line"],
                                                  input=scenario.get("input", {"medium": "speech"})),
                                 exchanges=before, facts=policy.records(scenario.get("facts", ())))
        prompt = policy.compile_prompt(state, "bench", declared)
        for _ in range(runs):
            if interrupted():
                return None
            times = []
            began = time.perf_counter()
            try:
                output = decide(prompt, lambda piece, times=times: times.append(time.perf_counter()))
            except (OSError, ValueError) as error:  # a failed call is an answer off the schema, and is kept
                output = f"{type(error).__name__}: {error}"
            full = time.perf_counter() - began
            reply = score(scenario, output)
            results.append({"id": scenario["id"], "kind": scenario["kind"], **reply,
                            "reply": json.loads(output)["reply"] if reply["schema"] else output[:200],
                            "first_s": round((times[0] - began) if times else full, 3), "full_s": round(full, 3),
                            **({"timing": output.timing} if getattr(output, "timing", None) else {})})
    return results


def percent(part, whole):
    return round(100 * part / whole, 1) if whole else None


def p95(values):
    return sorted(values)[round(0.95 * (len(values) - 1))]


def summary(candidate, results):
    """The row the comparison is read from. The don't-know rate is over the lines that have an answer."""
    answerable = [r for r in results if r["kind"] != "unknowable"]
    kinds = list(dict.fromkeys(r["kind"] for r in results))
    firsts, fulls = [r["first_s"] for r in results], [r["full_s"] for r in results]
    price = PRICES.get(candidate.partition(":")[2])
    spent = [r["timing"] for r in results if "prompt_tokens" in r.get("timing", {})]
    cost = price and spent and statistics.mean(
        (t["prompt_tokens"] * price[0] + t.get("gen_tokens", 0) * price[1]) / 1e6 for t in spent)
    return {"candidate": candidate, "answers": len(results),
            "answered_correctly": percent(sum(r["correct"] for r in results), len(results)),
            "dont_know_rate": percent(sum(r["dont_know"] for r in answerable), len(answerable)),
            "schema_failures": sum(not r["schema"] for r in results),
            "by_kind": {k: percent(sum(r["correct"] for r in results if r["kind"] == k),
                                   sum(r["kind"] == k for r in results)) for k in kinds},
            "first_p50": round(statistics.median(firsts), 3), "first_p95": p95(firsts),
            "full_p50": round(statistics.median(fulls), 3), "full_p95": p95(fulls),
            **({"cost_per_turn_usd": cost} if cost else {})}


def verdict(rows):
    """Per hosted candidate: "adopt", or why not. The newest row of each candidate counts."""
    latest = {row["candidate"]: row for row in rows}
    local = [row["answered_correctly"] for name, row in latest.items() if not name.startswith("anthropic:")]
    out = {}
    for name, row in latest.items():
        if not name.startswith("anthropic:"):
            continue
        if not local:
            out[name] = "no local candidate has run yet"
        elif row["schema_failures"]:
            out[name] = f"{row['schema_failures']} answers off the schema"
        elif row["first_p50"] > FIRST_P50_S or row["first_p95"] > FIRST_P95_S:
            out[name] = (f"first piece p50 {row['first_p50']} s, p95 {row['first_p95']} s: over the bar of "
                         f"{FIRST_P50_S} s and {FIRST_P95_S} s")
        elif row["answered_correctly"] - max(local) < ADOPT_MARGIN:
            out[name] = (f"{row['answered_correctly']} against the best local {max(local)}: under the margin of "
                         f"{ADOPT_MARGIN} points")
        else:
            out[name] = "adopt"
    return out


def rows(path=BRAIN_OUT):
    with open(path) as f:
        return [json.loads(line)["row"] for line in f]


def brain(kernel, candidates):
    sys.path.insert(0, os.path.abspath(kernel))
    from sage.adapters import model as providers
    with open(SCENARIOS) as f:
        scenarios = json.load(f)
    os.makedirs(os.path.dirname(BRAIN_OUT), exist_ok=True)
    for candidate in candidates:
        if candidate.startswith("anthropic:"):  # made-up lines only; Ollama is never touched
            results = answer_all(kernel, providers.hosted(os.environ.get("ANTHROPIC_API_KEY", ""),
                                                          candidate.partition(":")[2]), scenarios)
        else:
            wait_quiet()
            cold_load(candidate)
            began = time.time()
            results = answer_all(kernel, providers.provider(OLLAMA, candidate), scenarios,
                                 interrupted=lambda began=began: turned_since(bot_log(), began))
            restore()
            if results is None:
                print(json.dumps({"candidate": candidate, "stopped": "owner turn"}), flush=True)
                continue
        row = summary(candidate, results)
        print(json.dumps(row), flush=True)
        with open(BRAIN_OUT, "a") as f:
            f.write(json.dumps({"row": row, "results": results}) + "\n")
    print(json.dumps(verdict(rows())), flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "--verdict":
        sys.exit(print(json.dumps(verdict(rows()), indent=1)))
    if sys.argv[1] == "--brain":
        sys.exit(brain(sys.argv[2], sys.argv[3:]))
    kernel, models = sys.argv[1], sys.argv[2:]
    for model in models:
        wait_quiet()
        cold = cold_load(model)
        for attempt in (1, 2):
            log = run(kernel, model)
            if log is None:  # the owner spoke: their model back first, then wait for quiet again
                print(json.dumps({"model": model, "run": attempt, "stopped": "owner turn"}), flush=True)
                restore()
                wait_quiet()
                cold_load(model)
                log = run(kernel, model)
                if log is None:
                    print(json.dumps({"model": model, "run": attempt, "stopped": "owner turn again"}), flush=True)
                    break
            checks, emote, firsts, restating = parse(log)
            firsts.sort()
            row = {"model": model, "run": attempt, "cold_load_s": round(cold, 2),
                   "passed": sum(checks.values()), "checks": len(checks), "failed": sorted(k for k, v in checks.items() if not v),
                   "emote": emote, "restating": restating,
                   "first_p50": round(statistics.median(firsts), 3) if firsts else None,
                   "first_p95": round(firsts[round(0.95 * (len(firsts) - 1))], 3) if firsts else None,
                   "turns_timed": len(firsts)}
            print(json.dumps(row), flush=True)
            with open(OUT, "a") as f:
                f.write(json.dumps({**row, "log": log}) + "\n")
        restore()
