# AlphaAgent Roadmap

## Mission

Build a self-improving quant research harness on top of Hermes runtime.

## Milestone 1: Local research MVP

### Goal
Run a complete local research loop for a small set of US equity and crypto data.

### Scope
- local repo setup
- Hermes runtime import path established
- DuckDB + Parquet research data storage
- Postgres registries
- minimal safe factor DSL
- deterministic factor execution
- signal quality evaluation
- experiment logging
- retrieval of related prior experiments

### Deliverables
- project skeleton compiles and tests run
- local dockerized Postgres works
- sample equity data loads successfully
- sample crypto data loads successfully
- one example factor can be evaluated end-to-end
- experiment is persisted and queryable

### Exit criteria
- single research cycle can run from CLI or script
- outputs include metrics and an experiment record

## Milestone 2: Research memory and reusable skills

### Goal
Teach the harness to use prior experiments and distill reusable research knowledge.

### Scope
- memory registry
- skill registry
- related experiment retrieval
- failure taxonomy storage
- success pattern storage
- skill distillation prototype

### Exit criteria
- orchestrator uses prior experiments as context
- repeated experiments can produce condensed learnings

## Milestone 3: Self-improving loop

### Goal
Move from a static research loop to a system that gets better over time.

### Scope
- promotion judge
- retry / mutation policies
- novelty penalties
- stop policies
- skill promotion logic
- meta-policy notes and updates

### Exit criteria
- repeated runs show reduced duplicate work
- the system can classify and reuse useful research patterns

## Milestone 4: Broader data and more asset support

### Scope
- better equity fundamentals
- more crypto exchange data
- ETF / macro context layers
- richer novelty comparison
- broader robustness checks

## Milestone 5: Optional cloud migration

### Scope
- remote storage
- scheduled ingestion
- remote experiment batches
- shared team infrastructure

### Note
Cloud is not a first milestone requirement.
