# Recommended Repository Structure

## Root

- `README.md`: project overview
- `AGENTS.md`: coding-agent operating contract
- `ARCHITECTURE.md`: system design
- `ROADMAP.md`: milestones
- `TASKS.md`: current build tasks
- `ACCEPTANCE_CRITERIA.md`: what done means
- `DATA_PLAN.md`: data sources and storage
- `CLAUDE_CODE_GUIDE.md`: how Claude Code should work in this repo
- `CODEX_REVIEW_GUIDE.md`: how Codex should review this repo

## Main code directories

### `vendor/hermes-agent/`
Pinned Hermes runtime substrate.

### `alpha_harness/`
Own code lives here.

Key modules:
- `service.py` — domain service interface (entry point for all external callers)

Subdirectories:
- `orchestrator/`
- `evaluators/`
- `registries/`
- `memory/`
- `skills/`
- `data/`
- `factors/`
- `reports/`
- `schemas/`
- `db/`

Note: `agents/` and `tools/` are NOT part of Alpha Harness.
Those names belong to the Hermes runtime layer.
Any future Hermes adapter lives outside `alpha_harness/`.

### `configs/`
Use config files for:
- hermes runtime integration
- data source settings
- evaluation thresholds
- promotion policies

### `scripts/`
Command-line bootstrap and utility scripts.

### `tests/`
- `unit/`
- `integration/`
- `e2e/`

### `artifacts/`
Generated outputs:
- charts
- reports
- experiment json
- logs

### `data/`
Local data storage.
Suggested split:
- `raw/`
- `bronze/`
- `silver/`
- `gold/`

## Non-goals for the first structure

Do not start with:
- massive microservice sprawl
- too many nested packages
- premature cloud deployment directories
- live execution infrastructure
