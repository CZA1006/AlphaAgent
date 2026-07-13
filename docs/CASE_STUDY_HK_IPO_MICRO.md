# Case Study — HK IPO tick microstructure (the first surviving signal)

> The first AlphaAgent research where a signal survives the full honest
> gauntlet: a disjoint train/test split **and** realistic transaction
> cost **and** a long-only-implementable form.  It is also the project's
> most nuanced result — promising but not yet a confirmed tradable
> alpha, bottlenecked by data quantity.  Read this alongside the US
> case studies (`CASE_STUDY_HONEST*.md`) — the contrast is the point.

Data: real Bloomberg HK IPO tick + daily, in GCP
(`bloomberg-database-0629.hk_ipo_research`), 77 IPOs, daily panel
2025-12-03 → 2026-06-26, target tick lake 86.1 M rows
(`tick_manifest_target`; the legacy manifest can include wider capture).
LLM: DeepSeek-Chat-v3.1.

---

## Why this market, after the US null

Three US case studies found at best fragile, non-replicating daily
alpha on SP-50.  The operator's prior (separate) research also found
**no alpha** in HK IPO daily/hourly OHLCV over the listing→6-month-
lockup window.  The shared suspect: **OHLCV bars cannot express
order flow** — a bar tells you open/high/low/close, not whether trades
were buyer- or seller-initiated.  So we built tick-derived
microstructure features and asked: does *order-flow* information,
which the prior OHLCV research literally could not see, carry signal?

Microstructure features (per stock-day, computed server-side in
BigQuery from the tick lake — see `scripts/sql/micro_features_daily.sql`):
`ofi` (order-flow imbalance, Lee-Ready), `rel_spread`, `realized_vol`
(1-min sampled), `n_trades`, `tick_volume`, `avg_trade_size`,
`n_quotes`.  Joined into the panel as DSL fields so the LLM can
propose microstructure factors.

---

## Stage 1 — Full-window selection (lenient)

18 DeepSeek proposals over 3 cycles, **8 promoted — and 8/8 used
microstructure fields** (pure price/volume did not win on its own).
Flagship: `rank(ofi) - rank(rel_spread)` (buy high net-buying +
tight-spread names) — IC +0.148, rank_IC +0.141.  In-sample only, and
lenient — so this is a *lead*, not a result.

---

## Stage 2 — Disjoint train/test (the honest test)

- **Train (select):** 2025-12-12 → 2026-04-30
- **Test (held out):** 2026-05-01 → 2026-06-26

Fresh DeepSeek selection on train → **12 both-positive factors, all
microstructure**.  `rank(ofi) - rank(rel_spread)` reappears
independently (robustness sign).

**Per-factor persistence (train+ → test+ on rank_IC): 10/12.**
Under a no-edge null, disjoint-window sign agreement is ~50 %; 10/12 =
83 % has binomial p ≈ 1.9 %.  Several OFI factors got *stronger* OOS:

| factor | train rank_IC | test rank_IC |
|---|--:|--:|
| `rank(ofi) - rank(rel_spread)` | +0.138 | +0.026 |
| `zscore(ofi) * (1 - rank(rel_spread))` | +0.043 | +0.061 |
| `rank(ts_mean(ofi,5)) - rank(ts_mean(realized_vol,5))` | +0.030 | +0.086 |
| `rank(ts_mean(high-low,5)) - rank(ts_mean(rel_spread,5))` | +0.140 | −0.003 |

**But the promoted-by-strict-gates basket FAILED OOS** (rank_aggregate
−0.044).  Why: the gates selected the highest-train-IC factors — one of
which (`high-low - rel_spread`) overfit and flipped — while *rejecting*
some of the most persistent OFI factors (tail-concentration gate firing
on IPO first-day spikes).  The promoted set was also highly correlated
(+0.32), so no diversification.  **Lesson: on a short, IPO-noisy panel
the current selection machinery picks the wrong subset — train-IC is
not persistence.**

---

## Stage 3 — Cost realism (half-spread)

Measured IPO mean `rel_spread` = 0.78 % → **half-spread ≈ 39 bps**
(the harness default assumes only 5 bps).  Break-even cost
(`quantile_spread / turnover`) for the survivors is mostly **200–3600
bps**, far above 39 bps → **11/12 survive the half-spread hurdle**.
Necessary but not sufficient — this ignores market impact and short-
borrow feasibility.

---

## Stage 4 — Long-only, market-hedged, net of cost (the decisive form)

HK IPOs are hard to short during the 6-month lockup, so the
implementable strategy is **long the top-quintile microstructure
basket, hedge market with short HSI futures** (liquid, cheap), net of
the real 78 bps round-trip IPO spread.  OOS test window:

| factor | net 5-day excess | hit % |
|---|--:|--:|
| `rank(ts_mean(high-low,5)) - rank(ts_mean(rel_spread,5))` | **+0.0185** | 59 |
| `rank(ts_mean(n_trades,5)) - rank(avg_trade_size)` | **+0.0178** | 53 |
| `rank(ofi) - rank(rel_spread)` (flagship) | **+0.0104** | 53 |
| `rank(ts_delta(vwap,1)) - rank(ts_mean(avg_trade_size,3))` | +0.0089 | 52 |
| remaining 8 | ≈0 or negative | ~50 |

**4 of 12 stay positive net of cost out-of-sample, including the OFI
flagship.**  This is the first AlphaAgent signal to clear *all* of:
disjoint OOS + realistic cost + long-only-implementable.

---

## Honest verdict

**There is a real microstructure (order-flow) signal in HK IPOs that
survives the full gauntlet for a handful of factors — but it is modest,
and not yet a confirmed tradable alpha.**

The cold water, stated plainly:
- **Hit rates 47–59 %** — the flagship is 53 %, barely above a coin.
- **Test window is ~40 trading days** (~6–8 independent 5-day periods).
  Sampling noise is enormous; the annualized figures (+50–90 %) are
  *not* trustworthy on this sample and are deliberately omitted from
  the headline.
- **Cost model is still rough** — daily-turnover vs 5-day-hold
  mismatch, no market-impact term.
- **The strongest OOS net factors lean on trade-count / range**, not
  pure OFI; the OFI flagship is positive-but-modest.

What it is *not*: a money printer, or a finished strategy.  What it
*is*: the first time the honest pipeline produced a signal that didn't
die at the first out-of-sample or cost hurdle — and it did so exactly
where theory said to look (order flow, which OHLCV bars can't see).

---

## The real bottleneck: data quantity

The signal isn't limited by the factors or the engine — it's limited
by **40 days of out-of-sample data**, which can't carry statistical
confidence.  This is precisely why the data-scaling work matters:

- More IPOs accumulate over time (77 today, growing).
- The tick archive grows daily (Bloomberg's 140-day limit means it
  must be captured continuously — see `BLOOMBERG_DATASET_BUILD.md`).

As the OOS window lengthens, re-running this exact
disjoint + cost + long-only pipeline is what turns "a real lead" into
"confirmed or rejected."

## What's reproducible

- `scripts/sql/micro_features_daily.sql` — the tick → feature table.
- `scripts/sql/tick_manifest_target.sql` — target-scope tick coverage.
- `scripts/sql/ipo_event_terms_curated.sql` and
  `scripts/sql/ipo_event_features_daily.sql` — HKEX document-derived
  event dates and daily event features.
- `scripts/analysis/hk_ipo_micro_oos.py` — the per-factor
  train/test persistence + cost-realism + long-only-hedged analysis
  used here.
- `scripts/analysis/lockup_event_study.py` — exact-event study over
  curated HKEX/prospectus event dates.
- `configs/universes/hk_ipo.txt` — the 77-name universe.

## Next directions

1. **More data** — re-run as the OOS window lengthens (the binding
   constraint).
2. **Event-conditioned microstructure** — the HKEX document refill now
   provides exact greenshoe, stabilization, and cornerstone lockup dates.
   Event studies on those dates so far are honest nulls (see
   [`DESIGN_LOCKUP_EVENT_STUDY.md`](DESIGN_LOCKUP_EVENT_STUDY.md) §8–§10).
3. ~~**Selection fix**~~ **Built and backtested — a mixed result**
   (2026-07-13).  `alpha_harness/evaluators/persistence.py` scores
   factors by sub-window rank-IC sign consistency + stability instead
   of train-window mean;
   `scripts/analysis/hk_ipo_persistence_selection.py` replays the
   selection on this study's 12-factor answer key (4 embargoed
   sub-windows inside the train window; top-4 baskets; same test
   window).  Honest findings:
   - The known trap (`high-low − rel_spread`, highest train rank-IC,
     flipped OOS) is **excluded by construction** — its fold profile
     (+0.33/+0.21/−0.03/+0.15) loses to four factors positive in every
     fold.  The persistence basket's OOS rank-IC **doubles**
     (+0.0156 → +0.0329) and its internal correlation drops
     (+0.45 → +0.34).
   - **But neither ordering predicts OOS at the factor level** on this
     one window: selector→OOS Spearman is −0.55 (train-IC) vs −0.51
     (persistence) — the best OOS factors were *low*-persistence, and
     the persistence basket's long-only hedged net is *worse*
     (+0.0085 vs +0.0174).  One 40-day window cannot validate a
     selector; this is a mechanism demonstration, not proof.
   - **The tail-concentration hypothesis was refuted**: excluding each
     stock's first 5 trading days flips the gate on 0/12 factors (the
     concentration is real, not a debut artifact).  The sharper
     finding: 4 of the 5 gate-firing factors were OOS-*positive*
     (including the best performers) — on this panel the gate's
     criterion mispredicts OOS failure.  Recalibration should demote
     or re-threshold the gate for IPO profiles, not exclude debut
     days.
   - **Decision:** the persistence machinery is in the harness and
     unit-tested, but stays *opt-in* until the lengthened OOS window
     can arbitrate — changing the default selector on one window's
     evidence would repeat the original mistake in the other
     direction.
