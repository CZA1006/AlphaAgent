# Acceptance Criteria

## Milestone 1 Definition of Done

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
