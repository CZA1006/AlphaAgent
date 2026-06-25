# Honest Case Study v2 — robustness check

> The first honest case study
> ([`docs/CASE_STUDY_HONEST.md`](CASE_STUDY_HONEST.md), post-fix
> section) showed the agent loop producing a basket that cleared
> strict on both gates on a held-out year (Y2 = 2025-04-19 →
> 2026-04-17).  This v2 re-runs the same setup with the selection
> window slid by ~2 months to check whether the positive verdict
> generalized.
>
> **Spoiler: it didn't.**  Different selection window → different
> survivor pool → in-sample looks even cleaner → out-of-sample
> sign-flips.

All artifacts under `artifacts/honest_v2/`.

## Setup deltas vs the original honest study

| | original honest (post-fix) | CSv2 |
|---|---|---|
| Y1 (selection) | 2024-04-19 → 2025-04-18 (12 mo) | 2024-06-25 → 2025-05-21 (11 mo) |
| Y2 (test) | 2025-04-19 → 2026-04-17 (12 mo) | 2025-05-22 → 2026-04-17 (11 mo) |
| LLM | DeepSeek-Chat-v3.1 | DeepSeek-Chat-v3.1 (same) |
| Universe | SP-50 | SP-50 (same) |
| Theme | identical | identical |
| Regime | strict for combine, lenient for select | same |
| Audit fixes | Findings 1, 2 fixed; 9 still active | Findings 1, 2, 9 all fixed |

Polygon's free tier caps at the trailing 2 years from "today"
(2026-06-25 in our session), so a true 6-month slide back was
impossible — the data we'd need (2023-10 → 2024-04) is no longer
on the API.  The 2-month slide is what the available data supports;
Y2 windows almost completely overlap (both end 2026-04-17), so this
is really a test of "does a slightly different selection window
produce a robust basket?"

## Stage A — Y1 selection (lenient, 3 cycles, 6 candidates)

18 evaluations, **13** with both metrics positive (vs 6 in the
original honest study).  The post-Finding-9 SQE no longer inflates
IC, but on a slightly different window the LLM produced a different
— and surprisingly broad — survivor pool: a mix of
reversal-with-volatility-scaling and price-vs-VWAP factors.

Top 8 by in-sample rank_IC:

| expression | Y1 ic | Y1 ric | Y1 ho_ic | Y1 ho_ric |
|---|---:|---:|---:|---:|
| `rank(ts_delta(close, 10) / ts_std(close, 42))` | +0.029 | +0.034 | −0.178 | −0.387 |
| `rank((high - low) / ts_mean(close, 5) * -ts_delta(close, 3))` | +0.028 | +0.031 | −0.002 | −0.010 |
| `rank(-ts_delta(close, 5))` | +0.015 | +0.024 | +0.149 | +0.309 |
| `rank(-(close - ts_mean(vwap, 5)))` | +0.015 | +0.020 | +0.006 | +0.041 |
| `rank(ts_delta(close, 15) / ts_std(close, 30))` | +0.006 | +0.020 | −0.158 | −0.369 |
| `rank(ts_delta(close, 5) / ts_std(close, 21))` | +0.025 | +0.019 | −0.144 | −0.344 |
| `rank((close - ts_min(low, 10)) / (ts_max(high, 10) - ts_min(low, 10)))` | +0.008 | +0.013 | −0.079 | −0.152 |
| `rank(zscore(volume) * -ts_delta(close, 2))` | +0.015 | +0.012 | +0.014 | +0.026 |

Notice: per-factor holdout rank_IC is wildly inconsistent — some
strongly positive, some strongly negative.  The lenient regime
correctly accepts all of them onto the "both-positive in-sample"
list (it doesn't read holdout for selection), but the holdout
fields make it visible that several factors are already known to
decay.

## Stage B — Y1 combination (in-sample)

| method | basket Y1 ic | basket Y1 rank_ic | strict (Y1)? |
|---|---:|---:|---|
| rank_aggregate | **+0.0352** | **+0.0440** | ✅ both |
| zscore_average | **+0.0352** | **+0.0436** | ✅ both |
| equal_weight | **+0.0352** | **+0.0443** | ✅ both |

avg pairwise rank-corr: **−0.0068** (essentially zero — even more
decorrelated than the original honest study's +0.08).

**In-sample the basket looks great** — better-decorrelated pool,
basket clears strict on all three combination methods, IC higher
than the original honest study (`+0.035` vs `+0.033`).

## Stage C — Y2 out-of-sample (the headline)

Same component set on Y2 (never seen during selection):

| method | basket Y2 ic | basket Y2 rank_ic | strict (Y2)? |
|---|---:|---:|---|
| rank_aggregate | **−0.0234** | **−0.0137** | ❌ both |
| zscore_average | **−0.0234** | **−0.0143** | ❌ both |
| equal_weight | **−0.0234** | **−0.0134** | ❌ both |

avg pairwise rank-corr: −0.0055.

**The basket sign-flips out-of-sample.**  All three methods produce
negative IC and negative rank_IC on Y2.

## Joint verdict across the two honest studies

| | original honest (post-fix) | CSv2 |
|---|---|---|
| Y1 IC / rank_IC | +0.033 / +0.049 | +0.035 / +0.044 |
| Y1 strict? | ✅ both | ✅ both |
| **Y2 IC / rank_IC** | **+0.058 / +0.053** | **−0.023 / −0.014** |
| **Y2 strict?** | **✅ both (stronger than IS)** | **❌ both (sign-flipped)** |

Two case studies with almost-identical settings — same LLM, same
universe, same Y2 window (both end 2026-04-17), Y1 windows
offset by ~2 months — produce **opposite** out-of-sample
verdicts.  The in-sample numbers don't predict the out-of-sample
outcome.

This is not the harness failing.  This is the harness *correctly
surfacing* a property of marginal LLM-proposed alpha on SP-50: it
doesn't generalize robustly across small shifts in the selection
window.  The audit-fixed evaluator stack accurately reports both:

- *yes*, the Y1 basket clears strict in-sample (this is true)
- *yes*, the Y2 basket fails strict out-of-sample (this is also true)

Both honest case studies, taken together, are evidence that
**single-LLM, single-universe, single-window results are not yet
robust enough to call "alpha".**  The first study's positive
result was real-but-fragile.

## What this tells you about the project

The pre-audit case study's positive result was an artifact of
methodology + measurement bugs.  Once we fixed those, the first
honest study showed the architecture *can* produce out-of-sample-
validated alpha on real data.  This v2 shows the result doesn't
hold up under a tiny perturbation of the selection window.

**Truthful summary:**

1. The architecture works as designed: end-to-end LLM-driven
   research, honest measurement, correct refusal-or-promotion.
2. The architecture is not by itself a money printer.  Whether a
   *particular* LLM proposal pool generalizes is an empirical
   question that requires multiple disjoint windows / universes /
   LLMs to answer.
3. For a research-tool perspective: this is a great result.  The
   audit-fixed harness is telling us "your Y1 selection didn't
   transfer to Y2" in a way the pre-audit harness could not.
4. For a "ship a strategy" perspective: we'd need many more honest
   case studies before claiming any basket is tradable.

## Next-step candidates

- **Run CSv2 against a different LLM** (Qwen, Mistral via OpenRouter)
  on the same window to factor out LLM-specific proposal style.
- **Run CSv2 with rolling re-selection** — refit Y1 → Y2 quarterly,
  not annually — to see if a faster-adapting basket holds up better.
- **Broaden the universe** beyond SP-50 to reduce survivorship + give
  the LLM more decorrelation budget.

None of those are blocking; the harness is correct.  They're
research experiments for a "does this loop produce robust alpha
in general?" question that one case study cannot answer.

## Reproducing

```bash
set -a; source .env; set +a
mkdir -p artifacts/honest_v2

# Y1 — selection only
OPENROUTER_MODEL=deepseek/deepseek-chat-v3.1 uv run python -m scripts.validate_strict \
  --llm openrouter \
  --data-source parquet --universe configs/universes/sp50.txt \
  --start-date 2024-06-25 --end-date 2025-05-21 \
  --regime lenient --n-candidates 6 --n-cycles 3 \
  --cycle-id csv2-y1 \
  --validation-dir artifacts/honest_v2/validations

# Y2 — out-of-sample evaluation
uv run python -m scripts.combine_factors \
  --from-validation-report artifacts/honest_v2/validations \
  --filter-passes-ic --filter-passes-rank-ic \
  --data-source parquet --universe configs/universes/sp50.txt \
  --start-date 2025-05-22 --end-date 2026-04-17 \
  --regime strict --method rank_aggregate
```
