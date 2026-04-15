# Data Plan

## Initial asset focus

### US equities
Required early coverage:
- OHLCV bars
- symbol / ticker metadata
- corporate actions placeholders
- later: point-in-time fundamentals

### Crypto
Required early coverage:
- spot OHLCV
- perp-friendly symbol normalization
- exchange-scoped metadata
- later: funding, open interest, basis, liquidation data

## Local storage plan

### DuckDB + Parquet
Use for:
- historical market data
- factor panels
- intermediate research outputs
- local analytics queries

### Postgres
Use for:
- experiment registry
- hypothesis registry
- skill registry
- memory metadata
- market state metadata

### File artifacts
Store under `artifacts/`:
- charts
- reports
- experiment JSON
- logs

## Suggested data source plan

### Phase 1
- equities: start with a simple historical bars source and local samples
- crypto: start with a simple OHLCV source and local samples
- fundamentals: begin with adapter stubs and schemas, then extend

### Data-engineering rules
- keep symbol normalization explicit
- separate raw and cleaned layers
- preserve timestamps carefully
- make point-in-time constraints explicit in code comments and interfaces

## Recommended local directory split

```text
data/
├── raw/
├── bronze/
├── silver/
└── gold/
```

Meaning:
- `raw`: source dumps
- `bronze`: lightly normalized source-specific tables
- `silver`: cleaned and standardized datasets
- `gold`: research-ready data products

## Minimum viable first datasets

### Equities
- 3 to 10 liquid US tickers
- daily bars for a multi-year sample

### Crypto
- BTC/USDT and ETH/USDT or equivalent
- daily and optionally 1H bars

The goal is not coverage. The goal is a trustworthy loop.
