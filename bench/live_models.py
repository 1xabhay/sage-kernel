"""Live-turn model comparison: the kernel's own eval (evals/test_live_conversation.py), per model.

KERNEL is an exported commit of the kernel (git archive), so a work in progress is never measured.
The eval is run natively (not `make eval`'s container), against Ollama on 127.0.0.1:11434, twice per
model. A cold load (load_duration, from unloaded) is measured once. Each model is unloaded afterwards,
so the next one's runs don't share RAM with it.

    uv run python live_models.py /path/to/kernel-export model ...
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


if __name__ == "__main__":
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
