"""The kernel: applies the pure policy under one lock, journals each step, and runs decisions.

Devices reach it through an adapter (sage/adapters/http.py). The decision provider is a callable
`decide(prompt, heard) -> answer`: it calls `heard(piece)` with each piece of the model's raw
JSON answer as it streams, and returns the whole answer.
"""

import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import replace

from sage import disposition, manifest, policy, speech
from sage.journal import Journal
from sage.policy import CompiledPrompt, Refused

PROTOCOLS = (1,)
MAX_WAIT_S = 25.0
MIN_CONFIDENCE = 0.5  # a seen edge below this is journaled but is no arrival (lead, 2026-10-20)
NOTHING = {"senses": (), "tools": ()}
# What this kernel serves beyond v1's core, so a device can degrade gracefully instead of probing (v1.1).
FEATURES = ("acted", "body", "done_text", "health", "input", "moves", "notes", "presence")

Decide = Callable[[CompiledPrompt, Callable[[str], None]], str]


class Abandoned(Exception):
    """Raised from a model call's stream once its turn is no longer running, so the call ends early."""


class Guarded(Exception):
    """Raised from a model call's stream once a guard will surely reject its answer, so the retry starts at once."""


class Kernel:
    def __init__(self, journal: Journal, decide: Decide, fresh_s: float = 10.0, clock=time.time,
                 timings: disposition.Timings = disposition.DEFAULT, warm: Decide | None = None):
        self.journal, self.decide, self.fresh_s, self.clock = journal, decide, fresh_s, clock
        self.warm = warm  # a one-token call that fills the model server's prompt cache; None: never warm
        # Presence is not kept across restarts, so the first seen after one is an arrival; quiet is kept.
        self.timings = timings
        self.disposition = disposition.Disposition(quiet_until=journal.state.quiet_until)
        self.seen_by: str | None = None  # the device that most recently saw the person
        self.greet_for: str | None = None  # a greeting waiting for the body to be free
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.manifests: dict[str, dict] = {}  # declared again on every hello; never kept across restarts
        self.buckets: dict[str, tuple[float, float]] = {}
        self.written: dict[str, str] = {}  # per device, what may be said of its running turn's reply; never journaled
        with self.lock:
            self._commit(*policy.restarted(journal.state))

    @property
    def state(self) -> policy.Companion:
        return self.journal.state

    def _commit(self, state, rows) -> None:
        if rows:
            self.journal.append(rows, state, self.clock())
            self.changed.notify_all()
        self.written = {device: said for device, said in self.written.items()
                        if policy.lane(state, device).status == "active"}

    def _running(self) -> bool:
        return any(turn.status == "active" for turn in policy.turns(self.state))

    def view(self, source: str) -> dict:
        """This device's current turn: each device has a lane of its own (v1.2)."""
        turn = policy.lane(self.state, source)
        if turn.turn_id is None:
            return {}
        return {"turn": {"turn_id": turn.turn_id, "run_status": turn.status,
                         **({"timing": turn.timing} if turn.timing else {})}}

    def hello(self, source: str, data: dict) -> dict:
        protocols = data.get("protocols")
        if not isinstance(protocols, list) or set(data) - {"protocols", "manifest"}:
            raise Refused("invalid", "Send protocols (a list of versions) and manifest (an object)")
        common = [version for version in PROTOCOLS if version in protocols]
        if not common:
            raise Refused("protocol_mismatch", "Use a protocol version this kernel supports",
                          supported=list(PROTOCOLS))
        try:
            declared = manifest.check_manifest(data.get("manifest", {}))
        except ValueError as error:
            raise Refused("invalid", str(error)) from None
        with self.lock:
            # First seen, or a new body: a new system prompt, cold in the model server's cache.
            if self.warm and self.manifests.get(source) != declared and not self._running():
                threading.Thread(target=self._warm, args=(source,), daemon=True).start()
            self.manifests[source] = declared
            return {"protocol": max(common), "sequence": self.journal.head, "features": list(FEATURES)}

    @staticmethod
    def health() -> dict:
        """Answering at all is the health: ponytail: no journal or model check, add one when a device acts on it."""
        return {"ok": True, "protocol": max(PROTOCOLS), "features": list(FEATURES)}

    def _warm(self, source: str) -> None:
        """Prefill this device's next prompt (its system prompt and history) into the model server's cache,
        so its next turn skips the re-read: ~6 s cold after a hello, ~150 ms after a reply, ~600 ms after a trim,
        the whole history after a new fact. Never while a turn runs; abandoned when one starts.
        ponytail: Ollama runs one call at a time, so a turn arriving mid-prefill queues behind it; that prefill
        is the turn's own prefix, so it costs only the empty newest line and one token (~0.1 s). Close the
        socket if that changes."""
        def heard(piece: str) -> None:
            with self.lock:
                if self._running():
                    raise Abandoned(source)

        with self.lock:
            if self._running():
                return
            prompt = policy.compile_prompt(self.state, source, self.manifests[source])
        started = time.monotonic()
        try:
            self.warm(prompt, heard)
        except Abandoned:
            return
        except Exception as caught:  # noqa: BLE001 -- a failed warm-up costs only the prefill it meant to save
            print(f"sage: could not warm the model for {source}: {caught}", file=sys.stderr)
            return
        print(f"sage: warmed the model for {source} in {time.monotonic() - started:.1f} s", file=sys.stderr)

    def event(self, source: str, data: dict) -> dict:
        with self.lock:
            if data.get("kind") != "stop":  # a body can always be stopped
                allowed, self.buckets[source], retry_after = policy.take(self.buckets.get(source), time.monotonic())
                if not allowed:
                    raise Refused("rate_limited", f"Too many events from this device. Wait {retry_after} s.",
                                  retry_after=retry_after)
            kind = policy.check_event(data)
            state, rows = policy.sweep(self.state, self.clock())
            turn_id, text, timing = data.get("turn_id"), data.get("text"), policy.check_timing(data.get("timing"))
            status, reason = data.get("status"), data.get("reason")
            if kind == "submitted":
                state, more = policy.submit(state, source, turn_id, text, timing, self.journal.seen(source, turn_id),
                                            data.get("input"))
                if more:  # the person spoke to the kernel: it may start or end quiet
                    self.disposition, _ = disposition.step(self.disposition, "spoke", self.clock(), text, self.timings)
                    state = replace(state, quiet_until=self.disposition.quiet_until)
            elif kind == "presence":
                more = (policy.Row("presence", source, None, {"state": data["state"], "confidence": data["confidence"]}),)
            elif kind == "acted":
                state, more = policy.act(state, source, data)
            elif kind == "notes":
                state, more = policy.take_notes(state, source, data["topic"], data["passages"])
            elif kind == "stop":
                streaming = {device for device, said in self.written.items() if said}
                state, more = policy.stop(state, source, turn_id, streaming=streaming)
            elif kind == "acknowledged":
                state, more = policy.acknowledge(state, source, turn_id)
            elif kind == "result" and "sequence" in data:
                state, more = policy.report(state, source, self.journal.effect(data["sequence"]),
                                            status, text, reason, timing, data.get("duration_ms"))
            elif kind == "result":
                state, more = policy.report_turn(state, source, turn_id, self.journal.reply_of(source, turn_id),
                                                 status, text, reason, timing, data.get("duration_ms"))
            else:
                more = (policy.Row(kind, source),)
            self._commit(state, rows + more)
            if kind == "presence":
                self._presence(source, data["state"], data["confidence"])
            if kind == "submitted" and more:
                declared = self.manifests.get(source, NOTHING)
                prompt = policy.compile_prompt(state, source, declared)
                threading.Thread(target=self._think, args=(source, turn_id, prompt, declared), daemon=True).start()
            return {"sequence": self.journal.head, **self.view(source)}

    def _presence(self, source: str, edge: str, confidence: float) -> None:
        if edge == "seen" and confidence < MIN_CONFIDENCE:
            return
        self.disposition, out = disposition.step(self.disposition, edge, self.clock(), t=self.timings)
        if edge == "seen":
            self.seen_by = source
            self.greet_for = source if "greet" in out else self.greet_for
        elif self.greet_for == source:
            self.greet_for = None  # gone again before the greeting could start

    def tick(self) -> None:
        """Start a greeting or a nudge if one is due and its body is free: no turn of its own running, not acting."""
        with self.lock:
            state = self.state
            due = self.greet_for or self.seen_by  # the body a greeting or nudge would go to waits on its own lane
            if due and (policy.lane(state, due).status in ("active", "unknown") or self.written.get(due)
                        or any(effect.target == due for effect in state.open)):
                return
            now = self.clock()
            if disposition.state(self.disposition, now, self.timings) == "quiet":
                self.greet_for = None
                return
            if self.greet_for:
                device, reason, self.greet_for = self.greet_for, "greet", None
            else:
                self.disposition, out = disposition.step(self.disposition, "tick", now, t=self.timings)
                if "nudge" not in out or self.seen_by is None:
                    return
                device, reason = self.seen_by, "nudge"
            turn_id = "kernel-" + uuid.uuid4().hex
            state, rows = policy.start_proactive(state, device, turn_id, reason)
            self._commit(state, rows)
            declared = self.manifests.get(device, NOTHING)
            prompt = policy.compile_prompt(state, device, declared)
            threading.Thread(target=self._think, args=(device, turn_id, prompt, declared), daemon=True).start()

    def ticking(self, every: float = 0.25) -> None:
        while True:
            time.sleep(every)
            try:
                self.tick()
            except (OSError, sqlite3.Error) as caught:
                print(f"sage: could not start a proactive turn: {caught}", file=sys.stderr)

    def _ask(self, source: str, turn_id: str, prompt: CompiledPrompt, declared: dict, recent, cut=True) -> str:
        """One model call. Its reply streams as it decodes, held back while it could repeat one of `recent`
        or opens with what could be a claim of a sense the body lacks. With `cut`, a held-back answer a guard
        will surely reject ends the call there, and the answer so far is returned."""
        labels, names, senses = manifest.labels(declared["tools"]), self._names(declared), declared["senses"]
        pieces = []

        def heard(piece: str) -> None:
            with self.lock:  # stopped or replaced: end the call, or the model server serves it to the end first
                turn = policy.lane(self.state, source)
                if turn.turn_id != turn_id or turn.status != "active":
                    raise Abandoned(turn_id)
            pieces.append(piece)
            reply, closed = speech.partial_reply("".join(pieces))
            reply = (speech.whole_sentences if closed else speech.near_cap)(reply, prompt.max_reply)
            # Never a trailing space: the final reply is trimmed, and what streams must be its prefix.
            said = speech.speakable(reply, labels, closed, names).rstrip()
            if (policy.may_repeat(recent, said) or (closed and policy.repeats(recent, said))
                    or policy.may_claim(said, senses)):
                # Certain only on whole words: a last word still streaming may grow ("see" into "seem").
                sure = said if closed else said.rpartition(" ")[0]
                if cut and not self._streamed(source) and (policy.opens_like(recent, sure)
                                                           or policy.false_senses(sure, senses)):
                    raise Guarded(turn_id)
                return
            with self.lock:
                turn, written = policy.lane(self.state, source), self.written.get(source, "")
                if (turn.turn_id == turn_id and turn.status == "active" and len(said) > len(written)
                        and said.startswith(written)):
                    self.written[source] = said
                    self.changed.notify_all()
        try:
            return self.decide(prompt, heard)
        except Guarded:
            return "".join(pieces)

    @staticmethod
    def _names(declared: dict) -> tuple[str, ...]:
        return tuple(tool["name"] for tool in declared["tools"])

    def _said(self, output: str, declared: dict) -> str:
        return speech.speakable(speech.parse_output(output)[0], manifest.labels(declared["tools"]), True,
                                self._names(declared))

    def _streamed(self, source: str) -> str:
        with self.lock:
            return self.written.get(source, "")

    def _reject(self, source: str, turn_id: str, prompt: CompiledPrompt, output: str, reason: str) -> None:
        with self.lock:
            self._commit(self.state, policy.rejected(policy.focus(self.state, source), turn_id, prompt, output, reason))

    def _think(self, source: str, turn_id: str, prompt: CompiledPrompt, declared: dict) -> None:
        senses = declared["senses"]
        with self.lock:
            line = policy.lane(self.state, source).text
            # Asked what it just said, saying it again is the honest answer.
            recent = () if policy.asks_to_repeat(line) else policy.recent_replies(self.state, source)
        try:
            # A guard retries only while the device has heard nothing of the answer: a retry is new words, and
            # what already streamed must stay the reply's start. Past that, the answer stands (`decided` repairs
            # a claim), rather than a second call whose answer is then cut back to the streamed piece.
            output = self._ask(source, turn_id, prompt, declared, recent)
            if (policy.repeats(recent, said := self._said(output, declared))  # retried once; never silence
                    and not self._streamed(source)):
                self._reject(source, turn_id, prompt, output, "repeat of a recent reply")
                prompt = policy.retry_prompt(prompt, policy.repeat_note(said))
                output = self._ask(source, turn_id, prompt, declared, ())
            if ((claimed := policy.false_senses(self._said(output, declared), senses))  # retried once, then fixed
                    and not self._streamed(source)):
                self._reject(source, turn_id, prompt, output, "claims a sense this body lacks: " + ", ".join(claimed))
                prompt = policy.retry_prompt(prompt, policy.sense_note(claimed))
                output = self._ask(source, turn_id, prompt, declared, (), cut=False)  # no guard after it
            if (not self._said(output, declared) and policy.needs_words(line)  # retried once, then `decided` speaks
                    and not self._streamed(source)):
                self._reject(source, turn_id, prompt, output, "silent on a line that needs words")
                prompt = policy.retry_prompt(prompt, policy.NEEDS_WORDS)
                output = self._ask(source, turn_id, prompt, declared, (), cut=False)
            error = ""
        except Abandoned:
            return  # stopped meanwhile: the stop is journaled, and nothing is delivered
        except Exception as caught:  # noqa: BLE001 -- any provider or journal failure frees the turn
            output, error = "", f"{type(caught).__name__}: {caught}"[:500]
        with self.lock:
            state = policy.focus(self.state, source)
            if error:
                state, rows = policy.failed(state, turn_id, prompt, error)
            else:
                state, rows = policy.decided(state, turn_id, prompt, output, declared["tools"],
                                             self.journal.head + 1, self.clock(), senses=senses,
                                             streamed=self.written.get(source, ""),
                                             model_timing=getattr(output, "timing", None))
            try:
                self._commit(state, rows)
            except (OSError, sqlite3.Error) as caught:
                print(f"sage: could not journal the outcome of {turn_id}: {caught}", file=sys.stderr)
                return
            if self.warm and not error and source in self.manifests:  # while the body speaks the reply
                threading.Thread(target=self._warm, args=(source,), daemon=True).start()

    def effects(self, source: str, since: int, wait: float, offset: int) -> dict:
        def grew() -> bool:
            return policy.lane(self.state, source).status == "active" and len(self.written.get(source, "")) > offset

        with self.changed:
            if since > self.journal.head:
                raise Refused("reset", "Your cursor is beyond the journal. Fetch again from sequence.",
                              sequence=self.journal.head)
            self.changed.wait_for(lambda: self.journal.head > since or grew(), min(max(wait, 0.0), MAX_WAIT_S))
            head, view = self.journal.head, self.view(source)
            delta = ({"kind": "reply_delta", "turn_id": policy.lane(self.state, source).turn_id, "offset": offset,
                      "text": self.written[source][offset:]} if grew() else None)
        effects = self.journal.deliveries(source, since, head, self.clock(), self.fresh_s)
        return {"sequence": head, "effects": effects + ([delta] if delta else []), **view}
