# Case Study — SP-50 multi-window robustness study (the systematic null)

> The designed experiment that v1/v2/v3 could not be: **does the loop
> produce alpha on average, or only by chance?**  Run 2026-07-15 as a
> predeclared 12-cell grid.  Answer, stated plainly: **on SP-50 daily
> data under the hardened evaluation contract, no — the Y2 outcomes are
> indistinguishable from no-edge.**  Run id `sp50-rolling-v1`
> (`artifacts/robustness_study/sp50-rolling-v1/record.json`), total LLM
> cost ≈ $0.0375.

## Design (predeclared before any cell executed)

- **Data:** Polygon SP-50 daily bars, backfilled 2024-07-15 → 2026-06-30
  into the local parquet store (free tier reaches back ~2 years; the
  backfill froze the snapshot so every cell reads identical data).
- **Grid:** 3 rolling splits × 2 LLMs × 2 selection strategies = 12
  cells, written to the run record before execution; `no_basket` and
  `failed` cells stay in the denominator.

  | split | Y1 (selection, lenient) | Y2 (validation, strict) |
  |---|---|---|
  | 1 | 2024-07-15 → 2025-04-15 | 2025-04-22 → 2025-10-22 |
  | 2 | 2024-10-15 → 2025-07-15 | 2025-07-22 → 2026-01-22 |
  | 3 | 2025-01-15 → 2025-10-15 | 2025-10-22 → 2026-04-22 |

  A 7-day embargo separates Y1 from Y2 (the original case studies used
  adjacent windows).
- **LLMs:** `deepseek/deepseek-chat-v3.1` and `qwen/qwen3-235b-a22b-2507`
  (Qwen-2.5-72B is retired on OpenRouter; the Qwen3 successor stands in).
- **Selection arms:** `input_order` (the case-study behaviour: all Y1
  IC/rank-IC survivors) vs `persistence` (top-4 by sub-window sign
  stability) — so the open selector-arbitration question rides the
  same grid.
- **Per cell:** `validate_strict` (3 cycles × 6 candidates, lenient,
  Bonferroni-scaled thresholds under schema v6) → `combine_factors`
  (rank-aggregate basket, strict regime, evaluated on the split's Y2 window).
- **Predeclared primary statistic:** two-sided binomial sign test on
  basket Y2 rank-IC against the no-edge 0.5; strict-regime clears
  reported alongside.

## Result

| arm | executed | Y2 rank-IC positive | strict clears |
|---|---|---|---|
| DeepSeek × input_order | 3/3 | 1/3 | 0 |
| DeepSeek × persistence | 3/3 | 1/3 | 0 |
| Qwen3 × input_order | 2/3 | 1/2 | 0 |
| Qwen3 × persistence | 2/3 | 1/2 | 0 |
| **pooled** | **10/12** | **4/10 (sign p = 0.75)** | **0** |

Pooled mean Y2 rank-IC = **−0.011**.  Per-cell Y2 rank-ICs span −0.042
to +0.028; no basket came near the strict thresholds (every failure was
the IC/rank-IC gate itself, under the session-level Bonferroni
multiplier).  The two non-executed cells are honest denominators: one
Qwen Y1 produced a single filter-surviving candidate (cannot combine),
one Qwen call hit an HTTP read timeout.

**Selector arbitration read-out:** `persistence` vs `input_order`
showed no separation (2/5 vs 2/5 positive among executed cells) — on
no-edge outcomes neither ordering can help, consistent with the HK IPO
answer-key experiment's caution.

## Interpretation (the honest one)

- This is the **systematic version of v1/v2/v3**, and it lands on the
  same side: with 10 protocol-identical Y1→Y2 samples across two LLMs,
  positives occur at coin-flip rate, the pooled mean is slightly
  negative, and nothing clears strict.  **The v1 positive now looks
  like window luck, as v2/v3 already suggested.**
- The null is *strengthened* by the universe's survivorship bias:
  SP-50 tilts toward finding spurious alpha, and none was found.
- This does **not** say LLM factor mining is dead in general — it says
  *this proposer, this DSL, this daily-OHLCV universe, these window
  sizes* have no measurable average edge.  The HK IPO microstructure
  track (order-flow features invisible to OHLCV) remains the live
  hypothesis, pending its data update.

## Caveats

- Adjacent Y2 windows overlap by 3 months (quarterly steps × 6-month
  validation); the 10 cells are partially dependent and the sign test
  is descriptive, not exact.  (The direction of the result makes this
  moot: nothing is being claimed significant.)
- Y1 is 9 months (case studies used ~12) — chosen to fit 3 splits into
  the free-tier history.
- One arm substitutes Qwen3-235B for the retired Qwen-2.5-72B.

## Reproducing

```bash
export POLYGON_API_KEY=...   # backfill once
make backfill-sp50 ARGS="--start-date 2024-07-15 --end-date 2026-06-30"

export OPENROUTER_API_KEY=...
make robustness-study ARGS="--history-start 2024-07-15 --history-end 2026-06-30 \
  --y1-months 9 --y2-months 6 --step-months 3 \
  --llms openrouter:deepseek/deepseek-chat-v3.1,openrouter:qwen/qwen3-235b-a22b-2507 \
  --run-id sp50-rolling-v1"
```
