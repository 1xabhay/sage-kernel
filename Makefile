PYTHON ?= python3
GITLEAKS_IMAGE := ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f
RUFF_IMAGE := ghcr.io/astral-sh/ruff:0.17.1-alpine@sha256:b527bd3e15c43b4fb9fc3c0615cd2ed4df019d100459c37b0916dee999111190
SOURCES := sage tests conformance evals pyproject.toml
# Tests use their own compose project, never the live one: see compose.test.yaml.
TEST := docker compose -f compose.test.yaml
.PHONY: hooks up down check unit conformance eval lint vuln secrets hygiene

# Tracked git hooks: the push guard and the red/green commit gate (.githooks/).
hooks:
	git config core.hooksPath .githooks

# The kernel on 127.0.0.1:8776 (the old kernel keeps 8765/8766). Needs SAGE_DEVICES in the environment or .env.
up:
	docker compose up -d --build --wait kernel

down:
	docker compose down

# Unit tests, then the conformance suite, both in Docker on Python 3.12.
check: unit conformance

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

# The code is streamed in, never mounted: the Colima VM cannot see this folder.
lint:
	COPYFILE_DISABLE=1 tar -c $(SOURCES) | docker run --rm -i --network none --read-only --tmpfs /tmp $(RUFF_IMAGE) \
	  sh -c 'mkdir /tmp/src && cd /tmp/src && tar -x && ruff check --no-cache .'

# Nothing to audit while the kernel has no third-party dependencies; this fails the day one is added.
vuln:
	@grep -q '^dependencies = \[\]$$' pyproject.toml && echo "No third-party dependencies: nothing to audit." \
	  || { echo "pyproject.toml declares dependencies: lock them with hashes and audit them here." >&2; exit 1; }

# Secrets anywhere in the git history (gitleaks), offline; .git is streamed in, never mounted.
secrets:
	COPYFILE_DISABLE=1 tar -c .git | docker run --rm -i --network none --read-only --tmpfs /tmp:size=256m -e HOME=/tmp \
	  --entrypoint sh $(GITLEAKS_IMAGE) -c 'mkdir /tmp/repo && cd /tmp/repo && tar -x --no-same-owner \
	  && git config --global --add safe.directory /tmp/repo && gitleaks git --redact --no-banner .'

hygiene: lint vuln secrets check
