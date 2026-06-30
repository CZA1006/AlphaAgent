# AlphaAgent

AlphaAgent is a quant-native research system built on top of the Hermes runtime substrate.

The goal is **not** to build a generic agent that imitates a human analyst step by step. The goal is to build a **self-improving alpha research harness** that can:

- propose research hypotheses
- translate them into safe factor specifications
- run deterministic evaluation
- store experiment history
- classify failures
- distill reusable research skills
- improve its own research efficiency over time

## Initial market focus

Phase 1 focuses on:

- US equities
- Crypto spot and perp markets

The data model is designed to support additional asset classes later (ETFs, futures, options metadata, FX, rates, commodities).

## Core principle

**Hermes handles runtime orchestration. Alpha Harness handles quant reasoning.**

This repository keeps those responsibilities clearly separated, and a static auditor (`make audit`) blocks any inward import from `hermes.*` / `runtime.*` into the harness.

## What works today (post Round 4–9)

The full agent loop runs end-to-end on real data and **closes back on itself** — composites promoted by one cycle feed the proposer's prompt in the next:

```
LLM proposer → DSL compile → walk-forward + embargo + holdout evaluator
            → 6-gate judge → trail-stamped artifact + cycle report
            → combine_factors --promote → composite registry citizen
            → next cycle's proposer memory digest
```

Validated end-to-end against real DeepSeek + Qwen on real Polygon SP-50
**and** real Bloomberg HK IPO tick data (in GCP BigQuery).
The journey through six case studies + one audit:

1. [`docs/CASE_STUDY_2026Q2.md`](docs/CASE_STUDY_2026Q2.md) — first end-to-end run, reported a positive basket result.
2. [`docs/AUDIT_LOOK_AHEAD.md`](docs/AUDIT_LOOK_AHEAD.md) — systematic look-ahead audit found 3 CRITICAL bugs (combiner bypassed `HoldoutPolicy`, `FactorThumbnail` dropped the holdout block, `SignalQualityEvaluator` inflated IC via per-fold signal recomputation).  All fixed.
3. [`docs/CASE_STUDY_HONEST.md`](docs/CASE_STUDY_HONEST.md) — disjoint train (2024-04-19 → 2025-04-18) / test (2025-04-19 → 2026-04-17) re-run with all three fixes in place.  Basket clears strict in-sample AND out-of-sample (Y1 IC `+0.033` / rank_IC `+0.049`; Y2 IC `+0.058` / rank_IC `+0.053`).
4. [`docs/CASE_STUDY_HONEST_V2.md`](docs/CASE_STUDY_HONEST_V2.md) — Y1 window slid by ~2 months (selection: 2024-06-25 → 2025-05-21).  In-sample looks *better* — basket **sign-flips out-of-sample** (Y2 IC `−0.023`, rank_IC `−0.014`).
5. [`docs/CASE_STUDY_HONEST_V3.md`](docs/CASE_STUDY_HONEST_V3.md) — same window as v2, **different LLM** (Qwen-2.5-72B).  Different factor family, **also sign-flips out-of-sample** (Y2 IC `−0.036`, rank_IC `−0.043`).

**Joint verdict across 3 US studies:** 1 positive Y2, 2 negative Y2 — the US daily SP-50 alpha is real-but-fragile, and an honest "this loop produces alpha" claim requires a planned multi-run study.  What the US studies demonstrate is that the architecture honestly measures the question — not that the answer is "yes."

6. [`docs/CASE_STUDY_HK_IPO_MICRO.md`](docs/CASE_STUDY_HK_IPO_MICRO.md) — **the first signal to survive the full gauntlet.**  On real Bloomberg HK IPO data, DeepSeek proposed tick-derived **microstructure** factors (order-flow imbalance, spread, realized vol — information OHLCV bars cannot contain).  On a disjoint train/test split, **10/12 factors persisted out-of-sample** (p ≈ 1.9 % vs a no-edge null), and **4/12 stayed positive net of the real 78 bps IPO spread in a long-only, HSI-hedged form** — including the flagship `rank(ofi) - rank(rel_spread)`.  This is the first AlphaAgent result to clear disjoint-OOS **and** realistic cost **and** long-only-implementability at once.  Honest caveat: modest magnitude, hit rates ~50–59 %, and a ~40-day test window too short for confidence — the binding constraint is now **data quantity**, not the engine.  It also explains the operator's prior OHLCV null: order flow is new information.

### The judge stack (six gates)

Every cycle's promotion decision passes through six independently-validated gates, in order:

1. **data sufficiency** — `min_periods`, `min_assets`
2. **profile thresholds** — IC / rank-IC / quantile-spread minimums
3. **multi-horizon sign consistency** (Round 4A.3) — IC sign must hold across the configured forecast horizons
4. **walk-forward stability** (Round 4B + 4D) — at least 60% of (embargoed, purged) folds must be sign-consistent
5. **tail concentration** (Round 4C) — top-3 days cannot carry more than 50% of the gross long-short return
6. **out-of-sample holdout decay** (Round 4E) — last 20% of the window must agree in sign and decay no more than 50% from in-sample rank-IC

Any failure exits with a structured `FailureRecord` whose category and detail string land in the cycle report. Promotions stamp a `PromotionTrail` (Round 4F) — a SHA-256 of every evaluator + judge knob — so the on-disk factor zoo stays reproducible across config drift.

### Operator surface

Every research artifact is queryable from a CLI:

| Make target | What it does |
|---|---|
| `make doctor` | Preflight: env vars, Postgres reachability, audit, regime resolves |
| `make autonomous-mock` / `autonomous-real` | Full theme → proposer → cycles → refinement |
| `make validate-strict` | Strict-regime validation harness (Round 5) — supports `--llm openrouter`, `--n-cycles N`, `--regime {strict,lenient}` |
| `make refine-factor` | Replay refinement on a previously promoted factor under a new regime (Round 4H) |
| `make list-factors` | Browse the promoted-factor zoo (`--lineage`, `--diff-trails`, …) |
| `make list-cycles` | Browse cycle audit reports |
| `make list-trails` | Browse the standalone promotion-trail registry (Round 4J) |
| `make audit` | Static import auditor (no `hermes.*` in harness, no network in evaluators) |
| `make smoke` / `check-full` | End-to-end integration smoke + full quality gate |

### Agent loop on real data

`scripts/validate_strict.py --llm openrouter` is the production research entry point. It:

- loads parquet OHLCV (or live Polygon, or synthetic) for a chosen universe
- builds a strict / lenient `Regime` with all 6 judge gates active
- has the LLM proposer generate N candidates per cycle (memory-augmented after Round 4A.4)
- evaluates every candidate through walk-forward + embargo + holdout
- writes a `StrictValidationReport` with per-gate rejection counts
- repeats for `--n-cycles` so the proposer's memory digest grows over time

We've exercised this against 50 SP large-caps × 2 years of daily bars × 30+ LLM-proposed factors per regime, with all six judge gates confirmed firing in production. See [docs/LOCAL_TESTING.md](docs/LOCAL_TESTING.md) for end-to-end recipes.

### Multi-factor combination (Round 6 → 9)

`scripts/combine_factors.py` takes N DSL expressions and produces a basket
via rank aggregation, z-score average, or equal weight.  Returns per-factor
+ basket IC / rank-IC plus the average pairwise rank correlation so the
operator can see whether the combination should have helped.

Round 7 → 9 layered three things on top of the combination plumbing:

- **Round 7 — audit-grade reports.**  Every validation report now carries
  per-factor thumbnails (expression + headline metrics + gate name on
  reject) so downstream tools (notably the combiner) can reload the
  full evaluated set without re-running the cycle.  Round 7.1 fixed a
  measurement gap: the combiner now uses the same
  `WalkForwardEvaluator + regime` pipeline the validator does, so basket
  IC matches the validator's individual-factor IC byte-for-byte.
- **Round 8 — composites are first-class registry citizens.**
  `combine_factors --promote` writes a `PromotedArtifact` + `PromotionTrail`
  for the basket, with a stable `recipe_id` (SHA-256 of method + sorted
  canonical-AST hashes — permuted components collapse).  `FactorSpec`
  gained a `composite_recipe` field; the evaluator dispatches on it.
- **Round 9 — loop closure.**  Proposer memory digest reads the durable
  promoted-artifact index, so a basket promoted by `combine_factors` in
  one process shows up in the next `validate_strict` cycle's prompt.
  `RefinementRunner` learned a composite path: mutate one component at
  a time via the existing scalar mutator, rebuild the recipe, evaluate.
  `scripts/inspect_composite` is the read-only auditor (`--list` or
  `--recipe-id`).

Read the per-round design notes in
[`docs/ROUND7_TO_9_SUMMARY.md`](docs/ROUND7_TO_9_SUMMARY.md).  The
end-to-end case study (real DeepSeek + real Polygon SP-50, basket clears
strict on both gates with holdout-aware metrics) lives in
[`docs/CASE_STUDY_2026Q2.md`](docs/CASE_STUDY_2026Q2.md).

## Persistence backends

Registries (experiments, hypotheses, lineage memory) run against one of two backends:

- `memory` — default, zero setup, used by every test and local run.
- `sql` — Postgres-backed, opt-in via `--backend sql` or `ALPHA_AGENT_BACKEND=sql`.

The on-disk artifact stores (promoted factors, cycle reports, trail registry, validation reports) are JSON + JSONL files designed for jq pipelines and the read-only CLIs above. See [docs/BACKENDS.md](docs/BACKENDS.md).

## Reproducibility chain

Every promotion captures the regime that produced it via a `trail_id` hash. The chain:

- **Round 4F** — every PROMOTE writes its `PromotionTrail` into the artifact JSON (schema_version=3) and the index row.
- **Round 4G** — `RefinementRunner.refine_record()` refuses to mine factors whose lineage didn't validate under the *current* trail (regime drift detection).
- **Round 4H** — `make refine-factor ARGS="--factor-id <id> --cost-bps 5"` replays refinement on any historical factor under a new regime.
- **Round 4I** — `PromotionTrail.diff()` + `make list-factors --diff-trails A B` show field-level differences between two trails (`cost_bps: 2.0 → 5.0`).
- **Round 4J** — standalone `artifacts/trails/` registry; `make list-trails` lets operators browse / diff every regime ever used without knowing factor IDs.

## Running locally with real APIs

For wiring real OpenRouter / Polygon / Postgres keys into the stack — and the `make doctor` preflight that validates them — see [docs/LOCAL_TESTING.md](docs/LOCAL_TESTING.md). Short version:

```bash
cp .env.example .env && $EDITOR .env
make doctor && make run-mock          # baseline, no keys
make doctor-real && make run-real     # real LLM, synthetic data
make validate-strict ARGS="\
  --data-source parquet --universe configs/universes/sp50.txt \
  --start-date 2024-04-19 --end-date 2026-04-17 \
  --llm openrouter --n-cycles 5"      # full agent loop, real data
```

For the per-round design notes:

- [docs/ROUND3_SUMMARY.md](docs/ROUND3_SUMMARY.md) — Round 3 closeout
- [docs/ROUND4_TO_6_SUMMARY.md](docs/ROUND4_TO_6_SUMMARY.md) — 4A.1 through 4J + Round 5 + Round 6
- [docs/ROUND7_TO_9_SUMMARY.md](docs/ROUND7_TO_9_SUMMARY.md) — Round 7 thumbnails, 7.1 parity fix, Round 8 composite promotion, Round 9 loop closure + composite refinement + inspect
- [docs/CASE_STUDY_2026Q2.md](docs/CASE_STUDY_2026Q2.md) — first end-to-end run on real DeepSeek + Polygon SP-50 (pre-audit)
- [docs/AUDIT_LOOK_AHEAD.md](docs/AUDIT_LOOK_AHEAD.md) — look-ahead / leakage audit; 3 CRITICAL bugs found and fixed
- [docs/CASE_STUDY_HONEST.md](docs/CASE_STUDY_HONEST.md) — disjoint train/test re-run with all audit fixes; basket clears strict on both Y1 and Y2 out-of-sample

## What we're not doing yet

- live trading execution
- complex multi-agent debate
- high-frequency order-book strategy logic
- cloud-native production deployment
- UI polish
- broad market coverage before the core loop demonstrates a real-data PROMOTE_CANDIDATE
