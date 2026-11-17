"""The TTS server's contract, as the voice uses it (reachy_sage_voice/tts.py). Loads the real model.

    cd services && uv run python -m unittest discover -s tts -t tts
"""
import base64
import json
import os
import pathlib
import unittest
from unittest import mock

import app
from fastapi.testclient import TestClient

RATE, WIDTH = 24000, 2  # "pcm" is 16-bit mono at 24 kHz
POCKET_VOICES = {"alba", "anna", "azelma", "bill_boerst", "caro_davy", "charles", "cosette", "eponine", "eve",
                 "fantine", "george", "jane", "javert", "jean", "marius", "mary", "michael", "paul",
                 "peter_yearsley", "stuart_bell", "vera"}
ENDPOINTS = ("/v1/audio/speech", "/dev/captioned_speech")
# app.py's own claim for a streamed sentence's guessed length: "off by up to a quarter".
GUESS_TOLERANCE = 0.25
client = None


def setUpModule():
    """Start the app: its lifespan loads the model, once for every test here."""
    global client
    client = TestClient(app.app).__enter__()


def tearDownModule():
    """Stop the app."""
    client.__exit__(None, None, None)


def seconds(audio: bytes) -> float:
    """Return how long raw pcm plays."""
    return len(audio) / WIDTH / RATE


class SpeechContract(unittest.TestCase):
    def speak(self, path="/v1/audio/speech", **body):
        # The fields the voice sends (reachy_sage_voice/tts.py _speak); "kokoro" is its model name.
        return client.post(path, json={"model": "kokoro", "voice": "azelma", "speed": 1.0,
                                       "response_format": "pcm", **body})

    def test_health_is_ready_once_the_model_is_loaded(self):
        self.assertEqual(client.get("/health").json(), {"ready": True})

    def test_a_reply_comes_back_as_raw_pcm_of_a_plausible_length(self):
        r = self.speak(input="Good morning! I was watching the light on your desk.")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "audio/pcm")
        self.assertEqual(len(r.content) % WIDTH, 0)
        self.assertTrue(1.5 < seconds(r.content) < 6, len(r.content))

    def test_audio_streams_frame_by_frame(self):
        # Several chunks, not one body. The test client cannot show when each left:
        # bench/first_audio.py measures that against a running server.
        with client.stream("POST", "/v1/audio/speech", json={
                "voice": "azelma", "response_format": "pcm", "input": "Hello there. How are you today?"}) as r:
            chunks = [c for c in r.iter_raw() if c]
        self.assertGreaterEqual(len(chunks), 2)

    def test_the_fields_the_voice_sends_are_accepted(self):
        # Pocket has no speed control and one model: the voice styles' speeds and any model name
        # are ignored, and so are OpenAI's acting notes.
        extras = ({"speed": 0.9}, {"speed": 1.1}, {"model": "pocket"}, {"instructions": "cheerfully"})
        for extra in extras:
            with self.subTest(extra):
                self.assertEqual(self.speak(input="Sleep well.", **extra).status_code, 200)

    def test_azelma_speaks_at_the_owners_temperature(self):
        """The owner's choice, 2026-10-23."""
        self.assertEqual(app._model.temp, 0.6)

    def test_azelma_is_the_only_voice_and_any_other_is_a_400_naming_the_list(self):
        """The others' licences are unverified; cosette and jean are non-commercial."""
        self.assertEqual(client.get("/v1/audio/voices").json(), {"voices": ["azelma"]})
        for voice in ("alloy", "alba", "cosette"):
            with self.subTest(voice):
                r = self.speak(input="Hi.", voice=voice)
                self.assertEqual(r.status_code, 400)
                self.assertIn("/v1/audio/voices", r.text)

    def test_what_it_cannot_say_is_a_400_on_both_endpoints(self):
        for path in ENDPOINTS:
            for why, body in (("not pcm", {"input": "Hi.", "response_format": "mp3"}),
                              ("nothing to say", {"input": "   "}),
                              ("too fast", {"input": "Hi.", "speed": 3.0}),
                              ("not azelma", {"input": "Hi.", "voice": "alloy"})):
                with self.subTest(path=path, why=why):
                    self.assertEqual(self.speak(path, **body).status_code, 400)

    @unittest.skipUnless(os.environ.get("HF_HOME"),
                         "needs the services' own cache: make test and the image set HF_HOME")
    def test_only_azelma_is_baked_in(self):
        baked = {p.name for p in pathlib.Path(os.environ["HF_HOME"]).rglob("*.safetensors")}
        self.assertIn("azelma.safetensors", baked)
        self.assertEqual({n for n in baked if n.split(".")[0] in POCKET_VOICES}, {"azelma.safetensors"})

    def test_a_closed_stream_stops_generating(self):
        # The listener cut in: Pocket is told to stop instead of finishing a reply no one hears.
        stops = []

        def generate(state, text, stop):
            stops.append(stop)
            yield from ("frame one", "frame two")
        with mock.patch.object(app, "_model", mock.Mock(generate_audio_stream=generate)), \
                mock.patch.object(app, "state", lambda voice: None):
            stream = app.frames("Hello there.", "azelma")
            self.assertEqual(next(stream), "frame one")
            self.assertFalse(stops[0].is_set())
            stream.close()
        self.assertTrue(stops[0].is_set())


class CaptionedSpeechContract(unittest.TestCase):
    """POST /dev/captioned_speech, Kokoro-FastAPI's shape: audio plus [{word, start_time, end_time}],
    so the voice knows exactly which words were heard when it is interrupted."""

    def caption(self, text, **body):
        r = client.post("/dev/captioned_speech", json={"voice": "azelma", "response_format": "pcm",
                                                       "input": text, **body})
        self.assertEqual(r.status_code, 200, r.text)
        return [json.loads(line) for line in r.text.splitlines() if line.strip()]

    def test_every_input_word_comes_back_in_order_with_a_span_inside_the_audio(self):
        text = "Good morning! I was watching the light on your desk."
        chunks = self.caption(text, stream=False)
        self.assertEqual(len(chunks), 1)
        c = chunks[0]
        self.assertEqual(c["audio_format"], "audio/pcm")
        self.assertEqual([t["word"] for t in c["timestamps"]], text.split())
        ends = 0.0
        for t in c["timestamps"]:
            self.assertLessEqual(ends, t["start_time"] + 1e-6)
            self.assertLess(t["start_time"], t["end_time"])
            ends = t["end_time"]
        self.assertLessEqual(ends, seconds(base64.b64decode(c["audio"])) + 0.01)

    def test_streamed_frame_by_frame_each_sentences_words_on_its_first_frame(self):
        chunks = self.caption("Hello there. How are you today?", stream=True)
        worded = [i for i, c in enumerate(chunks) if c["timestamps"]]
        self.assertEqual([[t["word"] for t in chunks[i]["timestamps"]] for i in worded],
                         [["Hello", "there."], ["How", "are", "you", "today?"]])
        self.assertEqual(worded[0], 0)
        self.assertGreater(worded[1], 1)  # the first sentence came as several frames
        first = sum(seconds(base64.b64decode(c["audio"])) for c in chunks[:worded[1]])
        start = chunks[worded[1]]["timestamps"][0]["start_time"]
        self.assertAlmostEqual(start, first, delta=0.002)

    def test_a_streamed_sentences_words_end_within_a_quarter_of_its_real_audio(self):
        # Its words go out before its audio is made, timed by a guess (PAUSE_S, LETTERS_PER_S): an
        # interruption reports the words heard only as well as that guess holds.
        text = "I watched the rain on the window this morning. It was lovely."
        chunks = self.caption(text, stream=True)
        second = [i for i, c in enumerate(chunks) if c["timestamps"]][1]
        real = sum(seconds(base64.b64decode(c["audio"])) for c in chunks[:second])
        guessed = chunks[0]["timestamps"][-1]["end_time"]
        self.assertLess(abs(guessed - real) / real, GUESS_TOLERANCE, (guessed, real))

    def test_a_word_spoken_as_several_is_still_one_entry(self):
        # "25" is said as two words, and a name respelled for the voice ("Mee-kah,") is mapped back
        # to its spelling by exact match (sage_device.names): each comes back as it was written.
        for text in ("Set a timer for 25 minutes.", "Hi Mee-kah, I'm Sage."):
            with self.subTest(text):
                words = [t["word"] for c in self.caption(text) for t in c["timestamps"]]
                self.assertEqual(words, text.split())

    def test_words_share_the_sentence_by_their_length(self):
        self.assertEqual(app.words_spoken("one three", 0.8, 1.0),
                         [{"word": "one", "start_time": 1.0, "end_time": 1.3},
                          {"word": "three", "start_time": 1.3, "end_time": 1.8}])


if __name__ == "__main__":
    unittest.main()
