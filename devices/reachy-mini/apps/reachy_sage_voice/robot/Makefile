# The robot's own setup, kept here and installed over SSH (key login, pollen has sudo).
# ROBOT=user@host in a local robot/.env (git-ignored), else the robot's factory name.
-include .env
ROBOT ?= pollen@reachy-mini.local
SSH := ssh -o BatchMode=yes -o ConnectTimeout=5 $(ROBOT)
HOST := $(lastword $(subst @, ,$(ROBOT)))
CHIP := python3 -I ../reachy_sage_voice/chip.py
GAIN ?= 1.0
DROP_IN := /etc/systemd/system/reachy-mini-daemon.service.d/50-sage-no-relay.conf
# The guard's addresses, on the robot only (sage-guard.nft includes it; allow-this-mac writes it).
HOSTS := /etc/sage-guard/hosts.nft
.PHONY: test install check allow-this-mac guard health-tail media-reset beam-on beam-off beam-check relay-off relay-on relay-check

test:
	python3 -m unittest test_sage_health test_sage_guard

# Persistent journal, the health logger, then the guard (with a rollback, see guard).
install: test
	scp -q 50-sage-persistent.conf sage_health.py sage-health.service sage-guard.nft $(ROBOT):/tmp/
	$(SSH) 'set -e; sudo install -D -m 0644 /tmp/50-sage-persistent.conf /etc/systemd/journald.conf.d/50-sage-persistent.conf; \
	  sudo systemctl restart systemd-journald; \
	  sudo install -D -m 0644 /tmp/sage_health.py /usr/local/lib/sage/sage_health.py; \
	  sudo install -m 0644 /tmp/sage-health.service /etc/systemd/system/sage-health.service; \
	  sudo systemctl daemon-reload; sudo systemctl enable --now sage-health.service'
	$(MAKE) guard

# This Mac's en0 address, hardware address and LAN (hosts.py) into the robot's HOSTS, then the
# guard reloaded under its rollback, which also puts the old HOSTS back.
allow-this-mac:
	@hosts=$$(python3 -I hosts.py "$$(ipconfig getifaddr en0)" "$$(ifconfig en0 ether | awk '/ether/ {print $$2}')" \
	  "$$(ipconfig getoption en0 subnet_mask)") && printf '%s\n' "$$hosts" \
	  && printf '%s\n' "$$hosts" | $(SSH) 'set -e; cat > /tmp/sage-hosts.nft; sudo install -d -m 0755 $(dir $(HOSTS)); \
	  if test -f $(HOSTS); then sudo cp -p $(HOSTS) $(HOSTS).prev; fi; sudo install -m 0644 /tmp/sage-hosts.nft $(HOSTS)'
	$(MAKE) guard

# Load the guard with a 90 s rollback armed on the robot (the table goes, an old HOSTS comes
# back); keep it (and load it at boot) only if a fresh SSH login and the daemon still answer.
guard:
	scp -q sage-guard.nft $(ROBOT):/tmp/
	$(SSH) 'set -e; test -f $(HOSTS) || { echo "no $(HOSTS) on the robot: make allow-this-mac"; exit 1; }; \
	  sudo nft -c -f /tmp/sage-guard.nft; \
	  sudo systemd-run --quiet --unit=sage-guard-rollback --on-active=90 /bin/sh -c \
	    "nft delete table inet sage_guard; test ! -f $(HOSTS).prev || mv $(HOSTS).prev $(HOSTS)"; \
	  sudo nft -f /tmp/sage-guard.nft'
	$(SSH) true && curl -sf -m5 http://$(HOST):8000/api/daemon/status >/dev/null
	$(SSH) 'set -e; sudo systemctl stop sage-guard-rollback.timer; sudo rm -f $(HOSTS).prev; \
	  sudo install -m 0644 /tmp/sage-guard.nft /etc/nftables.d-sage-guard.nft; \
	  grep -q nftables.d-sage-guard /etc/nftables.conf || echo "include \"/etc/nftables.d-sage-guard.nft\"" | sudo tee -a /etc/nftables.conf >/dev/null; \
	  sudo nft -c -f /etc/nftables.conf; sudo systemctl enable nftables.service'
	@echo "guard kept and loaded at boot"

# What is installed and running, as it will be after a reboot.
check:
	$(SSH) 'set -e; journalctl --header | grep -q "/var/log/journal/" && echo "journal: persistent"; \
	  systemctl is-active sage-health.service; systemctl is-enabled sage-health.service; \
	  journalctl -u sage-health -n 1 --no-pager -o cat; \
	  sudo nft list table inet sage_guard | grep -q "counter.*drop" && echo "guard: loaded"; \
	  sudo nft list table inet sage_guard | grep -q "meta l4proto udp counter.*drop" && echo "guard: udp"; \
	  systemctl is-enabled nftables.service; grep -q nftables.d-sage-guard /etc/nftables.conf && echo "guard: at boot"'
	$(MAKE) --no-print-directory relay-check beam-check

health-tail:
	$(SSH) 'journalctl -u sage-health -f -o short-iso --no-pager'

# The daemon's media rebuilt from scratch, for a mic left dead by a USB drop (WebRTC connects,
# no audio): release, wait, acquire. Drops the camera and WebRTC briefly; a running voice resets
# again on its own (media_reset.py). Works only once the audio device is back on the robot.
media-reset:
	curl -sf -m10 -X POST http://$(HOST):8000/api/media/release && echo
	sleep 3
	curl -sf -m30 -X POST http://$(HOST):8000/api/media/acquire && echo
	curl -sf -m5 http://$(HOST):8000/api/media/status && echo

# The mic chip's ASR beam on the right channel, conference output kept on the left (chip.py).
# beam-on keeps what it read in .beam-before.json; beam-off writes that back. Nothing is saved on
# the chip, so a power cycle undoes it too (then beam-check fails, and so does make status).
beam-on:
	$(CHIP) on --robot $(ROBOT) --gain $(GAIN)

beam-off:
	$(CHIP) off --robot $(ROBOT)

beam-check:
	$(CHIP) check --robot $(ROBOT)

# Pollen's central relay off: the daemon starts it whenever an HF token is stored and has no
# switch, so 50-sage-no-relay.conf points it at a closed local port. Restarts the daemon (put the
# robot to sleep first); if the daemon does not answer within 60 s, the drop-in comes out again.
relay-off:
	scp -q 50-sage-no-relay.conf $(ROBOT):/tmp/
	$(SSH) 'set -e; sudo install -D -m 0644 /tmp/50-sage-no-relay.conf $(DROP_IN); \
	  sudo systemctl daemon-reload; sudo systemctl restart reachy-mini-daemon.service'
	@for i in $$(seq 30); do curl -sf -m2 http://$(HOST):8000/api/daemon/status >/dev/null && exit 0; sleep 2; done; \
	  echo "daemon silent after 60 s: taking the drop-in out"; $(MAKE) relay-on; exit 1
	$(MAKE) relay-check

relay-on:
	$(SSH) 'set -e; sudo rm -f $(DROP_IN); sudo systemctl daemon-reload; sudo systemctl restart reachy-mini-daemon.service'
	@echo "relay drop-in removed, daemon restarted"

# Off means: the drop-in is installed, the running daemon has it, and the relay is not connected.
relay-check:
	@state=$$(curl -sf -m5 http://$(HOST):8000/api/hf-auth/relay-status); echo "relay: $$state"; \
	  echo "$$state" | grep -q '"is_connected":true' && { echo "relay: still connected"; exit 1; } || true
	$(SSH) 'test -f $(DROP_IN) && tr "\0" "\n" < /proc/$$(pgrep -f -o reachy_mini.daemon.app.main)/environ | grep -qx REACHY_CENTRAL_URL=http://127.0.0.1:9' \
	  && echo "relay: drop-in installed and loaded"
