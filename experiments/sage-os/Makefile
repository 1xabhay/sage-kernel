RUST_IMAGE := rust:1.99.1-slim-bookworm@sha256:a24e292b4af7bdee351a31d3a6379972a5652f0ea22a1b6d6883324ac4f5097c
SOURCES := Cargo.toml Cargo.lock core sage wasm vectors routines
# Only `make eval` streams evals/ (the owner's live speech in evals/local); tests never do.
EVAL_SOURCES := $(SOURCES) evals
RUST_FILES = find core sage wasm routines -name "*.rs"
OSV_IMAGE := ghcr.io/google/osv-scanner:v2.2.3@sha256:fca9be4446310094be881b63f6862879282647f121ad3326e91dd90afbf8ef50
# One build cache per worktree, so parallel builds never wait on each other.
TARGET_VOLUME ?= sage-os-target-$(notdir $(CURDIR))
.PHONY: test lock up down wasm routines eval fmt lint vuln secrets imports

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
# $(1) runs in /src; $(2) overrides what is streamed in (default $(SOURCES)).
RUST = COPYFILE_DISABLE=1 tar --no-xattrs -c $(wildcard $(or $(2),$(SOURCES))) | docker run --rm -i \
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
	  cargo test --locked -q -p sage --test eval -- --ignored --nocapture live,$(EVAL_SOURCES)) | tee evals/.last-run.txt
	@[ -n "$(SAGE_EVAL_CASE)" ] || { echo; sed -n 's/^ledger: //p' evals/.last-run.txt; } >> evals/local/LEDGER.md
	@! grep -q '^ledger: - Failed' evals/.last-run.txt

# sage-core for the browser, and its size against Quila's 74 KB gzip budget.
wasm:
	$(call RUST,rustup target add wasm32-unknown-unknown >/dev/null 2>&1 && \
	  cargo build --locked -q -p sage-wasm --target wasm32-unknown-unknown --profile wasm && \
	  f=/target/wasm32-unknown-unknown/wasm/sage_wasm.wasm && \
	  echo "sage_wasm.wasm: $$(wc -c < $$f) bytes / $$(gzip -9 -c $$f | wc -c) gzipped")

# rustfmt the code in place: the formatted files are streamed back out.
fmt:
	$(call RUST,rustup component add rustfmt >/dev/null 2>&1 && cargo fmt --all && $(RUST_FILES) | tar -c -T -) | tar -x

# rustfmt and clippy, warnings as errors.
lint:
	$(call RUST,rustup component add rustfmt clippy >/dev/null 2>&1 && cargo fmt --all --check && \
	  cargo clippy --locked -q --workspace --all-targets -- -D warnings)

# Known vulnerabilities in Cargo.lock (osv-scanner; it asks the OSV database online).
vuln:
	COPYFILE_DISABLE=1 tar --no-xattrs -c Cargo.lock | docker run --rm -i --read-only --tmpfs /tmp --entrypoint sh $(OSV_IMAGE) \
	  -c 'cd /tmp && tar -x && /osv-scanner scan source --lockfile=Cargo.lock'

# Secrets in the whole repo's history: the root's gitleaks scan.
secrets:
	$(MAKE) -C ../.. secrets

# sage-core stays dependency-free of the other crates (the browser build uses it), and
# routines reach the kernel only over HTTP, never by linking it.
imports:
	@! grep -q 'path *=' core/Cargo.toml || { echo "core/Cargo.toml depends on a workspace crate" >&2; exit 1; }
	@! sed -n '/^\[dependencies\]/,/^\[/p' routines/Cargo.toml | grep -q 'path *=' \
	  || { echo "routines links a workspace crate outside dev-dependencies" >&2; exit 1; }
	@echo "imports: ok"

# Write Cargo.lock after changing dependencies.
lock:
	$(call RUST,cargo generate-lockfile -q && cat Cargo.lock) > Cargo.lock.new && mv Cargo.lock.new Cargo.lock
