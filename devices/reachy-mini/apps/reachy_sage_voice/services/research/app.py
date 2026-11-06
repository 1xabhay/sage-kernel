"""Song research: name a song from a mic snippet, then gather what is findable about it.

POST /identify takes a WAV from the robot's mic (about 10 s) and asks Shazam through shazamio:
only the audio's fingerprint leaves the machine. POST /research takes a title and an artist and
returns short passages, each tagged with its source, for the voice to answer the owner from:
MusicBrainz (credits, releases, the artist), Wikipedia (the song's and the artist's articles),
LRCLIB (lyrics) and a private SearXNG (news, and the web). No model runs here.
"""
import asyncio
import html.parser
import logging
import os
import re
import time
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel
from shazamio import Shazam

SEARXNG = os.environ.get("SEARXNG_URL", "http://searxng:8080")
# MusicBrainz asks every client to name itself and a way to reach its authors.
UA = "ReachySageVoice/0.1.0 ( https://github.com/1xabhay/sage-kernel )"
LIMIT, CAP = 600, 64  # characters per passage, passages per answer
BUDGET = 20.0  # seconds for every source, which all run at once
MB_GAP = 1.0  # seconds between MusicBrainz requests: their limit is one a second
TRANSPORT = None  # the tests swap in a fake web
_mb_lock, _mb_last = asyncio.Lock(), 0.0
log = logging.getLogger("research")
log.setLevel(logging.INFO)
logging.basicConfig(format="%(name)s: %(message)s")
app = FastAPI()

# Coarse to fine: a piece too long at one level is split at the next.
SPLITS = [(r"\n\s*\n", "\n"), (r"\n", "\n"), (r"(?<=[.!?])\s+", " "), (r"\s+", " ")]


def chunk(text, n, level=0):
    """Pieces of at most n characters, split at paragraphs, then lines, then sentences, then words."""
    text = text.strip()
    if len(text) <= n:
        return [text] if text else []
    if level == len(SPLITS):  # one word longer than n
        return [text[i:i + n] for i in range(0, len(text), n)]
    sep, joiner = SPLITS[level]
    out = []
    for piece in re.split(sep, text):
        for part in chunk(piece, n, level + 1):
            if out and len(out[-1]) + len(joiner) + len(part) <= n:
                out[-1] += joiner + part
            else:
                out.append(part)
    return out


# Words a spoken question uses, in each source's tag: the kernel matches questions to passages by words
# (BM25), and "What's he up to lately?" shares none with a headline. ponytail: hand-picked; embeddings
# in the kernel if questions keep missing.
CREDITS = "Credits (MusicBrainz; who produced, wrote, played and featured)"
RELEASES = "Releases (MusicBrainz; when it came out, singles and albums)"
LYRICS = "Lyrics (LRCLIB; the words, chorus and verses)"
NEWS = "News, latest (what the artist is doing lately, recently, now, new tours and releases)"


def tagged(rank, tag, text):
    """(rank, "tag: piece") for each piece of text. A lower rank is more useful and survives the cap."""
    return [(rank, f"{tag}: {c}") for c in chunk(text or "", LIMIT - len(tag) - 2)]


async def get(client, url, **params):
    r = await client.get(url, params=params)
    r.raise_for_status()
    return r


async def musicbrainz(client, path, **params):
    """One MusicBrainz request, at most one a second across every research in flight."""
    global _mb_last
    async with _mb_lock:
        await asyncio.sleep(max(0.0, _mb_last + MB_GAP - time.monotonic()))
        try:
            return (await get(client, f"https://musicbrainz.org/ws/2/{path}", fmt="json", **params)).json()
        finally:
            _mb_last = time.monotonic()


async def optional(what, call):
    """A cheap extra: on failure, None and a log line instead of losing the source."""
    try:
        return await call
    except Exception as e:
        log.warning("%s skipped: %r", what, e)


AUTHORS = ("writer", "composer", "lyricist", "producer")  # credits that lead


async def from_musicbrainz(client, title, artist):
    """The card, the credits, the releases and the artist."""
    quote = lambda s: re.sub(r'["\\]', "", s)
    found = await musicbrainz(client, "recording", limit=25,
                              query=f'recording:"{quote(title)}" AND artist:"{quote(artist)}"')
    recs = found.get("recordings", [])
    recs = [r for r in recs if r["title"].casefold() == title.casefold()] or recs
    if not recs:
        return []
    # The studio original: no "live" or "mix" note, and on the most releases.
    rec = max(recs, key=lambda r: (not r.get("disambiguation"), len(r.get("releases", []))))
    credits = rec.get("artist-credit", [])
    by = "".join(c["name"] + c.get("joinphrase", "") for c in credits) or artist
    featured = [c["name"] for prev, c in zip(credits, credits[1:])
                if re.search(r"feat|ft\.|with", prev.get("joinphrase", ""), re.I)]
    releases = sorted((r for r in rec.get("releases", []) if r.get("status") == "Official"),
                      key=lambda r: r.get("date") or "9999")
    album = next((r for r in releases if r.get("release-group", {}).get("primary-type") == "Album"
                  and not r["release-group"].get("secondary-types")), None)

    detail = await musicbrainz(client, f"recording/{rec['id']}", inc="artist-rels+work-rels+work-level-rels+genres")
    genres = [g["name"] for g in sorted(detail.get("genres", []), key=lambda g: -g.get("count", 0))[:3]]
    roles = {}  # role -> name -> attributes, e.g. instrument -> Max Martin -> [guitar, bass]
    for rel in detail.get("relations", []):
        for r in [rel] + rel.get("work", {}).get("relations", []):
            if r.get("artist"):
                roles.setdefault(r["type"], {}).setdefault(r["artist"]["name"], []).extend(r.get("attributes", []))
    label = await optional("MusicBrainz labels", musicbrainz(client, f"release/{album['id']}", inc="labels")) if album else None
    labels = list(dict.fromkeys(li["label"]["name"] for li in (label or {}).get("label-info", []) if li.get("label")))
    who = await optional("MusicBrainz artist", musicbrainz(client, f"artist/{credits[0]['artist']['id']}", inc="genres")) \
        if credits else None

    length = rec.get("length")
    card = [f'"{rec["title"]}" by {by}.', featured and f"Featuring: {', '.join(featured)}.",
            album and f"Album: {album['title']}.", rec.get("first-release-date") and f"Released: {rec['first-release-date']}.",
            genres and f"Genre: {', '.join(genres)}.", length and f"Length: {length // 60000}:{length // 1000 % 60:02d}.",
            labels and f"Label: {', '.join(labels)}."]
    out = tagged(0, "Card", " ".join(c for c in card if c))[:1]
    out += tagged(1, CREDITS, " ".join(
        f"{role.capitalize()}: " + ", ".join(n + (f" ({', '.join(dict.fromkeys(a))})" if a else "")
                                             for n, a in names.items()) + "."
        for role, names in sorted(roles.items(), key=lambda kv: kv[0] not in AUTHORS)))
    groups = {}  # each release group once, at its earliest date
    for r in releases:
        if r.get("release-group", {}).get("secondary-types"):  # compilations, live, remixes
            continue
        kind = r.get("release-group", {}).get("primary-type") or "Release"
        groups.setdefault((kind, r["title"]), r.get("date"))
    out += tagged(1, RELEASES, f"On {len(rec.get('releases', []))} releases; first issued as: " + "; ".join(
        f'{kind} "{name}"' + (f" ({date})" if date else "") for (kind, name), date in list(groups.items())[:12]) + ".")
    if who:
        span, area = who.get("life-span", {}), (who.get("area") or {}).get("name")
        facts = [who.get("type"), area and f"from {area}", span.get("begin") and f"begun {span['begin']}",
                 (who.get("begin-area") or {}).get("name") and f"in {who['begin-area']['name']}",
                 span.get("end") and f"ended {span['end']}",
                 who.get("genres") and "genres: " + ", ".join(g["name"] for g in who["genres"][:5])]
        out += tagged(1, "MusicBrainz, the artist", f"{who['name']}: " + "; ".join(f for f in facts if f) + ".")
    return out


# Wikipedia sections: tables or link lists in plain text, and the ones that carry the song's story.
NOISE = re.compile(r"see also|notes|references|external links|further reading|footnotes|bibliography|"
                   r"charts|certifications|release history|track ?listings?|sources|citations", re.I)
STORY = re.compile(r"background|development|writing|recording|production|composition|lyrics|style|"
                   r"personnel|sampl|inspiration|release", re.I)


async def wikipedia(client, query, title, tag, lead_rank, story_rank, rest_rank):
    """The best match's plain text, by section, if its title holds `title` (else it's another page)."""
    pages = (await get(client, "https://en.wikipedia.org/w/api.php", action="query", format="json",
                       formatversion=2, generator="search", gsrsearch=query, gsrlimit=1, prop="extracts",
                       explaintext=1, redirects=1)).json().get("query", {}).get("pages", [])
    if not pages or title.casefold() not in pages[0]["title"].casefold():
        return []
    parts = re.split(r"\n(=+) (.*?) \1\n?", "\n" + pages[0].get("extract", ""))
    out, section = tagged(lead_rank, tag, parts[0]), ""
    for level, name, body in zip(parts[1::3], parts[2::3], parts[3::3]):
        section = name if len(level) == 2 else section  # subsections file under their section
        if not NOISE.search(section):
            out += tagged(story_rank if STORY.search(section) else rest_rank, f"{tag}, {section}", body)
    return out


async def lyrics(client, title, artist):
    q = {"track_name": title, "artist_name": artist}
    try:
        text = (await get(client, "https://lrclib.net/api/get", **q)).json().get("plainLyrics")
    except httpx.HTTPStatusError:  # 404: no exact match
        text = None
    if not text:
        hits = (await get(client, "https://lrclib.net/api/search", **q)).json()
        text = next((h["plainLyrics"] for h in hits if h.get("plainLyrics")), None)
    return tagged(2, LYRICS, re.sub(r"(?m)^\s*♪\s*\n", "", text or ""))  # ♪: instrumental bars


def site(url):
    return (urlparse(url).hostname or "").removeprefix("www.")


def snippet(result):
    return ". ".join(s.strip() for s in (result.get("title"), result.get("content")) if s and s.strip())


async def searx(client, q, **params):
    return (await get(client, f"{SEARXNG}/search", q=q, format="json", **params)).json().get("results", [])


async def news(client, artist):
    """This month's news about the artist: headline and snippet, one passage each."""
    out = []
    # Only some news engines take a time range, and they are the ones that get blocked: then, newest first.
    results = (await searx(client, artist, categories="news", time_range="month")
               or await searx(client, artist, categories="news"))
    for r in sorted(results, key=lambda r: r.get("publishedDate") or "", reverse=True)[:8]:
        tag = ", ".join(t for t in (NEWS, (r.get("publishedDate") or "")[:10], site(r["url"])) if t)
        out += tagged(2, tag, snippet(r))[:1]
    return out


class Text(html.parser.HTMLParser):
    """A page's paragraph-like text: blocks of 80 characters or more, outside scripts and navigation."""
    SKIP = {"script", "style", "nav", "header", "footer", "aside", "noscript", "svg", "form", "button", "select"}
    BLOCK = {"p", "div", "li", "br", "section", "article", "main", "td", "blockquote", "pre",
             "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__()
        self.skip, self.blocks = 0, [[]]

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.blocks.append([])

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.blocks.append([])

    def handle_data(self, data):
        if not self.skip:
            self.blocks[-1].append(data)

    def text(self):
        # ponytail: the 80-character floor drops menus and link lists; a readability pass if pages stay noisy.
        blocks = (" ".join("".join(b).split()) for b in self.blocks)
        return "\n\n".join(b for b in blocks if len(b) >= 80)


async def page_text(client, url):
    r = await get(client, url)
    if "html" not in r.headers.get("content-type", ""):
        return ""
    parser = Text()
    parser.feed(r.text[:1_000_000])  # ponytail: the whole body downloads first; the 5 s timeout bounds it
    return parser.text()


ELSEWHERE = ("wikipedia.org", "youtube.com", "spotify.com", "apple.com", "lrclib.net")  # covered, or no text


async def web(client, title, artist):
    """What the web says about the song: the top results' snippets, and the text of the first three pages."""
    results = (await searx(client, f'"{title}" {artist} song meaning samples production'))[:8]
    out = [p for r in results for p in tagged(3, f"Web, {site(r['url'])}", snippet(r))[:1]]
    urls = [r["url"] for r in results if not any(site(r["url"]).endswith(h) for h in ELSEWHERE)][:3]
    for url, text in zip(urls, await asyncio.gather(*(page_text(client, u) for u in urls), return_exceptions=True)):
        if isinstance(text, Exception):
            log.warning("web page %s skipped: %r", url, text)
        else:
            out += tagged(3, f"Web, {site(url)}", text)[:3]
    return out


async def https_only(request):
    """Search results and their redirects can name any URL: only https leaves, plus our own SearXNG."""
    if request.url.scheme != "https" and not str(request.url).startswith(SEARXNG):
        raise httpx.UnsupportedProtocol(f"not https: {request.url}")


async def within_budget(name, source):
    try:
        return await asyncio.wait_for(source, BUDGET)
    except Exception as e:
        log.warning("%s skipped: %r", name, e)
        return []


class Song(BaseModel):
    title: str
    artist: str


@app.get("/health")
async def health():
    """Ready when SearXNG answers its /healthz. Always HTTP 200: a dead dependency is not a dead container."""
    try:
        async with httpx.AsyncClient(transport=TRANSPORT, timeout=1) as client:
            (await client.get(f"{SEARXNG}/healthz")).raise_for_status()
    except Exception as e:
        return {"ready": False, "searxng": repr(e)}
    return {"ready": True}


@app.post("/identify")
async def identify(file: UploadFile = File(...)):
    try:
        out = await Shazam().recognize(await file.read())
    except Exception as e:
        log.warning("Shazam failed: %r", e)
        raise HTTPException(502, "Shazam did not answer, or the audio is unreadable")
    track = out.get("track")
    if not track:
        return {"found": False}
    meta = {m.get("title"): m.get("text") for s in track.get("sections", []) for m in s.get("metadata", [])}
    return {"found": True, "title": track.get("title", ""), "artist": track.get("subtitle", ""),
            "album": meta.get("Album"), "year": meta.get("Released"), "genre": track.get("genres", {}).get("primary")}


@app.post("/research")
async def research(song: Song):
    start, title, artist = time.monotonic(), song.title.strip(), song.artist.strip()
    lead = re.split(r"\s+(?:feat\.?|ft\.|featuring)\s+", artist, flags=re.I)[0]  # the artist, for their page and news
    async with httpx.AsyncClient(transport=TRANSPORT, timeout=5, follow_redirects=True, headers={"User-Agent": UA},
                                 event_hooks={"request": [https_only]}) as client:
        sources = {
            "MusicBrainz": from_musicbrainz(client, title, artist),
            "Wikipedia, the song": wikipedia(client, f'"{title}" {lead} song', title, "Wikipedia, the song", 1, 2, 4),
            "LRCLIB": lyrics(client, title, artist),
            "Wikipedia, the artist": wikipedia(client, lead, lead, "Wikipedia, the artist", 2, 4, 5),
            "News": news(client, lead),
            "Web": web(client, title, lead),
        }
        answers = await asyncio.gather(*(within_budget(n, s) for n, s in sources.items()))
    # A stable sort: by usefulness, then in the order above.
    passages = sorted((p for a in answers for p in a), key=lambda p: p[0])
    if not passages or passages[0][0] != 0:  # MusicBrainz didn't answer: a card from what we were told
        passages[:0] = tagged(0, "Card", f'"{title}" by {artist}.')[:1]
    log.info('"%s" by %s: %d passages from %s in %.1f s', title, artist, min(len(passages), CAP),
             ", ".join(n for n, a in zip(sources, answers) if a) or "nothing", time.monotonic() - start)
    return {"topic": f'"{title}" by {artist}', "passages": [p for _, p in passages[:CAP]]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
