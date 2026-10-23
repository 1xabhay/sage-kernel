#!/bin/sh
# Checks the Makefile and compose file keep their promises, without running Docker:
# `make test` never streams evals/ (the owner's live speech), the compose project is
# named, the standard targets exist, and README links resolve.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
fail=0
no() { echo "FAIL: $*" >&2; fail=1; }

# A scratch copy with an evals/ folder, so $(wildcard) would pick it up. The real evals/ is never read.
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cp "$here/Makefile" "$tmp/" && mkdir "$tmp/evals" "$tmp/core" && touch "$tmp/Cargo.toml"
for t in test lint vuln; do
  make -s -n -C "$tmp" "$t" >"$tmp/out" 2>&1 || no "make -n $t: $(cat "$tmp/out")"
  grep -q 'evals' "$tmp/out" && no "make $t streams evals/"
done
for t in secrets imports; do make -s -n -C "$here" "$t" >/dev/null 2>&1 || no "no make $t"; done
grep -qx 'name: sage-os' "$here/compose.yaml" || no "compose.yaml has no 'name: sage-os'"

# Relative Markdown links in the README must exist.
for link in $(grep -o '](\.[^)#]*' "$here/README.md" | cut -c3-); do
  [ -e "$here/$link" ] || no "README links to missing $link"
done
grep -nE '`\.\./sage(-kernel)?`' "$here/README.md" && no "README names dead ../sage or ../sage-kernel"
[ "$fail" = 0 ] && echo "check-make: ok"
exit "$fail"
