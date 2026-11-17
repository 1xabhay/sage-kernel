"""Unit checks for the storage helpers, chiefly the traversal guard.

The API tests reach these through HTTP, where routing already rejects most
slashes; these hit the guard directly so it is not silently load-bearing.
"""

import os

import pytest
from reachy_mini_testbench import store


@pytest.mark.parametrize(
    "filename",
    ["../secret.jpg", "../../etc/passwd", "sub/dir.jpg", "/etc/passwd", ".."],
)
def test_safe_path_rejects_escapes(tmp_path, filename):
    with pytest.raises(ValueError, match="illegal filename"):
        store.safe_path(tmp_path, filename)


def test_safe_path_accepts_a_plain_name(tmp_path):
    assert store.safe_path(tmp_path, "capture_1.jpg") == tmp_path / "capture_1.jpg"


def test_listing_filters_by_suffix_and_sorts_newest_first(tmp_path):
    for age, name in enumerate(("b.png", "notes.txt", "a.jpg")):
        (tmp_path / name).write_bytes(b"x")
        os.utime(tmp_path / name, (1_700_000_000 - age * 60,) * 2)  # b.png is the newest

    assert [e["filename"] for e in store.listing(tmp_path, (".jpg", ".png"))] == ["b.png", "a.jpg"]


def test_listing_of_a_missing_directory_is_empty(tmp_path):
    assert store.listing(tmp_path / "nope", (".jpg",)) == []
