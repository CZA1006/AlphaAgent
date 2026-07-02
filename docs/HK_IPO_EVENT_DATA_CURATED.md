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

From the latest review:

- HKEX PDF/text uploaded: 287 PDFs and 287 text files.
- Prospectus coverage: 75 stocks.
- Allotment-results coverage: 75 stocks.
- Refill staging rows: 846 document registry rows, 2,070 event terms, 693 Bloomberg recheck rows.
- Curated outputs: 846 document rows, 1,782 curated event terms, 267 needs-review terms, 606 event dates.
- Daily event features: 7,118 rows, aligned to the `ipo_daily_prices` panel.

## Known Bloomberg Anomalies

Bloomberg lockup fields are hints, not truth:

- `06051`: lockup date earlier than listing date.
- `03636`: `EQY_IPO_LOCKUP_DT=2029-01-09`, more than three years after listing.

For lockup/greenshoe/stabilization research, prefer HKEX/prospectus-derived `ipo_event_dates_curated`. Use Bloomberg fields only as review candidates unless a source document confirms them.

## Doctor Commands

```bash
make doctor-hk-ipo-events
make doctor-hk-ipo-data
```

## Event Study

```bash
python -m scripts.analysis.lockup_event_study --event-type cornerstone_lockup_expiry
python -m scripts.analysis.lockup_event_study --event-type greenshoe_expiry
```

## Event-Conditioned Harness Run

Offline smoke over BigQuery daily + microstructure + curated event features:

```bash
make validate-hk-ipo-events ARGS="--no-write --json"
```

Use `ARGS="--llm openrouter --n-candidates 12"` for a live proposer run after setting `OPENROUTER_API_KEY`.
