# Project Status — AlphaAgent

> Single source of truth for "where the project actually is": what is
> built and proven, what is not, and what to do next.  Last refreshed
> after the three honest case studies and the 2026-07-14 bounded HK IPO
> loop, continuous-event, and OFI robustness audits.

For *how* the system works, read [`../ARCHITECTURE.md`](../ARCHITECTURE.md)
("How the agent does quant research") and
[`ROUND7_TO_9_SUMMARY.md`](ROUND7_TO_9_SUMMARY.md).  For the empirical
runs, read the four `CASE_STUDY_*` docs and
[`AUDIT_LOOK_AHEAD.md`](AUDIT_LOOK_AHEAD.md).

---

## One-paragraph summary

AlphaAgent is a self-improving quant-research harness on the Hermes
runtime.  An LLM proposes factor *expressions* from a theme; a safe
DSL compiles them; a deterministic walk-forward + embargo + holdout
evaluator scores them; a six-gate judge promotes / refines / rejects;
every promotion is reproducible via a config-hash trail; survivors
combine into baskets that can themselves be promoted and refined; and
promoted work feeds the next cycle's prompt.  The architecture runs
end-to-end against real DeepSeek + Qwen LLMs and real Polygon SP-50
data.  **What is proven is that the harness measures alpha honestly;
what is now also established is that on SP-50 daily OHLCV the loop does
*not* produce alpha on average** — the predeclared 12-cell rolling
robustness study (2026-07-15) found coin-flip Y2 outcomes and zero
strict clears across two LLMs and three windows
([`CASE_STUDY_ROBUSTNESS_SP50.md`](CASE_STUDY_ROBUSTNESS_SP50.md)).
The live alpha hypothesis is the HK IPO order-flow track.

---

## What we have achieved ✅

### Architecture & engineering

- **Clean two-layer split**, statically enforced: no `hermes.*`
  imports in the harness, LLM confined to `proposer/`.  `make audit`
  fails the build on violation.
- **Continuous integration (Productization P0 Stage 0)** — every push to
  `main` and every pull request runs the existing `make check` quality gate
  plus an independent integration smoke job on Python 3.11. The workflow
  uses a frozen lockfile, uv caching, read-only repository permissions, and
  requires no market-data or LLM secrets.
- **Typed market-pack foundation (Productization P0 Stage 1)** — immutable
  Pydantic contracts and a read-only JSON registry now describe HK IPO and
  US-equities data, DSL fields, mock presets, director topics, transitions,
  and SQL templates. The DSL, BigQuery loaders, SQL scripts, and LLM model
  selection now consume explicit configuration while retaining compatibility
  at existing CLI edges. A fixed synthetic panel pins the pre-migration data
  fingerprint and strict promotion trail id; a third-pack fixture proves
  pack load -> Parquet loader -> pack-only DSL field -> mock-LLM evaluation.
  The static audit blocks market literals throughout the generic core.
- **Pack-driven director and post-run policy (Productization P0 Stage 2)** —
  market packs now own director context, complete topic definitions, runner
  modules, and typed transition tables. The autonomous researcher accepts any
  registered market; HK IPO behavior is preserved while the US-equities pack
  proves topic selection and promotion transitions without market-id branches.
- **Typed SDK and artifact boundary (Productization P0 Stage 3)** —
  `alpha_harness.sdk` now exposes typed validation, combination, planning,
  autonomous-run, and artifact-query entry points. SDK validation and
  combination resolve one market pack and explicitly thread only that pack's
  DSL fields into compilation. `LocalArtifactStore` preserves the existing
  validation, promotion, trail, autonomous-run, and research-task paths and
  JSON bytes; legacy readers and writers round-trip through the abstraction.
  Four mock/synthetic golden-output tests pin the major CLI stdout contracts.
  P0 adds no HTTP server or serving dependency.
- **Safe factor DSL** — whitelisted fields + functions, typed AST, no
  `eval`/arbitrary code.  Deterministic, test-covered execution.
- **Continuous event proximity** — `event_decay(distance, half_life)` maps
  missing events to zero and decays from 1.0 by event distance. The HK IPO
  Director now requires a daily base signal plus this continuous interaction,
  and explicitly rejects another hard-Boolean-window search or unavailable
  `first_hour_*` fields on the daily loader.
- **Deterministic evaluator stack** — IC, rank-IC, quantile spread,
  turnover, cost-adjusted spread, Sharpe; sector/beta neutralization;
  multi-horizon labels.
- **Walk-forward + embargo + purge + global TAIL holdout** — calendar folds,
  embargo of `lag + horizon` days, then one trailing out-of-sample
  reservation. Judge-facing horizon and portfolio metadata is aggregated
  across folds rather than inherited from the first fold.
- **Six-gate promotion judge** — data sufficiency, profile thresholds,
  multi-horizon sign consistency, walk-forward stability, tail
  concentration, holdout decay. Schema v6 adds predeclared session-level
  multiple-hypothesis pressure: IC and rank-IC thresholds scale by the
  Bonferroni one-sided z-critical ratio (`1.6858×` for 18 proposal slots),
  while quantile spread remains an economic threshold. The family size,
  alpha, and multiplier are persisted in requests, reports, and trails.
  A fixed-snapshot seven-factor HK IPO OFI replay recorded `N=7` and
  `1.4895×`; all seven remained rejected by the same deeper gates
  (`tail_concentration=6`, `holdout_decay=1`), so the policy change did not
  manufacture a different conclusion for the current research lead.
- **Reproducibility chain** — `PromotionTrail` SHA-256 over every
  knob; trail-aware refinement guard; trail diff + registry.
- **Multi-factor combination + composites** — baskets are first-class
  `FactorSpec`s (`composite_recipe`), promotable, refinable, auditable,
  with order-invariant `recipe_id`s.
- **Durable proposer feedback loop** — in-process experiments and prior
  validation reports from the same point-in-time memory scope feed the next
  proposer cycle. The scope hashes the evaluation contract and input-panel
  contents; promoted composites also surface from their artifact index. A
  two-invocation CLI integration test verifies the cross-process path.
- **Typed cost-replay executor** — after event-conditioned discovery promotes
  candidates, the Director passes their exact validation cycle ids into a
  no-LLM, no-mutation replay at 15 bps. Schema-v7 validation reports capture
  candidate source, source cycles, panel fingerprint, and cost provenance;
  snapshot mismatches fail closed. Discovery promotions and replay survivors
  are counted separately.
- **Provider-reported LLM cost accounting** — OpenRouter's `usage.cost` is
  preserved through the typed response, call log, budget ledger, and validation
  report. Explicit token rates remain a fallback and reports distinguish actual
  cost calls from estimated calls.
- **Corrected bounded-loop acceptance (2026-07-14)** — DeepSeek completed three
  proposal cycles under the global-holdout evaluator and the then-current
  schema-v5 budget
  ledger: 18 proposals, 18 deterministic rejections, 0 promotions, 6,806
  tokens, and `$0.00296483` provider-reported cost across three actual-cost
  calls (zero estimated calls). Rejections were `threshold_ic=9`,
  `missing_metric=6`, `other=2`, and `threshold_quantile_spread=1`; policy
  opened event-truth review rather than spending more LLM budget. The follow-up
  five-check audit found 0 blocking issues and 280 review-backlog rows, then
  stopped completed. This supports event-gated cross-sectional sparsity as the
  immediate bottleneck, not a broken event-data alignment contract.
- **Round 10 composite-complement loop (2026-07-15)** — an explicit
  `--composite-complements` mode now asks the proposer for one scalar addition
  to an exact promoted basket, evaluates the augmented basket rather than the
  singleton, and rejects additions whose maximum absolute fold rank correlation
  exceeds 0.50,
  whose rank-IC lift is positive in fewer than 60% of walk-forward folds, or
  whose global-holdout lift is non-positive. Validation schema v7 persists the
  base recipe, component expression, incremental diagnostics, and exact
  composite recipe so 15 bps replay reconstructs the same factor. The mode
  fails closed when no eligible promoted composite exists.
- **Fixed-snapshot composite-anchor arbitration (2026-07-15)** — the
  predeclared seven-factor OFI smoothing family was ranked by training-fold
  persistence and replayed as a z-score top-4 basket on fingerprint
  `6bf7ac...` at 15 bps. Combination schema v3 now records the target/source
  fingerprints, source cycles, cost, proposal-family size, and Bonferroni
  multiplier. With `N=7` (`1.4895x`), the basket cleared adjusted IC and
  rank-IC (`+0.0898` / `+0.0901`) but was deterministically rejected because
  worst-fold tail concentration reached `1.05`; its components were also
  highly redundant (mean pairwise rank correlation `0.824`). No composite
  anchor was written or promoted.
- **Continuous-event search and deterministic OFI diagnosis (2026-07-14)** —
  two bounded DeepSeek runs tested 18 computable `event_decay` candidates for
  `$0.00445629` provider-reported cost: 0 promotions, 10 weak-IC rejects, and
  8 tail-concentration rejects, with no missing-field or missing-metric
  failures. A no-LLM base/event/composite decomposition showed that the event
  terms did not add robust incremental value; the signal body was smoothed
  OFI. A second no-LLM seven-factor gauntlet found monotone improvement from
  raw to 20-day OFI smoothing: turnover fell from 0.846 to 0.155, training
  rank-IC rose from +0.0528 to +0.0943, and global-holdout rank-IC rose from
  +0.0796 to +0.1567. None promoted: the strict evaluator takes the worst
  fold's tail concentration (0.95 for 20-day OFI), exposing subperiod
  fragility. Over the aggregate training window, the same ratio was only 0.14
  and became 0.21 after excluding each IPO's first five days, so debut spikes
  do not explain the failure. Adding `- rank(rel_spread)` inflated training IC
  but decayed sharply OOS. **Conclusion:** stop spending LLM budget on event
  interactions; retain 10/20-day OFI as unpromoted leads for deterministic
  regime and return-date attribution.
- **Episode-aware tail diagnostic (2026-07-15)** — portfolio metadata now
  splits multi-day forward labels into fixed-phase, non-overlapping cohorts and
  records the median/max share of positive return carried by each cohort's top
  three observations, plus the minimum positive cohort size. It is explicitly
  informational: `PromotionJudge` still reads only the original worst-fold
  `tail_concentration`. On the fixed HK IPO snapshot, aggregate episode shares
  were 0.470/0.408 for 10/20-day OFI, but each fold had only 3–4 positive
  observations in its thinnest phase and median top-three shares of 0.76–1.00.
  The overlap correction therefore confirms inadequate independent episode
  count rather than clearing the factors; both remain rejected.
- **Typed event-truth audit executor** — a read-only five-check BigQuery task
  writes generic research-task artifacts and feeds deterministic issue counts
  back to the post-run policy. The 2026-07-14 live smoke found 280 review rows
  but zero blocking evidence/date/alignment issues and full 77/77 prospectus
  plus allotment-announcement registry coverage.
- **Typed raw-tick materialization planner** — the Director can explicitly
  dispatch a versioned intraday-feature SQL contract frozen at 2026-06-26.
  The executor validates target/source/session/point-in-time rules, dry-runs
  only the SELECT body, and runs read-only nonpositive-tick QA by
  stock/date/event type. It has no write path: the candidate table is never
  created or replaced autonomously. BigQuery does not fully estimate external
  table scan cost, so the report marks the planner byte count incomplete. The
  2026-07-14 live read-only smoke reproduced 364,768 excluded values across
  12,632 stock/date/event-type groups and compiled all nine v1 features.
- **Guarded raw-tick write boundary** — materialization is available only through
  a separate operator command bound to a prior plan artifact, the exact rendered
  SQL SHA-256, explicit acknowledgement that external scan cost is unknown, and
  a positive `maximum_bytes_billed`. The SQL uses non-replacing `CREATE TABLE`
  with atomic seven-day expiration. Post-write acceptance fails closed on row
  count, stock coverage, stock/date uniqueness, date bounds, target identity,
  and expiration metadata. The autonomous runner cannot invoke this write path.
  **Exercised live 2026-07-14**: two under-capped attempts failed closed with
  zero billing and zero residue (external Parquet scans bill on *uncompressed*
  logical bytes, and BigQuery re-evaluates CTEs per reference — this query
  scans the external table ~5×, so the GCS file size is not the bound); the
  third attempt (40 GB cap, <$0.25) created the candidate and passed all six
  acceptance checks. The "missing first-hour features" review item was
  dispositioned: 84 % genuine first-hour thinness, ~0.5 % of the panel is
  capture gaps (queued for source QA). **Empirical outcome (Stage 5 of the
  case study): the intraday v1 features mostly add no OOS value over daily
  features — the table is left to expire; the opt-in loader/DSL wiring stays;
  one lead (`first_hour_n_trades − first_hour_avg_trade_size`, long-only net
  +0.0261 vs daily analog's +0.0178) is flagged for the next re-run.**  A
  follow-up for recurring materializations: restructure the SQL to a
  single-scan conditional aggregation (formal contract change).
- **Operator surface** — `validate_strict`, `combine_factors`,
  `refine_factor`, `inspect_composite`, `list_{factors,cycles,trails}`,
  `doctor`; memory + SQL registry backends behind protocols.
- **Remaining autonomy gap** — event studies and skill distillation are not yet
  wired into the typed task loop. Raw-tick writes deliberately remain outside
  autonomous dispatch.
- **Persistence-first selection machinery**
  (`alpha_harness/evaluators/persistence.py`): factors can be ordered
  by sub-window rank-IC sign consistency + stability instead of
  train-window mean IC — the fix for the case-study failure where the
  strict gates promoted the hottest-train-IC factor (which flipped
  OOS).  Backtested on the 12-factor HK IPO answer key: it excludes
  the trap factor by construction and doubles the basket's OOS
  rank-IC (+0.0156 → +0.0329), but on one 40-day window neither
  ordering predicts per-factor OOS rank (Spearman ≈ −0.5 for both) —
  so it stays **opt-in** (`combine_factors --selection-strategy
  persistence --top-k K`) until a longer OOS window arbitrates. The
  selection policy and candidate counts are persisted in combination
  reports and promotion trails. The original 2026-05-01 to 2026-06-26
  OOS window is now a consumed selector-development answer key; only
  later data qualifies as a fresh validation window. The
  companion tail-concentration audit refuted the "IPO debut spike"
  hypothesis (0/12 gate flips) and instead showed the gate criterion
  itself mispredicting OOS on this panel (4/5 gate-firing factors
  were OOS-positive).
- **~50 test files**, ruff-clean, with regression tests for each fixed
  audit finding.

### Scientific integrity

- **Self-auditing infrastructure caught its own bugs.** Because the
  combiner and the validator independently measure the same factor,
  their *disagreement* exposed 3 CRITICAL look-ahead / measurement
  bugs (combiner bypassed `HoldoutPolicy`; `FactorThumbnail` dropped
  the holdout block; `SignalQualityEvaluator` inflated IC via per-fold
  signal recomputation).  All fixed, all regression-tested.
- **Honest train/test methodology** — disjoint Y1 (selection) and Y2
  (validation) windows; no metric reported without an out-of-sample
  read.

### Empirical (real LLM + real data)

- Full loop exercised against **DeepSeek-Chat-v3.1** and
  **Qwen-2.5-72B** via OpenRouter on **Polygon SP-50** daily bars.
- **First live bounded autonomous run on HK IPO (2026-07-14)**: director selected event-conditioned
  microstructure; DeepSeek proposed 36 event-conditioned candidates
  over 3 cycles; the six-gate judge rejected 35 (weak IC, horizon
  sign-flips, tail concentration up to 4.9, degenerate event-gated
  cross-sections, and 4 candidates using intraday fields absent from
  the panel — rejected at execution exactly as designed); 1 promoted
  under the **lenient** regime and survived the automatic 15 bps
  cost-stress replay (`ts_delta(vwap, 2) *
  is_near_greenshoe_expiry_5d`, turnover 0.28, net spread +0.0122).
  **Audit correction:** this proves the autonomous control flow, not the
  factor. The old wrapper copied first-fold metadata, so +0.255 was the first
  fold's internal tail rather than a global holdout; the documented 5× ratio
  and ~43/8 coverage therefore do not share a reproducible window. The run
  also supplied no token pricing rates, so its USD ledger stayed at zero and
  the separate `$0.0036` estimate was not artifact-reproducible. Both defects
  are fixed. A deterministic replay on the identical data fingerprint
  (`6bf7ac...`) **rejected the factor**: training rank-IC +0.0230, global
  holdout rank-IC -0.0030, tail concentration 11.76, and Sharpe -2.12. The
  control loop succeeded; this candidate did not.
- One out-of-sample-positive basket (case study v1, post-fix); two
  out-of-sample-negative baskets (v2, v3) — see the verdict table
  below.
- **HK IPO tick microstructure** (real Bloomberg data in GCP BigQuery,
  77 IPOs + 86.1 M-row target tick lake): the **first signal to survive
  the full gauntlet** — disjoint OOS (10/12 factors persist, p ≈ 1.9 %)
  **and** realistic 78 bps cost **and** long-only-implementable (4/12
  positive net, incl. flagship `rank(ofi) - rank(rel_spread)`).  Modest
  magnitude, ~40-day test window — promising, not yet confirmed; bottleneck is data
  quantity.  See [`CASE_STUDY_HK_IPO_MICRO.md`](CASE_STUDY_HK_IPO_MICRO.md).
- **HK IPO event data enrichment**: HKEX prospectus/allotment documents
  are now curated into exact event dates and daily event features
  (`ipo_event_dates_curated`, `ipo_event_features_daily`) for greenshoe,
  stabilization, and cornerstone lockup research.  Known Bloomberg
  lockup anomalies are kept out of truth tables and routed to review.
- **HK IPO event studies — three event types, all honest nulls, and a
  data bug caught**
  (see [`DESIGN_LOCKUP_EVENT_STUDY.md`](DESIGN_LOCKUP_EVENT_STUDY.md)):
  - *Cornerstone lockup expiry*: clean negative on `listing + 6 mo`
    proxy dates (19 events, §8).  The exact-prospectus-date re-run
    initially looked "suggestive" (AR(τ=0) = −4.17 %), but an audit
    traced that entire signature to **one curated date that predated
    the stock's listing** (03378) snapping onto its IPO-debut crash.
    Corrected (N = 13): H1 CAR[−1,+3] = +0.04 % (median +3.4 %, 8/13
    positive) — **no expiry-day effect**; a persistent post-event OFI
    net-sell run is the only residual, noted not claimed (§9).
  - *Greenshoe expiry (56) and stabilization end (34)*: **clean
    nulls** — the raw means (+8.7 %/+13.7 % at τ=0!) were entirely
    manufactured by 4 more impossible curated dates landing on day-1
    pops; medians ≈ 0, sign tests ≈ coin flip, placebo windows match
    "event" windows.  27 of 34 stabilization ends are the same
    (stock, date) as greenshoe expiries — one day-30 boundary, not two
    samples (§10).
  - *Infrastructure hardened*: the curation SQL now routes
    **13 implausible event dates** (pre-listing, or day-30 types under
    listing + 20 d) to `needs_review` by construction; the event-study
    script gained a matching guard, median/sign-test statistics
    (IPO fat tails break the t-test), and a two-sided placebo —
    all regression-tested.  Sample sizes grow with ingestion: 15
    further lockup expiries already occurred after the panel's last
    date (2026-06-26), 22 more by 2026-09-30.

---

## What is limited / not yet proven ⚠️

### The headline limitation

**A single positive out-of-sample result does not replicate.**

| study | LLM | Y1 → Y2 | Y1 IC / ric | Y2 IC / ric | Y2 verdict |
|---|---|---|---|---|---|
| v1 | DeepSeek | 2024-04→2025-04 / 2025-04→2026-04 | +0.033 / +0.049 | +0.058 / +0.053 | ✅ |
| v2 | DeepSeek | 2024-06→2025-05 / 2025-05→2026-04 | +0.035 / +0.044 | −0.023 / −0.014 | ❌ |
| v3 | Qwen | 2024-06→2025-05 / 2025-05→2026-04 | +0.025 / +0.036 | −0.036 / −0.043 | ❌ |

Two LLMs on the same Y2 window both sign-flip out-of-sample.  The
failure is **window-specific, not LLM-specific**.  The v1 positive is
real-but-fragile.

### Evaluator leakage audit

- **Finding 4 closed (2026-07-15)** — beta neutralization now uses strictly
  lagged rolling OLS (60-bar lookback, 20 prior observations by default).
  Future-return mutation cannot change past residuals; the policy and window
  are persisted in evaluator metadata and promotion trails. The HK IPO OFI
  fixed-snapshot replay retained fingerprint `6bf7ac...`, regime trail
  `ef194f4dfc1f6c54`, all factor metrics, and the same 6 tail / 1 holdout
  rejection split.

### Structural limitations

- **Survivorship-biased universe** — SP-50 is 50 surviving large-caps
  (Finding 5); absolute IC is inflated by an unmeasured amount.
- **Polygon free-tier** caps history at trailing ~2 years and is
  region-blocked for Anthropic/Google/OpenAI models (we run on
  DeepSeek/Qwen).
- **Flat 5 bps cost model** — real execution cost is trade-size and
  liquidity dependent.
- **No live execution, no intraday, no broader asset classes.**
- **No currently valid composite anchor** — the complement architecture is
  implemented and synthetic-tested, but the canonical promoted zoo has no
  basket eligible under the current global-holdout contract. Pre-fix or stale
  promotions are intentionally not treated as anchors.

---

## What we should do next 🎯

The architecture is done enough to *answer* the real question, which a
single run cannot: **does this loop produce alpha on average, or only
by chance?**  Ranked by leverage:

### 0. Fixed-snapshot OFI attribution — completed, no promotion

Without extending the panel past 2026-06-26, attribute the 10-day and 20-day
OFI long-short returns by walk-forward fold, calendar date, listing age, and
event proximity. The deterministic report reproduces the judge exactly: the
2026-03-12 to 2026-05-04 fold has tail ratios 0.775/0.945, versus aggregate
in-sample ratios 0.203/0.157. Its top returns are 2026-03-13, 16, and 17, three
overlapping five-day labels from one March episode. IPO ages 31–90 days were
positive while 91+ days were negative, but that partition was inspected after
seeing the outcomes and is only a future hypothesis. **Decision:** both factors
remain rejected; no event or age filter is promoted on this snapshot.

The episode-aware diagnostic is now implemented and confirms that the apparent
three-date March cluster is not enough to rescue the factors: non-overlapping
fold cohorts contain only 3–4 positive observations in their thinnest phase.
The global-holdout embargo audit is also closed: window-local label construction
already purges the final `lag+horizon` training labels, and a holdout-price
mutation test proves training metrics cannot read them. Multiple-hypothesis
pressure accounting is now also closed under schema v6; historical reports are
not silently rewritten or retroactively relabeled. The final medium evaluator
gap, full-window beta estimation, is closed by causal rolling beta; the current
HK IPO results are unchanged because their strict regime uses sector rather
than beta neutralization.

### 1. Data scaling — the actual prerequisite (decided)

The fragility in v2/v3 is most likely **starved-for-data**: 50
survivorship-biased names × ~2y of daily bars gives the LLM almost no
decorrelation budget and the judge almost no statistical power.  The
scaling path is designed in
[`DATA_INFRA_PLAN.md`](DATA_INFRA_PLAN.md): Bloomberg tick ingestion →
cloud lake + access API → RAG → multi-market (HK …), built against the
existing `loader_factory` / `DataRequest` seams.  Buildable now: tick
schema, exporter interface + mock, cloud loader.  Blocked on a
Bloomberg Terminal + cloud account: the real data itself.

**Decision:** data scaling precedes more autonomy, and the future
autonomous research-director loop (Round 10) is **robustness-first** —
every self-generated candidate basket auto-confirmed on a held-out
window before it counts as alpha.

### 2. Multi-run robustness study — RUN (2026-07-15): a systematic null

The designed experiment exists (`scripts/robustness_study.py`,
`make robustness-study`: predeclared rolling Y1→Y2 grid with embargo
gap, `no_basket`/`failed` cells kept in the denominator,
smoke-verified to report "no edge" on a synthetic no-edge panel) —
**and has now been run for real** on freshly backfilled SP-50 parquet
(2024-07-15 → 2026-06-30), 3 rolling splits × {DeepSeek-v3.1,
Qwen3-235B} × {input_order, persistence} = 12 cells, ≈ $0.04 total.

**Result: 10/12 executed, Y2 rank-IC positive 4/10 (sign p = 0.75),
pooled mean −0.011, strict clears 0/10.**  Both LLMs, both selection
strategies, all three windows — nothing distinguishable from no-edge,
under Bonferroni-scaled thresholds, on a survivorship-biased universe
that should *favour* finding spurious alpha.  The v1 positive now
reads as window luck, as v2/v3 suggested.  The selector arms showed no
separation (2/5 vs 2/5).  Full write-up:
[`CASE_STUDY_ROBUSTNESS_SP50.md`](CASE_STUDY_ROBUSTNESS_SP50.md).

**What this changes:** "does the loop produce alpha on average on
daily OHLCV?" is now answered — no, for this proposer/DSL/universe.
The live alpha hypothesis is the HK IPO microstructure track
(order-flow information OHLCV cannot express), pending its data
update.  Re-running this same grid on HK IPO once the panel extends is
the natural next study.

### 3. Round 10 — activate composite complements when an anchor qualifies

The typed proposer contract, deterministic augmented-basket evaluation,
incremental gates, schema-v7 audit trail, and exact replay path are complete.
The next empirical step is to run `--composite-complements` once a basket has
survived the current promotion and global-holdout rules. Until then the mode
correctly fails closed rather than manufacturing an anchor from stale results.

### 4. Broaden data realism (lower priority)

Point-in-time universe loader (kill survivorship bias), liquidity-aware
cost model, longer history via a paid data tier.

---

## How to read the verdict

This is a **research-tool success and a trading-strategy non-result**,
and both halves are true at once:

- As an honest measurement instrument, the harness works: it found its
  own bugs, it reports out-of-sample truth, and it refuses to promote
  what doesn't survive.
- As an alpha generator, the evidence is one positive out of three
  honest attempts — not enough to claim anything.  The next milestone
  is the study that would actually settle it.

Claiming more than this would contradict our own data.  The project's
credibility comes from *not* overclaiming.
