# Initial Build Tasks for Claude Code

> **Historical document.** Every task group below shipped in Rounds 1–2,
> was extended in Round 3, and has been completely superseded by the
> Round 4–9 work. The judge stack now has six gates, the agent loop
> runs end-to-end against real OpenRouter + Polygon data, composites
> are first-class registry citizens that feed back into the proposer's
> memory digest, and the on-disk reproducibility chain
> (4F → 4G → 4H → 4I → 4J → Round 8 composite trails) is fully wired
> with operator CLIs.
>
> For the current status, read:
> - [README.md](README.md) — front-door status + capability table
> - [ROADMAP.md](ROADMAP.md) — milestone tracker (M1 → M3.12 complete)
> - [docs/ROUND3_SUMMARY.md](docs/ROUND3_SUMMARY.md) — Round 3 closeout
> - [docs/ROUND4_TO_6_SUMMARY.md](docs/ROUND4_TO_6_SUMMARY.md) — every
>   sub-round from 4A.1 through Round 6 with design notes
> - [docs/ROUND7_TO_9_SUMMARY.md](docs/ROUND7_TO_9_SUMMARY.md) —
>   Round 7 thumbnails, 7.1 combiner/validator parity, Round 8
>   composite promotion, Round 9 loop closure + composite refinement
>   + inspect_composite
> - [docs/CASE_STUDY_2026Q2.md](docs/CASE_STUDY_2026Q2.md) — end-to-end
>   run against real DeepSeek + Polygon SP-50
> - [docs/AUDIT_LOOK_AHEAD.md](docs/AUDIT_LOOK_AHEAD.md) — post-case-
>   study leakage audit + the two CRITICAL fixes that came out of it
>
> This file is kept for traceability only; do not use it as a live
> to-do list.

This file defines the first implementation wave.

## Objective
Create a local MVP that proves the Alpha Harness can run a deterministic research cycle on top of Hermes runtime.

## Task Group 1: Bootstrap the repository

### Tasks
- initialize Python project with `uv`
- create package layout under `alpha_harness/`
- create `tests/` structure
- create `configs/`, `scripts/`, `artifacts/`, and `data/` folders
- add basic lint/test configuration

### Expected outcome
A clean Python repo with repeatable local setup.

## Task Group 2: Add local infrastructure

### Tasks
- create `docker-compose.yml` with Postgres
- add environment example file
- add simple database connection module
- define initial schema migration or bootstrap SQL for registries

### Expected outcome
Local Postgres can start and be used by registry modules.

## Task Group 3: Create core schemas

### Tasks
Define typed schemas for:
- Hypothesis
- FactorSpec
- ExperimentRecord
- EvaluationBundle
- Skill
- RegimeState

### Expected outcome
The domain model is explicit and reusable.

## Task Group 4: Build data loaders

### Tasks
Create first-pass loaders for:
- US equity OHLCV
- SEC fundamentals placeholder or adapter stub
- crypto OHLCV

### Expected outcome
The system can fetch and persist sample data locally.

## Task Group 5: Build factor execution MVP

### Tasks
- define a minimal safe factor DSL
- implement a parser or restricted execution path
- support a small operator set
- compute one sample factor end-to-end

### Minimum operator set
- `lag`
- `ts_mean`
- `ts_std`
- `rank`
- `zscore`

### Expected outcome
One example factor can be defined without arbitrary code execution.

## Task Group 6: Build deterministic evaluator MVP

### Tasks
Implement:
- IC
- RankIC
- quantile spread
- basic novelty comparison stub

### Expected outcome
A factor can be scored deterministically.

## Task Group 7: Build registries

### Tasks
Implement registry interfaces and first storage paths for:
- experiments
- hypotheses
- skills
- memory

### Expected outcome
Experiments can be stored and queried.

## Task Group 8: Build a minimal research orchestrator

### Tasks
- create a research loop skeleton
- retrieve related prior context
- evaluate one hypothesis
- write experiment record
- return a decision

### Expected outcome
A scripted research cycle runs locally.

## Task Group 9: Connect Hermes runtime boundary

### Tasks
- define a thin integration boundary with Hermes runtime
- prove Alpha Harness can be invoked through that boundary
- avoid changing Hermes core unless absolutely needed

### Expected outcome
Hermes is usable as runtime substrate, not yet deeply modified.

## Explicit do-not-do list for this task wave

Do not prioritize:
- live trading execution
- high-frequency order book logic
- broad asset-class coverage
- cloud deployment
- complex autonomous agent societies
- fancy frontend
