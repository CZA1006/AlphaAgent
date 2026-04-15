# AGENTS.md

This file defines the working rules for all coding agents in this repository.

It should be treated as a persistent project contract.

## Project identity

This is a **quant research system** built using a Hermes runtime substrate plus a custom **Alpha Harness**.

This is **not** a generic chatbot and **not** a generic workflow assistant.

The purpose of this repository is to build a **self-improving alpha discovery harness** for US equities and crypto first.

## Golden rules

1. Deterministic engines own all quantitative truth.
2. LLM components may propose, critique, summarize, and distill.
3. LLM components must not be the final authority on statistics, backtests, or risk evaluation.
4. Every experiment must be reproducible.
5. Every experiment must be logged.
6. Every failed experiment must include a failure taxonomy.
7. All data handling must be point-in-time aware wherever applicable.
8. Hermes runtime and Alpha Harness must remain separated by clean interfaces.
9. Prefer typed schemas and deterministic tools over loose prompt-only behavior.
10. We are building research infrastructure first, not a live trading bot.

## Required architectural boundary

### Hermes runtime layer
Owns:
- runtime loop
- provider/model integration
- prompt assembly
- memory plumbing hooks
- tool execution hooks
- session infrastructure

### Alpha Harness layer
Owns:
- market state representation
- hypothesis generation contracts
- factor DSL and safe compilation
- deterministic factor execution
- evaluators
- experiment registry
- skill registry
- memory registry
- promotion decisions
- self-improvement policies

Do not blur this boundary.

## Coding rules

- Use Python 3.11.
- Use `uv` for environment and dependency management.
- Prefer small typed modules over large scripts.
- Prefer explicit interfaces over hidden coupling.
- Use dataclasses or Pydantic models for core schemas.
- Write tests for deterministic research logic.
- Avoid putting core research rules into prompts when they belong in code.
- Do not implement unrestricted code execution from LLM outputs.

## Data rules

### Phase 1 focus
- US equities
- Crypto spot/perp

### Storage principles
- Use DuckDB + Parquet for historical data and panel-like research outputs.
- Use Postgres for registries and structured metadata.
- Store experiment artifacts in the local filesystem under `artifacts/`.

### Point-in-time requirements
- Avoid survivorship bias in equity universes.
- Respect publication timestamps for fundamentals/news/event data.
- Keep symbol normalization explicit.
- Treat crypto exchange-specific data as exchange-scoped unless explicitly normalized.

## Evaluation rules

Every candidate factor should eventually support these checks:

- IC / RankIC
- quantile spread
- monotonicity
- turnover
- cost sensitivity
- novelty versus known factors
- stability across subperiods or regimes

Do not accept vague claims like "looks good" without deterministic outputs.

## Research loop expectations

The core loop should be:

1. retrieve relevant context
2. propose hypothesis
3. compile to safe factor spec
4. run deterministic evaluation
5. judge result
6. store experiment
7. write memory / update skills

## First milestone bias

In Milestone 1, optimize for:
- clean repo structure
- local reproducibility
- data ingestion
- minimal factor DSL
- experiment logging
- deterministic evaluation

In Milestone 1, do not optimize for:
- full autonomous multi-agent behavior
- production deployment
- performance micro-optimizations
- live execution

## Claude Code role

Claude Code is the primary implementation agent.
It should:
- scaffold modules
- implement features
- write tests
- update docs when architecture changes

## Codex role

Codex is the review and repair agent.
It should:
- review diffs
- find architectural drift
- detect hidden bugs
- identify leakage risks
- propose targeted fixes

## Final instruction

When in doubt, preserve:
- correctness
- reproducibility
- clear boundaries
- typed contracts
- deterministic evaluation

