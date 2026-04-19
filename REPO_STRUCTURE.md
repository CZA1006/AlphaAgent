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
- `docs/ROUND3_SUMMARY.md`: current status after Round 3 closeout
- `docs/LOCAL_TESTING.md`: real-API local-testing guide
- `docs/BACKENDS.md`: memory vs SQL registry backend selection

## Main code directories

### `vendor/hermes-agent/`
Pinned Hermes runtime substrate.

### `alpha_harness/`
Own code lives here.

Key modules:
- `service.py` — domain service interface (entry point for all external callers)

Subdirectories (current, post-Round-3):
- `orchestrator/` — research loop + `RefinementRunner`
- `proposer/` — `HypothesisProposer` (only caller of `llm/`)
- `llm/` — `LLMClient` protocol, `OpenRouterClient`, `MockLLMClient`
- `hermes_boundary/` — `HarnessAgentAdapter` + boundary contracts
- `evaluators/` — deterministic metrics + `PromotionJudge`
- `factors/` — DSL compiler, canonical AST, executor
- `retrieval/` — related-experiment retrieval
- `registries/` — experiments / hypotheses / memory (memory + sql)
- `memory/` — lineage memory
- `skills/` — skill registry stubs (not yet on the main path)
- `data/` — synthetic / parquet / polygon loaders
- `reports/`
- `schemas/`
- `db/` — connection + Postgres glue

Note: `agents/` and `tools/` are NOT part of Alpha Harness. Those names
belong to the Hermes runtime layer. The Hermes adapter lives in
`alpha_harness/hermes_boundary/` and exposes typed contracts only — it
does **not** import Hermes internals into business logic.

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
