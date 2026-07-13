# HK IPO Event Data Curated

This note records the current HKEX/Bloomberg event-data contract for the HK IPO research track.

## Source Run

Latest refill run used during the 2026-07-02 review:

- `run_id=20260702_043826`
- Raw PDFs: `gs://hk-ipo-research-bloomberg-database-0629/hk_ipo/hkex_documents/raw/run_id=20260702_043826`
- Extracted text: `gs://hk-ipo-research-bloomberg-database-0629/hk_ipo/hkex_documents/text/run_id=20260702_043826`
- Curated/report: `gs://hk-ipo-research-bloomberg-database-0629/hk_ipo/hkex_refill/curated/run_id=20260702_043826`

## BigQuery Tables

- `hkex_document_registry_curated`: latest HKEX document registry.
- `ipo_event_terms_curated`: event-term candidates that passed evidence and sanity filters.
- `ipo_event_terms_needs_review`: missing, weak-source, or anomalous terms.
- `ipo_event_dates_curated`: distinct event dates with source evidence.
- `ipo_event_features_daily`: one row per `ipo_daily_prices` stock-day, with nullable event-distance features.

The reproducible SQL lives in:

- `scripts/sql/ipo_event_terms_curated.sql`
- `scripts/sql/ipo_event_features_daily.sql`

## Coverage Snapshot

From the 2026-07-13 re-curation (after adding the implausible-date sanity filter):

- HKEX PDF/text uploaded: 287 PDFs and 287 text files.
- Prospectus coverage: 75 stocks.
- Allotment-results coverage: 75 stocks.
- Refill staging rows: 846 document registry rows, 2,070 event terms, 693 Bloomberg recheck rows.
- Curated outputs: 846 document rows, 1,790 curated event terms, 267 needs-review terms + 13 implausible-date terms, 593 event dates.
- Daily event features: 7,118 rows, aligned to the `ipo_daily_prices` panel.

## Known Bloomberg Anomalies

Bloomberg lockup fields are hints, not truth:

- `06051`: lockup date earlier than listing date.
- `03636`: `EQY_IPO_LOCKUP_DT=2029-01-09`, more than three years after listing.

For lockup/greenshoe/stabilization research, prefer HKEX/prospectus-derived `ipo_event_dates_curated`. Use Bloomberg fields only as review candidates unless a source document confirms them.

## Known HKEX-Extraction Anomalies (now filtered by construction)

The HKEX-extracted dates had their own error class: **event dates before
listing** (impossible for post-listing events).  These are catastrophic
downstream because event studies snap an event to the first trading day at or
after the event date — a pre-listing date lands on the IPO's first trading
day and injects the day-1 pop/crash into τ=0.  This manufactured two false
event-study leads before being caught (see
[`DESIGN_LOCKUP_EVENT_STUDY.md`](DESIGN_LOCKUP_EVENT_STUDY.md) §9–§10).

The curation SQL now routes to `ipo_event_terms_needs_review` with reason
`implausible_event_date` any term where:

- a post-listing event type (`stabilization_*`, `greenshoe_*` except
  `greenshoe_granted`, `cornerstone_lockup_expiry`, `pre_ipo_investor_unlock`)
  is dated before `ipo_master.listing_date`; or
- a day-30 event type (`stabilization_end`, `greenshoe_expiry`) is dated
  under `listing_date + 20 days` (the HK price-stabilizing rules put these
  ~30 days after listing).

13 terms are currently caught, including `03378` (cornerstone expiry 8 days
pre-listing), `02706`/`01989`/`01609` (greenshoe/stabilization dates 1 day
pre-listing), and `00068` (stabilization end at listing + 3 days).

Note on `status`: the refill agent stages rows as `candidate`
(extracted, sanity-passed) or `ok`; curation accepts both and routes anything
else to review.  The analysis script has a matching defense-in-depth guard
(`MIN_DAYS_FROM_LISTING` in `scripts/analysis/lockup_event_study.py`),
regression-tested in `tests/unit/test_lockup_event_study.py`.

## Doctor Commands

```bash
make doctor-hk-ipo-events
make doctor-hk-ipo-data
```

## Event Study

```bash
python -m scripts.analysis.lockup_event_study --event-type cornerstone_lockup_expiry
python -m scripts.analysis.lockup_event_study --event-type greenshoe_expiry
python -m scripts.analysis.lockup_event_study --event-type stabilization_end
```

Results and verdicts (all nulls as of 2026-07-13) live in
[`DESIGN_LOCKUP_EVENT_STUDY.md`](DESIGN_LOCKUP_EVENT_STUDY.md) §8–§10.

## Event-Conditioned Harness Run

Offline smoke over BigQuery daily + microstructure + curated event features:

```bash
make validate-hk-ipo-events ARGS="--no-write --json"
```

Use `ARGS="--llm openrouter --n-candidates 12"` for a live proposer run after setting `OPENROUTER_API_KEY`.

## Autonomous Topic Selection

Use the research director to choose the next HK IPO research topic and data
follow-up queue before running validation:

```bash
make research-director-hk-ipo
make research-director-hk-ipo ARGS="--json"
```

See `docs/RESEARCH_DIRECTOR.md` for the director contract and current
limitations.
