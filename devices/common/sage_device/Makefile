# Body-agnostic device code (sage_device), stdlib only. Bodies take it as a uv path dependency.
.PHONY: test lint imports vuln secrets hygiene

test:
	uv run pytest -q

lint:
	uv run ruff check .

# Import order and unused imports.
imports:
	uv run ruff check --select I,F401 .

# Known vulnerabilities in this uv.lock (the dev tools only): needs the network.
vuln:
	uv audit --locked --preview-features audit

# Secrets in the whole repository and its history: the root's scan (gitleaks, offline).
ROOT := $(shell git rev-parse --show-toplevel)
secrets:
	$(MAKE) -C $(ROOT) secrets

hygiene: lint imports vuln secrets test
