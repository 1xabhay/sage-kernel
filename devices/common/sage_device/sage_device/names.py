"""Names the voice must hear and say right: SAGE_VOICE_NAMES (default none).

    SAGE_VOICE_NAMES="Mika:Mee-kah:Micah|Mica|Meeka"

Each comma-separated entry is ``written:spoken:heard|heard...``. Transcripts have
every whole-word ``heard`` replaced by ``written`` before the kernel sees them
(the STT heard the owner's name as "Micah", and the kernel remembered
it so). The voice is sent ``spoken`` in place of ``written`` (Kokoro, an English
voice, reads "Mika" near "Mica"), and its word captions come back as
``written``, so the history never holds the respelling.

A ``heard`` right after "forget" or "not" is the wrong name being disowned: it stays,
as the first one listed (what the kernel remembered), or the kernel could not drop it.
"""

import re
from dataclasses import dataclass

DISOWNS = re.compile(r"\b(?:forget|not)[\s,]+$", re.IGNORECASE)


@dataclass(frozen=True)
class Name:
    """A name as written, as the voice says it, and as the STT may hear it instead."""

    written: str
    spoken: str
    heard: tuple[str, ...] = ()


def parse(spec: str) -> tuple[Name, ...]:
    """Read SAGE_VOICE_NAMES; an empty ``spoken`` means said as written."""
    found = []
    for entry in filter(None, (part.strip() for part in spec.split(","))):
        written, _, rest = entry.partition(":")
        spoken, _, heard = rest.partition(":")
        found.append(Name(written.strip(), spoken.strip() or written.strip(),
                          tuple(filter(None, (h.strip() for h in heard.split("|"))))))
    return tuple(found)


def _replace(text: str, word: str, by) -> str:
    return re.sub(rf"(?<![\w-]){re.escape(word)}(?![\w-])", by, text, flags=re.IGNORECASE)


def correct(text: str, names: tuple[Name, ...]) -> str:
    """``text`` with each name the STT hears (a variant, its respelling) put back as written."""
    for name in names:
        for heard in name.heard:
            text = _replace(text, heard, lambda found, name=name: (
                name.heard[0] if DISOWNS.search(found.string[:found.start()]) else name.written))
        for heard in (name.spoken, name.written):
            text = _replace(text, heard, name.written)
    return text


def spoken(text: str, names: tuple[Name, ...]) -> str:
    """``text`` as the voice should be sent it: a misheard name, remembered so, said right too."""
    for name in names:
        for word in (*name.heard, name.written):
            text = _replace(text, word, name.spoken)
    return text


def written(word: str, names: tuple[Name, ...]) -> str:
    """Put a caption word back as written, its punctuation kept."""
    for name in names:
        if name.spoken != name.written:
            word = _replace(word, name.spoken, name.written)
    return word
