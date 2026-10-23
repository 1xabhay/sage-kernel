# Pollen's MuJoCo sim, headless, on localhost only. Never the robot.
PORT ?= 8742
SCENE ?= minimal
include ../../audit.mk
.PHONY: sim test limits lint imports vuln secrets up down hygiene

sim:
	uv run python sim.py $(PORT) $(SCENE)

test:
	uv run pytest -q

# The limits table, as in README.md.
limits:
	uv run pytest -q -s tests/test_limits.py

lint:
	uv run ruff check . && uv run ruff format --check .

imports:
	uv run ruff check --select I,F401 .

vuln:
	$(AUDIT)

# The whole repository's history, once, at the root.
secrets:
	$(MAKE) -C ../../../.. secrets

# The sim in the foreground: Ctrl-C stops it, so there is nothing for down to stop.
up: sim

down:
	@echo "no service left running: make up runs the sim in the foreground"

hygiene: lint imports vuln secrets test
