.PHONY: test lint imports vuln secrets up down hygiene

# Native runs keep the Hugging Face cache inside this folder.
export HF_HOME := $(CURDIR)/.local/hf

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

# The contract tests again, inside the sealed images.
test-docker:
	docker compose build
	docker compose run --rm --no-deps stt python -m unittest test_app
	docker compose run --rm --no-deps tts python -m unittest test_app
	docker compose run --rm --no-deps research python -m unittest test_app

up:
	docker compose up -d --build

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
	cd "$(RECORDINGS)" && COPYFILE_DISABLE=1 tar -c *.wav | $(VOICEPRINT) sh -c \
	  'mkdir /tmp/recordings && tar -x -C /tmp/recordings && python app.py enroll /tmp/recordings /out/owner.npy'
	docker compose restart stt

# The voiceprint deleted, the STT restarted: it hears everyone again.
unenroll:
	$(VOICEPRINT) rm -f /out/owner.npy
	docker compose restart stt

# Song research alone, and one real lookup against it (needs the internet).
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
# ponytail: nothing restarts it if it dies (the voice shows on_down); launchd if that ever happens.
COLIMA ?= docker --context colima
POCKET_PORT ?= 8782
POCKET_PID := .local/pocket-$(POCKET_PORT).pid
POCKET_LOG ?= $(HOME)/Library/Logs/reachy-sage/tts-pocket.log
pocket-on:
	uv sync --no-default-groups --group tts
	cd tts && ../.venv/bin/python app.py fetch
	$(COLIMA) stop reachy-sage-voice-tts-1
	(cd tts && HF_HUB_OFFLINE=1 PORT=$(POCKET_PORT) exec nohup ../.venv/bin/python app.py >>"$(POCKET_LOG)" 2>&1) & echo $$! > $(POCKET_PID)
	until curl -sf -m 2 http://127.0.0.1:$(POCKET_PORT)/health | grep -q true; do sleep 1; done

pocket-off:
	-kill "$$(cat $(POCKET_PID))" && rm $(POCKET_PID)
	while curl -sf -m 2 http://127.0.0.1:$(POCKET_PORT)/health >/dev/null; do sleep 1; done
	$(COLIMA) start reachy-sage-voice-tts-1
	until curl -sf -m 2 http://127.0.0.1:$(POCKET_PORT)/health | grep -q true; do sleep 1; done
