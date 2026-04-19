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

### Status (Round 3) — ✅ complete

Shipped in Round 3:

- **Canonical AST novelty** — structural DSL equality, not string equality.
- **Related experiment retrieval** — prior experiments feed back into cycles
  by canonical AST + evaluation-profile match.
- **LLM provider layer** (`alpha_harness/llm/`) — typed `LLMClient`
  protocol, `OpenRouterClient`, `MockLLMClient`, `OpenRouterConfig.from_env`.
- **`HypothesisProposer`** with schema-constrained JSON and one bounded
  repair round; every candidate is DSL-compiled before it reaches the loop.
- **Controlled `RefinementRunner`** with hard budgets
  (`max_depth`, `max_variants_per_step`, `max_total_children`) and novelty
  checks against root + siblings.
- **Lineage memory** writes on every cycle so experiment graphs can be
  reconstructed from the registry alone.
- **Configurable SQL-backed persistence** (`memory` default, `sql` opt-in
  via `--backend` / `ALPHA_AGENT_BACKEND`), with all business logic typed
  against registry protocols — no backend branching outside the factory.
- **Concrete `HarnessAgentAdapter`** composing proposer + orchestrator +
  refinement runner behind the `ResearchCycleRequest` /
  `ThemeCycleRequest` boundary contracts.
- **Autonomous cycle script** (`scripts/autonomous_cycle.py`) — theme →
  proposals → cycles → auto-refine → summary, with `--mock-llm` and
  `--data-source {synthetic,parquet,polygon}`.
- **Real local-testing path** — `.env.example`, `make doctor{,-real,-sql}`,
  `make run-{mock,real,real-data,real-sql}`, documented in
  [docs/LOCAL_TESTING.md](docs/LOCAL_TESTING.md). Exercised end-to-end
  against live OpenRouter + Polygon.

Known limitations from real-local testing (Polygon free-tier 5 rpm,
small-universe IC noise, LLM-only-syntactic mutation, model-slug drift)
are captured in [docs/ROUND3_SUMMARY.md](docs/ROUND3_SUMMARY.md).

## Milestone 3.5: Round 4 — closing the learning loop

**Status: next active phase.** Do not restart Round 3 work here.

### Goal
Turn a loop that *proposes and scores* into one that *learns from what it
proposed*, without breaking the deterministic-truth boundary.

### Scope
- LLM-guided refinement: when a root returns `REFINE`, feed the evaluation
  bundle + failure taxonomy back to the LLM so it can propose structural
  variants (still DSL-validated, still novelty-checked, still budget-bound).
- Skill distillation prototype: cluster promoted / narrowly-refined
  experiments into reusable `Skill` entries the proposer can condition on.
- Richer evaluator: sector / beta neutralization, multi-horizon labels,
  turnover and cost sensitivity.
- A real research universe: Parquet backfill for ≥50 US-equity names so
  cycles run on statistically meaningful data without Polygon rate-limit
  gymnastics.
- Hermes actually driving the adapter from a live agent loop.
- Cost / rate-limit guards: token budget per cycle, 429 backoff,
  structured LLM call logging.

### Explicitly out of scope for Round 4
Live trading, cloud deployment, multi-agent debate, expanding the DSL
surface, adding new LLM providers.

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
