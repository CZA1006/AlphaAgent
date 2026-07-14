# Project Status — AlphaAgent

> Single source of truth for "where the project actually is": what is
> built and proven, what is not, and what to do next.  Last refreshed
> after the three honest case studies (v1 positive / v2 + v3 negative)
> and the look-ahead audit (3 CRITICAL bugs fixed).

For *how* the system works, read [`../ARCHITECTURE.md`](../ARCHITECTURE.md)
("How the agent does quant research") and
[`ROUND7_TO_9_SUMMARY.md`](ROUND7_TO_9_SUMMARY.md).  For the empirical
runs, read the four `CASE_STUDY_*` docs and
[`AUDIT_LOOK_AHEAD.md`](AUDIT_LOOK_AHEAD.md).

---

## One-paragraph summary

AlphaAgent is a self-improving quant-research harness on the Hermes
runtime.  An LLM proposes factor *expressions* from a theme; a safe
DSL compiles them; a deterministic walk-forward + embargo + holdout
evaluator scores them; a six-gate judge promotes / refines / rejects;
every promotion is reproducible via a config-hash trail; survivors
combine into baskets that can themselves be promoted and refined; and
promoted work feeds the next cycle's prompt.  The architecture runs
end-to-end against real DeepSeek + Qwen LLMs and real Polygon SP-50
data.  **What is proven is that the harness measures alpha honestly;
what is *not* proven is that the loop reliably produces alpha** — on
two LLMs over a shared out-of-sample window the baskets did not hold up.

---

## What we have achieved ✅

### Architecture & engineering

- **Clean two-layer split**, statically enforced: no `hermes.*`
  imports in the harness, LLM confined to `proposer/`.  `make audit`
  fails the build on violation.
- **Safe factor DSL** — whitelisted fields + functions, typed AST, no
  `eval`/arbitrary code.  Deterministic, test-covered execution.
- **Deterministic evaluator stack** — IC, rank-IC, quantile spread,
  turnover, cost-adjusted spread, Sharpe; sector/beta neutralization;
  multi-horizon labels.
- **Walk-forward + embargo + purge + TAIL holdout** — calendar folds,
  embargo of `lag + horizon` days, out-of-sample reservation.
- **Six-gate promotion judge** — data sufficiency, profile thresholds,
  multi-horizon sign consistency, walk-forward stability, tail
  concentration, holdout decay.
- **Reproducibility chain** — `PromotionTrail` SHA-256 over every
  knob; trail-aware refinement guard; trail diff + registry.
- **Multi-factor combination + composites** — baskets are first-class
  `FactorSpec`s (`composite_recipe`), promotable, refinable, auditable,
  with order-invariant `recipe_id`s.
- **Durable proposer feedback loop** — in-process experiments and prior
  validation reports from the same point-in-time memory scope feed the next
  proposer cycle. The scope hashes the evaluation contract and input-panel
  contents; promoted composites also surface from their artifact index. A
  two-invocation CLI integration test verifies the cross-process path.
- **Operator surface** — `validate_strict`, `combine_factors`,
  `refine_factor`, `inspect_composite`, `list_{factors,cycles,trails}`,
  `doctor`; memory + SQL registry backends behind protocols.
- **Remaining autonomy gap** — Director topics still share the
  `validate_strict` executor. Cost replay, event-truth review, raw-tick
  materialization, event studies, and skill distillation are not yet wired
  into one typed task loop.
- **Persistence-first selection machinery**
  (`alpha_harness/evaluators/persistence.py`): factors can be ordered
  by sub-window rank-IC sign consistency + stability instead of
  train-window mean IC — the fix for the case-study failure where the
  strict gates promoted the hottest-train-IC factor (which flipped
  OOS).  Backtested on the 12-factor HK IPO answer key: it excludes
  the trap factor by construction and doubles the basket's OOS
  rank-IC (+0.0156 → +0.0329), but on one 40-day window neither
  ordering predicts per-factor OOS rank (Spearman ≈ −0.5 for both) —
  so it stays **opt-in** (`combine_factors --selection-strategy
  persistence --top-k K`) until a longer OOS window arbitrates. The
  selection policy and candidate counts are persisted in combination
  reports and promotion trails. The original 2026-05-01 to 2026-06-26
  OOS window is now a consumed selector-development answer key; only
  later data qualifies as a fresh validation window. The
  companion tail-concentration audit refuted the "IPO debut spike"
  hypothesis (0/12 gate flips) and instead showed the gate criterion
  itself mispredicting OOS on this panel (4/5 gate-firing factors
  were OOS-positive).
- **~50 test files**, ruff-clean, with regression tests for each fixed
  audit finding.

### Scientific integrity

- **Self-auditing infrastructure caught its own bugs.** Because the
  combiner and the validator independently measure the same factor,
  their *disagreement* exposed 3 CRITICAL look-ahead / measurement
  bugs (combiner bypassed `HoldoutPolicy`; `FactorThumbnail` dropped
  the holdout block; `SignalQualityEvaluator` inflated IC via per-fold
  signal recomputation).  All fixed, all regression-tested.
- **Honest train/test methodology** — disjoint Y1 (selection) and Y2
  (validation) windows; no metric reported without an out-of-sample
  read.

### Empirical (real LLM + real data)

- Full loop exercised against **DeepSeek-Chat-v3.1** and
  **Qwen-2.5-72B** via OpenRouter on **Polygon SP-50** daily bars.
- One out-of-sample-positive basket (case study v1, post-fix); two
  out-of-sample-negative baskets (v2, v3) — see the verdict table
  below.
- **HK IPO tick microstructure** (real Bloomberg data in GCP BigQuery,
  77 IPOs + 86.1 M-row target tick lake): the **first signal to survive
  the full gauntlet** — disjoint OOS (10/12 factors persist, p ≈ 1.9 %)
  **and** realistic 78 bps cost **and** long-only-implementable (4/12
  positive net, incl. flagship `rank(ofi) - rank(rel_spread)`).  Modest
  magnitude, ~40-day test window — promising, not yet confirmed; bottleneck is data
  quantity.  See [`CASE_STUDY_HK_IPO_MICRO.md`](CASE_STUDY_HK_IPO_MICRO.md).
- **HK IPO event data enrichment**: HKEX prospectus/allotment documents
  are now curated into exact event dates and daily event features
  (`ipo_event_dates_curated`, `ipo_event_features_daily`) for greenshoe,
  stabilization, and cornerstone lockup research.  Known Bloomberg
  lockup anomalies are kept out of truth tables and routed to review.
- **HK IPO event studies — three event types, all honest nulls, and a
  data bug caught**
  (see [`DESIGN_LOCKUP_EVENT_STUDY.md`](DESIGN_LOCKUP_EVENT_STUDY.md)):
  - *Cornerstone lockup expiry*: clean negative on `listing + 6 mo`
    proxy dates (19 events, §8).  The exact-prospectus-date re-run
    initially looked "suggestive" (AR(τ=0) = −4.17 %), but an audit
    traced that entire signature to **one curated date that predated
    the stock's listing** (03378) snapping onto its IPO-debut crash.
    Corrected (N = 13): H1 CAR[−1,+3] = +0.04 % (median +3.4 %, 8/13
    positive) — **no expiry-day effect**; a persistent post-event OFI
    net-sell run is the only residual, noted not claimed (§9).
  - *Greenshoe expiry (56) and stabilization end (34)*: **clean
    nulls** — the raw means (+8.7 %/+13.7 % at τ=0!) were entirely
    manufactured by 4 more impossible curated dates landing on day-1
    pops; medians ≈ 0, sign tests ≈ coin flip, placebo windows match
    "event" windows.  27 of 34 stabilization ends are the same
    (stock, date) as greenshoe expiries — one day-30 boundary, not two
    samples (§10).
  - *Infrastructure hardened*: the curation SQL now routes
    **13 implausible event dates** (pre-listing, or day-30 types under
    listing + 20 d) to `needs_review` by construction; the event-study
    script gained a matching guard, median/sign-test statistics
    (IPO fat tails break the t-test), and a two-sided placebo —
    all regression-tested.  Sample sizes grow with ingestion: 15
    further lockup expiries already occurred after the panel's last
    date (2026-06-26), 22 more by 2026-09-30.

---

## What is limited / not yet proven ⚠️

### The headline limitation

**A single positive out-of-sample result does not replicate.**

| study | LLM | Y1 → Y2 | Y1 IC / ric | Y2 IC / ric | Y2 verdict |
|---|---|---|---|---|---|
| v1 | DeepSeek | 2024-04→2025-04 / 2025-04→2026-04 | +0.033 / +0.049 | +0.058 / +0.053 | ✅ |
| v2 | DeepSeek | 2024-06→2025-05 / 2025-05→2026-04 | +0.035 / +0.044 | −0.023 / −0.014 | ❌ |
| v3 | Qwen | 2024-06→2025-05 / 2025-05→2026-04 | +0.025 / +0.036 | −0.036 / −0.043 | ❌ |

Two LLMs on the same Y2 window both sign-flip out-of-sample.  The
failure is **window-specific, not LLM-specific**.  The v1 positive is
real-but-fragile.

### Known evaluator gaps (from the audit, still open)

- **Finding 3 (medium)** — TAIL holdout has no embargo gap; in-sample
  labels overlap ~`lag+horizon` days into the holdout window.
- **Finding 4 (medium)** — beta neutralization is estimated in-sample
  over the full window (documented as a deliberate first cut).
- **Finding 6 (medium)** — no multiple-hypothesis correction; with
  18+ proposals per study, some clear by chance.

### Structural limitations

- **Survivorship-biased universe** — SP-50 is 50 surviving large-caps
  (Finding 5); absolute IC is inflated by an unmeasured amount.
- **Polygon free-tier** caps history at trailing ~2 years and is
  region-blocked for Anthropic/Google/OpenAI models (we run on
  DeepSeek/Qwen).
- **Flat 5 bps cost model** — real execution cost is trade-size and
  liquidity dependent.
- **No live execution, no intraday, no broader asset classes.**
- **LLM doesn't yet *compose* with promoted composites** — they appear
  in its prompt (loop closure works) but it proposes fresh singletons;
  making it actively build on baskets is unstarted prompt-engineering.

---

## What we should do next 🎯

The architecture is done enough to *answer* the real question, which a
single run cannot: **does this loop produce alpha on average, or only
by chance?**  Ranked by leverage:

### 0. Data scaling — the actual prerequisite (decided)

The fragility in v2/v3 is most likely **starved-for-data**: 50
survivorship-biased names × ~2y of daily bars gives the LLM almost no
decorrelation budget and the judge almost no statistical power.  The
scaling path is designed in
[`DATA_INFRA_PLAN.md`](DATA_INFRA_PLAN.md): Bloomberg tick ingestion →
cloud lake + access API → RAG → multi-market (HK …), built against the
existing `loader_factory` / `DataRequest` seams.  Buildable now: tick
schema, exporter interface + mock, cloud loader.  Blocked on a
Bloomberg Terminal + cloud account: the real data itself.

**Decision:** data scaling precedes more autonomy, and the future
autonomous research-director loop (Round 10) is **robustness-first** —
every self-generated candidate basket auto-confirmed on a held-out
window before it counts as alpha.

### 1. Planned multi-run robustness study (highest leverage once data lands)

Turn the ad-hoc case studies into a designed experiment:

- **Rolling windows** — many disjoint Y1/Y2 splits (e.g. quarterly
  re-selection), not one annual split.
- **≥3 LLMs** — DeepSeek, Qwen, + one more (Mistral/Llama) to
  confirm the window-not-LLM diagnosis at scale.
- **≥2 universes** — SP-50 plus a broader, less survivorship-biased
  set (NDX-100 / Russell midcap) once backfilled.
- **The number that matters:** fraction of basket runs that clear
  strict on Y2, compared to a no-edge null.  *That* settles whether
  the loop produces alpha.

This is mostly orchestration (a harness around `validate_strict` +
`combine_factors` that sweeps windows/LLMs/universes and tallies), not
new core code.

### 2. Close the remaining audit findings (medium)

- Add the holdout embargo gap (Finding 3, ~15 lines).
- Record `n_proposals_in_session` in reports and let the judge tighten
  thresholds under multiple-hypothesis pressure (Finding 6).
- Optional: rolling/out-of-sample beta (Finding 4).

### 3. Round 10 — proposer prompt-engineering for composites

Teach the proposer to propose *complements* to promoted composites
(low-correlation additions), then measure whether composed baskets
generalize better than singleton baskets.  Depends on having several
promotions to chain, so it's downstream of (1).

### 4. Broaden data realism (lower priority)

Point-in-time universe loader (kill survivorship bias), liquidity-aware
cost model, longer history via a paid data tier.

---

## How to read the verdict

This is a **research-tool success and a trading-strategy non-result**,
and both halves are true at once:

- As an honest measurement instrument, the harness works: it found its
  own bugs, it reports out-of-sample truth, and it refuses to promote
  what doesn't survive.
- As an alpha generator, the evidence is one positive out of three
  honest attempts — not enough to claim anything.  The next milestone
  is the study that would actually settle it.

Claiming more than this would contradict our own data.  The project's
credibility comes from *not* overclaiming.
