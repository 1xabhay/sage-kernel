# The Reachy Mini device: the uv workspace here (the testbench, with the tests in tests/), and a
# fan-out over the apps that are projects of their own. Robot-free, GPU-free.
APPS := apps/reachy_sim apps/reachy_sage_voice
include audit.mk
.PHONY: test lint imports vuln secrets up down hygiene test-all hygiene-all

test:
	uv run pytest -q

# The testbench and the tests; the apps with their own project lint themselves.
lint:
	uv run ruff check apps/reachy_mini_testbench tests

imports:
	uv run ruff check --select I,F401 apps/reachy_mini_testbench tests

vuln:
	$(AUDIT)

# The whole repository's history, once, at the root.
secrets:
	$(MAKE) -C ../.. secrets

# The testbench is a robot app, started by the robot's daemon, never from here.
up down:
	@echo "no service: the testbench runs on the robot (make -C apps/reachy_sim up for a simulator)"

hygiene: lint imports vuln secrets test

# Every test here that needs no speech model: each app's, the voice against the real kernel, and the
# robot-side files. The STT and TTS contract tests load theirs: make -C $(VOICE)/services test.
VOICE := apps/reachy_sage_voice
test-all: test
	for app in $(APPS); do $(MAKE) -C $$app test || exit 1; done
	$(MAKE) -C $(VOICE) integration
	$(MAKE) -C $(VOICE)/robot test
	$(MAKE) -C $(VOICE)/services test-research

hygiene-all: hygiene
	for app in $(APPS); do $(MAKE) -C $$app hygiene || exit 1; done
