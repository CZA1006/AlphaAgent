# Research Director

`ResearchDirector` is the layer above the existing AlphaAgent validation loop.
The validation loop answers: "given this theme, can the agent propose and test
factor hypotheses?"  The director answers: "which theme should we test next,
what data is missing, and what command should run?"

## Current Autonomous Flow

```text
dataset/doctor snapshot
        +
validation history
        |
        v
ResearchDirector
        |
        +-- ranked research topics
        +-- data gaps and recommended actions
        +-- selected next validation command
        |
        v
validate_strict / HK IPO BigQuery loop
        |
        v
validation reports and promoted/rejected factors
```

The first implementation is deterministic and offline.  It is intentionally
safe: it plans the next topic and emits the command, but it does not spend GCP
or LLM budget unless an operator runs that command.

## HK IPO Entry Point

```bash
make research-director-hk-ipo
make research-director-hk-ipo ARGS="--json"
```

The selected HK IPO topic currently prioritizes event-conditioned
microstructure research because these tables are aligned:

- `ipo_daily_prices`
- `micro_features_daily`
- `tick_manifest_target`
- `ipo_event_features_daily`
- `ipo_event_dates_curated`

The director also keeps the following data work in queue:

- source-level QA for nonpositive tick value rows
- review of `ipo_event_terms_needs_review`
- backfill or explicit unavailable marking for missing HKEX document coverage
- exclusion of Bloomberg-only lockup anomalies from truth tables
- future intraday feature materialization from raw TRADE/BID/ASK ticks

## Next Execution Command

The director emits a validation command shaped like:

```bash
make validate-hk-ipo-events ARGS="--llm openrouter --n-candidates 12 --n-cycles 3"
```

That command still uses the existing harness loop:

1. load HK IPO BigQuery daily, microstructure, and event features
2. ask the proposer for candidates under the selected theme
3. compile DSL candidates
4. evaluate and refine under the selected regime
5. write validation reports and promoted artifacts

## The Autonomous Executor

`scripts/autonomous_researcher.py` closes the plan→execute loop under
operator guardrails:

```bash
make autonomous-researcher-hk-ipo                  # dry run: plan + emit command only
make autonomous-researcher-hk-ipo-run              # --execute: actually run validation
make autonomous-researcher-hk-ipo-run ARGS="--llm openrouter --iterations 3 --cost-budget-usd 2"
```

Each iteration: `ResearchDirector.plan` → run the selected
`validate_strict` command → read the new validation reports →
`ResearchPostRunPolicy.decide` picks the next topic (or stops) → the next
iteration re-plans with the updated history.  Guardrails: dry-run by
default, iteration cap, per-run timeout, token/cost budgets, and stop
after N consecutive no-promote iterations.  Every run writes a
machine-readable record to `artifacts/autonomous_runs/`.

`validate_strict` also reloads full validation reports from earlier processes
when their `memory_scope_id` matches the current run. The scope hashes the full
evaluation request, promotion trail, and actual input-panel contents. Their
factor expressions, decisions, rejection gates, and headline IC values are
merged into proposer memory before the first cycle. Current-cycle records take
precedence, and reports from another data snapshot or evaluation contract are
excluded. This makes feedback durable across autonomous iterations without
requiring Postgres for local runs.

Topics carry a typed executor. Discovery uses `propose`; the
`hk_ipo_cost_realism_oos` topic uses `replay_promoted`. On the switch, the
autonomous runner passes the exact promoted source cycle ids to
`validate_strict`, which reloads only those expressions, verifies the input
panel fingerprint, skips the LLM and mutation paths, and evaluates once at
15 bps instead of the 5 bps discovery baseline. The validation report records
the candidate source, source cycle ids, data fingerprint, and cost assumption.
The bounded run stops after this replay for operator inspection.

## What Is Not Fully Automated Yet

- **No scheduler** — an operator starts each run; nothing re-invokes the
  loop on a cadence.
- **Data work stays manual** — the director queues data gaps (refill,
  ingestion, QA) but the executor never performs them.
- **Event studies live outside the loop** — `scripts/analysis/*.py`
  (microstructure OOS, lockup/greenshoe/stabilization event studies) are
  operator-run analyses; the director does not schedule or read them.
- **Two topics still share the proposer executor** — event-truth review and
  raw-tick materialization currently change prompt guidance but do not yet
  dispatch document-review or feature-build tools. Cost replay now has a
  separate deterministic execution path.
- **Persistence selection is experimental** — `combine_factors` exposes
  `--selection-strategy persistence --top-k K`, and records that choice in
  its report and promotion trail, including the scoring-formula version. The
  default remains `input_order` until a
  fresh, untouched OOS window validates the policy.
