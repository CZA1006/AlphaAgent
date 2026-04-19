# AlphaAgent

AlphaAgent is a quant-native research system built on top of the Hermes runtime substrate.

The goal is **not** to build a generic agent that imitates a human analyst step by step. The goal is to build a **self-improving alpha research harness** that can:

- propose research hypotheses
- translate them into safe factor specifications
- run deterministic evaluation
- store experiment history
- classify failures
- distill reusable research skills
- improve its own research efficiency over time

## Initial market focus

Phase 1 focuses on:

- US equities
- Crypto spot and perp markets

The data model should still be designed to support additional asset classes later, including:

- ETFs
- futures
- options metadata
- FX
- rates
- commodities

## Core principle

**Hermes handles runtime orchestration. Alpha Harness handles quant reasoning.**

This repository should keep those responsibilities clearly separated.

## What we are building first

We are building a research MVP, not a live trading system.

The first goal is to make the following loop work end-to-end:

1. ingest market data
2. define a hypothesis
3. compile it into a safe factor spec
4. run deterministic evaluation
5. store the experiment
6. classify failure or promote candidate
7. write memory and reusable learnings

## What success looks like for MVP

The MVP is successful when the system can:

- ingest US equity bars and crypto OHLCV into local storage
- define and execute a small safe factor DSL
- evaluate factor quality with deterministic metrics
- log experiments into registries
- retrieve related past experiments
- let the research orchestrator use that context in the next cycle

## Round 3 capabilities (what works today)

**Round 3 is complete.** For the full accomplishments / limitations /
Round-4 scope writeup, see [docs/ROUND3_SUMMARY.md](docs/ROUND3_SUMMARY.md).

The research loop now runs end-to-end from a free-form research theme:

- **Canonical AST novelty** — structural DSL equality; rename / whitespace
  variants collapse to the same form.
- **Related experiment retrieval** — prior experiments feed back into new
  cycles by canonical AST + evaluation-profile match.
- **LLM provider layer** (`alpha_harness/llm/`) — typed `LLMClient`
  protocol, `OpenRouterClient`, `MockLLMClient`, env-loaded config.

- **Hypothesis proposer** — `HypothesisProposer` turns a theme into a
  bounded list of DSL-validated candidates via schema-constrained LLM
  calls. Invalid expressions are dropped with the exact compiler error,
  never re-emitted into the loop.
- **Bounded refinement** — `RefinementRunner` expands REFINE-verdict
  experiments with deterministic mutation templates (window scaling,
  wrap/unwrap `rank`/`zscore`, unwrap outer) under hard budgets, and
  novelty-checks every child against root + siblings.
- **Lineage memory** — every cycle writes a compact `MemoryEntry` to the
  memory registry so experiment graphs can be walked without extra
  bookkeeping.
- **Configurable persistence** — `memory` (default) or `sql` via
  `--backend sql` / `ALPHA_AGENT_BACKEND=sql`. All orchestrators are
  typed against registry protocols — no backend branching in business
  logic.
- **Hermes-facing adapter** — `HarnessAgentAdapter` composes the above
  behind the `ResearchCycleRequest` / `ThemeCycleRequest` boundary
  contracts. The adapter never overrides deterministic decisions; it only
  arranges calls.
- **Autonomous cycle script** — `scripts/autonomous_cycle.py` drives the
  full theme → proposals → cycles → auto-refine → summary path. Pass
  `--mock-llm` for a hermetic no-key local run:

  ```bash
  uv run python -m scripts.autonomous_cycle --mock-llm --n-candidates 3
  ```

### Still deferred (Round 4 and beyond)

- LLM-driven mutation suggestions (current mutations are syntactic only)
- Skill distillation and reuse across cycles
- Sector / beta neutralization in evaluator; multi-horizon labels
- Persistent `FactorRegistry` / `SkillRegistry` — still in-process
- Hermes actually driving the adapter from a live agent loop
- Token / rate-limit budgets on LLM calls
- Live trading execution, multi-agent debate, cloud-native deployment

## Persistence backends

Registries (experiments, hypotheses, lineage memory) run against one of two
backends:

- `memory` — default, zero setup, used by every test and local run.
- `sql` — Postgres-backed, opt-in via `--backend sql` or
  `ALPHA_AGENT_BACKEND=sql`.

See [docs/BACKENDS.md](docs/BACKENDS.md) for selection rules, Postgres
prerequisites, and the boundary contract business logic relies on.

## Running locally with real APIs

For wiring real OpenRouter / Polygon / Postgres keys into the stack — and
the `make doctor` preflight that validates them — see
[docs/LOCAL_TESTING.md](docs/LOCAL_TESTING.md).  Short version:

```bash
cp .env.example .env && $EDITOR .env
make doctor && make run-mock          # baseline, no keys
make doctor-real && make run-real     # real LLM, synthetic data
```

## What not to do yet

Do not prioritize these in the first milestone:

- live trading execution
- complex multi-agent debate
- high-frequency order-book strategy logic
- cloud-native production deployment
- UI polish
- broad market coverage before the core loop works
