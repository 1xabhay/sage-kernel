#!/usr/bin/env python3
"""Nothing published names this network or this machine: no private IPv4 address, no hardware (MAC) address,
no home folder. `--tree` scans every tracked file, `--stdin` scans lines piped in (pre-push sends the added
lines of the pushed range), `--self-test` checks the patterns. Exit 1 on any hit. Standard library only."""

import re
import subprocess
import sys

PATTERN = re.compile(
    r"\b(?:192\.168(?:\.\d{1,3}){2}|10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
    r"|\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b"
    r"|(?-i:/Users/[a-z]|/home/[a-z])",
    re.IGNORECASE,
)


def hits(lines, where):
    return [f"{where}:{n}: {m.group(0)}" for n, line in enumerate(lines, 1) for m in PATTERN.finditer(line)]


def tree():
    paths = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True).stdout.split(b"\0")
    found = []
    for path in filter(None, paths):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                found += hits(f, path.decode())
        except (IsADirectoryError, FileNotFoundError):  # submodules, deleted-but-tracked files
            pass
    return found


def self_test():
    # Built by concatenation so this file never matches itself.
    bad = ["192.168" + ".1.20", "10" + ".0.0.7", "172.16" + ".3.4", "172.31" + ".0.1", "aa:bb:cc" + ":dd:ee:ff",
           "AA:BB:CC" + ":DD:EE:FF", "/Users" + "/abby", "/home" + "/pi/x"]
    good = ["127.0.0.1", "0.0.0.0", "172.15.0.1", "172.32.0.1", "8.8.8.8", "110.1.2.3", "v1.10.2", "12:30:00",
            "/Users/", "/home/$USER", "/Users/X", "sha256:c00b6bd0aeb3", "localhost:8776"]
    for s in bad:
        assert PATTERN.search(s), s
    for s in good:
        assert not PATTERN.search(s), s
    print("hygiene self-test: ok")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode == "--self-test":
        sys.exit(self_test())
    if mode not in ("--tree", "--stdin"):
        sys.exit("usage: hygiene.py --tree | --stdin | --self-test")
    found = tree() if mode == "--tree" else hits(sys.stdin, "stdin")
    print("\n".join(found), file=sys.stderr) if found else None
    sys.exit(1 if found else 0)
