"""The STT server's contract, as Pipecat's OpenAISTTService uses it. Loads the real model, once.

    cd services && uv run python -m unittest discover -s stt -t stt
"""
import io
import pathlib
import re
import tempfile
import unittest
from unittest import mock

import app
import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient

# SYNTHETIC: Kokoro bf_emma saying these words, 16 kHz mono (made by sage-kernel bench/).
FIXTURE = pathlib.Path(__file__).parent / "fixtures/good-morning-little-robot.wav"


client = None


def setUpModule():
    """Start the app with no voiceprint: its lifespan loads the model once."""
    # No voiceprint, whatever SAGE_VOICEPRINT or a mounted /profiles holds: the same run natively,
    # in `make test-docker` and in a deploy. OwnerVoice enrolls its own.
    global client
    patch = mock.patch.object(app, "VOICEPRINT", pathlib.Path("/nonexistent/owner.npy"))
    patch.start()
    unittest.addModuleCleanup(patch.stop)
    client = TestClient(app.app).__enter__()  # the lifespan: one model load for every test here
    unittest.addModuleCleanup(client.__exit__, None, None, None)


def wav(audio, rate):
    buf = io.BytesIO()
    sf.write(buf, audio, rate, format="WAV")
    return buf.getvalue()


def noise(seconds=2.0):
    return np.random.default_rng(0).normal(0, 0.05, int(16000 * seconds)).astype("float32")


class NoVoiceprint(unittest.TestCase):
    def test_the_speaker_model_stays_out_of_memory_until_a_voiceprint_needs_it(self):
        # A start of its own; `loaded` puts the shared model back after it.
        loaded = mock.patch.multiple(app, _model=app._model, _speaker=app._speaker,
                                     _voiceprint=app._voiceprint)
        with loaded, mock.patch.object(app, "load"), \
                mock.patch.object(app, "load_speaker") as speaker, TestClient(app.app):
            speaker.assert_not_called()


class Confidence(unittest.TestCase):
    """Per-word confidence from the decoder's token log-probabilities, with no model loaded."""

    def test_a_word_is_as_sure_as_its_least_sure_token(self):
        words = app.confidence([" good", " mor", "ning"], [-0.01, -0.5, -0.05])
        self.assertEqual([w for w, _ in words["words"]], ["good", "morning"])
        self.assertAlmostEqual(words["words"][1][1], round(float(np.exp(-0.5)), 3))
        self.assertEqual(words["min"], words["words"][1][1])
        self.assertAlmostEqual(words["mean"], round((np.exp(-0.01) + np.exp(-0.5)) / 2, 3), places=3)

    def test_no_tokens_is_no_confidence(self):
        self.assertIsNone(app.confidence([], []))

    def test_the_answer_carries_it_beside_the_text_and_old_clients_read_text_as_before(self):
        result = mock.Mock(text=" Hi there", tokens=[" Hi", " there"], logprobs=[-0.1, -0.2])
        fake = mock.Mock()
        fake.with_timestamps.return_value.recognize.return_value = result
        with mock.patch.object(app, "_model", fake):
            r = client.post("/v1/audio/transcriptions",
                            files={"file": ("a.wav", wav(noise(1.0), 16000), "audio/wav")}).json()
        self.assertEqual(r["text"], " Hi there")
        self.assertEqual(r["confidence"]["words"], [["Hi", round(float(np.exp(-0.1)), 3)],
                                                    ["there", round(float(np.exp(-0.2)), 3)]])
        self.assertIsNone(r["similarity"])  # no voiceprint enrolled: no score


class TranscriptionContract(unittest.TestCase):
    def post(self, data: bytes, **fields):
        return client.post("/v1/audio/transcriptions", files={"file": ("a.wav", data, "audio/wav")},
                                data={"model": "parakeet-tdt-0.6b-v2", **fields})

    def test_health_is_ready_once_the_model_is_loaded(self):
        self.assertEqual(client.get("/health").json(), {"ready": True, "enrolled": False})

    def test_speech_comes_back_as_its_words_whichever_openai_fields_pipecat_sends(self):
        every = {"language": "en", "prompt": "", "temperature": "0", "response_format": "json"}
        for fields in ({}, every):
            with self.subTest(fields):
                r = self.post(FIXTURE.read_bytes(), **fields)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(re.findall(r"[a-z]+", r.json()["text"].lower()),
                                 ["good", "morning", "little", "robot"])

    def test_silence_is_empty_text_not_an_error(self):
        r = self.post(wav(np.zeros(16000, dtype="float32"), 16000))
        self.assertEqual((r.status_code, r.json()["text"].strip()), (200, ""))

    def test_other_sample_rates_are_resampled(self):
        a, rate = sf.read(FIXTURE, dtype="float32")
        a24 = np.interp(np.arange(0, len(a), rate / 24000), np.arange(len(a)), a).astype("float32")
        self.assertIn("morning", self.post(wav(a24, 24000)).json()["text"].lower())

    def test_what_it_cannot_read_or_answer_is_a_400(self):
        speech = FIXTURE.read_bytes()
        for why, data, fields in (("not audio", b"not a wav", {}),
                                  ("a format it cannot answer", speech, {"response_format": "srt"})):
            with self.subTest(why):
                self.assertEqual(self.post(data, **fields).status_code, 400)


class OwnerVoice(unittest.TestCase):
    """With a voiceprint enrolled, only the owner's voice is transcribed (reachy_sage_voice/owner.py)."""

    @classmethod
    def setUpClass(cls):
        cls.recordings = tempfile.TemporaryDirectory()
        sf.write(pathlib.Path(cls.recordings.name) / "turn-1.wav", sf.read(FIXTURE, dtype="float32")[0], 16000)
        sf.write(pathlib.Path(cls.recordings.name) / "too-short.wav", np.zeros(4000, dtype="float32"), 16000)
        cls.voiceprint = pathlib.Path(cls.recordings.name) / "owner.npy"
        cls.report = app.enroll(pathlib.Path(cls.recordings.name), cls.voiceprint)
        app._voiceprint = np.load(cls.voiceprint)

    @classmethod
    def tearDownClass(cls):
        app._voiceprint = None
        cls.recordings.cleanup()

    def verify(self, data: bytes):
        return client.post("/v1/speaker/verify", files={"file": ("a.wav", data, "audio/wav")}).json()

    def test_enrolling_averages_the_recordings_long_enough_to_judge(self):
        self.assertEqual([name for name, _ in self.report], ["turn-1.wav"])
        self.assertEqual(np.load(self.voiceprint).shape, (256,))

    def test_health_says_a_voiceprint_is_enrolled(self):
        self.assertEqual(client.get("/health").json(), {"ready": True, "enrolled": True})

    def test_the_owner_is_verified_and_transcribed(self):
        answer = self.verify(FIXTURE.read_bytes())
        self.assertTrue(answer["owner"] and answer["enrolled"])
        self.assertGreater(answer["similarity"], app.THRESHOLD)
        r = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", FIXTURE.read_bytes(), "audio/wav")})
        self.assertIn("morning", r.json()["text"].lower())

    def test_any_other_sound_is_not_the_owner_and_comes_back_as_no_text(self):
        self.assertFalse(self.verify(wav(noise(), 16000))["owner"])
        r = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", wav(noise(), 16000), "audio/wav")})
        self.assertEqual((r.status_code, r.json()["text"]), (200, ""))


if __name__ == "__main__":
    unittest.main()
