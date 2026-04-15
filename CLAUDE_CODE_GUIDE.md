# Claude Code Guide

Claude Code is the primary implementation agent for this repository.

## Main responsibilities
- scaffold code
- implement modules
- add tests
- update documentation when architecture changes
- keep boundaries clear

## Working style

### Preferred behavior
- make small, coherent changes
- keep modules typed
- favor explicit interfaces
- add docstrings where domain concepts matter
- write tests for deterministic behavior

### Avoid
- giant one-shot rewrites
- hiding domain logic inside prompts
- introducing unnecessary abstractions too early
- creating broad dependency sprawl

## First implementation priorities
1. repo structure
2. local environment
3. schemas
4. data loaders
5. factor DSL
6. evaluator
7. registries
8. orchestrator
9. Hermes boundary integration

## Required discipline
- do not mix Hermes internals directly into Alpha Harness code unless necessary
- do not add live trading code in the first milestone
- do not bypass registries for experiment history

## Definition of a good patch
A good patch should:
- improve the working system
- preserve architecture boundaries
- be testable locally
- have clear acceptance criteria

## Documentation expectation
Whenever a new important module is added, update relevant docs if needed.
