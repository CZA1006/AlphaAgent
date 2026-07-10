.PHONY: install dev lint format typecheck test test-unit test-integration \
       db-up db-down db-status db-bootstrap db-reset check check-full clean \
       doctor doctor-mock doctor-real doctor-sql audit smoke \
       doctor-hk-ipo-data doctor-hk-ipo-events research-director-hk-ipo \
       run-mock run-real run-real-data run-real-sql \
       autonomous-mock autonomous-real \
       autonomous-researcher-hk-ipo autonomous-researcher-hk-ipo-run \
       validate-hk-ipo-events \
       backfill-sp50 backfill \
       list-factors list-cycles refine-factor list-trails validate-strict

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

check: lint typecheck audit test-unit

# `check-full` adds the integration smoke run (~30s) on top of `check`.
# CI can pick which gate to spend its budget on; local devs typically
# run `check` for fast feedback and `check-full` before pushing.
check-full: check smoke

# Run-time scope auditors — fail the build if alpha_harness imports the
# Hermes runtime, or if an evaluator pulls in network / subprocess / LLM
# SDKs.  Pure source inspection — no module side-effects.
audit:
	uv run python -m alpha_harness.audit

# End-to-end smoke: drive the autonomous cycle through mock-LLM with
# tmp-dir wiring and assert reports + factor-zoo round-trip.  Marked
# `integration` so unit gates skip it.
smoke:
	uv run pytest tests/integration/test_autonomous_smoke.py -m integration

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

doctor-hk-ipo-data:
	uv run --extra gcp python -m scripts.doctor_hk_ipo_data

doctor-hk-ipo-events:
	uv run --extra gcp python -m scripts.doctor_hk_ipo_events

research-director-hk-ipo:
	uv run python -m scripts.research_director --market hk_ipo $(ARGS)

autonomous-researcher-hk-ipo:
	uv run --extra gcp python -m scripts.autonomous_researcher --market hk_ipo $(ARGS)

autonomous-researcher-hk-ipo-run:
	uv run --extra gcp python -m scripts.autonomous_researcher --market hk_ipo --execute $(ARGS)

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

# ── Parquet backfill (Round 4A.2) ────────────────────────────────────────────
# One-time population of data/silver/equities from Polygon.  Idempotent —
# subsequent runs skip symbols whose Parquet file already covers the window.
# Honours POLYGON_RPM; expect roughly (n_symbols / rpm) minutes on free tier.
backfill-sp50:
	@test -n "$$POLYGON_API_KEY" || { echo "error: POLYGON_API_KEY is empty." >&2; exit 2; }
	uv run python -m scripts.backfill_parquet \
		--universe configs/universes/sp50.txt \
		$(ARGS)

# Generic backfill: pass `ARGS="--universe ... --start-date ..."` to override.
backfill:
	@test -n "$$POLYGON_API_KEY" || { echo "error: POLYGON_API_KEY is empty." >&2; exit 2; }
	uv run python -m scripts.backfill_parquet $(ARGS)

# ── Factor zoo ───────────────────────────────────────────────────────────────

list-factors:
	uv run python -m scripts.list_factors $(ARGS)

list-cycles:
	uv run python -m scripts.list_cycles $(ARGS)

# Round 4H — re-run refinement on a previously promoted factor under a
# new evaluation regime.  Requires --factor-id.
refine-factor:
	uv run python -m scripts.refine_factor $(ARGS)

# Round 4J — list / diff entries in the standalone trail registry.
list-trails:
	uv run python -m scripts.list_trails $(ARGS)

# Round 5 — run the autonomous loop under StrictRegime and report which
# of the 6 robustness gates rejected each candidate.  Default is
# synthetic data; pass --data-source parquet --universe ... for real.
validate-strict:
	uv run python -m scripts.validate_strict $(ARGS)

validate-hk-ipo-events:
	uv run --extra gcp python -m scripts.validate_strict \
		--data-source bigquery \
		--universe configs/universes/hk_ipo.txt \
		--start-date 2025-12-12 \
		--end-date 2026-06-26 \
		--regime lenient \
		--llm mock \
		--mock-preset hk_ipo_events \
		--theme "HK IPO event-conditioned microstructure signals" \
		--extra-guidance "Focus on interactions between OFI, spread, realized volatility, greenshoe expiry, stabilization windows, and cornerstone lockup expiry. Prefer event-conditioned expressions over generic price or volume factors." \
		$(ARGS)

# ── Cleanup ──────────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
	rm -rf dist/ build/ *.egg-info/
