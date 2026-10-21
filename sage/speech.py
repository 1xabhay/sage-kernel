"""Reading the model's reply-first JSON answer, and keeping what is said to plain speech.

Pure. The model answers `{"reply": "...", "<tool>": {...}}` under an Ollama `format` schema, so
the reply can be decoded while it streams and the actions are taken only from the parsed object.
"""

import json
import re
import unicodedata

START = re.compile(r'\s*\{\s*"reply"\s*:\s*"')
ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
STAGE = re.compile(r"\*[^*]*\*")
# The kernel's own notes to the model, should it ever copy one: never spoken.
ANNOTATION = re.compile(r"[(\[][^()\[\]]*\b(interrupted|body could not|json fields|you just said|this body cannot|stay sage)\b"
                        r"[^()\[\]]*[)\]]", re.IGNORECASE)
EMOJI_PARTS = {0x200D, 0xFE0F, 0x20E3}
# Talk of being interrupted, the kernel's barge-in note paraphrased ("I was interrupted", live run 5b).
INTERRUPTION = re.compile(r"\b(?:i|i'm|i've|we)\b[^.!?]{0,20}\b(?:interrupt|cut off)|\binterrupt\w*\s+(?:me|us)\b"
                          r"|\bcut(?:ting)? (?:me|us) off\b")
# Generic-assistant sentences ("How can I help you today?", "feel free to ask"; live phi4-mini, 2026-10-21).
ASSISTANT = re.compile(r"\bassist|\bhow (?:can|may) i help\b|\bfeel free to\b|\bcertainly\b|\blet me know if\b"
                       r"|\bis there anything (?:else )?(?:i|you)\b|\bhere to help\b")
UNSPOKEN = (INTERRUPTION, ASSISTANT)
# Openings held while streaming, until they could no longer become a sentence that is never spoken.
# ponytail: a fixed list, so a sentence opening otherwise ("Oh, you interrupted") can stream its start.
OPENINGS = ("i was interrupted", "i got interrupted", "i ve been interrupted", "i was cut off", "i got cut off",
            "sorry i was interrupted", "you interrupted me", "you cut me off", "you keep interrupting me",
            "you keep cutting me off", "how can i help", "how can i assist", "how may i help", "how may i assist",
            "feel free to", "let me know if", "is there anything", "certainly", "i m here to help",
            "i m here to assist", "if you need assistance", "if you need anything")


def partial_reply(raw: str) -> tuple[str, bool]:
    """The reply decoded so far from a streaming answer, and whether its string has closed."""
    start = START.match(raw)
    if not start:
        return "", False
    out, index = [], start.end()
    while index < len(raw):
        char = raw[index]
        if char == '"':
            return "".join(out), True
        if char != "\\":
            out.append(char)
            index += 1
            continue
        if index + 1 >= len(raw):
            break  # an escape cut off mid-stream
        code = raw[index + 1]
        if code == "u":
            digits = raw[index + 2:index + 6]
            if len(digits) < 4:
                break
            try:
                out.append(chr(int(digits, 16)))
            except ValueError:
                break
            index += 6
        else:
            out.append(ESCAPES.get(code, code))
            index += 2
    return "".join(out), False


def parse_output(raw: str) -> tuple[str, tuple[tuple[str, object], ...], str]:
    """(reply, ((tool, input), ...), problem). A broken answer keeps its decoded reply and takes no action."""
    try:
        answer = json.loads(raw)
    except ValueError:
        return partial_reply(raw)[0], (), "the answer was not complete JSON"
    if not isinstance(answer, dict) or not isinstance(answer.get("reply"), str):
        return "", (), "the answer had no reply string"
    calls = tuple((name, value) for name, value in answer.items() if name != "reply" and value is not None)
    return answer["reply"], calls, ""


def whole_sentences(reply: str, limit: int) -> str:
    """A reply the decoder cut at `limit` characters, back to its last whole sentence (or word): never mid-word."""
    if len(reply) < limit or reply.rstrip()[-1:] in tuple(".!?\"'"):
        return reply
    reply = reply[:limit]
    end = max(reply.rfind(mark) for mark in ".!?")
    return reply[:end + 1] if end >= 0 else reply[:reply.rfind(" ")].rstrip()


def near_cap(reply: str, limit: int) -> str:
    """While streaming: a sentence after the first is held until it ends. If the decoder's cap at `limit`
    lands inside it, `whole_sentences` cuts the reply back to the sentence before, so what streams must
    never run ahead of that. The first sentence is safe: with no sentence end, the cut falls at a space."""
    partial = re.split(r"(?<=[.!?])\s+", reply[:limit])[-1]
    start = len(reply[:limit]) - len(partial)
    return reply[:start].rstrip() if partial and start > 0 else reply


def _emoji(char: str) -> bool:
    point = ord(char)
    # "Lo" also drops stray non-Latin letters ("They met\u86df") from these English replies.
    return (unicodedata.category(char) in ("So", "Lo") or point in EMOJI_PARTS or 0x1F3FB <= point <= 0x1F3FF)


def _unspoken(text: str, closed: bool) -> str:
    """`text` without sentences that are never spoken (talk of being interrupted, assistant-speak); while
    streaming, the sentence being written is also held while it could still become one."""
    pieces = re.split(r"(?<=[.!?])(\s+)", text)  # sentence, gap, sentence, gap, ..., the last sentence
    out = []
    for index in range(0, len(pieces), 2):
        sentence, gap = pieces[index], pieces[index + 1] if index + 1 < len(pieces) else ""
        plain = sentence.lower().replace("\u2019", "'")
        opening = " ".join(re.sub(r"[^\w\s]", " ", plain).split())
        if any(p.search(plain) for p in UNSPOKEN) or (not closed and index == len(pieces) - 1 and opening
                                                      and any(o.startswith(opening) for o in OPENINGS)):
            continue
        out.append(sentence + gap)
    return "".join(out)


def speakable(text: str, labels, closed: bool, tools=()) -> str:
    """What may be said of `text`: no labels, emoji, *stage directions* or tool syntax; digits stay.

    Tool syntax is a bracketed span naming a declared tool ("(emote: )", "[wave]") or a tool's name
    used as a label ("emote:"); the tool's name as an ordinary word stays. While streaming (`closed`
    false) an unfinished word, stage direction or bracket is held back, so what streams is always a
    prefix of what is finally said.
    """
    if not closed:
        text = text[:max(text.rfind(" "), text.rfind("\n")) + 1]
        if text.count("*") % 2:
            text = text[:text.rfind("*")]
        opened = max(text.rfind("("), text.rfind("["))
        if opened >= 0 and not re.search(r"[)\]]", text[opened:]):
            text = text[:opened]
    text = ANNOTATION.sub(" ", STAGE.sub(" ", "".join(char for char in text if not _emoji(char))))
    text = _unspoken(text, closed)
    if tools:
        names = "|".join(map(re.escape, tools))
        text = re.sub(rf"[(\[][^()\[\]]*\b({names})\b[^()\[\]]*[)\]]|\b({names})\s*:", " ", text,
                      flags=re.IGNORECASE)
    if labels:
        text = re.sub(r"(?<!\w)(" + "|".join(map(re.escape, labels)) + r")(?!\w)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).lstrip()
    return text.rstrip() if closed else text
