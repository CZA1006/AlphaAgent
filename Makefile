.PHONY: install dev lint format typecheck test test-unit test-integration \
       db-up db-down db-status db-bootstrap db-reset check clean

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
	uv run python scripts/bootstrap_db.py

db-reset:
	docker compose down -v
	@echo "Postgres volume removed. Run 'make db-bootstrap' to recreate."

# ── Cleanup ──────────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
	rm -rf dist/ build/ *.egg-info/
