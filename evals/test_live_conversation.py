"""Regression eval, against real Ollama: the first live robot conversation must not fail the same way again.

Opt-in (`make eval`): it needs Ollama with SAGE_MODEL pulled at SAGE_EVAL_MODEL_URL. It replays the five
lines of that conversation through the kernel's HTTP API, as the robot sent them, and reports latency.
LiveRun5b replays live run 5b (2026-10-21) the same way and checks conversation quality.
The live lines are the owner's own speech and stay in evals/local/ (gitignored); without them the eval
replays written fixtures of the same shape (first_run.json, run_5b.json).
"""

import json
import os
import re
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent


def fixture(name: str, here: Path = HERE):
    """A replayed conversation: the live one in local/ (gitignored, real speech) if present, else the written one."""
    live = here / "local" / name
    return json.loads((live if live.exists() else here / name).read_text())


CASE = fixture("first_run.json")
RUN_5B = fixture("run_5b.json")
TOKEN = "reachy-" + "e" * 32
URL = os.environ.get("SAGE_EVAL_MODEL_URL", "http://127.0.0.1:11434")
# The lines below are written, not recorded: each has the shape of a live line that failed as described.
MODEL = os.environ.get("SAGE_MODEL", "qwen3:1.7b")
# Live run 3: "Can you see me?" got "Yes, I can see you." from a body that declares only speech.
SIGHT = ("Can you see my face?", "What can you see right now?")
# Live run 4: "How can I assist you?" closed two replies in a row, spoken from the stream both times.
ASSIST = ("Hi there!", "Can you hear me okay?", "Much better than this morning.")
# Live run 4: asked to act, it only announced ("I'll speak about something. Let's see..."); asked what it
# had said, it made something up.
ACT = ("Go on, talk about anything.", "So what do you say?", "Fine, do it.")
RECALL = "Sorry, what did you just say?"
# Live: after barge-ins the history held {"reply": "I'll (interrupted)"} and the robot then said it aloud.
BARGE = ("Tell me a long story about a lighthouse.", "Tell me one more long story.")
AFTER_BARGE = "Okay, keep going."
ANNOUNCES = re.compile(r"^(i'll|i will|i'm going to|i am going to|let's|let me|let us|sure|okay|ok|alright|all right"
                       r"|of course|great|yes|absolutely)\b")
CLAIMS_SIGHT = re.compile(r"\bi (can|do) see\b|\bi see (you|a|the|your)\b|\bi'm (seeing|looking at)\b|\byes\b.*\bsee\b")
SAYS_BLIND = re.compile(r"\b(can ?not|can't|cannot|unable to|don't have|don't|do not)\b.*\b(see|sight|vision|eyes|camera)\b"
                        r"|\bno (eyes|camera|vision)\b|\bsee nothing\b")


def words(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", text.lower()).split()


STOP = {'a', 'an', 'the', 'i', 'you', 'me', 'my', 'your', 'is', 'are', 'was', 'do', 'did', 'does', 'to', 'of', 'and', 'or', 'but', 'it', 'that', 'this', 'what', 'so', 'be', 'am', 'not', 'don', 't', 's', 'm', 're', 've', 'll', 'd', 'can', "can't"}


def restates(line: str, reply: str) -> bool:
    """Whether a reply mostly says the person's line back: 60% or more of its content words (2 or more)."""
    content = [w for w in words(line) if w not in STOP and len(w) > 2]
    return len(content) >= 2 and sum(w in set(words(reply)) for w in content) / len(content) >= 0.6


def content(reply: str) -> list[str]:
    """The sentences of a reply that say something: not announcements or filler ("I'll…", "Let's see…")."""
    sentences = re.split(r"(?<=[.!?])\s+|\.\.\.\s*", reply.lower().replace("\u2019", "'"))
    return [s for s in sentences if len(words(s)) >= 3 and not ANNOUNCES.match(s.strip())]


class Session:
    """A throwaway kernel (fresh temp state, random port) that replays lines as the robot sent them."""

    @classmethod
    def boot(cls, manifest) -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]
        cls.state = tempfile.mkdtemp()
        env = {**os.environ, "SAGE_STATE_DIR": cls.state, "SAGE_DEVICE_PORT": str(cls.port),
               "SAGE_DEVICES": f"reachy={TOKEN}", "SAGE_MODEL_URL": URL, "SAGE_MODEL": MODEL}
        cls.kernel = subprocess.Popen([sys.executable, "-m", "sage"], cwd=ROOT, env=env, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while cls.call("GET", "/v1/effects")[0] != 401 and time.monotonic() < deadline:
            time.sleep(0.05)
        return cls.call("POST", "/v1/hello", {"protocols": [1], "manifest": manifest})[1]["sequence"]

    @classmethod
    def replay(cls, manifest, lines, barge=(), greet=False) -> list[dict]:
        since, turns = cls.boot(manifest), []
        if greet:
            turns.append(cls.greet(since))
            since = cls.call("GET", "/v1/effects?since=0")[1]["sequence"]
        for index, line in enumerate(lines):
            turns.append(cls.turn(f"live{index}", line, since, interrupt=line in barge))
            since = cls.call("GET", "/v1/effects?since=0")[1]["sequence"]
        cls.kernel.terminate()
        cls.kernel.wait(5)
        return turns

    @classmethod
    def greet(cls, since) -> dict:
        """The person comes into view: the kernel's greeting, played in full."""
        start, reply = time.monotonic(), None
        cls.call("POST", "/v1/events", {"kind": "presence", "state": "seen", "confidence": 1.0})
        while reply is None and time.monotonic() - start < 30:
            body = cls.call("GET", f"/v1/effects?since={since}&wait=5")[1]
            for effect in body["effects"]:
                if effect.get("sequence") and (effect.get("turn_id") or "").startswith("kernel-"):
                    cls.call("POST", "/v1/events", {"kind": "result", "sequence": effect["sequence"], "status": "done"})
                    reply = effect["text"] if effect["kind"] == "reply" else reply
            since = body["sequence"]
        return {"line": "(greeting)", "reply": reply or "", "first_s": None, "emote": None, "spoken": reply or "",
                "diverged": False}

    @classmethod
    def call(cls, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {TOKEN}", **({"Content-Type": "application/json"} if data else {})}
        try:
            with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}", data, headers,
                                                               method=method), timeout=40) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")
        except OSError:
            return 0, None

    @classmethod
    def turn(cls, turn_id, line, since, interrupt=False):
        start, first, offset, reply, emote, streamed = time.monotonic(), None, 0, None, None, ""
        cls.call("POST", "/v1/events", {"kind": "submitted", "turn_id": turn_id, "text": line})
        while reply is None and time.monotonic() - start < 30:
            body = cls.call("GET", f"/v1/effects?since={since}&wait=5&offset={offset}")[1]
            for effect in body["effects"]:
                if effect["kind"] == "reply_delta":
                    first = first or round(time.monotonic() - start, 3)
                    offset += len(effect["text"])
                    streamed += effect["text"]
                    if interrupt:  # a barge-in: the person speaks over the first piece
                        cls.call("POST", "/v1/events", {"kind": "result", "turn_id": turn_id, "status": "interrupted",
                                                        "text": streamed})
                        return {"line": line, "reply": "", "first_s": first, "emote": emote, "spoken": streamed,
                                "interrupted": True, "diverged": False}
                    continue
                if effect["turn_id"] == turn_id:  # the robot played everything it was given
                    cls.call("POST", "/v1/events", {"kind": "result", "sequence": effect["sequence"], "status": "done"})
                    if effect["kind"] == "reply":
                        reply = effect["text"]
                    elif effect["kind"] == "emote":
                        emote = effect["input"].get("name")
            if not any(effect["kind"] == "reply_delta" for effect in body["effects"]):
                since = body["sequence"]
        reply = reply or ""
        # What the robot says: the pieces as they stream, then whatever of the final reply they did not hold.
        diverged = not reply.startswith(streamed.rstrip())
        spoken = streamed if diverged else streamed + reply[len(streamed):]
        return {"line": line, "reply": reply, "first_s": first, "emote": emote, "spoken": spoken.strip(),
                "diverged": diverged}

    @classmethod
    def report(cls, turns) -> None:
        first = sorted(t["first_s"] for t in turns if t["first_s"] is not None)
        print(f"\n{cls.__name__} on {MODEL}: first reply piece p50 {statistics.median(first):.2f} s, "
              f"p95 {first[int(0.95 * (len(first) - 1))]:.2f} s, max {max(first):.2f} s, over {len(first)} turns; emote chosen "
              f"{sum(t['emote'] is not None for t in turns)}/{len(turns)}", file=sys.stderr)
        for t in turns:
            flag = "  RESTATES THE LINE" if restates(t["line"], t["reply"]) else ""
            print(f"  {t['line']!r} -> {t['spoken']!r} [{t['emote']}] ({t['first_s']} s){flag}", file=sys.stderr)


class LiveConversation(Session, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        lines = CASE["lines"] + list(SIGHT) + list(ASSIST) + list(ACT) + [RECALL] + list(BARGE) + [AFTER_BARGE]
        cls.turns = cls.replay(CASE["manifest"], lines, BARGE)
        state = cls.state
        with sqlite3.connect(Path(state) / "kernel.sqlite3") as journal:  # what the model was told, as journaled
            cls.tool_messages = [m["content"] for (body,) in journal.execute(
                "SELECT body FROM journal WHERE kind IN ('decided', 'rejected')")
                for m in json.loads(body)["prompt"]["context"] if m["role"] == "tool"]
        cls.report(cls.turns)
        print(f"  restating: {sum(restates(t['line'], t['reply']) for t in cls.turns)}/{len(cls.turns)}",
              file=sys.stderr)

    def test_what_streamed_is_what_was_journaled(self):
        for t in self.turns:
            with self.subTest(line=t["line"]):
                self.assertFalse(t["diverged"], (t["spoken"], t["reply"]))

    def test_the_body_s_reports_never_say_done_and_always_name_the_body(self):
        """The fake robot reports every emote done; live run 2 replayed that as "(your  body ...: emote: done)"."""
        for message in self.tool_messages:
            with self.subTest(message=message):
                self.assertNotIn("done", message)
                self.assertNotIn("your  body", message)
                self.assertRegex(message, r"^\(your reachy body could not do \w+: [^:]+\)$"
                                          r"|^\(the person interrupted you (after: \".*\"|before you said anything)\)$")

    def test_a_body_without_vision_never_claims_sight(self):
        for t in self.turns:
            if t["line"] in SIGHT:
                with self.subTest(line=t["line"]):
                    self.assertNotRegex(t["reply"].lower().replace("’", "'"), CLAIMS_SIGHT)

    def test_asked_what_it_sees_it_says_it_cannot_see(self):
        reply = next(t["reply"] for t in self.turns if t["line"] == SIGHT[1])
        self.assertRegex(reply.lower().replace("’", "'"), SAYS_BLIND)

    def test_no_reply_speaks_a_tool_s_syntax(self):
        """Live run 3 spoke "(emote: )" aloud, copying the reminder's shape."""
        for name in (tool["name"] for tool in CASE["manifest"]["tools"]):
            syntax = re.compile(rf"[(\[][^)\]]*\b{name}\b|\b{name}\s*:|:\s*{name}\b", re.IGNORECASE)
            for t in self.turns:
                with self.subTest(line=t["line"], tool=name):
                    self.assertNotRegex(t["reply"], syntax)

    def test_no_sentence_is_spoken_in_two_replies_in_a_row(self):
        def sentences(text):
            return {" ".join(words(s)) for s in re.split(r"(?<=[.!?])\s+", text) if words(s)}
        for before, after in zip(self.turns, self.turns[1:]):
            if after["line"] == RECALL:
                continue  # asked what it just said, saying it again is the honest answer
            with self.subTest(line=after["line"]):
                self.assertFalse(sentences(before["spoken"]) & sentences(after["spoken"]),
                                 (before["spoken"], after["spoken"]))

    def test_no_reply_restates_the_person_s_line(self):
        for t in self.turns:
            if t["line"] != RECALL:
                with self.subTest(line=t["line"]):
                    self.assertFalse(restates(t["line"], t["reply"]), t["reply"])

    def test_a_long_story_ends_on_a_whole_sentence(self):
        for t in self.turns:
            if len(t["reply"]) > 120:
                with self.subTest(line=t["line"]):
                    self.assertRegex(t["reply"], r"[.!?\"']$")

    def test_asked_to_act_it_acts_instead_of_announcing(self):
        for t in self.turns:
            if t["line"] in ACT:
                with self.subTest(line=t["line"]):
                    self.assertTrue(content(t["reply"]), t["reply"])

    def test_asked_what_it_just_said_it_quotes_its_last_reply(self):
        index = next(i for i, t in enumerate(self.turns) if t["line"] == RECALL)
        before, reply = words(self.turns[index - 1]["spoken"]), set(" ".join(words(self.turns[index]["reply"])).split())
        runs = {" ".join(before[i:i + 3]) for i in range(len(before) - 2)}
        said = " ".join(words(self.turns[index]["reply"]))
        self.assertTrue(any(run in said for run in runs) or (before and set(before) <= reply),
                        (self.turns[index - 1]["spoken"], self.turns[index]["reply"]))

    def test_after_barge_ins_no_reply_speaks_an_annotation(self):
        for t in self.turns:
            with self.subTest(line=t["line"]):
                self.assertNotRegex(t["spoken"].lower(), r"interrupted|\(the person|\[json|\(your \w* ?body")

    def test_every_line_gets_a_reply(self):
        self.assertTrue(all(t["reply"] for t in self.turns if not t.get("interrupted")), self.turns)

    def test_no_reply_mentions_a_body(self):
        for t in self.turns:
            with self.subTest(line=t["line"]):
                self.assertNotRegex(t["reply"].lower(), r"\bbod(y|ies)\b")

    def test_no_two_replies_share_an_opening_template(self):
        openings = [tuple(words(t["reply"])[:4]) for t in self.turns if len(words(t["reply"])) >= 4]
        self.assertEqual(len(openings), len(set(openings)), [t["reply"] for t in self.turns])

    def test_each_reply_is_short(self):
        for t in self.turns:
            with self.subTest(line=t["line"]):
                self.assertLessEqual(len(t["reply"]), 160)

    def test_each_reply_answers_its_own_line_not_another(self):
        """Live, one reply quoted the line before. No 4-word run of another line may appear in a reply."""
        def runs(text):
            w = words(text)
            return {" ".join(w[i:i + 4]) for i in range(len(w) - 3)}
        for index, t in enumerate(self.turns):
            if t["line"] in CASE["may_quote"]:
                continue
            others = set().union(*(runs(line) for other, line in enumerate(CASE["lines"]) if other != index))
            with self.subTest(line=t["line"]):
                self.assertFalse(runs(t["reply"]) & (others - runs(t["line"])), t["reply"])


# Live run 5b (qwen3:1.7b): replies that say nothing of their own.
STOCK = {"thank you", "thanks", "i m sorry", "sorry", "i m here", "i m here for you", "hello", "hi", "hey", "okay",
         "ok", "sure", "yes", "no", "thank god", "that s great", "great", "i see", "got it", "i understand",
         "you re right", "alright", "mm hm", "let s talk"}
INTERRUPTION = re.compile(r"\binterrupt|\bcut (me )?off\b", re.IGNORECASE)
# Live, phi4-mini on the old persona: "How can I help you today?", "feel free to ask", "Certainly, ...".
ASSISTANT = re.compile(r"\bassist|\bhow (can|may) i help\b|\bfeel free to ask\b|\bcertainly\b", re.IGNORECASE)


def hollow(reply: str, name: str = "") -> bool:
    """Whether every sentence of a reply is a stock phrase or one word (the person's name aside)."""
    sentences = [" ".join(w for w in words(s) if w != name.lower()) for s in re.split(r"(?<=[.!?,])\s+", reply)]
    return all(s in STOCK or len(s.split()) <= 1 for s in sentences)


def echoes(line: str, reply: str) -> bool:
    """Whether a reply only says the person's line back: restates it, or uses no word the line did not."""
    said = words(reply)
    return restates(line, reply) or bool(said) and set(said) <= set(words(line))


def engages(line: str, reply: str) -> bool:
    """Whether a reply asks back or picks up a word of the person's line without echoing it."""
    picked = {w for w in words(line) if w not in STOP and len(w) > 2} & set(words(reply))
    return "?" in reply or bool(picked) and not echoes(line, reply)


class LiveRun5b(Session, unittest.TestCase):
    """Live run 5b, replayed: "I'm here for you." eight times in a row, hollow and parroted replies,
    "I was interrupted" spoken, and the person's name lost. Then one probe: their name."""

    @classmethod
    def setUpClass(cls):
        name = RUN_5B["name"]
        cls.turns = cls.replay(RUN_5B["manifest"], RUN_5B["assistant_lines"] + RUN_5B["lines"] + [name["probe"]],
                                RUN_5B["barge_in"])
        cls.answered = [t for t in cls.turns if not t.get("interrupted") and t["line"] != name["probe"]]
        cls.report(cls.turns)
        print(f"  hollow {sum(hollow(t['reply'], name['name']) for t in cls.answered)}, echoes "
              f"{sum(echoes(t['line'], t['reply']) for t in cls.answered)}, engages "
              f"{sum(engages(t['line'], t['reply']) for t in cls.answered)} of {len(cls.answered)}", file=sys.stderr)

    def test_no_reply_is_said_three_times_or_more(self):
        said = [" ".join(words(t["spoken"])) for t in self.turns if words(t["spoken"])]
        self.assertLess(max(map(said.count, said)), 3, said)

    def test_replies_say_more_than_stock_phrases(self):
        empty = [t["reply"] for t in self.answered if hollow(t["reply"], RUN_5B["name"]["name"])]
        self.assertLessEqual(len(empty), 2, empty)

    def test_no_reply_says_the_person_s_line_back(self):
        for t in self.answered:
            with self.subTest(line=t["line"]):
                self.assertFalse(echoes(t["line"], t["reply"]), t["reply"])

    def test_no_reply_talks_of_being_interrupted(self):
        for t in self.turns:
            with self.subTest(line=t["line"]):
                self.assertNotRegex(t["spoken"], INTERRUPTION)

    def test_at_least_a_quarter_of_replies_engage(self):
        engaged = [t["reply"] for t in self.answered if engages(t["line"], t["reply"])]
        self.assertGreaterEqual(len(engaged), len(self.answered) / 4, [t["reply"] for t in self.answered])

    def test_no_reply_speaks_like_a_generic_assistant(self):
        for t in self.turns:
            with self.subTest(line=t["line"]):
                self.assertNotRegex(t["spoken"], ASSISTANT)

    def test_it_holds_the_person_s_name(self):
        name = RUN_5B["name"]
        probe = next(t["reply"] for t in self.turns if t["line"] == name["probe"])
        self.assertIn(name["name"].lower(), words(probe), probe)
        for t in self.turns:
            with self.subTest(line=t["line"]):
                self.assertFalse({n.lower() for n in name["not"]} & set(words(t["spoken"])), t["spoken"])


DONT_KNOW = re.compile(r"\bi don'?t (understand|know)\b|\bi'?ve never heard\b", re.IGNORECASE)


def invented_names(reply: str, said: str) -> set[str]:
    """Capitalised words inside a sentence that the person never said: names made up ("Hey there, Alex!")."""
    names = set()
    for sentence in re.split(r"(?<=[.!?])\s+", reply):
        for word in re.findall(r"(?<=[\s,])([A-Z][a-z]+)", sentence):
            if word not in ("I", "Sage") and word.lower() not in words(said):
                names.add(word)
    return names


class LiveRun4b(Session, unittest.TestCase):
    """qwen3:4b-instruct's first live session: a made-up name, lyrics answered, five "I don't know"s, fragments."""

    @classmethod
    def setUpClass(cls):
        case = RUN_5B["run_4b"]
        cls.turns = cls.replay(RUN_5B["manifest"], case["lines"], greet=True)
        cls.report(cls.turns)

    def test_no_reply_uses_a_name_the_person_never_said(self):
        said = " ".join(t["line"] for t in self.turns)
        for t in self.turns:
            with self.subTest(line=t["line"]):
                self.assertFalse(invented_names(t["spoken"], said), t["spoken"])

    def test_lyrics_are_not_answered_as_the_person_s_words(self):
        for t in self.turns:
            if t["line"] in RUN_5B["run_4b"]["lyrics"]:
                self.assertNotRegex(t["reply"].lower(), r"missed you too|i missed you", t["reply"])

    def test_never_more_than_two_i_don_t_knows_in_a_row(self):
        flat = [bool(DONT_KNOW.search(t["reply"].replace("\u2019", "'"))) for t in self.turns]
        self.assertNotIn([True] * 3, [flat[i:i + 3] for i in range(len(flat))], [t["reply"] for t in self.turns])

    def test_a_fragment_gets_a_guess_or_a_huh(self):
        for t in self.turns:
            if t["line"] in RUN_5B["run_4b"]["fragments"]:
                with self.subTest(line=t["line"]):
                    self.assertIn("?", t["reply"])
                    self.assertNotRegex(t["reply"].replace("\u2019", "'"), DONT_KNOW)

    def test_no_reply_says_the_person_s_line_back(self):
        for t in self.turns[1:]:
            with self.subTest(line=t["line"]):
                self.assertFalse(echoes(t["line"], t["reply"]), t["reply"])


if __name__ == "__main__":
    unittest.main()
