# Tidelink developer commands. Run `make` to see them all.
SHELL := /bin/bash
PY    ?= python3.12
VENV  := .venv
BIN   := $(VENV)/bin
PORT  ?= 8000
APP   ?= sales-assistant

.DEFAULT_GOAL := help
.PHONY: help setup venv web-install env dev run run-mock build-web test lint fmt typecheck-web check \
        llm-check smoke mcp-inspect cf-push cf-logs clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-15s\033[0m %s\n",$$1,$$2}'

# ---------------------------------------------------------------- setup
setup: venv web-install env build-web ## One-time setup: Python venv, npm deps, .env, UI build
	@echo "Setup complete. Next: make dev   (or make run-mock for a no-keys demo)"

venv: ## Create .venv and install Python dependencies
	$(PY) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements-dev.txt

web-install: ## Install web dependencies
	cd web && npm install

env: ## Create .env from .env.example (never overwrites)
	@if [ -f .env ]; then echo ".env already exists; leaving it alone"; else \
	  cp .env.example .env && echo "Created .env: set the AZURE_OPENAI_* values (and LIVE_MCP_URL if you have one)"; fi

# ------------------------------------------------------------------ run
dev: ## Run API with auto-reload + rebuild UI on change (http://localhost:8000)
	@trap 'kill 0' EXIT; \
	(cd web && npm run watch) & \
	$(BIN)/python -m uvicorn app.main:app --reload --port $(PORT) \
	  --reload-dir app --reload-dir scenarios --reload-dir data

run: build-web ## Build the UI and run the app as it runs on Cloud Foundry
	$(BIN)/python -m uvicorn app.main:app --port $(PORT) --proxy-headers

run-mock: build-web ## Run with the offline mock model (ignores AZURE_OPENAI_*)
	AZURE_OPENAI_ENDPOINT= $(BIN)/python -m uvicorn app.main:app --port $(PORT)

build-web: ## Bundle the React UI into app/static
	cd web && npm run build

# ----------------------------------------------------------------- quality
test: ## Run the Python test suite
	$(BIN)/python -m pytest

lint: ## Lint and format-check Python
	$(BIN)/ruff check app tests
	$(BIN)/ruff format --check app tests

fmt: ## Auto-format Python
	$(BIN)/ruff format app tests
	$(BIN)/ruff check --fix app tests

typecheck-web: ## Type-check the React app
	cd web && npm run typecheck

check: lint test typecheck-web build-web ## Everything CI runs

llm-check: ## Check Azure OpenAI answers and can call tools
	$(BIN)/python -m app.llm_check

smoke: ## Smoke-test a running app (BASE_URL=... to target Cloud Foundry)
	@bash scripts/smoke.sh

mcp-inspect: ## Open MCP Inspector; connect to http://localhost:8000/mcp
	@echo "Transport: Streamable HTTP   URL: http://localhost:$(PORT)/mcp"
	@echo "Header (only if MCP_AUTH_REQUIRED=true): Authorization: Bearer <MCP_SERVER_TOKEN from .env>"
	npx @modelcontextprotocol/inspector

# ------------------------------------------------------------ cloud foundry
cf-push: build-web ## Build the UI and cf push
	cf push -f manifest.yml

cf-logs: ## Tail recent app logs
	cf logs $(APP) --recent

clean: ## Remove build output and caches
	rm -rf app/static .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
