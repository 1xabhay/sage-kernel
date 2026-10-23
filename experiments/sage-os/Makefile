RUST_IMAGE := rust:1.99.1-slim-bookworm@sha256:a24e292b4af7bdee351a31d3a6379972a5652f0ea22a1b6d6883324ac4f5097c
SOURCES := Cargo.toml Cargo.lock core sage wasm vectors routines evals
# One build cache per worktree, so parallel builds never wait on each other.
TARGET_VOLUME ?= sage-os-target-$(notdir $(CURDIR))
.PHONY: test lock up down wasm routines eval

# The kernel on 127.0.0.1:8786. Needs SAGE_DEVICES (and SAGE_OWNERS) in the environment or .env.
up:
	docker compose up -d --build kernel

# The routine runner beside it; needs routines=<token> in SAGE_DEVICES and SAGE_ROUTINES_TOKEN.
routines:
	docker compose up -d --build routines

down:
	docker compose down

# The code is streamed in, never mounted: the Colima VM cannot see this folder.
# Named volumes keep downloaded crates and build output between runs.
RUST = COPYFILE_DISABLE=1 tar --no-xattrs -c $(wildcard $(SOURCES)) | docker run --rm -i \
	-v sage-os-cargo:/usr/local/cargo/registry -v $(TARGET_VOLUME):/target -e CARGO_TARGET_DIR=/target \
	$(RUST_IMAGE) sh -c 'mkdir /src && cd /src && tar -x && $(1)'

# Unit tests and the golden vectors.
test:
	$(call RUST,cargo test --locked)

# Live evals against a real model (evals/local/cases.json): a table, and an entry appended to evals/local/LEDGER.md.
# SAGE_MODEL=qwen2.5:7b make eval compares a model; SAGE_EVAL_CASE=recall runs one case and records nothing.
SAGE_EVAL_MODEL_URL ?= http://host.docker.internal:11434
SAGE_MODEL ?= qwen3:1.7b
eval:
	$(call RUST,SAGE_EVAL_MODEL_URL=$(SAGE_EVAL_MODEL_URL) SAGE_MODEL=$(SAGE_MODEL) SAGE_EVAL_CASE=$(SAGE_EVAL_CASE) \
	  SAGE_EVAL_DATE=$(shell date +%F) SAGE_EVAL_COMMIT=$(shell git describe --always --dirty) \
	  cargo test --locked -q -p sage --test eval -- --ignored --nocapture live) | tee evals/.last-run.txt
	@[ -n "$(SAGE_EVAL_CASE)" ] || { echo; sed -n 's/^ledger: //p' evals/.last-run.txt; } >> evals/local/LEDGER.md
	@! grep -q '^ledger: - Failed' evals/.last-run.txt

# sage-core for the browser, and its size against Quila's 74 KB gzip budget.
wasm:
	$(call RUST,rustup target add wasm32-unknown-unknown >/dev/null 2>&1 && \
	  cargo build --locked -q -p sage-wasm --target wasm32-unknown-unknown --profile wasm && \
	  f=/target/wasm32-unknown-unknown/wasm/sage_wasm.wasm && \
	  echo "sage_wasm.wasm: $$(wc -c < $$f) bytes / $$(gzip -9 -c $$f | wc -c) gzipped")

# Write Cargo.lock after changing dependencies.
lock:
	$(call RUST,cargo generate-lockfile -q && cat Cargo.lock) > Cargo.lock.new && mv Cargo.lock.new Cargo.lock
