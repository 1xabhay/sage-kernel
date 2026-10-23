"""Keep the docs honest: every relative link and every documented `make` command resolves.

    python3 make/docs.py --check     check the tracked *.md files of this checkout
    python3 make/docs.py             run the self-test

For each tracked Markdown file it reports
  (a) a relative link that names no tracked file or folder, and
  (b) a `make [-C dir] target` written in a code span or block that `make -n -C dir target` rejects.
Nothing is run: `make -n` only prints the recipe. A `make` line marked "(coming: ...)" documents a
target another lane is still building, and is skipped. Stdlib only.
"""

import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]

# Paths owned by other lanes, which fix their own docs (findings are listed in their PRs).
SKIP = (
    "devices/reachy-mini/apps/reachy_sage_voice/",  # VOICE lane (B3, reachy/sage-voice)
    "devices/reachy-mini/apps/reachy_mini_pet/",  # PET lane (D1, devices/archive-pet): archived or ported
    "experiments/",  # EXPERIMENTS lane (PR #6, repo/experiments)
    "apps/music-telling/",  # EXPERIMENTS lane: PR #6 moves it to experiments/music-telling
)
# Records of the old kernel: their links must resolve, their `make` commands were that repo's.
HISTORICAL = ("docs/history/",)

FENCE = re.compile(r"^\s*(```|~~~)")
CODE_SPAN = re.compile(r"(`+)(.+?)\1")
LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REF_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)")
MAKE = re.compile(r"(?<![\w/.$-])make((?:\s+\S+)*)")
TARGET = re.compile(r"[A-Za-z0-9_][\w.-]*")


def tracked(root: Path) -> set[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True, check=True)
    return set(filter(None, out.stdout.split("\0")))


def scan(text: str):
    """Yield (line number, prose without code spans, code spans and code-block lines) per line."""
    fenced = False
    for n, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            yield n, "", [line]
        else:
            yield n, CODE_SPAN.sub("", line), [m.group(2) for m in CODE_SPAN.finditer(line)]


def make_calls(code: str):
    """(dirs given with -C, targets) for each `make` invocation in a piece of shell."""
    for m in MAKE.finditer(code):
        try:
            words = shlex.split(m.group(1))
        except ValueError:  # an unbalanced quote: good enough to find the targets
            words = m.group(1).split()
        dirs, targets = [], []
        i = 0
        while i < len(words):
            w = words[i]
            if w == "-C" and i + 1 < len(words):
                dirs.append(words[i + 1])
                i += 2
                continue
            if w.startswith("-") or "=" in w:
                i += 1
                continue
            if not TARGET.fullmatch(w):
                break  # `#`, `&&`, `|`, prose: the command ends here
            targets.append(w)
            i += 1
        yield dirs, targets


def norm(path: str):
    """A repo-relative path with `.` and `..` folded ("" is the root), or None if it climbs out."""
    parts: list[str] = []
    for part in path.split("/"):
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part not in ("", "."):
            parts.append(part)
    return "/".join(parts)


def home(path: str, files: set[str]) -> PurePosixPath:
    """Where a doc's `make` runs: the nearest folder at or above it that has a tracked Makefile."""
    # ponytail: nearest-Makefile guess; a doc that means another folder must say `make -C dir`.
    d = PurePosixPath(path).parent
    while str(d) != "." and f"{d}/Makefile" not in files:
        d = d.parent
    return d


def resolves(root: Path, d: str, target: str, cache: dict) -> bool:
    key = (str(d), target)
    if key not in cache:
        if not (root / d / "Makefile").is_file():
            cache[key] = False
        else:
            r = subprocess.run(["make", "-n", "-C", str(root / d), target], capture_output=True, text=True)
            cache[key] = r.returncode == 0
    return cache[key]


def check(root: Path = ROOT, skip=SKIP) -> list[str]:
    files = tracked(root)
    dirs = {str(p) for f in files for p in PurePosixPath(f).parents}
    cache: dict = {}
    findings = []
    for path in sorted(f for f in files if f.lower().endswith(".md") and not f.startswith(tuple(skip))):
        base = PurePosixPath(path).parent
        for n, prose, code in scan((root / path).read_text(encoding="utf-8", errors="replace")):
            for raw in LINK.findall(prose) + REF_DEF.findall(prose):
                target = raw.split("#", 1)[0].split("?", 1)[0]
                if not target or re.match(r"[a-z][a-z0-9+.-]*:", target, re.I):
                    continue  # an anchor in this page, or a URL
                rel = norm(target.lstrip("/") if target.startswith("/") else f"{base}/{target}")
                if rel is None or not (rel == "" or rel in files or rel in dirs):
                    findings.append(f"{path}:{n}: link {raw} resolves to no tracked path")
            if path.startswith(HISTORICAL) or "(coming:" in prose or any("(coming:" in c for c in code):
                continue
            for c in code:
                for cdirs, targets in make_calls(c):
                    d = norm("/".join([str(home(path, files)), *cdirs]))
                    if d is None or any(x.startswith(("/", "~", "$")) for x in cdirs):
                        continue  # outside this checkout: not ours to check
                    for t in targets or [None]:
                        if t is None:
                            continue  # bare `make`: the default goal, nothing named to check
                        if not resolves(root, d, t, cache):
                            where = f" -C {d}" if d else ""
                            findings.append(f"{path}:{n}: `make{where} {t}` does not resolve")
    return findings


def selftest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        r = Path(tmp)
        (r / "sub").mkdir()
        (r / "Makefile").write_text("test:\n\techo test\n")
        (r / "sub" / "Makefile").write_text("run:\n\techo run\n")
        (r / "sub" / "README.md").write_text(
            "[up](../README.md) [gone](../NOPE.md) [web](https://x.y) [here](#a) [dir](../sub/)\n"
            "`make run` `make test`\n"  # sub's Makefile: run resolves, test does not
            "```sh\nmake -C .. test   # root\nFOO=1 make -C .. nope && echo\nmake run X=\"a b\"\n```\n"
            "`make deploy` (coming: C4)\n"
            "`[not](a/link.md)` and `cmake build`\n"
        )
        (r / "README.md").write_text("[sub](sub/README.md) [ref]\n\n[ref]: sub/missing.md\n")
        (r / "docs" / "history").mkdir(parents=True)
        (r / "docs" / "history" / "old.md").write_text("[ok](../../README.md) [bad](gone.md) `make old`\n")
        (r / "skipped").mkdir()
        (r / "skipped" / "x.md").write_text("[bad](nowhere.md) `make nope`\n")
        subprocess.run(["git", "init", "-q", str(r)], check=True)
        subprocess.run(["git", "-C", str(r), "add", "."], check=True)
        got = check(r, skip=("skipped/",))
        assert got == [
            "README.md:3: link sub/missing.md resolves to no tracked path",
            "docs/history/old.md:1: link gone.md resolves to no tracked path",
            "sub/README.md:1: link ../NOPE.md resolves to no tracked path",
            "sub/README.md:2: `make -C sub test` does not resolve",
            "sub/README.md:5: `make nope` does not resolve",
        ], got
        assert list(make_calls("make -C a -C b x y # c")) == [(["a", "b"], ["x", "y"])]


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        found = check()
        print("\n".join(found) or "docs: every link and make command resolves")
        sys.exit(1 if found else 0)
    selftest()
    print("docs.py self-test OK")
