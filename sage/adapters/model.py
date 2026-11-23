"""The decision providers: Ollama's native /api/chat, streamed NDJSON, thinking off, answer constrained by `format`;
and, off unless SAGE_HOSTED asks, a hosted model for the lines a device marks as addressed, with the local one
as its fallback.

Standard library only. The answer is the model's raw JSON; the kernel decodes and checks it.
"""

import http.client
import json
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from sage.policy import CompiledPrompt

LOCAL = ("localhost", "127.0.0.1", "::1", "host.docker.internal")
TIMEOUT_S = 30
MAX_ANSWER = 65536
# bench/LEDGER.md: num_predict 200 for the format_best harness; keep the live model loaded.
# Temperature 0.4 (owner, 2026-10-20; the bench ran 0.7).
# num_ctx capped: Ollama's default context can take ~15 GB on this 16 GB Mac. The live prompt is ~3k tokens; the
# worst case is ~4.1k once each line keeps its reminder (cache), so 5120: ~75 MB more KV cache than 4096 at q8_0.
OPTIONS = {"temperature": 0.4, "num_predict": 200, "num_ctx": 5120}
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


def _bad(url: str) -> bool:
    """Whether `url` is not a bare base URL: https, or plain http to this machine only; no path, no credentials."""
    parsed = urlparse(url)
    return bool(parsed.scheme not in ("http", "https") or not parsed.hostname
                or (parsed.scheme == "http" and parsed.hostname not in LOCAL) or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path.strip("/"))


def _chunks(response):
    """Ollama's stream as (piece, chunk), through its done chunk. OSError, like the network's own, for a stream
    that is cut, malformed, an error, or ends before its done chunk: a server that closes cleanly mid-answer
    raises nothing by itself, and half an answer is not the answer."""
    try:
        for line in response:
            if not line.strip():
                continue
            chunk = json.loads(line)
            if "error" in chunk:
                raise OSError(f"Ollama: {chunk['error']}"[:200])
            yield chunk.get("message", {}).get("content", ""), chunk
            if chunk.get("done"):
                return
    # IncompleteRead (cut inside a chunk) is an HTTPException, not an OSError; the rest are a malformed line.
    except (http.client.HTTPException, ValueError, TypeError, AttributeError) as error:
        raise OSError(f"Ollama: a broken stream ({type(error).__name__})") from None
    raise OSError("Ollama: the stream ended before the answer did")


def provider(url: str, model: str, num_predict: int | None = None):
    """A `decide(prompt, heard) -> answer` for the kernel, for the Ollama at base URL `url`.
    `num_predict=1` makes a warm-up: it prefills the prompt into Ollama's cache and decodes one token."""
    if not model or _bad(url):
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
            for piece, final in _chunks(response):
                if piece:
                    answer.append(piece)
                    if sum(map(len, answer)) > MAX_ANSWER:
                        raise OSError("The model's answer is too long")
                    heard(piece)
        done = Answer("".join(answer))
        done.timing = timing(final)
        return done
    return decide


# The memory writer: always this machine's model (owner, 2026-11-21: memory is written locally), with the
# talking model's own sampling and context size: another num_ctx would reload the model under the conversation.
# ponytail: 500 tokens for five short records and a day line, unmeasured; `python3 evals/memory.py writer` counts
# answers cut short.
WRITER_PREDICT = 500


def writer(url: str, model: str):
    """A `write(prompt, heard) -> answer` for the kernel's memory writer, on the Ollama at `url`, never streamed
    to anyone. OSError for a call that did not end with the model's own done chunk: the kernel keeps the
    conversation and asks again later, where half an answer would have been its last word. `write.hang_up()`, from any thread, ends a call in flight at once, even one still waiting for
    its first piece: the model server stops when its client is gone, so a turn never queues behind the writer.
    (`provider` cannot: urllib keeps its socket to itself.)"""
    if not model or _bad(url):
        raise ValueError("The memory writer needs SAGE_MODEL_URL and a model name (SAGE_WRITER_MODEL, or SAGE_MODEL)")
    parsed, calls = urlparse(url), []
    options = {**OPTIONS, **next((o for family, o in FAMILY.items() if model.startswith(family)), {}),
               "num_predict": WRITER_PREDICT}

    def write(prompt: CompiledPrompt, heard) -> str:
        secure = parsed.scheme == "https"
        call = (http.client.HTTPSConnection if secure else http.client.HTTPConnection)(
            parsed.hostname, parsed.port, timeout=TIMEOUT_S)
        call.connect()
        # The socket, not the connection: http.client lets go of its own once a response has no length.
        calls.append(sock := call.sock)
        try:
            call.request("POST", "/api/chat", json.dumps({
                "model": model, "messages": prompt.messages(), "format": prompt.format, "stream": True, "think": False,
                "keep_alive": KEEP_ALIVE, "options": options}), {"Content-Type": "application/json"})
            response = call.getresponse()
            if response.status != 200:
                raise OSError(f"Ollama: HTTP {response.status}")
            answer = []
            for piece, _ in _chunks(response):  # cut, malformed or over before its done chunk: OSError, no answer
                if piece:
                    answer.append(piece)
                    if sum(map(len, answer)) > MAX_ANSWER:
                        raise OSError("The writer's answer is too long")
                    heard(piece)
            return "".join(answer)
        except http.client.HTTPException as error:  # hung up mid-answer, or a server that is not Ollama
            raise OSError(f"Ollama: {type(error).__name__}") from None
        finally:
            calls.remove(sock)
            call.close()

    def hang_up() -> None:
        for sock in calls.copy():  # the calling thread removes its own
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # already gone
    write.hang_up = hang_up
    return write


# The hosted brain ---------------------------------------------------------------------------
# Off unless SAGE_HOSTED=anthropic. What leaves this machine: the compiled prompt of a turn its device marked
# as addressed (system text and context text), and the answer schema. Never audio, never an image, never the
# prompt of a line that carried no mark. The vendor's terms on what it keeps: SECURITY.md.

HOSTED_URL = "https://api.anthropic.com"
HOSTED_MODEL = "claude-haiku-5-5"
API_VERSION = "2026-10-21"
# ponytail: a guess (design 4-brain, scenario 3). Set it to the hosted first-piece p95 that bench/live_models.py
# --brain reports; SAGE_HOSTED_DEADLINE_S overrides.
HOSTED_DEADLINE_S = 2.5
# ponytail: a guess. The longest silence between two pieces before the call fails (connect and each read).
HOSTED_STALL_S = 8
# The local model's num_predict is 200; an answer is ~25 tokens at p50 (journal). Thinking is off, so none go to it.
HOSTED_MAX_TOKENS = 300
# ponytail: thinking off and effort low, for the first piece's sake, unmeasured. If the comparison shows wrong
# answers, try {"type": "adaptive"} here and read its first-piece column again.
HOSTED_THINKING, HOSTED_EFFORT = {"type": "disabled"}, "low"
# How long a device's mark stands. ponytail: a guess, longer than a turn with its retries (the local timeout
# is 30 s) and far shorter than the gap before a proactive turn.
ADDRESSED_S = 60
# JSON Schema keywords the vendor's structured output refuses; the kernel checks the answer itself anyway.
UNSUPPORTED = {"minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
               "minItems", "maxItems"}
CAP = "\n\nYour reply is at most {cap} characters."  # the local format enforces maxLength; here it is said


def strict(node):
    """`node`, a JSON Schema, as the hosted model's structured output takes it: no length or range keywords, and
    every object closed."""
    if isinstance(node, list):
        return [strict(item) for item in node]
    if not isinstance(node, dict):
        return node
    kept = {key: {name: strict(schema) for name, schema in value.items()} if key == "properties" else strict(value)
            for key, value in node.items() if key not in UNSUPPORTED}
    if "properties" in kept or kept.get("type") == "object":
        kept["additionalProperties"] = False
    return kept


def _pieces(response, usage: dict):
    """The answer's text, piece by piece, from the vendor's event stream; its token counts go into `usage`.
    OSError, like the network's own, for a stream that is cut, malformed, refused or ends before its
    message_stop: a server or proxy that closes cleanly mid-answer raises nothing by itself."""
    try:
        for line in response:
            if not line.startswith(b"data:"):
                continue
            event = json.loads(line[5:])
            kind = event.get("type")
            if kind == "error":
                raise OSError(f"Hosted model: {event.get('error', {}).get('type')}"[:200])
            if kind == "message_start":
                usage.update(event.get("message", {}).get("usage") or {})
            elif kind == "message_delta":
                usage.update(event.get("usage") or {})
                if event.get("delta", {}).get("stop_reason") == "refusal":
                    raise OSError("Hosted model: refused")
            elif kind == "content_block_delta" and event["delta"].get("type") == "text_delta" and event["delta"]["text"]:
                yield event["delta"]["text"]
            elif kind == "message_stop":
                return
    # IncompleteRead (cut inside a chunk) is an HTTPException, not an OSError; the rest are a malformed event.
    except (http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError) as error:
        raise OSError(f"Hosted model: a broken stream ({type(error).__name__})") from None
    raise OSError("Hosted model: the stream ended before the answer did")


def hosted(key: str, model: str = HOSTED_MODEL, url: str = HOSTED_URL, stall_s: float = HOSTED_STALL_S):
    """A `decide(prompt, heard) -> answer` on the vendor's Messages API, streamed. `key` goes in one request
    header and nowhere else: no message, log line or error holds it."""
    if not key or not model or _bad(url):
        raise ValueError("The hosted model needs ANTHROPIC_API_KEY, a model name, and an https base URL with no path")
    endpoint = url.rstrip("/") + "/v1/messages"
    opener = urllib.request.build_opener(NoRedirect())

    def decide(prompt: CompiledPrompt, heard) -> Answer:
        # The kernel's notes to the model have their own role locally; this API has two, and they read as heard.
        messages = [{"role": m["role"] if m["role"] == "assistant" else "user", "content": m["content"]}
                    for m in prompt.context]
        body = json.dumps({"model": model, "max_tokens": HOSTED_MAX_TOKENS, "stream": True,
                           "system": prompt.system + CAP.format(cap=prompt.max_reply), "messages": messages,
                           "thinking": HOSTED_THINKING,
                           "output_config": {"effort": HOSTED_EFFORT,
                                             "format": {"type": "json_schema", "schema": strict(prompt.format)}}})
        request = urllib.request.Request(endpoint, body.encode(), {"Content-Type": "application/json", "x-api-key": key,
                                                                   "anthropic-version": API_VERSION})
        began, first, answer, usage = time.monotonic(), None, [], {}
        try:
            response = opener.open(request, timeout=stall_s)
        except urllib.error.HTTPError as error:
            try:
                kind = json.loads(error.read(4096))["error"]["type"]
            except (ValueError, KeyError, TypeError, OSError):
                kind = "error"
            error.close()
            raise OSError(f"Hosted model: HTTP {error.code} {kind}"[:200]) from None
        except http.client.HTTPException as error:  # an answer that is not HTTP
            raise OSError(f"Hosted model: {type(error).__name__}") from None
        with response:
            for piece in _pieces(response, usage):
                answer.append(piece)
                if sum(map(len, answer)) > MAX_ANSWER:
                    raise OSError("The model's answer is too long")
                first = first or time.monotonic()
                heard(piece)
        if not answer:  # it ended as it should and said nothing: a failure, so `routed` asks the local model
            raise OSError("Hosted model: an empty answer")
        done = Answer("".join(answer))
        sent = sum(usage.get(name) or 0 for name in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        done.timing = {"model": model, "prompt_tokens": sent, "gen_tokens": usage.get("output_tokens") or 0,
                       "first_ms": round(((first or time.monotonic()) - began) * 1000),
                       "total_ms": round((time.monotonic() - began) * 1000)}
        return done
    return decide


class Addressed:
    """Which line each device last submitted, when the device marked it as addressed (PROTOCOL.md, Addressed).

    ponytail: `decide(prompt, heard)` is told no turn, so a prompt is matched to its mark by the line its newest
    message carries. Exact for one device: its next line, marked or not, replaces the mark. Two devices sending
    the same words within ADDRESSED_S, one marked and one not, would both match; pass the mark with the turn
    when `decide` grows a third argument."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.lines = clock, {}

    def mark(self, device: str, text, addressed: bool) -> None:
        if addressed and isinstance(text, str) and text.strip():
            self.lines[device] = (text, self.clock() + ADDRESSED_S)
        else:
            self.lines.pop(device, None)

    def __call__(self, prompt: CompiledPrompt) -> bool:
        newest = prompt.context[-1]["content"] if prompt.context else ""
        unstamped = newest.split("\n", 1)[-1]  # after the [when] line, if there is one
        now = self.clock()
        return any(until > now and part.startswith((text + "\n", f'heard (unclear): "{text}"'))
                   for text, until in list(self.lines.values()) for part in (newest + "\n", unstamped + "\n"))


class _Late(Exception):
    """Raised into a call nobody waits for any more, at its next piece."""


def _first_within(call, prompt: CompiledPrompt, heard, deadline_s: float):
    """`call(prompt, heard)`, with `heard` run on this thread; TimeoutError if no piece and no answer came
    within `deadline_s`. After a first piece the call's own stall timeout bounds it."""
    events, gone = queue.Queue(), threading.Event()

    def piece(text: str) -> None:
        if gone.is_set():
            raise _Late
        events.put(("piece", text))

    def run() -> None:
        try:
            events.put(("answer", call(prompt, piece)))
        except BaseException as caught:  # noqa: BLE001 -- handed to the waiting thread, which raises it
            events.put(("error", caught))
    threading.Thread(target=run, daemon=True).start()
    wait = deadline_s
    try:
        while True:
            try:
                kind, value = events.get(timeout=wait)
            except queue.Empty:
                raise TimeoutError(f"no first piece in {deadline_s} s") from None
            if kind == "answer":
                return value
            if kind == "error":
                raise value
            wait = None
            heard(value)
    finally:
        gone.set()


def routed(local, hosted, addressed, deadline_s: float = HOSTED_DEADLINE_S):
    """A `decide` that asks `hosted` for a prompt `addressed(prompt)` says was marked, and `local` for every
    other. A hosted call that fails, or gives no first piece within `deadline_s`, is answered by `local`
    instead, and the answer's timing says why (`fallback`). Once a piece was heard, a failure is the turn's:
    other words cannot follow words already spoken."""
    def decide(prompt: CompiledPrompt, heard) -> Answer:
        if not addressed(prompt):
            return local(prompt, heard)
        started = []

        def first(piece: str) -> None:
            started.append(piece)
            heard(piece)
        try:
            return _first_within(hosted, prompt, first, deadline_s)
        except (OSError, ValueError, KeyError, TypeError) as error:  # the network, the vendor, or a malformed stream
            if started:
                raise
            # The error's class and the vendor's error type only: never the request, so never the key.
            print(f"sage: the hosted model did not answer ({type(error).__name__}: {str(error)[:200]}); "
                  "answering locally", file=sys.stderr)
            answer = local(prompt, heard)
            answer = answer if isinstance(answer, Answer) else Answer(answer)
            answer.timing = {**(answer.timing or {}), "fallback": type(error).__name__}
            return answer
    return decide


def from_env(env, local):
    """`(decide, marks)` as the environment asks. Without SAGE_HOSTED: `local` itself and no marks, and nothing
    ever leaves. SAGE_HOSTED=anthropic: addressed lines go to SAGE_HOSTED_MODEL (default HOSTED_MODEL) with the
    key in ANTHROPIC_API_KEY, falling back to `local` after SAGE_HOSTED_DEADLINE_S."""
    which = env.get("SAGE_HOSTED", "")
    if not which:
        return local, None
    if which != "anthropic":
        raise ValueError("SAGE_HOSTED is anthropic, or unset for no hosted model")
    marks = Addressed()
    brain = hosted(env.get("ANTHROPIC_API_KEY", ""), env.get("SAGE_HOSTED_MODEL") or HOSTED_MODEL,
                   env.get("SAGE_HOSTED_URL") or HOSTED_URL)
    return routed(local, brain, marks, float(env.get("SAGE_HOSTED_DEADLINE_S") or HOSTED_DEADLINE_S)), marks
