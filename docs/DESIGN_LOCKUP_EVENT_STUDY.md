# Design — HK IPO 6-month lockup-expiry event study (tick)

> Minimal-viable design for the highest-theory-value untested direction:
> using tick order flow around the **6-month lockup expiry** — HK IPO's
> most documented anomaly.  The MVP (`scripts/analysis/lockup_event_study.py`)
> is now built and run twice: on **proxy dates** (`listing + 6 mo`) the
> result was **a clean negative** (§8 — the placebo caught a false
> positive, which is the whole point of running it); on **exact
> prospectus-extracted dates** (`ipo_event_dates_curated`) the
> theory-predicted signature *appears* but is statistically
> indistinguishable from the post-IPO background drift —
> **suggestive but underpowered**, N = 14 (§9).

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

## 9. Re-run with exact prospectus dates — suggestive but underpowered

The HKEX document refill (see
[`HK_IPO_EVENT_DATA_CURATED.md`](HK_IPO_EVENT_DATA_CURATED.md)) replaced
the `listing + 6 mo` proxy with **exact cornerstone-lockup expiry dates
extracted from prospectuses** (`ipo_event_dates_curated`).  Re-run:

```bash
uv run python -m scripts.analysis.lockup_event_study --event-type cornerstone_lockup_expiry
```

**Sample: 14 events / 13 stocks** with the exact expiry inside the tick
window (65 curated cornerstone-lockup dates exist in total; most fall
outside 2025-12 → 2026-06).  Smaller than the proxy study's 19 — the
exact dates both move and filter events.

### What changed: the signature shape appears for the first time

With proxy dates (§8), τ = 0 was a non-event — AR flat, no order-flow
response.  With exact dates, the **theory-predicted "unlock supply"
signature emerges at exactly τ = 0**:

- **AR(τ=0) = −4.17 %** — the single most negative day in the
  [−10, +10] window (proxy version: ~flat at the event).
- **OFI turns persistently net-sell *after* the event**: mean OFI over
  τ = 0…+5 sits at −0.055 to −0.121 every single day, after a mixed-sign
  pre-event profile.  This is the "unlocked holders selling into the
  market" pattern H1 predicts, appearing only once the event dates are
  correct.

### Hypothesis tests (honest: none reach significance)

| test | result | verdict |
|---|---|---|
| **H1** selling pressure at expiry | CAR[−1,+3] = **−4.355 %**, t = −0.81, N = 14 | ❌ right sign, *not significant* |
| **H2** overhang scaling | corr(CAR, cornerstone %) = **+0.07**, N = 11 | ❌ no scaling (wrong sign, ~zero) |
| **H3** pre-positioning | mean ofi(τ∈[−5,−1]) = **+0.025**, t = +0.57 | ❌ no front-running (slightly net-buy) |
| **placebo** (CAR[−1,+3] at τ₀ − 40 d) | **−10.03 %**, t = −1.76, N = 13 | 🚨 still *more* negative than the real event |

### Honest read: an upgrade from "clean negative" to "suggestive but underpowered"

- **For the effect:** the event-day AR spike and the post-event
  net-sell OFI run are exactly the shape H1 predicts, they appear at
  exactly τ = 0, and they appear *only* when the proxy dates are
  replaced with exact ones.  That is what a real-but-small effect
  looks like when the event timestamp gets fixed.
- **Against calling it real:** N = 14 gives H1 a t of −0.81 — nowhere
  near significance — and the **placebo window is still more negative
  than the event window**.  The pervasive post-IPO downward drift
  (CAR ≈ −8.7 % by τ = 0 even before the event contributes) remains the
  dominant feature of this panel, and 14 events cannot separate a
  −4 % event effect from that background.  H2 (overhang scaling) and
  H3 (pre-positioning), which would corroborate a supply-pressure
  mechanism, both fail.

**Conclusion:** the exact dates changed the verdict from "no
expiry-specific effect" (§8) to "**the expiry-specific signature is
now visible but cannot be distinguished from background drift at
N = 14**."  Not promotable, not tradable, not claimed as an anomaly.
The decisive variable is unchanged from §8 and from the microstructure
study: **more events** — the sample grows automatically as the tick
archive and IPO count accumulate.

**Nearer-term follow-up:** the same curated tables hold much larger
event samples inside the tick window — **greenshoe_expiry (59 events)**
and **stabilization_end (38 events)** vs cornerstone lockup's 14.
Running the identical script/placebo methodology on those event types
is the cheapest next test of whether *any* scheduled IPO event moves
order flow at current data scale.
