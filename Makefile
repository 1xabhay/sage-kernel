PYTHON ?= python3
GITLEAKS_IMAGE := ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f
RUFF_IMAGE := ghcr.io/astral-sh/ruff:0.17.1-alpine@sha256:b527bd3e15c43b4fb9fc3c0615cd2ed4df019d100459c37b0916dee999111190
SOURCES := sage tests conformance evals make pyproject.toml
# A linked worktree (a parallel lane) is not the main checkout: it tests in its own project and image,
# named after its folder, and never touches the live kernel.
LINKED := $(shell [ "$$(git rev-parse --path-format=absolute --git-dir 2>/dev/null)" = \
  "$$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" ] || echo yes)
LANE := $(if $(LINKED),$(notdir $(CURDIR)),main)
MAIN_ONLY = @[ -z "$(LINKED)" ] || { echo "make $@: the live kernel runs only from the main checkout, not lane $(LANE)." >&2; exit 1; }
# One image per commit: sage-kernel:<sha>, built from git archive of a clean HEAD. make test runs it, make deploy
# runs that same image live. A dirty tree (the commit gate, work in progress) tests sage-kernel:dev-<lane>.
SHA := $(shell git rev-parse --short=12 HEAD 2>/dev/null)
DIRTY := $(shell git status --porcelain --untracked-files=no 2>/dev/null)
TEST_TAG := $(if $(DIRTY),dev-$(LANE),$(SHA))
# Tests use their own compose project, never the live one: see compose.test.yaml.
TEST := SAGE_TAG=$(TEST_TAG) docker compose -p sage-kernel-test-$(LANE) -f compose.test.yaml
LIVE := sage-kernel-kernel-1
DEPLOY_LOG ?= $(HOME)/Library/Logs/reachy-sage/DEPLOYS.log
# Every project in the monorepo. music-telling moves from apps/ to experiments/ in PR #6: either is found.
PROJECTS := . devices/reachy-mini experiments/sage-os $(wildcard apps/music-telling experiments/music-telling)
.PHONY: help hooks deploy up up-tag down image dev-image test-image backup run smoke release test check unit \
  conformance eval source imports docs lint vuln secrets hygiene test-all hygiene-all conventions

help:
	@echo "make test        unit + conformance inside the commit's image (dirty tree: dev-<lane> image)"
	@echo "make hygiene     what CI runs: source docs lint vuln secrets test"
	@echo "make run         a throwaway kernel on a free port, temp state, fake model (never 8776, never the journal)"
	@echo "make smoke       run + the README's curl walkthrough, then stop"
	@echo "make backup      read-only snapshot of the live journal into .backups/ (mode 600, integrity-checked)"
	@echo "make deploy      main checkout only: image, test, backup, up that image, check mounts + hello, log it"
	@echo "                 (make up is make deploy)"
	@echo "rollback         SAGE_TAG=<previous sha> make up-tag   (shas: tail \$$HOME/Library/Logs/reachy-sage/DEPLOYS.log)"
	@echo "make release     check tag kernel/vX.Y.Z at HEAD = pyproject version = CHANGELOG; never pushes"
	@echo "make test-all    test in every project; hygiene-all likewise; conventions reports gaps (STRICT=1 fails)"

# Tracked git hooks: the push guard and the red/green commit gate (.githooks/).
hooks:
	git config core.hooksPath .githooks

# The image of HEAD, never of the working tree: refuses uncommitted changes. Untracked files are not in it.
image:
	@[ -z "$$(git status --porcelain --untracked-files=no)" ] \
	  || { echo "make $@: commit or stash first; an image is built only from a commit." >&2; exit 1; }
	git archive HEAD | docker build -t sage-kernel:$(SHA) -

dev-image:
	docker build -q -t sage-kernel:dev-$(LANE) .

test-image: $(if $(DIRTY),dev-image,image)

# The live journal: the volume sage-kernel_kernel-data at /data, and a kernel answering on 127.0.0.1:8776
# (hello with no token: 401, so no device session is touched).
CHECK_LIVE = @docker inspect -f '{{range .Mounts}}{{.Name}}->{{.Destination}} {{end}}' $(LIVE) \
  | grep -q 'sage-kernel_kernel-data->/data ' || { echo "make $@: $(LIVE) does not mount sage-kernel_kernel-data \
  at /data: the journal is not live. Roll back: SAGE_TAG=<previous sha> make up-tag" >&2; exit 1; }; \
  code=$$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8776/v1/hello); [ "$$code" = 401 ] \
  || { echo "make $@: no kernel answers hello on 127.0.0.1:8776 (HTTP $$code)" >&2; exit 1; }; \
  echo "live: $$(docker inspect -f '{{.Config.Image}}' $(LIVE)), sage-kernel_kernel-data -> /data, hello answers"

# The kernel on 127.0.0.1:8776 (the old kernel keeps 8765/8766). Needs SAGE_DEVICES in the environment or .env.
deploy:
	$(MAIN_ONLY)
	@mkdir -p .deploy && set -o pipefail && $(MAKE) image test backup 2>&1 | tee .deploy/$(SHA).log
	SAGE_TAG=$(SHA) docker compose up -d --wait kernel
	$(CHECK_LIVE)
	@mkdir -p $(dir $(DEPLOY_LOG)) && n=$$(awk '/^Ran [0-9]+ tests? in/ {s += $$2} END {print s + 0}' .deploy/$(SHA).log) \
	  && echo "$$(date -u +%Y-%m-%dT%H:%M:%SZ) deploy $(SHA) kernel tests $$n pass" | tee -a $(DEPLOY_LOG)

up: deploy

# Rollback, or any image make image built before: SAGE_TAG=<sha> make up-tag. No build, no tests.
up-tag:
	$(MAIN_ONLY)
	@[ -n "$(SAGE_TAG)" ] || { echo "usage: SAGE_TAG=<sha> make up-tag (images: docker images sage-kernel)" >&2; exit 1; }
	docker image inspect sage-kernel:$(SAGE_TAG) > /dev/null
	SAGE_TAG=$(SAGE_TAG) docker compose up -d --wait kernel
	$(CHECK_LIVE)
	@mkdir -p $(dir $(DEPLOY_LOG)) && echo "$$(date -u +%Y-%m-%dT%H:%M:%SZ) up-tag $(SAGE_TAG) kernel" | tee -a $(DEPLOY_LOG)

# Stops the kernel; the journal stays in its volume. The tag is not needed to stop it.
down:
	$(MAIN_ONLY)
	SAGE_TAG=none docker compose down

# A consistent, read-only copy of the live journal (sqlite's serialize over a mode=ro connection, inside the
# running container), mode 600, integrity-checked. Safe at any time: it never writes to the container.
backup:
	@umask 077 && mkdir -p .backups && f=.backups/journal-$$(date -u +%Y%m%dT%H%M%SZ).sqlite3 \
	  && docker exec $(LIVE) python3 -c "import sqlite3, sys; sys.stdout.buffer.write(sqlite3.connect(\
	  'file:/data/kernel.sqlite3?mode=ro', uri=True).serialize())" > $$f && [ -s $$f ] \
	  && [ "$$($(PYTHON) -c "import sqlite3, sys; print(sqlite3.connect(f'file:{sys.argv[1]}?immutable=1', \
	  uri=True).execute('pragma integrity_check').fetchone()[0])" $$f)" = ok ] && echo "backup: $$f" \
	  || { echo "make $@: no good copy of the live journal" >&2; rm -f $$f; exit 1; }

# A throwaway native kernel: free port, temp state, the conformance suite's fake model.
run:
	$(PYTHON) make/run.py run

smoke:
	$(PYTHON) make/run.py smoke

# Checks only; tagging and pushing stay with the owner.
release:
	$(PYTHON) make/release_check.py

# Unit tests, then the conformance suite, both in Docker on Python 3.12. `check` is the old name.
test: unit conformance

check: test

unit: test-image
	$(TEST) run --rm conformance python -m unittest discover -s tests -v

# Device protocol v1, black-box over HTTP, in Docker on Python 3.12 (compose.test.yaml)
# (its `conformance` service runs this suite). Setting SAGE_KERNEL_CMD runs on the host instead,
# against that kernel, for fast iteration.
conformance: $(if $(SAGE_KERNEL_CMD),,test-image)
ifneq ($(and $(wildcard compose.test.yaml),$(if $(SAGE_KERNEL_CMD),,yes)),)
	$(TEST) run --rm conformance python -m unittest discover -s conformance -v
else
	$(PYTHON) -m unittest discover -s conformance -v
endif

# Opt-in: live robot conversations, replayed through the kernel against real Ollama on the host.
# SAGE_MODEL=phi4-mini make eval picks the model (default qwen3:1.7b).
eval: test-image
	$(TEST) run --rm -e SAGE_EVAL_MODEL_URL=http://host.docker.internal:11434 -e SAGE_MODEL conformance \
	  python -m unittest discover -s evals -v

# Source-tree checks on the host (sealed containers, ignore files, hooks, no machine names, the core's
# imports); they need the checkout, so they skip inside the image.
source:
	$(PYTHON) -m unittest tests.test_deploy tests.test_boundary $(if $(wildcard tests/test_docs.py),tests.test_docs)

# The core imports only the standard library and its own modules.
imports:
	$(PYTHON) -m unittest tests.test_boundary

# The docs' commands and links are real. make/docs.py arrives with PR #9 (repo/docs): skipped until then.
docs:
ifneq ($(wildcard make/docs.py),)
	$(PYTHON) make/docs.py --check
else
	@echo "warning: make/docs.py is not here yet (PR #9): make docs skipped" >&2
endif

# The code is streamed in, never mounted: the Colima VM cannot see this folder.
lint:
	COPYFILE_DISABLE=1 tar -c $(SOURCES) | docker run --rm -i --network none --read-only --tmpfs /tmp $(RUFF_IMAGE) \
	  sh -c 'mkdir /tmp/src && cd /tmp/src && tar -x && ruff check --no-cache .'

# Nothing to audit while the kernel has no third-party dependencies; this fails the day one is added.
vuln:
	@grep -q '^dependencies = \[\]$$' pyproject.toml && echo "No third-party dependencies: nothing to audit." \
	  || { echo "pyproject.toml declares dependencies: lock them with hashes and audit them here." >&2; exit 1; }

# Secrets anywhere in the git history (gitleaks), offline. The history goes in as a bundle of every ref, never
# mounted, so this works in a linked worktree too (where .git is only a pointer file).
secrets:
	git bundle create --quiet - --all | docker run --rm -i --network none --read-only --tmpfs /tmp:size=256m \
	  -e HOME=/tmp --entrypoint sh $(GITLEAKS_IMAGE) -c 'cat > /tmp/all.bundle && git init -q /tmp/repo \
	  && cd /tmp/repo && git fetch -q /tmp/all.bundle "+refs/*:refs/*" && rm /tmp/all.bundle \
	  && gitleaks git --redact --no-banner .'

# What CI runs.
hygiene: source docs lint vuln secrets test

# The same target in every project; a project with no Makefile yet is skipped, loudly.
FAN_OUT = failed=; for p in $(PROJECTS); do [ -f $$p/Makefile ] \
  || { echo "warning: $$p has no Makefile yet: skipped" >&2; continue; }; \
  $(MAKE) -C $$p $(1) || failed="$$failed $$p"; done; [ -z "$$failed" ] || { echo "make $@ failed in:$$failed" >&2; exit 1; }

test-all:
	@$(call FAN_OUT,test)

hygiene-all:
	@$(call FAN_OUT,hygiene)

# ponytail: report-only while the per-project PRs are open (devices Makefile, #6 experiments, imports/vuln
# targets); flip to strict (drop the STRICT switch) once they have all merged.
conventions:
	$(PYTHON) make/conventions.py $(if $(STRICT),--strict) $(PROJECTS)
