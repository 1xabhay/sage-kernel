"""Web search: the voice's research service (POST /search {"q"} -> {"passages"}), over its local SearXNG.

Standard library only. Only the query leaves this machine, through that service; nothing comes back but snippets.
"""

import json
import urllib.request
from urllib.parse import urlparse

from sage.adapters.model import LOCAL, NoRedirect

TIMEOUT_S = 3.0  # a voice turn waits on it: past this, the turn is answered without the web
MAX_PASSAGES, MAX_CHARS = 5, 600  # notes' own caps are 64 x 600 (PROTOCOL §10); the prompt shows 4


def provider(url: str, timeout: float = TIMEOUT_S):
    """A `search(query) -> passages` for the research service at base URL `url`, on this machine."""
    parsed = urlparse(url)
    if (parsed.scheme != "http" or parsed.hostname not in LOCAL or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path.strip("/")):
        raise ValueError("Set SAGE_SEARCH_URL to the research service's base URL on this machine "
                         "(plain http, no path), or leave it empty to never search")
    endpoint, opener = url.rstrip("/") + "/search", urllib.request.build_opener(NoRedirect())

    def search(query: str) -> list[str]:
        request = urllib.request.Request(endpoint, json.dumps({"q": query}).encode(),
                                         {"Content-Type": "application/json"})
        with opener.open(request, timeout=timeout) as response:
            passages = json.loads(response.read(MAX_PASSAGES * MAX_CHARS * 4 + 4096)).get("passages", [])
        return [str(p)[:MAX_CHARS] for p in passages if isinstance(p, str)][:MAX_PASSAGES]
    return search
