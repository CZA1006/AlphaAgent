# Rounds 7 – 9 Summary

> Audit-grade, composite-aware extensions on top of the Round-4-to-6
> harness.  Round 7 made strict-validation reports self-contained; 7.1
> fixed a latent measurement gap between validator and combiner; Round 8
> made baskets first-class registry citizens; Round 9 closed the loop
> back into the proposer and made composites refinable + inspectable.

This doc complements
[`docs/ROUND4_TO_6_SUMMARY.md`](ROUND4_TO_6_SUMMARY.md) — read that first
for the strict-regime / gate / trail vocabulary used here.

---

## Round 7 — Strict-validation thumbnails

**Problem.** After a `validate_strict` cycle, the only persisted memory
of *rejected* candidates was the experiment registry — which the
combiner couldn't easily query without re-running the whole cycle.
We needed audit reports that survive across sessions without depending
on the in-memory registry state.

**Change.** Added `FactorThumbnail` to `alpha_harness/reports/validation.py`:
a per-factor record (factor_id, expression, decision, headline metrics,
gate name on reject) embedded in every `StrictValidationReport`.
Downstream tools — notably `scripts/combine_factors --from-validation-
report` — now reload the full set of evaluated factors directly from
`artifacts/validations/*.json`.

**Files:** `alpha_harness/reports/validation.py`, `reports/__init__.py`,
`scripts/combine_factors.py` (loader).

---

## Round 7.1 — Combiner / validator measurement parity

**Problem.** `combine_factors` evaluated factor signals via bare
`compute_mean_ic` / `compute_mean_rank_ic` / `compute_quantile_spread`
over the full window, while `validate_strict` ran the same factors
through `WalkForwardEvaluator` with embargo, sector neutralization,
cost adjustment, and a TAIL holdout.  Same factor, two answers:
observed +0.0215 in a validation report vs −0.0329 in the combiner.

**Change.** Threaded the regime through both per-factor and basket
evaluation in the combiner.  Extracted `evaluate_precomputed_signal()`
as a public function in `evaluators/signal_quality.py` so the basket
signal (no DSL form) can re-use the post-execution pipeline.  Added a
`_PrecomputedSignalEvaluator` adapter wrapped in
`WalkForwardEvaluator(regime.walk_forward_config())` for both
individuals and baskets.

**Result.** Per-factor metrics in the combiner now match validation
report thumbnails byte-for-byte.

**Files:** `evaluators/signal_quality.py`, `scripts/combine_factors.py`.

---

## Round 8 — Composite factors as registry citizens

The Round-6 combination thesis was validated under Round 7.1: the top-3
equal-weight basket of lenient survivors **beat the best individual on
both IC (+0.0295 vs +0.0273) and rank_IC (+0.0400 vs +0.0361)** under
the strict regime.  Round 8 turned that result into infrastructure.

### Phase A — `CombinationReport` artifact

One JSON per basket run, mirroring `StrictValidationReport`'s on-disk
shape (`artifacts/combinations/{cycle_id}.json` + append-only
`_index.jsonl`).  Captures the recipe, regime trail id, basket
thumbnail, per-component thumbnails, average pairwise rank-correlation,
and a `passes_regime` flag.

The `recipe_id` is SHA-256 over `(method, sorted canonical-AST hashes
of components)` — **permuted components collapse to the same id**, so
the novelty check can't be tricked by reordering.  This makes the
report (and later, the registry pointer) idempotent on logical recipe
identity, not syntactic order.

`CombinationRecipe.build()` is the canonical constructor; downstream
callers should never set `recipe_id` directly.

### Phase B — composites as first-class FactorSpecs

Decision: **surgical, not DSL-invasive.**  Added `composite_recipe:
CombinationRecipe | None` as a parallel field on `FactorSpec` and a
one-line dispatch in `SignalQualityEvaluator.evaluate()`:

```python
if factor.composite_recipe is not None:
    signal = execute_composite(factor.composite_recipe, df)
else:
    # ... existing DSL path unchanged
```

Both paths land at `evaluate_precomputed_signal`, so **every Round 4
gate (walk-forward, embargo, holdout, tail concentration, sign
consistency) works for composites for free** — no separate evaluator
stack to maintain.

`combine_factors --promote` writes both a `PromotedArtifact` and a
`PromotionTrail` when the basket clears the regime gates.  The trail
hash incorporates regime knobs (neutralize, cost_bps, walk-forward
config, judge thresholds) so the same recipe under two regimes lands
in two distinct artifacts.

Future iterations of the DSL `combine()` node are deliberately
deferred — the parallel-field approach is ~150 lines vs ~400 for a
parser extension, and we'd rather see real composite-usage patterns
before paying that cost.

---

## Round 9 — Closing the composite loop

### Phase A — Loop closure (the must-have)

**A.1 Memory digest reads the promoted-artifact index.**
`alpha_harness/proposer/memory.py:build_memory_digest` gained an optional
`promoted_index_path` kwarg.  When set (`validate_strict` and
`autonomous_cycle` both pass it now), the digest emits a section like:

```
Recently promoted composites (use these as building blocks, ...):
  - combine.equal_weight([rank(...), rank(...), ...])
      recipe_id=593ca7ddcda1c8d6  (ic=+0.030, rank_ic=+0.033)
```

The composites are sourced from the durable
`artifacts/promoted/_index.jsonl` mirror, not the in-memory
registry — so a basket promoted by yesterday's
`combine_factors --promote` is visible to today's `validate_strict`
cycle **even when the registry is fresh**.  Dedupes by `recipe_id`,
caps at `top_composites` (default 2), defensive on every read.

This is the deliverable that makes Round 8 actually close the agent
loop.  Without it, baskets sat on disk but the proposer was blind to
them.

**A.2 Deterministic composite factor_id.**
`_promote_basket` now pins `FactorSpec.id =
f"composite_{recipe_id}_{trail_id[:6]}"`.  Re-promoting the same recipe
under the same regime overwrites the existing artifact; promotion
under a different regime gets a distinct file.

Fixed a latent `Path` import-shadowing bug in `autonomous_cycle.py`
that surfaced once the new tests exercised the import path.

### Phase B — Composite refinement

`RefinementRunner._expand_composite` mirrors the scalar expand path:

1. **`propose_composite_mutations(recipe, brief)`** (in
   `orchestrator/mutations.py`) holds N−1 components fixed and mutates
   the i-th component via the existing scalar `propose_mutations`
   (window halve/double, wrap/unwrap rank/zscore, …).  Each viable
   inner mutation rebuilds the recipe; no-op mutations (child
   recipe_id == parent recipe_id) are filtered.  Labels carry the
   component index (`component_2:window_double`) so the audit trail
   stays readable.

2. **`service.run_research_cycle(precompiled_factor=...)`** is a new
   kwarg that lets the runner bypass the DSL compiler — composites
   have no DSL string, so there's nothing to parse.  Scalar callers
   leave the kwarg unset and behave exactly as before.

3. **Budget caps unchanged.** `max_depth`, `max_variants_per_step`,
   and `max_total_children` apply to the flattened composite candidate
   list, so a wide basket can't blow through the budget by virtue of
   having many components to mutate.

Out of scope (Round 10+): mutating the *method* itself (equal_weight →
zscore_average), composite-of-composites refinement.

### Phase C — Read-side ergonomics

**`scripts/inspect_composite`** — pure read auditor for promoted
composites.  Two modes:

- `--list`: table of every promoted composite under `--promoted-dir`,
  newest first, with recipe_id / method / n_components / ic / rank_ic /
  promoted_at.
- `--recipe-id <id>`: detailed view — recipe, components, metrics,
  regime trail summary, refinement ancestry walked up via
  `parent_factor_id` until the root.

**`docs/ROUND7_TO_9_SUMMARY.md`** (this file).

---

## What stays out of scope

These are intentionally deferred — each is its own round, not bundled
into the loop-closure work:

- **DSL `combine()` node.**  Right end-state, wrong cost-benefit until
  composite usage patterns are settled.
- **Method-level refinement** (equal_weight → zscore_average).  Today
  only components mutate.
- **Cross-recipe novelty** ("recipe A is too similar to recipe B
  because their components share 4 of 5 ASTs").  Single-recipe-id
  collision is the current bar.
- **Auto-search over basket subsets.**  The "try all `C(N, 3)` and
  pick the best by rank_IC" optimizer is research tooling, not loop
  infrastructure.
- **Proposer prompt-engineering to actively *use* composites.**
  Composites are now in the prompt context; whether the LLM
  productively composes them is an experiment, not an engineering task.
- **ExperimentRegistry hookup that writes composites to a SQL backend
  during `--promote`.**  Cross-session lineage via the durable
  artifact index covers the agent-loop use case; SQL-registry parity
  is a polish task for later.
