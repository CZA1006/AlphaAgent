# Case Study — HK IPO tick microstructure (a surviving signal that did not survive confirmation)

> **Final verdict (2026-07-17, Stage 6): the signal is dead.**  The
> pre-registered confirmation on the July data update found that (a)
> the tick re-ingestion **revised the historical OFI series itself** —
> the original quote capture was incomplete, and on the completed data
> the flagship's in-sample edge shrinks from +0.138 to +0.035 — and
> (b) on the untouched fresh window every factor and both selector
> baskets failed decisively.  Stages 1–5 below are preserved as the
> honest record of how the lead was found, stress-tested, and finally
> killed by its own confirmation protocol.  The enduring lesson is
> §Stage 6's: **capture completeness is a first-order risk for
> microstructure research.**

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

## Stage 5 — Intraday v1 candidate features (2026-07-14): mostly no incremental value

The operator-approved raw-tick materialization produced
`micro_features_intraday_v1_candidate` (7,118 stock-days, 11 first-hour
/ auction features, 7-day expiring).  The features were wired into the
loader and DSL **opt-in** (`BigQueryEquitiesLoader(with_intraday_features=True)`,
`hk_ipo_micro_oos --with-intraday`) and 9 intraday candidate factors
were run through the same disjoint train/test + 78 bps pipeline
(`scripts/analysis/hk_ipo_intraday_factors.txt`), with the two daily
winners as side-by-side baselines.

**Result: first-hour features are mostly noisier versions of their
daily counterparts, not new information.**

- The intraday flagship analog `rank(first_hour_ofi) −
  rank(first_hour_rel_spread)` **failed OOS** (train +0.125 → test
  −0.004) while the daily flagship survived (+0.138 → +0.026) —
  first-hour OFI alone is a worse estimator than full-day OFI.
- Sign persistence ≈ 5/9 (coin flip), vs 10/12 for the daily factor
  set on the same windows.  Most intraday factors had negative
  long-only net OOS.
- **The one exception:** `rank(first_hour_n_trades) −
  rank(first_hour_avg_trade_size)` persisted (train +0.135 → test
  +0.026) **and beat its daily analog on the implementable form**
  (long-only hedged net +0.0261 vs +0.0178, hit 59 %).  Early
  trade-count intensity may time the activity signal better than the
  full-day version.  One factor out of nine on a 40-day window is
  within multiple-comparison noise — recorded as a *lead*, not a
  finding.

**Disposition:** the candidate table is left to expire (2026-07-21);
no permanent table is promoted.  The loader/DSL wiring stays (opt-in,
inert when the table is absent — a query against an expired candidate
fails loudly).  Re-materialize and re-run when the OOS window
lengthens; the `first_hour_n_trades` lead is the specific thing to
check first.

---

## Stage 6 — Pre-registered confirmation (2026-07-17): the signal is dead

The July data update landed: daily panel extended 2026-06-26 →
2026-07-17, universe 77 → 128 IPOs, tick lake 86.1 M → 111.8 M rows.
The pre-registered confirmation checklist was executed the same day
(same 12 factors, same scripts, original 77-stock universe for
comparability).  Two independent findings, both fatal:

### Finding 1 — the re-ingestion revised history: the OFI edge was partly a capture artifact

For the **same 77 stocks over the same period**, the rebuilt lake
holds **98.1 M events vs 86.1 M** (+14 %), almost entirely additional
*quote* events.  More quotes → different mids → different Lee-Ready
trade signing → a revised OFI series.  The internal contrast is
decisive — identical train window, identical universe:

| factor (train rank-IC) | original capture | completed capture |
|---|--:|--:|
| `rank(ts_mean(n_trades,5)) − rank(avg_trade_size)` (count-based) | +0.1338 | **+0.1338** (bit-identical) |
| `rank(ts_mean(high−low,5)) − rank(ts_mean(rel_spread,5))` (mildly quote-dep.) | +0.1403 | +0.1307 |
| `rank(ofi) − rank(rel_spread)` (flagship) | +0.1381 | **+0.0351** |
| three further OFI factors | +0.03…+0.04 | **sign-flipped in-sample** |

Trade-derived quantities were stable; every quote-derived quantity
moved; the OFI factors moved most.  **The flagship's documented edge
was substantially an artifact of incomplete quote capture**, not (as
the honest-but-wrong Stage 2 read had it) order-flow information the
OHLCV bars couldn't see.

### Finding 2 — the untouched fresh window (2026-06-27 → 2026-07-17) was a rout

On the never-touched 14-trading-day window, all 12 factors had deeply
negative long-only hedged net returns (−6.5 % to −14 % per 5 days, hit
rates 0–14 %) — the IPO panel fell hard against HSI across the board,
a regime event no factor tilt survives in long-only form.  The
**selector arbitration** (pre-registered third test) returned "no
winner": by-trainIC basket −0.2546 test rank-IC vs by-persistence
−0.2780, both hit 0 %.  Persistence selection stays opt-in and is now
moot for this factor set.

### Verdict

- The headline claim of this case study — "the first signal to survive
  the full gauntlet" — **is withdrawn.**  It survived the gauntlet on
  incomplete data and did not survive data completion plus one fresh
  window.
- The confirmation protocol worked exactly as designed: the lead was
  pre-registered, re-tested once on new data, and killed — instead of
  compounding into a false conviction.
- **Methodological takeaway for any future microstructure work:**
  quote-capture completeness must be a tracked, versioned property of
  the dataset (the doctor already counts rows; it should fingerprint
  coverage per stock-day), and any quote-derived result needs a
  capture-stability check before it is called a lead.

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
  used here (`--with-intraday` joins the intraday v1 candidate table).
- `scripts/analysis/hk_ipo_intraday_factors.txt` — the Stage-5
  intraday candidate factors + daily baselines.
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
