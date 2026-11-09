"""The TTS server's contract, as Pipecat's OpenAITTSService uses it. Loads the real model.

    cd services && uv run python -m unittest discover -s tts -t tts
"""
import base64
import json
import os
import pathlib
import unittest

import app
from fastapi.testclient import TestClient

RATE, WIDTH = 24000, 2  # what Pipecat assumes for "pcm": 16-bit mono at 24 kHz
POCKET_VOICES = {"alba", "anna", "azelma", "bill_boerst", "caro_davy", "charles", "cosette", "eponine", "eve",
                 "fantine", "george", "jane", "javert", "jean", "marius", "mary", "michael", "paul",
                 "peter_yearsley", "stuart_bell", "vera"}


class SpeechContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app.app).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def speak(self, **body):
        # Exactly the fields Pipecat sends (services/openai/tts.py run_tts).
        return self.client.post("/v1/audio/speech", json={"model": "pocket", "voice": "azelma",
                                                          "response_format": "pcm", **body})

    def test_health_is_ready_once_the_model_is_loaded(self):
        self.assertEqual(self.client.get("/health").json(), {"ready": True})

    def test_a_reply_comes_back_as_raw_pcm_of_a_plausible_length(self):
        r = self.speak(input="Good morning! I was watching the light on your desk.")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "audio/pcm")
        self.assertEqual(len(r.content) % WIDTH, 0)
        self.assertTrue(1.5 < len(r.content) / WIDTH / RATE < 6, len(r.content))

    def test_each_sentence_is_streamed_as_soon_as_it_is_made(self):
        with self.client.stream("POST", "/v1/audio/speech", json={
                "voice": "azelma", "response_format": "pcm", "input": "Hello there. How are you today?"}) as r:
            chunks = [c for c in r.iter_raw() if c]
        self.assertGreaterEqual(len(chunks), 2)

    def test_speed_is_accepted_within_range(self):  # Pocket has no speed control: ignored
        self.assertEqual(self.speak(input="Sleep well.", speed=0.9).status_code, 200)
        self.assertEqual(self.speak(input="Sleep well.", speed=3.0).status_code, 400)

    def test_the_owners_voice_and_temperature(self):
        """The owner's choice, 2026-10-23."""
        self.assertEqual((app.VOICE, app._model.temp), ("azelma", 0.6))

    def test_a_respelled_name_is_spoken(self):
        """sage_device.names sends the owner's name like this."""
        audio = self.speak(input="Hi Mee-kah, I'm Sage.").content
        self.assertGreater(len(audio), RATE * WIDTH // 2)

    def test_instructions_are_accepted_and_ignored(self):
        self.assertEqual(self.speak(input="Okay.", instructions="cheerfully").status_code, 200)

    def test_an_unknown_voice_is_a_400_naming_the_list(self):
        r = self.speak(input="Hi.", voice="alloy")
        self.assertEqual(r.status_code, 400)
        self.assertIn("/v1/audio/voices", r.text)
        voices = self.client.get("/v1/audio/voices").json()["voices"]
        self.assertIn("azelma", voices)
        self.assertFalse({"cosette", "jean"} & set(voices))  # non-commercial licences

    def test_azelma_is_the_only_voice(self):
        """The others' licences are unverified; cosette and jean are non-commercial."""
        self.assertEqual(self.client.get("/v1/audio/voices").json(), {"voices": ["azelma"]})
        self.assertEqual(self.speak(input="Hi.", voice="alba").status_code, 400)

    @unittest.skipUnless(os.environ.get("HF_HUB_OFFLINE"), "the baked cache: in the image only")
    def test_only_azelma_is_baked_in(self):
        baked = {p.name for p in pathlib.Path(os.environ["HF_HOME"]).rglob("*.safetensors")}
        self.assertIn("azelma.safetensors", baked)
        self.assertEqual({n for n in baked if n.split(".")[0] in POCKET_VOICES}, {"azelma.safetensors"})

    def test_a_format_other_than_pcm_is_a_400(self):
        self.assertEqual(self.speak(input="Hi.", response_format="mp3").status_code, 400)

    def test_empty_input_is_a_400(self):
        self.assertEqual(self.speak(input="   ").status_code, 400)



class CaptionedSpeechContract(unittest.TestCase):
    """POST /dev/captioned_speech, Kokoro-FastAPI's shape: audio plus [{word, start_time, end_time}],
    so the voice knows exactly which words were heard when it is interrupted."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app.app).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def caption(self, text, **body):
        r = self.client.post("/dev/captioned_speech", json={"voice": "azelma", "response_format": "pcm",
                                                            "input": text, **body})
        self.assertEqual(r.status_code, 200, r.text)
        return [json.loads(line) for line in r.text.splitlines() if line.strip()]

    def test_every_input_word_comes_back_in_order_with_a_span_inside_the_audio(self):
        text = "Good morning! I was watching the light on your desk."
        chunks = self.caption(text, stream=False)
        self.assertEqual(len(chunks), 1)
        c = chunks[0]
        self.assertEqual(c["audio_format"], "audio/pcm")
        seconds = len(base64.b64decode(c["audio"])) / WIDTH / RATE
        self.assertEqual([t["word"] for t in c["timestamps"]], text.split())
        ends = 0.0
        for t in c["timestamps"]:
            self.assertLessEqual(ends, t["start_time"] + 1e-6)
            self.assertLess(t["start_time"], t["end_time"])
            ends = t["end_time"]
        self.assertLessEqual(ends, seconds + 0.01)

    def test_streamed_frame_by_frame_each_sentences_words_on_its_first_frame(self):
        chunks = self.caption("Hello there. How are you today?", stream=True)
        worded = [i for i, c in enumerate(chunks) if c["timestamps"]]
        self.assertEqual([[t["word"] for t in chunks[i]["timestamps"]] for i in worded],
                         [["Hello", "there."], ["How", "are", "you", "today?"]])
        self.assertEqual(worded[0], 0)
        self.assertGreater(worded[1], 1)  # the first sentence came as several frames
        first = sum(len(base64.b64decode(c["audio"])) for c in chunks[:worded[1]]) / WIDTH / RATE
        start = chunks[worded[1]]["timestamps"][0]["start_time"]
        self.assertAlmostEqual(start, first, delta=0.002)

    def test_a_word_spoken_as_several_is_still_one_entry(self):
        words = [t["word"] for c in self.caption("Set a timer for 25 minutes.") for t in c["timestamps"]]
        self.assertEqual(words, ["Set", "a", "timer", "for", "25", "minutes."])

    def test_words_share_the_sentence_by_their_length(self):
        self.assertEqual(app.words_spoken("one three", 0.8, 1.0),
                         [{"word": "one", "start_time": 1.0, "end_time": 1.3},
                          {"word": "three", "start_time": 1.3, "end_time": 1.8}])

    def test_the_same_validation_as_plain_speech(self):
        r = self.client.post("/dev/captioned_speech", json={"input": "Hi.", "voice": "alloy"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
