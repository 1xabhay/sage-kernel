"""The decision provider: Ollama's native /api/chat, streamed NDJSON, thinking off, answer constrained by `format`.

Standard library only. The answer is the model's raw JSON; the kernel decodes and checks it.
"""

import json
import urllib.request
from urllib.parse import urlparse

from sage.policy import CompiledPrompt

LOCAL = ("localhost", "127.0.0.1", "::1", "host.docker.internal")
TIMEOUT_S = 30
MAX_ANSWER = 65536
# bench/LEDGER.md: num_predict 200 for the format_best harness; keep the live model loaded.
# Temperature 0.4 (owner, 2026-10-20; the bench ran 0.7).
# num_ctx capped: Ollama's default context can take ~15 GB on this 16 GB Mac. The live prompt is ~3k tokens.
OPTIONS = {"temperature": 0.4, "num_predict": 200, "num_ctx": 4096}
# Per model family, by name prefix. Qwen3's card: near-greedy decoding repeats endlessly; these are its
# non-thinking settings (live run 5b looped "I'm here for you." at 0.4 with no penalty).
FAMILY = {"qwen3": {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5}}
KEEP_ALIVE = -1
# Ollama's own counts and durations (ns), sent on the final chunk, as journaled.
TIMING = {"prompt_tokens": "prompt_eval_count", "prefill_ms": "prompt_eval_duration", "gen_tokens": "eval_count",
          "decode_ms": "eval_duration", "load_ms": "load_duration"}


class Answer(str):
    """The model's raw answer, with Ollama's own timing from its final chunk (None when it sent none)."""
    timing: dict | None = None


def timing(chunk: dict) -> dict | None:
    found = {key: chunk[name] for key, name in TIMING.items() if type(chunk.get(name)) is int}
    return {key: round(value / 1e6) if key.endswith("_ms") else value for key, value in found.items()} or None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def provider(url: str, model: str, num_predict: int | None = None):
    """A `decide(prompt, heard) -> answer` for the kernel, for the Ollama at base URL `url`.
    `num_predict=1` makes a warm-up: it prefills the prompt into Ollama's cache and decodes one token."""
    parsed = urlparse(url)
    if (not model or parsed.scheme not in ("http", "https") or not parsed.hostname
            or (parsed.scheme == "http" and parsed.hostname not in LOCAL) or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path.strip("/")):
        raise ValueError("Set SAGE_MODEL_URL to Ollama's base URL (no path; plain http only on this machine) "
                         "and SAGE_MODEL to a model name")
    endpoint = url.rstrip("/") + "/api/chat"
    opener = urllib.request.build_opener(NoRedirect())
    options = {**OPTIONS, **next((o for family, o in FAMILY.items() if model.startswith(family)), {}),
               **({"num_predict": num_predict} if num_predict else {})}

    def decide(prompt: CompiledPrompt, heard) -> Answer:
        body = json.dumps({"model": model, "messages": prompt.messages(), "format": prompt.format, "stream": True,
                           "think": False, "keep_alive": KEEP_ALIVE,
                           "options": options}).encode()
        request = urllib.request.Request(endpoint, body, {"Content-Type": "application/json"})
        answer, final = [], {}
        with opener.open(request, timeout=TIMEOUT_S) as response:
            for line in response:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if "error" in chunk:
                    raise OSError(f"Ollama: {chunk['error']}")
                piece = chunk.get("message", {}).get("content", "")
                if piece:
                    answer.append(piece)
                    if sum(map(len, answer)) > MAX_ANSWER:
                        raise OSError("The model's answer is too long")
                    heard(piece)
                if chunk.get("done"):
                    final = chunk
                    break
        done = Answer("".join(answer))
        done.timing = timing(final)
        return done
    return decide
