"""The pet's character: its name, every word the kernel says for it or to the model, and the word lists and
patterns the kernel reads its language with, from one JSON file.

sage/character.json is the default, the pet Sage, in English. SAGE_CHARACTER names another file: another pet, or
another language, with no change to the kernel. That file holds every key the default holds, each of the same
kind; a missing or wrong one stops the start with the file and the key named.
"""

import json
import os
import re
import string
from pathlib import Path

DEFAULT = Path(__file__).with_name("character.json")
KINDS = {str: "text", list: "a list of texts", dict: "an object"}


def _read(path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"{path}: {error}") from None


def _check(default, got, where: str = "") -> None:
    """ValueError unless `got` has the shape of `default`, the default file's value under the key `where`."""
    if type(got) is not type(default):
        raise ValueError(f"{where or 'the file'} must be {KINDS[type(default)]}")
    if isinstance(default, dict):
        for key, value in default.items():
            inner = f"{where}.{key}" if where else key
            if key not in got:
                raise ValueError(f"{inner} is missing")
            _check(value, got[key], inner)
    elif isinstance(default, list) and not all(isinstance(item, str) for item in got):
        raise ValueError(f"{where} must be {KINDS[list]}")


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}


def _joined(alternatives, where: str):
    """A pattern from its alternatives, any of which may match. None, or an empty one, would match every line."""
    if isinstance(alternatives, dict):
        return {key: _joined(value, f"{where}.{key}") for key, value in alternatives.items()}
    try:
        if not alternatives or not all(alternatives):
            raise ValueError("it needs at least one alternative, none of them empty")
        return re.compile("|".join(alternatives)).pattern
    except (ValueError, re.error) as error:
        raise ValueError(f"{where} is not a usable pattern: {error}") from None


def load(path: str = "") -> dict:
    """The character in the file at `path`, or the default one, each pattern joined from its alternatives;
    ValueError, naming file and key, if the file will not do."""
    default = _read(DEFAULT)
    got = _read(path) if path else default
    try:
        _check(default, got)
        for key, template in default["templates"].items():  # the kernel fills these fields, and no others
            try:
                unknown = _fields(got["templates"][key]) - _fields(template)
            except ValueError:  # an unmatched brace
                unknown = {"{"}
            if unknown:
                raise ValueError(f"templates.{key} may hold only the fields of the default: "
                                 + (", ".join(sorted(_fields(template))) or "none"))
        for key, names in default["calendar"].items():  # read by month, weekday and part of the day
            if len(got["calendar"][key]) != len(names):
                raise ValueError(f"calendar.{key} must hold {len(names)} texts")
        return {**got, "patterns": _joined(got["patterns"], "patterns")}
    except ValueError as error:
        raise ValueError(f"{path or DEFAULT}: {error}") from None


# ponytail: read once, at import, into module-level names (policy.PERSONA and the rest): one character per
# process, and a changed file needs a restart. Pass it through Kernel when two pets share a process.
CHARACTER = load(os.environ.get("SAGE_CHARACTER", ""))
