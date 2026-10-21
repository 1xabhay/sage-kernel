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

Decide = Callable[[CompiledPrompt, Callable[[str], None]], str]


class Kernel:
    def __init__(self, journal: Journal, decide: Decide, fresh_s: float = 10.0, clock=time.time,
                 repeat_fallback: bool = False, timings: disposition.Timings = disposition.DEFAULT):
        self.journal, self.decide, self.fresh_s, self.clock = journal, decide, fresh_s, clock
        self.repeat_fallback = repeat_fallback  # experiment, off by default: see policy.acknowledgement
        # Presence is not kept across restarts, so the first seen after one is an arrival; quiet is kept.
        self.timings = timings
        self.disposition = disposition.Disposition(quiet_until=journal.state.quiet_until)
        self.seen_by: str | None = None  # the device that most recently saw the person
        self.greet_for: str | None = None  # a greeting waiting for the body to be free
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.manifests: dict[str, dict] = {}  # declared again on every hello; never kept across restarts
        self.buckets: dict[str, tuple[float, float]] = {}
        self.written = ""  # what may be said of the running turn's reply so far; never journaled
        with self.lock:
            self._commit(*policy.restarted(journal.state))

    @property
    def state(self) -> policy.Companion:
        return self.journal.state

    def _commit(self, state, rows) -> None:
        if rows:
            self.journal.append(rows, state, self.clock())
            self.changed.notify_all()
        if state.turn.status != "active":
            self.written = ""

    def view(self, source: str) -> dict:
        """The current turn, for the device that started it."""
        turn = self.state.turn
        if turn.source != source or turn.turn_id is None:
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
            self.manifests[source] = declared
            return {"protocol": max(common), "sequence": self.journal.head}

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
                state, more = policy.submit(state, source, turn_id, text, timing, self.journal.seen(source, turn_id))
                if more:  # the person spoke to the kernel: it may start or end quiet
                    self.disposition, _ = disposition.step(self.disposition, "spoke", self.clock(), text, self.timings)
                    state = replace(state, quiet_until=self.disposition.quiet_until)
            elif kind == "presence":
                more = (policy.Row("presence", source, None, {"state": data["state"], "confidence": data["confidence"]}),)
            elif kind == "stop":
                state, more = policy.stop(state, source, turn_id, streaming=bool(self.written))
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
                threading.Thread(target=self._think, args=(turn_id, prompt, declared), daemon=True).start()
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
        """Start a greeting or a nudge if one is due and the body is free: no turn running, no device acting."""
        with self.lock:
            state = self.state
            if state.turn.status in ("active", "unknown") or state.open or self.written:
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
            threading.Thread(target=self._think, args=(turn_id, prompt, declared), daemon=True).start()

    def ticking(self, every: float = 0.25) -> None:
        while True:
            time.sleep(every)
            try:
                self.tick()
            except (OSError, sqlite3.Error) as caught:
                print(f"sage: could not start a proactive turn: {caught}", file=sys.stderr)

    def _ask(self, turn_id: str, prompt: CompiledPrompt, declared: dict, recent, sentences=frozenset()) -> str:
        """One model call. Its reply streams as it decodes, held back while it could repeat one of `recent`
        or opens with what could be a claim of a sense the body lacks."""
        labels, names, senses = manifest.labels(declared["tools"]), self._names(declared), declared["senses"]
        with self.lock:
            line = self.state.turn.text
        pieces = []

        def heard(piece: str) -> None:
            pieces.append(piece)
            reply, closed = speech.partial_reply("".join(pieces))
            reply = (speech.whole_sentences if closed else speech.near_cap)(reply, manifest.MAX_REPLY)
            # Never a trailing space: the final reply is trimmed, and what streams must be its prefix.
            said = policy.streamable(speech.speakable(reply, labels, closed, names), sentences).rstrip()
            if (policy.may_repeat(recent, said) or (closed and policy.repeats(recent, said))
                    or policy.may_claim(said, senses) or policy.may_announce(said, line)):
                return
            with self.lock:
                turn = self.state.turn
                if (turn.turn_id == turn_id and turn.status == "active" and len(said) > len(self.written)
                        and said.startswith(self.written)):
                    self.written = said
                    self.changed.notify_all()
        return self.decide(prompt, heard)

    @staticmethod
    def _names(declared: dict) -> tuple[str, ...]:
        return tuple(tool["name"] for tool in declared["tools"])

    def _said(self, output: str, declared: dict) -> str:
        return speech.speakable(speech.parse_output(output)[0], manifest.labels(declared["tools"]), True,
                                self._names(declared))

    def _reject(self, turn_id: str, prompt: CompiledPrompt, output: str, reason: str) -> None:
        with self.lock:
            self._commit(self.state, policy.rejected(self.state, turn_id, prompt, output, reason))

    def _think(self, turn_id: str, prompt: CompiledPrompt, declared: dict) -> None:
        senses = declared["senses"]
        with self.lock:
            recent = policy.recent_replies(self.state, self.state.turn.source)
            sentences = policy.recent_sentences(self.state, self.state.turn.source)
            line = self.state.turn.text
            if policy.asks_to_repeat(line):  # saying it again is the honest answer
                recent, sentences = (), frozenset()
        try:
            output = self._ask(turn_id, prompt, declared, recent, sentences)
            if policy.repeats(recent, said := self._said(output, declared)):  # retried once; never silence
                self._reject(turn_id, prompt, output, "repeat of a recent reply")
                prompt = policy.retry_prompt(prompt, policy.repeat_note(said))
                output = self._ask(turn_id, prompt, declared, (), sentences)
            if policy.asks_to_act(line) and policy.announces_only(self._said(output, declared)):  # once, then as is
                self._reject(turn_id, prompt, output, "announces without acting")
                prompt = policy.retry_prompt(prompt, policy.ACT_NOTE)
                output = self._ask(turn_id, prompt, declared, (), sentences)
            reply = speech.parse_output(output)[0]
            if reply.strip() and not self._said(output, declared) and speech.ASSISTANT.search(reply.lower()):
                self._reject(turn_id, prompt, output, "only assistant-speak")  # once, then silence
                prompt = policy.retry_prompt(prompt, policy.OWN_WORDS)
                output = self._ask(turn_id, prompt, declared, (), sentences)
            if claimed := policy.false_senses(self._said(output, declared), senses):  # retried once, then fixed
                self._reject(turn_id, prompt, output, "claims a sense this body lacks: " + ", ".join(claimed))
                prompt = policy.retry_prompt(prompt, policy.sense_note(claimed))
                output = self._ask(turn_id, prompt, declared, (), sentences)
            error = ""
        except Exception as caught:  # noqa: BLE001 -- any provider or journal failure frees the turn
            output, error = "", f"{type(caught).__name__}: {caught}"[:500]
        with self.lock:
            if error:
                state, rows = policy.failed(self.state, turn_id, prompt, error)
            else:
                state, rows = policy.decided(self.state, turn_id, prompt, output, declared["tools"],
                                             self.journal.head + 1, self.clock(), senses=senses,
                                             fallback=self.repeat_fallback, streamed=self.written)
            try:
                self._commit(state, rows)
            except (OSError, sqlite3.Error) as caught:
                print(f"sage: could not journal the outcome of {turn_id}: {caught}", file=sys.stderr)

    def effects(self, source: str, since: int, wait: float, offset: int) -> dict:
        def grew() -> bool:
            turn = self.state.turn
            return turn.source == source and turn.status == "active" and len(self.written) > offset

        with self.changed:
            if since > self.journal.head:
                raise Refused("reset", "Your cursor is beyond the journal. Fetch again from sequence.",
                              sequence=self.journal.head)
            self.changed.wait_for(lambda: self.journal.head > since or grew(), min(max(wait, 0.0), MAX_WAIT_S))
            head, view = self.journal.head, self.view(source)
            delta = ({"kind": "reply_delta", "turn_id": self.state.turn.turn_id, "offset": offset,
                      "text": self.written[offset:]} if grew() else None)
        effects = self.journal.deliveries(source, since, head, self.clock(), self.fresh_s)
        return {"sequence": head, "effects": effects + ([delta] if delta else []), **view}
