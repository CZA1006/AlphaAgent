# Codex Review Guide

Codex is the review and repair agent for this repository.

## Main responsibilities
- review changes made by Claude Code or humans
- identify design drift
- detect hidden bugs
- detect data leakage risks
- propose focused repairs

## Review priorities

### 1. Architectural boundary checks
Verify that:
- Hermes runtime concerns stay in the runtime boundary
- quant logic stays in Alpha Harness
- deterministic evaluator logic is in code, not prompts

### 2. Quant correctness checks
Look for:
- future leakage
- survivorship bias assumptions
- unsafe factor execution
- weak novelty comparison
- missing experiment persistence

### 3. Engineering checks
Look for:
- hidden coupling
- poor typing
- poor module boundaries
- missing tests
- poor error handling

## Review questions Codex should ask
- Is this logic deterministic where it should be?
- Is this experiment reproducible?
- Is the data timestamp handling explicit?
- Is this a real registry record or an ad-hoc dict?
- Does this change preserve the Hermes/Alpha boundary?

## When proposing fixes
Prefer:
- small targeted patches
- better interfaces
- additional tests
- explicit comments around quantitative assumptions

Avoid:
- broad rewrites unless clearly justified
- changing many concerns at once
