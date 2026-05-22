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

### Status — ✅ complete

Shipped across 10 sub-rounds (4A.1 → 4A.10) plus 4B → 4J:

- **4A.1** cost / rate-limit / call-hygiene guardrails (token budget, 429
  backoff, structured LLM call log)
- **4A.2** real research universe via Parquet backfill (50 SP large-caps,
  2 years of daily bars)
- **4A.3** richer evaluator: sector / beta neutralization, multi-horizon
  labels, cost-adjusted spread, multi-horizon sign-consistency gate
- **4A.4** memory-aware proposer: rolling digest of recent experiments
  fed into the proposer prompt
- **4A.5** promoted-factor zoo with diff-friendly JSON + append-only
  index
- **4A.6** targeted refinement via `RefinementBrief` (mutation
  prioritization based on which gate failed)
- **4A.7** lineage-aware factor zoo (`parent_factor_id`,
  `refinement_round`, lineage-tree CLI)
- **4A.8** cycle audit reports (`StrictValidationReport` precursor)
- **4A.9** runtime scope auditors (`make audit` blocks `hermes.*` imports,
  evaluator IO)
- **4A.10** end-to-end smoke marker + `make check-full`
- **4B** walk-forward stability gate (`fraction_positive_rank_ic`)
- **4C** risk-aware portfolio metrics + tail-concentration gate
- **4D** calendar-aware embargo + purged folds (closes the lookahead bug
  in 4B)
- **4E** out-of-sample holdout decay gate
- **4F** `PromotionTrail` — immutable SHA-256 of every evaluator + judge
  knob, captured on every promote
- **4G** trail-aware refinement guard
  (`RefinementRunner.refine_record()` refuses to mine factors whose
  lineage didn't validate under the current trail)
- **4H** seeded refinement CLI (`scripts/refine_factor.py`,
  `make refine-factor`)
- **4I** `PromotionTrail.diff()` + `make list-factors --diff-trails`
- **4J** standalone trail registry (`artifacts/trails/`,
  `make list-trails`)

The judge stack is now a 6-gate filter (data sufficiency → profile
thresholds → sign-consistency → walk-forward stability →
tail-concentration → holdout decay), every promotion is regime-stamped,
and the operator surface (5 CLIs) lets researchers query / replay /
diff anything in the on-disk record.

## Milestone 3.6: Round 5 — strict-regime real-data validation

### Status — ✅ complete

- **`StrictRegime`** + **`LenientRegime`** in `alpha_harness/regimes.py`:
  frozen dataclasses bundling every evaluator + judge knob into one
  hashable config.
- **`scripts/validate_strict.py`**: drives the autonomous-cycle stack
  against a named regime, persists a per-cycle `StrictValidationReport`
  with per-gate rejection counts.
- **`--llm openrouter`** wires the real LLM proposer through the same
  budget + call-log + structured-JSON guards that `autonomous_cycle`
  uses. Verified end-to-end against DeepSeek-Chat-v3.1 on SP-50.
- **`--n-cycles N`** + memory digest: 5-cycle real-LLM run on SP-50
  produced 30 LLM-proposed factors, fired 4 of 6 judge gates in
  production for the first time.

Outcome: 0 promotions across 60 LLM-proposed factors on SP-50 (strict +
lenient combined). Result is real research signal — the strict regime is
correctly correlated with downstream failure, not over-aggressive.

## Milestone 3.7: Round 6 — multi-factor combination

### Status — ✅ first cut shipped

- **`alpha_harness/combination/`** module: `compute_signal()`,
  `combine_signals()` (rank-aggregate / z-score-average / equal-weight),
  `pairwise_rank_corr()` diagnostic.
- **`scripts/combine_factors.py`**: takes N DSL expressions, scores each
  individually plus the basket, returns a per-factor + basket table with
  the average pairwise rank-correlation.

First real-data test (5 mixed factors, SP-50): basket IC = -0.019 with
avg pairwise rank-corr = +0.05. The plumbing works; manufacturing a
combination that promotes is now an experiment, not an engineering
task.

## Milestone 3.8 — 3.12: Rounds 7–9 + case study + audit

### Status — ✅ complete

Rounds 7 → 9 turned baskets from operator one-shots into agent-loop
citizens.  Round 7 made validation reports self-contained
(`FactorThumbnail`); 7.1 closed the measurement gap between combiner
and validator (`evaluate_precomputed_signal` extracted, walk-forward
parity guaranteed).  Round 8 promoted baskets to registry citizens
(`FactorSpec.composite_recipe`, `CombinationRecipe` with hashable
`recipe_id`, `combine_factors --promote`).  Round 9 closed the loop
back to the proposer: memory digest reads the durable promoted-
artifact index, refinement learned a composite path (mutate one
component, rebuild the recipe), and `scripts/inspect_composite` lets
operators audit any recipe.

The Q2 2026 case study (`docs/CASE_STUDY_2026Q2.md`) ran the full
Round-1-to-9 stack against real DeepSeek + real Polygon SP-50.  The
post-study look-ahead audit (`docs/AUDIT_LOOK_AHEAD.md`) found
**three CRITICAL bugs** — combiner bypassed `HoldoutPolicy`,
`FactorThumbnail` dropped the holdout block, and
`SignalQualityEvaluator` recomputed signals per-fold (inflating IC
via fold-boundary rolling-window degeneracy).  All three fixed.

The **honest re-run** with disjoint train/test windows
(`docs/CASE_STUDY_HONEST.md`, post-fix section) is the headline
result: on Y2 (out-of-sample, never used for selection), the
basket clears strict on both gates with IC `+0.058` and rank_IC
`+0.053` — actually stronger than its Y1 in-sample metrics.
Promoted as composite `recipe_id=635f8a09903a2c37`.  This is the
first demonstration that the architecture produces a real,
out-of-sample-validated alpha on real markets with honest
measurement.

Full per-sub-round design notes in
[`docs/ROUND7_TO_9_SUMMARY.md`](docs/ROUND7_TO_9_SUMMARY.md).

## Milestone 4: Broader data and more asset support

### Scope
- better equity fundamentals
- more crypto exchange data
- ETF / macro context layers
- richer novelty comparison
- broader robustness checks
- universe diversification (mid-caps, longer horizons, international)
  to find a regime where the harness's gates promote real factors

## Milestone 5: Optional cloud migration

### Scope
- remote storage
- scheduled ingestion
- remote experiment batches
- shared team infrastructure

### Note
Cloud is not a first milestone requirement.
