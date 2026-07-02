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

## What Is Not Fully Automated Yet

The planning object is machine-readable, but direct execution is not enabled by
default.  To make this fully self-running, add a controlled executor that:

1. calls `ResearchDirector.plan`
2. runs the selected validation args under explicit budget limits
3. records the report path back into the next director context
4. opens a data-refill topic when a blocking data gap appears
5. stops on repeated no-progress cycles or budget exhaustion
