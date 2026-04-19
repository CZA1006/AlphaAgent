# Round 3 Completion Summary

**Status: complete.** Round 4 is the next active phase.

This document is the single source of truth for what Round 3 delivered,
what is genuinely working, and what is deliberately still open. Older
milestone docs (`TASKS.md`, `IMPLEMENTATION_SEQUENCE.md`,
`ACCEPTANCE_CRITERIA.md`) describe earlier waves and are retained as
historical reference — when they disagree with this file, **this file
wins**.

---

## 1. What Round 3 set out to do

Move AlphaAgent from "one hand-written factor evaluated end-to-end" (the
Round 2 MVP) to "a theme becomes N LLM-proposed, DSL-validated candidates
that are scored, judged, and optionally refined, with durable lineage."

The architecture goal: keep the LLM strictly on the *proposal* side of
the boundary, and keep all quantitative truth deterministic.

---

## 2. What now genuinely works

### Research loop

- **Canonical AST novelty** (`alpha_harness/factors/canonical.py`) —
  structural equality of DSL expressions, not string equality. Variants
  that differ only by alias or whitespace collapse to the same canonical
  form and are rejected as duplicates.
- **Related experiment retrieval** (`alpha_harness/retrieval/`) — the
  orchestrator queries prior experiments by canonical AST similarity +
  evaluation-profile match so context can be reused across cycles.
- **Hypothesis proposer** (`alpha_harness/proposer/`) — takes a free-form
  theme, issues a schema-constrained JSON request to an `LLMClient`, and
  DSL-compiles every candidate before it can reach the research loop.
  Invalid candidates are dropped with the exact compiler error; a single
  bounded repair round is allowed.
- **Controlled refinement loop** (`alpha_harness/orchestrator/refinement.py`)
  — expands `REFINE`-verdict experiments using deterministic mutation
  templates (window scaling, wrap/unwrap `rank` / `zscore`, unwrap outer)
  under hard budgets (`max_depth`, `max_variants_per_step`,
  `max_total_children`). Every child is novelty-checked against root +
  siblings.
- **Lineage memory** (`alpha_harness/memory/`) — every cycle writes a
  compact `MemoryEntry` so parent/child graphs can be reconstructed from
  the registry alone.

### Persistence

- **Configurable SQL-backed path** — `ALPHA_AGENT_BACKEND=sql` or
  `--backend sql` swaps the three core registries (experiments,
  hypotheses, memory) to Postgres. All business logic is typed against
  registry protocols; no `if backend == "sql"` branching outside
  `registries/factory.py`. See [BACKENDS.md](BACKENDS.md).

### LLM provider layer

- **`alpha_harness/llm/`** — typed `LLMClient` protocol, immutable
  `OpenRouterConfig` loader (raises `LLMConfigError` on missing key),
  `OpenRouterClient` for real calls, and `MockLLMClient` for hermetic
  tests. The proposer is the only component that talks to it.

### Hermes-facing adapter

- **`HarnessAgentAdapter`** (`alpha_harness/hermes_boundary/`) composes
  proposer + orchestrator + refinement runner behind
  `ResearchCycleRequest` and `ThemeCycleRequest` contracts. The adapter
  never overrides deterministic decisions — it only arranges calls.

### Autonomous demo

- **`scripts/autonomous_cycle.py`** — theme → proposals → cycles →
  auto-refine → summary. Supports `--mock-llm` (hermetic) and
  `--data-source {synthetic,parquet,polygon}`.
- **`scripts/doctor.py`** — preflight that redacts values and tells you
  which `make run-*` target is safe to invoke.

### Real local-testing path

Documented and exercised end-to-end:

- `.env.example` covers OpenRouter, Polygon, Postgres, and mode
  selection.
- `Makefile` auto-loads `.env` and exposes:
  `make doctor{,-mock,-real,-sql}`,
  `make run-{mock,real,real-data,real-sql}`,
  `make autonomous-{mock,real}`.
- [LOCAL_TESTING.md](LOCAL_TESTING.md) walks the five-rung ladder from
  mock to full real stack.

A real end-to-end run on 2024-07-01..2024-12-31 Polygon bars for
AAPL/MSFT/GOOG/NVDA/META succeeded: Claude Sonnet 4.6 produced three
schema-valid DSL factors, the evaluator scored them on real data, and
all three were correctly *rejected* (IC in [-0.11, 0.02]) — the harness
did not launder a weak factor into a pass.

---

## 3. Limitations discovered during real local testing

These are observations from the real-API run, not TODOs implied to land
in Round 3. They shape Round 4 priorities.

- **Polygon free tier: 5 requests/minute** — autonomous runs with >5
  symbols in one call hit HTTP 429. Free tier also restricts aggregates
  to roughly the last two years. Workarounds today: fewer symbols, or
  batch-ingest into Parquet and point the loader at `--data-source
  parquet`.
- **Small-universe statistical floor** — 5 names × 6 months of daily
  bars is ≈125 cross-sectional observations. IC estimates are noisy at
  that scale; a 2% IC threshold is a blunt but honest gate. A usable
  research universe is ≥50 names × ≥12 months.
- **LLM-driven mutation is still syntactic only.** Refinement expands
  with deterministic templates; the LLM is not yet invited to propose
  structural variants when a root hits `REFINE`.
- **OpenRouter model slugs drift.** The old `anthropic/claude-3.5-sonnet`
  slug was retired mid-session; the default is now
  `anthropic/claude-sonnet-4.6`. The config path surfaces 404s clearly,
  but there is no automatic fallback.
- **No cost/rate-limit caps in code.** Token spend and API pacing are
  the operator's responsibility for now.

---

## 4. What is still placeholder or deferred

| Area | State |
|------|-------|
| Live trading / order execution | Not started (by design). |
| Multi-agent debate / coordination | Not started (by design). |
| Cloud-native deployment | Not started (by design). |
| LLM-guided refinement mutations | Deferred to Round 4. |
| Persistent `FactorRegistry` / `SkillRegistry` | Still in-process. |
| Automatic retry / cost caps on LLM calls | Manual for now. |
| Scheduled / automated market-data ingestion | Ad-hoc only. |
| Hermes runtime actually calling the adapter from a live agent loop | Adapter exists; the driving runtime is Round 4. |
| Sector / beta neutralization in evaluator | Not yet. |
| Multi-horizon label set | Single fixed horizon. |

---

## 5. Production-ready vs local-demo-ready vs deferred

**Production-ready (safe to build on without redesign):**

- Core schemas, DSL compiler, deterministic executor, evaluator, judge.
- Registry protocols + memory/sql factories.
- Canonical AST novelty and related-experiment retrieval.
- `HarnessAgentAdapter` boundary contracts.

**Local-demo-ready (works, but calibrated for dev machines):**

- Autonomous cycle script against real OpenRouter + Polygon.
- Doctor preflight and Makefile targets.
- SQL backend via docker-compose Postgres.

**Deferred to Round 4+:**

- Everything in §4.

---

## 6. What Round 4 should focus on

Round 4 is about closing the *learning* loop, not re-opening Round 3.
The current loop proposes and scores; it does not yet learn from what it
proposed.

Suggested priorities, in rough order:

1. **LLM-guided refinement.** When a root experiment returns `REFINE`,
   feed the evaluation + failure taxonomy back to the LLM and let it
   propose structural variants (still DSL-validated, still novelty-
   checked, still budget-bound).
2. **Skill distillation prototype.** Turn clusters of promoted or
   narrowly-refined experiments into reusable `Skill` entries that the
   proposer can condition on next time.
3. **Richer evaluator.** Sector / beta neutralization, multi-horizon
   labels, turnover and cost sensitivity — existing schemas already
   reserve fields for these.
4. **Proper universe.** Parquet backfill scripts for a ≥50-name US
   equity panel so cycles run on statistically meaningful data without
   Polygon rate-limit gymnastics.
5. **Hermes drives the adapter.** Wire an actual Hermes agent loop to
   call `HarnessAgentAdapter.run_theme` — the boundary contracts are
   ready; the caller is not.
6. **Cost / rate-limit guards.** Token budget per cycle, backoff on 429,
   structured LLM call logging.

**Out of scope for Round 4:** live trading, cloud deployment, multi-agent
debate, expanding the DSL surface, adding new LLM providers.

---

## 7. Manual verification checklist before tagging Round 3

- `make check` passes (`ruff`, `mypy`, unit tests).
- `make run-mock` succeeds on a clean clone with no keys.
- `make doctor-real` passes once `.env` has an `OPENROUTER_API_KEY`.
- `.env` is not staged (`git status` should be clean of it).
- OpenRouter + Polygon keys used during testing have been rotated.
