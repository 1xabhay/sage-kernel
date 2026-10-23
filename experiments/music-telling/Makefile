# music-telling — common commands
# Everything runs through uv; `make setup` is the only prerequisite.

UV      ?= uv
# Always this project's own .venv, even when another venv is active (VIRTUAL_ENV).
export UV_PROJECT_ENVIRONMENT := $(CURDIR)/.venv
PY      := $(UV) run --locked --project $(CURDIR)
THEME   ?= leaving home for the first time
LENGTH  ?= 8
POOL    ?= 40
OUT     ?= out/story.md
MODEL   ?= qwen2.5:7b

.DEFAULT_GOAL := help
.PHONY: help setup setup-mood ollama tell doctor test lint fmt clean clean-cache \
        docker-build tell-docker docker-shell up down logs tell-fast serve image-test \
        vuln secrets imports

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Create .venv from uv.lock (no mood models)
	$(UV) sync --locked --project $(CURDIR)

setup-mood: ## Install with Essentia mood models (heavier: TensorFlow)
	$(UV) sync --locked --project $(CURDIR) --extra mood

ollama: ## Install Ollama and pull the local model (free, no API key)
	@command -v ollama >/dev/null || brew install ollama
	@curl -sf http://localhost:11434/api/tags >/dev/null \
	  || (echo "starting ollama..."; (ollama serve >/tmp/ollama.log 2>&1 &); sleep 5)
	ollama pull $(MODEL)
	@echo "Ready. Run: make doctor"

serve: ## Start the Ollama server in the foreground
	ollama serve

tell: ## Build a story. Override: make tell THEME="..." LENGTH=6
	@mkdir -p $(dir $(OUT))
	$(PY) music-telling tell "$(THEME)" --length $(LENGTH) --pool $(POOL) --out $(OUT)
	@echo "\nWrote $(OUT)"

tell-fast: ## Build a story with no audio analysis (quickest path)
	@mkdir -p $(dir $(OUT))
	$(PY) music-telling tell "$(THEME)" --length $(LENGTH) --pool $(POOL) --no-audio --out $(OUT)

doctor: ## Check data sources, audio stack and credentials
	$(PY) music-telling doctor

test: ## Run the test suite (no network, no API key needed)
	$(PY) pytest -q

lint: ## Lint with ruff
	$(PY) ruff check src tests

vuln: ## Audit the locked dependencies for known vulnerabilities (pip-audit, needs the internet)
	$(UV) export --locked --project $(CURDIR) --all-extras --no-emit-project --format requirements-txt -o .audit-requirements.txt -q
	$(UV) tool run pip-audit@2.9.0 --strict --disable-pip -r .audit-requirements.txt; s=$$?; rm -f .audit-requirements.txt; exit $$s

secrets: ## Scan the whole repo's history for secrets (root gitleaks target)
	$(MAKE) -C ../.. secrets

imports: ## Import boundaries
	@echo "n/a: no boundary (a standalone CLI with one package)"

fmt: ## Format with ruff
	$(PY) ruff format src tests
	$(PY) ruff check --fix src tests

clean: ## Remove build artifacts
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache out
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

clean-cache: ## Drop the HTTP/audio/model cache
	$(PY) python -c "import shutil; from music_telling.config import settings; \
	shutil.rmtree(settings.cache_dir, ignore_errors=True); print('cleared', settings.cache_dir)"

# ---------------------------------------------------------------- docker

docker-build: ## Build the image
	docker compose build

up: ## Run `doctor` in the container (compose)
	docker compose run --rm music-telling doctor

tell-docker: ## Build a story inside the container (Markdown on stdout)
	docker compose run --rm music-telling tell "$(THEME)" --length $(LENGTH)

docker-shell: ## Interactive shell in the container
	docker compose run --rm --entrypoint bash music-telling

down: ## Stop and remove the containers (named volumes are kept)
	docker compose down

logs: ## Tail compose logs
	docker compose logs -f

IMAGE_TEST_TAG ?= music-telling-image-test:$(shell date +%s)
image-test: ## Build the lite image, check its entrypoint and tests offline, then remove it
	docker build -q --target lite -t $(IMAGE_TEST_TAG) . >/dev/null
	docker run --rm --network none $(IMAGE_TEST_TAG) --help >/dev/null \
	  && docker run --rm --network none --entrypoint pytest $(IMAGE_TEST_TAG) -q; \
	  s=$$?; docker rmi -f $(IMAGE_TEST_TAG) >/dev/null; exit $$s
