"""A scripted device-protocol v1 server on localhost, for the HTTP client's tests.

Each route answers from its own queue of (status, body) replies, or of
callables that build one from what was sent so far; the last one repeats.
Every request is recorded as (method, path, query, headers, body).
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from pipecat.tests import utils as pipecat_test_utils

TOKEN = "t" * 32

# Pipecat's run_test already waits on the pipeline's started event, but gives up
# after 1 s, which a busy machine misses. Same wait, a generous ceiling: a slow
# start passes, and only a pipeline that never starts fails.
pipecat_test_utils.run_test.__kwdefaults__["start_timeout"] = 10.0


@pytest.fixture(autouse=True)
def no_real_dotenv(monkeypatch):
    """No test reads a real .env: the only settings files are the ones a test makes."""
    import reachy_sage_voice.__main__ as voice

    monkeypatch.setattr(voice, "env_files", lambda: [])


@pytest.fixture(autouse=True)
def no_real_heartbeat(monkeypatch):
    """No test writes the real heartbeat that `make status` reads, or keeps another's table."""
    from reachy_sage_voice import health

    monkeypatch.setattr(health, "start", lambda *args, **kwargs: None)
    monkeypatch.setattr(health, "TABLE", {})


@pytest.fixture(autouse=True)
def single_writer():
    """M1 (LAYERS.md §4): with the arbiter, every motion call a fake sees is the runner's send."""
    from fake_mini import OFF_RUNNER

    OFF_RUNNER.clear()
    yield
    assert not OFF_RUNNER, f"M1: motion calls off the runner: {sorted(set(OFF_RUNNER))}"


@pytest.fixture(autouse=True)
def no_slew_limit(request, monkeypatch):
    """Outside test_arbiter (and tests marked ``slew``) the slew caps are off: quick gotos."""
    if request.module.__name__.endswith("test_arbiter") or request.node.get_closest_marker("slew"):
        return
    from reachy_sage_voice.motion import arbiter

    for caps in (arbiter.VMAX, arbiter.AMAX):
        for key in caps:
            monkeypatch.setitem(caps, key, 1e9)
    if "real_speed" in request.fixturenames:
        return
    # and a move lasts as long as the old fixed one did (make's segment_s), not seconds
    for key in arbiter.PEAK_SPEED:
        monkeypatch.setitem(arbiter.PEAK_SPEED, key, 1e9)
    monkeypatch.setattr(arbiter, "MIN_MOVE_S", 0.02)


@pytest.fixture(autouse=True)
def no_track_ramp(monkeypatch):
    """Bring the tracker's weight up at once, but in the tests of its ramp (they set the time)."""
    from reachy_sage_voice import executor

    monkeypatch.setattr(executor, "TRACK_RAMP_S", 0.0, raising=False)


@pytest.fixture
def real_speed():
    """Move at the robot's peak speeds and minimum duration (``no_slew_limit`` keeps them)."""


@pytest.fixture(params=["0", "1"], ids=["arbiter-off", "arbiter-on"])
def arbiter(request, monkeypatch):
    """Run a test with the motion arbiter off and on (SAGE_VOICE_ARBITER), both paths kept."""
    monkeypatch.setenv("SAGE_VOICE_ARBITER", request.param)


class FakeKernel:
    def __init__(self):
        self.replies: dict[tuple[str, str], list[tuple[int, object]]] = {}
        self.requests: list[dict] = []
        self._lock = threading.Lock()

    def script(self, method: str, path: str, *replies: tuple[int, object]) -> None:
        self.replies[(method, path)] = list(replies)

    def answer(self, method: str, path: str) -> tuple[int, object]:
        with self._lock:
            queue = self.replies.get((method, path)) or [(404, {"code": "not_found"})]
            reply = queue.pop(0) if len(queue) > 1 else queue[0]
        return reply(self) if callable(reply) else reply

    def events(self, kind: str | None = None) -> list[dict]:
        return [r["body"] for r in self.requests
                if r["path"] == "/v1/events" and (kind is None or r["body"]["kind"] == kind)]


@pytest.fixture
def kernel():
    fake = FakeKernel()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            fake.requests.append({
                "method": self.command, "path": url.path, "query": parse_qs(url.query),
                "headers": dict(self.headers), "body": json.loads(raw) if raw else None})
            status, body = fake.answer(self.command, url.path)
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _serve  # noqa: N815 - http.server names them

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    fake.url = f"http://127.0.0.1:{server.server_port}"
    yield fake
    server.shutdown()
    server.server_close()


@pytest.fixture
def http_server(kernel):
    """Serve the same scripted routes, for services other than the kernel."""
    return kernel
