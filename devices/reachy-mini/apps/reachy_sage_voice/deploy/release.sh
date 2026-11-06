#!/bin/sh
# make release: HEAD, tagged voice/v<pyproject version>, to the Hugging Face Space (distribution only).
# The upload is a whitelist of tracked files, scanned for addresses and home folders first;
# DRY_RUN=1 lists it and stops. The Space then mirrors it: files no longer shipped are deleted.
set -eu
die() { echo "release: $*" >&2; exit 1; }

version=$(git show HEAD:./pyproject.toml | python3 -I -c 'import sys, tomllib; print(tomllib.loads(sys.stdin.read())["project"]["version"])')
tag=voice/v$version
[ "$(git rev-parse -q --verify "refs/tags/$tag^{commit}" || true)" = "$(git rev-parse HEAD)" ] \
	|| die "tag $tag is not at HEAD (pyproject.toml says $version): git tag $tag, or bump the version"
sha=$(git rev-parse --short=7 HEAD)
top=$(git rev-parse --show-toplevel)
tree=HEAD:$(git rev-parse --show-prefix)  # this folder's tree, read from the top: no cwd limiting

# What the Space needs to run the voice as the README says (make run, make status, make -C services up).
SHIP='^(README\.md|index\.html|style\.css|LICENSE|NOTICE|pyproject\.toml|uv\.lock|\.python-version|\.env\.example|Makefile|audit-ignore\.txt|reachy_sage_voice/.*|services/.*)$'
# Never: settings, the owner's voice, recordings, tests and their fixtures.
NEVER='(^|/)\.env$|^services/profiles/|(^|/)recordings?/|\.wav$|(^|/)test_[^/]*\.py$|/fixtures/'
files=$(git -C "$top" ls-tree -r --name-only "$tree" | grep -E "$SHIP" | grep -vE "$NEVER")

dir=$(mktemp -d "${TMPDIR:-/tmp}/voice-release.XXXXXX")
trap 'rm -rf "$dir"' EXIT
# shellcheck disable=SC2086  # one tracked path per word: none has a space
git -C "$top" archive "$tree" $files | tar -x -C "$dir"
# The path dependencies (devices/common) ship as vendor/<name>, their path rewritten: no
# devices/common on the Space. Their tests stay out like the app's.
for dep in $(git show HEAD:./pyproject.toml | python3 -I deploy/sources.py "$(git rev-parse --show-prefix)" | tr ' ' '='); do
	raw=${dep%%=*} path=${dep#*=}
	name=vendor/$(basename "$path")
	dep_files=$(git -C "$top" ls-tree -r --name-only "HEAD:$path" | grep -vE "$NEVER")
	mkdir -p "$dir/$name"
	# shellcheck disable=SC2086
	git -C "$top" archive "HEAD:$path" $dep_files | tar -x -C "$dir/$name"
	for f in pyproject.toml uv.lock; do
		[ ! -f "$dir/$f" ] || python3 -I -c 'import sys; p, a, b = sys.argv[1:]; t = open(p).read(); open(p, "w").write(t.replace(f"\"{a}\"", f"\"{b}\""))' "$dir/$f" "$raw" "$name"
	done
	files="$files
$(printf '%s\n' $dep_files | sed "s|^|$name/|")"
	echo "vendored $raw as $name"
done
# Every path source of the upload has to resolve inside it, and the lock may not name the old one.
python3 -I deploy/sources.py < "$dir/pyproject.toml" | while read -r raw path; do
	[ -f "$dir/$path/pyproject.toml" ] || die "the upload's pyproject names $raw, which it does not carry"
done
! grep -qF '"../' "$dir/uv.lock" 2>/dev/null || die "the upload's uv.lock still names a path outside it"
# No pipefail in sh: an archive that failed must never become an upload that deletes the Space.
[ -n "$files" ] || die "nothing to ship"
for f in $files; do [ -f "$dir/$f" ] || die "the export is missing $f"; done
python3 -I deploy/scan.py "$dir" || die "nothing uploaded"
printf 'release: %s\n' $files
if [ -n "${DRY_RUN:-}" ]; then
	echo "DRY_RUN: would upload these $(echo "$files" | wc -l | tr -d ' ') files to $SPACE as \"$tag $sha\""
	exit 0
fi
$HF upload "$SPACE" "$dir" . --repo-type space --delete '*' --commit-message "$tag $sha"
