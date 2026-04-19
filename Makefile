.PHONY: install dev lint format typecheck test test-unit test-integration \
       db-up db-down db-status db-bootstrap db-reset check clean \
       doctor doctor-mock doctor-real doctor-sql \
       run-mock run-real run-real-data run-real-sql \
       autonomous-mock autonomous-real

# ── Local env auto-load ──────────────────────────────────────────────────────
# When `.env` exists, export every variable it declares so the targets below
# (and the scripts they invoke) see keys without the developer needing to
# `source .env` first.  The file is gitignored — never commit real secrets.
ifneq (,$(wildcard ./.env))
  include .env
  export
endif

# ── Setup ────────────────────────────────────────────────────────────────────

install:
	uv sync

dev:
	uv sync --extra dev

# ── Quality gates ────────────────────────────────────────────────────────────

check: lint typecheck test-unit

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy alpha_harness/

# ── Tests ────────────────────────────────────────────────────────────────────

test:
	uv run pytest

test-unit:
	uv run pytest tests/unit/

test-integration:
	uv run pytest tests/integration/ -m integration

# ── Postgres ─────────────────────────────────────────────────────────────────

db-up:
	docker compose up -d postgres
	@echo "Waiting for Postgres to be ready..."
	@until docker compose exec postgres pg_isready -U $${POSTGRES_USER:-alphaagent} > /dev/null 2>&1; do sleep 0.5; done
	@echo "Postgres is ready."

db-down:
	docker compose down

db-status:
	@docker compose ps postgres
	@docker compose exec postgres pg_isready -U $${POSTGRES_USER:-alphaagent} 2>/dev/null \
		&& echo "Connection: OK" || echo "Connection: FAILED"

db-bootstrap: db-up
	uv run python -m scripts.bootstrap_db

db-reset:
	docker compose down -v
	@echo "Postgres volume removed. Run 'make db-bootstrap' to recreate."

# ── Doctor (preflight validation) ────────────────────────────────────────────
# Quickly answer "given the current .env, which run paths will work?".
# No live API calls are made — this is safe to run repeatedly.

doctor:
	uv run python -m scripts.doctor --mode all

doctor-mock:
	uv run python -m scripts.doctor --mode mock

doctor-real:
	uv run python -m scripts.doctor --mode real

doctor-sql:
	uv run python -m scripts.doctor --mode sql

# ── Local run entrypoints ────────────────────────────────────────────────────
# Opinionated one-liners that cover the four local-testing paths.  Pass
# additional flags via `ARGS=...` — e.g.  make run-real ARGS="--n-days 240".

ARGS ?=

# Mock LLM + synthetic data + in-memory registries.  Needs no keys.
run-mock:
	uv run python -m scripts.autonomous_cycle --mock-llm $(ARGS)

# Real LLM (OpenRouter) + synthetic data + in-memory registries.
# Requires OPENROUTER_API_KEY.  Uses the single-hypothesis script so one
# cycle finishes in seconds and is cheap on tokens.
run-real:
	@test -n "$$OPENROUTER_API_KEY" || { \
		echo "error: OPENROUTER_API_KEY is empty.  Fill it in .env or run 'make run-mock'." >&2; \
		exit 2; }
	uv run python -m scripts.run_research_cycle $(ARGS)

# Real LLM + real Polygon equity data + in-memory registries.
run-real-data:
	@test -n "$$OPENROUTER_API_KEY" || { echo "error: OPENROUTER_API_KEY is empty." >&2; exit 2; }
	@test -n "$$POLYGON_API_KEY"    || { echo "error: POLYGON_API_KEY is empty." >&2; exit 2; }
	uv run python -m scripts.run_research_cycle \
		--data-source polygon \
		--symbols AAPL,MSFT,GOOG \
		$(ARGS)

# Real LLM + real Polygon + Postgres registries.  Needs `make db-bootstrap` once.
run-real-sql:
	@test -n "$$OPENROUTER_API_KEY" || { echo "error: OPENROUTER_API_KEY is empty." >&2; exit 2; }
	@test -n "$$POLYGON_API_KEY"    || { echo "error: POLYGON_API_KEY is empty." >&2; exit 2; }
	uv run python -m scripts.run_research_cycle \
		--data-source polygon \
		--symbols AAPL,MSFT,GOOG \
		--backend sql \
		$(ARGS)

# Autonomous (theme → proposer → refinement) variants.
autonomous-mock:
	uv run python -m scripts.autonomous_cycle --mock-llm $(ARGS)

autonomous-real:
	@test -n "$$OPENROUTER_API_KEY" || { echo "error: OPENROUTER_API_KEY is empty." >&2; exit 2; }
	uv run python -m scripts.autonomous_cycle $(ARGS)

# ── Cleanup ──────────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
	rm -rf dist/ build/ *.egg-info/
