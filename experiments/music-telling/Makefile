# music-telling — common commands
# Everything runs through uv; `make setup` is the only prerequisite.

UV      ?= uv
PY      := $(UV) run
THEME   ?= leaving home for the first time
LENGTH  ?= 8
POOL    ?= 40
OUT     ?= out/story.md
MODEL   ?= qwen2.5:7b

.DEFAULT_GOAL := help
.PHONY: help setup setup-mood ollama tell doctor test lint fmt clean clean-cache \
        docker-build tell-docker docker-shell up down logs tell-fast serve

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Create the uv venv and install the package (no mood models)
	$(UV) venv
	$(UV) pip install -e ".[dev]"

setup-mood: ## Install with Essentia mood models (heavier: TensorFlow)
	$(UV) venv
	$(UV) pip install -e ".[dev,mood]"

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

tell-docker: ## Build a story inside the container
	docker compose run --rm music-telling tell "$(THEME)" --length $(LENGTH) --out /app/out/story.md

docker-shell: ## Interactive shell in the container
	docker compose run --rm --entrypoint bash music-telling

down: ## Stop and remove compose resources
	docker compose down -v

logs: ## Tail compose logs
	docker compose logs -f
