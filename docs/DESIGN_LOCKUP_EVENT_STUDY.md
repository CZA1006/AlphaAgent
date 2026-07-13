# Design — HK IPO 6-month lockup-expiry event study (tick)

> Minimal-viable design for the highest-theory-value untested direction:
> using tick order flow around the **6-month lockup expiry** — HK IPO's
> most documented anomaly.  The MVP (`scripts/analysis/lockup_event_study.py`)
> has now been run on **proxy dates** (`listing + 6 mo`): a clean negative
> (§8 — the placebo caught a false positive); on **exact
> prospectus-extracted dates**: an initially-promising signature that an
> audit traced to a *single contaminated event date* — the corrected
> N = 13 result shows **no expiry-day abnormal-return effect**, with a
> persistent post-event OFI net-sell run as the only H1-consistent
> residual (§9); and on the two larger event types, **greenshoe expiry
> (56) and stabilization end (34): clean nulls** — and the run's real
> product was catching 13 impossible curated event dates and hardening
> the curation + script against them (§10).

---

## 0. Feasibility (checked against the real data — honest first)

The lockup-expiry date ≈ `listing_date + 6 months`.  Of the 77 IPOs,
**only 19 have their expiry inside the tick window** (2025-12-12 →
2026-06-26) — the rest listed too early (tick archive starts 2025-12)
or too late (expiry after 2026-06).

**19 events is small.**  This study can detect a *strong* average
effect but not a subtle one, and any cross-sectional scaling test
(overhang → pressure) on 19 points is suggestive at best.  That is the
binding limitation and must headline any result — same lesson as the
microstructure case study: the bottleneck is data quantity, and it
improves automatically as the tick archive and IPO count grow.

---

## 1. The hypotheses (why this should have signal)

At the 6-month mark, cornerstone investors' shares unlock → a supply
overhang hits the market.  The classic, economically-grounded
predictions:

- **H1 — Selling pressure at expiry.**  Abnormal (market-hedged)
  returns are negative and order flow (`ofi`) turns net-sell in a
  window around τ = 0 (the expiry date).
- **H2 — Overhang scaling.**  The effect is larger for stocks with a
  bigger unlock — i.e. higher `cornerstone_pct_of_post_ipo_share_capital`
  (or `cornerstone_pct_of_offer_total`).
- **H3 — Pre-positioning.**  Informed flow front-runs: `ofi` /
  abnormal return weaken *before* τ = 0, not only on the day.

These use information the operator's prior OHLCV research couldn't see
(order-flow direction) *and* a catalyst daily cross-sectional factors
wash out (a stock-specific scheduled event).

---

## 2. Data

| Need | Source |
|---|---|
| Event date (lockup expiry) | `ipo_master.listing_date` + 6 months (proxy; ideally the exact prospectus date later) |
| Overhang size | `hkex_cornerstone_investors` (`pct_of_post_ipo_share_capital`, `shares_allocated`) and `hkex_ipo_allotment_summary` (`cornerstone_pct_of_offer_total`, `cornerstone_shares_total`) |
| Daily abnormal return | `ipo_daily_prices` (stock) − `market_factors_daily` (HSI) |
| Tick order flow / spread / vol around event | `micro_features_daily` (already built: `ofi`, `rel_spread`, `realized_vol`, …) |

The microstructure features needed already exist — `micro_features_daily`
gives per-(stock, day) `ofi` etc., which is exactly what an event-time
profile aggregates.

---

## 3. Methodology (event study ≠ cross-sectional IC)

The harness today aligns by **calendar date** and correlates a signal
with forward returns across the universe.  An event study aligns by
**event time** and averages across events:

1. **Event time.**  For each of the 19 events, define
   τ = (trading_date − expiry_date) in trading days, keep τ ∈ [−10, +10].
2. **Per-event, per-τ measures.**
   - abnormal return AR(τ) = stock 1-day return − HSI 1-day return
   - order flow OFI(τ) = `ofi` (already net-signed)
   - spread/vol change vs the pre-event baseline (τ ∈ [−10, −6])
3. **Aggregate across events.**
   - average AR(τ) and cumulative CAR(τ) = Σ AR over the window
   - average OFI(τ) and cumulative ΣOFI(τ)
4. **Tests.**
   - H1: is CAR over τ ∈ [−1, +3] significantly < 0?  (t-test across the
     19 events; report the t-stat *and* the raw N — with 19 events, be
     explicit about power.)
   - H2: cross-sectional regression of each event's CAR on its
     overhang % — sign and significance (19 points → descriptive).
   - H3: is mean OFI(τ) < 0 for τ < 0?
5. **Honesty controls.**
   - A **placebo** at a non-event date (e.g. τ relative to a random
     mid-life date) to confirm the effect is specific to expiry.
   - Report effect size in return terms, not just t-stats.

---

## 4. How it fits the harness

This is a **new evaluation shape** the cross-sectional engine doesn't
have.  Three options, increasing cost:

- **(MVP — recommended) Standalone analysis script.**
  `scripts/analysis/lockup_event_study.py`, same style as
  `hk_ipo_micro_oos.py`: pull the 19 events + overhang + daily/HSI +
  `micro_features_daily`, build the event-time panel, print CAR/OFI
  profiles + the H1/H2/H3 tests + placebo.  **No harness change.**  If
  there's no signal in 19 events, we stop here cheaply.
- **(If MVP shows signal) An event-study evaluator mode.**  A new
  `evaluators/event_study.py` that the regime/judge can call — aligns by
  an event-date column, computes CAR/abnormal-OFI, gates on event-window
  significance.  Real work; only worth it if the MVP is promising.
- **(Later) LLM proposes event factors.**  Expose `days_to_lockup` and
  `cornerstone_overhang_pct` as panel fields so the proposer can invent
  event-conditioned factors (e.g. `ofi * (days_to_lockup < 3)`).  Needs
  the DSL to support the new fields + a comparison operator (the DSL has
  no booleans today) — the largest change.

**Recommendation: build only the MVP script first.**  It answers "is
there a lockup-expiry order-flow effect at all?" for ~1 script's worth
of work and zero harness risk, and it reuses everything already built
(`micro_features_daily`, the BigQuery loader, the HSI hedge logic).

---

## 5. Honest limitations (state these up front in any result)

- **19 events.**  Detects only a strong average effect; the overhang-
  scaling test is descriptive.  Grows with the archive.
- **Lockup date is a `listing + 6 months` proxy.**  Real prospectus
  lockup dates can differ (and pre-IPO holders may have different terms
  than cornerstones).  Refining to exact dates is a later improvement.
- **Tick coverage at the event.**  Some events sit near the window
  edge; `micro_features_daily` coverage at τ must be checked per event.
- **Implementability.**  A short-the-overhang trade needs borrow, which
  IPOs lack — so as with the microstructure study, the realistic form is
  long-biased (e.g. avoid / underweight high-overhang names into expiry,
  or a market-hedged tilt), not a clean short.

---

## 6. Proposed phasing

1. **MVP analysis script** (`lockup_event_study.py`) — CAR + abnormal-OFI
   event profiles over the 19 events, H1/H2/H3 + placebo, all honest
   about N.  ~1 script, no harness change.
2. **Decide** from the MVP: real effect → proceed; nothing → stop, and
   the negative result is itself worth recording.
3. **(Conditional) event-study evaluator** in the harness, then expose
   `days_to_lockup` / overhang as DSL fields for the LLM.

---

## 7. What I'd need to start the MVP

Nothing new — all data is in place (`ipo_master`,
`hkex_cornerstone_investors`, `ipo_daily_prices`, `market_factors_daily`,
`micro_features_daily`).  The MVP is a self-contained script + a short
results section appended here.

---

## 8. MVP result on proxy dates — a clean negative (placebo did its job)

Built as `scripts/analysis/lockup_event_study.py` and run on the **19**
IPOs whose `listing + 6 months` expiry falls in the tick window.

**Event-time abnormal-return profile** (AR = stock − HSI, market-hedged):
the negative drift is concentrated *before* the event (CAR ≈ −7.8 % by
τ = −4) and goes **flat around the expiry itself** (τ = 0).

| test | result | verdict |
|---|---|---|
| **H1** selling pressure at expiry | CAR[−1,+3] = **+0.49 %**, t = +0.18, N = 19 | ❌ inconclusive — no pressure concentrated at expiry |
| **H2** overhang scaling | corr(CAR, cornerstone %) = −0.16, N = 10 | ⚠️ weak / noise |
| **H3** pre-positioning | mean ofi(τ∈[−5,−1]) = −0.010, t = −0.24, N = 95 | ❌ insignificant |
| **placebo** (CAR[−1,+3] at a non-event date, τ₀ − 40 d) | **−9.45 %, t = −2.25**, N = 19 | 🚨 *more* negative + significant than the real event |

**The placebo is the punchline.**  Without it, the −4 % CAR and
"persistent pre-event weakness" could be mis-read as a lockup effect.
The placebo shows a *larger, significant* negative CAR at a random
non-event date — so the negativity is **general post-IPO drift, not an
expiry-specific anomaly**.  Sensible economically: post-IPO
underperformance plays out in the first months; by the 6-month expiry
the selling is already done, and the expiry is a non-event.

**Conclusion:** on the current data (19 events, `listing + 6 mo` proxy),
**no evidence of a lockup-expiry order-flow anomaly.**  Per §6 this is a
"stop here" — and the negative result is itself worth recording.

**Honest limits (why this isn't a hard "no"):** N = 19 (and τ > +3
coverage drops to 8–16 at the data edge) → underpowered; the lockup date
is a `listing + 6 mo` proxy (true cornerstone / pre-IPO unlock dates may
differ and smear the event); the broad post-IPO drift dominates.  Both
this study and the microstructure one are bottlenecked by the same
thing — **data quantity** — and both should be re-run as the tick
archive and IPO count grow.  Sharpening the event with exact prospectus
lockup dates (a data-extraction task) is the one refinement most likely
to change the verdict.  *That refinement has since been done — see §9.*

---

## 9. Re-run with exact prospectus dates — corrected to a null after a contaminated event was caught

The HKEX document refill (see
[`HK_IPO_EVENT_DATA_CURATED.md`](HK_IPO_EVENT_DATA_CURATED.md)) replaced
the `listing + 6 mo` proxy with **exact cornerstone-lockup expiry dates
extracted from prospectuses** (`ipo_event_dates_curated`).  Re-run:

```bash
uv run python -m scripts.analysis.lockup_event_study --event-type cornerstone_lockup_expiry
```

### Correction notice (read this first)

The first exact-date run (14 events) appeared to show the
theory-predicted signature: AR(τ=0) = −4.17 % (the most negative day in
the window) and H1 CAR[−1,+3] = −4.36 % — "suggestive but
underpowered."  **That signature was an artifact.**  The §10 audit
found that one of the 14 events, `03378`, had its curated "expiry"
dated **2025-12-15 — eight days before the stock even listed**
(2025-12-23).  The script snaps an event to the first trading day at or
after the event date, so this impossible date landed on 03378's IPO
debut, which crashed (worst early day −46 %), single-handedly
manufacturing the "event-day selling pressure."  With the bad date
routed to review (curation-level sanity filter + script-level guard,
see §10), the clean result is:

### Clean result (13 events / 13 stocks)

| test | result | verdict |
|---|---|---|
| **H1** selling pressure at expiry | CAR[−1,+3] = **+0.04 %**, t = +0.01, N = 13; median **+3.40 %**, sign test 8/13 positive (p = 0.58) | ❌ no event-day pressure at all |
| **H2** overhang scaling | corr(CAR, cornerstone %) = **−0.16**, N = 10 | ❌ noise |
| **H3** pre-positioning | mean ofi(τ∈[−5,−1]) = **+0.025**, t = +0.57 | ❌ no front-running |
| **placebo** (CAR[−1,+3] at τ₀ − 40 d) | **−10.03 %**, t = −1.76, N = 13 | 🚨 far more negative than the real event |

AR(τ=0) is −0.94 % — ordinary for this drifting panel, not a spike.
The **one H1-consistent residual** is order flow: mean OFI is net-sell
on every day of τ = 0…+5 (−0.06 to −0.16) after a mixed-sign pre-event
profile.  But OFI is broadly net-sell across this post-IPO panel and
the study has no OFI placebo, so this is weak evidence, noted rather
than claimed.

**Conclusion: no evidence of a lockup-expiry abnormal-return effect on
exact dates either** — the verdict returns to §8's, now on correct
timestamps.  Not promotable, not tradable, not an anomaly.  The
decisive variable is unchanged: **more events**.  As of 2026-07-13, 15
further cornerstone expiries have already occurred after the panel's
last date (2026-06-26) and 22 more arrive by 2026-09-30 — refreshing
the tick/daily ingestion roughly doubles N immediately and brings it
to ~50 by early Q4.

---

## 10. Greenshoe expiry & stabilization end — clean nulls, and the audit that mattered

The same script and hypotheses were run on the two much larger curated
event types inside the tick window:

```bash
uv run python -m scripts.analysis.lockup_event_study --event-type greenshoe_expiry
uv run python -m scripts.analysis.lockup_event_study --event-type stabilization_end
```

### The raw first run was a trap

The unaudited output looked spectacular: mean AR(τ=0) = **+8.7 %**
(greenshoe, 59 events) and **+13.7 %** (stabilization end, 38 events),
H1 t ≈ +2.0.  Every part of that was an artifact:

1. **Impossible curated dates snapped onto IPO day-1 pops.**  Three
   stocks (`02706`, `01989`, `01609`) had greenshoe/stabilization dates
   curated to **one day before their listing**; `00068`'s stabilization
   end sat at listing + 3 days.  The event-alignment rule (first
   trading day ≥ event date) mapped all of them onto the first trading
   day(s) — day-1 moves of +244 %, +119 %, +101 %, +34 % — which
   contributed essentially the entire mean.  Median AR(τ=0) was
   ≈ 0 and only ~50 % of events were positive.
2. **The placebo was mechanically N = 0.**  These events sit ~30 days
   after listing, so the τ₀ − 40 d placebo always fell before listing
   and silently vanished — the study ran with no control at all.
3. **The two event types are largely the same day.**  27 of the 34
   stabilization-end dates are identical (stock, date) pairs to
   greenshoe-expiry dates (both are the day-30 boundary of the HK
   price-stabilizing rules) — not two independent samples.

### Fixes (now in the pipeline, regression-tested)

- **Curation-level sanity filter** (`scripts/sql/ipo_event_terms_curated.sql`):
  post-listing event types dated before listing, or day-30 types dated
  under listing + 20 d, are routed to `ipo_event_terms_needs_review`
  with reason `implausible_event_date` — **13 rows caught** across
  event types, including the `03378` date that contaminated §9.
- **Script-level guard** (`MIN_DAYS_FROM_LISTING`) drops any such event
  that reaches the analysis, loudly.
- **Robust statistics**: H1 now also reports the median CAR and a
  two-sided binomial sign test — the mean/t-test is not trustworthy
  under IPO day-1 fat tails.
- **Two-sided placebo**: τ₀ − 40 d *and* τ₀ + 40 d, so early-life
  events get a functioning control.

### Clean results

| | greenshoe_expiry (N = 56) | stabilization_end (N = 34) |
|---|---|---|
| AR(τ=0) mean / median / % positive | +2.10 % / +0.40 % / 52 % | +0.70 % / −0.54 % / 38 % |
| H1 CAR[−1,+3] mean | +3.41 % (t = +1.84) | +2.13 % (t = +1.23) |
| H1 median / sign test | +0.07 % / 29/56 pos (p = 0.89) | +0.07 % / 18/34 pos (p = 0.86) |
| placebo CAR (τ₀ + 40 d) | +2.07 % (t = +1.31, N = 45) | +1.15 % (t = +0.47, N = 25) |
| H3 pre-event OFI | −0.023 (t = −1.56) | −0.041 (t = −2.07) |

The residual positive means are pure right-tail skew (medians ≈ 0, sign
tests ≈ coin flip) and the placebo windows show the same magnitudes as
the "event" windows.  H2 uses cornerstone overhang, which is not the
relevant treatment size for these event types — reported for
completeness, not interpreted.  The nominally significant
stabilization-end H3 is the panel-wide post-IPO net-sell OFI drift, not
an event-specific signal (no OFI placebo exists to separate them).

**Conclusion: no evidence that greenshoe expiry or stabilization end
moves abnormal returns at the current data scale — a clean null on the
largest event samples we have.**  Theory would predict *negative*
pressure when stabilization support is withdrawn; neither the sign nor
the shape appears.  The economically sensible read: the day-30 boundary
is fully anticipated and priced, and (per the §8/§9 lesson) whatever
drama exists in young HK IPOs lives in the general post-listing drift,
not in scheduled calendar events.

The lasting value of this run is infrastructural: the curated event
contract now rejects impossible dates by construction, and the event
study is robust to the exact failure modes (fat tails, missing
placebo, contaminated timestamps) that produced three false leads in a
row before controls caught them.
