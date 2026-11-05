# AIOps Incident Engine - operator and developer commands.
#
# Requirements: Docker with Compose v2, GNU make, bash, openssl and python3 (for the
# smoke test). Quality checks run inside the dev container, so the host needs no
# Python packages. `make help` lists the targets.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory

# Optional local settings (ports, PIP_INDEX_URL for a PyPI mirror) come from .env.
-include .env

COMPOSE ?= docker compose
SERVICE ?=
MESSAGE ?=

# The dev container runs as the host user, so files it writes stay yours.
export DEV_UID := $(shell id -u)
export DEV_GID := $(shell id -g)
DEV_RUN := $(COMPOSE) --profile dev run --rm dev

LOCK_IMAGE := python:3.12.15-slim-trixie
UV_VERSION := 0.13.5
# Generous network timeouts (seconds) for slow links to the package index.
LOCK_HTTP_TIMEOUT ?= 180
UV_COMPILE := /tmp/.local/bin/uv pip compile --quiet --generate-hashes \
	--python-version 3.12 --python-platform x86_64-unknown-linux-gnu \
	--custom-compile-command "make lock"

# The image the Code-node tests run in (it has Node.js); .env may override it.
SCREENSHOTS_IMAGE := aiops-screenshots:local
N8N_IMAGE ?= n8nio/n8n:2.45.6@sha256:45edf89655fd66dd55ea9336c1791ad14777599fdbf6c5aec6db583ede99a40b
REQUIRED_SECRETS := metrics_token callback_token n8n_webhook_secret n8n_owner_password \
	telegram_bot_token

.PHONY: help env secrets lock build up down destroy ps logs migrate revision \
	test lint format typecheck audit ci smoke evaluate workflow workflow-check \
	test-workflow n8n-setup telegram-test telegram-chats simulate screenshots

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_.-]+:.*## / {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

env: ## Create .env from .env.example (never overwrites)
	@if [[ -e .env ]]; then echo "kept     .env"; else cp .env.example .env && echo "created  .env"; fi

secrets: ## Generate missing secrets in ./secrets (never overwrites)
	@bash scripts/generate_secrets.sh

lock: ## Re-resolve and hash-pin requirements*.txt (Python 3.12, Linux)
	docker run --rm --volume "$(CURDIR):/src" --workdir /src \
		--user "$(DEV_UID):$(DEV_GID)" --env HOME=/tmp \
		--env PIP_DEFAULT_TIMEOUT=$(LOCK_HTTP_TIMEOUT) --env UV_HTTP_TIMEOUT=$(LOCK_HTTP_TIMEOUT) \
		--env UV_HTTP_CONNECT_TIMEOUT=$(LOCK_HTTP_TIMEOUT) --env UV_HTTP_RETRIES=5 \
		$(if $(PIP_INDEX_URL),--env PIP_INDEX_URL=$(PIP_INDEX_URL) --env UV_DEFAULT_INDEX=$(PIP_INDEX_URL)) \
		$(LOCK_IMAGE) sh -c '\
		pip install --quiet --user uv==$(UV_VERSION) && \
		$(UV_COMPILE) requirements.in --output-file requirements.txt && \
		$(UV_COMPILE) requirements-dev.in --output-file requirements-dev.txt && \
		cd tools/screenshots && $(UV_COMPILE) requirements.in --output-file requirements.txt'

build: ## Build the runtime and dev images
	$(COMPOSE) build
	$(COMPOSE) --profile dev build dev

up: ## Build, migrate, set up n8n and start the stack; waits until health checks pass
	@for name in $(REQUIRED_SECRETS); do \
		test -e "secrets/$$name" || { echo "secrets/$$name is missing: run 'make secrets'"; exit 1; }; \
	done
	$(COMPOSE) up --detach --build --wait --wait-timeout 300

down: ## Stop the stack (data volumes are kept)
	$(COMPOSE) --profile dev down --remove-orphans

destroy: ## Stop the stack and DELETE all data (incidents, n8n workflows and credentials)
	@read -r -p "This permanently deletes all data. Type 'yes' to continue: " answer; \
		[[ "$$answer" == "yes" ]] || { echo "Aborted."; exit 1; }
	$(COMPOSE) --profile dev down --volumes --remove-orphans

ps: ## Service status
	$(COMPOSE) ps

logs: ## Follow logs (SERVICE=api|n8n|postgres|redis to filter)
	$(COMPOSE) logs --follow --tail=200 $(SERVICE)

migrate: ## Apply database migrations as the schema-owner role
	$(COMPOSE) run --rm migrate

revision: ## Autogenerate a migration from the models (MESSAGE="add incidents")
	@test -n "$(MESSAGE)" || { echo 'usage: make revision MESSAGE="..."'; exit 1; }
	$(COMPOSE) run --rm --user "$(DEV_UID):$(DEV_GID)" --volume "$(CURDIR)/src:/app/src" \
		migrate alembic revision --autogenerate -m "$(MESSAGE)"

test: ## Unit and integration tests with coverage, against this stack's PostgreSQL and Redis
	$(DEV_RUN) pytest --cov --cov-report=term-missing:skip-covered

lint: ## Ruff lint and format verification
	$(DEV_RUN) sh -c 'ruff check . && ruff format --check .'

format: ## Apply Ruff fixes and formatting
	$(DEV_RUN) sh -c 'ruff check --fix . && ruff format .'

typecheck: ## Strict mypy
	$(DEV_RUN) mypy

audit: ## Check locked runtime dependencies for known vulnerabilities
	$(DEV_RUN) pip-audit --requirement requirements.txt --require-hashes --disable-pip --strict

ci: lint typecheck test audit workflow-check test-workflow ## Run the same checks as CI

smoke: ## End-to-end checks of the running stack (SMOKE_ARGS=--telegram also pages Telegram)
	python3 scripts/smoke_test.py $(SMOKE_ARGS)

screenshots: ## PNGs of the n8n workflow, a real execution and the API docs into docs/images
	@mkdir -p docs/images
	docker build --quiet --tag $(SCREENSHOTS_IMAGE) $(if $(PIP_INDEX_URL),--build-arg PIP_INDEX_URL=$(PIP_INDEX_URL)) \
		tools/screenshots > /dev/null
	docker run --rm --network host --shm-size 512m --user "$(DEV_UID):$(DEV_GID)" --env HOME=/tmp \
		--volume "$(CURDIR)/docs/images:/out" \
		--volume "$(CURDIR)/secrets/n8n_owner_password:/run/secrets/n8n_owner_password:ro" \
		$(SCREENSHOTS_IMAGE) --email "$(or $(N8N_OWNER_EMAIL),owner@example.com)"

workflow: ## Rebuild the n8n workflow JSON from scripts/build_workflow.py and n8n_workflows/code
	python3 scripts/build_workflow.py

workflow-check: ## Fail if the committed n8n workflow JSON is out of date
	python3 scripts/build_workflow.py --check

test-workflow: ## Unit tests of the n8n Code nodes (node --test, in the n8n image)
	docker run --rm --read-only --network none --user node \
		--volume "$(CURDIR)/n8n_workflows:/workflows:ro" --entrypoint node \
		$(N8N_IMAGE) --test /workflows/tests/code_nodes.test.mjs

n8n-setup: ## Re-import n8n's credentials and workflow (after changing TELEGRAM_*), restart n8n
	$(COMPOSE) stop n8n
	$(COMPOSE) run --rm n8n-setup --force
	$(COMPOSE) up --detach --wait n8n

TELEGRAM_ENV = TELEGRAM_CHAT_ID="$(TELEGRAM_CHAT_ID)" \
	TELEGRAM_API_BASE_URL="$(TELEGRAM_API_BASE_URL)" \
	HTTPS_PROXY="$(N8N_HTTPS_PROXY)" NO_PROXY="$(N8N_NO_PROXY)"

telegram-test: ## Check the bot token and TELEGRAM_CHAT_ID, and send one test message
	$(TELEGRAM_ENV) python3 scripts/telegram_test.py $(TELEGRAM_TEST_ARGS)

telegram-chats: ## List the chats that recently wrote to the bot, with their IDs
	$(TELEGRAM_ENV) python3 scripts/telegram_test.py --find-chat

simulate: ## Fire a simulated alert and follow it (SCENARIO=disk-full SIMULATE_ARGS="--severity critical")
	python3 scripts/simulate_alert.py $(SCENARIO) $(SIMULATE_ARGS)

evaluate: ## Run the analysis scenarios (EVAL_ARGS="--analyzer ollama --model llama3.1")
	docker run --rm --network host --volume "$(CURDIR):/app" --workdir /app \
		--user "$(DEV_UID):$(DEV_GID)" --env HOME=/tmp \
		aiops-n8n-incident-engine:dev python scripts/evaluate_analysis.py $(EVAL_ARGS)
