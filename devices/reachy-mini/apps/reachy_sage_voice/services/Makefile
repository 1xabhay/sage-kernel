.PHONY: test lint imports vuln secrets up down hygiene deploy up-tag

# Native runs keep the Hugging Face cache inside this folder.
export HF_HOME := $(CURDIR)/.local/hf

# The tag of stt, research and searxng (compose.yaml): by default the one running, so enroll, down
# and the rest act on the live images; dev when none runs. deploy and up-tag name their own.
ifndef SAGE_SERVICES_TAG
SAGE_SERVICES_TAG := $(or $(shell docker inspect -f '{{.Config.Image}}' reachy-sage-voice-stt-1 2>/dev/null \
  | sed 's/.*://'),dev)
endif
export SAGE_SERVICES_TAG

test:
	uv run python -m unittest discover -s stt -t stt
	uv run python -m unittest discover -s tts -t tts
	uv run python -m unittest discover -s research -t research

# Each image installs only its own group, with hashes.
lock:
	uv lock
	uv export --frozen --no-default-groups --group stt --no-emit-project -o stt/requirements.lock
	uv export --frozen --no-default-groups --group tts --no-emit-project -o tts/requirements.lock
	uv export --frozen --no-default-groups --group research --no-emit-project -o research/requirements.lock

# The contract tests again, inside the sealed images (of the working tree, tagged dev).
test-docker: export SAGE_SERVICES_TAG := dev
test-docker:
	docker compose build
	docker compose run --rm --no-deps stt python -m unittest test_app
	docker compose run --rm --no-deps tts python -m unittest test_app
	docker compose run --rm --no-deps research python -m unittest test_app

# The working tree, tagged dev (a checkout with no git, such as the Space, has only this).
# On this Mac, make deploy runs a commit instead.
up: export SAGE_SERVICES_TAG := dev
up:
	docker compose up -d --build

# --- Deploy and rollback (README, "Deploy and maintain") -----------------------------------------
# One image per commit and service, reachy-sage-<svc>:<sha>, built from git archive of the commit's
# folder, never the working tree; then the contract tests inside the new images, then up-tag. If the
# services are not healthy, the tag that ran before comes back. Rollback by hand: make up-tag.
SHA ?= HEAD
SERVICES := stt research searxng
DEPLOY_LOG ?= $(HOME)/Library/Logs/reachy-sage/DEPLOYS.log
LOG_LINE = mkdir -p "$$(dirname "$(DEPLOY_LOG)")" && echo "$$(date -u +%Y-%m-%dT%H:%M:%SZ) $(1)" | tee -a "$(DEPLOY_LOG)"
# Ready from the host too, past the healthchecks --wait already waited for: STT, and research (with SearXNG).
HEALTH = for port in 8781 8783; do curl -sf -m 5 http://127.0.0.1:$$port/health | grep -q '"ready": *true' \
  || { echo "make $@: nothing ready on 127.0.0.1:$$port" >&2; exit 1; }; done

deploy:
	@sha=$$(git rev-parse --short=12 "$(SHA)^{commit}") && prev=$(SAGE_SERVICES_TAG) || exit 1; \
	top=$$(git rev-parse --show-toplevel) && here=$$(git rev-parse --show-prefix) || exit 1; \
	for s in $(SERVICES); do git -C "$$top" archive "$$sha:$$here$$s" | docker build -q -t reachy-sage-$$s:$$sha - > /dev/null \
	  || { echo "make $@: reachy-sage-$$s:$$sha did not build; nothing changed" >&2; exit 1; }; done; \
	docker compose stop stt > /dev/null; \
	for s in stt research; do SAGE_SERVICES_TAG=$$sha docker compose run -T --rm --no-deps \
	  -e SAGE_VOICEPRINT=/nonexistent $$s python -m unittest -q test_app \
	  || { echo "make $@: $$s's tests fail at $$sha: back to $$prev" >&2; \
	  $(MAKE) -s up-tag SAGE_SERVICES_TAG=$$prev; exit 1; }; done; \
	if $(MAKE) -s up-tag SAGE_SERVICES_TAG=$$sha; then $(call LOG_LINE,deploy $$sha services); \
	else echo "make $@: $$sha is not healthy: back to $$prev" >&2; $(MAKE) -s up-tag SAGE_SERVICES_TAG=$$prev; \
	  $(call LOG_LINE,rollback $$sha services to $$prev); exit 1; fi

# Any tag a deploy built, no build and no tests: make up-tag SAGE_SERVICES_TAG=<sha> (shas: DEPLOY_LOG).
up-tag:
	@[ "$(origin SAGE_SERVICES_TAG)" != file ] \
	  || { echo "usage: make up-tag SAGE_SERVICES_TAG=<sha> (grep services $(DEPLOY_LOG))" >&2; exit 1; }
	@for s in $(SERVICES); do docker image inspect reachy-sage-$$s:$(SAGE_SERVICES_TAG) > /dev/null || exit 1; done
	docker compose up -d --no-build --wait $(SERVICES)
	@$(HEALTH)
	@$(call LOG_LINE,up-tag $(SAGE_SERVICES_TAG) services)

# Stops and removes the containers; the voiceprint volume stays (volumes are never removed).
down:
	docker compose down

# The app's lint, imports and secrets cover this folder; this uv.lock has its own audit.
lint imports secrets:
	$(MAKE) -C .. $@

vuln:
	uv audit --locked --preview-features audit

hygiene: lint imports vuln secrets test

# The owner's voiceprint from their recordings (SAGE_VOICE_RECORD_DIR, anyone else's deleted),
# then the STT restarted to hear only them. Prints each recording's similarity: a low one may
# be someone else. The recordings go in as a tar stream and the voiceprint into the
# reachy-sage-voice-profiles volume: nothing is mounted from the host. make unenroll undoes it.
RECORDINGS ?= profiles/recordings
VOICEPRINT := docker compose run -T --rm --no-deps -v reachy-sage-voice-profiles:/out stt
enroll:
	@test -n "$$(ls "$(RECORDINGS)"/*.wav 2>/dev/null)" || { echo "no WAVs in $(RECORDINGS): make enroll RECORDINGS=<folder>"; exit 1; }
	(cd "$(RECORDINGS)" && COPYFILE_DISABLE=1 tar -c *.wav) | $(VOICEPRINT) sh -c \
	  'mkdir /tmp/recordings && tar -x -C /tmp/recordings && python app.py enroll /tmp/recordings /out/owner.npy'
	docker compose restart stt

# The voiceprint deleted, the STT restarted: it hears everyone again.
unenroll:
	$(VOICEPRINT) rm -f /out/owner.npy
	docker compose restart stt

# Song research alone, and one real lookup against it (needs the internet).
up-research: export SAGE_SERVICES_TAG := dev
up-research:
	docker compose up -d --build --wait research searxng

research-smoke:
	curl -sf -X POST http://127.0.0.1:8783/research -H 'content-type: application/json' \
	  -d '{"title": "Blinding Lights", "artist": "The Weeknd"}' | python3 -m json.tool

native-stt:
	PORT=8791 uv run python stt/app.py

native-tts:
	cd tts && PORT=8792 uv run python app.py

# Pocket TTS natively on the TTS port, the Docker TTS stopped; pocket-off undoes it. Measured
# 2026-11-05 with 2 of the VM's 4 CPUs busy: native real-time factor 0.28, no underruns; Colima 0.62,
# gaps up to 325 ms (those located were in a reply's first frames), and 0.8 GB of a 4 GB VM.
# It runs as a LaunchAgent (pocket.plist.in): launchd restarts it after a crash and at login.
COLIMA ?= docker --context colima
POCKET_PORT ?= 8782
POCKET_PID := .local/pocket-$(POCKET_PORT).pid
POCKET_LOG ?= $(HOME)/Library/Logs/reachy-sage/tts-pocket.log
POCKET_LABEL := ai.sage.reachy-pocket
POCKET_PLIST ?= $(HOME)/Library/LaunchAgents/$(POCKET_LABEL).plist
POCKET_AGENT = gui/$$(id -u)/$(POCKET_LABEL)
pocket-plist:
	mkdir -p "$$(dirname "$(POCKET_PLIST)")" "$$(dirname "$(POCKET_LOG)")"
	sed -e "s|@LABEL@|$(POCKET_LABEL)|g" -e "s|@SERVICES@|$(CURDIR)|g" -e "s|@PORT@|$(POCKET_PORT)|g" \
	  -e "s|@LOG@|$(POCKET_LOG)|g" pocket.plist.in > "$(POCKET_PLIST)"

pocket-on: pocket-plist
	uv sync --no-default-groups --group tts
	cd tts && ../.venv/bin/python app.py fetch
	$(COLIMA) stop reachy-sage-voice-tts-1
	-test -f $(POCKET_PID) && kill "$$(cat $(POCKET_PID))" && rm $(POCKET_PID)  # one started before launchd
	while curl -sf -m 2 http://127.0.0.1:$(POCKET_PORT)/health >/dev/null; do sleep 1; done
	-launchctl bootout $(POCKET_AGENT) 2>/dev/null
	launchctl bootstrap gui/$$(id -u) "$(POCKET_PLIST)"
	until curl -sf -m 2 http://127.0.0.1:$(POCKET_PORT)/health | grep -q true; do sleep 1; done

# The plist goes too, or the next login starts azelma again over the Docker TTS's port.
pocket-off:
	-launchctl bootout $(POCKET_AGENT) 2>/dev/null
	rm -f "$(POCKET_PLIST)"
	-test -f $(POCKET_PID) && kill "$$(cat $(POCKET_PID))" && rm $(POCKET_PID)  # one started before launchd
	while curl -sf -m 2 http://127.0.0.1:$(POCKET_PORT)/health >/dev/null; do sleep 1; done
	$(COLIMA) start reachy-sage-voice-tts-1
	until curl -sf -m 2 http://127.0.0.1:$(POCKET_PORT)/health | grep -q true; do sleep 1; done
