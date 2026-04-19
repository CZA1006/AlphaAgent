# Acceptance Criteria

> **Status note.** Milestone 1 (Round 2 MVP) and the Round 3 autonomy
> extension are both complete. For the current post-Round-3 status and
> Round 4 scope, read [docs/ROUND3_SUMMARY.md](docs/ROUND3_SUMMARY.md)
> first — this file is retained as historical reference.

## Round 3 Exit Criteria — ✅ met

- `HypothesisProposer` produces schema-valid, DSL-compiled candidates
  from a free-form theme, using a typed `LLMClient`.
- `RefinementRunner` expands `REFINE`-verdict experiments under hard
  budgets with canonical-AST novelty checks.
- Canonical AST novelty + related-experiment retrieval are wired into
  the orchestrator.
- Lineage memory is written on every cycle.
- `HarnessAgentAdapter` exposes `ResearchCycleRequest` /
  `ThemeCycleRequest` contracts; no deterministic decision is overridden
  by the adapter.
- `memory` and `sql` registry backends are selectable via CLI flag or
  `ALPHA_AGENT_BACKEND`; business logic is typed against registry
  protocols.
- `scripts/autonomous_cycle.py` runs end-to-end in mock mode and in full
  real mode (OpenRouter + Polygon), with `make doctor` preflight
  validation.
- Default test run (`make test`) requires no keys and no network.

## Milestone 1 Definition of Done (historical)

The first milestone is complete when all items below are true.

### Repo and environment
- project installs with `uv`
- tests can be run locally
- linting and type checks run locally
- Docker Postgres starts successfully

### Data layer
- at least one US equity sample dataset can be ingested
- at least one crypto sample dataset can be ingested
- ingested data is persisted in Parquet or equivalent local store

### Domain model
- core schemas exist and are used by code
- registry code does not pass around loose untyped dicts for core entities

### Factor layer
- at least one factor can be expressed using the safe DSL
- unrestricted arbitrary code execution is not used
- factor execution is deterministic and test-covered

### Evaluation layer
- IC is computed deterministically
- RankIC is computed deterministically
- quantile spread or equivalent portfolio spread metric exists
- evaluation outputs are persisted in an experiment record

### Registry layer
- experiment records are written to persistent storage
- experiments can be queried by id
- a simple search or retrieval of related experiments exists

### Orchestrator layer
- a single scripted research cycle runs end-to-end
- the cycle returns a decision such as reject, refine, archive_only, or promote_candidate

### Architecture discipline
- Hermes runtime and Alpha Harness are separated by explicit interfaces
- deterministic research logic is not hidden inside prompts

## Quality bar

The milestone is not complete if:
- the system only works from a notebook but not from a reproducible script
- core logic is mostly prompt text instead of code
- experiment outputs are not persisted
- there is no clear boundary between runtime substrate and quant harness
