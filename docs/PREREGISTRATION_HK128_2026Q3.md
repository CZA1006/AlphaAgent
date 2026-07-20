# Pre-registration — HK IPO research on the completed 128-stock dataset (2026 Q3, v1)

> Registered before execution; the git commit introducing this file is
> the registration timestamp.  Everything below is fixed before any
> test statistic is computed.  Results will be appended in clearly
> marked sections and never edited retroactively.

## Dataset under test

- Universe: `configs/universes/hk_ipo.txt` @ 128 names (regenerated
  2026-07-20 from `ipo_master`).
- Panel: BigQuery `hk_ipo_research`, daily 2025-12-03 → 2026-07-17,
  tick-derived features from the **completed** quote capture
  (111.8 M-row lake, zero known volume>0/no-tick gaps), event truth
  rebuilt 2026-07-20 (933 curated dates, 125/128 stocks, implausible
  dates filtered to review).
- Exact panel fingerprints are recorded by the run artifacts.

## Contamination ledger (what has already been seen)

The 77-stock sub-panel has been analyzed extensively through
2026-07-17 (including the 2026-06-27 → 07-17 window).  The **51 stocks
added in the July ingestion have never been analyzed**, and **no data
after 2026-07-17 exists yet**.  Therefore:

- All data ≤ 2026-07-17 is treated as **selection/training material
  only** — no primary claim can rest on it.
- The only clean confirmation material is **future data**.

## Primary track — discovery now, confirmation on future data only

**P1.** Run the bounded autonomous discovery loop on the full
128-stock panel (all data ≤ 2026-07-17 as its evaluation window):

```
make autonomous-researcher-hk-ipo-run \
  ARGS="--llm openrouter --iterations 2 --cost-budget-usd 1"
```

(DeepSeek-chat-v3.1; schema-v6 Bonferroni family pressure applies
automatically; LLM budget ≤ $1.)

**Registered confirmation protocol for anything promoted:** the exact
promoted expressions and trails are frozen in the run artifacts.  They
are confirmed or killed **only** on the first **30 HK trading days
after 2026-07-17** (expected to complete ~2026-08-28), via one
cost-replay evaluation at 15 bps on that window alone.  Confirmation
requires, on the confirmation window: positive rank-IC **and** positive
net quantile spread at 15 bps.  One shot; no re-runs against the
observed confirmation window; failures are recorded as kills.

If discovery promotes nothing, that is the primary result and the
confirmation phase is void.

## Secondary S1 — listing-age regime hypothesis (retained from the OFI attribution)

Registered hypothesis (formed post-hoc on the *old incomplete* capture,
explicitly retained for a future test): smoothed-OFI rank-IC is
**positive for listing age 31–90 days and negative for 91+ days**.

- Factors: `rank(ts_mean(ofi, 10))` and `rank(ts_mean(ofi, 20))`.
- Test: per-bucket (31–90 d vs 91+ d by `days_since_listing`) mean
  daily cross-sectional rank-IC over the full completed panel
  (2025-12-12 → 2026-07-17, 128 stocks), with day-level t-statistics.
- Support requires the (+, −) sign pattern for **both** smoothings and
  |t| ≥ 2 in at least one bucket per smoothing.
- Honesty label: the 77-stock portion of this panel is contaminated
  (hypothesis-formation material, though on differently-captured
  data); the clean re-read happens on the confirmation window above.
  S1 is secondary and cannot promote anything by itself.

## Secondary S2 — factor-family read on the 51 never-analyzed stocks

The 12-factor microstructure family
(`scripts/analysis/hk_ipo_micro_factors.txt`) evaluated **only on the
51 stocks absent from the pre-July universe**, over their full
histories: per-factor rank-IC and long-only HSI-hedged net at the
measured spread.  Statistic: two-sided sign test across the 12 factor
rank-ICs against 0.5.  Descriptive secondary — the family was declared
dead on the confirmation runs; this read can flag "capture completeness
changes the picture on fresh names" but cannot revive the family by
itself.

## Multiplicity accounting

- P1: handled by schema-v6 predeclared family pressure inside the loop.
- S1: 4 statistics (2 smoothings × 2 buckets), reported jointly.
- S2: 12 statistics summarized by one sign test.
- Primary claims can only originate from P1's future-window
  confirmation.  S1/S2 are reported in full regardless of outcome.

## Budget and stopping

LLM ≤ $1 total; BigQuery reads under existing loader caps; no writes
to non-artifact tables; no re-runs of any test against an observed
window.  Results appended below after execution; the confirmation
section is appended ~2026-08-28.

---

# RESULTS (appended 2026-07-20, same day as registration)

## P1 — discovery: zero promotions out of 108 candidates; confirmation phase void

Two runs, both with schema-v6 family pressure and the strict gate stack:

1. `autonomous-hk_ipo-20260720T102938Z-33285e6d` — the registered make
   command.  **Protocol deviation (recorded):** the hk_ipo pack's topic
   definition pins `--end-date 2026-06-26`, so this run evaluated the
   128-stock panel only through 06-26.  Result: 2 iterations × 3
   cycles × 12 = **72 candidates, 0 promoted**; the runner stopped
   itself on the no-promote policy.
2. `prereg-hk128-p1-full-window` — supplementary run at the registered
   window (2025-12-12 → **2026-07-17**), panel fingerprint
   `a32719bf…30ff21e`: 3 cycles × 12 = **36 candidates, 0 promoted**.

**Primary result: the loop promotes nothing on the completed 128-stock
dataset.  Per the registered protocol, the future-window confirmation
phase is void.**  Follow-up (config debt, not research): topic
`validation_args` should derive the end date from the data snapshot
rather than pinning it.

## S1 — listing-age regime: NOT SUPPORTED (opposite sign)

Completed panel, 128 stocks, registered decision rule:

| factor | 31–90 d | 91+ d |
|---|---|---|
| `rank(ts_mean(ofi,10))` | **−0.0406** (t = −1.98, 119 d) | −0.0079 (t = −0.39, 79 d) |
| `rank(ts_mean(ofi,20))` | **−0.0212** (t = −1.02, 119 d) | −0.0193 (t = −0.97, 79 d) |

The registered (+, −) pattern does not appear anywhere; the 31–90 d
bucket is *negative* under both smoothings — nearly significantly so
in the wrong direction for the 10-day version.  The post-hoc age
pattern observed on the old incomplete capture was a phantom.
**S1 rejected and closed.**

## S2 — factor family on the 51 never-analyzed stocks: family stays dead

Full-history read, measured half-spread 45.5 bps: **10/12 factors have
negative rank-IC** (two-sided sign test p = 0.039), flagship
`rank(ofi) − rank(rel_spread)` = −0.111; long-only hedged net negative
for 10/12.  The completed capture on fresh names does not rescue the
family.  (The systematic negative lean is an unregistered observation:
recorded, not claimed.)

## Ledger

All three registered tests executed once, no re-runs against observed
windows.  LLM cost within the $1 budget (provider-reported figures in
the run artifacts).  **Net scientific position after this
registration: the completed HK dataset yields no promotable signal
from this factor vocabulary and closes both retained secondary
hypotheses.**  Any further HK work needs a genuinely new hypothesis
class (new features, new mechanism), not another pass of this
vocabulary.
