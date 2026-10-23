"""Schema flattening and response parsing - the fragile part of local models."""

import pytest
from pydantic import BaseModel, ValidationError

from music_telling.llm.providers.base import (
    json_schema,
    parse_into,
    validation_hint,
)
from music_telling.models import Story


class Inner(BaseModel):
    n: int


class Outer(BaseModel):
    label: str
    items: list[Inner]


def test_refs_are_inlined():
    """llama.cpp grammars handle $ref inconsistently, so nothing may survive."""
    schema = json_schema(Outer)
    assert "$defs" not in schema
    assert schema["properties"]["items"]["items"]["properties"]["n"]["type"] == "integer"
    assert "$ref" not in repr(schema)


def test_nested_model_schema_is_flat():
    schema = json_schema(Story)
    assert "$defs" not in schema and "$ref" not in repr(schema)
    beat = schema["properties"]["beats"]["items"]
    assert {"position", "title", "prose", "quote"} <= set(beat["properties"])


def test_inlined_ref_keeps_sibling_description():
    schema = json_schema(Story)
    # `beats` carries its own description alongside the inlined item schema.
    assert schema["properties"]["beats"]["type"] == "array"


@pytest.mark.parametrize(
    "raw",
    [
        '{"label":"a","items":[]}',
        '```json\n{"label":"a","items":[]}\n```',
        'Here you go:\n```\n{"label":"a","items":[]}\n```\nHope that helps!',
        '<think>let me plan</think>{"label":"a","items":[]}',
        'Sure! {"label":"a","items":[]} Let me know if you need changes.',
    ],
)
def test_extract_json_survives_model_chatter(raw):
    assert parse_into(Outer, raw).label == "a"


def test_extract_json_respects_braces_in_strings():
    raw = 'prose: {"label":"a }{ b","items":[]} trailing'
    assert parse_into(Outer, raw).label == "a }{ b"


def test_extract_json_handles_escaped_quotes():
    raw = '{"label":"she said \\"go\\"","items":[]}'
    assert parse_into(Outer, raw).label == 'she said "go"'


def test_validation_hint_names_the_bad_field():
    try:
        Outer.model_validate({"label": "a", "items": [{"n": "not-an-int"}]})
    except ValidationError as exc:
        hint = validation_hint(exc)
        assert "items.0.n" in hint
    else:
        pytest.fail("expected ValidationError")


def test_objects_are_sealed_against_extra_keys():
    schema = json_schema(Outer)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["items"]["items"]["additionalProperties"] is False


def test_sealing_reaches_deeply_nested_objects():
    beat = json_schema(Story)["properties"]["beats"]["items"]
    assert beat["additionalProperties"] is False


def test_search_queries_are_normalized_into_song_searches():
    """A small model may return bare concepts; grounding must still search songs."""
    from music_telling.llm.discover import normalize_queries

    out = normalize_queries("leaving home", ["Staying home", "best tracks about exile"])
    assert out[0] == "songs about leaving home"  # theme-derived floor
    assert "songs about Staying home" in out  # bare concept repaired
    assert "best tracks about exile" in out  # already a song query
    assert len(out) == len(set(q.lower() for q in out))  # deduped


def test_gather_context_respects_the_char_budget(monkeypatch):
    """An uncapped sweep would overflow a local model's context window."""
    from music_telling.sources import websearch

    monkeypatch.setattr(
        websearch,
        "search_web",
        lambda q, max_results=6: [
            {"title": f"{q}-{i}", "body": "x" * 500, "href": f"h{q}{i}"} for i in range(6)
        ],
    )
    out = websearch.gather_context(["a", "b", "c"], per_query=6, max_chars=1000)
    assert len(out) <= 1000
    # Round-robin: every query contributes before the budget runs out.
    assert out.count("### results for:") == 3


def test_every_array_in_every_schema_is_bounded():
    """Unbounded arrays let constrained decoding generate forever.

    This is not theoretical: an unbounded `list[Candidate]` made a local model
    emit 8871 tokens without ever closing the array, hanging the pipeline.
    """
    from music_telling import models as M

    schemas = [
        M.CandidateList,
        M.ThemeBrief,
        M.Judgement,
        M.ScreenVerdict,
        M.Story,
        M.StoryOrder,
    ]
    unbounded = []

    def walk(node, path):
        if isinstance(node, dict):
            if node.get("type") == "array" and "maxItems" not in node:
                unbounded.append(path)
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    for model in schemas:
        walk(json_schema(model), model.__name__)
    assert not unbounded, f"unbounded arrays: {unbounded}"
