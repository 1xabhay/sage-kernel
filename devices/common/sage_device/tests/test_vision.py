"""Sight: a frame becomes a few words from a local vision model; only the words go on."""

import base64
import json
import urllib.error

from sage_device import vision


class Answer:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def test_a_frame_is_asked_about_in_one_short_plain_sentence_and_no_one_is_identified(monkeypatch):
    sent = []

    def urlopen(request, timeout):
        sent.append((request.full_url, json.loads(request.data), timeout))
        return Answer({"response": "  A person holding a red mug\n at a desk.  "})
    monkeypatch.setattr(vision.urllib.request, "urlopen", urlopen)
    said = vision.describe(b"\xff\xd8jpeg", "What am I holding?", url="http://m:11434",
                           model="eyes")
    assert said == "A person holding a red mug at a desk."
    url, body, timeout = sent[0]
    assert url == "http://m:11434/api/generate" and body["model"] == "eyes" and not body["stream"]
    assert body["images"] == [base64.b64encode(b"\xff\xd8jpeg").decode()]
    assert "What am I holding?" in body["prompt"] and "never" in body["prompt"].lower()
    assert "a person" in body["prompt"].lower()  # people only as "a person", never who
    assert timeout == vision.TIMEOUT_S


def test_a_look_shares_the_kernel_s_runner_and_never_unloads_it(monkeypatch):
    """One model for words and sight: the kernel's context and keep-alive, or it loads twice."""
    sent = []

    def urlopen(request, timeout):
        sent.append(json.loads(request.data))
        return Answer({"response": "A desk."})
    monkeypatch.setattr(vision.urllib.request, "urlopen", urlopen)
    vision.describe(b"\xff\xd8jpeg", None, url="http://m:11434", model="eyes")
    assert sent[0]["options"]["num_ctx"] == vision.NUM_CTX == 5120
    assert sent[0]["keep_alive"] == -1  # a request without it resets the model's expiry to 5 min


def test_without_a_question_it_says_what_the_scene_is():
    assert "What am I holding?" not in vision.prompt(None)
    assert "?" not in vision.prompt(None).split("\n")[-1]


def test_a_long_answer_is_cut_to_the_kernel_s_cap_at_a_word(monkeypatch):
    monkeypatch.setattr(vision.urllib.request, "urlopen",
                        lambda request, timeout: Answer({"response": "word " * 100}))
    said = vision.describe(b"x", None, url="http://m", model="eyes")
    assert len(said) <= vision.MAX_SEEN and said.endswith("word")


def test_no_frame_a_dead_model_or_a_blank_answer_is_nothing_seen(monkeypatch):
    assert vision.describe(None, "What is this?", url="http://m", model="eyes") is None

    def down(request, timeout):
        raise urllib.error.URLError("refused")
    monkeypatch.setattr(vision.urllib.request, "urlopen", down)
    assert vision.describe(b"x", None, url="http://m", model="eyes") is None
    for body in ({"response": " "}, {"error": "x"}):
        monkeypatch.setattr(vision.urllib.request, "urlopen",
                            lambda request, timeout, body=body: Answer(body))
        assert vision.describe(b"x", None, url="http://m", model="eyes") is None
    monkeypatch.setattr(vision.urllib.request, "urlopen", lambda request, timeout: Answer({}))
    assert vision.describe(b"x", None, url="http://m", model="eyes") is None


def test_a_scene_told_again_in_other_words_is_not_a_change():
    before = "A person at a desk with a laptop, evening light."
    assert not vision.changed(before, "A person at the desk with a laptop in evening light.")
    assert vision.changed(before, "An empty room, the lights off.")
    assert vision.changed(None, before)
