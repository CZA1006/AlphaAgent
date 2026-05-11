# Repository Structure

## Root

- `README.md` — project overview + status
- `AGENTS.md` — coding-agent operating contract
- `ARCHITECTURE.md` — system design
- `ROADMAP.md` — milestones (current: Round 6 complete, Round 7+ open)
- `TASKS.md` — historical Round-1-2 task list (kept for traceability)
- `ACCEPTANCE_CRITERIA.md` — what done means at each milestone
- `DATA_PLAN.md` — data sources, storage, point-in-time discipline
- `CLAUDE_CODE_GUIDE.md` — how Claude Code should work in this repo
- `CLAUDE_CODE_START_PROMPT.md` — opening prompt for Claude Code sessions
- `CODEX_REVIEW_GUIDE.md` — how Codex should review this repo
- `HERMES_INTEGRATION_PLAN.md` — Hermes runtime adapter notes
- `IMPLEMENTATION_SEQUENCE.md` — historical Round-1 implementation order
- `Makefile` — `make doctor`, `validate-strict`, `list-{factors,cycles,trails}`,
  `refine-factor`, `audit`, `smoke`, `check`, `check-full`, `combine-factors`
- `docs/ROUND3_SUMMARY.md` — Round 3 closeout
- `docs/ROUND4_TO_6_SUMMARY.md` — per-sub-round design notes for 4A.1
  through 4J + Round 5 + Round 6
- `docs/LOCAL_TESTING.md` — real-API local-testing guide
- `docs/BACKENDS.md` — memory vs SQL registry backend selection

## Main code directories

### `vendor/hermes-agent/`
Pinned Hermes runtime substrate.

### `alpha_harness/`
Own code lives here.

Top-level entry:
- `service.py` — `AlphaHarnessService` domain interface (compiler +
  evaluator + judge composition)
- `regimes.py` — `StrictRegime` / `LenientRegime` named-regime
  presets (Round 5)

Subpackages:

- `orchestrator/` — `ResearchOrchestrator`, `RefinementRunner` (with
  trail-aware guard from 4G), deterministic mutation templates
- `proposer/` — `HypothesisProposer`, memory-digest builder (4A.4)
- `refiner/` — `RefinementBrief` + brief-aware mutation prioritisation
  (4A.6)
- `llm/` — `LLMClient` protocol, `OpenRouterClient`, `MockLLMClient`,
  `LoggingLLMClient`, `BudgetedLLMClient` + `TokenBudget` (4A.1)
- `hermes_boundary/` — `HarnessAgentAdapter` + boundary contracts
- `evaluators/`
  - `signal_quality.py` — IC / RankIC / quantile-spread evaluator
  - `walk_forward.py` — fold splitter + aggregator with embargo (4B+4D)
  - `neutralize.py` — sector / beta neutralisation (4A.3)
  - `portfolio.py` — Sharpe / drawdown / hit-rate / tail concentration
    (4C)
  - `promotion_judge.py` — six-gate judge: data, profile,
    sign-consistency (4A.3), walk-forward stability (4B),
    tail-concentration (4C), holdout decay (4E)
  - `novelty.py` — canonical-AST novelty checker
- `combination/` — Round 6: rank-aggregate / z-score-average /
  equal-weight basket combiners + pairwise rank-correlation
- `factors/` — DSL parser, canonical AST, executor, compiler
- `retrieval/` — related-experiment retrieval
- `registries/` — experiments / hypotheses / memory (memory + sql
  backends behind protocols)
- `memory/` — lineage memory writer
- `artifacts/` — Round 4 on-disk stores:
  - `promoted.py` — `PromotedArtifactWriter` (per-factor JSON +
    `_index.jsonl`, schema_version=3 with promotion_trail)
  - `trail_registry.py` — standalone `TrailRegistryWriter` (4J)
- `audit/` — `assert_clean_imports` (no `hermes.*` in harness),
  `assert_no_outbound_io_in_evaluators` (4A.9)
- `reports/`
  - `cycle_report.py` — per-cycle audit JSON + `list-cycles` reader
  - `validation.py` — `StrictValidationReport` + per-gate failure
    classifier (Round 5)
- `data/` — synthetic / parquet / polygon equity loaders, ccxt crypto,
  Polygon rate-limit guard
- `schemas/` — Pydantic models (`FactorSpec` with lineage fields from
  4A.7, `PromotionTrail` from 4F, `EvaluationBundle`, `HoldoutPolicy`,
  …)
- `db/` — connection + Postgres glue
- `skills/` — skill registry stubs (not yet on the main path)

Note: `agents/` and `tools/` are NOT part of Alpha Harness. Those names
belong to the Hermes runtime layer. The Hermes adapter lives in
`alpha_harness/hermes_boundary/` and exposes typed contracts only — it
does **not** import Hermes internals into business logic. The
`make audit` target enforces this statically.

### `configs/`
- `universes/sp50.txt` — 50 SP large-caps (the Round 4A.2 backfill
  target)
- `universes/sp50_sectors.csv` — sector tags for sector-neutralisation
- evaluation thresholds, promotion policies live in `regimes.py` now
  rather than YAML

### `scripts/`
Operator surfaces:

- `autonomous_cycle.py` — full theme → proposals → cycles → refinement
  loop with `--mock-llm` / OpenRouter / SQL / artifacts wiring
- `validate_strict.py` — Round 5 strict-regime validation harness with
  `--llm openrouter`, `--n-cycles N`, `--regime {strict,lenient}`,
  `--memory-depth`
- `refine_factor.py` — Round 4H seeded refinement CLI
- `combine_factors.py` — Round 6 multi-factor combination
- `list_factors.py` — promoted-factor zoo browser
  (`--lineage`, `--diff-trails`, `--show-trail`)
- `list_cycles.py` — cycle audit report browser
- `list_trails.py` — standalone trail registry browser
- `doctor.py` — preflight (env, Postgres, audit, regime resolution,
  mock-LLM smoke)
- `backfill_parquet.py` — Polygon → local Parquet equity backfill
- `bootstrap_db.py` — Postgres schema setup
- `run_research_cycle.py` — single-hypothesis cycle (legacy, kept for
  the doctor's quick-real path)

### `tests/`
- `unit/` — ~600 unit tests covering schemas, evaluators, judge gates,
  refinement, artifacts, trails, audit, validation reports, combination
- `integration/` — `@pytest.mark.integration`: autonomous-cycle smoke,
  strict-validation smoke, SQL orchestrator + registries (skipped
  unless Postgres is up)
- `e2e/` — single end-to-end research cycle
- `helpers/` — stubs (`StubSignalQualityEvaluator`)

### `artifacts/`
Generated outputs (gitignored at the root):
- `promoted/` — per-factor JSON + index from `PromotedArtifactWriter`
- `trails/` — standalone `PromotionTrail` registry from
  `TrailRegistryWriter`
- `reports/` — per-cycle audit JSONs from `CycleReportWriter`
- `validations/` — per-cycle strict-regime reports from
  `StrictValidationReportWriter`
- `llm_calls/` — per-cycle LLM call logs (`LLMCallLogger`)

Note: `alpha_harness/artifacts/` (the Python module) is *not* gitignored
— only root-level `/artifacts/` is. The `.gitignore` is anchored
explicitly so the module stays tracked.

### `data/`
Local data storage:
- `silver/equities/` — Polygon Parquet backfill
- `bronze/`, `gold/` — reserved for future data-pipeline stages
- `raw/` — for unprocessed downloads

## Non-goals for this structure

Do not start with:
- massive microservice sprawl
- too many nested packages
- premature cloud deployment directories
- live execution infrastructure
