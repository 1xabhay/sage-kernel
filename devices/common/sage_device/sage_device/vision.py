"""Sight: a camera frame becomes a few plain words from a vision model on this machine.

Only the words go on, to the kernel as an ``acted`` ``saw`` (protocol v1.3): the frame is never
stored or sent anywhere else (owner, 2026-11-08). People are only ever "a person": who they are is
not the body's to guess. Body-agnostic: any body with a camera and Ollama can use it.
"""

import base64
import json
import re
import urllib.error
import urllib.request

MAX_SEEN = 200  # the kernel's cap on what a body saw (policy.MAX_SEEN)
TIMEOUT_S = 20.0  # a cold model loads first; past this nothing is seen
SAME = 0.5  # word overlap at or above which two scenes are the same one told again
# The kernel's own runner options (sage/adapters/model.py): with one model for words and sight, a
# different context makes Ollama load it twice, and a request without keep_alive resets "forever".
NUM_CTX = 5120


def prompt(question: str | None) -> str:
    """Return the model's ask: ``question`` answered from the frame, or the scene in short."""
    rules = ("You are a small robot's eyes. Answer in one plain sentence of at most 25 words. "
             "Describe people only as a person, by what they do or hold; never guess who they are, "
             "their age, looks or feelings. Say only what you can see; if unsure, say so.")
    ask = (f'The person asks: "{question}". Say what in the image answers it.' if question
           else "Say what the scene is: who is there, doing what, and the main things in view.")
    return f"{rules}\n{ask}"


def describe(jpeg: bytes | None, question: str | None, *, url: str, model: str,
             timeout: float = TIMEOUT_S) -> str | None:
    """Return what ``jpeg`` shows in MAX_SEEN characters or fewer; None: no frame, no answer."""
    if not jpeg:
        return None
    body = {"model": model, "prompt": prompt(question), "stream": False,
            "images": [base64.b64encode(jpeg).decode()],
            "keep_alive": -1,
            "options": {"temperature": 0.2, "num_predict": 60, "num_ctx": NUM_CTX}}
    request = urllib.request.Request(url.rstrip("/") + "/api/generate",
                                     data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            said = json.loads(answer.read()).get("response") or ""
    except (urllib.error.URLError, OSError, ValueError):
        return None
    said = " ".join(said.split())
    if len(said) > MAX_SEEN:
        said = said[:MAX_SEEN + 1].rsplit(" ", 1)[0].rstrip(",;:")
    return said or None


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower())
            if len(w) > 2 and w not in ("the", "and", "with")}


def changed(before: str | None, now: str) -> bool:
    """Whether ``now`` is a different scene from ``before``, not the same one in other words."""
    if not before:
        return True
    a, b = _words(before), _words(now)
    return len(a & b) / max(len(a | b), 1) < SAME
