"""make prune: old Sage images, voice exports, recordings and journal backups; nothing else, ever.

`python3 make/prune.py <the main checkout>`; DRY_RUN=1 lists and deletes nothing. Stdlib only.
Keeps the newest 3 tags of each sage-kernel and reachy-sage-* image (apart, the newest 3 dev-<lane>
test builds) and any a container uses, the newest 2 voice exports and the one launchd runs
(VOICE_PLIST), recording sessions up to 7 days old by the date in their name, and the newest 14
journal backups (make backup's journal-*.sqlite3: a backup the owner named stays). Volumes and
other images are never touched.
"""

import datetime as dt
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

IMAGE = re.compile(r"^(sage-kernel|reachy-sage-[a-z0-9-]+)$")
EXPORT = re.compile(r"^[0-9a-f]{7,40}$")
SESSION = re.compile(r"^session-(\d{8})-\d{6}$")
BACKUP = re.compile(r"^journal-\d{8}T\d{6}Z\.sqlite3$")
KEEP_TAGS, KEEP_EXPORTS, SESSION_DAYS, KEEP_BACKUPS = 3, 2, 7, 14


def stale(dates: dict, keep: int, using=()) -> list:
    """The names to delete: all but the newest ``keep`` by date, and never one in ``using``."""
    newest = set(sorted(dates, key=dates.get, reverse=True)[:keep])
    return sorted(name for name in dates if name not in newest and name not in using)


def old_sessions(names, today: dt.date, days: int) -> list:
    """The recording sessions whose name's date is more than ``days`` before ``today``."""
    cutoff = (today - dt.timedelta(days=days)).strftime("%Y%m%d")
    return sorted(name for name in names if (m := SESSION.match(name)) and m.group(1) < cutoff)


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout


def images() -> list:
    """The image tags to delete, per repository; a tag any container (even stopped) uses stays."""
    using = set(docker("ps", "-a", "--format", "{{.Image}}").split())
    repos: dict = {}
    for line in docker("images", "--format", "{{.Repository}}\t{{.Tag}}\t{{.CreatedAt}}").splitlines():
        repo, tag, created = line.split("\t")
        if IMAGE.match(repo) and tag != "<none>":
            when = dt.datetime.strptime(created[:25], "%Y-%m-%d %H:%M:%S %z")
            # A lane's test build (dev-<lane>) never pushes a deployable tag out: each counts apart.
            repos.setdefault((repo, tag.startswith("dev-")), {})[f"{repo}:{tag}"] = when
    return [name for tags in repos.values() for name in stale(tags, KEEP_TAGS, using)]


def running_export(voice: Path) -> str:
    """The export the voice's LaunchAgent runs, from its WorkingDirectory; '' if none."""
    plist = Path(os.environ.get("VOICE_PLIST", "~/Library/LaunchAgents/ai.sage.reachy-voice.plist")).expanduser()
    try:
        app = Path(plistlib.loads(plist.read_bytes())["WorkingDirectory"])
        return app.relative_to(voice).parts[0]
    except (OSError, KeyError, ValueError, IndexError, plistlib.InvalidFileException):
        return ""


def names(folder: Path, pattern: re.Pattern) -> list:
    return [p.name for p in folder.iterdir() if pattern.match(p.name)] if folder.is_dir() else []


def main(root: Path) -> int:
    dry = bool(os.environ.get("DRY_RUN"))
    voice, recordings, backups = root / ".deploy" / "voice", root / ".deploy" / "recordings", root / ".backups"
    exports = {n: (voice / n).stat().st_mtime for n in names(voice, EXPORT)}
    paths = [voice / n for n in stale(exports, KEEP_EXPORTS, {running_export(voice)})]
    paths += [recordings / n for n in old_sessions(names(recordings, SESSION), dt.date.today(), SESSION_DAYS)]
    paths += [backups / n for n in stale({n: n for n in names(backups, BACKUP)}, KEEP_BACKUPS)]
    failed = 0
    for image in images():
        print(f"{'would delete' if dry else 'delete'} {image}")
        if not dry:  # no -f: an image a container needs is refused
            failed |= subprocess.run(["docker", "rmi", image], check=False, stdout=subprocess.DEVNULL).returncode
    for path in paths:
        print(f"{'would delete' if dry else 'delete'} {path}")
        if not dry:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
