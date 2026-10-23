PYTHON ?= python3
GITLEAKS_IMAGE := ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f
RUFF_IMAGE := ghcr.io/astral-sh/ruff:0.17.1-alpine@sha256:b527bd3e15c43b4fb9fc3c0615cd2ed4df019d100459c37b0916dee999111190
SOURCES := sage tests conformance evals pyproject.toml
# A linked worktree (a parallel lane) is not the main checkout: it tests in its own project and image,
# named after its folder, and never touches the live kernel.
LINKED := $(shell [ "$$(git rev-parse --path-format=absolute --git-dir 2>/dev/null)" = \
  "$$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" ] || echo yes)
LANE := $(if $(LINKED),$(notdir $(CURDIR)),main)
MAIN_ONLY = @[ -z "$(LINKED)" ] || { echo "make $@: the live kernel runs only from the main checkout, not lane $(LANE)." >&2; exit 1; }
# Tests use their own compose project, never the live one: see compose.test.yaml.
TEST := LANE=$(LANE) docker compose -p sage-kernel-test-$(LANE) -f compose.test.yaml
.PHONY: hooks up down test check unit conformance eval source lint vuln secrets hygiene

# Tracked git hooks: the push guard and the red/green commit gate (.githooks/).
hooks:
	git config core.hooksPath .githooks

# The kernel on 127.0.0.1:8776 (the old kernel keeps 8765/8766). Needs SAGE_DEVICES in the environment or .env.
up:
	$(MAIN_ONLY)
	docker compose up -d --build --wait kernel

down:
	$(MAIN_ONLY)
	docker compose down

# Unit tests, then the conformance suite, both in Docker on Python 3.12. `check` is the old name.
test: unit conformance

check: test

unit:
	$(TEST) run --build --rm conformance python -m unittest discover -s tests -v

# Device protocol v1, black-box over HTTP, in Docker on Python 3.12 (compose.test.yaml)
# (its `conformance` service runs this suite). Setting SAGE_KERNEL_CMD runs on the host instead,
# against that kernel, for fast iteration.
conformance:
ifneq ($(and $(wildcard compose.test.yaml),$(if $(SAGE_KERNEL_CMD),,yes)),)
	$(TEST) run --build --rm conformance python -m unittest discover -s conformance -v
else
	$(PYTHON) -m unittest discover -s conformance -v
endif

# Opt-in: live robot conversations, replayed through the kernel against real Ollama on the host.
# SAGE_MODEL=phi4-mini make eval picks the model (default qwen3:1.7b).
eval:
	$(TEST) run --build --rm -e SAGE_EVAL_MODEL_URL=http://host.docker.internal:11434 -e SAGE_MODEL conformance \
	  python -m unittest discover -s evals -v

# Source-tree checks on the host (sealed containers, ignore files, hooks, no machine names, the core's
# imports); they need the checkout, so they skip inside the image.
source:
	$(PYTHON) -m unittest tests.test_deploy tests.test_boundary

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
hygiene: source lint vuln secrets test
