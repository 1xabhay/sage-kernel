"""The research server's contract, offline: the web and Shazam are fakes.

    cd services && uv run python -m unittest discover -s research -t research
"""
import asyncio
import re
import unittest

import app
import httpx
from fastapi.testclient import TestClient

TITLE, ARTIST = "Blinding Lights", "The Weeknd"
PARAGRAPH = ("The song was written over a long night in the studio. It borrows its drums from eighties synth-pop, "
             "and its bass line runs under every verse. Critics called it a throwback that sounded new. ")
SONG_ARTICLE = (f"\"{TITLE}\" is a song by {ARTIST}. {PARAGRAPH}\n\n"
                f"== Background and development ==\n{PARAGRAPH * 3}\n\n"
                f"== Commercial performance ==\n" + "\n\n".join([PARAGRAPH * 2] * 60) + "\n\n"
                "== References ==\nShould never be read.")
ARTIST_ARTICLE = (f"{ARTIST} is a Canadian singer. {PARAGRAPH}\n\n"
                  "== Personal life ==\n" + "\n\n".join([PARAGRAPH * 2] * 40))
LYRICS = "I've been tryna call\nI've been on my own for long enough\n\nI said, ooh, I'm blinded by the lights"
HTML = ("<html><head><script>var junk = 1;</script><style>p {}</style></head><body><nav>Home About Contact</nav>"
        f"<p>{PARAGRAPH}</p></body></html>")


def mb(request):
    path = request.url.path
    if path == "/ws/2/recording":
        return {"recordings": [
            {"id": "live", "score": 100, "title": TITLE, "disambiguation": "live", "releases": []},
            {"id": "rec", "score": 100, "title": TITLE, "length": 200046, "first-release-date": "2019-11-29",
             "artist-credit": [{"name": ARTIST, "joinphrase": " feat. ", "artist": {"id": "art", "name": ARTIST}},
                               {"name": "Guest", "artist": {"id": "g", "name": "Guest"}}],
             "releases": [
                 {"id": "single", "title": TITLE, "status": "Official", "date": "2019-11-29",
                  "release-group": {"primary-type": "Single"}},
                 {"id": "album", "title": "After Hours", "status": "Official", "date": "2020-03-20",
                  "release-group": {"primary-type": "Album"}}]}]}
    if path == "/ws/2/recording/rec":
        return {"genres": [{"name": "synth-pop", "count": 3}, {"name": "jazz", "count": 1}], "relations": [
            {"type": "producer", "artist": {"name": "Max Martin"}, "attributes": []},
            {"type": "performance", "work": {"title": TITLE, "relations": [
                {"type": "writer", "artist": {"name": "Belly"}}]}}]}
    if path == "/ws/2/release/album":
        return {"label-info": [{"label": {"name": "XO"}}, {"label": {"name": "Republic"}}]}
    if path == "/ws/2/artist/art":
        return {"name": ARTIST, "type": "Person", "area": {"name": "Canada"},
                "life-span": {"begin": "1990-02-16"}, "genres": [{"name": "r&b", "count": 5}]}
    return None


def web(request):
    host, q = request.url.host, request.url.params
    if host == "musicbrainz.org":
        body = mb(request)
        return httpx.Response(200, json=body) if body else httpx.Response(404)
    if host == "en.wikipedia.org":
        song = "song" in q["gsrsearch"]
        title, text = (TITLE, SONG_ARTICLE) if song else (ARTIST, ARTIST_ARTICLE)
        return httpx.Response(200, json={"query": {"pages": [{"title": title, "extract": text}]}})
    if host == "lrclib.net":
        return httpx.Response(200, json={"plainLyrics": LYRICS})
    if host == "searxng":
        if q.get("categories") == "news":
            return httpx.Response(200, json={"results": [
                {"title": "The Weeknd announces a tour", "content": "Dates for next year.",
                 "url": "https://news.example/tour", "publishedDate": "2026-10-12T08:00:00"}]})
        return httpx.Response(200, json={"results": [
            {"title": "What Blinding Lights means", "content": "A song about longing.",
             "url": f"https://www.page{i}.example/blinding"} for i in range(5)]})
    if host.endswith(".example"):
        return httpx.Response(200, html=HTML)
    return httpx.Response(404)


class FakeShazam:
    answer = {}

    async def recognize(self, data):
        return self.answer


class ResearchContract(unittest.TestCase):
    def setUp(self):
        self.handler = web
        app.TRANSPORT = httpx.MockTransport(lambda r: self.handler(r))
        app.MB_GAP = 0
        app.Shazam = FakeShazam
        self.client = TestClient(app.app)

    def research(self):
        r = self.client.post("/research", json={"title": TITLE, "artist": ARTIST})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_health_is_ready_when_searxng_answers(self):
        self.assertEqual(self.client.get("/health").json(), {"ready": True})

    def searxng_health(self, answer):
        """/health with SearXNG's /healthz answering `answer` (a status code, or an exception to raise)."""
        def handler(request):
            if request.url.host == "searxng" and request.url.path == "/healthz":
                if isinstance(answer, Exception):
                    raise answer
                return httpx.Response(answer)
            return web(request)
        self.handler = handler
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)  # a dead dependency is not a dead container
        return r.json()

    def test_health_is_not_ready_when_searxng_does_not_answer(self):
        body = self.searxng_health(httpx.ConnectError("connection refused"))
        self.assertIs(body["ready"], False)
        self.assertIn("connection refused", body["searxng"])

    def test_health_is_not_ready_when_searxng_is_unhealthy(self):
        body = self.searxng_health(503)
        self.assertIs(body["ready"], False)
        self.assertIn("503", body["searxng"])

    def test_the_topic_names_the_song(self):
        self.assertEqual(self.research()["topic"], '"Blinding Lights" by The Weeknd')

    def test_passage_zero_is_the_card(self):
        card = self.research()["passages"][0]
        for part in ("Card", TITLE, ARTIST, "Guest", "After Hours", "2019-11-29", "synth-pop", "3:20", "XO"):
            self.assertIn(part, card)
        self.assertNotIn("\n", card)

    def test_every_passage_is_tagged_and_at_most_600_characters(self):
        passages = self.research()["passages"]
        tags = r"^(Card|Credits|Releases|MusicBrainz|Wikipedia|Lyrics|News|Web)[^:]*: "
        for p in passages:
            self.assertLessEqual(len(p), 600, p)
            self.assertRegex(p, tags)

    def test_each_source_answers(self):
        text = "\n".join(self.research()["passages"])
        for part in ("Producer: Max Martin", "Writer: Belly", "MusicBrainz, the artist", "Wikipedia, the song: ",
                     "Wikipedia, the song, Background and development", "Wikipedia, the artist: ",
                     app.LYRICS + ": I've been tryna call", app.NEWS + ", 2026-10-12, news.example: The Weeknd announces",
                     "Web, page0.example: What Blinding Lights means", "Web, page1.example: The song was written"):
            self.assertIn(part, text)
        self.assertNotIn("var junk", text)
        self.assertNotIn("Home About Contact", text)
        self.assertNotIn("Should never be read", text)

    def test_the_cap_keeps_64_and_trims_the_least_useful(self):
        passages = self.research()["passages"]
        self.assertEqual(len(passages), 64)
        text = "\n".join(passages)
        self.assertIn(app.LYRICS, text)
        self.assertIn("News, ", text)
        self.assertNotIn("Wikipedia, the artist, Personal life", text)  # the artist's long body goes first
        first = lambda tag: next(i for i, p in enumerate(passages) if p.startswith(tag))
        self.assertLess(first("Credits"), first("Lyrics"))
        self.assertLess(first("Lyrics"), first("Wikipedia, the song, Commercial performance"))

    def test_a_failing_source_is_skipped(self):
        def broken(request):
            if request.url.host == "lrclib.net":
                raise httpx.ConnectError("down")
            if request.url.host == "musicbrainz.org":
                return httpx.Response(503)
            return web(request)
        self.handler = broken
        passages = self.research()["passages"]
        self.assertTrue(passages[0].startswith("Card: "))
        self.assertIn(TITLE, passages[0])
        self.assertFalse(any(p.startswith(("Lyrics", "Credits", "Releases", "MusicBrainz")) for p in passages))
        self.assertTrue(any(p.startswith("Wikipedia") for p in passages))

    def test_a_slow_source_is_dropped_at_the_budget(self):
        async def slow(request):
            if request.url.host == "lrclib.net":
                await asyncio.sleep(5)
            return web(request)
        self.handler = slow
        app.BUDGET, budget = 0.5, app.BUDGET
        try:
            passages = self.research()["passages"]
        finally:
            app.BUDGET = budget
        self.assertFalse(any(p.startswith("Lyrics") for p in passages))
        self.assertTrue(any(p.startswith("Wikipedia") for p in passages))

    def identify(self, answer):
        FakeShazam.answer = answer
        return self.client.post("/identify", files={"file": ("a.wav", b"RIFF....WAVE", "audio/wav")}).json()

    def test_identify_not_found(self):
        self.assertEqual(self.identify({"matches": []}), {"found": False})

    def test_identify_found(self):
        track = {"title": TITLE, "subtitle": ARTIST, "genres": {"primary": "Pop"}, "sections": [
            {"type": "SONG", "metadata": [{"title": "Album", "text": "After Hours"},
                                          {"title": "Released", "text": "2019"}]}]}
        self.assertEqual(self.identify({"matches": [{}], "track": track}), {
            "found": True, "title": TITLE, "artist": ARTIST, "album": "After Hours", "year": "2019", "genre": "Pop"})


class Chunking(unittest.TestCase):
    def test_sentences_stay_whole(self):
        text = " ".join(f"Sentence number {i} has a few words in it." for i in range(60))
        chunks = app.chunk(text, 200)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 200)
            self.assertRegex(c, r"^Sentence number \d+ .*\.$")
        self.assertEqual(" ".join(chunks), text)

    def test_paragraphs_are_kept_apart_before_sentences_are(self):
        chunks = app.chunk("First paragraph.\n\nSecond paragraph.", 20)
        self.assertEqual(chunks, ["First paragraph.", "Second paragraph."])

    def test_a_run_without_punctuation_splits_between_words(self):
        text = " ".join(["word"] * 300)
        chunks = app.chunk(text, 100)
        self.assertTrue(all(len(c) <= 100 and re.fullmatch(r"word( word)*", c) for c in chunks))
        self.assertEqual(" ".join(chunks), text)


if __name__ == "__main__":
    unittest.main()
