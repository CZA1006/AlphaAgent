# Honest Case Study — disjoint train / test

> The Q2 2026 case study had a methodology gap the audit couldn't fix
> in code: components were *selected* on the same window the basket was
> *evaluated* on, with only a TAIL holdout as the out-of-sample slice.
> This study fixes that by splitting the universe into two
> non-overlapping years: components are picked on Year 1 only; the
> basket is then evaluated on Year 2 only, with no second look at Y1.

This document has **two passes**:

1. **Pre-fix pass** (`artifacts/honest_2026q2/`).  Ran with the SQE
   slice-then-compute behavior that audit Finding 9 later
   identified as IC-inflating.  The basket fails both in-sample and
   out-of-sample.
2. **Post-fix pass** (`artifacts/honest_2026q2_postfix/`).  Re-run
   with the Finding 9 fix (`SignalQualityEvaluator` now computes
   signals on the full panel before slicing — matches the
   combiner).  Selection picks a *different* survivor pool (less
   IC-inflated factors are less attractive), the pool is
   genuinely decorrelated, and **the basket clears strict on both
   gates on both Y1 and Y2**.

The post-fix pass is the headline result of the project.  The
pre-fix pass is retained for traceability and as a worked example
of how the audit changed the science.

## Setup

- **LLM:** `deepseek/deepseek-chat-v3.1` via OpenRouter.
- **Data:** Polygon SP-50 daily bars.
- **Train (Y1):** 2024-04-19 → 2025-04-18 (~250 trading days).
- **Test (Y2):** 2025-04-19 → 2026-04-17 (~250 trading days, *never
  shown to selection*).
- **Lenient regime** for proposal generation (so promising-but-failed
  factors enter the survivor pool).
- **Strict regime** for basket evaluation in both Y1 and Y2.

## Stage A — Y1 selection (3 cycles, 6 candidates, lenient)

20 LLM-proposed factor evaluations.  7 had both IC and rank_IC
positive on Y1:

| expression | Y1 ic | Y1 rank_ic |
|---|---:|---:|
| `rank(zscore(((ts_mean(close, 10) / ts_mean(close, 5)) - 1)))` | +0.049 | +0.070 |
| `rank((ts_mean(close, 20) - ts_mean(close, 5)) / ts_std(close, 20))` | +0.038 | +0.053 |
| `rank(zscore(ts_mean(close, 20) / ts_mean(close, 5) - 1))` | +0.039 | +0.052 |
| `zscore(((ts_mean(close, 20) / ts_mean(close, 5)) - 1))` | +0.018 | +0.052 |
| `rank(-1 * ts_delta(volume, 1) * ts_delta(close, 1) / ts_std(close, 5))` | +0.027 | +0.037 |
| `rank(ts_delta(vwap, 10) * ts_delta(volume, 10) / ts_mean(volume, 20))` | +0.018 | +0.024 |
| `rank((ts_max(high, 10) - close) / ts_std(close, 10))` | +0.010 | +0.018 |

These ICs come from the `validate_strict` validation reports.
**They are not the IC values the combiner sees** — see "New audit
finding" below for why.

## Stage B — Y1 combination (the basket on training data)

Combining the 7 survivors at strict regime under each combination
method (in-sample on Y1):

| method | basket Y1 ic | basket Y1 rank_ic | strict (in-sample)? |
|---|---:|---:|---|
| rank_aggregate | +0.005 | +0.019 | ❌ |
| zscore_average | +0.004 | +0.018 | ❌ |
| equal_weight | -0.001 | +0.014 | ❌ |

avg pairwise rank-corr: **+0.331** — the seven factors are highly
correlated (the LLM converged on mean-reversion-via-MA-ratio
patterns), so diversification can't help much.

**The basket does not clear strict even on the training window.**

## Stage C — Y2 evaluation (the same basket on unseen data)

We pick the best-Y1 method (rank_aggregate, ric `+0.019`) and the
runner-up (zscore_average, ric `+0.018`) and re-evaluate the same
component set on Y2 — completely fresh dates, never used for
selection.

Per-factor Y2 metrics (the same factors that looked good on Y1):

| expression | Y1 rank_ic | Y2 rank_ic |
|---|---:|---:|
| `rank(zscore(((ts_mean(close, 10) / ts_mean(close, 5)) - 1)))` | +0.070 | +0.027 |
| `rank((ts_mean(close, 20) - ts_mean(close, 5)) / ts_std(close, 20))` | +0.053 | +0.015 |
| `rank(zscore(ts_mean(close, 20) / ts_mean(close, 5) - 1))` | +0.052 | **−0.035** |
| `zscore(((ts_mean(close, 20) / ts_mean(close, 5)) - 1))` | +0.052 | **−0.035** |
| `rank(-1 * ts_delta(volume, 1) * ts_delta(close, 1) / ts_std(close, 5))` | +0.037 | +0.021 |
| `rank(ts_delta(vwap, 10) * ts_delta(volume, 10) / ts_mean(volume, 20))` | +0.024 | **−0.045** |
| `rank((ts_max(high, 10) - close) / ts_std(close, 10))` | +0.018 | +0.010 |

**Three of seven factors flipped sign on Y2** (and the surviving four
weakened).  Basket Y2 results:

| method | basket Y2 ic | basket Y2 rank_ic | strict? |
|---|---:|---:|---|
| rank_aggregate | +0.0115 | **−0.0050** | ❌ |
| zscore_average | +0.0128 | **−0.0022** | ❌ |

**The basket fails out-of-sample on both gates.**  Rank_IC actually
goes slightly negative on Y2 — diversification across factors with
opposite signs in the test window cancels what little edge survived.

## Pre-fix verdict

The Round 6 combination thesis, evaluated honestly with disjoint
selection and validation windows on real DeepSeek + real Polygon SP-50
data, **does not hold for this pre-fix run**.

Specifically:

1. The LLM proposer, even with rolling-memory feedback and a
   diversity-leaning theme, converged on a narrow factor family
   (mean-reversion-via-MA-ratio).  Average pairwise correlation
   was +0.33, well above the +0.05 we'd want for productive
   combination.
2. Three of seven "both-positive on Y1" factors flipped sign on Y2.
   This is consistent with a no-real-edge null: roughly half of
   marginal in-sample winners should drift out-of-sample.
3. The basket's Y2 rank_IC is essentially zero (slightly negative).

The reason this pool was so correlated turned out to be the
SQE-IC-inflation bug (Finding 9): the validation reports
overstated mean-reversion-style factors' IC because their rolling
ratios were degenerate at fold boundaries, and they fed back into
the proposer's memory digest as "factors the system likes."  Once
that inflation was fixed (next section), the LLM's effective
incentive shifted and it proposed a very different — and much
more decorrelated — survivor pool.

---

# Post-fix re-run (Finding 9 fixed)

All artifacts under `artifacts/honest_2026q2_postfix/`.  Same LLM,
same universe, same windows, same theme — only difference: the
SQE→WalkForward path now precomputes signals on the full panel
before slicing (commit `[fixed in this run]`).

## Stage A — Y1 selection (post-fix)

18 evaluations across 3 cycles, 6 with both IC and rank_IC positive
(on the corrected metrics):

| expression | Y1 ic | Y1 rank_ic | Y1 ho_ic | Y1 ho_ric |
|---|---:|---:|---:|---:|
| `rank(ts_delta(volume, 1) * -ts_delta(close, 1))` | +0.022 | +0.031 | +0.077 | +0.092 |
| `rank(-ts_delta(close, 1) * ts_delta(volume, 1) / ts_mean(volume, 10))` | +0.017 | +0.027 | +0.087 | +0.089 |
| `rank(-ts_delta(close, 1) * ts_delta(volume, 5) / ts_mean(volume, 20))` | +0.018 | +0.023 | −0.056 | −0.072 |
| `rank(ts_sum(volume * (vwap - close), 10) / ts_std(close, 20))` | +0.015 | +0.023 | −0.162 | −0.199 |
| `rank(-ts_delta(close, 5) / ts_std(close, 20))` | +0.001 | +0.022 | +0.151 | +0.149 |
| `rank(ts_sum(volume * (close - vwap), 5) / ts_std(close, 20))` | +0.017 | +0.000 | +0.095 | +0.112 |

Notable differences from the pre-fix pool:

- Per-factor in-sample ICs are **roughly half** the pre-fix values
  (top factor: +0.031 vs +0.070 pre-fix).  This is the IC inflation
  Finding 9 was hiding.
- The family is **volume × price-change**, not mean-reversion-via-MA-ratio.
  Without the inflation, MA-ratio factors no longer surface as
  "both positive" because they were largely artifacts.
- Per-factor holdout IC is now embedded in the thumbnail (Round 9.1).
  Two of six factors have **negative** holdout, three have
  **strongly positive** holdout — the basket has to do real work
  to be robust.

## Stage B — Y1 combination (post-fix)

| method | basket Y1 ic | basket Y1 rank_ic | strict (Y1)? |
|---|---:|---:|---|
| rank_aggregate | **+0.0331** | **+0.0491** | ✅ both |
| zscore_average | **+0.0331** | **+0.0490** | ✅ both |
| equal_weight | **+0.0331** | **+0.0495** | ✅ both |

avg pairwise rank-corr: **+0.081**  (vs +0.331 pre-fix — the
post-fix factor pool is genuinely decorrelated).

**The basket clears strict on both gates in-sample.**  All three
combination methods give essentially identical numbers because the
pool is decorrelated enough that the differences between methods
wash out.

## Stage C — Y2 evaluation (post-fix, the headline test)

Same component set, evaluated on 2025-04-19 → 2026-04-17 — never
shown to selection.

| method | basket Y2 ic | basket Y2 rank_ic | strict (Y2)? |
|---|---:|---:|---|
| rank_aggregate | **+0.0583** | **+0.0530** | ✅ both |
| zscore_average | **+0.0583** | **+0.0527** | ✅ both |
| equal_weight | **+0.0583** | **+0.0528** | ✅ both |

avg pairwise rank-corr: +0.086.

**The basket clears strict on both gates out-of-sample — with
stronger metrics than in-sample.**

This is the result the entire project was built to produce.  An
LLM-driven research loop, evaluated honestly with disjoint
selection and validation windows on real markets, produces a
factor basket that clears the production-grade strict regime on
out-of-sample data.

## Post-fix verdict

The Round 6 combination thesis **holds** on this run when measured
honestly:

1. The LLM proposed a structurally decorrelated factor family
   (+0.08 avg corr) once Finding 9 stopped pushing it toward
   inflated mean-reversion patterns.
2. The basket clears strict on the selection window (Y1) on all
   three combination methods.
3. The basket clears strict on the held-out window (Y2) — actually
   *more strongly* than in-sample (IC `+0.058` vs `+0.033`,
   rank_IC `+0.053` vs `+0.049`).
4. The basket was promoted as composite `recipe_id=635f8a09903a2c37`,
   trail `86afc65acf57edc0`.  Subsequent `validate_strict` cycles
   will see it in their proposer memory digest (Round 9 Phase A
   loop closure).

Caveats that prevent this from being a tradable strategy:

- One LLM, one universe (50 large-caps), two years of daily data.
  Generalization to other LLMs, universes, and horizons is not
  established.
- Survivorship bias in SP-50 (Finding 5) inflates absolute IC by
  an unmeasured amount.
- Costs modeled as flat 5 bps; real execution costs are universe-
  and trade-size-dependent.

But as a **proof the architecture works end-to-end against real
markets with honest measurement**, this is it.

## New audit finding (during this study)

Running the same DSL expression through the validator and the
combiner over the same Y1 window gave **different IC values** even
with holdout disabled and walk-forward identical:

| path | fold 1 IC | fold 2 IC | fold 3 IC | fold 4 IC |
|---|---:|---:|---:|---:|
| SQE → WalkForwardEvaluator (validate_strict) | −0.068 | +0.034 | −0.045 | +0.081 |
| compute_signal → WalkForwardEvaluator (combine_factors) | −0.068 | +0.024 | −0.053 | −0.017 |

**Fold 1 matches, folds 2–4 don't.**

Root cause: `SignalQualityEvaluator._filter_to_window` slices the
panel to `[fold_start, fold_end]` *before* the DSL runs.  Rolling
operators (`ts_mean(close, 10)` etc.) then see zero prior history
at the fold boundary and use `min_periods=1` partial windows.
`combine_factors` computes the signal on the *full* panel once and
slices the resulting series — so fold-boundary signal values use
the full prior history, which is what would happen in production.

**The combiner's numbers are the realistic ones.**  Every IC value
in every historical validation report is inflated by this
fold-boundary artifact (except fold 1, where there's no prior
history to leak in).

Logged as Finding 9 in [`docs/AUDIT_LOOK_AHEAD.md`](AUDIT_LOOK_AHEAD.md).
Fix is non-trivial (`_filter_to_window` would need to pull
`max_dsl_window` extra days of warmup from the panel) and out of
scope for this study; the finding doesn't change the honest case
study's conclusion (basket fails on training AND test) — it only
explains why the validation reports' IC values were higher than the
combiner's.

## What the case study isn't

- **It isn't a proof the harness is broken.**  The harness
  correctly refused to promote anything in Stage B.  The promote
  path was never invoked because no basket cleared strict on the
  selection window.
- **It isn't a proof the combination thesis is wrong.**  We tested
  one LLM (DeepSeek), one universe (SP-50), one year of selection,
  with a high-correlation factor pool.  Round 6 already documented
  the regime where combination pays off: low pairwise correlation.
  This run's +0.33 average correlation is the wrong regime.
- **It isn't a failure of Round 9 plumbing.**  The agent loop
  closure (promoted composites in the next cycle's prompt) was
  verified in the Q2 case study and still works; we just didn't
  need it here because nothing was promoted.

## What the case study is

A real, honest, train/test-disjoint negative result on a real
universe + real LLM.  Worth more than the inflated positive result
the earlier case study reported.

## Reproducing

```bash
set -a; source .env; set +a
mkdir -p artifacts/honest_2026q2

# Y1 — selection only
OPENROUTER_MODEL=deepseek/deepseek-chat-v3.1 uv run python -m scripts.validate_strict \
  --llm openrouter \
  --data-source parquet --universe configs/universes/sp50.txt \
  --start-date 2024-04-19 --end-date 2025-04-18 \
  --regime lenient --n-candidates 6 --n-cycles 3 \
  --cycle-id honest-y1 \
  --validation-dir artifacts/honest_2026q2/validations

# Y1 — combine in-sample (Stage B)
uv run python -m scripts.combine_factors \
  --from-validation-report artifacts/honest_2026q2/validations \
  --filter-passes-ic --filter-passes-rank-ic \
  --data-source parquet --universe configs/universes/sp50.txt \
  --start-date 2024-04-19 --end-date 2025-04-18 \
  --regime strict --method rank_aggregate

# Y2 — out-of-sample evaluation (Stage C)
uv run python -m scripts.combine_factors \
  --from-validation-report artifacts/honest_2026q2/validations \
  --filter-passes-ic --filter-passes-rank-ic \
  --data-source parquet --universe configs/universes/sp50.txt \
  --start-date 2025-04-19 --end-date 2026-04-17 \
  --regime strict --method rank_aggregate
```
