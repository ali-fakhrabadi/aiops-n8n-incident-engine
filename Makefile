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
UV_COMPILE := /tmp/.local/bin/uv pip compile --quiet --generate-hashes \
	--python-version 3.12 --python-platform x86_64-unknown-linux-gnu \
	--custom-compile-command "make lock"

.PHONY: help env secrets lock build up down destroy ps logs migrate revision \
	test lint format typecheck audit ci smoke evaluate

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_.-]+:.*## / {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

env: ## Create .env from .env.example (never overwrites)
	@if [[ -e .env ]]; then echo "kept     .env"; else cp .env.example .env && echo "created  .env"; fi

secrets: ## Generate missing secrets in ./secrets (never overwrites)
	@bash scripts/generate_secrets.sh

lock: ## Re-resolve and hash-pin requirements*.txt (Python 3.12, Linux)
	docker run --rm --volume "$(CURDIR):/src" --workdir /src \
		--user "$(DEV_UID):$(DEV_GID)" --env HOME=/tmp \
		$(if $(PIP_INDEX_URL),--env PIP_INDEX_URL=$(PIP_INDEX_URL) --env UV_DEFAULT_INDEX=$(PIP_INDEX_URL)) \
		$(LOCK_IMAGE) sh -c '\
		pip install --quiet --user uv==$(UV_VERSION) && \
		$(UV_COMPILE) requirements.in --output-file requirements.txt && \
		$(UV_COMPILE) requirements-dev.in --output-file requirements-dev.txt'

build: ## Build the runtime and dev images
	$(COMPOSE) build
	$(COMPOSE) --profile dev build dev

up: ## Build, migrate and start the stack; waits until health checks pass
	@test -s secrets/metrics_token || { echo "run 'make secrets' first"; exit 1; }
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

ci: lint typecheck test audit ## Run the same checks as CI

smoke: ## End-to-end checks of the running stack
	python3 scripts/smoke_test.py $(SMOKE_ARGS)

evaluate: ## Run the analysis scenarios (EVAL_ARGS="--analyzer ollama --model llama3.1")
	docker run --rm --network host --volume "$(CURDIR):/app" --workdir /app \
		--user "$(DEV_UID):$(DEV_GID)" --env HOME=/tmp \
		aiops-n8n-incident-engine:dev python scripts/evaluate_analysis.py $(EVAL_ARGS)
