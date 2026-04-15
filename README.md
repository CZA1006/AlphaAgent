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

## What not to do yet

Do not prioritize these in the first milestone:

- live trading execution
- complex multi-agent debate
- high-frequency order-book strategy logic
- cloud-native production deployment
- UI polish
- broad market coverage before the core loop works
