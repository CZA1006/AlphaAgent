# Pre-registration — free-float-conditioned cornerstone lockup effect (2026 Q3, v1)

> Registered before any test statistic is computed; the git commit
> introducing this file is the registration timestamp.  Results are
> appended in marked sections and never edited retroactively.

## Motivation (literature prior, not data-derived)

The aggregate cornerstone-lockup-expiry effect is a **null** on this
dataset (see [`DESIGN_LOCKUP_EVENT_STUDY.md`](DESIGN_LOCKUP_EVENT_STUDY.md)
§9, §11: N = 27, H1 CAR[−1,+3] not significant, OFI residual did not
replicate).  This pre-registration tests a **new conditioning variable
never examined before**: **free float**.

External priors motivating the direction (fixed before looking at any
free-float split):

- HK market commentary (Reuters, 2026): recent listings' *small free
  float* makes them "prone to stock price manipulation" with "strong
  selling pressure when the lockup period expires."
- Microstructure intuition: a fixed unlock supply is a larger fraction
  of tradable shares — and harder for the market to absorb — when the
  free float is small.

**Registered hypothesis H:** the cornerstone-lockup-expiry selling
signature (negative abnormal return around τ = 0 and net-sell OFI
after τ = 0) is **concentrated in low-free-float stocks and absent or
weaker in high-free-float stocks.**  Formally, the low-float bucket's
event-window CAR is more negative than the high-float bucket's
(a negative low−high interaction).

## Dataset and split rule (fixed now)

- Universe / panel: `configs/universes/hk_ipo.txt` @ 128 names,
  completed capture, event truth rebuilt 2026-07-20.
- Free float: `ipo_daily_prices.free_float_pct` (126/128 available;
  cross-sectional quartiles 45.9 / 65.0 / 100.0).  A stock's free
  float is taken as its value on/near the event date.
- **Bucketing rule (pre-declared):** split events at the **median
  free_float_pct of the event set** into `low_float` (≤ median) and
  `high_float` (> median).  Median split fixed in advance — no
  threshold tuning.
- Event: `cornerstone_lockup_expiry`, exact curated dates, the
  implausible-date filter already applied.

## Contamination ledger

The **aggregate** result on the 28 in-window events (≤ 2026-07-17) is
already known (null).  Splitting a known-null sample by a new variable
is a garden-of-forking-paths risk: any low-float effect found in the
exploratory read below is **hypothesis-generating only** and cannot be
claimed.  The clean test is future events.

## Exploratory read (≤ 2026-07-17, contaminated — cannot promote)

On the 28 in-window events, report per bucket: CAR[−1,+3] (mean, t,
median, sign test), event-window mean OFI (τ = 0…+3), and the low−high
CAR interaction with its t.  Descriptive only; direction noted, never
claimed.

## Registered confirmation (future events only — the real test)

Cornerstone expiries occurring **after 2026-07-17** are the clean
sample (22 expected by 2026-09-30, ~56 by year-end).  When ≥ 20 future
events have both an expiry date and tick coverage (expected
~2026-10, after the next data ingestion), run the identical
median-split analysis **once** on the future events alone.

**Registered decision rule for H (one shot, no re-runs):**
1. low_float bucket CAR[−1,+3] < 0 with t ≤ −1.7, **and**
2. low−high interaction < 0 with t ≤ −1.7, **and**
3. high_float bucket CAR[−1,+3] not significantly negative
   (t > −1.7).
All three required to call H supported; otherwise H is recorded as
rejected/null.  A placebo (τ₀ ± 40 d) is reported alongside.

## Multiplicity and budget

One primary interaction test on the future window; the exploratory
read is descriptive and reported in full regardless of sign.  No LLM
cost (deterministic BigQuery analysis under existing loader caps); no
writes to non-artifact tables; no re-runs of the confirmation test
against an observed window.
