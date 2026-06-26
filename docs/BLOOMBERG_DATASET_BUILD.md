# Task: Build a shared Bloomberg tick dataset (HK + beyond)

> Concrete build spec for turning Bloomberg Terminal tick data into a
> cloud-hosted dataset that any AlphaAgent machine can query.  Grounded
> in a real HK IPO tick sample (`00100 MiniMax-W`) and the official
> Bloomberg `IntradayTickRequest` mechanics.  This is the F1+F2
> execution detail behind [`DATA_INFRA_PLAN.md`](DATA_INFRA_PLAN.md).

---

## 0. The single most important constraint

**Bloomberg only serves intraday *tick* data for the last ~140 calendar
days.**  ([Bloomberg API Developer's Guide](https://data.bloomberglp.com/professional/sites/4/blpapi-developers-guide-2.54.pdf))

Consequence: tick history **cannot be backfilled** the way we backfilled
daily Polygon bars.  If we don't pull a day within ~140 days of it
happening, it is **gone forever** (short of buying Bloomberg's separate
Tick History / B-PIPE product).  Therefore the dataset build is a
**standing, scheduled archival job**, not a one-off download.  This
reframes the whole task: priority #1 is *start capturing daily now* so
the archive grows; analysis tooling can come later.

---

## 1. What the data actually looks like (real sample)

Source files (one stock, the MiniMax-W HK IPO):

```
00100_MiniMax-W/
├── metadata.json      # 436 B  — provenance
├── all_ticks.csv      # 122 MB — BID + ASK + TRADE merged
├── all_ticks.parquet  #  12 MB — same, columnar (≈10× smaller)
├── quote_recap.csv    #  71 MB — BID + ASK only
└── trade_recap.csv    #  51 MB — TRADE only
```

### `metadata.json`

```json
{
  "stock_code": "00100", "english_name": "MiniMax-W",
  "listing_date": "2026-01-09", "sector": "TMT", "sub_sector": "AI",
  "bloomberg_ticker": "100 HK Equity",
  "fetched_at_utc": "2026-05-12T06:29:37Z",
  "query_window_utc": ["2026-01-08T16:00:00Z", "2026-02-06T15:59:59Z"],
  "target_trading_days": 15
}
```

Note the `query_window` is in **UTC but bounded on HK session edges**
(16:00 UTC = 00:00 HKT next day boundary).  Provenance fields
(`bloomberg_ticker`, `fetched_at_utc`) are exactly what we need for an
auditable lake.

### Tick rows (`all_ticks.parquet` — the canonical artifact)

Parquet schema (SNAPPY, 1.27 M rows, 15 trading days, 2 row groups):

| column | arrow type | meaning |
|---|---|---|
| `event_type` | string | `BID` / `ASK` / `TRADE` |
| `time` | `timestamp[us, tz=UTC]` | event time, microsecond, UTC |
| `type` | string | duplicate of `event_type` (Bloomberg's field name) |
| `value` | double | price |
| `size` | int64 | quote size or trade size (shares) |
| `conditionCodes` | string | exchange/Bloomberg trade condition flags |
| `exchangeCode` | string | `H` = HKEX primary |
| `hk_time` | `timestamp[us, tz=Asia/Hong_Kong]` | same instant, local |
| `trading_date` | `date32` | HK session date (the natural partition key) |

Observed in the sample:
- **Event mix** (first 200k rows): ~69 % TRADE, ~16 % BID, ~15 % ASK.
- **Condition codes** on trades: `IE`, `P`, `X` (and blank).  These are
  Bloomberg/exchange condition flags — `IE` clusters at the open
  (auction/indicative), `P` dominates continuous trading, `X` is rare.
  **Do not hard-code meanings** — Bloomberg condition codes are
  documented per-exchange in the terminal (`DOCS`/`QR`) and via
  [Bloomberg's condition-code reference](https://www.bloomberg.com/professional/products/data/enterprise-catalog/reference/trading-strategies-with-condition-codes-in-tick-history-data/).
  Capture them verbatim and map them in a separate, versioned lookup so
  research can filter auction / odd-lot / off-exchange prints.
- **Volume spikes on listing day** (325k rows day 1, decaying to ~25k)
  — IPO microstructure; a normal liquid name will be flatter.

### Sizing math (the lake budget)

- This IPO: **12 MB parquet / stock / 15 active days ≈ 0.8 MB/stock/day**
  on a *hot* name.  A typical liquid HK name is lighter; an illiquid one
  far lighter.
- Order-of-magnitude for a real universe: **500 liquid HK names × ~250
  trading days ≈ 100 GB/year** of tick parquet.  Comfortably cloud-scale
  (cents/GB/month on object storage), but **not** something to keep on a
  laptop.  CSV is ~10× larger — **never store or ship CSV**; parquet is
  the wire + rest format.

---

## 2. Official Bloomberg mechanics

The sample is the output of a `//blp/refdata` **`IntradayTickRequest`**
(via `blpapi`, usually wrapped by `xbbg`).

- **Request shape:** `security` (e.g. `100 HK Equity`),
  `eventTypes` (`TRADE`, `BID`, `ASK`, and optionally `BID_BEST`,
  `ASK_BEST`, `MID_PRICE`, `AT_TRADE`, `SETTLE`), `startDateTime`,
  `endDateTime` (UTC), `includeConditionCodes=true`,
  `includeExchangeCodes=true`.
- **`TRADE` = `LAST_PRICE`** for tick requests (per the Developer's
  Guide).
- **Per-request limits:** intraday tick is large; Bloomberg caps the
  response and daily data-point quota (the "ZFP"/data limits depend on
  the terminal's entitlement).  Practically: **request one
  `(security, trading_date)` at a time** and throttle.
- **History limit: ~140 calendar days** (see §0).
- **Entitlement:** `IntradayTickRequest` from a Desktop API
  (`localhost:8194`) needs a logged-in Terminal on that machine.  There
  is **no way to call it from an arbitrary cloud box** — hence the
  push-from-terminal architecture below.

Sources:
[Bloomberg API Developer's Guide](https://data.bloomberglp.com/professional/sites/4/blpapi-developers-guide-2.54.pdf) ·
[MATLAB Bloomberg `timeseries` (tick) docs](https://www.mathworks.com/help/datafeed/bloomberg.timeseries.html) ·
[HKEX Trading Mechanism](https://www.hkex.com.hk/Services/Trading/Securities/Overview/Trading-Mechanism?sc_lang=en)

---

## 3. Target architecture — how other computers access it

```
┌─ Terminal machine (Windows, logged-in Bloomberg) ────────────────┐
│  scripts/bloomberg_export.py                                       │
│    blpapi/xbbg IntradayTickRequest                                 │
│      per (security, trading_date)                                  │
│    → normalize to canonical tick parquet                          │
│    → write local + UPLOAD to object store                          │
│  (runs on a SCHEDULE — daily, within the 140-day window)          │
└───────────────────────────────┬───────────────────────────────────┘
                                 │  one-way push (parquet only)
                                 ▼
┌─ Cloud object store  (S3 / GCS / R2) ─────────────────────────────┐
│  ticks/market=HK/symbol=00100/date=2026-01-09/ticks.parquet       │
│  _catalog/manifest.parquet   (what exists + checksums + fetched_at)│
└───────────────────────────────┬───────────────────────────────────┘
                                 │  read (many machines)
                 ┌───────────────┼───────────────┐
                 ▼               ▼                ▼
        research laptop   autonomous loop   teammate / notebook
        source="cloud"    source="cloud"    direct parquet read
```

**Two access modes** (both register in
`alpha_harness/data/loader_factory.py`, so no consumer code changes):

1. **Direct object reads (recommended first).**  `source="cloud"` with
   `base_path="s3://bucket/ticks"` → loader reads parquet straight from
   the bucket via `s3fs`/`gcsfs` + `pyarrow`.  Zero services to run.
   Other computers just need read credentials.
2. **Thin read API (add only when needed).**  A small FastAPI service in
   front of the lake (`GET /ticks?symbol=&market=&start=&end=`) for
   access control / metering / many small queries.  Speaks
   `DataRequest` in, Arrow out.

**Why push, not a pull-API-on-the-terminal:** the Terminal box is
license-locked and firewalled; you cannot (and per Bloomberg terms
should not) expose its data feed as a network service.  The terminal
only ever *uploads* finished parquet.

---

## 4. Canonical schema in the harness

Add a tick path to the existing data layer (the harness already isolates
sources behind `loader_factory` + `DataRequest`):

- **`BarFrequency.TICK`** (new enum member) — or a dedicated
  `TickRecord` Pydantic model for validation at the edges only.
- **Canonical tick parquet columns** (rename Bloomberg's into ours,
  keep provenance):

  | ours | from sample | notes |
  |---|---|---|
  | `symbol` | metadata `stock_code` + market | e.g. `00100.HK` |
  | `event_type` | `event_type` | enum BID/ASK/TRADE |
  | `ts_utc` | `time` | `timestamp[us, tz=UTC]` |
  | `price` | `value` | double |
  | `size` | `size` | int64 |
  | `condition_codes` | `conditionCodes` | verbatim string |
  | `exchange_code` | `exchangeCode` | `H` = HKEX |
  | `trading_date` | `trading_date` | partition key |
  | `source` | const `"bloomberg"` | provenance |
  | `fetched_at_utc` | metadata | provenance/audit |

- **Never materialize tick as `list[Bar]`** — 1.27 M rows for *one*
  stock-fortnight.  Tick stays columnar (Arrow/parquet) end to end; the
  Pydantic models are only for small edge validation, not the hot path.
- **Bar-from-tick resampling** lives in the data layer (PIT-safe), so a
  factor can still request "5-minute bars built from tick."

---

## 5. Build pipeline — step by step

### Stage 1 — Capture (on the terminal machine), the urgent part

`scripts/bloomberg_export.py` (Windows + logged-in Terminal):

1. Read a universe file (`configs/universes/hk_*.txt`) of Bloomberg
   tickers (`100 HK Equity`, `700 HK Equity`, …).
2. For each `(ticker, trading_date)` in the last ≤140 days **not already
   in the catalog**:
   - `IntradayTickRequest` with `eventTypes=[TRADE,BID,ASK]`,
     `includeConditionCodes`, `includeExchangeCodes`.
   - Normalize → canonical tick schema (§4) → write
     `ticks/market=HK/symbol=…/date=…/ticks.parquet`.
   - Write/append a sidecar `metadata.json` per (symbol, day) for
     provenance.
3. Upload new partitions to the object store; update
   `_catalog/manifest.parquet`.
4. Idempotent + resumable (skip partitions already in the catalog) —
   same discipline `scripts/backfill_parquet.py` already uses.
5. **Schedule it** (Windows Task Scheduler / cron-equivalent) to run
   every trading evening so nothing ages past 140 days.

> Build the `BloombergExporter` **interface + a mock now** (returns the
> sample file's shape) so the normalize/partition/upload code is fully
> testable without a terminal.  Swap in real `blpapi` calls on the
> terminal box.

### Stage 2 — Store (cloud)

- Object store bucket; partition `market/symbol/trading_date`.
- `_catalog/manifest.parquet`: one row per `(market, symbol, date)` with
  `n_rows`, `bytes`, `checksum`, `fetched_at_utc`, `source`.  This is the
  cheap "what do we have?" index nobody has to list-bucket for.

### Stage 3 — Access (any machine)

- `alpha_harness/data/cloud_loader.py` — a `cloud` source in
  `loader_factory`, reads partitions via `pyarrow` + `s3fs`/`gcsfs`,
  honors `DataRequest` (symbols, date range), returns a DataFrame or an
  Arrow handle for tick-scale pulls.
- Local cache tier: reuse `data/silver/` as a read-through cache so hot
  partitions aren't re-downloaded.
- Optional FastAPI read service later.

---

## 6. Caveats grounded in the sample

- **Survivorship / IPO bias:** an IPO's first days are extreme
  (325k → 25k ticks/day).  A research universe must mix listing
  vintages and **must not drop delisted names** (the `EquityBar` schema
  already mandates this — Finding 5 in the audit).
- **Condition codes are not optional noise.** Auction prints (`IE`-ish),
  odd lots, and off-exchange (`P`/`X`-ish) distort naive VWAP/returns.
  Capture verbatim; maintain a **versioned code→meaning map** per
  exchange; let regimes/evaluators choose what to include.
- **Timezone discipline:** keep both `ts_utc` and `trading_date` (HK
  session date).  HK has a lunch break and a closing auction (CAS); bar
  resampling must use the HK trading calendar, not a naive 24h day.
- **Two timestamps already provided** (`time` UTC + `hk_time` local) —
  keep UTC as the source of truth, derive local for display only.
- **Licensing:** Bloomberg data redistribution is contractually
  restricted.  The cloud lake must be **access-controlled to entitled
  users only** — this is a compliance requirement, not just good
  practice.  Confirm your Bloomberg agreement permits the internal
  storage/sharing pattern before going wide.

---

## 7. Task checklist (build order)

**Buildable now (no terminal / no cloud):**
- [ ] Add `BarFrequency.TICK` + `TickRecord` to `data/models.py`.
- [ ] `BloombergExporter` interface + mock that emits the sample's
      canonical schema; unit-test normalize → partition.
- [ ] `data/cloud_loader.py` interface + tests against a local
      "fake bucket" (a temp dir) so the read path is proven.
- [ ] Catalog/manifest writer + reader.
- [ ] Tick→bar resampler (PIT-safe, HK-calendar-aware) + tests.

**Blocked on a Bloomberg Terminal:**
- [ ] Real `blpapi`/`xbbg` `IntradayTickRequest` in
      `scripts/bloomberg_export.py`.
- [ ] Scheduled daily capture job (within the 140-day window).
- [ ] HK universe files + condition-code map.

**Blocked on a cloud account:**
- [ ] Provision bucket + read credentials.
- [ ] Wire `source="cloud"` to the real bucket; deploy optional read API.

**The urgent one:** stand up Stage 1 capture as soon as a terminal is
available — every day not captured is lost to the 140-day window.

---

## 8. How this powers AlphaAgent

Once the HK tick lake exists and `source="cloud"` is wired, the
existing engine consumes it unchanged: `validate_strict
--data-source cloud --universe configs/universes/hk_liquid.txt
--frequency tick` (with a tick→bar resample), and the
robustness-first autonomous loop (Round 10) finally has the
breadth + granularity it needs to look for alpha that survives
out-of-sample — which the 50-name × 2y daily US sample could not
provide (see the honest case studies).

Sources:
[Bloomberg API Developer's Guide](https://data.bloomberglp.com/professional/sites/4/blpapi-developers-guide-2.54.pdf) ·
[Bloomberg condition codes in tick history](https://www.bloomberg.com/professional/products/data/enterprise-catalog/reference/trading-strategies-with-condition-codes-in-tick-history-data/) ·
[HKEX Trading Mechanism](https://www.hkex.com.hk/Services/Trading/Securities/Overview/Trading-Mechanism?sc_lang=en)
