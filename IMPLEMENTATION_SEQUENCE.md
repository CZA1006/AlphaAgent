# Implementation Sequence

> **Historical document.** Steps 1–9 below shipped across Rounds 1–3.
> For the current state and Round 4 sequencing, see
> [docs/ROUND3_SUMMARY.md](docs/ROUND3_SUMMARY.md) and the Round 4
> section of [ROADMAP.md](ROADMAP.md).

## Step 1
Initialize repo and Python project.

Expected result:
- importable package layout
- local tooling works

## Step 2
Add local infrastructure.

Expected result:
- Postgres starts via Docker Compose
- environment variables are documented

## Step 3
Define schemas.

Expected result:
- core entities are typed and reusable

## Step 4
Add data loaders and local persistence.

Expected result:
- equity and crypto sample data can be stored locally

## Step 5
Implement factor DSL and executor.

Expected result:
- one example factor runs deterministically

## Step 6
Implement evaluator.

Expected result:
- factor gets scored using deterministic metrics

## Step 7
Implement experiment registry.

Expected result:
- experiment records can be persisted and queried

## Step 8
Implement minimal research orchestrator.

Expected result:
- a single research cycle runs end-to-end

## Step 9
Wire the Hermes runtime boundary.

Expected result:
- Hermes can serve as runtime substrate without absorbing quant logic
