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

---

# EXPLORATORY RESULTS (appended 2026-07-21; contaminated — cannot promote)

Ran `scripts/analysis/lockup_freefloat_split.py` on the 27 in-window
events with free-float data (median split at free_float_pct = 51.7%;
low_float N = 14, high_float N = 13):

| bucket | CAR[−1,+3] | median | sign | post-event OFI (τ 0..+3) |
|---|---|---|---|---|
| low_float | −0.68 % (t = −0.22) | +2.63 % | 8/14 pos | −0.007 (t = −0.20) |
| high_float | −2.92 % (t = −0.70) | −1.06 % | 5/13 pos | **+0.220 (t = +2.46)** |

**low−high interaction = +2.24 % (t = +0.43).**

**The exploratory read runs *against* hypothesis H, not for it:**

- The registered direction was a *negative* low−high interaction (low
  float more negative).  Observed interaction is **positive** — the
  low-float bucket is the *less* negative one, with a positive median.
- Nothing is significant (both buckets |t| < 0.7; interaction
  t = +0.43).
- The only significant number is high_float post-event OFI = +0.22
  (net *buying*, τ = 0..+3) — the opposite of a selling signature, and
  a lone significant cell out of many is expected under the
  forking-paths caveat.

**Read:** free-float conditioning does not reveal the hypothesized
concentration of lockup selling pressure; if anything the sign is
reversed.  The prior for H on the future confirmation window is now
weak.  The registered decision rule is left in place and will still be
evaluated once, honestly, on post-2026-07-17 events — but the
exploratory evidence suggests H will not survive.  Recorded as a
near-dead lead pending the one-shot future test.
