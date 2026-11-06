"""Eval statistics and discipline: paired tests, confidence intervals, ledger rows, frozen sets."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

import score  # noqa: E402

SHA = "ab" * 32


def test_wilson_interval_matches_known_values():
    assert score.wilson(5, 10) == pytest.approx((0.2366, 0.7634), abs=1e-4)
    lo, hi = score.wilson(0, 10)
    assert lo == 0 and hi == pytest.approx(0.2775, abs=1e-4)
    assert score.wilson(0, 0) is None


def test_mcnemar_is_the_exact_binomial_on_discordant_pairs():
    a = [False] * 10 + [False] * 40
    b = [True] * 10 + [False] * 40
    result = score.mcnemar(a, b)
    assert (result["a_only"], result["b_only"]) == (0, 10)
    assert result["p"] == pytest.approx(2 * 0.5 ** 10)
    assert score.mcnemar(b, b)["p"] == 1.0
    swapped = score.mcnemar([True] * 5 + [False] * 5, [False] * 5 + [True] * 5)
    assert swapped["p"] == 1.0


def test_paired_bootstrap_finds_a_real_difference_and_no_difference():
    refs = [f"look left number {i}" for i in range(50)]
    perfect = list(refs)
    worse = [r if i >= 20 else "luke laughed" for i, r in enumerate(refs)]
    found = score.paired_wer(refs, perfect, worse)
    assert found["n"] == 50 and found["wer_a"] == 0
    assert found["delta"] == pytest.approx(-found["wer_b"])
    lo, hi = found["ci"]
    assert lo <= found["delta"] <= hi < 0  # A is better, and the interval says so
    assert found["mcnemar"]["p"] < 1e-5
    tie = score.paired_wer(refs, [r if i >= 10 else "x" for i, r in enumerate(refs)],
                           [r if not 10 <= i < 20 else "x" for i, r in enumerate(refs)])
    assert tie["delta"] == 0 and tie["ci"][0] < 0 < tie["ci"][1] and tie["mcnemar"]["p"] == 1.0


def test_paired_bootstrap_is_repeatable_by_seed():
    refs = ["a b c d"] * 30
    a = ["a b c d" if i % 3 else "a x c d" for i in range(30)]
    b = ["a b c d" if i % 2 else "x x c d" for i in range(30)]
    assert score.paired_wer(refs, a, b, seed=1)["ci"] == score.paired_wer(refs, a, b, seed=1)["ci"]


def test_wer_ci_brackets_the_corpus_wer():
    refs = ["one two three four"] * 40
    hyps = ["one two three four" if i % 4 else "one too three for" for i in range(40)]
    lo, hi = score.wer_ci(refs, hyps)
    assert lo < score.wer(refs, hyps) < hi


# The ledger


def test_ledger_refuses_a_row_without_n_ci_or_manifest_hash(tmp_path):
    path = tmp_path / "LEDGER.md"
    good = {"n": 30, "ci": (0.1, 0.3), "sha256": SHA}
    for missing in ("n", "ci", "sha256"):
        with pytest.raises(ValueError, match=missing):
            score.ledger(path, "stt", "a", "set", "wer 0.200", **{**good, missing: None})
    for bad in ({"n": 0}, {"ci": (0.3, 0.1)}, {"sha256": "abc"}):
        with pytest.raises(ValueError):
            score.ledger(path, "stt", "a", "set", "wer 0.200", **{**good, **bad})
    assert not path.exists()
    score.ledger(path, "stt", "a", "set", "wer 0.200", **good)
    score.ledger(path, "stt", "b", "set", "wer 0.100", **good)
    lines = path.read_text().splitlines()
    assert sum("| date |" in line for line in lines) == 1
    assert lines[-1].endswith(f"| stt | b | set | 30 | [0.100, 0.300] | wer 0.100 | {SHA} |")


def write_manifest(folder: Path, rows: list[dict]) -> None:
    folder.mkdir(exist_ok=True)
    (folder / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


ROWS = [{"wav": "a.wav", "session": "1-quiet", "text": "x"}, {"wav": "b.wav", "session": "2-quiet",
                                                              "text": "y"}]


def test_manifest_hash_covers_exactly_the_lines_scored(tmp_path):
    write_manifest(tmp_path, ROWS)
    whole = hashlib.sha256((tmp_path / "manifest.jsonl").read_bytes()).hexdigest()
    assert score.manifest_sha256(tmp_path) == whole
    one = score.manifest_sha256(tmp_path, ["2-quiet"])
    assert one == hashlib.sha256((json.dumps(ROWS[1]) + "\n").encode()).hexdigest()


def test_freeze_writes_the_hash_once_and_refuses_a_changed_test_set(tmp_path):
    data, frozen = tmp_path / "set", tmp_path / "FROZEN.md"
    write_manifest(data, ROWS)
    sha = score.freeze(data, ["2-quiet"], frozen)
    assert sha == score.manifest_sha256(data, ["2-quiet"])
    assert score.freeze(data, ["2-quiet"], frozen) == sha  # again: nothing new
    assert sum(sha in line for line in frozen.read_text().splitlines()) == 1
    write_manifest(data, [ROWS[0], {**ROWS[1], "text": "relabeled"}])
    with pytest.raises(ValueError, match="frozen"):
        score.freeze(data, ["2-quiet"], frozen)


def test_freeze_cli(tmp_path, capsys):
    data, frozen = tmp_path / "set", tmp_path / "FROZEN.md"
    write_manifest(data, ROWS)
    score.main(["freeze", str(data), "--session", "2-quiet", "--frozen", str(frozen)])
    assert score.manifest_sha256(data, ["2-quiet"]) in capsys.readouterr().out
    assert "2-quiet" in frozen.read_text()


def test_paired_cli_compares_two_transcript_sets_and_writes_a_full_row(tmp_path, capsys):
    data = tmp_path / "set"
    rows = [{"wav": f"{i}.wav", "text": "look left", "speaker": "owner", "session": "s"} for i in range(20)]
    write_manifest(data, rows)
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_text("".join(json.dumps({"wav": r["wav"], "text": "look left"}) + "\n" for r in rows))
    b.write_text("".join(json.dumps({"wav": r["wav"], "text": "luke left"}) + "\n" for r in rows))
    ledger = tmp_path / "LEDGER.md"
    score.main(["paired", str(data), "--a", str(a), "--b", str(b), "--ledger", str(ledger)])
    assert "mcnemar" in capsys.readouterr().out
    row = ledger.read_text().splitlines()[-1]
    assert "| paired |" in row and "| 20 |" in row and score.manifest_sha256(data) in row
    assert "delta -0.500" in row
