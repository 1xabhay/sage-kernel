test:
	uv run pytest -q

# The voice against the real Sage kernel on a free loopback port, with its conformance
# fake model. CPU only. The kernel is SAGE_KERNEL_DIR, default the monorepo root.
integration:
	uv run pytest -q -m integration tests/integration

# The voice on the robot. Reads the environment and .env: see README.md, "Settings".
# On a Mac, caffeinate keeps it awake while it runs: idle sleep drops the robot link. No flag holds
# a closed lid on battery (live 2026-10-28: "Clamshell Sleep"), and -s holds only on AC.
export SAGE_VOICE_BUILD ?= $(shell git rev-parse --short HEAD 2>/dev/null)
run:
	$(if $(shell command -v caffeinate 2>/dev/null),caffeinate -i -s) uv run python -m reachy_sage_voice

# Up or down, and why: the robot's daemon, the kernel, STT, TTS, research and Ollama.
# Exits nonzero if any is down. Stdlib only: plain python3, no app environment needed.
status:
	python3 -I reachy_sage_voice/status.py

# --- Deploy and release (README, "Deploy") ------------------------------------------------------
# On this Mac the voice is a launchd LaunchAgent (KeepAlive) running a `git archive` export of one
# commit, in DEPLOY_ROOT/.deploy/voice/<sha>: the main checkout, whichever worktree deploys. Its
# settings come only from ENV_FILE (`make env` writes it from .env.example; fill in the token).
# Rollback is `make deploy SHA=<previous>`; the last three exports stay.
SHA ?= HEAD
LABEL ?= ai.sage.reachy-voice
LOGS ?= $(HOME)/Library/Logs/reachy-sage
DEPLOY_LOG ?= $(LOGS)/DEPLOYS.log
ENV_FILE ?= $(HOME)/Library/Application Support/reachy_mini/sage_voice/.env
PLIST ?= $(HOME)/Library/LaunchAgents/$(LABEL).plist
DEPLOY_ROOT ?= $(shell cd "$$(git rev-parse --path-format=absolute --git-common-dir)/.." && pwd)
LAUNCH_PATH ?= /opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
# The log line that says the pipeline is running, and how long a deploy waits for it.
STARTED ?= started running
DEPLOY_WAIT_S ?= 240
# One test nodeid allowed to fail at SHA, printed loudly; never set it by default.
DEPLOY_ALLOW_XFAIL ?=
SPACE ?= sabhay22/reachy_sage_voice
HF ?= uvx --from huggingface_hub==0.36.0 hf
export SHA LABEL LOGS DEPLOY_LOG ENV_FILE PLIST DEPLOY_ROOT LAUNCH_PATH STARTED DEPLOY_WAIT_S DEPLOY_ALLOW_XFAIL SPACE HF
.PHONY: deploy undeploy plist logs env release

deploy undeploy:
	sh deploy/deploy.sh $@

# The plist alone, for a look: make plist EXPORT=<app dir> SHORT=<sha> PLIST=<out>.
plist:
	sh deploy/deploy.sh plist "$(EXPORT)" "$(SHORT)" "$(PLIST)"

logs:
	tail -n 100 -F "$$(plutil -extract StandardOutPath raw "$(PLIST)")"

# Never overwrites: an existing .env is the owner's.
env:
	@if [ -e "$(ENV_FILE)" ]; then echo "kept $(ENV_FILE) (never overwritten)"; \
	else (umask 077 && mkdir -p "$$(dirname "$(ENV_FILE)")" && cp .env.example "$(ENV_FILE)" && chmod 600 "$(ENV_FILE)") \
	  && echo "wrote $(ENV_FILE): fill in SAGE_VOICE_KERNEL_TOKEN and REACHY_MINI_HOST"; fi

# HEAD to the Space, from tag voice/v<version>; DRY_RUN=1 lists the files and stops.
release:
	sh deploy/release.sh

# First audio per clause rule, against a running Kokoro (make -C services up first).
# Results go in sage-kernel bench/LEDGER.md, "Voice first audio".
bench-first-audio:
	SAGE_VOICE_TTS_URL=$${SAGE_VOICE_TTS_URL:-http://127.0.0.1:8782} uv run python bench/first_audio.py

# Song naming under simulated room damage, against a running research service (make -C services
# up-research first). Fetches five iTunes previews into bench/.audio (git-ignored); needs ffmpeg.
bench-identify:
	SAGE_VOICE_RESEARCH_URL=$${SAGE_VOICE_RESEARCH_URL:-http://127.0.0.1:8783} uv run python bench/identify_noise.py

# Offline eval (eval/score.py): one scorer per stage, a dated row in eval/LEDGER.md per run.
# CPU only, and no live service unless ARGS names one (--stt-url, --research-url).
# SET is a dataset recorded from eval/script.md; OPTION labels the ledger row.
SET ?= eval/recordings
OPTION ?= default
eval-voice:
	uv run pytest -q tests/test_eval.py

eval-voice-record:
	uv run python eval/record.py $(ARGS)

eval-voice-stt eval-voice-speaker eval-voice-turns:
	uv run python eval/score.py $(@:eval-voice-%=%) $(SET) --option "$(OPTION)" $(ARGS)

# The replayed public set (eval/replay.py, runbook in eval/script.md, "Replay set"): fetch once,
# then one run per speaker placement, the voice stopped. DIST in metres; MUSIC, a song file looped
# under the speech at SNR dB; ARGS="--limit 5" is a level check. Preflight first, every time.
DIST ?= 0.5
SNR ?= 10
REPLAY := uv run --with sounddevice==0.5.3 python eval/replay.py
eval-voice-replay-fetch:
	$(REPLAY) fetch $(ARGS)

eval-voice-replay:
	$(REPLAY) preflight
	$(REPLAY) run --distance $(DIST) $(if $(MUSIC),--music "$(MUSIC)" --snr $(SNR)) $(ARGS)

# v2v_ms p50/p95 per lane (reflex, kernel) from one bot log (LAYERS.md §0); read only.
# LOG is a stamped one, e.g. the newest $(LOGS)/bot-*.log.
latency:
	uv run python -m sage_device.latency "$(LOG)"

# The owner's table for one session, against the targets (sage_device/pet.py); read only, counts only.
# NAME, for the Uses name row, is written|spoken of ENV_FILE's first SAGE_VOICE_NAMES unless given.
NAME ?= $(shell sed -n 's/^SAGE_VOICE_NAMES="*\([^:|,"]*\):\([^:|,"]*\).*/\1|\2/p' "$(ENV_FILE)" 2>/dev/null)
pet:
	@uv run python -m reachy_sage_voice.meter "$(LOG)" $(if $(NAME),--name "$(NAME)")

# SOURCE: saved `make latency` output, or a copy of the kernel's journal (never the live one).
eval-voice-latency:
	uv run python eval/score.py latency $(SOURCE) --option "$(OPTION)" $(ARGS)

eval-voice-song-qa:
	uv run python eval/score.py song-qa --option "$(OPTION)" $(ARGS)

# --- Gates ---------------------------------------------------------------------------------------
# Lint runs in Docker, its image pinned by digest, read-only, no network. The code is streamed in as git's tracked files, never mounted: the Colima VM cannot
# see this folder. TRACKED runs from the repo root, so ruff finds the pyproject.toml this one extends.
RUFF_IMAGE := ghcr.io/astral-sh/ruff:0.17.1-alpine@sha256:b527bd3e15c43b4fb9fc3c0615cd2ed4df019d100459c37b0916dee999111190
TRACKED = (cd ../.. && git ls-files -z pyproject.toml apps/reachy_sage_voice | COPYFILE_DISABLE=1 tar --null -T - -c)
UNPACK = mkdir /tmp/src && cd /tmp/src && tar -x --no-same-owner && cd apps/reachy_sage_voice
# Ruff's findings as "count file code", line numbers dropped so moving code keeps the count.
RUFF_COUNTS = set -o pipefail && ruff check --exit-zero --no-cache --output-format concise . | sed -n "s/:[0-9]*:[0-9]*: \([^ ]*\) .*/ \1/p" | sort | uniq -c
.PHONY: test lint lint-baseline imports vuln secrets up down hygiene preflight backup-journal

# Lint fails on any finding not in lint-baseline.txt: a new file or rule, or one more of an old one.
# ponytail: the baseline keeps the findings from before this gate; fixing some never fails, so run
# `make lint-baseline` after a cleanup to ratchet it down. Delete it once it is empty.
lint:
	$(TRACKED) | docker run --rm -i --network none --read-only --tmpfs /tmp $(RUFF_IMAGE) sh -c '$(UNPACK) \
	  && { $(RUFF_COUNTS); } > /tmp/now && awk '\''NR == FNR { base[$$2 " " $$3] = $$1; next } \
	  $$1 > base[$$2 " " $$3] + 0 { print "new lint: " $$2 " " $$3 " (" $$1 ", baseline " base[$$2 " " $$3] + 0 ")"; bad = 1 } \
	  END { if (bad) print "uv run ruff check <file> shows where"; else print "lint: nothing new"; exit bad }'\'' lint-baseline.txt /tmp/now'

lint-baseline:
	$(TRACKED) | docker run --rm -i --network none --read-only --tmpfs /tmp $(RUFF_IMAGE) sh -c '$(UNPACK) && $(RUFF_COUNTS)' > lint-baseline.txt

# Import order and unused imports, here and in services/, as the devices workspace checks them.
imports:
	uv run ruff check --select I,F401 .

# Known vulnerabilities in this uv.lock (uv audit, from the advisory databases): needs the network.
# The advisories in audit-ignore.txt have no fix inside a pin we can't move; each says why.
AUDIT_IGNORE = $(addprefix --ignore ,$(shell sed 's/\#.*//' audit-ignore.txt))
vuln:
	uv audit --locked --preview-features audit $(AUDIT_IGNORE)
	$(MAKE) -C services vuln

# Secrets in the whole repository and its history: the root's scan (gitleaks, offline).
ROOT := $(shell git rev-parse --show-toplevel)
secrets:
	$(MAKE) -C $(ROOT) secrets

# STT, TTS and research in Docker; down keeps the volumes (the voiceprint).
up down:
	$(MAKE) -C services $@

# The tests run on the host (`make test`): on Linux, reachy-mini's pygobject builds from source and
# needs gobject-introspection and cairo, so tests in Docker wait for an image of their own.
hygiene: lint imports vuln secrets test

# Before anything live: status, robot check, Docker context, a fresh journal backup, one model.
SAGE_KERNEL_BACKUPS ?= $(HOME)/sage-kernel/.backups
KERNEL_CONTAINER ?= sage-kernel-kernel-1
export SAGE_KERNEL_BACKUPS
preflight:
	python3 -E -s -m reachy_sage_voice.preflight

# A copy of the kernel's journal, with its -wal and -shm, into SAGE_KERNEL_BACKUPS, mode 600. docker
# cp reads the live files and never writes them. Touched after, so its age is the copy's.
# ponytail: a copy taken mid-checkpoint can tear (as in sage-kernel's `make latency`); take another.
backup-journal:
	@umask 077 && mkdir -p "$(SAGE_KERNEL_BACKUPS)" && copy="$(SAGE_KERNEL_BACKUPS)/journal-$$(date +%Y%m%d-%H%M%S).sqlite3" \
	  && docker cp -q $(KERNEL_CONTAINER):/data/kernel.sqlite3 "$$copy" \
	  && for end in -wal -shm; do docker cp -q $(KERNEL_CONTAINER):/data/kernel.sqlite3$$end "$$copy$$end" 2>/dev/null || true; done \
	  && chmod 600 "$$copy"* && touch "$$copy"* && echo "$$copy"
