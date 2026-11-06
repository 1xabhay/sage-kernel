"""Device protocol v1 over plain HTTP and JSON, standard library only. See docs/PROTOCOL.md.

The kernel never learns what a device is: a device is a token, the name the owner gave it, and
the manifest it last sent.
"""

import hmac
import json
import math
import os
import re
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from sage.kernel import Kernel
from sage.policy import Refused

STATUS = {"invalid": 400, "unauthorized": 401, "not_found": 404, "busy": 409, "unknown_pending": 409,
          "forbidden": 409, "stale": 409, "reset": 409, "hello_first": 409, "protocol_mismatch": 426,
          "rate_limited": 429}
ROUTES = {("POST", "/v1/hello"), ("POST", "/v1/events"), ("GET", "/v1/effects"), ("GET", "/v1/health")}
NAME = re.compile(r"[a-z0-9_-]{1,64}")
MAX_BODY = 131072  # notes carry a document (64 passages of 600 characters, JSON-escaped)


def parse_devices(spec: str) -> dict[str, str]:
    """Read `name=token,...` into {token: name}. Issuing a token is the owner's consent."""
    devices = {}
    for entry in filter(None, (part.strip() for part in spec.split(","))):
        name, _, token = entry.partition("=")
        if not NAME.fullmatch(name) or len(token) < 32 or name in devices.values():
            raise ValueError("SAGE_DEVICES entries must be name=token: a unique lowercase name and a token "
                             "of at least 32 characters")
        devices[token] = name
    return devices


class Devices:
    """SAGE_DEVICES, plus SAGE_DEVICES_FILE re-read whenever it changes: checked on every request, no restart.

    The file holds the same `name=token` entries, one per line or comma-separated, `#` starts a comment.
    A missing file holds no tokens; a file that does not parse keeps the tokens already in use.
    """

    def __init__(self, spec: str, path: str = ""):
        self.spec, self.path = spec, path
        self.seen = self.stamp()
        self.tokens = self.read()  # at start, a bad file stops the kernel instead

    def stamp(self):
        try:
            stat = os.stat(self.path) if self.path else None
        except OSError:
            return None
        return stat and (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def read(self) -> dict[str, str]:
        text = ""
        try:
            if self.path:
                with open(self.path) as file:
                    text = file.read()
        except FileNotFoundError:
            pass
        return parse_devices(",".join([self.spec, *(line.partition("#")[0] for line in text.splitlines())]))

    def current(self) -> dict[str, str]:
        # ponytail: no lock; two requests may both re-read a changed file, and the last one wins with the same tokens.
        stamp = self.stamp()
        if stamp != self.seen:
            self.seen = stamp  # once per change, so a bad file is reported once, not on every request
            try:
                self.tokens = self.read()
            except (OSError, ValueError) as error:
                print(f"sage: SAGE_DEVICES_FILE not reloaded, the old tokens stay: {error}", file=sys.stderr)
        return self.tokens


def serve(kernel: Kernel, devices: Devices, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def send(self, status: int, data: dict) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def refuse(self, refused: Refused) -> None:
            self.send(STATUS[refused.code], {"code": refused.code, "message": refused.message, **refused.extra})

        def device(self) -> str:
            header = self.headers.get("Authorization", "")
            given = header.removeprefix("Bearer ").encode() if header.startswith("Bearer ") else b""
            # Compare against every token so the time taken says nothing about which one is close.
            matches = [name for token, name in devices.current().items() if hmac.compare_digest(token.encode(), given)]
            if not given or not matches:
                raise Refused("unauthorized", "Send Authorization: Bearer <device token>")
            return matches[0]

        def body(self) -> dict:
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise Refused("invalid", "Send Content-Type: application/json")
            length = self.headers.get("Content-Length", "")
            if not length.isdigit() or not 0 < int(length) <= MAX_BODY:
                raise Refused("invalid", f"The body must be 1 to {MAX_BODY} bytes")
            try:
                data = json.loads(self.rfile.read(int(length)))
            except ValueError:
                raise Refused("invalid", "The body must be JSON") from None
            if not isinstance(data, dict):
                raise Refused("invalid", "The body must be a JSON object")
            return data

        def handle_route(self, method: str) -> None:
            url = urlparse(self.path)
            try:
                if (method, url.path) not in ROUTES:
                    raise Refused("not_found", "Device routes are POST /v1/hello, POST /v1/events, "
                                               "GET /v1/effects and GET /v1/health")
                source = self.device()  # health too: a 401 already tells a stranger the kernel is up
                if url.path == "/v1/health":
                    return self.send(200, kernel.health())
                if url.path in ("/v1/events", "/v1/effects") and source not in kernel.manifests:
                    raise Refused("hello_first", "This kernel has not had your hello since it started. "
                                                 "Send hello, then retry.")
                if url.path == "/v1/effects":
                    return self.send(200, kernel.effects(source, *self.cursor(parse_qs(url.query))))
                data = self.body()
                reply = kernel.hello(source, data) if url.path == "/v1/hello" else kernel.event(source, data)
                self.send(200, reply)
            except Refused as refused:
                self.refuse(refused)
            except (OSError, sqlite3.Error) as error:
                print(f"sage: {error}", file=sys.stderr)
                self.send(500, {"code": "storage", "message": "The kernel could not save this. "
                                                              "Retry the same request."})

        def cursor(self, query: dict) -> tuple[int, float, int]:
            try:
                since, offset = int(query.get("since", ["0"])[0]), int(query.get("offset", ["0"])[0])
                wait = float(query.get("wait", ["0"])[0])
                if since < 0 or offset < 0 or not math.isfinite(wait):
                    raise ValueError
            except ValueError:
                raise Refused("invalid", "since and offset must be whole numbers ≥ 0, wait a number of seconds")
            return since, wait, offset

        def do_GET(self):
            self.handle_route("GET")

        def do_POST(self):
            self.handle_route("POST")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
