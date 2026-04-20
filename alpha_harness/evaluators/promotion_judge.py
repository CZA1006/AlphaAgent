"""Promotion judge — decides whether a factor is promoted, refined, or rejected.

Implements the ExperimentJudge protocol. Checks data sufficiency, profile
thresholds, novelty, and margin in sequence. All inputs arrive through the
method signature — no ambient state.
"""

from __future__ import annotations

from alpha_harness.evaluators.novelty import NoveltyEvaluator
from alpha_harness.schemas.evaluation import (
    EvaluationBundle,
    EvaluationProfile,
    EvaluationRequest,
)
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    FailureCategory,
    FailureRecord,
    JudgmentDetail,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis


class PromotionJudge:
    """Decide whether an evaluated factor should be promoted, refined, or rejected.

    Implements the ``ExperimentJudge`` protocol from ``service.py``.

    Parameters
    ----------
    novelty_evaluator:
        Novelty checker. If None, a default (empty) one is used.
    refine_margin:
        When a metric passes its threshold by less than this relative margin,
        the factor is sent to REFINE instead of PROMOTE. Default 0.20 means
        a metric must exceed its threshold by at least 20% to promote.
    """

    def __init__(
        self,
        novelty_evaluator: NoveltyEvaluator | None = None,
        refine_margin: float = 0.20,
    ) -> None:
        self._novelty = novelty_evaluator or NoveltyEvaluator()
        self._refine_margin = refine_margin

    def judge(
        self,
        hypothesis: Hypothesis,
        factor: FactorSpec,
        evaluation: EvaluationBundle,
        request: EvaluationRequest,
    ) -> JudgmentDetail:
        """Apply promotion logic and return a :class:`JudgmentDetail`.

        Order of checks:
            1. Data sufficiency (n_periods, n_assets vs profile).
            2. Profile pass (required metrics present and above thresholds).
            3. Novelty (factor not too similar to existing factors).
            4. Margin check (borderline metrics → REFINE rather than PROMOTE).
        """
        profile = request.profile

        # ── 1. Data sufficiency ────────────────────────────────────────
        failure = self._check_data_sufficiency(evaluation, profile)
        if failure is not None:
            return JudgmentDetail(
                decision=ExperimentDecision.REJECT,
                failure=failure,
                notes="Insufficient data coverage.",
            )

        # ── 2. Profile pass ────────────────────────────────────────────
        failure = self._check_profile(evaluation, profile)
        if failure is not None:
            return JudgmentDetail(
                decision=ExperimentDecision.REJECT,
                failure=failure,
                notes="Signal quality below required thresholds.",
            )

        # ── 2b. Multi-horizon sign consistency ─────────────────────────
        failure = self._check_sign_consistency(evaluation)
        if failure is not None:
            return JudgmentDetail(
                decision=ExperimentDecision.REJECT,
                failure=failure,
                notes="IC sign flipped across forecast horizons.",
            )

        # ── 3. Novelty ────────────────────────────────────────────────
        verdict = self._novelty.check_novelty(factor)
        if not verdict.is_novel:
            return JudgmentDetail(
                decision=ExperimentDecision.REJECT,
                failure=FailureRecord(
                    category=FailureCategory.DUPLICATE,
                    detail=verdict.detail,
                ),
                notes=f"Too similar to factor {verdict.most_similar_factor_id}.",
            )

        # ── 4. Margin check → REFINE vs PROMOTE ──────────────────────
        if self._is_borderline(evaluation, profile):
            return JudgmentDetail(
                decision=ExperimentDecision.REFINE,
                notes="Metrics pass but are within the refine margin.",
            )

        return JudgmentDetail(
            decision=ExperimentDecision.PROMOTE_CANDIDATE,
            notes="All checks passed — promoting.",
        )

    # ── Internal helpers ──────────────────────────────────────────────────

    def _check_data_sufficiency(
        self, evaluation: EvaluationBundle, profile: EvaluationProfile
    ) -> FailureRecord | None:
        """Reject if observed data coverage is below profile minimums."""
        if (
            evaluation.n_periods is not None
            and evaluation.n_periods < profile.min_periods
        ):
            return FailureRecord(
                category=FailureCategory.DATA_INSUFFICIENT,
                detail=(
                    f"n_periods={evaluation.n_periods} "
                    f"< min_periods={profile.min_periods}"
                ),
            )
        if (
            evaluation.n_assets is not None
            and evaluation.n_assets < profile.min_assets
        ):
            return FailureRecord(
                category=FailureCategory.DATA_INSUFFICIENT,
                detail=(
                    f"n_assets={evaluation.n_assets} "
                    f"< min_assets={profile.min_assets}"
                ),
            )
        return None

    def _check_profile(
        self, evaluation: EvaluationBundle, profile: EvaluationProfile
    ) -> FailureRecord | None:
        """Reject if any required metric is missing or below threshold."""
        for metric in profile.required_metrics:
            value = getattr(evaluation, metric.value, None)
            if value is None:
                return FailureRecord(
                    category=FailureCategory.WEAK_SIGNAL,
                    detail=f"Required metric '{metric.value}' is missing.",
                )
            threshold = profile.thresholds.get(metric.value)
            if threshold is not None and value < threshold:
                return FailureRecord(
                    category=FailureCategory.WEAK_SIGNAL,
                    detail=(
                        f"{metric.value}={value:.4f} "
                        f"< threshold={threshold:.4f}"
                    ),
                )
        return None

    def _check_sign_consistency(
        self, evaluation: EvaluationBundle
    ) -> FailureRecord | None:
        """Reject multi-horizon evaluations whose IC sign isn't robust.

        When the evaluator computed IC across multiple horizons (indicated
        by ``metadata["ic_by_horizon"]``), require at least two horizons to
        share the sign of the primary horizon's IC.  Absent that metadata,
        this check is a no-op — single-horizon behaviour is unchanged.
        """
        horizons = evaluation.metadata.get("ic_by_horizon")
        if not isinstance(horizons, dict) or len(horizons) < 2:
            return None
        same_sign = evaluation.metadata.get("ic_sign_consistent_horizons")
        if not isinstance(same_sign, int):
            return None
        if same_sign < 2:
            return FailureRecord(
                category=FailureCategory.WEAK_SIGNAL,
                detail=(
                    f"ic_sign_consistent_horizons={same_sign} "
                    f"across {len(horizons)} horizons (need >= 2)."
                ),
            )
        return None

    def _is_borderline(
        self, evaluation: EvaluationBundle, profile: EvaluationProfile
    ) -> bool:
        """Check if any required metric is within the refine margin of its threshold."""
        for metric in profile.required_metrics:
            value = getattr(evaluation, metric.value, None)
            threshold = profile.thresholds.get(metric.value)
            if value is not None and threshold is not None and threshold > 0:
                margin = (value - threshold) / threshold
                if margin < self._refine_margin:
                    return True
        return False
