"""Novelty evaluator — checks whether a factor is structurally distinct.

Upgrades Round 1's exact-string comparison to canonical-AST comparison
backed by the DSL parser.  Two factors that differ only in whitespace or
commutative operand order compare equal; two factors that differ only in
a window size register as near-duplicates via a Jaccard similarity score.

The evaluator can be seeded with any combination of:
    * a static ``existing_expressions`` list (tuples of ``(id, expression)``)
    * an ``ExperimentRegistry`` whose records supply live comparisons

Correlation-based novelty (return-series cosine similarity) is intentionally
out of scope for this round.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from alpha_harness.factors.canonical import CanonNode, canon_similarity, canonicalize
from alpha_harness.factors.dsl_parser import DslParseError, parse_expression
from alpha_harness.registries.protocols import ExperimentRegistryProtocol
from alpha_harness.schemas.factor import FactorSpec


@dataclass(frozen=True)
class NoveltyVerdict:
    """Result of a novelty check."""

    is_novel: bool
    similarity_score: float  # 0.0 = completely unique, 1.0 = exact duplicate
    most_similar_factor_id: str | None  # id of the closest existing factor
    detail: str  # human-readable explanation


# An existing-factor record as used internally: id, raw expression, canonical form.
_Existing = tuple[str, str, CanonNode | None]


class NoveltyEvaluator:
    """Check whether a factor is sufficiently distinct from known factors.

    The similarity between two factors is either ``1.0`` (canonical forms
    match) or a weighted Jaccard on ``(node_type, name)`` multisets of the
    canonical trees.  When a candidate cannot be canonicalized (e.g. the
    expression fails to parse), the evaluator falls back to whitespace-
    insensitive string equality so the check still behaves deterministically.

    Parameters
    ----------
    existing_expressions:
        Static list of ``(factor_id, expression)`` tuples.  Useful in tests
        and in the absence of a persisted registry.
    similarity_threshold:
        Factors with similarity *at or above* this value are flagged as
        duplicates (``is_novel=False``).
    experiment_registry:
        Optional registry consulted on every ``check_novelty`` call so the
        comparison set reflects newly-recorded experiments without the
        evaluator having to be re-instantiated.
    """

    def __init__(
        self,
        existing_expressions: list[tuple[str, str]] | None = None,
        similarity_threshold: float = 0.85,
        experiment_registry: ExperimentRegistryProtocol | None = None,
    ) -> None:
        self._threshold = similarity_threshold
        self._registry = experiment_registry

        self._static: list[_Existing] = []
        for factor_id, expression in existing_expressions or []:
            self._static.append(
                (factor_id, expression, _try_canonicalize_expression(expression))
            )

    # ── Public API ────────────────────────────────────────────────────────

    def check_novelty(self, factor: FactorSpec) -> NoveltyVerdict:
        """Evaluate whether ``factor`` is sufficiently novel."""
        candidate_canon = self._canonicalize_factor(factor)

        best_score = 0.0
        best_id: str | None = None
        found_any = False

        for factor_id, expression, canon in self._iter_existing():
            found_any = True
            score = _score(
                factor.expression, candidate_canon, expression, canon
            )
            if score > best_score:
                best_score = score
                best_id = factor_id
                if best_score >= 1.0:
                    break

        if not found_any:
            return NoveltyVerdict(
                is_novel=True,
                similarity_score=0.0,
                most_similar_factor_id=None,
                detail="No existing factors to compare against.",
            )

        is_novel = best_score < self._threshold
        return NoveltyVerdict(
            is_novel=is_novel,
            similarity_score=round(best_score, 4),
            most_similar_factor_id=best_id,
            detail=(
                f"Most similar factor: {best_id} (score={best_score:.4f}). "
                f"{'Novel — below' if is_novel else 'Duplicate — above'} "
                f"threshold {self._threshold}."
            ),
        )

    # ── Internals ─────────────────────────────────────────────────────────

    def _iter_existing(self) -> Iterator[_Existing]:
        """Yield static entries followed by live registry entries."""
        yield from self._static
        if self._registry is not None:
            yield from _registry_entries(self._registry)

    @staticmethod
    def _canonicalize_factor(factor: FactorSpec) -> CanonNode | None:
        """Canonicalize a factor using its stored AST if available.

        Composite factors (Round 8) have no DSL AST — their expression is
        a synthetic ``<composite:{recipe_id}>`` placeholder.  We return
        ``None`` so the score fallback drops to string equality, which
        does the right thing automatically: two composites with the same
        ``recipe_id`` share the same placeholder string → similarity 1.0
        → flagged as a duplicate.  Two composites with different recipe
        ids have different placeholders → similarity 0.0.  Composites
        and scalar factors never collide because the placeholder won't
        equal any DSL expression.
        """
        if factor.composite_recipe is not None:
            return None  # forces string-equality fallback, see docstring
        if factor.operator_tree is not None:
            try:
                return canonicalize(factor.operator_tree)
            except (ValueError, KeyError, TypeError):
                pass
        return _try_canonicalize_expression(factor.expression)


# ── Module-level helpers ─────────────────────────────────────────────────────


def _try_canonicalize_expression(expression: str) -> CanonNode | None:
    """Parse+canonicalize an expression; return ``None`` on any failure."""
    try:
        return canonicalize(parse_expression(expression))
    except (DslParseError, ValueError, KeyError, TypeError):
        return None


def _registry_entries(registry: ExperimentRegistryProtocol) -> Iterable[_Existing]:
    """Yield ``(id, expression, canonical)`` for each experiment record."""
    for record in registry.list_all():
        factor = record.factor
        canon: CanonNode | None = None
        if factor.operator_tree is not None:
            try:
                canon = canonicalize(factor.operator_tree)
            except (ValueError, KeyError, TypeError):
                canon = None
        if canon is None:
            canon = _try_canonicalize_expression(factor.expression)
        # Prefer the stable factor name over the experiment record id — it's
        # the user-facing handle for the factor.
        yield (factor.name, factor.expression, canon)


def _score(
    candidate_expr: str,
    candidate_canon: CanonNode | None,
    other_expr: str,
    other_canon: CanonNode | None,
) -> float:
    """Compute similarity with an automatic fallback to string equality."""
    if candidate_canon is not None and other_canon is not None:
        if candidate_canon == other_canon:
            return 1.0
        return canon_similarity(candidate_canon, other_canon)
    # Fallback: whitespace-insensitive string equality.
    if candidate_expr.strip() == other_expr.strip():
        return 1.0
    return 0.0
