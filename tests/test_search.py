"""Web search: which lines ask the web, what leaves the Mac, and the turn answered from what came back."""

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from sage import policy
from sage.adapters import search
from sage.journal import Journal
from sage.kernel import Kernel

SURE = {"medium": "speech", "confidence": 0.95}


class Asks(unittest.TestCase):
    def test_questions_about_fresh_or_outside_facts_ask_the_web(self):
        for line in ("What's the weather like today?", "Who won the game last night?", "Who is Taylor Swift?",
                     "Can you look up when the next eclipse is?", "What's the latest news?",
                     "When does the new Zelda come out?", "Search for pasta recipes."):
            with self.subTest(line):
                self.assertTrue(policy.needs_search(line, SURE, ()))

    def test_talk_with_sage_itself_does_not(self):
        for line in ("How are you?", "What's your name?", "I love the news.", "Do you like music?",
                     "What did you just say?", "Thanks!", "Who are you?", "What is it?", "What was I doing?"):
            with self.subTest(line):
                self.assertEqual(policy.needs_search(line, SURE, ()), "")

    def test_a_search_word_inside_heard_lyrics_is_no_request(self):  # replay, 11-05: a song's line said "Google"
        line = "Spent a couple tickets on the bag and Google got a top five, then she took the long way home."
        self.assertEqual(policy.needs_search(line, SURE, ()), "")
        self.assertTrue(policy.needs_search("Please google the opening hours of the library.", SURE, ()))

    def test_a_line_that_may_be_misheard_never_leaves_the_mac(self):
        self.assertEqual(policy.needs_search("what's the weather today", None, ()), "")  # unfinished
        self.assertEqual(policy.needs_search("What's the weather today?", {"medium": "speech", "confidence": 0.4}, ()), "")
        self.assertTrue(policy.needs_search("what's the weather today", {"medium": "text"}, ()))

    def test_a_kernel_note_is_not_a_question(self):
        self.assertEqual(policy.needs_search("[The person is back. Who is it?]", SURE, ()), "")

    def test_names_the_person_told_sage_stay_home(self):
        facts = ("Their name is Mika.", "Their sister Kayla lives in Leeds.")
        query = policy.needs_search("What's the news in Leeds for Kayla today?", SURE, facts)
        self.assertTrue(query)
        self.assertNotIn("Kayla", query)
        self.assertNotIn("Leeds", query)  # a place in a fact goes too: a rule cannot tell it from a name
        self.assertIn("news", query)
        self.assertEqual(policy.needs_search("Who is Mika?", SURE, facts), "")  # nothing left to ask
        self.assertNotIn("Mika", policy.needs_search("What's the weather for Mika's run today?", SURE, facts))


class Turn(unittest.TestCase):
    def kernel(self, found):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        prompts, asked = [], []

        def decide(prompt, heard):
            prompts.append(prompt)
            heard(json.dumps({"reply": "Sunny."}))
            return json.dumps({"reply": "Sunny."})

        def look(query):
            asked.append(query)
            return found(query)
        kernel = Kernel(Journal(Path(directory.name) / "k.sqlite3"), decide, search=look)
        kernel.hello("reachy", {"protocols": [1], "manifest": {"senses": ["speech"], "tools": []}})
        return kernel, prompts, asked

    def ask(self, kernel, text, turn_id="r1"):
        kernel.event("reachy", {"kind": "submitted", "turn_id": turn_id, "text": text, "input": SURE})
        deadline = time.monotonic() + 5
        while kernel.state.turn.status == "active" and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_the_answer_is_written_from_what_the_web_said_and_the_notes_are_kept(self):
        kernel, prompts, asked = self.kernel(lambda q: ["metoffice.gov.uk: Today sunny, 21 C, light wind."])
        self.ask(kernel, "What's the weather like today?")
        self.assertEqual(asked, ["What's the weather like today?"])
        self.assertIn("Today sunny, 21 C", prompts[-1].context[-1]["content"])
        self.assertIn("found on the web", prompts[-1].context[-1]["content"])
        self.assertEqual(kernel.state.topic, "What's the weather like today?")  # journaled, with its source

    def test_a_line_that_needs_no_web_is_not_searched(self):
        kernel, prompts, asked = self.kernel(lambda q: ["x"])
        self.ask(kernel, "How are you?")
        self.assertEqual(asked, [])
        self.assertEqual(len(prompts), 1)

    def test_nothing_found_or_a_failed_search_still_answers(self):
        def down(query):
            raise OSError("research is down")
        for found in (lambda q: [], down):
            with self.subTest(found):
                kernel, prompts, _ = self.kernel(found)
                self.ask(kernel, "Who won the game last night?")
                self.assertEqual(len(prompts), 1)
                self.assertNotIn("found on the web", prompts[-1].context[-1]["content"])
                self.assertEqual(kernel.state.exchanges[-1].reply, "Sunny.")

    def test_a_turn_stopped_while_searching_never_reaches_the_model(self):
        started, release = threading.Event(), threading.Event()

        def slow(query):
            started.set()
            release.wait(5)
            return ["bbc.co.uk: a score."]
        kernel, prompts, _ = self.kernel(slow)
        kernel.event("reachy", {"kind": "submitted", "turn_id": "r1", "text": "Who won the game last night?",
                                "input": SURE})
        self.assertTrue(started.wait(5))
        kernel.event("reachy", {"kind": "stop", "turn_id": "r1"})
        release.set()
        time.sleep(0.2)
        self.assertEqual(prompts, [])


class Adapter(unittest.TestCase):
    def serve(self, reply, delay=0.0):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                time.sleep(delay)
                body = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}", seen

    def test_it_posts_the_query_and_returns_capped_passages(self):
        url, seen = self.serve({"topic": "q", "passages": ["a" * 900] + [f"p{n}" for n in range(20)]})
        passages = search.provider(url)("Who won?")
        self.assertEqual(seen, [("/search", {"q": "Who won?"})])
        self.assertEqual(len(passages), search.MAX_PASSAGES)
        self.assertEqual(len(passages[0]), 600)

    def test_a_slow_search_gives_up_within_its_budget(self):
        url, _ = self.serve({"passages": ["late"]}, delay=1.0)
        started = time.monotonic()
        with self.assertRaises(OSError):
            search.provider(url, timeout=0.3)("Who won?")
        self.assertLess(time.monotonic() - started, 0.9)

    def test_only_this_machine_over_plain_http(self):
        for url in ("http://example.com:8783", "ftp://127.0.0.1", "http://127.0.0.1:8783/path"):
            with self.subTest(url), self.assertRaises(ValueError):
                search.provider(url)


if __name__ == "__main__":
    unittest.main()
