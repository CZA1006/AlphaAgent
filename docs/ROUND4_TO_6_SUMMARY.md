# Round 4 → Round 6 — closing the learning loop

Round 3 (see `docs/ROUND3_SUMMARY.md`) shipped a research loop that
*proposes and scores*. Rounds 4 → 6 turned it into one that *learns
from what it proposed*, *captures the regime that produced every
decision*, and *replays / diffs / combines* historical factors — all
without breaking the deterministic-truth boundary.

Sub-rounds are listed in shipped order. Each one is a separate commit on
`main`; every one is small and rolled back independently if needed.

---

## Round 4A — harness scaffolding (10 sub-rounds)

### 4A.1 — cost / rate-limit / call-hygiene guardrails
- `BudgetedLLMClient` enforces per-cycle token + USD caps; raises
  `BudgetExceededError` (exit code 3) when exceeded.
- `LLMCallLogger` writes append-only JSONL to
  `artifacts/llm_calls/{cycle_id}.jsonl` — previews + SHA-256
  fingerprints only, never raw prompts or keys.
- Polygon loader pacing + 429 backoff via `request_with_retry`.

### 4A.2 — real research universe via Parquet backfill
- `scripts/backfill_parquet.py` + `make backfill-sp50` populate
  `data/silver/equities/{symbol}.parquet` for the 50 SP large-caps in
  `configs/universes/sp50.txt`.
- Idempotent + resumable: cache predicate allows 7-day end-side slack.
- Free-tier 500-row truncation documented and enforced as the default
  start-date (today − 2 years).

### 4A.3 — richer evaluator
- Sector / beta / both neutralisation modes (`alpha_harness/evaluators/neutralize.py`).
- Multi-horizon labels (`extra_horizons` on `LabelDefinition`); judge
  enforces `ic_sign_consistent_horizons >= 2`.
- Cost-adjusted spread: `net_quantile_spread = quantile_spread -
  (cost_bps/10000) * turnover`.

### 4A.4 — memory-aware proposer
- `alpha_harness/proposer/memory.py::build_memory_digest()` produces a
  ≤ 1.2 KB summary of recent experiments (rolling decision counts,
  promoted expressions, top rejection categories, fingerprints).
- Threaded into `ProposalRequest.prior_memory` and rendered as a
  "What has already been tried" prompt section.

### 4A.5 — promotion artifacts + factor zoo
- `PromotedArtifactWriter` writes `artifacts/promoted/{factor_id}.json`
  (atomic via `os.replace`) plus `_index.jsonl`.
- `scripts/list_factors.py` browses the zoo with `--sort-by`,
  `--since`, `--limit`, `--json`.

### 4A.6 — targeted refinement via `RefinementBrief`
- New `alpha_harness/refiner/` module with `RefinementBrief` that
  captures *which* judge gate borderlined or failed, with margins +
  flags (`weak_cross_sectional`, `turnover_high`, `cost_drag_large`,
  `sign_inconsistent`).
- `propose_mutations(expression, brief=...)` reorders mutation
  candidates so the most-targeted edit runs first within
  `max_variants_per_step`.
- `FactorSpec.parent_factor_id` + `refinement_round` for lineage.

### 4A.7 — lineage-aware factor zoo
- Promoted artifact + index row carry lineage fields.
- `scripts/list_factors.py` grew `--lineage` (inline columns + tree
  view), `--min-refinement-round`, `--max-refinement-round`.
- `ResearchOrchestrator.summary()` includes
  `refinement_rounds_seen: {round: count}` histogram.

### 4A.8 — cycle audit reports
- `alpha_harness/reports/cycle_report.py::CycleReport`,
  `CycleReportWriter` mirrors the promoted-artifact contract (atomic
  JSON + `_index.jsonl`).
- Captures per-experiment thumbnails (with lineage), decision counts,
  refinement-round histogram, optional `BudgetSnapshot`.
- `scripts/list_cycles.py` browses with `--since`, `--limit`, `--json`.

### 4A.9 — runtime scope auditors
- `alpha_harness/audit/imports.py` walks every `.py` under
  `alpha_harness/` with `ast.parse` and rejects:
  - `hermes.*` / `runtime.*` imports (AGENTS.md rule #8)
  - `requests` / `urllib` / `httpx` / `subprocess` / `openai` /
    `anthropic` / `alpha_harness.llm` imports under `evaluators/`
- `make audit` wired into `make check`.
- `scripts/doctor.py` probes the auditors at preflight.

### 4A.10 — end-to-end smoke marker
- `tests/integration/test_autonomous_smoke.py` drives the full
  autonomous-cycle stack against tmp-scoped artifact dirs.
- New Makefile targets: `make smoke`, `make check-full` (= `check` +
  `smoke`).

---

## Round 4B — walk-forward stability gate

- `alpha_harness/evaluators/walk_forward.py::WalkForwardEvaluator`
  wraps any inner `FactorEvaluator`. Splits the eval window into rolling
  folds (default 4 folds × 60 days × 20-day step), evaluates each, and
  aggregates per-fold means + `fraction_positive_rank_ic`.
- Judge gate: when `metadata.walk_forward.n_folds >= 2`, require
  `fraction_positive_rank_ic >= min_fraction_positive_folds` (default
  0.6); otherwise REJECT as `WEAK_SIGNAL` with a clear detail.
- Single-fold and pre-4B bundles bypass the gate.
- CLI: `--walk-forward`, `--n-folds`, `--fold-size-days`, `--step-days`
  on `autonomous_cycle.py`.

---

## Round 4C — risk-aware portfolio metrics

- `alpha_harness/evaluators/portfolio.py` exposes
  `compute_long_short_returns()` + `compute_portfolio_metrics()`:
  Sharpe (annualised √252), max drawdown, hit rate, **tail
  concentration** (sum of top-3 days / total), n_periods.
- `SignalQualityEvaluator` populates `metadata["portfolio"]` and now
  also fills `EvaluationBundle.sharpe`.
- Judge gate: REJECT (`OTHER`) when `tail_concentration > 0.5`. Bundles
  without portfolio metadata bypass the gate.
- `ExperimentThumbnail` (cycle reports) surfaces sharpe / max_drawdown
  / hit_rate.

---

## Round 4D — calendar-aware embargo + purged folds

The 4B walk-forward had a subtle lookahead: with a 5-day forward-return
label, fold N's last 4 labelled days overlap fold N+1's signal window.

- `WalkForwardConfig.embargo_days` (default `None` → auto-derived from
  `lag_bars + forecast_horizon_bars`) trims the *end* of every fold.
- `min_fold_days` (default 20) purges folds whose post-embargo span
  shrinks too far. Purged count surfaces in
  `metadata.walk_forward.purged_folds`.
- `fold_windows()` exposes the purged spans for tests + tooling.
- `ExperimentThumbnail.walk_forward` carries the embargo audit into
  cycle reports.

---

## Round 4E — out-of-sample holdout decay

- `HoldoutPolicy` + `HoldoutStrategy` enum on `EvaluationRequest`.
  Default `NONE`; `TAIL` carves the trailing `holdout_fraction` of
  the eval window.
- `SignalQualityEvaluator` runs an in-sample pass + a holdout pass
  with `policy=NONE` (recursion bounded). Holdout metrics + `decay_ratio
  = holdout_rank_ic / in_sample_rank_ic` land under
  `metadata["holdout"]`.
- Judge gate: REJECT (`WEAK_SIGNAL`) on rank-IC sign-flip *or*
  `decay_ratio < min_holdout_decay_ratio` (default 0.5). Bundles
  without holdout metadata bypass.
- `ExperimentThumbnail.holdout` carries a slim `{rank_ic, decay_ratio,
  holdout_start, holdout_end}` block.
- CLI: `--holdout-fraction`, `--holdout-strategy`.

---

## Round 4F — promotion-trail reproducibility snapshot

After 5 judge-gate additions (4A.3, 4B, 4C, 4D, 4E) plus 4 evaluator
knobs, reading the registry today and 6 months from now could return
different decisions for the same factor. Trails close that gap.

- `PromotionTrail` (Pydantic, `alpha_harness/schemas/experiment.py`)
  captures every immutable evaluator + judge knob: neutralize mode,
  sector_map hash, cost_bps, label horizons, holdout policy,
  walk-forward sizing, all four judge thresholds.
- `PromotionTrail.from_inputs()` computes a 16-char SHA-256
  `trail_id`. Identical configs collapse to the same id.
- `JudgmentDetail.promotion_trail` and
  `ExperimentRecord.promotion_trail` carry the trail end-to-end.
  Populated only on PROMOTE_CANDIDATE.
- `PromotedArtifactWriter` bumps schema_version to 3, writes the full
  block into the per-factor JSON and `trail_id` into the `_index.jsonl`
  row.
- `scripts/list_factors.py` grows `--trail-id <id>` filter and
  `--show-trail` dump.

---

## Round 4G — trail-aware refinement guard

- Module-level helper `trail_status(record, current_trail_id)` returns
  `'match' | 'mismatch' | 'legacy' | 'unset'`.
- `RefinementRunner.__init__` takes optional `judge_thresholds` dict.
  When supplied, the runner computes the current `trail_id` once per
  invocation.
- New public method `RefinementRunner.refine_record(seed_record,
  eval_request)`:
  - REFINE seeds expand normally
  - PROMOTE seeds compare trails: match → skip with `regime_skips`,
    mismatch → log "regime drift" and proceed, legacy/unset →
    proceed defensively
- `RefinementResult` exposes `current_trail_id`, `regime_skips`,
  `trail_mismatches`.

---

## Round 4H — seeded refinement CLI

- `alpha_harness/artifacts/promoted.py::record_from_payload(payload)`
  rehydrates an `ExperimentRecord` from a v3 artifact JSON. Legacy
  v1/v2 payloads yield records with `promotion_trail=None`.
- `scripts/refine_factor.py`:
  - `--factor-id <id>` loads the seed
  - `--cost-bps`, `--neutralize`, `--holdout-fraction`, etc.
    let the operator dial in a new regime
  - drives `RefinementRunner.refine_record(seed, request)` against
    synthetic price data
  - prints structured summary or `--json` showing seed_trail_id,
    current_trail_id, trail_status, regime_skips, child decisions
- `make refine-factor` mirrors `make list-factors`.
- Doctor probes that `scripts.refine_factor` imports cleanly.

---

## Round 4I — promotion-trail field-level diff

- `PromotionTrail.diff(other)` returns `{field: (self_value,
  other_value)}` for every differing field. Excludes `trail_id` (it's a
  hash). Tuple-swap symmetric.
- `scripts/refine_factor.py` prints a "Trail diff (seed → current)"
  block on mismatch; JSON mode includes `trail_diff` map.
- `scripts/list_factors.py` grows `--diff-trails A B`.

---

## Round 4J — promotion-trail registry

Trails (4F) lived only inside per-factor JSONs. Answering "which trails
have we ever used?" required scanning every promoted file.

- `alpha_harness/artifacts/trail_registry.py::TrailRegistryWriter`
  appends to `artifacts/trails/_index.jsonl` on first appearance of a
  `trail_id` (writing the full `PromotionTrail` JSON to
  `{trail_id}.json`); updates an existing row's `factor_ids` list when
  a new factor promotes under the same trail.
- `read_trails()`, `read_trail(trail_id)` helpers.
- `PromotedArtifactWriter` takes optional `trail_registry=` kwarg.
- `scripts/list_trails.py` lists rows (newest first), `--json`,
  `--diff A B` (re-uses 4I).
- Doctor probe checks the trail-dir for writability + schema integrity.

---

## Round 5 — strict-regime real-data validation

The 6-gate judge stack worked in unit tests but had never produced a
real-data PROMOTE_CANDIDATE under all gates simultaneously. Round 5
made that the headline workflow.

### 5.0 — `StrictRegime`
- `alpha_harness/regimes.py` introduces a frozen dataclass bundling
  every evaluator + judge knob (sector neutralize, cost_bps=5,
  multi-horizon labels, walk-forward + embargo, holdout 20%, all 4
  judge thresholds) into one immutable + hashable config.
- `STRICT_REGIME` is the canonical instance.
- `get_regime(name)` looks up registered regimes.

### 5.0 — strict-validation report
- `alpha_harness/reports/validation.py::StrictValidationReport`:
  per-cycle JSON capturing `cycle_id`, `regime_trail_id`, counts +
  per-gate rejection breakdown.
- `classify_failure(detail)` parses every `PromotionJudge` failure
  string into one of 11 canonical gate names so
  `n_rejected_by_gate` aggregates cleanly.
- Mirrors `PromotedArtifactWriter`'s atomic-write + `_index.jsonl`
  contract.

### 5.0 — `scripts/validate_strict.py`
- Three data paths: `--data-source synthetic` (no keys),
  `parquet` (after `make backfill-sp50`), `polygon` (live).
- `--llm openrouter` constructs the full `OpenRouterClient →
  LoggingLLMClient → BudgetedLLMClient` stack identical to
  `autonomous_cycle`. Mock LLM is the default.
- `BudgetExceededError` exits 3, `OpenRouterError` exits 4, both
  with clean error messages.

### 5.1 — `LENIENT_REGIME` + `--regime` flag
- The strict run rejected 30/30 LLM-proposed factors at the IC
  gate. Halving the IC / rank-IC bar lets near-miss factors through
  to the deeper gates.
- `LENIENT_REGIME(ic=0.01, rank_ic=0.015, qs=0.0025, min_periods=40)`
  — same walk-forward, embargo, holdout, tail-concentration as strict.
- Re-running 5-cycle DeepSeek under `--regime lenient` fired
  tail_concentration twice and walk_forward_stability once — the
  first real-data evidence those gates work end-to-end.

### Multi-cycle with proposer memory
- `--n-cycles N` runs N back-to-back cycles, sharing the experiment
  registry so the Round 4A.4 memory digest grows. Cycle IDs auto-suffix
  with `-cNN`.
- `--memory-depth` / `--no-memory` mirror `autonomous_cycle`.
- `_print_multi_summary` aggregates per-cycle counts plus a cumulative
  rejection-by-gate breakdown.

### Real-data findings
- 60 LLM-proposed factors evaluated across strict + lenient regimes
  on SP-50 / 2024-04 → 2026-04.
- 0 promotions across all 60 (strict + lenient combined).
- 4 of 6 judge gates fired in production: `threshold_ic`,
  `threshold_rank_ic`, `threshold_quantile_spread`,
  `tail_concentration`, `walk_forward_stability`. Sign-consistency
  and holdout-decay never triggered (no factor cleared the in-sample
  IC bar by enough to reach them).
- The strict regime's IC gate is correctly correlated with downstream
  failure, not over-aggressive — every factor that passed it under
  lenient died on a deeper gate.
- The agent's exploration narrows under memory pressure: cycles 2–5
  converged on a `rank(price-vwap) * zscore(volume)` meta-pattern
  even after seeing it consistently rejected.

---

## Round 6 — multi-factor combination

Individual factors that fail the strict regime sometimes survive when
combined. Round 6 wires the simplest viable test.

- `alpha_harness/combination/combiner.py`:
  - `compute_signal(expression, df)` — DSL → `pd.Series`
  - `combine_signals(signals, timestamps, method)` with
    `RANK_AGGREGATE` (default — Borda count, robust to outliers),
    `ZSCORE_AVERAGE`, `EQUAL_WEIGHT`
  - `pairwise_rank_corr(signals, timestamps)` — diagnostic showing
    *why* combination did or didn't help
- `scripts/combine_factors.py`:
  - `--expr` (repeat) or `--expressions-file` accept input
  - `--method` picks the combiner
  - `--regime` picks the threshold bar (strict/lenient)
  - per-factor + basket table + average pairwise rank-corr
- First real-data test (5 mixed factors, SP-50, rank-aggregate,
  strict): basket IC = -0.019, avg pairwise rank-corr = +0.05.
  The plumbing works; manufacturing a combination that promotes is
  now an experiment, not an engineering task.

---

## What's open (Round 7+)

1. **A regime where something promotes.** SP-50 / 2024–2026 / 5-day
   horizon doesn't yield. Likely candidates: mid-caps, longer
   horizons (20d), international equities, or a different DSL
   primitive (sector-relative, cross-asset).
2. **Persistent cross-cycle factor zoo for the combiner.** Right now
   `combine_factors` takes expressions on the command line. A
   `--from-cycle <cycle_id>` flag would pull from
   `validate_strict`'s registry dump.
3. **Hermes runtime actually driving the loop.** The
   `HarnessAgentAdapter` boundary is in place; the runtime that calls
   `run_theme` from a live agent loop is not yet wired.
4. **Regime detection / live monitoring.** Track promoted factors'
   rolling out-of-sample IC; surface "regime change" when a previously
   promoted trail's holdout decay crosses a threshold.
