# Hermes Integration Plan

## Strategy

We are following:

**Route C: reuse Hermes runtime substrate and build Alpha Harness separately.**

## Integration philosophy

Do not start by deeply modifying Hermes core.

Instead:
- pin Hermes to a known revision under `vendor/`
- define a narrow runtime boundary
- call Alpha Harness tools and services through that boundary
- keep quant-specific logic in `alpha_harness/`

## What Hermes should provide first
- agent runtime loop entry point
- prompt assembly entry point
- tool invocation path
- session or context plumbing where useful

## What Alpha Harness should provide first
- research orchestrator
- registries
- factor DSL
- evaluators
- data tools
- memory and skill stores

## First integration target
The first integration target is not full autonomy.
It is simply:

- prove the Alpha Harness can be invoked using the Hermes runtime substrate
- prove deterministic outputs are stored and reused

## Integration anti-patterns
Do not:
- push core quant evaluation into generic Hermes skills
- tightly couple Alpha schemas to Hermes internal structures
- fork Hermes deeply before the local MVP works

## Future possibility
If later the Alpha Harness exposes generally useful runtime patterns, those can be considered for upstream-inspired refactoring. Not before the harness itself is validated.
