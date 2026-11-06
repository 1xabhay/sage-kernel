"""No tracked file under devices/ names a LAN address, a hardware address or a home folder.

The robot's and the Mac's addresses live in local files only (robot/.env, the robot's
/etc/sage-guard/hosts.nft, REACHY_MINI_HOST), and the kernel is found from the checkout.
"""

import pathlib
import re
import subprocess

import pytest

DEVICES = pathlib.Path(__file__).resolve().parents[4]
FORBIDDEN = re.compile(
    r"\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"  # private IPv4
    r"|\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b"  # hardware address
    r"|/(?:Users|home)/[a-z]"  # a home folder
)


def findings(text: str) -> list[str]:
    return [match.group(0) for match in FORBIDDEN.finditer(text)]


def test_no_tracked_file_under_devices_names_an_address_or_a_home_folder():
    try:
        listing = subprocess.run(["git", "ls-files", "-z"], cwd=DEVICES, check=True,
                                 capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not in a git checkout")
    bad = []
    for name in filter(None, listing.split("\0")):
        try:
            text = (DEVICES / name).read_text()
        except (UnicodeDecodeError, FileNotFoundError):
            continue  # binary, or deleted in the working tree
        bad += [f"{name}:{text.count(chr(10), 0, text.index(hit)) + 1}: {hit}" for hit in findings(text)]
    assert bad == []


def test_each_kind_is_caught():
    hw = ":".join(["02", "00", "5e", "00", "53", "01"])
    planted = ["ROBOT ?= pollen@192.168" + ".1.9", "10.0.0" + ".1", "172.20.1" + ".2", hw, hw.upper(),
               "/Users" + "/someone/x", "/home" + "/someone"]
    assert [bool(findings(line)) for line in planted] == [True] * len(planted)
    assert findings("127.0.0.1:8776 0.0.0.0 1.10.0 172.15.0.1 10:30 /Users/<u>") == []
