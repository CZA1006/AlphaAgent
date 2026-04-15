"""Hermes integration boundary — adapter contracts between runtimes.

This package defines the formal boundary between two systems:

    Alpha Harness (this project)
        Owns: schemas, evaluators, registries, orchestrator, data loaders.
        Guarantees: deterministic evaluation, typed failure taxonomy,
        point-in-time correctness, reproducibility.
        Never calls: assemble_prompt, run_agent_step, invoke_tool, or any
        LLM-facing API directly.

    Hermes Agent Runtime (external)
        Owns: LLM orchestration, prompt assembly, tool dispatch, agent
        lifecycle, conversation state.
        Provides: hypothesis generation, natural-language reasoning about
        experiment results, skill synthesis from memory patterns.

Dependency rule (AGENTS.md #8):
    The dependency arrow points INWARD.  Hermes adapts INTO Alpha Harness
    services.  Alpha Harness never imports from Hermes.

    ┌──────────────────────────────────────────────────┐
    │  Hermes Runtime                                  │
    │  (LLM, prompts, tools, agent steps)              │
    │                                                  │
    │    ┌──────────────────────────────────────────┐   │
    │    │  Adapter Layer (this package)            │   │
    │    │  AgentRuntimeAdapter                     │   │
    │    │  MemoryProviderAdapter                   │   │
    │    │  ContextInjectionAdapter                 │   │
    │    └────────────────┬─────────────────────────┘   │
    │                     │ calls into                   │
    │    ┌────────────────▼─────────────────────────┐   │
    │    │  Alpha Harness Services                  │   │
    │    │  AlphaHarnessService                     │   │
    │    │  ResearchOrchestrator                    │   │
    │    │  ExperimentRegistry, MemoryRegistry ...  │   │
    │    └──────────────────────────────────────────┘   │
    └──────────────────────────────────────────────────┘

What lives in this package:
    - Protocol definitions for the three adapter types.
    - Dataclasses for adapter input/output that do NOT depend on Hermes types.
    - Stub implementations that work without Hermes installed.

What does NOT live here:
    - Actual Hermes imports or vendored Hermes code.
    - LLM prompts, tool schemas, or agent step logic.
    - Any code that would make alpha_harness depend on hermes at import time.
"""
