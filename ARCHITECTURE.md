# AlphaAgent Architecture

## High-level decision

We are using:

- **Hermes runtime substrate** for agent execution plumbing
- **Alpha Harness** for quant-native research logic

We are **not** directly turning Hermes into a quant system by stuffing quant behavior into prompts or generic skills.

## System layers

### 1. Entry surfaces
Examples:
- CLI
- Python scripts
- notebooks
- later: internal API or dashboard

### 2. Hermes runtime layer
Responsibilities:
- agent runtime loop
- prompt assembly
- provider/model abstraction
- session plumbing
- memory hooks
- tool invocation hooks

This layer should remain as close as possible to upstream Hermes behavior unless there is a very strong reason to modify it.

### 3. Alpha Harness layer
Responsibilities:
- research orchestration
- market state handling
- hypothesis lifecycle
- factor specification lifecycle
- deterministic evaluation
- experiment logging
- failure taxonomy
- memory and skill promotion

This layer is the main source of long-term product differentiation.

### 4. Deterministic core
This is where quantitative truth lives.

It should include:
- data loaders
- point-in-time joins
- factor DSL execution
- neutralization/transforms
- evaluators
- report builders
- registry persistence

### 5. Registries and memory
Long-lived structured stores for:
- hypotheses
- factors
- experiments
- skills
- memories
- market states

## Design principles

### Principle A: hard tools over soft prompts
Anything that decides quantitative truth must live in code, not only in prompts.

### Principle B: reusable schemas
Core entities must have typed schemas.
Examples:
- Hypothesis
- FactorSpec
- ExperimentRecord
- EvaluationBundle
- Skill
- RegimeState

### Principle C: research loop before autonomy
The first goal is a trustworthy research loop.
Only after that works should we push more autonomy.

### Principle D: memory is structured
Memory should not be only chat history.
We need structured research memory, including:
- success patterns
- failure patterns
- experiment lineage
- promotion history
- meta-policy notes

## Composite factors (Round 8 → 9)

Round 6 introduced multi-factor combination as an operator one-shot.
Rounds 8 and 9 turned baskets into first-class registry citizens
without inventing a new evaluation pipeline:

- **`FactorSpec.composite_recipe`** — optional `CombinationRecipe`
  field on the existing factor schema.  Non-`None` ⇒ the factor is
  a basket; the DSL `expression` becomes a placeholder
  `"<composite:{recipe_id}>"`.
- **`SignalQualityEvaluator.evaluate`** — one-line dispatch on
  `composite_recipe`.  Composites go through
  `execute_composite(recipe, df)` (a thin wrapper around
  `compute_signal` + `combine_signals`).  Both paths land at the
  shared `evaluate_precomputed_signal`, so every Round 4 gate
  (walk-forward, embargo, holdout, tail concentration, sign
  consistency) works for composites automatically.
- **`recipe_id`** — SHA-256 of `(method, sorted canonical-AST
  hashes of components)`.  Permuted components collapse to the
  same id, so the novelty check can't be tricked by reordering.
- **`combine_factors --promote`** — when a basket clears the
  regime, writes a `PromotedArtifact` (with the recipe in the
  payload) + a `PromotionTrail`, and the deterministic
  `factor_id = composite_{recipe_id}_{trail_prefix}` makes
  re-promotion idempotent.
- **Proposer memory** reads the durable
  `artifacts/promoted/_index.jsonl` and surfaces recent composites
  in the prompt, so the next cycle's LLM sees what's already been
  promoted.
- **`RefinementRunner._expand_composite`** mutates one component
  at a time via the existing scalar mutator and rebuilds the
  recipe — composites participate in the iterative search the
  same way scalar factors do.

The deliberate design choice: **a parallel field, not a new DSL
node.**  Adding a `combine()` operator to the DSL would have been
~400 lines of parser / AST / executor / refiner work; the parallel-
field approach is ~150 and leaves every existing path untouched.

## Current directory layout (post-Round-3)

```text
alpha-agent/
├── vendor/
│   └── hermes-agent/
├── alpha_harness/
│   ├── service.py           # domain service interface
│   ├── config.py            # BackendConfig + PostgresSettings
│   ├── orchestrator/        # research loop + refinement runner
│   ├── proposer/            # HypothesisProposer (LLM-facing)
│   ├── llm/                 # LLMClient protocol + OpenRouter + Mock
│   ├── hermes_boundary/     # adapter + ResearchCycle/ThemeCycle contracts
│   ├── evaluators/          # deterministic IC / RankIC / quantile spread / judge
│   ├── factors/             # DSL compiler + canonical AST + executor
│   ├── retrieval/           # related-experiment retrieval
│   ├── registries/          # experiment / hypothesis / memory (memory + sql)
│   ├── memory/              # lineage memory schema + helpers
│   ├── skills/              # skill registry stubs (not yet on main path)
│   ├── data/                # synthetic + parquet + polygon loaders
│   ├── reports/             # report builders
│   ├── schemas/             # Pydantic/typed core entities
│   └── db/                  # SQLAlchemy / psycopg connection glue
├── configs/
├── scripts/                 # run_research_cycle, autonomous_cycle, doctor, bootstrap_db
├── tests/
└── artifacts/
```

Notes:

- `agents/` and `tools/` are intentionally absent from Alpha Harness.
  Those are Hermes runtime concepts. The `hermes_boundary/` package is
  the *only* place Alpha Harness meets Hermes; it exposes typed
  request/response contracts, nothing more.
- The LLM lives strictly on the proposal side of the boundary:
  `proposer/` calls `llm/`. No evaluator, judge, compiler, or registry
  ever calls an LLM. This invariant is what lets every quantitative
  decision remain deterministic and reproducible.

## First milestone architecture scope

Must include:
- local project bootstrapping
- domain service interface (Alpha Harness exposes typed services; Hermes adapts into them)
- local Postgres for registries
- DuckDB + Parquet data path
- minimal factor DSL
- deterministic evaluator
- experiment registry
- memory retrieval stub

Can wait until later:
- live brokerage/exchange connectivity
- full multi-agent architecture
- cloud deployment
- distributed compute
- UI layer
