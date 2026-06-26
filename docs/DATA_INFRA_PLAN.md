# Data Infrastructure Scaling Plan

> Forward-looking plan for moving AlphaAgent from "50 US large-caps of
> daily Polygon bars on a laptop" to "a large multi-market tick + bar
> lake sourced from Bloomberg, stored in the cloud, queried remotely,
> and enriched with RAG."  Complements [`../DATA_PLAN.md`](../DATA_PLAN.md)
> (which covers the *current* local data layer); this doc is the
> *scaling* roadmap.
>
> Status: **planning only.**  None of Phase F is built — most of it is
> blocked on hardware/accounts we don't have yet (a Bloomberg Terminal,
> a cloud bucket).  The point of this doc is to design the data path
> *before* we automate research on top of it, so the autonomous loop
> (Round 10) is built against a data interface that won't have to change.

---

## Why this matters now

Two of the three honest case studies failed out-of-sample
(`CASE_STUDY_HONEST_V2/V3.md`).  A leading suspect is **too little
data and too narrow a universe**: 50 survivorship-biased large-caps ×
~2 years of daily bars gives the LLM almost no decorrelation budget and
the judge almost no statistical power.  More data — more names, longer
history, finer granularity (tick), more markets — is the single most
likely lever to turn fragile in-sample baskets into robust ones.

So data scaling is not a "later, nice-to-have" — it is **the
prerequisite for the autonomous alpha loop to find anything real.**

---

## The architecture already has the seams

We do *not* need to redesign the harness to plug in new data.  The
existing abstractions isolate every consumer from the data source:

| Seam | File | What it isolates |
|---|---|---|
| `create_equities_loader(source=...)` | `data/loader_factory.py` | source selection — adding `"bloomberg"` / `"cloud"` is a new branch, no business-logic change |
| `DataRequest` / `DataResult` | `data/models.py` | typed request/response contract every loader speaks |
| `Bar` / `EquityBar` / `CryptoBar` | `data/models.py` | canonical bar shape; `source` field carries provenance |
| `BarFrequency` | `data/models.py` | frequency enum (currently DAILY→MINUTE_1; **tick needs a new member**) |
| `AssetClass` | `schemas/hypothesis.py` | currently `US_EQUITY` / `CRYPTO`; **HK etc. are new members** |
| `FundamentalRecord` | `data/models.py` | already PIT-correct (`published_at` vs `period_end`) |
| `retrieval/` | `retrieval/related_experiments.py` | the slot RAG extends |

Everything downstream — DSL, evaluators, judge, regimes, the
autonomous loop — speaks `DataRequest`/DataFrame and never knows where
the bytes came from.  That is the property that makes this plan
incremental rather than a rewrite.

---

## Phase F1 — Bloomberg ingestion bridge

**Problem.** We have no Bloomberg API entitlement that works from an
arbitrary machine.  Bloomberg data is only accessible from a logged-in
**Bloomberg Terminal** (via `blpapi` / the `xbbg` wrapper), and that
machine is license-locked and typically firewalled.

**Design: push, not pull.**  Don't try to expose an API *on* the
terminal machine.  Instead run a one-way **export job on the terminal
machine** that pulls from Bloomberg and writes to our lake:

```
[Bloomberg Terminal machine]
   blpapi / xbbg
        │  pull historical + tick
        ▼
   scripts/bloomberg_export.py   (runs ON the terminal box)
        │  normalize → our Bar / tick schema → parquet
        ▼
   upload to cloud object store  (Phase F2)
```

- **Scope per run:** a universe file + date range + fields, mirroring
  `DataRequest`, so the export speaks the same contract.
- **Output format:** partitioned parquet (`symbol=/date=`) for bars;
  for tick, partitioned parquet with one file per `(symbol, date)` —
  tick is far too large for the Pydantic `list[Bar]` path (millions of
  rows/symbol/day), so tick gets a **columnar-only** route that never
  materializes per-row models.
- **Idempotent + resumable** like `backfill_parquet.py` already is —
  skip `(symbol, date)` partitions already present.
- **Provenance:** `source="bloomberg"`, capture the snapshot date so
  re-pulls are auditable.

**New code (when a terminal is available):**
`scripts/bloomberg_export.py` + a `BloombergExporter` that wraps
`xbbg`.  We can write the *interface* and a mock now; the real
`blpapi` calls need the terminal to test.

**Tick-data schema gap to close first:**
- Add `BarFrequency.TICK` (or a separate `TickRecord` model:
  `symbol, timestamp, price, size, exchange, conditions`).
- Add a tick-aware loader path that returns an Arrow/parquet handle,
  not a `list[Bar]`.
- Decide bar-from-tick aggregation lives in the harness (so factors
  can request "5m bars built from tick") — a resampling step in the
  data layer, PIT-safe.

---

## Phase F2 — Cloud data lake + access API

**Goal.** One machine (the terminal box, or a backfill server) writes;
many machines (research runs, the autonomous loop, notebooks) read —
without copying the whole lake around.

**Storage:** object store (S3 / GCS / R2) holding the partitioned
parquet/tick lake, plus a small **catalog/manifest** (what symbols ×
dates × frequencies × markets exist, with checksums + snapshot dates).

**Two access patterns, pick per use:**
1. **Direct object reads** — the loader reads parquet straight from the
   bucket via `s3fs`/`gcsfs` + `pyarrow`. Simplest; good for batch
   research. Add `create_equities_loader(source="cloud", base_path="s3://…")`.
2. **Thin read API** — a small FastAPI service in front of the lake
   (`GET /bars?symbols=…&start=…&end=…&freq=…` → parquet/Arrow
   stream). Better for many small queries, access control, and
   rate-limiting; the loader becomes an HTTP client
   (`source="api"`). The API speaks `DataRequest` in, `DataResult` +
   Arrow out — same contract.

**Recommendation:** start with **direct object reads** (zero service to
operate), add the **read API** only when multiple users / access
control / query metering actually matter.

**New code:** `data/cloud_loader.py` (object-store `EquitiesLoader`),
optionally `services/data_api/` (FastAPI). Both register in
`loader_factory`. No consumer changes.

**Cost/perf notes:** tick lake is large — partition tightly
(`market/symbol/date`), store column-pruned parquet, and cache hot
partitions locally (the existing `data/silver/` dir becomes a local
cache tier in front of the bucket).

---

## Phase F3 — RAG for research power

**Goal.** Give the proposer and the (Round 10) research director access
to *knowledge*, not just past-experiment metrics: factor-zoo
literature, the team's own research notes, prior promoted-factor
rationales, market-regime commentary.

**Build on `retrieval/`, which already exists** for related-experiment
retrieval. RAG extends it:

- **Index:** embed a corpus (papers, internal notes, past
  `ExperimentRecord` rationales + promotion trails) into a vector store
  (start local: `chroma`/`faiss`; cloud later).
- **Retrieve:** on each director/proposer call, pull top-K relevant
  chunks for the current theme/market and inject them into the prompt
  alongside the existing memory digest.
- **Boundary stays intact:** RAG lives on the **proposal side** only
  (`proposer/` + `retrieval/`). No evaluator/judge ever sees retrieved
  text — determinism preserved.

**Sequencing:** RAG is most valuable *after* the autonomous loop exists
(it makes the director's theme generation smarter) and *after* there's
a real data lake (more markets → more for the director to reason
about). So: F1 → F2 → autonomous loop → F3.

---

## Phase F4 — Multi-market (HK and beyond)

Once Bloomberg is the source, new markets are mostly **data + config**,
not new engine code:

- Add `AssetClass.HK_EQUITY` (and others) to `schemas/hypothesis.py`.
- Add universe files (`configs/universes/hk_*.txt`) + sector maps.
- Bloomberg export handles the symbology (e.g. `700 HK Equity`).
- **Market-specific care:** trading calendars (HK holidays ≠ US),
  currency, lot sizes, and a **point-in-time, survivorship-free
  universe** (the current SP-50 is survivorship-biased — Finding 5).
  The `EquityBar` schema already mandates "don't drop delisted
  symbols"; the Bloomberg export must honor it.
- Regimes may need per-market presets (`regimes.py` already supports
  named presets — add `strict_hk` etc. if thresholds differ).

---

## How this feeds the autonomous loop (Round 10)

The Round 10 self-directed research loop (a `ResearchDirector` that
generates its own themes and hunts alpha with no human topic) is
**decided to be robustness-first**: every candidate basket must clear
the strict regime on a *held-out* window before it counts as alpha —
directly answering the v2/v3 fragility finding.

That loop's quality is bounded by its data. With today's SP-50 × 2y it
will mostly autogenerate fragile baskets (we've seen this). With the
F1–F4 lake — more names, longer history, tick granularity, multiple
markets — it finally has the decorrelation budget and statistical power
to find baskets that survive out-of-sample. **That's why the data plan
comes before the autonomous loop in the build order.**

---

## Recommended build order

1. **Tick schema gap** (`BarFrequency.TICK` / `TickRecord` +
   columnar loader path) — small, unblocks everything tick.
2. **F1 Bloomberg export** interface + mock now; real `blpapi` wiring
   when a terminal is available.
3. **F2 cloud lake** with direct object reads (`source="cloud"`);
   read API only if/when multi-user.
4. **Round 10 autonomous loop** (robustness-first) — now has real data
   to chew on.
5. **F3 RAG** — makes the director smarter once there's a lake + loop.
6. **F4 multi-market** (HK …) — data + config on top of F1–F2.

Steps 1–3 and 5–6 are data/infra; step 4 is the research engine. The
engine is already built and audited — what it lacks is fuel.

---

## What's blocked vs buildable today

| Item | Status |
|---|---|
| Tick schema (`BarFrequency.TICK` / `TickRecord`) | **buildable now** (pure schema) |
| `BloombergExporter` interface + mock | **buildable now** (real calls need terminal) |
| `cloud_loader.py` against a public test bucket | buildable now if we have any bucket |
| Real Bloomberg pull | **blocked** — needs terminal + entitlement |
| Cloud bucket + catalog | **blocked** — needs a cloud account |
| Read API service | buildable now, deploy blocked on cloud |
| RAG index | buildable now (local vector store); better after lake |
| HK / multi-market data | **blocked** — needs Bloomberg |

The honest takeaway: we can build the **interfaces and the tick
schema** now so nothing downstream churns later, but the *data itself*
waits on a Bloomberg Terminal and a cloud account.
