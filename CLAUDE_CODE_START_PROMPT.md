# Claude Code Start Prompt

Use this as the initial high-level brief for Claude Code.

---

You are working in the `AlphaAgent` repository.

This project builds a self-improving quant research harness on top of a Hermes runtime substrate.

Read these files first and follow them closely:
- `AGENTS.md`
- `ARCHITECTURE.md`
- `ROADMAP.md`
- `TASKS.md`
- `ACCEPTANCE_CRITERIA.md`
- `REPO_STRUCTURE.md`
- `HERMES_INTEGRATION_PLAN.md`
- `IMPLEMENTATION_SEQUENCE.md`
- `DATA_PLAN.md`

Your current mission is to implement Milestone 1: a **local research MVP**.

Important rules:
- deterministic quantitative logic must live in code, not only prompts
- Hermes runtime and Alpha Harness must remain separated
- use Python 3.11 and `uv`
- start with local-only infrastructure
- do not add live trading execution
- do not add unrestricted code execution from model outputs
- all experiments must be persisted

Work in small, testable steps.

Your first concrete objectives are:
1. initialize the project structure
2. add local Postgres via Docker Compose
3. define core schemas
4. implement sample data loaders for equities and crypto
5. implement a minimal safe factor DSL and executor
6. implement deterministic evaluators for IC, RankIC, and quantile spread
7. implement experiment registry persistence
8. implement a minimal research orchestrator
9. create a thin Hermes runtime integration boundary

For each step:
- explain what files you will add or modify
- implement the code
- add or update tests
- summarize what is complete and what remains

Do not over-engineer. Prioritize a working deterministic research loop.

---
