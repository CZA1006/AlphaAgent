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

## What works today (post Round 4–6)

The full agent loop runs end-to-end on real data:

```
LLM proposer → DSL compile → walk-forward + embargo evaluator
            → 6-gate judge → trail-stamped artifact + cycle report
```

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

### Multi-factor combination (Round 6)

`scripts/combine_factors.py` takes N DSL expressions and produces a basket via rank aggregation, z-score average, or equal weight. Returns per-factor + basket IC / rank-IC plus the average pairwise rank correlation so the operator can see whether the combination should have helped.

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

For the per-round design notes (4A.1 through 4J + Round 5 + Round 6), see [docs/ROUND4_TO_6_SUMMARY.md](docs/ROUND4_TO_6_SUMMARY.md). Round 3 closeout lives in [docs/ROUND3_SUMMARY.md](docs/ROUND3_SUMMARY.md).

## What we're not doing yet

- live trading execution
- complex multi-agent debate
- high-frequency order-book strategy logic
- cloud-native production deployment
- UI polish
- broad market coverage before the core loop demonstrates a real-data PROMOTE_CANDIDATE
