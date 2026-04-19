# Persistence backends

AlphaAgent has two interchangeable persistence backends for its registries
(experiments, hypotheses, memory).

| Backend  | When to use                                         | Requires        |
|----------|-----------------------------------------------------|-----------------|
| `memory` | Tests, notebooks, any single-process local run      | Nothing         |
| `sql`    | Persistent research runs, sharing results across runs | Postgres 14+ |

The default is **`memory`**, so `uv run python -m scripts.run_research_cycle`
works with zero database setup.

## Selecting a backend

Three ways, in order of precedence:

1. **CLI flag** on the research script:
   ```bash
   uv run python -m scripts.run_research_cycle --backend sql
   ```

2. **Environment variable** (picked up by `BackendConfig.from_env()`):
   ```bash
   export ALPHA_AGENT_BACKEND=sql
   uv run python -m scripts.run_research_cycle
   ```

3. **Programmatic** — construct a `BackendConfig` directly:
   ```python
   from alpha_harness.config import BackendConfig
   from alpha_harness.registries.factory import build_registries

   bundle = build_registries(BackendConfig.sql())
   # bundle.experiments / bundle.hypotheses / bundle.memories
   ```

Invalid values raise `ValueError` — there is no silent fallback.

## SQL backend prerequisites

1. Postgres reachable via standard env vars (all optional; defaults shown):
   ```bash
   export POSTGRES_USER=alphaagent
   export POSTGRES_PASSWORD=alphaagent_dev
   export POSTGRES_HOST=localhost
   export POSTGRES_PORT=5432
   export POSTGRES_DB=alphaagent
   ```
   A `docker-compose.yml` at the repo root starts a matching instance.

2. Tables are created automatically on first use (`auto_create_tables=True`).
   For controlled environments set `auto_create_tables=False` and run
   `uv run python scripts/bootstrap_db.py` explicitly.

## What is SQL-backed, what is still in-memory

| Component                | Memory mode | SQL mode            |
|--------------------------|-------------|---------------------|
| `ExperimentRegistry`     | In-process  | `SqlExperimentRegistry` (Postgres) |
| `HypothesisRegistry`     | In-process  | `SqlHypothesisRegistry` (Postgres) |
| `MemoryRegistry` (lineage) | In-process | `SqlMemoryRegistry` (Postgres) |
| `FactorRegistry`         | In-process  | In-process (not yet needed on the main path) |
| `SkillRegistry`          | In-process  | In-process (not yet needed on the main path) |
| LLM response cache       | n/a         | n/a (not persisted) |
| Market-data loaders      | Local files | Local files         |

The three registries on the core research loop — experiments, hypotheses,
and lineage memory — are the ones that need durability, so they are the
ones the factory switches. Everything else remains in-process; when a
persistent `FactorRegistry` or `SkillRegistry` is added, the factory is
the single place to wire them.

## Boundary guarantee

Business logic (`ResearchOrchestrator`, `RefinementRunner`,
`RelatedExperimentRetriever`, `NoveltyEvaluator`) is typed against
`ExperimentRegistryProtocol` / `HypothesisRegistryProtocol` /
`MemoryRegistryProtocol` rather than concrete classes. There is no
`if backend == "sql"` branching anywhere outside `registries/factory.py`.

## Integration tests

Postgres-backed orchestrator coverage lives in
`tests/integration/test_sql_orchestrator.py` under the `integration`
marker. It is skipped automatically when Postgres is unreachable, so the
default `uv run pytest` run stays hermetic. To run it:

```bash
export POSTGRES_DB=alphaagent_test   # avoid clobbering a real database
uv run pytest -m integration -v
```
