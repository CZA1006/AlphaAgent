# Honest Case Study v3 — different LLM, same window

> CSv2 ([`docs/CASE_STUDY_HONEST_V2.md`](CASE_STUDY_HONEST_V2.md))
> showed a DeepSeek-proposed basket that looked great in-sample on
> Y1 = 2024-06-25 → 2025-05-21 but sign-flipped out-of-sample on
> Y2 = 2025-05-22 → 2026-04-17.  This v3 re-runs the same setup
> with one variable changed — **the LLM** — to test whether the
> negative Y2 verdict was DeepSeek-specific.

All artifacts under `artifacts/honest_v3/`.

## Setup deltas vs CSv2

| | CSv2 | CSv3 |
|---|---|---|
| LLM | `deepseek/deepseek-chat-v3.1` | **`qwen/qwen-2.5-72b-instruct`** |
| Y1 / Y2 | identical | identical |
| Universe | SP-50 | SP-50 |
| Theme | identical | identical |
| Regime | strict for combine, lenient for select | same |
| Audit fixes | all 3 CRITICAL fixed | same |

Qwen survived the proof-of-life call cleanly — no region block.

## Stage A — Y1 selection (Qwen, 3 cycles, 6 candidates)

18 evaluations, **3** with both metrics positive — much narrower
pool than DeepSeek's 13 on the identical window.  Qwen's proposals
were also structurally more conservative: 2 of 3 survivors are
single-operator volume momentum, 1 is a vol-of-vol ratio.

| expression | Y1 ic | Y1 ric | Y1 ho_ic | Y1 ho_ric |
|---|---:|---:|---:|---:|
| `rank(ts_std(close, 5) / ts_std(close, 20))` | +0.032 | +0.045 | +0.008 | +0.065 |
| `rank(ts_delta(volume, 5))` | +0.012 | +0.030 | −0.240 | −0.103 |
| `rank(ts_delta(volume, 1))` | +0.010 | +0.008 | −0.046 | −0.091 |

The Y1 holdout block already shows decay on 2 of 3 factors — the
volume-momentum pair has strongly negative holdout IC and rank_IC.
A diligent operator could have caught this at promotion time.

## Stage B — Y1 combination (in-sample)

| method | basket Y1 ic | basket Y1 rank_ic | strict (Y1)? |
|---|---:|---:|---|
| rank_aggregate | **+0.0253** | **+0.0360** | ✅ both |
| zscore_average | **+0.0253** | **+0.0363** | ✅ both |
| equal_weight | **+0.0253** | **+0.0357** | ✅ both |

avg pairwise rank-corr: **+0.186** (vs CSv2's −0.007 — Qwen's
narrower pool is more correlated, which is a red flag the
combination thesis doesn't want).

## Stage C — Y2 out-of-sample

| method | basket Y2 ic | basket Y2 rank_ic | strict (Y2)? |
|---|---:|---:|---|
| rank_aggregate | **−0.0359** | **−0.0428** | ❌ both |
| zscore_average | **−0.0359** | **−0.0423** | ❌ both |
| equal_weight | **−0.0359** | **−0.0429** | ❌ both |

avg pairwise rank-corr: +0.187.

**Sign-flips on both gates — more strongly than CSv2.**

## Joint verdict across all three honest case studies

| # | LLM | Y1 window | Y2 window | Y1 IC / ric | Y2 IC / ric | Y1 ✅? | Y2 ✅? |
|---|---|---|---|---|---|---|---|
| v1 | DeepSeek | 2024-04 → 2025-04 | 2025-04 → 2026-04 | +0.033 / +0.049 | +0.058 / +0.053 | ✅ | **✅** |
| v2 | DeepSeek | 2024-06 → 2025-05 | 2025-05 → 2026-04 | +0.035 / +0.044 | −0.023 / −0.014 | ✅ | ❌ |
| v3 | Qwen | 2024-06 → 2025-05 | 2025-05 → 2026-04 | +0.025 / +0.036 | **−0.036 / −0.043** | ✅ | ❌ |

Two LLMs, identical Y2 window (2025-05-22 → 2026-04-17), both
sign-flip.  This narrows the diagnosis:

- **It is not LLM-specific.**  DeepSeek and Qwen, proposing
  structurally different factor families, both produce baskets
  that fail on this Y2.
- **It is largely window-specific.**  v1's Y2 (2025-04 onward)
  cleared strict; v2/v3's Y2 (2025-05 onward, only 1 month later
  start) failed.  Whatever changed in May 2025 broke both kinds
  of basket.
- **The v1 positive result is now suspect.**  One positive
  against two negatives on overlapping Y2 windows is the
  literature-standard "single-positive-result-doesn't-replicate"
  pattern.  The honest read is: **the v1 result was real-but-
  fragile, and the underlying alpha is not robust across small
  perturbations of the validation window.**

## What this tells us about the architecture

1. **The architecture works correctly on all three runs.**  Each
   honest case study produces verifiably-correct in-sample and
   out-of-sample metrics.  The audit infrastructure is doing its
   job.
2. **Single-window, single-LLM, single-universe case studies are
   not enough to claim "the system produces alpha."**  We have
   one positive (v1) and two negatives (v2, v3) on very similar
   conditions.  No serious quant claim is supportable on that
   evidence.
3. **The framework is now strong enough to *settle* the question
   with the right experiment.**  What we need is a planned
   multi-run study: many disjoint Y1/Y2 windows, multiple LLMs,
   multiple universes, all running through the same audit-clean
   pipeline.  Then count: what fraction of basket runs clear
   strict on Y2 vs what you'd expect from a no-edge null?  That
   number is the answer.

## Honest summary

The project's earlier "this loop produces alpha" claim, refined
through three honest case studies, becomes:

> **The architecture honestly measures whether LLM-proposed factor
> combinations have out-of-sample edge.  On real SP-50 data with
> two different LLMs, single-window results are not robust — one
> positive Y2 verdict out of three honest attempts.  A planned
> multi-run study is the next step to settle whether the loop
> produces alpha *on average* or only by chance.**

That's a defensible scientific statement.  It says nothing more
than what the data supports.

## Reproducing

```bash
set -a; source .env; set +a
mkdir -p artifacts/honest_v3

# Y1 — selection (Qwen)
OPENROUTER_MODEL=qwen/qwen-2.5-72b-instruct uv run python -m scripts.validate_strict \
  --llm openrouter \
  --data-source parquet --universe configs/universes/sp50.txt \
  --start-date 2024-06-25 --end-date 2025-05-21 \
  --regime lenient --n-candidates 6 --n-cycles 3 \
  --cycle-id csv3-y1 \
  --validation-dir artifacts/honest_v3/validations

# Y2 — out-of-sample evaluation
uv run python -m scripts.combine_factors \
  --from-validation-report artifacts/honest_v3/validations \
  --filter-passes-ic --filter-passes-rank-ic \
  --data-source parquet --universe configs/universes/sp50.txt \
  --start-date 2025-05-22 --end-date 2026-04-17 \
  --regime strict --method rank_aggregate
```
