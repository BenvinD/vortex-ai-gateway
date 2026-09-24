# One-word entry points. Every recipe here is a command AGENTS.md already
# documents; this file only saves typing them. `make` with no target lists them.

.DEFAULT_GOAL := help
.PHONY: help install run lint format typecheck test check image demo demo-down

GATEWAY  ?= http://localhost:8000
REQUESTS ?= 600

help:  ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install:  ## uv sync: .venv with the project and dev tooling
	uv sync

run:  ## the API on :8000 with reload, mock provider, no Redis
	uv run uvicorn vortex_ai_gateway.gateway:app --reload

lint:  ## ruff check
	uv run --no-sync ruff check src tests

format:  ## ruff format
	uv run --no-sync ruff format src tests

typecheck:  ## mypy --strict
	uv run --no-sync mypy src

test:  ## pytest with coverage
	uv run --no-sync pytest

# The CI job's steps, in CI's order, against the same non-editable install.
check:  ## everything CI runs, in CI's order
	uv sync --locked --no-editable
	uv run --no-sync ruff check src tests
	uv run --no-sync ruff format --check src tests
	uv run --no-sync mypy src
	uv run --no-sync pytest -v

image:  ## build the gateway image
	docker build -t vortex-ai-gateway:local .

# Gateway, Redis, Prometheus, Grafana and Jaeger, healthy, with traffic on the
# dashboard. Needs Docker and uv, and no vendor keys: the mock answers.
demo:  ## the whole stack, with traffic, in one command
	VORTEX_TRACING_ENABLED=true docker compose --profile tracing up -d --build --wait
	uv run scripts/generate_traffic.py --url $(GATEWAY) --requests $(REQUESTS)
	@echo
	@echo "  gateway     $(GATEWAY)/docs"
	@echo "  grafana     http://localhost:3000   (dashboard: Vortex AI Gateway)"
	@echo "  prometheus  http://localhost:9090"
	@echo "  jaeger      http://localhost:16686"
	@echo
	@echo "  make demo-down to stop it."

demo-down:  ## stop the demo stack and drop its volumes
	docker compose --profile tracing down -v
