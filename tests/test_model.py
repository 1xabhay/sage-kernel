"""The Ollama decision provider sends exactly the compiled prompt and streams back the raw answer."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sage import policy
from sage.adapters.model import provider
from sage.policy import Companion, Turn

EMOTE = {"name": "emote", "description": "Show a feeling.",
         "inputSchema": {"type": "object", "properties": {"name": {"enum": ["cheerful1"]}}}}
MANIFEST = {"senses": ("speech",), "tools": (EMOTE,)}


class FakeOllama:
    def __init__(self, pieces, status=200):
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                fake.requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(status)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                for index, piece in enumerate(pieces):
                    done = index == len(pieces) - 1
                    self.wfile.write(json.dumps({"message": {"role": "assistant", "content": piece},
                                                 "done": done}).encode() + b"\n")
                    self.wfile.flush()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ModelTests(unittest.TestCase):
    def test_ollama_receives_exactly_the_compiled_prompt_with_thinking_off_and_streams_back(self):
        fake = FakeOllama(['{"reply": "Yay, ', 'hello!", "emote": {"name": "cheerful1"}}', ""])
        self.addCleanup(fake.close)
        prompt = policy.compile_prompt(Companion(turn=Turn("r1", "reachy", "active", "Hi")), "reachy", MANIFEST)
        heard = []
        raw = provider(fake.url, "qwen3:1.7b")(prompt, heard.append)
        self.assertEqual(raw, '{"reply": "Yay, hello!", "emote": {"name": "cheerful1"}}')
        self.assertEqual(heard, ['{"reply": "Yay, ', 'hello!", "emote": {"name": "cheerful1"}}'])
        path, request = fake.requests[0]
        self.assertEqual(path, "/api/chat")
        self.assertEqual((request["model"], request["messages"], request["format"], request["stream"],
                          request["think"]), ("qwen3:1.7b", prompt.messages(), prompt.format, True, False))
        provider(fake.url, "qwen3:1.7b")(policy.retry_prompt(prompt), heard.append)
        self.assertEqual(fake.requests[1][1]["options"]["temperature"], policy.RETRY_TEMPERATURE)

    def test_sampling_follows_the_model_family_and_the_context_is_capped(self):
        """Qwen3's card: near-greedy decoding repeats endlessly; its non-thinking settings are below.
        Ollama's default context can take ~15 GB on this 16 GB Mac, so num_ctx is capped."""
        fake = FakeOllama(['{"reply": "Hi"}'] * 1)
        self.addCleanup(fake.close)
        prompt = policy.compile_prompt(Companion(turn=Turn("r1", "reachy", "active", "Hi")), "reachy", MANIFEST)
        for name in ("qwen3:1.7b", "qwen3:4b-instruct", "phi4-mini"):
            provider(fake.url, name)(prompt, lambda piece: None)
        qwen, instruct, phi = (request["options"] for _, request in fake.requests)
        self.assertEqual({k: qwen[k] for k in ("temperature", "top_p", "top_k", "min_p", "presence_penalty")},
                         {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5})
        self.assertEqual(instruct, qwen)
        self.assertEqual((phi["temperature"], "presence_penalty" in phi), (0.4, False))
        self.assertEqual({qwen["num_ctx"], phi["num_ctx"]}, {4096})

    def test_an_ollama_error_fails_the_decision(self):
        fake = FakeOllama([], status=404)
        self.addCleanup(fake.close)
        prompt = policy.compile_prompt(Companion(turn=Turn("r1", "reachy", "active", "Hi")), "reachy", MANIFEST)
        with self.assertRaises(OSError):
            provider(fake.url, "missing")(prompt, lambda piece: None)

    def test_only_local_models_may_use_plain_http(self):
        for url in ("http://example.com", "ftp://127.0.0.1", "http://user:pw@127.0.0.1", "", "http://127.0.0.1/v1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                provider(url, "fake")
        with self.assertRaises(ValueError):
            provider("http://127.0.0.1:11434", "")


if __name__ == "__main__":
    unittest.main()
