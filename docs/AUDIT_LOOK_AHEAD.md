# Look-ahead / leakage audit

> Systematic review of every place future data could leak into a
> reported metric, conducted after the Q2 2026 case study.  Findings
> are ranked **CRITICAL** (changes how you should read the case-study
> numbers), **medium** (affects metric precision but not direction),
> and **low** (acknowledged design choices with bounded impact).

| # | finding | severity | status |
|---|---|---|---|
| 1 | Combiner's basket evaluation bypasses `HoldoutPolicy` | **CRITICAL** | ✅ fixed in `c535059` |
| 2 | `CombinationReport.basket_metrics` drops `metadata.holdout` | **CRITICAL** | ✅ fixed in `c535059` |
| 3 | TAIL holdout label overlap | medium | resolved by window-local labels; regression added 2026-07-15 |
| 4 | Beta neutralization is estimated in-sample over full window | medium | resolved by strictly lagged rolling OLS |
| 5 | SP-50 universe is survivorship-biased by construction | medium | acknowledged in universe header |
| 6 | No multiple-hypothesis pressure over proposer cycles | medium | resolved by schema v6 session thresholds |
| 7 | DSL rolling operators use `min_periods=1` | low | acknowledged in code |
| 8 | Sector neutralization uses a static (non-point-in-time) map | low | acknowledged in code |
| 9 | `SignalQualityEvaluator` recomputes signals per-fold, inflating IC vs the realistic compute-then-slice approach | **CRITICAL** | ✅ fixed in this revision; regression test in `tests/unit/test_finding9_regression.py` |

The rest of the harness — DSL operators, forward-return construction,
walk-forward fold layout, proposer memory recency, judge gate cascade
— is structurally sound.

---

## Finding 1 (CRITICAL): combiner bypasses HoldoutPolicy

**Where:** `scripts/combine_factors.py` → `_PrecomputedSignalEvaluator.evaluate` →
`alpha_harness/evaluators/signal_quality.py:evaluate_precomputed_signal`.

**What's wrong.** `evaluate_precomputed_signal` is a thin function that
goes straight to forward-returns + IC computation — it never branches
on `request.holdout.strategy == TAIL` the way
`SignalQualityEvaluator.evaluate` does.  So the basket evaluation
ignores the holdout policy entirely.  The validator
(`scripts/validate_strict.py`) does honor it.

**Impact on the case study.**

| metric | reported in case study | should have been | implication |
|---|---:|---:|---|
| basket IC (zscore_average) | +0.0392 (walk-forward) | +0.0085 (full window, no walk-forward) | walk-forward overlapping folds inflated the mean |
| basket rank_IC | +0.0432 (walk-forward) | +0.0090 (full window) | same |

The +0.0392 figure is a **per-fold mean** across 4 overlapping 60-day
folds with no out-of-sample protection.  Per-fold ICs were
`[−0.0018, +0.0542, +0.0805, +0.0241]` — fold 1 was negative, fold 3
carried most of the lift.  Whether the basket survives on a clean
20 % tail holdout is unknown because the holdout block was never
populated for the precomputed-signal path.

**Why we didn't catch this in Round 7.1.**  Round 7.1 fixed the
combiner so it threads the regime through walk-forward.  But
`evaluate_precomputed_signal` was extracted as the "post-DSL" path,
and the holdout branch lives in
`SignalQualityEvaluator._evaluate_with_holdout`, which only fires from
`SignalQualityEvaluator.evaluate`.  The precomputed adapter calls the
extracted function directly, so the holdout dispatch never runs.

**Fix.**  Replicate the holdout-split dispatch inside
`evaluate_precomputed_signal`: if `request.holdout.strategy == TAIL`
and `holdout_fraction > 0`, split the window the same way
`_evaluate_with_holdout` does, run the function recursively with
`holdout=NONE` on each half, and merge the holdout block into
`metadata`.  Then update `CombinationReport.basket_metrics` to
surface the holdout block (see Finding 2).

## Finding 2 (CRITICAL): `FactorThumbnail` drops `metadata.holdout`

**Where:** `alpha_harness/reports/validation.py:FactorThumbnail`.

`FactorThumbnail` captures `ic`, `rank_ic`, `quantile_spread`,
`net_quantile_spread`, `sharpe`, `turnover` — but not
`bundle.metadata["holdout"]`.  So even when the validator *does*
populate the holdout block (it does for scalar factors via
`SignalQualityEvaluator._evaluate_with_holdout`), the holdout IC is
lost when the thumbnail is serialized into the validation /
combination report.  A reader of the report can't tell whether the
factor's edge survived out-of-sample.

**Fix.** Add `holdout_ic`, `holdout_rank_ic`, `holdout_decay_ratio`
to `FactorThumbnail` and populate them from `bundle.metadata.get
("holdout", {})` at build time.  Schema version bump (1 → 2).

## Finding 3 (resolved): TAIL holdout label overlap

**Original location:**
`alpha_harness/evaluators/signal_quality.py:_evaluate_with_holdout`.

The in-sample window is `[eval_start, is_end]` where `is_end =
split_start − 1 day`.  The in-sample IC at date `is_end − k` (for k
in [0, horizon − 1]) uses forward returns computed from prices
`[is_end − k + lag, is_end − k + lag + horizon]`, which lies inside
the holdout window.  So ~6 days of in-sample labels overlap with the
holdout span.

**Resolution (2026-07-15).** This finding described the pre-Finding-9 execution
order. The current scalar and precomputed paths first slice the in-sample
`df`/`signal`, then construct forward returns within that slice. Consequently,
the final `lag + horizon` training labels are NaN and cannot read holdout
prices. The walk-forward wrapper uses the same window-local inner evaluator.
All three paths now record `embargo_bars` and
`embargo_mode=window_local_forward_returns` in holdout metadata.

Regression coverage mutates every holdout price and verifies that in-sample IC,
rank-IC, and quantile spread remain unchanged while holdout rank-IC changes.
Explicitly trimming another `lag + horizon` days would double-purge the label
boundary and is therefore not applied.

## Finding 4 (resolved): in-sample beta estimation

**Where:** `alpha_harness/evaluators/neutralize.py:_beta_neutralize`.

Per-symbol beta is estimated over the full evaluation window and used
to neutralize every date in that window.  Future-dated returns thus
contribute to the beta used for past-dated residuals.

**Resolution (2026-07-15).** Beta neutralization now uses per-symbol rolling
OLS with a mandatory one-date lag. The coefficient applied at date `t` sees at
most the prior 60 paired observations and requires 20 by default; neither the
current return nor any future return can enter the estimate. Rows before warmup
remain unavailable instead of receiving an in-sample fallback. Both parameters
are typed request fields, are recorded in evaluator metadata, and enter the
promotion-trail hash when beta neutralization is active.

A mutation regression changes all returns after a cutoff and proves every
pre-cutoff residual is byte-for-byte unchanged. The `BOTH` path also preserves
sector residuals when sector demeaning makes the market component zero, avoiding
an unnecessary beta warmup sample loss. Existing HK IPO strict studies use
`NeutralizeMode.SECTOR`, so this closes a future-use leakage path without
retroactively changing their metrics. A seven-factor OFI replay on fingerprint
`6bf7ac...` retained the same regime trail (`ef194f4dfc1f6c54`), rejection
split (`tail_concentration=6`, `holdout_decay=1`), and every factor metric;
only run-generated factor ids differ.

## Finding 5 (medium): SP-50 universe survivorship bias

**Where:** `configs/universes/sp50.txt`.

The 50 names are large-cap US equities chosen for "deep liquidity and
long continuous history" — literally a survivorship filter.  Any IC
measured on this list is overstated relative to a point-in-time
universe (which would include names that delisted or got acquired
during the period).

**Mitigation:** universe header documents the bias.  Severity is
bounded for a 2-year window in mega-caps — delistings are rare —
but the harness should support a point-in-time universe loader for
serious research (out of scope for the current rounds).

## Finding 6 (resolved): no multiple-hypothesis pressure

**Where:** the proposer + judge stack as a whole.

`--n-cycles 3 --n-candidates 6` produces 18 factor evaluations.  With
strict-regime thresholds (ic ≥ 0.02, rank_IC ≥ 0.03), some factors
will clear by chance — the harness doesn't apply Bonferroni or
false-discovery correction over the cycle.  The Q2 2026 case study's
4 "both-positive" survivors among 23 evaluations is consistent with
selection from a no-effect null at conventional FDR levels.

**Prior mitigation:** the 6-gate judge (walk-forward stability, holdout
decay, tail concentration, sign consistency) provided indirect correction,
but the cumulative regime did not track or display the hypothesis-family
size.

**Resolution (2026-07-15).** The predeclared proposal-slot count is now part of
`EvaluationRequest`, schema-v6 validation reports, memory scopes, and promotion
trails. The judge applies a deterministic Bonferroni z-critical multiplier to
IC and rank-IC thresholds only:

`z(1 - 0.05 / N) / z(0.95)`

`N=1` preserves the previous thresholds exactly; `N=18` multiplies them by
1.6858. Quantile spread remains an economic threshold rather than being
misrepresented as a significance test. This is an explicit pressure policy,
not a p-value computed from observed IC. A synthetic 3×6 acceptance run records
`N=18` even when the proposer returns fewer valid candidates, preventing
post-hoc relaxation of the family size.

## Finding 7 (low): `min_periods=1` in DSL rolling operators

**Where:** `alpha_harness/factors/dsl_executor.py:_rolling_*`.

`ts_mean(close, 20)` on bar 5 of a symbol uses 5 observations, not 20.
This is **not** a leak (no future data), but it makes the first
~window dates of each symbol noisy — the per-date IC at the
boundary could be biased.

**Mitigation:** the eval window in the case study (2024-04-19 →
2026-04-17) is much longer than any factor's window (max 30 days), so
boundary effects affect <2 % of the panel.  Acknowledged in the
module docstring.

## Finding 9 (CRITICAL): SQE per-fold signal recomputation inflates IC

**Where:** `alpha_harness/evaluators/signal_quality.py:_filter_to_window`
+ `SignalQualityEvaluator.evaluate`.

**Discovered:** during the honest-train/test case study
(`docs/CASE_STUDY_HONEST.md`).

**What's wrong.**  When `WalkForwardEvaluator` slices the eval window
into folds and asks the inner evaluator for IC on each fold, the
two inner evaluator implementations behave differently:

- **`SignalQualityEvaluator`** filters `df` to
  `[fold_start, fold_end]` *before* the DSL runs, so rolling
  operators (`ts_mean(close, 10)` etc.) see zero prior history at
  the fold boundary.  With `min_periods=1` (Finding 7), early-fold
  signal values are computed from 1, 2, 3 … observations instead of
  the full window — i.e., they're degenerate.
- **`_PrecomputedSignalEvaluator`** (used by `combine_factors`)
  computes the signal on the *full* panel once and slices the
  resulting series.  Early-fold signal values use full prior
  history.

Empirically, running the same DSL expression on the same Y1
window through both paths (holdout disabled, walk-forward identical
in both):

| path | fold 1 | fold 2 | fold 3 | fold 4 |
|---|---:|---:|---:|---:|
| SQE → WF | −0.068 | +0.034 | −0.045 | +0.081 |
| precomputed → WF | −0.068 | +0.024 | −0.053 | −0.017 |

Fold 1 matches because there's no prior history regardless.  Folds
2–4 differ — sometimes dramatically.  The fold-averaged IC swings
from +0.0003 (precomputed) to +0.049 (SQE) for one of the
case-study top factors.

**Direction:** SQE generally produces *higher* IC.  Reason: rank()
and zscore() are sensitive to tail outliers; degenerate
early-fold-window signal values give a few extreme normalized
ranks that happen to correlate with forward returns by chance.

**Severity: CRITICAL.**  Every IC value in every historical
validation report has been inflated by this fold-boundary
artifact.  The "case study success" basket from
`docs/CASE_STUDY_2026Q2.md` was selected based on these inflated
component metrics.  When the honest study re-evaluated the same
components on Y2 via the combiner's path, three of seven factors
flipped sign — exactly what a no-real-edge null predicts.

**Fix shipped.**  `SignalQualityEvaluator.evaluate` now computes the
factor signal on the full `self._data` panel *before* filtering to
`[eval_start, eval_end]`.  The signal (and aligned `df`) are then
sliced to the request window and handed to
`evaluate_precomputed_signal`.  This makes the SQE path
byte-equivalent to the combiner's `_PrecomputedSignalEvaluator`
flow.

Regression test: `tests/unit/test_finding9_regression.py`.  Asserts
fold-by-fold IC parity between `SignalQualityEvaluator → WalkForward`
and `compute_signal → _PrecomputedInner → WalkForward` over a
deterministic 250-day × 20-symbol panel.  Fails loudly if a future
change re-introduces per-fold signal recomputation.

The fix changed the **honest case study verdict**: pre-fix Y1
metrics were inflated (the IC bug pushed mean-reversion-via-MA-
ratio factors into the survivor pool), basket failed on both Y1
and Y2.  Post-fix, the LLM's effective survivor pool shifted to
genuinely decorrelated volume × price-change factors (avg corr
+0.08 vs +0.33 pre-fix), and the basket cleared strict on both Y1
and Y2 — see `docs/CASE_STUDY_HONEST.md` post-fix section.

## Finding 8 (low): static sector map

**Where:** `configs/universes/sp50_sectors.csv` + neutralize loader.

The sector map is a `dict[symbol, sector]` with no date dimension.
For US equities, sector reclassifications are extremely rare on the
2-year horizon, so the in-practice impact is near-zero.

## Verified-clean components

These were audited and showed no leak:

* **DSL time-series operators** (`ts_mean`, `ts_std`, `ts_sum`,
  `ts_min`, `ts_max`, `ts_delta`, `ts_lag`).  All use
  `pandas.Series.rolling()` (default `closed='right'`, looks at
  current + previous bars) or `shift(positive)` (which pulls past
  values forward).  No operator reads future prices.
* **Cross-sectional operators** (`rank`, `zscore`).  Grouped per
  timestamp — no temporal contamination.
* **Forward-return construction**
  (`build_forward_returns`).  `fwd_return[t] = close[t + lag] →
  close[t + lag + horizon]`, with `lag_bars` default = 1.  At
  signal-time `t`, the signal is computed from prices through `t`
  (backward operators only); the label uses prices from `t + 1`
  onward.  No alignment leak.
* **Walk-forward fold layout** (`fold_windows`).  Embargo defaults
  to `lag + horizon` days, trimmed off the *end* of each fold so
  the closing label doesn't extend past `eval_end`.  Overlapping
  folds (step < size) are statistically suboptimal — they
  oversample the time series and inflate the
  `fraction_positive_rank_ic` denominator — but they don't constitute
  look-ahead bias.  Each fold is a self-contained evaluation; no fold
  "trains" on another.
* **Judge gate cascade** (`promotion_judge.py`).  Six gates apply
  to already-computed metrics: data sufficiency → profile
  thresholds → sign consistency → walk-forward stability → tail
  concentration → holdout decay.  No gate looks at data the
  evaluator didn't already process.
* **Proposer memory digest** (`proposer/memory.py`).  Sources from
  `registry.list_recent()` (ordered by `created_at DESC`) and the
  durable promoted-artifact index.  Both contain only records
  created *before* this cycle started.  No future leak across
  cycle boundaries.
* **Composite recipe + executor**.  Components are independent DSL
  expressions; combining them via rank / zscore / mean at each
  timestamp doesn't introduce any temporal coupling.  The recipe's
  hash is over the canonical AST, not over evaluation data.

---

## What this means for the Q2 2026 case study

The case study's headline result — **basket IC `+0.0392` / rank_IC
`+0.0432`, clearing strict on both gates** — was computed without
holdout protection (Finding 1) and was a walk-forward mean over
overlapping folds.  The pooled full-window IC of the same basket is
`+0.0085 / +0.0090`, which does **not** clear strict.

This doesn't mean the combination thesis is wrong — the per-fold ICs
were positive in 3 of 4 folds, and the agent loop closure (Round 9
Phase A) was verified end-to-end on real LLM output regardless of the
metric magnitude.  But the bottom-line "basket beats strict" claim
deserves a strikethrough until the holdout-aware combiner re-runs
the experiment.

## Action items

1. **(CRITICAL)** ✅ Done — Fix `evaluate_precomputed_signal` to honor
   `HoldoutPolicy`.  Re-run the Q2 case-study combination with
   holdout-aware metrics and update `docs/CASE_STUDY_2026Q2.md`.
2. **(CRITICAL)** ✅ Done — Extend `FactorThumbnail` with holdout fields;
   bump schema version.
3. **(CRITICAL)** ✅ Done — Fix Finding 9.  Re-running historical
   validation reports is no longer needed: post-fix re-run of the
   case study produced a positive verdict
   (`docs/CASE_STUDY_HONEST.md` post-fix section), so the corrected
   metrics speak for themselves going forward.
4. **(resolved)** Verify and surface the window-local holdout purge.
5. **(resolved)** Record the predeclared proposal family and apply the
   schema-v6 Bonferroni z-threshold pressure policy.
6. **(low)** Add point-in-time universe loader path for future
   research; SP-50 is fine for harness-validation purposes.

Findings 1, 2 are fixed.  Finding 9 is the next must-fix CRITICAL.
The rest are well-flagged design choices, not silent cheats.
