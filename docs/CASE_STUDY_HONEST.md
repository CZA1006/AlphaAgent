# Honest Case Study — disjoint train / test

> The Q2 2026 case study had a methodology gap the audit couldn't fix
> in code: components were *selected* on the same window the basket was
> *evaluated* on, with only a TAIL holdout as the out-of-sample slice.
> This study fixes that by splitting the universe into two
> non-overlapping years: components are picked on Year 1 only; the
> basket is then evaluated on Year 2 only, with no second look at Y1.

All artifacts under `artifacts/honest_2026q2/`.

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

## Verdict

The Round 6 combination thesis, evaluated honestly with disjoint
selection and validation windows on real DeepSeek + real Polygon SP-50
data, **does not hold for this run**.

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
   The Round 6 thesis predicts that decorrelated weak factors will
   combine to clear strict; correlated weak factors will not.  This
   run produced the second condition.

This is **the kind of result the harness is supposed to produce**.
The earlier "case study success" was driven by data-snooping (same
window for selection and evaluation) and a measurement bug
(combiner bypassed `HoldoutPolicy`).  With the methodology bug
fixed (disjoint windows) and the code bug fixed (commit `c535059`),
the system correctly says: "this batch of LLM-proposed factors does
not survive."

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
