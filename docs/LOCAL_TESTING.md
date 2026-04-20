# Local testing with real APIs

This guide is for the step between "unit tests pass" and "Round 4 autonomy
work begins" — it covers how to wire real keys into the repo and exercise the
full stack locally.

Everything here is **opt-in**.  The default `make test` path never touches
the network or requires any key.

---

## 0. One-time setup

```bash
cp .env.example .env       # copy the template
$EDITOR .env               # fill the REQUIRED fields for the paths you plan to run
make install               # or `make dev` if you want lint/test tooling
```

The Makefile auto-loads `.env` — you do **not** need to `source` it yourself.
If you invoke scripts directly from your shell, run `set -a; source .env; set +a`
first.

---

## 1. What you must fill in `.env`

The table below is the minimum.  Everything else in `.env.example` has sane
defaults and can stay blank.

| Variable              | Fill when you want to…                 | Where it's used                     |
|-----------------------|-----------------------------------------|-------------------------------------|
| `OPENROUTER_API_KEY`  | Run a real LLM (proposer / autonomous)  | `alpha_harness/llm/config.py`       |
| `POLYGON_API_KEY`     | Load real US-equity bars                | `alpha_harness/data/polygon_equities.py` |
| `POSTGRES_*`          | Use the SQL registry backend            | `alpha_harness/config.py` + `docker-compose.yml` |

The `POSTGRES_*` defaults already match `docker-compose.yml`, so you only
need to touch them if you run Postgres outside Docker or change the password.

**Never commit `.env`.** It is listed in `.gitignore`; `git status` should
not show it after you fill it in.

---

## 2. Preflight — `make doctor`

Before your first real run, validate the configuration:

```bash
make doctor            # checks everything
make doctor-mock       # only what mock mode needs (always passes)
make doctor-real       # checks OPENROUTER_API_KEY loads cleanly
make doctor-sql        # also tries a Postgres SELECT 1
```

The doctor never makes live LLM or Polygon calls — it only checks that
variables are present, numeric fields parse, and Postgres is reachable.
Exit code `0` means the matching `make run-*` command is safe to invoke.

Typical failure → fix mapping:

| Doctor says                                        | Fix                                         |
|----------------------------------------------------|---------------------------------------------|
| `OPENROUTER_API_KEY is set — empty or unset`       | Fill it in `.env`                            |
| `OpenRouterConfig.from_env()` error about a float  | `OPENROUTER_TEMPERATURE` / `_TIMEOUT` malformed |
| `Postgres reachable … ConnectionRefusedError`      | `make db-up && make db-bootstrap`            |
| `Local Parquet store … empty`                      | `uv run python -m scripts.sample_ingest`     |

---

## 3. Test paths in recommended order

Work your way down this list; each rung adds one external dependency.
If a later rung fails, drop to the previous rung and `make doctor` to
isolate the cause (config vs code vs provider).

### 3.1 Mock mode — no keys required

```bash
make run-mock
```

Runs the full proposer → research → refinement loop with a mock LLM and
synthetic price data.  This must succeed before you add any keys.

### 3.2 Real LLM + synthetic data

```bash
make doctor-real
make run-real                      # single-cycle, minimal tokens
# or
make autonomous-real ARGS="--n-candidates 2"
```

Uses real OpenRouter calls against synthetic price data.  This is the
cheapest way to sanity-check that the LLM wiring works — a single
autonomous cycle with two candidates typically costs well under a cent.

### 3.3 Real LLM + real Polygon data

```bash
make doctor                        # need both keys
make run-real-data ARGS="--symbols AAPL,MSFT --start-date 2024-01-01 --end-date 2024-03-31"
```

Pulls bars from Polygon and evaluates a factor on them.

**Free-tier constraints (observed during Round 3 testing):**

- ~5 requests/minute — keep `--symbols` to 5 or fewer per invocation.
- Aggregates restricted to roughly the last 2 years — default
  `--start-date` in `autonomous_cycle.py` is `2024-07-01` for that reason.

A successful run looks like this (trimmed):

```text
INFO  Loaded 640 bars for 5 symbols from polygon
INFO  Dispatching theme '...' to HarnessAgentAdapter.
INFO  Running research cycle for hypothesis ...: rank(ts_mean(volume, 5) / ts_mean(volume, 20))
INFO  Cycle complete for hypothesis ... → decision=reject
...
  Roots:
    [rejected] rank_ts_mean_volume_5_ts_mean_volume_20 ic=-0.0072 rank_ic=0.0214
```

`decision=reject` on a tiny 5-name × 6-month panel is *expected and
correct* — the evaluator is doing its job, not failing. Promoted
candidates become more likely with a larger universe (Round 4).

### 3.4 Real LLM + real Polygon + Postgres

```bash
make db-up            # start the container (first time only)
make db-bootstrap     # create registry tables (first time only)
make doctor-sql
make run-real-sql
```

Same as 3.3 but experiments, hypotheses, and lineage memory land in
Postgres instead of in-memory dicts.  Inspect them with any psql client:

```bash
docker compose exec postgres psql -U alphaagent -c "SELECT id, decision FROM experiments ORDER BY created_at DESC LIMIT 5;"
```

### 3.5 Real research universe — one-time Parquet backfill

```bash
make doctor-real                  # need POLYGON_API_KEY
make backfill-sp50                # ~10 minutes on free-tier (5 rpm × 50 names)
```

Populates `data/silver/equities/{symbol}.parquet` for the 50 tickers in
`configs/universes/sp50.txt`.  After the first successful run, every
`--data-source parquet` cycle reads locally and no longer hits Polygon:

```bash
make run-real ARGS="--data-source parquet --symbols AAPL,MSFT,NVDA --start-date 2024-01-01 --end-date 2024-06-30"
make autonomous-real ARGS="--data-source parquet --n-candidates 3"
```

The backfill is **idempotent and resumable** — each symbol whose file
already covers the requested window is skipped with a ``cache-hit`` log
line.  Re-running `make backfill-sp50` after an interruption (Ctrl-C,
network blip, sleep) picks up where it left off.

Customising the universe:

```bash
# A narrower slice (one-off)
make backfill ARGS="--universe configs/universes/sp50.txt --start-date 2024-01-01 --end-date 2024-06-30"

# Your own list
cp configs/universes/sp50.txt configs/universes/my_watchlist.txt
$EDITOR configs/universes/my_watchlist.txt
make backfill ARGS="--universe configs/universes/my_watchlist.txt"
```

Tips:

- **Free-tier pacing.** Polygon limits you to ~5 rpm. 50 symbols ≈ 10
  minutes — leave the terminal alone; the rate limiter will sleep as
  needed.  Tune via `POLYGON_RPM` if you have a paid plan.
- **Free-tier row cap — important.** Polygon's free tier silently
  truncates each aggregates response at exactly **500 rows** and does
  not issue a pagination token, so a paid-tier-style "2023-01-01 →
  today" request actually returns roughly the most recent 2 years per
  symbol.  The backfill default now matches this (`--start-date =
  today − 2 years`) and the Polygon loader logs a warning the first
  time it sees a suspicious 500-row response.  Paid plans lift the cap
  — set `--start-date` further back as soon as you upgrade.
- **Idempotency on reruns.** The cache predicate allows a 7-day slack
  on the end side (Polygon bar-latency + weekends), so running
  `make backfill-sp50` the next day does not refetch the whole
  universe.  Gaps longer than a week still trigger a refetch.
- **Force-refresh.** Pass `--force` to unconditionally rewrite every
  file, or move the start date further back to explicitly request
  older data.

### 3.6 Passing extra flags

Every `run-*` and `autonomous-*` target honours the `ARGS` variable:

```bash
make run-real ARGS="--expression 'rank(ts_std(close, 20))' --n-days 240"
make autonomous-real ARGS="--theme 'intraday mean reversion' --n-candidates 3"
```

See `uv run python -m scripts.run_research_cycle --help` and
`uv run python -m scripts.autonomous_cycle --help` for the full surface.

---

## 4. Debugging guide

| Symptom                                         | Where to look first                          |
|-------------------------------------------------|-----------------------------------------------|
| Script exits immediately with non-zero          | `make doctor` — almost always a missing var  |
| `LLMConfigError: OPENROUTER_API_KEY is not set` | `.env` not loaded; use Makefile target or `source .env` |
| `psycopg.OperationalError: connection refused`  | `make db-up`; check `POSTGRES_HOST=localhost` |
| `No data returned from polygon`                 | Date range, ticker spelling, or free-tier rate limit |
| LLM returns nonsense / proposer drops all       | Try a different current slug (e.g. `anthropic/claude-opus-4.7`) or raise temperature. OpenRouter retires slugs periodically — check https://openrouter.ai/models if you get a 404. |
| `429 Too Many Requests` from Polygon            | Free tier is ~5 req/min. Pass fewer `--symbols` or wait 60s between runs. |
| `403 Forbidden` from Polygon aggregates         | Free tier restricts bars to roughly the last 2 years. Pick a recent `--start-date`. |
| `make run-real` prints `OPENROUTER_API_KEY is empty` but `.env` has it | You exported the var in a prior shell with an empty value; `unset OPENROUTER_API_KEY && make run-real` |

---

## 5. Round 4A.1 guardrails — cost, rate limits, call hygiene

Added in Round 4A.1.  All opt-in via env / CLI; defaults stay friendly to
mock-mode dev.

### 5.1 Per-cycle LLM token & cost budget

| Variable / flag                           | Meaning                                                  |
|-------------------------------------------|----------------------------------------------------------|
| `ALPHA_AGENT_TOKEN_BUDGET` / `--token-budget`         | Hard cap on cumulative `total_tokens` per cycle   |
| `ALPHA_AGENT_COST_BUDGET_USD` / `--cost-budget-usd`   | Hard cap in USD per cycle                         |
| `ALPHA_AGENT_PROMPT_COST_PER_1K`          | Prompt rate (USD / 1K tokens) used to price calls        |
| `ALPHA_AGENT_COMPLETION_COST_PER_1K`      | Completion rate (USD / 1K tokens)                        |

When either cap is set, the autonomous cycle wraps its LLM client in a
`BudgetedLLMClient`.  The call that would push the ledger over the cap is
still issued and *logged* (so you can see what tripped the limit); the
next call then raises `BudgetExceededError` and the cycle exits with
code `3`.

**Recommended starting values** (already present in `.env.example`):

```
ALPHA_AGENT_TOKEN_BUDGET=50000
ALPHA_AGENT_COST_BUDGET_USD=0.50
ALPHA_AGENT_PROMPT_COST_PER_1K=0.003
ALPHA_AGENT_COMPLETION_COST_PER_1K=0.015
```

Adjust the `*_COST_PER_1K` rates to your actual OpenRouter model before
leaning on the dollar cap — the defaults track
`anthropic/claude-sonnet-4.6` and will mis-price anything else.

### 5.2 Structured LLM call log

Every cycle (mock or real) writes an append-only JSONL record per call
to:

```
artifacts/llm_calls/{cycle_id}.jsonl
```

Override the base directory with `ALPHA_AGENT_LLM_LOG_DIR` or
`--llm-log-dir`.  Override the cycle id with `--cycle-id` (default is
`cycle-<12-hex>`).

Each line contains: timestamp, `cycle_id`, `purpose`, `latency_ms`,
request metadata (model, temperature, per-message role + length +
80-char preview, SHA-256 fingerprint of the joined messages), response
metadata (model, finish_reason, token counts, 200-char content preview,
SHA-256 of the full content), and `error` (if any).

**Redaction contract:** API keys and full prompt/response text are
*never* written — only previews + SHA-256 fingerprints.  The preview
cap is 80 chars per message / 200 chars per response.

Quick inspection:

```bash
tail -n +1 artifacts/llm_calls/*.jsonl | jq '.response.total_tokens'
```

### 5.3 Polygon pacing & 429 retry

| Variable         | Default | Meaning                                                    |
|------------------|---------|------------------------------------------------------------|
| `POLYGON_RPM`    | `5`     | Requests/min the Polygon loader paces itself at            |

`PolygonEquitiesLoader` now goes through `request_with_retry`, which:

1. Acquires a sliding-window rate limiter (5 rpm by default) before
   every attempt.
2. On HTTP 429, honours `Retry-After` when present; otherwise uses
   exponential backoff (base `2.0`, up to `max_retries=4`).
3. Returns the final 429 to the caller once retries are exhausted, so
   `raise_for_status()` surfaces a normal error.

### 5.4 Round 4A.3 evaluator richness (sector/beta/horizons/cost)

`scripts/autonomous_cycle.py` now exposes four evaluator knobs.  All are
optional — omitting them preserves the prior single-horizon, no-neutralize
behaviour.

```bash
uv run python -m scripts.autonomous_cycle \
  --mock-llm \
  --data-source parquet --symbols AAPL,MSFT,JPM,XOM \
  --neutralize sector \
  --sector-map configs/universes/sp50_sectors.csv \
  --extra-horizons 1,20 \
  --cost-bps 5.0
```

- `--neutralize {none,sector,beta,both}` — cross-sectional residualization
  applied to forward returns.  `sector` subtracts the per-date sector mean;
  `beta` subtracts `beta_i * universe_mean[t]` using in-sample beta.
- `--sector-map PATH` — required for `sector` / `both`.  A
  `{symbol,sector}` CSV; `configs/universes/sp50_sectors.csv` ships for the
  default universe.  Unmapped symbols land in `UNKNOWN`.
- `--extra-horizons 1,20` — additionally evaluate at 1- and 20-bar forward
  returns.  Results land in `metadata.ic_by_horizon`; the judge rejects
  factors whose IC sign agrees with the primary horizon in fewer than two
  of the evaluated horizons.
- `--cost-bps 5.0` — round-trip bps applied to `turnover` to produce
  `net_quantile_spread = quantile_spread - (cost_bps/10000) * turnover`.

### 5.5 What's deliberately deferred to later Round 4 phases

- **Cloud / remote deployment.** Everything here assumes local dev.
- **Cross-cycle budget accumulation / dashboards.** Budgets are
  per-cycle; aggregation over runs is not automated.
- **Live-data ingestion pipelines beyond ad-hoc Polygon calls.** No
  scheduled refresh, no Parquet backfill automation.
- **Hermes-side prompt assembly.** The adapter boundary is in place but
  the runtime that actually calls `HarnessAgentAdapter.run_theme` from a
  live agent loop ships in Round 4.
- **Secrets management.** For local dev `.env` + `make doctor` is enough;
  production secret rotation is out of scope.

---

## 6. Quick reference

```bash
# first time
cp .env.example .env && $EDITOR .env
make install
make doctor-mock && make run-mock         # confirms baseline

# add real LLM
make doctor-real && make run-real

# add real data
make doctor && make run-real-data

# add SQL
make db-up && make db-bootstrap
make doctor-sql && make run-real-sql
```

Every step above is idempotent; re-running it is safe.
