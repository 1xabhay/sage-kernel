#!/bin/sh
# The voice as a launchd LaunchAgent on this Mac, from a SHA-pinned export (README, "Deploy").
#   deploy.sh deploy                  make deploy SHA=<sha>: refuse, export, test, check, load, wait
#   deploy.sh plist <app> <sha> <out> make plist: render deploy/launchd.plist.in
#   deploy.sh export <sha> <out>      the export alone, at <out>/<the app's path from the top>
#   deploy.sh undeploy                make undeploy: unload, remove the plist (the exports stay)
# The settings (LABEL, LOGS, PLIST, ...) come from the Makefile, which exports them.
set -eu
umask 077
uid=$(id -u)
die() { echo "deploy: $*" >&2; exit 1; }

render() {  # ponytail: paths go into the XML unescaped; one with & or < would need escaping
	mkdir -p "$(dirname "$3")"
	sed -e "s|@LABEL@|$LABEL|g" -e "s|@APP@|$1|g" -e "s|@SHA@|$2|g" -e "s|@LOG@|$LOGS/bot-$2.log|g" \
		-e "s|@PATH@|$LAUNCH_PATH|g" deploy/launchd.plist.in > "$3"
}

export_tree() {  # <commit> <out>: the app, at its path from the top, and what its lock needs
	top=$(git rev-parse --show-toplevel)
	prefix=$(git rev-parse --show-prefix)
	rm -rf "$2.tmp" && mkdir -p "$2.tmp"
	root_files=""
	git cat-file -e "$1:pyproject.toml" 2>/dev/null && root_files=pyproject.toml  # test_docs reads it
	# uv sync --locked resolves the path dependencies from the export, so they come along.
	deps=$(git show "$1:./pyproject.toml" | python3 -I deploy/sources.py "$prefix" | cut -d' ' -f2)
	# shellcheck disable=SC2086  # one tracked path per word: none has a space
	git -C "$top" archive "$1" $root_files "$prefix" $deps | tar -x -C "$2.tmp"
	[ -f "$2.tmp/$prefix/pyproject.toml" ] || die "git archive of $1 failed"
	for dep in $deps; do [ -f "$2.tmp/$dep/pyproject.toml" ] || die "the export of $1 is missing $dep"; done
	mv "$2.tmp" "$2"
}

deploy() {
	[ -z "$(git status --porcelain -- .)" ] || die "the app's tree is dirty: commit or stash it first (git status -- .)"
	full=$(git rev-parse --verify -q "$SHA^{commit}") || die "$SHA is not a commit here"
	sha=$(git rev-parse --short=7 "$full")
	git -C "$DEPLOY_ROOT" check-ignore -q .deploy/voice \
		|| die "$DEPLOY_ROOT/.deploy/ is not git-ignored: the guards PR ignores it (or: echo .deploy/ >> $DEPLOY_ROOT/.git/info/exclude)"
	[ -f "$ENV_FILE" ] || die "no settings at $ENV_FILE: run make env, then fill in the token"

	src=$(pwd)
	top=$(git rev-parse --show-toplevel)
	prefix=$(git rev-parse --show-prefix)
	out=$DEPLOY_ROOT/.deploy/voice/$sha
	app=${out}/${prefix%/}
	if [ ! -d "$app" ]; then  # an export is never rewritten: a running voice may be in it
		export_tree "$full" "$out"
	fi
	echo "deploy: $sha exported to $app"
	cd "$app"
	env -u VIRTUAL_ENV uv sync --locked

	rc=0
	# The export sits inside the main checkout: stop git there, or tests read that repo.
	tests=$(GIT_CEILING_DIRECTORIES=$out env -u VIRTUAL_ENV uv run --frozen pytest -q -rfE -p no:cacheprovider 2>&1) || rc=$?
	printf '%s\n' "$tests" | tail -n 12
	passed=$(printf '%s\n' "$tests" | grep -oE '[0-9]+ passed' | tail -n 1 | cut -d' ' -f1)
	note=""
	if [ "$rc" -ne 0 ]; then
		failed=$(printf '%s\n' "$tests" | sed -n 's/^FAILED \([^ ]*\).*/\1/p')
		errors=$(printf '%s\n' "$tests" | grep -c '^ERROR ' || true)
		if [ "$rc" -eq 1 ] && [ -n "${DEPLOY_ALLOW_XFAIL:-}" ] && [ "$failed" = "$DEPLOY_ALLOW_XFAIL" ] && [ "$errors" -eq 0 ]; then
			echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
			echo "!!! DEPLOY_ALLOW_XFAIL: $DEPLOY_ALLOW_XFAIL FAILED at $sha and is let through"
			echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
			note=", 1 allowed fail $DEPLOY_ALLOW_XFAIL"
		else
			die "the tests fail at $sha (exit $rc): nothing was loaded. Only DEPLOY_ALLOW_XFAIL=<nodeid> lets one known failure through"
		fi
	fi

	.venv/bin/python -c 'import sys
from reachy_sage_voice import status
from reachy_sage_voice.__main__ import load_settings
load_settings()
sys.exit(status.main())' || die "make status: something is down (above); nothing was loaded"

	# One voice drives the robot: a hand-run one (make run) has to stop first.
	agent=$(launchctl print "gui/$uid/$LABEL" 2>/dev/null | awk '$1 == "pid" { print $3 }')
	others=$(pgrep -f 'python[0-9.]* (-u )?-m reachy_sage_voice' | grep -vx "${agent:-none}" || true)
	[ -z "$others" ] || die "a voice already runs outside launchd (pid $(echo $others)): stop it first"

	log=$LOGS/bot-$sha.log
	mkdir -p "$LOGS" && touch "$log"
	start=$(wc -c < "$log")
	cd "$src"  # the template is this checkout's, so a rollback to an older SHA renders too
	render "$app" "$sha" "$PLIST"
	plutil -lint -s "$PLIST"
	launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
	n=0
	while launchctl print "gui/$uid/$LABEL" >/dev/null 2>&1; do
		n=$((n + 1)) && [ "$n" -le 30 ] || die "$LABEL did not unload"
		sleep 1
	done
	launchctl bootstrap "gui/$uid" "$PLIST"
	launchctl kickstart -k "gui/$uid/$LABEL"

	waited=0
	until tail -c "+$((start + 1))" "$log" | grep -q "$STARTED"; do
		if [ "$waited" -ge "$DEPLOY_WAIT_S" ]; then
			tail -n 30 "$log" >&2
			die "no \"$STARTED\" in $log after ${DEPLOY_WAIT_S}s. It stays loaded and launchd retries it: make logs, or roll back with make deploy SHA=<previous>"
		fi
		sleep 2 && waited=$((waited + 2))
	done
	echo "deploy: $sha is running ($LABEL, log $log)"

	mkdir -p "$(dirname "$DEPLOY_LOG")"
	echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) deploy $sha voice tests $passed pass$note" >> "$DEPLOY_LOG"

	# The last three exports stay, for rollback; only .deploy/voice is touched.
	cd "$DEPLOY_ROOT/.deploy/voice" && touch "$sha"
	ls -1t | grep -E '^[0-9a-f]{7,40}$' | tail -n +4 | while read -r old; do
		rm -rf "./$old" && echo "deploy: removed the old export $old"
	done
}

case "${1:-}" in
	deploy) deploy ;;
	plist) render "$2" "$3" "$4" ;;
	export) export_tree "$(git rev-parse --verify "$2^{commit}")" "$3" ;;
	undeploy)
		launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
		rm -f "$PLIST"
		echo "deploy: $LABEL unloaded and $PLIST removed; the exports stay in $DEPLOY_ROOT/.deploy/voice" ;;
	*) die "usage: deploy.sh deploy | plist <app> <sha> <out> | export <sha> <out> | undeploy" ;;
esac
