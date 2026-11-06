"""Open-source web search (DuckDuckGo via `ddgs`) used to ground discovery.

The LLM proposes songs from memory; web results supply current, human-curated
songlists so the candidate pool isn't limited to what the model recalls.
"""

from __future__ import annotations

from ..cache import cached_json


def search_web(query: str, max_results: int = 8) -> list[dict]:
    """Return [{title, body, href}] for a query. Never raises."""

    def _run() -> list[dict]:
        try:
            from ddgs import DDGS
        except ImportError:  # pragma: no cover - dependency is declared
            return []
        try:
            with DDGS() as ddgs:
                return [
                    {
                        "title": r.get("title", ""),
                        "body": r.get("body", ""),
                        "href": r.get("href", ""),
                    }
                    for r in ddgs.text(query, max_results=max_results)
                ]
        except Exception:
            # Search is an enhancement, not a hard dependency of the pipeline.
            return []

    return cached_json("websearch", f"{query}|{max_results}", _run) or []


def gather_context(queries: list[str], per_query: int = 6, max_chars: int = 8000) -> str:
    """Flatten several searches into one grounding block for the discovery prompt.

    Hard-capped: an uncapped sweep can run to tens of thousands of characters and
    silently overflow a local model's context, truncating the instructions that
    follow it. Results are interleaved across queries so the cap doesn't starve
    the later ones.
    """
    seen: set[str] = set()
    per_source: list[list[str]] = []
    for q in queries:
        hits = search_web(q, max_results=per_query)
        if not hits:
            continue
        block: list[str] = [f"### results for: {q}"]
        for h in hits:
            key = h["href"] or h["title"]
            if key in seen:
                continue
            seen.add(key)
            body = " ".join((h["body"] or "").split())[:300]
            block.append(f"- {h['title']}: {body}")
        if len(block) > 1:
            per_source.append(block)

    lines: list[str] = []
    total = 0
    # Round-robin so every query contributes before any one exhausts the budget.
    for depth in range(max(len(b) for b in per_source) if per_source else 0):
        for block in per_source:
            if depth >= len(block):
                continue
            line = block[depth]
            if total + len(line) > max_chars:
                return "\n".join(lines)
            lines.append(line)
            total += len(line) + 1
    return "\n".join(lines)
