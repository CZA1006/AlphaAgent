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

**Region gotcha:** the default `OPENROUTER_MODEL`
(`anthropic/claude-sonnet-4.6`) is refused with 403 "provider Terms of
Service" from this environment — every live run must override it, e.g.
`export OPENROUTER_MODEL=deepseek/deepseek-chat-v3.1` (the model the
case studies used).

A USD cap now fails closed unless both pricing rates are explicit. Set them
from the provider's current model pricing before using `--cost-budget-usd`:

```bash
export ALPHA_AGENT_PROMPT_COST_PER_1K=<prompt-rate>
export ALPHA_AGENT_COMPLETION_COST_PER_1K=<completion-rate>
```

Validation schema v5 records provider-reported `usage.cost`, cumulative tokens,
calls, and the fallback rates. It also distinguishes actual-cost calls from
estimated calls. The 2026-07-14 first run predated this guard: its `$0.0036`
figure was an external estimate, not a reproducible artifact value.

The first run's sole candidate was replayed on the same panel fingerprint after
the global-holdout fix and rejected: rank-IC fell from +0.0230 in training to
-0.0030 on the trailing holdout, while tail concentration reached 11.76. It is
not a promoted research lead.

The corrected live acceptance run (`autonomous-hk_ipo-20260714T103615Z-9609039a`)
used three DeepSeek calls and recorded 6,806 tokens / `$0.00296483`, with
`actual_cost_calls=3` and `estimated_cost_calls=0`. All 18 candidates were
rejected and policy selected event-truth review. That read-only follow-up found
0 blocking issues and 280 review rows, then emitted `stop_completed`; no extra
LLM cycle was warranted.

The next discovery contract replaces hard five-day event flags with
`event_decay(distance, half_life)`. Each proposal must retain a daily
microstructure base signal and add a continuous event interaction, so stocks
without an active event remain in the cross-section. The daily-only run also
forbids `first_hour_*` proposals unless the intraday loader is explicitly
enabled.

That contract has now been exercised and should not be repeated on the current
snapshot. Two bounded runs produced 18 computable candidates: 10 failed IC and
8 failed tail concentration, with no missing-field failures. Deterministic
base/event/composite replay attributed the apparent strength to OFI rather than
the event interaction. The follow-up seven-factor smoothing gauntlet left every
candidate rejected, but identified 10-day and 20-day OFI means as the only
leads worth deterministic attribution: they reduced turnover and improved both
training and global-holdout rank-IC, while still failing the worst-fold tail
gate. Do not spend another LLM cycle on event interactions until the data
fingerprint changes or the fixed-snapshot attribution produces a predeclared
regime hypothesis.

The attribution is complete. It reproduced worst-fold tail ratios 0.775 and
0.945 for 10-day and 20-day OFI and traced both to one March episode whose top
dates are consecutive, overlapping five-day labels. The 31–90-day listing-age
bucket was positive and the 91+ bucket negative, but this is post-hoc evidence,
not a promotion rule. Keep both factors rejected and retain the age pattern for
a future untouched-window test. The episode-aware statistic is now present as
informational metadata only. It reports aggregate non-overlapping top-three
positive shares of 0.470/0.408 for 10/20-day OFI, while the thinnest fold
cohorts contain only 3–4 positive observations. This confirms inadequate
independent episode count and does not relax the current promotion gate.

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
The bounded run stops after this replay for operator inspection. Policy output
separates discovery promotions from replay survivors, so one factor is not
counted twice.

`hk_ipo_event_truth_review` uses the separate `event_truth_audit` executor.
It runs five read-only BigQuery checks for the review backlog, curated source
evidence, implausible curated dates, daily-feature alignment, and per-document
type coverage. Results are written as typed research-task artifacts under
`artifacts/research_tasks/`; query failures fail closed, while data-quality
findings are reported as `blocked` or `review_required`. The post-run policy
consumes the task index and stops the bounded run for inspection.

`hk_ipo_raw_tick_intraday_features` uses the
`raw_tick_materialization_plan` executor. Select it explicitly with:

```bash
make autonomous-researcher-hk-ipo-run \
  ARGS="--topic-id hk_ipo_raw_tick_intraday_features"
```

The task validates the committed SQL contract, dry-runs its SELECT body, and
executes only the read-only nonpositive-tick QA query. The plan is frozen at
2026-06-26 and targets `micro_features_intraday_v1_candidate`, but no table is
created. Since BigQuery dry-run does not fully estimate scans of the external
tick table, the artifact records `cost_estimate_complete=false`.

The candidate write is intentionally a separate operator action. It is not a
Director executor and requires all approval factors in one invocation:

```bash
make plan-hk-ipo-raw-tick-materialization \
  ARGS="--task-id raw-tick-plan-v1"

make materialize-hk-ipo-raw-tick ARGS="\
  --execute \
  --plan-artifact artifacts/research_tasks/raw-tick-plan-v1.json \
  --approve-sql-sha256 <hash-from-plan> \
  --acknowledge-external-scan-cost-unknown \
  --max-bytes-billed <operator-limit>"
```

The execution SQL cannot replace an existing table and sets a seven-day
expiration atomically. A completed write is not accepted until the typed task
report confirms 7,118 rows, 77 stocks, unique stock/date keys, no dates after
2026-06-26, the exact candidate target, and valid expiration metadata.

## What Is Not Fully Automated Yet

- **No scheduler** — an operator starts each run; nothing re-invokes the
  loop on a cadence.
- **Data work stays manual** — the director queues data gaps (refill,
  ingestion, QA) but the executor never performs them.
- **Event studies live outside the loop** — `scripts/analysis/*.py`
  (microstructure OOS, lockup/greenshoe/stabilization event studies) are
  operator-run analyses; the director does not schedule or read them.
- **Raw-tick writes require an operator** — the typed planner and source QA are
  dispatched, while the hash-bound, capped write command remains outside the
  autonomous loop.
  Auction order-book imbalance and quote-recovery speed remain blocked until
  their source fields and deterministic definitions are reviewed.
- **Persistence selection is experimental** — `combine_factors` exposes
  `--selection-strategy persistence --top-k K`, and records that choice in
  its report and promotion trail, including the scoring-formula version. The
  default remains `input_order` until a
  fresh, untouched OOS window validates the policy.
