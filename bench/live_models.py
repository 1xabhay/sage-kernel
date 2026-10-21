"""Live-turn model comparison: the kernel's own eval (evals/test_live_conversation.py), per model.

KERNEL is an exported commit of the kernel (git archive), so a work in progress is never measured.
The eval is run natively (not `make eval`'s container), against Ollama on 127.0.0.1:11434, twice per
model. A cold load (load_duration, from unloaded) is measured once. Each model is unloaded afterwards,
so the next one's runs don't share RAM with it.

    uv run python live_models.py /path/to/kernel-export model ...
"""
import json
import os
import re
import statistics
import subprocess
import sys
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".local/live_models.jsonl")


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
                                    "options": {"num_predict": 1}})["load_duration"] / 1e9


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
    env = {**os.environ, "SAGE_MODEL": model, "SAGE_EVAL_MODEL_URL": OLLAMA}
    p = subprocess.run(["uv", "run", "--no-project", "--python", "3.12", "python", "-m", "unittest", "discover",
                        "-s", "evals", "-v"], cwd=kernel, env=env, capture_output=True, text=True, timeout=900)
    return p.stdout + p.stderr


if __name__ == "__main__":
    kernel, models = sys.argv[1], sys.argv[2:]
    for model in models:
        cold = cold_load(model)
        for attempt in (1, 2):
            log = run(kernel, model)
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
        ollama("/api/generate", {"model": model, "keep_alive": 0})
